"""Archives de slot : le filet de sécurité de toute écriture dans la pédale.

Archives : un dossier sauvegardes/AAAAMMJJ-HHMMSS-motif[-N] par archive, avec pour chaque
slot ses données (slotNN.f32, 8192 octets) et sa fiche (slotNN.json). La fiche porte un
sceau : le SHA-256 de la sérialisation canonique de tous ses autres champs, empreinte des
données comprise. Relue, une fiche dont un seul champ a changé, ou dont un seul bit a
basculé, est refusée ; de même des données dont un seul bit a basculé. Le sceau n'est pas
une signature : qui veut retoucher une fiche peut recalculer le sceau ; il protège des
accidents (disque, copie, édition maladroite), pas d'une falsification délibérée.
La fiche est liée à son archive (nom du dossier, motif, heure) : une fiche ou une paire
déplacée ou copiée dans une autre archive est refusée. Une fiche ou des données
remplacées par un lien symbolique, même vers un fichier valide, sont refusées sans que
le lien soit suivi (disque.lire_fichier).

Une archive ne s'écrit qu'une fois : un slot déjà présent n'est jamais remplacé. Chaque
fichier est écrit à côté, sous un nom caché (.slotNN.f32.XXXX.tmp), synchronisé, puis mis
en place par un lien qui échoue si la cible existe (disque.ecrire_une_fois). Après une
coupure, de tels fichiers provisoires cachés peuvent rester : tout code qui liste une
archive doit ignorer les fichiers dont le nom commence par un point.

Les fiches et les données ont le mode 0600, quel que soit l'umask.

Heure : creer_archive et ecrire_slot exigent un `quand` avec fuseau, le même pour les
deux. Le nom du dossier porte l'heure qu'exprime `quand` dans son propre fuseau (passer
datetime.now().astimezone() pour l'heure locale) ; la fiche garde quand.isoformat() à la
seconde. Ni l'écriture ni la relecture ne dépendent du fuseau de la machine.

Ce module ne parle jamais à la pédale.
"""

import hashlib
import json
import os
import re
import stat
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from optima_air import codec, disque, messages
from optima_air.erreurs import ArchiveAbimee, ErreurBornes, ErreurDisque

MOTIFS = ("avant-ecriture", "manuelle", "complete", "import", "altere")
REGLAGE_MAX = 100  # volume, low cut et high cut d'un slot : 0..100
FIRMWARE_MAX = 127  # le firmware est un octet de données SysEx
# L'identifiant archivé est gardé tel quel, bit 31 (IR d'usine) compris.
IDENTIFIANT_MAX = 0xFFFFFFFF


def _nombre(valeur: int) -> str:
    """Entier pour un message ; str() d'un entier de plus de 4300 chiffres lève."""
    return str(valeur) if valeur.bit_length() <= 64 else f"de {valeur.bit_length()} bits"


_HEXADECIMAL = re.compile(r"(?:[0-9a-f]{2})*")
# AAAAMMJJ-HHMMSS-motif, puis -2, -3… si plusieurs archives tombent la même seconde.
_NOM_ARCHIVE = re.compile(
    r"(?P<horodatage>[0-9]{8}-[0-9]{6})-(?P<motif>"
    + "|".join(re.escape(motif) for motif in MOTIFS)
    + r")(?:-(?:[2-9]|[1-9][0-9]+))?"
)


@dataclass(frozen=True)
class FicheSlot:
    """Fiche d'un slot archivé. Champs dans l'ordre du fichier ; `sceau` est le SHA-256 de
    la sérialisation canonique de tous les autres (voir _sceau)."""

    format: int
    archive: str  # nom du dossier d'archive
    slot: int
    nom_hex: str
    identifiant: int  # annoncé par la pédale, bit 31 compris
    identifiant_calcule: int  # codec.identifiant des données
    volume: int
    low_cut: int
    high_cut: int
    date: str  # isoformat à la seconde, avec fuseau
    firmware: int | None  # None seulement pour le motif « import »
    motif: str
    sha256: str  # empreinte des données
    sceau: str

    @property
    def identifiant_concorde(self) -> bool:
        """Vrai si l'identifiant annoncé par la pédale (bit 31 ignoré) est celui des données."""
        return (self.identifiant & 0x7FFFFFFF) == self.identifiant_calcule


