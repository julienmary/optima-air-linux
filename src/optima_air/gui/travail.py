"""Le fil de travail de la fenêtre : il possède la session et lui seul parle à la pédale.

Ce module n'importe pas GTK. Il publie des messages par une fonction fournie par la
fenêtre, qui se charge de les ramener dans le fil d'affichage.

Il émet des lectures, et l'activation d'un slot quand la fenêtre le demande. La valeur
d'un potard vient toujours d'une lecture d'état,
jamais de la conversion d'un Control Change : un message reçu de la pédale sert seulement
de signal « quelque chose a changé, relis l'état ».
"""

import queue
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from optima_air.device import (
    BilanRestauration,
    Connexion,
    Constat,
    LectureSlot,
    Sauvegarde,
    Session,
)
from optima_air.erreurs import ErreurOptimaAir
from optima_air.i18n import _
from optima_air.messages import NB_SLOTS, Tableau
from optima_air.messages_reception import Etat
from optima_air.midi import ControlChange

PAUSE_AVANT_NOUVEL_ESSAI_S = 1.0
ECOUTE_S = 0.05  # cadence de rafraîchissement des vu-mètres
RELECTURE_AU_PLUS_TARD_S = 1.0
# Niveaux de sortie, émis toutes les 30 ms une fois l'annonce de connexion reçue par la
# pédale. Identifiés sur la pédale le 2026-11-21 : le CC 96 monte avec la reverb (signal
# traité), le CC 97 non (signal direct).
CC_NIVEAU_WET = 96
CC_NIVEAU_DRY = 97
CC_NIVEAUX = frozenset({CC_NIVEAU_WET, CC_NIVEAU_DRY})
NIVEAU_MAX = 127


@dataclass(frozen=True)
class Connecte:
    connexion: Connexion


@dataclass(frozen=True)
class Deconnecte:
    raison: str


@dataclass(frozen=True)
class EtatLu:
    etat: Etat


@dataclass(frozen=True)
class IrLue:
    lecture: LectureSlot


@dataclass(frozen=True)
class Niveaux:
    """Derniers niveaux reçus, de 0 à 127. L'unité n'est pas connue."""

    dry: int
    wet: int


@dataclass(frozen=True)
class TableauLu:
    """Un tableau de réglages par slot, tel que la pédale vient de le renvoyer."""

    tableau: Tableau
    valeurs: tuple[int, ...]


@dataclass(frozen=True)
class EcritureCommencee:
    slot: int


@dataclass(frozen=True)
class EcritureTerminee:
    """Le slot a été écrit, relu à l'identique, et son ancien contenu archivé."""

    slot: int
    archive: Path
    lecture: LectureSlot


@dataclass(frozen=True)
class EcritureEchouee:
    slot: int
    raison: str


@dataclass(frozen=True)
class RepriseConstatee:
    """À la connexion, un journal d'écriture interrompue a été trouvé et le slot relu."""

    constat: Constat


@dataclass(frozen=True)
class Progres:
    """Une opération longue avance : `fait` slots traités sur `total`."""

    operation: str  # « Sauvegarde » ou « Restauration »
    fait: int
    total: int


@dataclass(frozen=True)
class SauvegardeTerminee:
    sauvegarde: Sauvegarde


@dataclass(frozen=True)
class SauvegardeEchouee:
    raison: str


@dataclass(frozen=True)
class RestaurationTerminee:
    """Une archive a été restaurée. La liste des slots est republiée juste après."""

    archive: Path
    bilan: BilanRestauration


@dataclass(frozen=True)
class RestaurationEchouee:
    raison: str


@dataclass(frozen=True)
class _DemandeSauvegarde:
    pass


@dataclass(frozen=True)
class _DemandeArchive:
    archive: Path
    slots: tuple[int, ...] | None  # None : tous ceux de l'archive


class _ArretDemande(ErreurOptimaAir):
    """La fenêtre se ferme pendant une opération longue : on s'arrête entre deux slots."""


@dataclass(frozen=True)
class _DemandeEcriture:
    slot: int
    donnees: bytes | None = None  # un chargement
    nom: str | None = None
    archive: Path | None = None  # une restauration


type Message = (
    Connecte
    | Deconnecte
    | EtatLu
    | IrLue
    | Niveaux
    | TableauLu
    | EcritureCommencee
    | EcritureTerminee
    | EcritureEchouee
    | RepriseConstatee
    | Progres
    | SauvegardeTerminee
    | SauvegardeEchouee
    | RestaurationTerminee
    | RestaurationEchouee
)


