"""Conversions entre les valeurs brutes de la pédale et ce qu'on affiche."""

import math


def volume_db(valeur: int) -> float:
    """Volume d'un slot, 0..100 vers -20..+20 dB."""
    return (valeur - 50) * 0.4


def low_cut_hz(valeur: int) -> int:
    """Low cut d'un slot, 0..100 vers 20..300 Hz."""
    return round(valeur / 100 * 280 + 20)


def high_cut_hz(valeur: int) -> int:
    """High cut d'un slot, 0..100 vers 10..20 kHz."""
    return round(10000 * 2 ** (valeur / 100))


def cc_vers_valeur(octet: int) -> int:
    """Octet de Control Change reçu (0..127) vers une valeur 0..100, comme l'officielle."""
    valeur = math.trunc((octet + 0.63) * 100 / 127)
    return valeur + 1 if octet not in (0, 127) else valeur


def version_texte(version: int) -> str:
    """Octet de version vers son écriture : 23 donne « 2.3 »."""
    if version < 100:
        return f"{version // 10}.{version % 10}"
    return f"{version // 100}.{version % 100 // 10}.{version % 10}"
