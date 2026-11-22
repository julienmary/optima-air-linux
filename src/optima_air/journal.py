"""Journal d'écriture : la trace, sur le disque, d'une écriture d'IR en cours.

Il est posé avant la première trame d'un upload et retiré après une relecture conforme.
S'il existe à l'ouverture d'une session, une écriture a été interrompue (plantage, coupure,
erreur) : l'état du slot visé est inconnu tant qu'il n'a pas été relu.

Ce module ne parle jamais à la pédale.
"""

import contextlib
import json
import os
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from optima_air import disque, messages
from optima_air.erreurs import ArchiveAbimee, ErreurDisque
from optima_air.i18n import _

NOM = "ecriture-en-cours.json"
# Après une écriture interrompue, la pédale est laissée tranquille ce temps-là (une note
# ancienne parle d'une session d'upload qui expire vers 30 s ; non vérifié).
SILENCE_APRES_LA_DERNIERE_TRAME = timedelta(seconds=35)
# Si la date de la dernière trame manque (le processus est mort), on compte depuis le
# début, avec la marge du design : un upload dure moins d'une seconde, mais chaque accusé
# pourrait en théorie se faire attendre.
DUREE_MAXIMALE_D_UN_UPLOAD = timedelta(seconds=100)

_CLES = (
    "format",
    "slot",
    "debut",
    "archive",
    "sha256",
    "nom_hex",
    "identifiant",
    "derniere_trame",
    "altere",
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_NOM_HEX = re.compile(r"[0-9a-f]{64}")
_NOM_ARCHIVE = re.compile(r"[0-9]{8}-[0-9]{6}-[a-z-]+(?:-[0-9]+)?")
_IDENTIFIANT_MAX = 0x7FFFFFFF


@dataclass(frozen=True)
class Journal:
    slot: int
    debut: str  # date du début de l'écriture, ISO avec fuseau
    archive: str  # nom du dossier d'archive qui permet de revenir à l'état d'avant
    sha256: str  # empreinte des 8192 octets qu'on voulait écrire
    nom_hex: str  # les 32 octets du nom qu'on voulait écrire
    identifiant: int
    derniere_trame: str | None = None  # date de la dernière trame émise, si elle est connue
    altere: bool = False  # vrai une fois constaté que le slot n'est ni l'ancien ni le nouveau

    def fin_du_silence(self) -> datetime:
        """Avant cette date, rien ne doit être émis vers la pédale."""
        if self.derniere_trame is not None:
            return datetime.fromisoformat(self.derniere_trame) + SILENCE_APRES_LA_DERNIERE_TRAME
        return (
            datetime.fromisoformat(self.debut)
            + DUREE_MAXIMALE_D_UN_UPLOAD
            + SILENCE_APRES_LA_DERNIERE_TRAME
        )

    def avec_derniere_trame(self, quand: datetime) -> "Journal":
        return replace(self, derniere_trame=_date(quand))

    def marque_altere(self) -> "Journal":
        return replace(self, altere=True)


def _date(quand: datetime) -> str:
    if quand.utcoffset() is None:
        raise ValueError(_("a journal date must carry a time zone"))
    return quand.isoformat(timespec="seconds")


def nouveau(
    *, slot: int, debut: datetime, archive: str, sha256: str, nom_brut: bytes, identifiant: int
) -> Journal:
    return Journal(
        slot=slot,
        debut=_date(debut),
        archive=archive,
        sha256=sha256,
        nom_hex=nom_brut.hex(),
        identifiant=identifiant,
    )


def _est_date(texte: Any) -> bool:
    if type(texte) is not str:
        return False
    try:
        relue = datetime.fromisoformat(texte)
    except ValueError:
        return False
    return relue.utcoffset() is not None and relue.isoformat(timespec="seconds") == texte


def _refus(contenu: Any) -> str | None:
    if type(contenu) is not dict:
        return _("JSON object expected, got {type}").format(type=type(contenu).__name__)
    try:
        disque.controler_format_et_cles(contenu, _CLES)
    except disque.Illisible as erreur:
        return str(erreur)
    if type(contenu["slot"]) is not int or not 0 <= contenu["slot"] < messages.NB_SLOTS:
        return _("slot out of range")
    if not _est_date(contenu["debut"]):
        return _("{field}: ISO date with time zone expected").format(field="debut")
    if contenu["derniere_trame"] is not None and not _est_date(contenu["derniere_trame"]):
        return _("{field}: ISO date with time zone, or null, expected").format(
            field="derniere_trame"
        )
    if type(contenu["archive"]) is not str or _NOM_ARCHIVE.fullmatch(contenu["archive"]) is None:
        return _("{field}: backup folder name expected").format(field="archive")
    if type(contenu["sha256"]) is not str or _SHA256.fullmatch(contenu["sha256"]) is None:
        return _("{field}: 64 hexadecimal digits expected").format(field="sha256")
    if type(contenu["nom_hex"]) is not str or _NOM_HEX.fullmatch(contenu["nom_hex"]) is None:
        return _("{field}: 32 bytes in hexadecimal expected").format(field="nom_hex")
    identifiant = contenu["identifiant"]
    if type(identifiant) is not int or not 0 <= identifiant <= _IDENTIFIANT_MAX:
        return _("identifier out of range")
    if type(contenu["altere"]) is not bool:
        return _("{field}: boolean expected").format(field="altere")
    return None


def lire(racine: Path) -> Journal | None:
    """Le journal, ou None s'il n'y en a pas. Un journal illisible lève ArchiveAbimee : on
    ne sait alors rien de l'écriture interrompue, et il ne faut surtout pas l'ignorer."""
    chemin = racine / NOM
    try:
        contenu = disque.charger_json(disque.lire_fichier(chemin, disque.TAILLE_MAX_JSON))
    except FileNotFoundError:
        return None
    except disque.Illisible as erreur:
        raise ArchiveAbimee(
            _("Unreadable write journal: {file} ({error})").format(file=chemin, error=erreur)
        ) from erreur
    refus = _refus(contenu)
    if refus is not None:
        raise ArchiveAbimee(
            _("Unreadable write journal: {file} ({error})").format(file=chemin, error=refus)
        )
    return Journal(**{cle: contenu[cle] for cle in _CLES if cle != "format"})


def ecrire(racine: Path, journal: Journal) -> None:
    """Pose ou remplace le journal, de façon durable. ErreurDisque si c'est impossible :
    dans ce cas, aucune écriture vers la pédale ne doit commencer."""
    disque.creer_dossiers(racine)
    contenu = {"format": disque.FORMAT, **asdict(journal)}
    disque.ecrire_en_remplacant(racine / NOM, json.dumps(contenu, indent=1).encode("ascii"))


def supprimer(racine: Path) -> None:
    """Retire le journal, de façon durable. Sans effet s'il n'existe pas."""
    chemin = racine / NOM
    try:
        os.unlink(chemin)
    except FileNotFoundError:
        return
    except OSError as erreur:
        raise ErreurDisque(
            _("Cannot remove the journal {file}: {error}").format(
                file=chemin, error=disque.texte_systeme(erreur)
            )
        ) from erreur
    with contextlib.suppress(ErreurDisque):
        disque.synchroniser_dossier(racine)