class Travailleur(threading.Thread):
    """Se connecte, publie l'état, puis le suit jusqu'à l'arrêt ou la déconnexion."""

    def __init__(
        self,
        ouvrir: Callable[[], AbstractContextManager[Session]],
        publier: Callable[[Message], None],
        horloge: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(name="optima-air-travail", daemon=True)
        self._ouvrir = ouvrir
        self._publier = publier
        self._horloge = horloge
        self._arret = threading.Event()
        self._demandes: queue.SimpleQueue[int] = queue.SimpleQueue()
        self._potards: queue.SimpleQueue[tuple[str, int]] = queue.SimpleQueue()
        self._reglages_slot: queue.SimpleQueue[tuple[Tableau, int, int]] = queue.SimpleQueue()
        self._ecritures: queue.SimpleQueue[
            _DemandeEcriture | _DemandeSauvegarde | _DemandeArchive
        ] = queue.SimpleQueue()

    def arreter(self) -> None:
        """Demande l'arrêt. Le port est fermé par le fil lui-même, en sortant de sa boucle."""
        self._arret.set()

    def demander_slot(self, slot: int) -> None:
        """Depuis le fil d'affichage : demande d'activer un slot. Traitée par le fil de travail."""
        self._demandes.put(slot)

    def demander_reglage(self, nom: str, valeur: int) -> None:
        """Depuis le fil d'affichage : demande de régler un potard du preamp (0 à 100)."""
        self._potards.put((nom, valeur))

    def demander_reglage_slot(self, tableau: Tableau, slot: int, valeur: int) -> None:
        """Depuis le fil d'affichage : volume, coupe-bas ou coupe-haut d'un slot (0 à 100)."""
        self._reglages_slot.put((tableau, slot, valeur))

    def demander_ecriture(self, slot: int, donnees: bytes, nom: str) -> None:
        """Depuis le fil d'affichage : écrire une IR préparée dans un slot."""
        self._ecritures.put(_DemandeEcriture(slot, donnees=donnees, nom=nom))

    def demander_restauration(self, archive: Path, slot: int) -> None:
        """Depuis le fil d'affichage : réécrire un slot depuis une archive."""
        self._ecritures.put(_DemandeEcriture(slot, archive=archive))

    def demander_sauvegarde(self) -> None:
        """Depuis le fil d'affichage : lire les 23 slots et les archiver."""
        self._ecritures.put(_DemandeSauvegarde())

    def demander_restauration_archive(
        self, archive: Path, slots: tuple[int, ...] | None = None
    ) -> None:
        """Depuis le fil d'affichage : remettre les slots demandés (par défaut tous ceux de
        l'archive) dans l'état de l'archive. Seuls ceux qui diffèrent sont réécrits."""
        self._ecritures.put(_DemandeArchive(archive, slots))

    def run(self) -> None:
        while not self._arret.is_set():
            try:
                with self._ouvrir() as session:
                    self._suivre(session)
            except ErreurOptimaAir as erreur:
                self._oublier_les_demandes()
                self._publier(Deconnecte(str(erreur)))
            self._arret.wait(PAUSE_AVANT_NOUVEL_ESSAI_S)

    def _oublier_les_demandes(self) -> None:
        """Après une erreur, rien de ce qui attendait ne doit partir tout seul à la
        reconnexion : une écriture, surtout, se redemande."""
        files: tuple[queue.SimpleQueue[Any], ...] = (
            self._ecritures,
            self._demandes,
            self._potards,
            self._reglages_slot,
        )
        for file in files:
            while not file.empty():
                file.get()

    def _suivre(self, session: Session) -> None:
        connexion = session.connecter()
        self._publier(Connecte(connexion))
        constat = session.constater_reprise()
        if constat is not None:
            self._publier(RepriseConstatee(constat))
        etat = connexion.etat
        self._lire_ir(session, etat.slot_actif)
        derniere_lecture = self._horloge()
        niveaux = {CC_NIVEAU_DRY: 0, CC_NIVEAU_WET: 0}
        while not self._arret.is_set():
            a_relire = self._traiter_demandes(session)
            niveaux_recus = False
            for evenement in session.ecouter(ECOUTE_S):
                if isinstance(evenement, ControlChange) and evenement.numero in CC_NIVEAUX:
                    niveaux[evenement.numero] = evenement.valeur
                    niveaux_recus = True
                else:
                    a_relire = True
            if niveaux_recus:
                self._publier(Niveaux(niveaux[CC_NIVEAU_DRY], niveaux[CC_NIVEAU_WET]))
            if not a_relire and self._horloge() - derniere_lecture < RELECTURE_AU_PLUS_TARD_S:
                continue
            nouvel_etat = session.lire_etat()
            derniere_lecture = self._horloge()
            if nouvel_etat == etat:
                continue
            slot_change = nouvel_etat.slot_actif != etat.slot_actif
            etat = nouvel_etat
            self._publier(EtatLu(etat))
            if slot_change:
                self._lire_ir(session, etat.slot_actif)

    def _traiter_demandes(self, session: Session) -> bool:
        """Exécute les demandes en attente, en ne gardant que la dernière de chaque sorte :
        un potard qu'on tourne ne donne qu'un envoi par tour de boucle (20 par seconde au
        plus). Rend vrai s'il faut relire l'état."""
        ecrit = False
        while not self._ecritures.empty():
            demande = self._ecritures.get()
            if isinstance(demande, _DemandeSauvegarde):
                self._sauvegarder(session)
            elif isinstance(demande, _DemandeArchive):
                self._restaurer_archive(session, demande)
            else:
                self._ecrire(session, demande)
            ecrit = True
        slot: int | None = None
        while not self._demandes.empty():
            slot = self._demandes.get()
        potards: dict[str, int] = {}
        while not self._potards.empty():
            nom, valeur = self._potards.get()
            potards[nom] = valeur
        for nom, valeur in potards.items():
            session.regler(nom, valeur)
        par_slot: dict[tuple[Tableau, int], int] = {}
        while not self._reglages_slot.empty():
            tableau, numero, valeur = self._reglages_slot.get()
            par_slot[(tableau, numero)] = valeur
        for (tableau, numero), valeur in par_slot.items():
            self._publier(TableauLu(tableau, session.regler_slot(tableau, numero, valeur)))
        if slot is not None:
            session.activer_slot(slot)
        return ecrit or slot is not None or bool(potards)

    def _ecrire(self, session: Session, demande: _DemandeEcriture) -> None:
        """Écrit ou restaure un slot. En cas d'échec, le dit puis laisse l'erreur remonter :
        la session est abandonnée, et la suivante constatera l'état du slot."""
        self._publier(EcritureCommencee(demande.slot))
        try:
            if demande.archive is not None:
                resultat = session.restaurer_slot(demande.archive, demande.slot)
            elif demande.donnees is not None and demande.nom is not None:
                resultat = session.ecrire_slot(demande.slot, demande.donnees, demande.nom)
            else:
                return
        except ErreurOptimaAir as erreur:
            self._publier(EcritureEchouee(demande.slot, str(erreur)))
            raise
        self._publier(EcritureTerminee(demande.slot, resultat.archive, resultat.lecture))
        for tableau in Tableau:
            self._publier(TableauLu(tableau, session.lire_tableau(tableau)))

    def _progres(self, operation: str, fait: int, total: int) -> None:
        """Le rappel donné à une opération longue : publie l'avancement, et arrête
        l'opération entre deux slots si la fenêtre se ferme."""
        if self._arret.is_set():
            raise _ArretDemande(_("Stop requested."))
        self._publier(Progres(operation, fait, total))

    def _sauvegarder(self, session: Session) -> None:
        try:
            sauvegarde = session.sauvegarder_tout(
                sur_progres=lambda slot: self._progres(_("Backup"), slot, NB_SLOTS)
            )
        except ErreurOptimaAir as erreur:
            self._publier(SauvegardeEchouee(str(erreur)))
            raise
        self._publier(SauvegardeTerminee(sauvegarde))

    def _restaurer_archive(self, session: Session, demande: _DemandeArchive) -> None:
        """Comme _ecrire : un échec est dit, puis la session est abandonnée."""
        try:
            bilan = session.restaurer_archive(
                demande.archive, demande.slots, sur_progres=self._progres
            )
        except ErreurOptimaAir as erreur:
            self._publier(RestaurationEchouee(str(erreur)))
            raise
        self._publier(RestaurationTerminee(demande.archive, bilan))
        # Noms, réglages et courbe ont pu changer : tout est relu et republié.
        connexion = session.connecter()
        self._publier(Connecte(connexion))
        self._lire_ir(session, connexion.etat.slot_actif)

    def _lire_ir(self, session: Session, slot: int) -> None:
        self._publier(IrLue(session.lire_slot(slot)))
