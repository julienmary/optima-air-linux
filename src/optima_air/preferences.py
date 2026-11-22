"""Préférences d'affichage : pour l'instant, la couleur d'accent de la fenêtre.

Rangées dans $XDG_CONFIG_HOME/optima-air/preferences.json. Rien ici n'est critique : un
fichier absent ou illisible donne les valeurs par défaut, et un échec d'écriture se borne
à ne pas retenir le choix. Ce module ne parle jamais à la pédale.
"""

import json
import os
import re
from pathlib import Path

from optima_air.i18n import _

COULEUR_PAR_DEFAUT = "#2c75ff"  # bleu électrique
NOM = "preferences.json"
_COULEUR = re.compile(r"#[0-9a-f]{6}")


def dossier_par_defaut() -> Path:
    """$XDG_CONFIG_HOME/optima-air, ou ~/.config/optima-air si la variable est vide,
    absente ou relative."""
    base = os.environ.get("XDG_CONFIG_HOME", "")
    if not base or not Path(base).is_absolute():
        base = str(Path.home() / ".config")
    return Path(base) / "optima-air"


def est_couleur(texte: object) -> bool:
    """Vrai pour « #rrggbb » en minuscules."""
    return isinstance(texte, str) and _COULEUR.fullmatch(texte) is not None


def lire_couleur(dossier: Path) -> str:
    try:
        contenu = json.loads((dossier / NOM).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return COULEUR_PAR_DEFAUT
    couleur = contenu.get("couleur") if isinstance(contenu, dict) else None
    if isinstance(couleur, str) and est_couleur(couleur):
        return couleur
    return COULEUR_PAR_DEFAUT


def ecrire_couleur(dossier: Path, couleur: str) -> bool:
    """Retient la couleur. Rend faux si elle n'a pas pu être écrite."""
    if not est_couleur(couleur):
        raise ValueError(
            _("colour {colour!r}: lowercase “#rrggbb” expected").format(colour=couleur)
        )
    try:
        dossier.mkdir(parents=True, exist_ok=True)
        provisoire = dossier / (NOM + ".tmp")
        provisoire.write_text(json.dumps({"couleur": couleur}) + "\n", encoding="utf-8")
        os.replace(provisoire, dossier / NOM)
    except OSError:
        return False
    return True


def composantes(couleur: str) -> tuple[float, float, float]:
    """« #rrggbb » en trois nombres de 0 à 1."""
    if not est_couleur(couleur):
        raise ValueError(
            _("colour {colour!r}: lowercase “#rrggbb” expected").format(colour=couleur)
        )
    return (
        int(couleur[1:3], 16) / 255,
        int(couleur[3:5], 16) / 255,
        int(couleur[5:7], 16) / 255,
    )


def en_hexadecimal(rouge: float, vert: float, bleu: float) -> str:
    """Trois nombres de 0 à 1 (bornés au besoin) en « #rrggbb »."""
    octets = [round(min(max(c, 0.0), 1.0) * 255) for c in (rouge, vert, bleu)]
    return "#{:02x}{:02x}{:02x}".format(*octets)


def texte_lisible_sur(couleur: str) -> str:
    """Noir ou blanc, selon ce qui se lit le mieux sur cette couleur. Le critère est la
    luminance relative du WCAG ; le seuil de 0,3 est un choix d'aspect, pris au-dessus du
    point d'égal contraste (0,179) pour garder du blanc sur les couleurs saturées moyennes,
    dont le bleu d'origine."""

    def lineaire(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    rouge, vert, bleu = (lineaire(c) for c in composantes(couleur))
    luminance = 0.2126 * rouge + 0.7152 * vert + 0.0722 * bleu
    return "#000000" if luminance > 0.3 else "#ffffff"
