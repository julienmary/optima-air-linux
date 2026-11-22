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
from optima_air.i18n import _, decimal

FREQUENCE_HZ = 48000
NB_ECHANTILLONS = codec.TAILLE_IR // 4
COEFFICIENTS_APPLIQUES = 1024
TAILLE_FFT = 16384
PLANCHER_DB = -120.0


def impulsion_naturelle(donnees: bytes) -> npt.NDArray[np.float64]:
    """Les 2048 échantillons dans l'ordre du temps (t = 0 en premier)."""
    if len(donnees) != codec.TAILLE_IR:
        raise ErreurBornes(
            _("an IR is {expected} bytes, got {got}").format(
                expected=codec.TAILLE_IR, got=len(donnees)
            )
        )
    stockee = np.frombuffer(donnees, dtype="<f4")
    return stockee[::-1].astype(np.float64)


def reponse_en_frequence(
    donnees: bytes, nb_points: int = 400, f_min: float = 20.0, f_max: float = 20000.0
) -> tuple[list[float], list[float]]:
    """Module de la réponse, en dB, sur nb_points fréquences réparties en échelle
    logarithmique de f_min à f_max. Calculé sur les coefficients que la pédale applique."""
    if nb_points < 2 or not 0 < f_min < f_max <= FREQUENCE_HZ / 2:
        raise ErreurBornes(_("invalid frequency range or number of points"))
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
        raise ErreurBornes(
            _("Unreadable audio file: {file} ({error})").format(file=chemin, error=erreur)
        ) from erreur
    avertissements: list[str] = []
    if echantillons.shape[1] > 1:
        avertissements.append(
            _("The file has {count} channels: only the first one is kept.").format(
                count=echantillons.shape[1]
            )
        )
    signal = np.ascontiguousarray(echantillons[:, 0])
    if len(signal) == 0:
        raise ErreurBornes(_("Empty audio file: {file}").format(file=chemin))
    if not np.all(np.isfinite(signal)):
        raise ErreurBornes(_("The file contains non-finite samples: {file}").format(file=chemin))
    if frequence != FREQUENCE_HZ:
        diviseur = math.gcd(FREQUENCE_HZ, int(frequence))
        signal = scipy.signal.resample_poly(
            signal, FREQUENCE_HZ // diviseur, int(frequence) // diviseur
        )
        avertissements.append(
            _("Sample rate of {rate} Hz: the file was resampled to {target} kHz.").format(
                rate=frequence, target=FREQUENCE_HZ // 1000
            )
        )
    if len(signal) > NB_ECHANTILLONS:
        avertissements.append(
            _(
                "The file lasts {duration} ms: only the first {count} samples ({limit} ms) "
                "are kept."
            ).format(
                duration=decimal(len(signal) / FREQUENCE_HZ * 1000, 0),
                count=NB_ECHANTILLONS,
                limit=decimal(NB_ECHANTILLONS / FREQUENCE_HZ * 1000, 1),
            )
        )
    naturelle = np.zeros(NB_ECHANTILLONS, dtype=np.float64)
    utile = min(len(signal), NB_ECHANTILLONS)
    naturelle[:utile] = signal[:utile]
    if np.any(naturelle[COEFFICIENTS_APPLIQUES:] != 0.0):
        avertissements.append(
            _(
                "The pedal only plays the first {count} samples ({limit} ms): the rest of "
                "this IR will be stored but not heard."
            ).format(
                count=COEFFICIENTS_APPLIQUES,
                limit=decimal(COEFFICIENTS_APPLIQUES / FREQUENCE_HZ * 1000, 1),
            )
        )
    module = np.abs(np.fft.rfft(naturelle, TAILLE_FFT))[BINS_NORMALISATION]
    produit = float(module.max() * module.min())
    if not math.isfinite(produit) or produit <= 0.0:
        raise ErreurBornes(
            _("Cannot normalise {file}: no signal between 400 Hz and 3 kHz.").format(file=chemin)
        )
    normalisee = (naturelle / math.sqrt(produit)).astype("<f4")
    if not np.all(np.isfinite(normalisee)):
        raise ErreurBornes(
            _("Normalising {file} exceeds the range of floating-point numbers.").format(file=chemin)
        )
    crete = float(np.max(np.abs(normalisee)))
    if crete > CRETE_INHABITUELLE:
        avertissements.append(
            _(
                "After normalisation, the IR peaks at {peak} (usual IRs stay below 10): the "
                "file has a very deep dip between 400 Hz and 3 kHz, and the output level may "
                "be very high."
            ).format(peak=decimal(crete, 0))
        )
    return IrPreparee(normalisee[::-1].tobytes(), tuple(avertissements))


def contenu_wav(donnees: bytes) -> bytes:
    """Le fichier WAV que l'appli officielle exporte pour cette IR : 8260 octets, temps
    remis à l'endroit, sans gain."""
    if len(donnees) != codec.TAILLE_IR:
        raise ErreurBornes(
            _("an IR is {expected} bytes, got {got}").format(
                expected=codec.TAILLE_IR, got=len(donnees)
            )
        )
    stockee = np.frombuffer(donnees, dtype="<f4")
    return EN_TETE_WAV + stockee[::-1].tobytes()
