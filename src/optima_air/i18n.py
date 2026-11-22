"""Traduction des textes affichés (gettext, domaine « optima-air »).

Les textes du code sont en anglais ; les traductions sont des catalogues compilés (.mo)
cherchés d'abord dans le paquet (`optima_air/locale/`, rempli par
`tools/build-translations.sh` quand on tourne depuis le dépôt), puis dans le dossier système
(`/usr/share/locale`, pour un paquet installé). La langue est celle de l'environnement
(`LANGUAGE`, `LC_ALL`, `LC_MESSAGES`, `LANG`) ; sans catalogue qui convienne, l'anglais.

Règles d'écriture : `_("Slot {slot} …").format(slot=…)`, jamais de f-string dans `_()` (le
texte ne serait pas extrait), champs nommés pour que la traduction puisse les déplacer.
"""

import gettext
import locale
from pathlib import Path

DOMAINE = "optima-air"
DOSSIER_DU_PAQUET = Path(__file__).resolve().parent / "locale"


def charger() -> gettext.NullTranslations:
    """Le catalogue de la langue de l'environnement, ou aucun (anglais)."""
    for dossier in (DOSSIER_DU_PAQUET, None):  # None : le dossier système de gettext
        try:
            return gettext.translation(DOMAINE, dossier)
        except FileNotFoundError:
            continue
    return gettext.NullTranslations()


_traduction = charger()


def utiliser(traduction: gettext.NullTranslations) -> None:
    """Remplace le catalogue en cours (pour les tests). Les textes déjà traduits au moment
    de l'import d'un module ne changent pas : on traduit donc au moment de l'affichage."""
    global _traduction
    _traduction = traduction


def _(texte: str) -> str:
    return _traduction.gettext(texte)


def ngettext(singulier: str, pluriel: str, nombre: int) -> str:
    return _traduction.ngettext(singulier, pluriel, nombre)


def decimal(valeur: float, decimales: int) -> str:
    """Un nombre décimal écrit selon la locale numérique (21,3 en français, 21.3 en
    anglais). Le signe n'est écrit que s'il est négatif."""
    return locale.format_string(f"%.{decimales}f", valeur)


def decimal_signe(valeur: float, decimales: int) -> str:
    """Comme decimal, avec le signe toujours écrit (+1,5 ou -1,5)."""
    return locale.format_string(f"%+.{decimales}f", valeur)
