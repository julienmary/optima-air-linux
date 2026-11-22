"""Briques de disque du dossier de données : lecture bornée, JSON strict, écriture durable.

Ce module ne connaît ni le registre ni les archives, et ne parle jamais à la pédale.

Lecture : lire_fichier ne lit qu'un fichier ordinaire de taille bornée, contrôlé sur le
descripteur ouvert, et n'ouvre jamais la cible d'un lien symbolique : un lien à la place
du fichier lu, même cassé, est refusé sans être suivi (O_NOFOLLOW) ; charger_json
n'admet que du JSON strict (ASCII seul, clés uniques, ni NaN ni Infinity) ;
controler_format_et_cles exige le champ format et les seules clés attendues. Un refus
lève Illisible, que l'appelant convertit en ArchiveAbimee en nommant le fichier.

Écriture : chaque fichier NOM est écrit à côté, sous un nom caché (.NOM.XXXX.tmp), en
mode 0600 quel que soit l'umask, synchronisé, puis mis en place : par renommage
(ecrire_en_remplacant), ou par un lien qui échoue si la cible existe (ecrire_une_fois,
qui lève alors DejaPresent). Chaque entrée de dossier créée ou mise en place est rendue
durable par la synchronisation de son dossier parent. Après une coupure, de tels fichiers
provisoires cachés peuvent rester : tout code qui liste un dossier de données doit ignorer
les fichiers dont le nom commence par un point. Aucune écriture ne suit un lien
symbolique posé à la place du fichier final : le provisoire est créé par mkstemp
(O_EXCL), le renommage remplace le lien lui-même, et le lien de mise en place échoue
devant tout nom déjà pris, lien cassé compris.

Verrou : ouvrir_verrou ouvre, ou crée, un fichier de verrou sans jamais suivre un lien
symbolique, et n'accepte qu'un fichier ordinaire, contrôlé sur le descripteur ouvert avant
tout verrouillage ; un refus lève ErreurDisque.

Erreurs : une OSError d'écriture, de création ou de synchronisation devient une
ErreurDisque nommant l'opération et le chemin ; l'OSError d'origine est attachée
(raise ... from).
"""

import contextlib
import errno
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from optima_air.erreurs import ErreurDisque

FORMAT = 1  # version du format du registre et des fiches
# Taille maximale relue d'un registre ou d'une fiche. Une fiche écrite fait moins de
# 1000 octets, même avec un nom d'archive de 255 caractères.
TAILLE_MAX_JSON = 4096
MODE_FICHIER = 0o600


def montrer(texte: str) -> str:
    """Texte lu sur le disque, rendu sans risque dans un message (caractères de contrôle,
    moitiés de paires UTF-16 et octets non UTF-8 d'un nom de fichier échappés)."""
    return texte if texte.isascii() and texte.isprintable() else repr(texte)


class Illisible(Exception):
    """Raison pour laquelle un fichier relu est refusé ; convertie en ArchiveAbimee."""


class DejaPresent(Exception):
    """Le fichier final existe déjà ; il n'a pas été touché."""


def texte_systeme(erreur: OSError) -> str:
    return erreur.strerror or str(erreur)


@contextlib.contextmanager
def envelopper(operation: str, chemin: Path) -> Iterator[None]:
    """Enveloppe toute OSError en ErreurDisque nommant l'opération et le chemin."""
    try:
        yield
    except OSError as erreur:
        raise ErreurDisque(f"Impossible {operation} {chemin} : {texte_systeme(erreur)}") from erreur


def _refus_d_ouverture(chemin: Path, erreur: OSError) -> str:
    """Raison d'un échec d'ouverture. Avec O_NOFOLLOW, un lien symbolique en dernier
    composant échoue en ELOOP, comme une boucle de liens plus haut dans le chemin : lstat,
    qui ne suit pas le lien, dit lequel des deux."""
    if erreur.errno == errno.ELOOP:
        with contextlib.suppress(OSError):
            if stat.S_ISLNK(os.lstat(chemin).st_mode):
                return "est un lien symbolique"
    return texte_systeme(erreur)


