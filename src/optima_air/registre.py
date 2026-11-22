"""Registre des fonctions à l'essai : la barrière qui reste fermée au moindre doute.

Registre (verifications.json) : {"format": 1, "validees": [...]}, liste triée sans doublon
de fonctions connues. Absent, il ne valide rien ; abîmé, il lève ArchiveAbimee et n'est
jamais réparé ni repris de zéro. Un lien symbolique à sa place, même cassé, est un
registre abîmé : il n'est jamais suivi (disque.lire_fichier). Ses mises à jour se font
sous un verrou exclusif (verifications.verrou, flock) tenu de la lecture à la mise en
place ; le registre est remplacé d'un bloc (disque.ecrire_en_remplacant), jamais vu à
moitié écrit.

Le registre a le mode 0600, quel que soit l'umask ; le verrou est créé en 0600 moins
l'umask. Un lien symbolique à la place du verrou, même cassé, ou autre chose qu'un fichier
ordinaire, est refusé (ErreurDisque) sans être suivi ni verrouillé (disque.ouvrir_verrou).
Ce module ne parle jamais à la pédale.
"""

import contextlib
import fcntl
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

from optima_air import disque
from optima_air.erreurs import ArchiveAbimee, ErreurBornes, ErreurDisque
from optima_air.i18n import _

FONCTIONS_A_L_ESSAI = ("annonce", "usine", "preset-enregistrer", "preset-restaurer", "manual")
# Attente maximale du verrou du registre, puis ErreurDisque.
_ATTENTE_VERROU = 2.0
_PAUSE_VERROU = 0.01

_NOM_REGISTRE = "verifications.json"
_NOM_VERROU = "verifications.verrou"
_CLES_REGISTRE = ("format", "validees")


def verifier_fonction(nom: str) -> None:
    if nom not in FONCTIONS_A_L_ESSAI:
        raise ErreurBornes(
            _("Unknown function “{name}”. Known: {known}.").format(
                name=nom, known=", ".join(FONCTIONS_A_L_ESSAI)
            )
        )


def _validees_depuis(brut: bytes) -> frozenset[str]:
    contenu = disque.charger_json(brut)
    if type(contenu) is not dict:
        raise disque.Illisible(
            _("JSON object expected, got {type}").format(type=type(contenu).__name__)
        )
    disque.controler_format_et_cles(contenu, _CLES_REGISTRE)
    validees = contenu["validees"]
    if (
        type(validees) is not list
        or not all(type(nom) is str and nom in FONCTIONS_A_L_ESSAI for nom in validees)
        or validees != sorted(set(validees))
    ):
        raise disque.Illisible(_("sorted list of known functions, without duplicates, expected"))
    return frozenset(validees)


def lire_registre(racine: Path) -> frozenset[str]:
    """Fonctions validées. Registre absent : aucune ; illisible, ou lien symbolique à sa
    place (même cassé) : ArchiveAbimee."""
    chemin = racine / _NOM_REGISTRE
    try:
        return _validees_depuis(disque.lire_fichier(chemin, disque.TAILLE_MAX_JSON))
    except FileNotFoundError:
        return frozenset()
    except disque.Illisible as erreur:
        raise ArchiveAbimee(
            _("Unreadable trial register: {file} ({error})").format(file=chemin, error=erreur)
        ) from erreur


@contextlib.contextmanager
def _verrou_registre(racine: Path) -> Iterator[None]:
    """Verrou exclusif (flock) sur verifications.verrou, attendu au plus _ATTENTE_VERROU
    secondes, puis ErreurDisque. Libéré à la sortie, quelle qu'elle soit."""
    chemin = racine / _NOM_VERROU
    # Un lien symbolique ou autre chose qu'un fichier ordinaire à sa place : ErreurDisque,
    # sans que rien soit suivi ni verrouillé.
    descripteur = disque.ouvrir_verrou(chemin)
    try:
        echeance = time.monotonic() + _ATTENTE_VERROU
        while True:
            try:
                fcntl.flock(descripteur, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= echeance:
                    raise ErreurDisque(
                        _(
                            "Trial register locked by another process ({file}): try again "
                            "once the other command has finished."
                        ).format(file=chemin)
                    ) from None
                time.sleep(_PAUSE_VERROU)
            except OSError as erreur:
                raise ErreurDisque(
                    _("Cannot lock {file}: {error}").format(
                        file=chemin, error=disque.texte_systeme(erreur)
                    )
                ) from erreur
        yield
    finally:
        # Fermer le seul descripteur de cette ouverture libère le verrou.
        with contextlib.suppress(OSError):
            os.close(descripteur)


def modifier_registre(racine: Path, nom: str, *, valider: bool) -> None:
    """Lecture, modification et mise en place sous le verrou. Un registre abîmé lève
    ArchiveAbimee et reste tel quel."""
    verifier_fonction(nom)
    disque.creer_dossiers(racine)
    with _verrou_registre(racine):
        validees = lire_registre(racine)
        validees = validees | {nom} if valider else validees - {nom}
        contenu = {"format": disque.FORMAT, "validees": sorted(validees)}
        octets = (json.dumps(contenu, indent=1, ensure_ascii=True) + "\n").encode("ascii")
        disque.ecrire_en_remplacant(racine / _NOM_REGISTRE, octets)