_CHAMPS = tuple(champ.name for champ in fields(FicheSlot))
_CHAMPS_ENTIERS = ("slot", "identifiant", "identifiant_calcule", "volume", "low_cut", "high_cut")
_CHAMPS_TEXTES = ("archive", "nom_hex", "date", "motif", "sha256", "sceau")


def _sceau(champs: dict[str, Any]) -> str:
    """SHA-256 hexadécimal de la sérialisation canonique des champs (sceau exclu)."""
    canonique = json.dumps(
        {cle: valeur for cle, valeur in champs.items() if cle != "sceau"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonique.encode("ascii")).hexdigest()


def _horodatage(instant: datetime) -> str:
    """AAAAMMJJ-HHMMSS de l'heure exprimée par l'instant, dans son propre fuseau. Année
    toujours sur quatre chiffres (strftime ne la complète pas partout)."""
    return (
        f"{instant.year:04d}{instant.month:02d}{instant.day:02d}-"
        f"{instant.hour:02d}{instant.minute:02d}{instant.second:02d}"
    )


def _refus_valeurs(
    *,
    slot: int,
    nom_brut: bytes,
    identifiant: int,
    volume: int,
    low_cut: int,
    high_cut: int,
    firmware: int | None,
    motif: str,
) -> str | None:
    """Raison du refus des valeurs d'une fiche, ou None. Les mêmes bornes valent à
    l'écriture et à la relecture : une fiche relue est une fiche qu'on aurait pu écrire."""
    entiers = {
        "slot": slot,
        "identifiant": identifiant,
        "volume": volume,
        "low_cut": low_cut,
        "high_cut": high_cut,
    }
    for nom, valeur in entiers.items():
        # type(x) is int, exprès : True est un int, et serait écrit « true » dans la fiche.
        if type(valeur) is not int:
            return f"{nom} : entier attendu, reçu {type(valeur).__name__}"
    if firmware is not None and type(firmware) is not int:
        return f"firmware : entier ou rien attendu, reçu {type(firmware).__name__}"
    if type(nom_brut) is not bytes:
        return f"nom de slot : octets attendus, reçu {type(nom_brut).__name__}"
    if type(motif) is not str:
        return f"motif : texte attendu, reçu {type(motif).__name__}"
    if not 0 <= slot < messages.NB_SLOTS:
        return f"slot {_nombre(slot)} hors de 0..{messages.NB_SLOTS - 1}"
    if len(nom_brut) != messages.LONGUEUR_NOM:
        return f"nom de slot de {len(nom_brut)} octets au lieu de {messages.LONGUEUR_NOM}"
    if not 0 <= identifiant <= IDENTIFIANT_MAX:
        return f"identifiant {_nombre(identifiant)} hors de 0..{IDENTIFIANT_MAX:#x}"
    for nom, valeur in (("volume", volume), ("low_cut", low_cut), ("high_cut", high_cut)):
        if not 0 <= valeur <= REGLAGE_MAX:
            return f"{nom} {_nombre(valeur)} hors de 0..{REGLAGE_MAX}"
    if firmware is not None and not 0 <= firmware <= FIRMWARE_MAX:
        return f"firmware {_nombre(firmware)} hors de 0..{FIRMWARE_MAX}"
    if motif not in MOTIFS:
        return f"Motif d'archive « {disque.montrer(motif)} » inconnu."
    # La session connaît toujours le firmware ; seul un import peut l'ignorer.
    if firmware is None and motif != "import":
        return "firmware absent : seul le motif « import » l'admet"
    return None


def _refus_archive(nom_archive: str, motif: str, date: str) -> str | None:
    """Raison du refus d'une fiche au regard du nom de son dossier d'archive, ou None :
    nom bien formé, même motif, date canonique avec fuseau et même heure que le nom.
    L'heure comparée est celle qu'exprime la date dans son propre fuseau : la relecture
    ne dépend pas du fuseau de la machine."""
    morceaux = _NOM_ARCHIVE.fullmatch(nom_archive)
    if morceaux is None:
        return f"nom d'archive « {disque.montrer(nom_archive)} » mal formé"
    if motif != morceaux["motif"]:
        return (
            f"motif « {disque.montrer(motif)} » différent de celui de l'archive "
            f"{disque.montrer(nom_archive)}, du motif « {morceaux['motif']} »"
        )
    try:
        instant = datetime.fromisoformat(date)
    except ValueError:
        return f"date {date!r} illisible"
    if instant.utcoffset() is None:
        return f"date {date!r} sans fuseau horaire"
    if instant.isoformat(timespec="seconds") != date:
        return f"date {date!r} non canonique"
    if _horodatage(instant) != morceaux["horodatage"]:
        return f"date {date!r} différente de l'heure de l'archive {disque.montrer(nom_archive)}"
    return None


def _controler_quand(quand: datetime) -> None:
    if not isinstance(quand, datetime):
        raise ErreurBornes(f"quand : date attendue, reçu {type(quand).__name__}")
    if quand.utcoffset() is None:
        raise ErreurBornes(
            "quand doit porter un fuseau horaire (par exemple datetime.now().astimezone())"
        )


def _controler_slot(slot: int) -> None:
    if type(slot) is not int or not 0 <= slot < messages.NB_SLOTS:
        raise ErreurBornes(f"slot {slot!r} hors de 0..{messages.NB_SLOTS - 1}")


def _controler_dossier(dossier: Path) -> None:
    """ErreurDisque si le dossier n'existe pas ou n'est pas un dossier."""
    with disque.envelopper("d'écrire dans le dossier", dossier):
        etat = os.stat(dossier)
    if not stat.S_ISDIR(etat.st_mode):
        raise ErreurDisque(f"Impossible d'écrire dans {dossier} : ce n'est pas un dossier")


def _existe(chemin: Path) -> bool:
    """Vrai si une entrée de ce nom existe, quelle qu'elle soit (lien mort compris)."""
    try:
        os.lstat(chemin)
    except FileNotFoundError:
        return False
    except OSError as erreur:
        raise ErreurDisque(
            f"Impossible de vérifier {chemin} : {disque.texte_systeme(erreur)}"
        ) from erreur
    return True


def creer_archive(racine: Path, motif: str, quand: datetime) -> Path:
    if motif not in MOTIFS:
        raise ErreurBornes(f"Motif d'archive « {motif} » inconnu.")
    _controler_quand(quand)
    dossier = racine / "sauvegardes"
    disque.creer_dossiers(dossier)
    base = f"{_horodatage(quand)}-{motif}"
    archive = dossier / base
    numero = 2
    while True:
        # mkdir sans exist_ok : deux créations simultanées n'ont jamais le même dossier.
        try:
            os.mkdir(archive)
            break
        except FileExistsError:
            archive = dossier / f"{base}-{numero}"
            numero += 1
        except OSError as erreur:
            raise ErreurDisque(
                f"Impossible de créer l'archive {archive} : {disque.texte_systeme(erreur)}"
            ) from erreur
    disque.synchroniser_dossier(dossier)
    return archive


def _preparer_fiche(
    archive: Path,
    *,
    slot: int,
    nom_brut: bytes,
    identifiant: int,
    volume: int,
    low_cut: int,
    high_cut: int,
    firmware: int | None,
    motif: str,
    quand: datetime,
    donnees: bytes,
) -> tuple[FicheSlot, bytes]:
    """Contrôle tout et sérialise la fiche, sceau compris, sans rien écrire."""
    refus = _refus_valeurs(
        slot=slot,
        nom_brut=nom_brut,
        identifiant=identifiant,
        volume=volume,
        low_cut=low_cut,
        high_cut=high_cut,
        firmware=firmware,
        motif=motif,
    )
    if refus is not None:
        raise ErreurBornes(refus)
    if type(donnees) is not bytes:
        raise ErreurBornes(f"données : octets attendus, reçu {type(donnees).__name__}")
    if len(donnees) != codec.TAILLE_IR:
        raise ErreurBornes(f"une IR fait {codec.TAILLE_IR} octets, reçu {len(donnees)}")
    _controler_quand(quand)
    date = quand.isoformat(timespec="seconds")
    refus = _refus_archive(archive.name, motif, date)
    if refus is not None:
        raise ErreurBornes(refus)
    champs: dict[str, Any] = {
        "format": disque.FORMAT,
        "archive": archive.name,
        "slot": slot,
        "nom_hex": nom_brut.hex(),
        "identifiant": identifiant,
        "identifiant_calcule": codec.identifiant(donnees),
        "volume": volume,
        "low_cut": low_cut,
        "high_cut": high_cut,
        "date": date,
        "firmware": firmware,
        "motif": motif,
        "sha256": hashlib.sha256(donnees).hexdigest(),
    }
    fiche = FicheSlot(**champs, sceau=_sceau(champs))
    octets = (json.dumps(asdict(fiche), indent=1, ensure_ascii=True) + "\n").encode("ascii")
    return fiche, octets


def ecrire_slot(
    archive: Path,
    donnees: bytes,
    *,
    slot: int,
    nom_brut: bytes,
    identifiant: int,
    volume: int,
    low_cut: int,
    high_cut: int,
    firmware: int | None,
    motif: str,
    quand: datetime,
) -> FicheSlot:
    fiche, octets_fiche = _preparer_fiche(
        archive,
        slot=slot,
        nom_brut=nom_brut,
        identifiant=identifiant,
        volume=volume,
        low_cut=low_cut,
        high_cut=high_cut,
        firmware=firmware,
        motif=motif,
        quand=quand,
        donnees=donnees,
    )
    _controler_dossier(archive)
    chemin_donnees = archive / f"slot{fiche.slot:02d}.f32"
    chemin_fiche = archive / f"slot{fiche.slot:02d}.json"
    deja = ErreurBornes(
        f"slot {fiche.slot} déjà archivé dans {archive} : une archive ne s'écrit qu'une "
        "fois, faire une nouvelle archive."
    )
    if _existe(chemin_donnees) or _existe(chemin_fiche):
        raise deja
    try:
        # Les données d'abord, rendues durables : une fiche ne désigne jamais des données
        # pas encore écrites.
        disque.ecrire_une_fois(chemin_donnees, donnees)
        disque.ecrire_une_fois(chemin_fiche, octets_fiche)
    except disque.DejaPresent as erreur:
        raise deja from erreur
    return fiche


def _fiche_depuis(contenu: Any, chemin: Path) -> FicheSlot:
    """Fiche lue d'un objet JSON : sceau d'abord, puis format, clés et types."""

    def illisible(raison: str) -> ArchiveAbimee:
        return ArchiveAbimee(f"Fiche illisible : {chemin} ({raison})")

    if type(contenu) is not dict:
        raise illisible(f"objet JSON attendu, reçu {type(contenu).__name__}")
    if type(contenu.get("sceau")) is not str:
        raise illisible("champ « sceau » absent ou pas un texte")
    try:
        sceau = _sceau(contenu)
    except (ValueError, RecursionError) as erreur:
        raise illisible(f"sceau incalculable : {erreur}") from erreur
    if sceau != contenu["sceau"]:
        raise ArchiveAbimee(f"Fiche retouchée ou abîmée : {chemin} (sceau incorrect)")
    try:
        disque.controler_format_et_cles(contenu, _CHAMPS)
    except disque.Illisible as erreur:
        raise illisible(str(erreur)) from erreur
    for cle in _CHAMPS_ENTIERS:
        # type(x) is int, exprès : en Python, true (bool) est un int.
        if type(contenu[cle]) is not int:
            raise illisible(f"champ « {cle} » : entier attendu")
    for cle in _CHAMPS_TEXTES:
        if type(contenu[cle]) is not str:
            raise illisible(f"champ « {cle} » : texte attendu")
    firmware = contenu["firmware"]
    if firmware is not None and type(firmware) is not int:
        raise illisible("champ « firmware » : entier ou null attendu")
    return FicheSlot(**{cle: contenu[cle] for cle in _CHAMPS})


def _refus_fiche_relue(fiche: FicheSlot, archive: Path, slot: int) -> str | None:
    if fiche.slot != slot:
        return f"fiche du slot {fiche.slot} rangée sous le slot {slot}"
    if _HEXADECIMAL.fullmatch(fiche.nom_hex) is None:
        return "nom_hex : hexadécimal en minuscules attendu"
    refus = _refus_valeurs(
        slot=fiche.slot,
        nom_brut=bytes.fromhex(fiche.nom_hex),
        identifiant=fiche.identifiant,
        volume=fiche.volume,
        low_cut=fiche.low_cut,
        high_cut=fiche.high_cut,
        firmware=fiche.firmware,
        motif=fiche.motif,
    )
    if refus is not None:
        return refus
    if fiche.archive != archive.name:
        return (
            f"fiche de l'archive « {disque.montrer(fiche.archive)} » trouvée dans "
            f"« {disque.montrer(archive.name)} »"
        )
    return _refus_archive(fiche.archive, fiche.motif, fiche.date)


_NOM_FICHE = re.compile(r"slot([0-9]{2})\.json")


def lister(racine: Path) -> list[Path]:
    """Les dossiers d'archive, du plus récent au plus ancien. Seuls les vrais dossiers au
    nom d'archive sont rendus ; rien n'est vérifié de leur contenu (voir lire_slot)."""
    dossier = racine / "sauvegardes"
    try:
        with os.scandir(dossier) as entrees:
            noms = [
                entree.name
                for entree in entrees
                if _NOM_ARCHIVE.fullmatch(entree.name) is not None
                and entree.is_dir(follow_symlinks=False)
            ]
    except FileNotFoundError:
        return []
    except OSError as erreur:
        raise ErreurDisque(
            f"Impossible de lire {dossier} : {disque.texte_systeme(erreur)}"
        ) from erreur
    return [dossier / nom for nom in sorted(noms, reverse=True)]


def slots_presents(archive: Path) -> tuple[int, ...]:
    """Numéros des slots dont l'archive porte une fiche, en ordre croissant. Rien n'est
    vérifié : c'est lire_slot qui dit si le slot est utilisable."""
    try:
        noms = os.listdir(archive)
    except OSError as erreur:
        raise ArchiveAbimee(
            f"Archive illisible : {archive} ({disque.texte_systeme(erreur)})"
        ) from erreur
    slots = set()
    for nom in noms:
        trouve = _NOM_FICHE.fullmatch(nom)
        if trouve is not None and int(trouve.group(1)) < messages.NB_SLOTS:
            slots.add(int(trouve.group(1)))
    return tuple(sorted(slots))


def lire_slot(archive: Path, slot: int) -> tuple[FicheSlot, bytes]:
    _controler_slot(slot)
    chemin_fiche = archive / f"slot{slot:02d}.json"
    chemin_donnees = archive / f"slot{slot:02d}.f32"
    try:
        contenu = disque.charger_json(disque.lire_fichier(chemin_fiche, disque.TAILLE_MAX_JSON))
    except FileNotFoundError as erreur:
        raise ArchiveAbimee(f"Fiche illisible : {chemin_fiche} (absente)") from erreur
    except disque.Illisible as erreur:
        raise ArchiveAbimee(f"Fiche illisible : {chemin_fiche} ({erreur})") from erreur
    fiche = _fiche_depuis(contenu, chemin_fiche)
    refus = _refus_fiche_relue(fiche, archive, slot)
    if refus is not None:
        raise ArchiveAbimee(f"Fiche incohérente : {chemin_fiche} ({refus})")
    try:
        donnees = disque.lire_fichier(chemin_donnees, codec.TAILLE_IR, exacte=True)
    except FileNotFoundError as erreur:
        raise ArchiveAbimee(f"Données illisibles : {chemin_donnees} (absentes)") from erreur
    except disque.Illisible as erreur:
        raise ArchiveAbimee(f"Données illisibles : {chemin_donnees} ({erreur})") from erreur
    if hashlib.sha256(donnees).hexdigest() != fiche.sha256:
        raise ArchiveAbimee(f"Empreinte incorrecte : {chemin_donnees} ne sera pas utilisé.")
    calcule = codec.identifiant(donnees)
    if calcule != fiche.identifiant_calcule:
        raise ArchiveAbimee(
            f"Fiche incohérente : {chemin_fiche} (identifiant recalculé {calcule:#010x}, "
            f"la fiche porte {fiche.identifiant_calcule:#010x})"
        )
    return fiche, donnees
