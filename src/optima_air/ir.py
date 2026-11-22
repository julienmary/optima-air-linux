"""Calculs sur une réponse impulsionnelle telle que la pédale la stocke.

Une IR stockée fait 2048 échantillons float32 à 48 kHz, en ordre temporel INVERSÉ :
l'échantillon de temps t = 0 est le dernier du tampon. La pédale n'applique que les 1024
premiers coefficients en temps naturel (21,3 ms), d'après l'analyse de son firmware : la
courbe est donc calculée sur ceux-là, pour montrer ce qu'on entend.
"""

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import numpy.typing as npt
import scipy.signal
import soundfile

from optima_air import codec
from optima_air.erreurs import ErreurBornes

FREQUENCE_HZ = 48000
NB_ECHANTILLONS = codec.TAILLE_IR // 4
COEFFICIENTS_APPLIQUES = 1024
TAILLE_FFT = 16384
PLANCHER_DB = -120.0


def impulsion_naturelle(donnees: bytes) -> npt.NDArray[np.float64]:
    """Les 2048 échantillons dans l'ordre du temps (t = 0 en premier)."""
    if len(donnees) != codec.TAILLE_IR:
        raise ErreurBornes(f"une IR fait {codec.TAILLE_IR} octets, reçu {len(donnees)}")
    stockee = np.frombuffer(donnees, dtype="<f4")
    return stockee[::-1].astype(np.float64)


def reponse_en_frequence(
    donnees: bytes, nb_points: int = 400, f_min: float = 20.0, f_max: float = 20000.0
) -> tuple[list[float], list[float]]:
    """Module de la réponse, en dB, sur nb_points fréquences réparties en échelle
    logarithmique de f_min à f_max. Calculé sur les coefficients que la pédale applique."""
    if nb_points < 2 or not 0 < f_min < f_max <= FREQUENCE_HZ / 2:
        raise ErreurBornes("plage de fréquences ou nombre de points invalide")
    entendue = impulsion_naturelle(donnees)[:COEFFICIENTS_APPLIQUES]
    module = np.abs(np.fft.rfft(entendue, TAILLE_FFT))
    frequences = np.geomspace(f_min, f_max, nb_points)
    indices = np.clip(np.rint(frequences * TAILLE_FFT / FREQUENCE_HZ), 0, len(module) - 1)
    plancher = 10.0 ** (PLANCHER_DB / 20.0)
    decibels = 20.0 * np.log10(np.maximum(module[indices.astype(np.int64)], plancher))
    return [float(f) for f in frequences], [float(d) for d in decibels]


# --- préparation d'un WAV pour l'upload, et export ---

CRETE_INHABITUELLE = 30.0
BINS_NORMALISATION = slice(136, 1024)  # environ 400 Hz à 3 kHz sur une FFT de 16384 points
# En-tête des exports de l'appli officielle : WAVE_FORMAT_EXTENSIBLE, float32, mono, 48 kHz,
# 8192 octets de données. Recopié d'un export réel.
EN_TETE_WAV = bytes.fromhex(
    "524946463c20000057415645666d742028000000feff010080bb000000ee0200"
    "0400200016002000040000000300000000001000800000aa00389b7164617461"
    "00200000"
)


@dataclass(frozen=True)
class IrPreparee:
    """Une IR prête à être écrite dans un slot, et ce qu'il faut dire à l'utilisateur."""

    donnees: bytes
    avertissements: tuple[str, ...]


def preparer_wav(chemin: Path) -> IrPreparee:
    """Lit un WAV et le met dans la forme que la pédale stocke, comme le fait l'appli
    officielle : premier canal, 48 kHz, 2048 premiers échantillons, temps inversé, puis
    normalisation spectrale (le milieu, en dB, entre le maximum et le minimum du module de
    400 Hz à 3 kHz est ramené à 0 dB). ErreurBornes si le fichier est inutilisable."""
    try:
        echantillons, frequence = soundfile.read(str(chemin), dtype="float64", always_2d=True)
    except Exception as erreur:  # soundfile lève ses propres types selon le défaut du fichier
        raise ErreurBornes(f"Fichier audio illisible : {chemin} ({erreur})") from erreur
    avertissements: list[str] = []
    if echantillons.shape[1] > 1:
        avertissements.append(
            f"Le fichier a {echantillons.shape[1]} canaux : seul le premier est gardé."
        )
    signal = np.ascontiguousarray(echantillons[:, 0])
    if len(signal) == 0:
        raise ErreurBornes(f"Fichier audio vide : {chemin}")
    if not np.all(np.isfinite(signal)):
        raise ErreurBornes(f"Le fichier contient des échantillons non finis : {chemin}")
    if frequence != FREQUENCE_HZ:
        diviseur = math.gcd(FREQUENCE_HZ, int(frequence))
        signal = scipy.signal.resample_poly(
            signal, FREQUENCE_HZ // diviseur, int(frequence) // diviseur
        )
        avertissements.append(
            f"Fréquence de {frequence} Hz : le fichier a été rééchantillonné en 48 kHz."
        )
    if len(signal) > NB_ECHANTILLONS:
        avertissements.append(
            f"Le fichier dure {len(signal) / FREQUENCE_HZ * 1000:.0f} ms : seuls les "
            f"{NB_ECHANTILLONS} premiers échantillons (42,7 ms) sont gardés."
        )
    naturelle = np.zeros(NB_ECHANTILLONS, dtype=np.float64)
    utile = min(len(signal), NB_ECHANTILLONS)
    naturelle[:utile] = signal[:utile]
    if np.any(naturelle[COEFFICIENTS_APPLIQUES:] != 0.0):
        avertissements.append(
            "La pédale ne joue que les 1024 premiers échantillons (21,3 ms) : la suite de "
            "cette IR sera stockée mais pas entendue."
        )
    module = np.abs(np.fft.rfft(naturelle, TAILLE_FFT))[BINS_NORMALISATION]
    produit = float(module.max() * module.min())
    if not math.isfinite(produit) or produit <= 0.0:
        raise ErreurBornes(
            f"Impossible de normaliser {chemin} : aucun signal entre 400 Hz et 3 kHz."
        )
    normalisee = (naturelle / math.sqrt(produit)).astype("<f4")
    if not np.all(np.isfinite(normalisee)):
        raise ErreurBornes(f"La normalisation de {chemin} dépasse la plage des flottants.")
    crete = float(np.max(np.abs(normalisee)))
    if crete > CRETE_INHABITUELLE:
        avertissements.append(
            f"Après normalisation, l'IR atteint {crete:.0f} en crête (les IR habituelles "
            "restent sous 10) : le fichier a un creux très profond entre 400 Hz et 3 kHz, "
            "et le niveau de sortie risque d'être très élevé."
        )
    return IrPreparee(normalisee[::-1].tobytes(), tuple(avertissements))


def contenu_wav(donnees: bytes) -> bytes:
    """Le fichier WAV que l'appli officielle exporte pour cette IR : 8260 octets, temps
    remis à l'endroit, sans gain."""
    if len(donnees) != codec.TAILLE_IR:
        raise ErreurBornes(f"une IR fait {codec.TAILLE_IR} octets, reçu {len(donnees)}")
    stockee = np.frombuffer(donnees, dtype="<f4")
    return EN_TETE_WAV + stockee[::-1].tobytes()