def lire_fichier(chemin: Path, taille: int, *, exacte: bool = False) -> bytes:
    """Contenu d'un fichier ordinaire d'au plus `taille` octets (exactement `taille` si
    `exacte`).

    O_NOFOLLOW : un lien symbolique en dernier composant, même cassé, est refusé sans être
    suivi ; sa cible n'est jamais ouverte (un lien vers le port de la pédale ne l'ouvre
    pas). Les dossiers du chemin, eux, peuvent être des liens. O_NONBLOCK : une FIFO ne
    bloque pas l'ouverture. Le type et la taille sont contrôlés par fstat sur le
    descripteur ouvert, puis lus depuis ce même descripteur.

    FileNotFoundError remonte telle quelle (un registre absent est un cas normal) ; tout
    autre refus, lien symbolique compris, lève Illisible."""
    try:
        descripteur = os.open(chemin, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise
    except OSError as erreur:
        raise Illisible(_refus_d_ouverture(chemin, erreur)) from erreur
    try:
        etat = os.fstat(descripteur)
        if not stat.S_ISREG(etat.st_mode):
            raise Illisible("pas un fichier ordinaire")
        if exacte and etat.st_size != taille:
            raise Illisible(f"{etat.st_size} octets au lieu de {taille}")
        if etat.st_size > taille:
            raise Illisible(f"{etat.st_size} octets, plus que les {taille} permis")
        morceaux: list[bytes] = []
        reste = etat.st_size + 1  # un octet de plus : un fichier qui a grossi se voit
        while reste > 0:
            morceau = os.read(descripteur, reste)
            if not morceau:
                break
            morceaux.append(morceau)
            reste -= len(morceau)
        contenu = b"".join(morceaux)
        if len(contenu) != etat.st_size:
            raise Illisible("taille changée pendant la lecture")
        return contenu
    except OSError as erreur:
        raise Illisible(texte_systeme(erreur)) from erreur
    finally:
        with contextlib.suppress(OSError):  # fichier lu : rien à perdre à la fermeture
            os.close(descripteur)


def ouvrir_verrou(chemin: Path) -> int:
    """Descripteur du fichier de verrou `chemin`, à verrouiller ensuite par flock ; créé
    en MODE_FICHIER moins l'umask s'il n'existe pas.

    O_NOFOLLOW : un lien symbolique en dernier composant, même cassé, est refusé sans être
    suivi ; rien n'est créé à travers lui, et sa cible n'est jamais ouverte (un lien vers
    le port de la pédale ne l'ouvre pas). O_NONBLOCK : une FIFO ne bloque pas
    l'ouverture. Le type est contrôlé par fstat sur le descripteur ouvert : autre chose
    qu'un fichier ordinaire est refusé, descripteur refermé, avant tout verrouillage.

    Tout refus lève ErreurDisque."""
    try:
        descripteur = os.open(
            chemin,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            MODE_FICHIER,
        )
    except OSError as erreur:
        raise ErreurDisque(
            f"Impossible de créer le verrou {chemin} : {_refus_d_ouverture(chemin, erreur)}"
        ) from erreur
    try:
        with envelopper("de créer le verrou", chemin):
            etat = os.fstat(descripteur)
        if not stat.S_ISREG(etat.st_mode):
            raise ErreurDisque(
                f"Impossible de créer le verrou {chemin} : n'est pas un fichier ordinaire"
            )
    except BaseException:
        # L'erreur d'origine prime sur un éventuel échec de la fermeture.
        with contextlib.suppress(OSError):
            os.close(descripteur)
        raise
    return descripteur


def _sans_doublon(paires: list[tuple[str, Any]]) -> dict[str, Any]:
    objet: dict[str, Any] = {}
    for cle, valeur in paires:
        if cle in objet:
            raise ValueError(f"clé « {montrer(cle)} » en double")
        objet[cle] = valeur
    return objet


def _refuser_constante(nom: str) -> Any:
    raise ValueError(f"{nom} n'est pas du JSON standard")


def charger_json(brut: bytes) -> Any:
    """JSON strict : ASCII seul (tout ce qui est écrit ici l'est), clés uniques, ni NaN ni
    Infinity. Une imbrication trop profonde est refusée comme le reste."""
    try:
        return json.loads(
            brut.decode("ascii"),
            object_pairs_hook=_sans_doublon,
            parse_constant=_refuser_constante,
        )
    except (ValueError, RecursionError) as erreur:
        raise Illisible(f"n'est pas du JSON : {erreur}") from erreur


def controler_format_et_cles(contenu: dict[str, Any], cles: tuple[str, ...]) -> None:
    """Champ format égal à FORMAT, chaque clé attendue présente, aucune autre."""
    if "format" not in contenu:
        raise Illisible("champ « format » absent")
    valeur = contenu["format"]
    # type(x) is int, exprès : True == 1 en Python.
    if type(valeur) is not int or valeur != FORMAT:
        raise Illisible(f"format {valeur!r} inconnu")
    for cle in cles:
        if cle not in contenu:
            raise Illisible(f"champ « {cle} » absent")
    for cle in contenu:
        if cle not in cles:
            raise Illisible(f"champ « {montrer(cle)} » inconnu")


def synchroniser_dossier(dossier: Path) -> None:
    """Rend durables les entrées du dossier (fichier mis en place, dossier créé)."""
    with envelopper("de synchroniser le dossier", dossier):
        descripteur = os.open(dossier, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descripteur)
        finally:
            os.close(descripteur)


def creer_dossiers(dossier: Path) -> None:
    """Comme mkdir -p, chaque création rendue durable dans son dossier parent. Un chemin
    du parcours qui existe sans être un dossier lève ErreurDisque."""
    a_creer: list[Path] = []
    courant = dossier
    while True:
        try:
            etat = os.stat(courant)
        except (FileNotFoundError, NotADirectoryError) as erreur:
            if courant.parent == courant:
                raise ErreurDisque(
                    f"Impossible de créer le dossier {dossier} : {texte_systeme(erreur)}"
                ) from erreur
            a_creer.append(courant)
            courant = courant.parent
            continue
        except OSError as erreur:
            raise ErreurDisque(
                f"Impossible de créer le dossier {dossier} : {texte_systeme(erreur)}"
            ) from erreur
        if not stat.S_ISDIR(etat.st_mode):
            raise ErreurDisque(
                f"Impossible de créer le dossier {dossier} : {courant} n'est pas un dossier"
            )
        break
    for chemin in reversed(a_creer):
        try:
            os.mkdir(chemin)
        except FileExistsError as erreur:
            if os.path.isdir(chemin):
                continue  # créé entre-temps par un autre
            raise ErreurDisque(
                f"Impossible de créer le dossier {dossier} : {chemin} n'est pas un dossier"
            ) from erreur
        except OSError as erreur:
            raise ErreurDisque(
                f"Impossible de créer le dossier {chemin} : {texte_systeme(erreur)}"
            ) from erreur
        synchroniser_dossier(chemin.parent)


def _ecrire_provisoire(chemin: Path, contenu: bytes) -> Path:
    """Écrit le contenu dans un fichier caché à côté de `chemin`, en mode 0600, et le
    synchronise. En cas d'échec, le fichier provisoire est effacé."""
    with envelopper("d'écrire", chemin):
        descripteur, nom = tempfile.mkstemp(
            dir=chemin.parent, prefix=f".{chemin.name}.", suffix=".tmp"
        )
    provisoire = Path(nom)
    try:
        with envelopper("d'écrire", chemin):
            try:
                os.fchmod(descripteur, MODE_FICHIER)  # exact, quel que soit l'umask
                vue = memoryview(contenu)
                while vue:
                    vue = vue[os.write(descripteur, vue) :]
                os.fsync(descripteur)
            finally:
                os.close(descripteur)
    except BaseException:
        # L'erreur d'origine prime sur un éventuel échec de l'effacement.
        with contextlib.suppress(OSError):
            provisoire.unlink()
        raise
    return provisoire


def ecrire_en_remplacant(chemin: Path, contenu: bytes) -> None:
    """Écrit à côté, synchronise, puis renomme par-dessus : le fichier n'est jamais vu à
    moitié écrit, et l'ancien contenu reste en place tant que le nouveau n'est pas
    entièrement sur le disque."""
    provisoire = _ecrire_provisoire(chemin, contenu)
    try:
        with envelopper("de mettre en place", chemin):
            os.replace(provisoire, chemin)
    except BaseException:
        with contextlib.suppress(OSError):
            provisoire.unlink()
        raise
    synchroniser_dossier(chemin.parent)


def ecrire_une_fois(chemin: Path, contenu: bytes) -> None:
    """Comme ecrire_en_remplacant, mais la mise en place par os.link échoue si `chemin`
    existe, même créé par un autre processus entre-temps : DejaPresent, et le fichier
    existant n'est pas touché."""
    provisoire = _ecrire_provisoire(chemin, contenu)
    try:
        try:
            os.link(provisoire, chemin)
        except FileExistsError as erreur:
            raise DejaPresent(chemin) from erreur
        except OSError as erreur:
            raise ErreurDisque(
                f"Impossible de mettre en place {chemin} : {texte_systeme(erreur)}"
            ) from erreur
    finally:
        # Si l'effacement échoue, le fichier caché reste : voir la docstring du module.
        with contextlib.suppress(OSError):
            provisoire.unlink()
    synchroniser_dossier(chemin.parent)
