"""Session avec la pédale : requêtes, délais, nouveaux essais et règles de sécurité.

Tout ce qui part vers la pédale passe par Session._emettre. La fenêtre et la ligne de
commande n'ont pas d'autre chemin vers le port.

Sont ouverts : les lectures, l'annonce, les réglages de la liste fermée, et l'écriture
d'une IR par la seule méthode ecrire_slot, qui archive le slot avant, pose un journal, et
relit après. Une trame d'écriture émise hors de cette méthode est refusée.
"""

import contextlib
import hashlib
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from optima_air import codec, journal, messages
from optima_air import messages_emission as emission
from optima_air import messages_reception as reception
from optima_air.erreurs import (
    ArchiveAbimee,
    DelaiDepasse,
    EcritureInterrompue,
    ErreurBornes,
    ErreurOptimaAir,
    ErreurReponse,
    FirmwareInconnu,
    FonctionALEssai,
)
from optima_air.messages import Tableau
from optima_air.messages_emission import Categorie, EtapeUpload, Trame
from optima_air.messages_reception import Etat, InfoSlot, Statut
from optima_air.midi import Evenement, SysEx
from optima_air.store import Magasin
from optima_air.transport import Transport

FIRMWARE_VERIFIE = 0x17
DELAI_S = 1.0
ENVOIS_PAR_LECTURE = 3  # un envoi et deux nouveaux essais
LECTURES_DE_SLOT = 2  # une lecture, et une relecture si l'identifiant ne concorde pas
PAS_S = 0.05
SILENCE_AVANT_RELECTURE_S = 0.5
PATIENCE_SILENCE_S = 5.0
LONGUEUR_REPONSE_ANNONCE = 14
PAUSE_APRES_REGLAGE_S = 0.05
RELECTURES_APRES_REGLAGE = 5
MASQUE_IDENTIFIANT = 0x7FFFFFFF  # le bit 31 marque une IR d'usine, pas les données
DELAI_BILAN_S = 2.0  # attente des derniers accusés après la dernière trame d'un upload
SILENCE_APRES_ECHEC_S = 35.0
NB_ACCUSES_DE_BLOC = codec.NB_BLOCS + 1  # les 84 blocs et le bloc de fin
NB_ACCUSES_DE_CADRE = 2  # l'en-tête et le pied
STATUTS_DE_CADRE = frozenset({Statut.OK, Statut.PAQUET_FINI, Statut.FLASH_OK})


def _maintenant() -> datetime:
    return datetime.now().astimezone()


@dataclass(frozen=True)
class Connexion:
    """Ce que la séquence de connexion a lu."""

    firmware: int
    volumes: tuple[int, ...]
    low_cuts: tuple[int, ...]
    high_cuts: tuple[int, ...]
    etat: Etat
    slots: tuple[InfoSlot, ...]


@dataclass(frozen=True)
class LectureSlot:
    """Un slot lu : ce que la pédale annonce, ce qu'elle a rendu, et l'identifiant recalculé.

    Les sommes du protocole ne voient pas toutes les altérations, et la réponse à une
    lecture d'IR ne dit pas de quel slot elle vient : l'identifiant est le contrôle fort.
    Un écart n'est pas une erreur (un slot peut porter une étiquette qui ne correspond
    pas à ses données) : c'est à l'appelant de le signaler.
    """

    infos: InfoSlot
    donnees: bytes
    identifiant_calcule: int

    @property
    def concorde(self) -> bool:
        return (self.infos.identifiant & MASQUE_IDENTIFIANT) == self.identifiant_calcule


@dataclass(frozen=True)
class ResultatEcriture:
    """Une écriture d'IR menée à bout : l'archive du contenu d'avant, et le slot relu."""

    archive: Path
    lecture: LectureSlot


@dataclass(frozen=True)
class Sauvegarde:
    """Une sauvegarde complète. `discordants` : les slots dont l'identifiant annoncé par la
    pédale ne correspond pas aux données lues, même après relecture (archivés quand même)."""

    archive: Path
    discordants: tuple[int, ...]


@dataclass(frozen=True)
class BilanRestauration:
    """Ce qu'a fait la restauration d'une archive, slot par slot."""

    reecrits: tuple[int, ...]  # IR, nom ou identifiant différaient : le slot a été réécrit
    regles: tuple[int, ...]  # seuls volume, coupe-bas ou coupe-haut différaient
    inchanges: tuple[int, ...]  # déjà identiques à l'archive : rien n'a été émis


@dataclass(frozen=True)
class Constat:
    """Ce qu'on trouve dans le slot d'une écriture interrompue.

    issue : « aboutie » (le slot contient ce qu'on voulait écrire), « inchange » (il contient
    toujours ce qu'il y avait avant), ou « altere » (ni l'un ni l'autre).
    """

    issue: str
    slot: int
    archive: Path


class Session:
    """Une session n'est pas sûre entre fils d'exécution : un seul fil la pilote."""

    def __init__(
        self,
        transport: Transport,
        magasin: Magasin,
        horloge: Callable[[], float] = time.monotonic,
        maintenant: Callable[[], datetime] = _maintenant,
    ) -> None:
        self._transport = transport
        self._magasin = magasin
        self._horloge = horloge
        self._maintenant = maintenant
        self._journal_consulte = False
        self._tampon: deque[Evenement] = deque()
        self.firmware: int | None = None
        self.firmware_debloque = False
        self.annonce_emise = False
        self._ecriture_ouverte = False
        self._silence_jusqu_a = float("-inf")
        self.non_sollicites: deque[Evenement] = deque(maxlen=4096)

    @property
    def magasin(self) -> Magasin:
        """Le dossier de données de cette session."""
        return self._magasin

    @property
    def transport(self) -> Transport:
        """Le port, pour lire ses compteurs de diagnostic. Ne jamais écrire dessus."""
        return self._transport

    # --- émission : le seul chemin vers le port ---

    def _autres_que_lecture_permis(self) -> bool:
        return self.firmware == FIRMWARE_VERIFIE or (
            self.firmware is not None and self.firmware_debloque
        )

    def _emettre(self, trame: Trame, *, essai: bool = False) -> None:
        emission.controler_trame(trame)
        if not self._journal_consulte:
            # Avant la toute première émission : une écriture interrompue lors d'un
            # lancement précédent impose-t-elle encore le silence ?
            en_cours = journal.lire(self._magasin.racine)  # illisible : rien n'est émis
            self._journal_consulte = True
            if en_cours is not None:
                reste = (en_cours.fin_du_silence() - self._maintenant()).total_seconds()
                if reste > 0:
                    self._silence_jusqu_a = self._horloge() + reste
        if self._horloge() < self._silence_jusqu_a:
            raise EcritureInterrompue(
                "Une écriture a été interrompue : la pédale est laissée tranquille encore "
                f"{self._silence_jusqu_a - self._horloge():.0f} s."
            )
        if trame.categorie is Categorie.ECRITURE and not self._ecriture_ouverte:
            # Une trame d'écriture ne part que de ecrire_slot, après archive et journal.
            raise ErreurBornes(
                f"« {trame.nom} » : une trame d'écriture ne peut partir que d'une écriture "
                "de slot complète (archive, journal, relecture)."
            )
        if trame.categorie is not Categorie.LECTURE and not self._autres_que_lecture_permis():
            if self.firmware is None:
                raise FirmwareInconnu(
                    "La version du firmware n'a pas encore été lue : seules les lectures "
                    "sont permises."
                )
            raise FirmwareInconnu(
                f"Firmware {self.firmware:#04x} inconnu (protocole vérifié sur "
                f"{FIRMWARE_VERIFIE:#04x}) : seules les lectures sont permises."
            )
        if trame.essai is not None and not essai and not self._magasin.est_validee(trame.essai):
            raise FonctionALEssai(
                f"« {trame.nom} » est une fonction à l'essai (« {trame.essai} »), "
                "pas encore validée sur la pédale."
            )
        self._transport.ecrire(trame.octets)

    # --- réception ---

    def _suivant(self, attente_s: float) -> Evenement | None:
        if not self._tampon:
            self._tampon.extend(self._transport.lire(attente_s))
        return self._tampon.popleft() if self._tampon else None

    def _attendre(self, accepte: Callable[[bytes], bool], delai_s: float) -> bytes | None:
        """Premier SysEx accepté avant le délai. Tout le reste est mis de côté."""
        echeance = self._horloge() + delai_s
        while True:
            reste = echeance - self._horloge()
            if reste <= 0 and not self._tampon:
                return None
            evenement = self._suivant(max(min(reste, PAS_S), 0.0))
            if evenement is None:
                continue
            if isinstance(evenement, SysEx) and accepte(evenement.octets):
                return evenement.octets
            self.non_sollicites.append(evenement)

    def _requete(self, trame: Trame, accepte: Callable[[bytes], bool]) -> bytes:
        """Demande de lecture : réémise jusqu'à deux fois si la réponse ne vient pas."""
        if trame.categorie is not Categorie.LECTURE:
            raise ErreurBornes(f"« {trame.nom} » : seule une lecture peut être réémise.")
        for _ in range(ENVOIS_PAR_LECTURE):
            self._emettre(trame)
            reponse = self._attendre(accepte, DELAI_S)
            if reponse is not None:
                return reponse
        raise DelaiDepasse(
            f"Pas de réponse de la pédale à « {trame.nom} » après {ENVOIS_PAR_LECTURE} envois."
        )

    # --- lectures ---

    def lire_version(self) -> int:
        reponse = self._requete(emission.demande_version(), reception.est_reponse_version)
        self.firmware = reception.decoder_version(reponse)
        return self.firmware

    def lire_tableau(self, tableau: Tableau) -> tuple[int, ...]:
        reponse = self._requete(
            emission.demande_tableau(tableau),
            lambda message: reception.est_reponse_tableau(message, tableau),
        )
        return reception.decoder_tableau(reponse, tableau)

    def lire_etat(self) -> Etat:
        reponse = self._requete(emission.demande_etat(), reception.est_reponse_etat)
        return reception.decoder_etat(reponse)

    def lire_infos_slot(self, slot: int) -> InfoSlot:
        def accepte(message: bytes) -> bool:
            if not reception.est_reponse_infos(message):
                return False
            try:
                return reception.decoder_infos_slot(message).slot == slot
            except ErreurReponse:
                return False

        reponse = self._requete(emission.demande_infos_slot(slot), accepte)
        return reception.decoder_infos_slot(reponse)

    def _collecter_transfert(self) -> list[bytes]:
        recus: list[bytes] = []
        while len(recus) <= reception.NB_MESSAGES_LECTURE:
            message = self._attendre(reception.est_message_de_transfert, DELAI_S)
            if message is None:
                break
            recus.append(message)
            if reception.est_pied_de_transfert(message):
                break
        return recus

    def _attendre_silence(self) -> None:
        """Attend 500 ms sans SysEx, pour ne pas entrelacer deux lectures."""
        limite = self._horloge() + PATIENCE_SILENCE_S
        while self._attendre(lambda _message: True, SILENCE_AVANT_RELECTURE_S) is not None:
            if self._horloge() > limite:
                raise ErreurReponse("La pédale émet des SysEx sans interruption.")

    def lire_ir(self, slot: int) -> bytes:
        """Les 8192 octets du slot, toutes sommes vérifiées. Sans effet sur la pédale."""
        trame = emission.demande_ir(slot)
        derniere: ErreurReponse | None = None
        for tentative in range(ENVOIS_PAR_LECTURE):
            if tentative:
                self._attendre_silence()
            self._emettre(trame)
            try:
                return reception.assembler_lecture(self._collecter_transfert())
            except ErreurReponse as erreur:
                derniere = erreur
        raise ErreurReponse(f"Lecture du slot {slot} impossible : {derniere}")

    def lire_ir_verifiee(self, slot: int) -> bytes:
        """Deux lectures de suite, qui doivent être identiques."""
        premiere = self.lire_ir(slot)
        if self.lire_ir(slot) != premiere:
            raise ErreurReponse(f"Deux lectures du slot {slot} donnent des données différentes.")
        return premiere

    def lire_slot(self, slot: int) -> LectureSlot:
        """Infos et IR du slot, l'IR étant lue deux fois. Si l'identifiant recalculé ne
        correspond pas à l'identifiant annoncé, tout est relu une fois ; un écart qui
        persiste est rendu tel quel, à charge pour l'appelant de le signaler."""
        for _ in range(LECTURES_DE_SLOT):
            infos = self.lire_infos_slot(slot)
            donnees = self.lire_ir_verifiee(slot)
            lecture = LectureSlot(infos, donnees, codec.identifiant(donnees))
            if lecture.concorde:
                break
        return lecture

    # --- écriture d'une IR : la seule porte ---

    def ecrire_slot(
        self,
        slot: int,
        donnees: bytes,
        nom: str,
        *,
        identifiant: int | None = None,
        maintenant: Callable[[], datetime] | None = None,
    ) -> ResultatEcriture:
        """Écrit une IR dans un slot. Il n'existe pas d'autre chemin, ni de variante qui
        saute une étape :

        1. refus préalables (firmware, bornes, écriture interrompue en attente) ;
        2. archive du contenu actuel du slot ;
        3. journal posé sur le disque ;
        4. émission des 89 trames à la cadence de l'appli officielle, accusés surveillés ;
        5. bilan des accusés ;
        6. relecture du slot, comparée à ce qui a été émis ;
        7. journal retiré.

        Aucune trame n'est jamais réémise. Après un échec à partir de l'étape 4, plus rien
        n'est émis pendant 35 s, et le journal reste : voir constater_reprise.
        """
        racine = self._magasin.racine
        maintenant = maintenant or self._maintenant
        # 1. Refus préalables : rien n'a encore été émis ni écrit sur le disque.
        en_cours = journal.lire(racine)
        if en_cours is not None:
            reste = (en_cours.fin_du_silence() - maintenant()).total_seconds()
            if reste > 0:
                raise EcritureInterrompue(
                    f"Une écriture du slot {en_cours.slot} a été interrompue : attendre "
                    f"encore {reste:.0f} s avant d'émettre quoi que ce soit."
                )
            if en_cours.slot != slot:
                raise EcritureInterrompue(
                    f"Une écriture du slot {en_cours.slot} a été interrompue : tant que ce "
                    "slot n'est pas remis d'aplomb, il est le seul qu'on puisse écrire."
                )
        if not self._autres_que_lecture_permis():
            raise FirmwareInconnu(
                "Écriture refusée : le firmware n'est pas lu, ou n'est pas celui sur lequel "
                "le protocole a été vérifié."
            )
        if identifiant is None:
            identifiant = codec.identifiant(donnees)
        etapes = emission.construire_upload(slot, donnees, identifiant, nom)
        nom_brut = nom.encode("ascii").ljust(messages.LONGUEUR_NOM, b"\x00")
        # 2. Archive du contenu actuel.
        quand = maintenant()
        avant = self.lire_slot(slot)
        reglages = {tableau: self.lire_tableau(tableau)[slot] for tableau in Tableau}
        motif = "avant-ecriture" if en_cours is None else "altere"
        archive = self._magasin.creer_archive(motif, quand)
        self._magasin.ecrire_slot(
            archive,
            slot=slot,
            nom_brut=avant.infos.nom_brut,
            identifiant=avant.infos.identifiant,
            volume=reglages[Tableau.VOLUME],
            low_cut=reglages[Tableau.LOW_CUT],
            high_cut=reglages[Tableau.HIGH_CUT],
            firmware=self.firmware,
            motif=motif,
            quand=quand,
            donnees=avant.donnees,
        )
        # 3. Journal. L'archive de retour reste celle d'origine si le slot était déjà altéré.
        trace = journal.nouveau(
            slot=slot,
            debut=maintenant(),  # daté au moment où il est posé, après la lecture et l'archive
            archive=archive.name if en_cours is None else en_cours.archive,
            sha256=hashlib.sha256(donnees).hexdigest(),
            nom_brut=nom_brut,
            identifiant=identifiant,
        )
        if en_cours is not None:
            trace = trace.marque_altere()
        journal.ecrire(racine, trace)
        try:
            # 4 et 5. Émission et bilan des accusés.
            self._envoyer_upload(etapes, slot, identifiant, nom_brut)
            # 6. Relecture.
            apres = self.lire_slot(slot)
            if (
                apres.donnees != donnees
                or apres.infos.nom_brut != nom_brut
                or apres.infos.identifiant != identifiant
            ):
                raise ErreurReponse(
                    f"Après l'écriture, le slot {slot} ne contient pas ce qui a été émis."
                )
        except BaseException:
            self._silence_jusqu_a = self._horloge() + SILENCE_APRES_ECHEC_S
            with contextlib.suppress(ErreurOptimaAir):
                journal.ecrire(racine, trace.avec_derniere_trame(maintenant()))
            raise
        # 7. Tout est conforme.
        journal.supprimer(racine)
        return ResultatEcriture(archive, apres)

    def _envoyer_upload(
        self, etapes: Sequence[EtapeUpload], slot: int, identifiant: int, nom_brut: bytes
    ) -> None:
        recus: list[bytes] = []
        self._ecriture_ouverte = True
        try:
            for etape in etapes:
                self._emettre(etape.trame)
                self._collecter(recus, etape.pause_s)
                self._refuser_un_mauvais_accuse(recus)
            echeance = self._horloge() + DELAI_BILAN_S
            while not self._bilan_complet(recus) and self._horloge() < echeance:
                self._collecter(recus, PAS_S)
                self._refuser_un_mauvais_accuse(recus)
        finally:
            self._ecriture_ouverte = False
        self._verifier_bilan(recus, slot, identifiant, nom_brut)

    def _collecter(self, recus: list[bytes], duree_s: float) -> None:
        """Pendant duree_s, range dans recus les accusés et les réponses d'infos de slot."""
        echeance = self._horloge() + duree_s
        while True:
            reste = echeance - self._horloge()
            if reste <= 0 and not self._tampon:
                return
            evenement = self._suivant(max(min(reste, PAS_S), 0.0))
            if evenement is None:
                continue
            if isinstance(evenement, SysEx) and (
                reception.est_accuse(evenement.octets)
                or reception.est_reponse_infos(evenement.octets, messages.CMD_NOM)
                or reception.est_reponse_infos(evenement.octets, messages.CMD_INFOS)
            ):
                recus.append(evenement.octets)
            else:
                self.non_sollicites.append(evenement)

    @staticmethod
    def _accuses(recus: Sequence[bytes]) -> list[tuple[int, Statut]]:
        return [reception.decoder_accuse(m) for m in recus if reception.est_accuse(m)]

    def _refuser_un_mauvais_accuse(self, recus: Sequence[bytes]) -> None:
        """Arrête tout dès qu'un accusé porte une erreur, un statut inconnu (decoder_accuse
        lève alors ErreurReponse), ou un statut inattendu pour un bloc."""
        for commande, statut in self._accuses(recus):
            if statut is Statut.ERREUR:
                raise ErreurReponse("La pédale signale une erreur pendant l'écriture de l'IR.")
            if commande == messages.CMD_BLOC and statut is not Statut.OK:
                raise ErreurReponse(f"Accusé de bloc inattendu : {statut.name}.")

    def _bilan_complet(self, recus: Sequence[bytes]) -> bool:
        accuses = self._accuses(recus)
        return (
            sum(1 for commande, _ in accuses if commande == messages.CMD_BLOC) >= NB_ACCUSES_DE_BLOC
            and sum(1 for commande, _ in accuses if commande == messages.CMD_CADRE)
            >= NB_ACCUSES_DE_CADRE
            and any(reception.est_reponse_infos(m, messages.CMD_NOM) for m in recus)
            and any(reception.est_reponse_infos(m, messages.CMD_INFOS) for m in recus)
        )

    def _verifier_bilan(
        self, recus: Sequence[bytes], slot: int, identifiant: int, nom_brut: bytes
    ) -> None:
        accuses = self._accuses(recus)
        blocs = [statut for commande, statut in accuses if commande == messages.CMD_BLOC]
        cadres = [statut for commande, statut in accuses if commande == messages.CMD_CADRE]
        if len(blocs) != NB_ACCUSES_DE_BLOC:
            raise ErreurReponse(
                f"{len(blocs)} accusés de bloc reçus au lieu de {NB_ACCUSES_DE_BLOC}."
            )
        if len(cadres) < NB_ACCUSES_DE_CADRE or not STATUTS_DE_CADRE.issuperset(cadres):
            raise ErreurReponse("Accusés de l'en-tête et du pied manquants ou inattendus.")
        attendu = InfoSlot(slot, identifiant, nom_brut)
        for commande, quoi in (
            (messages.CMD_NOM, "l'écho du nom"),
            (messages.CMD_INFOS, "les infos"),
        ):
            reponses = [m for m in recus if reception.est_reponse_infos(m, commande)]
            if len(reponses) != 1:
                raise ErreurReponse(f"Après l'écriture, {quoi} : {len(reponses)} réponse(s).")
            if reception.decoder_infos_slot(reponses[0], commande) != attendu:
                raise ErreurReponse(f"Après l'écriture, {quoi} ne correspondent pas à l'émission.")

    def constater_reprise(
        self, *, maintenant: Callable[[], datetime] | None = None
    ) -> Constat | None:
        """S'il existe un journal d'écriture interrompue, relit le slot et dit ce qu'il
        contient. Rend None s'il n'y a rien à constater. Le journal est retiré si le slot
        contient ce qu'on voulait écrire ou ce qu'il y avait avant ; sinon il reste, marqué
        « altéré », et seule une écriture sur ce slot est permise."""
        racine = self._magasin.racine
        maintenant = maintenant or self._maintenant
        en_cours = journal.lire(racine)
        if en_cours is None:
            return None
        reste = (en_cours.fin_du_silence() - maintenant()).total_seconds()
        if reste > 0:
            raise EcritureInterrompue(
                f"Une écriture du slot {en_cours.slot} a été interrompue : attendre encore "
                f"{reste:.0f} s avant d'émettre quoi que ce soit."
            )
        archive = racine / "sauvegardes" / en_cours.archive
        lecture = self.lire_slot(en_cours.slot)
        if (
            hashlib.sha256(lecture.donnees).hexdigest() == en_cours.sha256
            and lecture.infos.nom_brut == bytes.fromhex(en_cours.nom_hex)
            and lecture.infos.identifiant == en_cours.identifiant
        ):
            journal.supprimer(racine)
            return Constat("aboutie", en_cours.slot, archive)
        fiche, anciennes = self._magasin.lire_slot(archive, en_cours.slot)
        if (
            lecture.donnees == anciennes
            and lecture.infos.nom_brut == bytes.fromhex(fiche.nom_hex)
            and lecture.infos.identifiant == fiche.identifiant
        ):
            journal.supprimer(racine)
            return Constat("inchange", en_cours.slot, archive)
        journal.ecrire(racine, en_cours.marque_altere())
        return Constat("altere", en_cours.slot, archive)

    def restaurer_slot(
        self,
        archive: Path,
        slot: int,
        *,
        nom: str | None = None,
        maintenant: Callable[[], datetime] | None = None,
    ) -> ResultatEcriture:
        """Réécrit un slot depuis une archive (sceau vérifié) : données, nom, identifiant,
        puis les trois réglages s'ils diffèrent. `nom` remplace un nom archivé qui ne peut
        pas être réémis."""
        fiche, donnees = self._magasin.lire_slot(archive, slot)
        if nom is None:
            nom = bytes.fromhex(fiche.nom_hex).rstrip(b"\x00").decode("latin-1")
        resultat = self.ecrire_slot(
            slot, donnees, nom, identifiant=fiche.identifiant, maintenant=maintenant
        )
        voulus = (
            (Tableau.VOLUME, fiche.volume),
            (Tableau.LOW_CUT, fiche.low_cut),
            (Tableau.HIGH_CUT, fiche.high_cut),
        )
        for tableau, valeur in voulus:
            if self.lire_tableau(tableau)[slot] != valeur:
                self.regler_slot(tableau, slot, valeur)
        return resultat

    def restaurer_archive(
        self,
        archive: Path,
        slots: Sequence[int] | None = None,
        *,
        maintenant: Callable[[], datetime] | None = None,
        sur_progres: Callable[[str, int, int], None] | None = None,
    ) -> BilanRestauration:
        """Remet la pédale dans l'état d'une archive, pour les slots demandés (par défaut
        tous ceux qu'elle contient). En trois temps :

        1. toute l'archive est relue et contrôlée (sceaux, empreintes) ;
        2. chaque slot de la pédale est relu et comparé : seul un slot dont l'IR, le nom ou
           l'identifiant diffèrent sera réécrit, et ce qui ne pourrait pas être émis (nom
           non conforme, IR d'usine) est refusé ici, avant la première écriture ;
        3. les slots à réécrire le sont par restaurer_slot (archive, journal, relecture),
           les autres reçoivent au besoin leurs trois réglages.

        S'arrête à la première erreur. `sur_progres(etape, fait, total)` est appelé avant
        chaque slot des temps 2 (« Comparaison ») et 3 (« Restauration »)."""
        if slots is None:
            slots = self._magasin.slots_presents(archive)
        if not slots:
            raise ArchiveAbimee(f"L'archive {archive} ne contient aucun slot.")
        contenus = {slot: self._magasin.lire_slot(archive, slot) for slot in slots}
        a_reecrire: list[int] = []
        for rang, slot in enumerate(slots):
            if sur_progres is not None:
                sur_progres("Comparaison", rang, len(slots))
            fiche, donnees = contenus[slot]
            nom_brut = bytes.fromhex(fiche.nom_hex).ljust(messages.LONGUEUR_NOM, b"\x00")
            actuel = self.lire_slot(slot)
            if (
                actuel.donnees == donnees
                and actuel.infos.nom_brut == nom_brut
                and actuel.infos.identifiant == fiche.identifiant
            ):
                continue
            try:
                emission.construire_upload(
                    slot, donnees, fiche.identifiant, nom_brut.rstrip(b"\x00").decode("latin-1")
                )
            except ErreurBornes as erreur:
                raise ErreurBornes(
                    f"Le slot {slot} de l'archive {archive.name} diffère de la pédale mais "
                    f"ne peut pas être réécrit tel quel ({erreur}). Rien n'a été écrit."
                ) from erreur
            a_reecrire.append(slot)
        reecrits: list[int] = []
        regles: list[int] = []
        inchanges: list[int] = []
        for rang, slot in enumerate(slots):
            if sur_progres is not None:
                sur_progres("Restauration", rang, len(slots))
            if slot in a_reecrire:
                self.restaurer_slot(archive, slot, maintenant=maintenant)
                reecrits.append(slot)
                continue
            fiche, _ = contenus[slot]
            voulus = (
                (Tableau.VOLUME, fiche.volume),
                (Tableau.LOW_CUT, fiche.low_cut),
                (Tableau.HIGH_CUT, fiche.high_cut),
            )
            regle = False
            for tableau, valeur in voulus:
                if self.lire_tableau(tableau)[slot] != valeur:
                    self.regler_slot(tableau, slot, valeur)
                    regle = True
            (regles if regle else inchanges).append(slot)
        return BilanRestauration(tuple(reecrits), tuple(regles), tuple(inchanges))

    # --- sauvegarde complète (lecture seule) ---

    def sauvegarder_tout(
        self,
        *,
        maintenant: Callable[[], datetime] | None = None,
        sur_progres: Callable[[int], None] | None = None,
    ) -> Sauvegarde:
        """Lit les 23 slots et leurs réglages, et les range dans une archive « complete ».
        Rien n'est écrit dans la pédale. Si une lecture échoue, l'erreur remonte et
        l'archive reste incomplète sur le disque : elle ne contient que des slots sûrs."""
        quand = (maintenant or self._maintenant)()
        firmware = self.firmware if self.firmware is not None else self.lire_version()
        tableaux = {tableau: self.lire_tableau(tableau) for tableau in Tableau}
        archive = self._magasin.creer_archive("complete", quand)
        discordants: list[int] = []
        for slot in range(messages.NB_SLOTS):
            if sur_progres is not None:
                sur_progres(slot)
            lecture = self.lire_slot(slot)
            if not lecture.concorde:
                discordants.append(slot)
            self._magasin.ecrire_slot(
                archive,
                slot=slot,
                nom_brut=lecture.infos.nom_brut,
                identifiant=lecture.infos.identifiant,
                volume=tableaux[Tableau.VOLUME][slot],
                low_cut=tableaux[Tableau.LOW_CUT][slot],
                high_cut=tableaux[Tableau.HIGH_CUT][slot],
                firmware=firmware,
                motif="complete",
                quand=quand,
                donnees=lecture.donnees,
            )
        return Sauvegarde(archive, tuple(discordants))

    # --- réglages ---

    def activer_slot(self, slot: int) -> Etat:
        """Rend un slot actif, puis relit l'état jusqu'à ce que la pédale l'annonce.

        L'ordre n'a pas d'accusé et n'est jamais réémis. Si la pédale n'annonce pas le
        slot demandé après quelques relectures, ErreurReponse est levée.
        """
        self._emettre(emission.activer_slot(slot))
        for _ in range(RELECTURES_APRES_REGLAGE):
            self._attendre(lambda _message: False, PAUSE_APRES_REGLAGE_S)
            etat = self.lire_etat()
            if etat.slot_actif == slot:
                return etat
        raise ErreurReponse(
            f"La pédale annonce le slot {etat.slot_actif} après la demande du slot {slot}."
        )

    def regler_slot(self, tableau: Tableau, slot: int, valeur: int) -> tuple[int, ...]:
        """Règle le volume, le coupe-bas ou le coupe-haut d'un slot, puis rend le tableau
        tel que la pédale le renvoie.

        Le tableau est relu juste avant, et seule l'entrée du slot est modifiée. La pédale
        doit renvoyer en écho exactement ce qui a été émis ; sinon ErreurReponse. L'écriture
        n'est jamais réémise.
        """
        if not 0 <= slot < messages.NB_SLOTS:
            raise ErreurBornes(f"slot {slot} hors de 0..{messages.NB_SLOTS - 1}")
        valeurs = list(self.lire_tableau(tableau))
        valeurs[slot] = valeur
        self._emettre(emission.ecrire_tableau(tableau, valeurs))
        echo = self._attendre(
            lambda message: reception.est_reponse_tableau(message, tableau, ecriture=True),
            DELAI_S,
        )
        if echo is None:
            raise DelaiDepasse(f"Pas d'écho de la pédale à l'écriture du tableau {tableau.name}.")
        recues = reception.decoder_tableau(echo, tableau, ecriture=True)
        if list(recues) != valeurs:
            raise ErreurReponse(f"L'écho du tableau {tableau.name} diffère de ce qui a été émis.")
        return recues

    def regler(self, nom: str, valeur: int) -> None:
        """Un réglage par Control Change (voir messages_emission.REGLAGES). Sans accusé,
        jamais réémis : c'est à l'appelant de relire l'état pour connaître la valeur que
        la pédale a retenue."""
        self._emettre(emission.regler(nom, valeur))

    # --- annonce (fonction à l'essai) ---

    def annoncer(self, *, essai: bool = False) -> bool:
        """Émet l'annonce de connexion. Rend vrai si la pédale y répond. Aucun nouvel essai."""
        self._emettre(emission.annonce(), essai=essai)
        self.annonce_emise = True
        reponse = self._attendre(lambda message: len(message) == LONGUEUR_REPONSE_ANNONCE, DELAI_S)
        return reponse is not None

    # --- séquence de connexion ---

    def connecter(self) -> Connexion:
        """Lit tout l'état de la pédale. N'émet que des lectures, plus l'annonce si elle
        a été validée et que le firmware est connu."""
        firmware = self.lire_version()
        high_cuts = self.lire_tableau(Tableau.HIGH_CUT)
        low_cuts = self.lire_tableau(Tableau.LOW_CUT)
        volumes = self.lire_tableau(Tableau.VOLUME)
        etat = self.lire_etat()
        slots = tuple(self.lire_infos_slot(slot) for slot in range(messages.NB_SLOTS))
        if self._magasin.est_validee("annonce") and self._autres_que_lecture_permis():
            self.annoncer()
        return Connexion(firmware, volumes, low_cuts, high_cuts, etat, slots)

    # --- écoute ---

    def ecouter(self, duree_s: float) -> Iterator[Evenement]:
        """Rend les événements mis de côté, puis ceux qui arrivent pendant duree_s."""
        while self.non_sollicites:
            yield self.non_sollicites.popleft()
        echeance = self._horloge() + duree_s
        while True:
            reste = echeance - self._horloge()
            if reste <= 0 and not self._tampon:
                return
            evenement = self._suivant(max(min(reste, PAS_S), 0.0))
            if evenement is not None:
                yield evenement
