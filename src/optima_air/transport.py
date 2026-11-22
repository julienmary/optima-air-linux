"""Port ALSA rawmidi de la pédale : détection de la carte, verrou, port.

Le découpage du flux en événements est dans midi.
"""

import contextlib
import errno
import fcntl
import math
import os
import re
import select
import stat
import struct
from pathlib import Path
from typing import Protocol

from optima_air import midi
from optima_air.erreurs import (
    AccesRefuse,
    ErreurBornes,
    ErreurOptimaAir,
    PedaleAbsente,
    PedaleDebranchee,
    PortOccupe,
)
from optima_air.i18n import _


class Transport(Protocol):
    """Ce que la session attend d'un port. La fausse pédale des tests l'implémente aussi."""

    def ecrire(self, octets: bytes) -> None: ...

    def lire(self, attente_s: float) -> list[midi.Evenement]: ...

    def fermer(self) -> None: ...


_LIGNE_CARTE = re.compile(r"^\s*(\d+)\s+\[(\S+)\s*\]")


def trouver_port(cartes: str) -> str:
    """Chemin du port rawmidi, à partir du contenu de /proc/asound/cards."""
    for ligne in cartes.splitlines():
        trouve = _LIGNE_CARTE.match(ligne)
        if trouve and trouve.group(2) == "NAI5":
            return f"/dev/snd/midiC{trouve.group(1)}D0"
    raise PedaleAbsente(_("Pedal not found: no “NAI5” sound card. Check the USB cable."))


def _refus_du_verrou(chemin: Path, erreur: OSError) -> str:
    """Raison d'un échec sur le fichier de verrou. Avec O_NOFOLLOW, un lien symbolique en
    dernier composant échoue en ELOOP, comme une boucle de liens plus haut dans le chemin :
    lstat, qui ne suit pas le lien, dit lequel des deux."""
    if erreur.errno == errno.ELOOP:
        with contextlib.suppress(OSError):
            if stat.S_ISLNK(os.lstat(chemin).st_mode):
                return _("is a symbolic link")
    return str(erreur)


class Verrou:
    """Verrou exclusif entre processus : un seul parle à la pédale à la fois.

    Il ne protège que contre une autre instance d'optima-air : contre un programme tiers,
    c'est le noyau qui refuse la seconde ouverture de la sortie du port. Le PortRawmidi
    à qui on le confie en prend possession et le libère lui-même. Un verrou ne se confie
    qu'une fois, à un seul port : `confier` refuse la seconde fois.

    Le fichier de verrou ($XDG_RUNTIME_DIR/optima-air.lock) doit être un fichier ordinaire :
    un lien symbolique à sa place, même cassé, n'est jamais suivi, et tout autre type est
    refusé avant d'être verrouillé (ErreurOptimaAir).
    """

    def __init__(self) -> None:
        self._fd = -1
        self._confie = False
        dossier = os.environ.get("XDG_RUNTIME_DIR")
        if not dossier:
            raise ErreurOptimaAir(_("XDG_RUNTIME_DIR is not set: cannot take the lock."))
        chemin = Path(dossier) / "optima-air.lock"
        descripteur = -1
        try:
            # L'ouverture est dans le try : une interruption qui tombe une fois le
            # descripteur affecté ne le fait pas fuir. O_NOFOLLOW : un lien symbolique à la
            # place du verrou, même cassé, est refusé sans être suivi ; rien n'est créé à
            # travers lui, et sa cible (le port de la pédale, par exemple) n'est jamais
            # ouverte. O_NONBLOCK : une FIFO ne bloque pas l'ouverture.
            descripteur = os.open(
                chemin,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                0o600,
            )
            # Contrôlé sur le descripteur ouvert, avant tout flock.
            if not stat.S_ISREG(os.fstat(descripteur).st_mode):
                raise ErreurOptimaAir(
                    _("Cannot take the lock {file}: {error}").format(
                        file=chemin, error=_("not a regular file")
                    )
                )
            fcntl.flock(descripteur, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException as erreur:
            if descripteur >= 0:
                # L'erreur d'origine prime sur un éventuel échec de la fermeture.
                with contextlib.suppress(OSError):
                    os.close(descripteur)
                if isinstance(erreur, BlockingIOError):  # flock : verrou déjà pris
                    raise PortOccupe(
                        _("The pedal is already used by another instance of optima-air.")
                    ) from None
            if isinstance(erreur, OSError):
                raise ErreurOptimaAir(
                    _("Cannot take the lock {file}: {error}").format(
                        file=chemin, error=_refus_du_verrou(chemin, erreur)
                    )
                ) from erreur
            raise
        self._fd = descripteur

    @property
    def tenu(self) -> bool:
        """Vrai tant que le verrou n'a pas été libéré."""
        return self._fd >= 0

    @property
    def confie(self) -> bool:
        """Vrai une fois le verrou confié à un port (il le reste après la libération)."""
        return self._confie

    def confier(self) -> None:
        """Marque le verrou comme confié à un port, qui en prend possession.

        Lève ErreurOptimaAir, sans rien changer, si le verrou a déjà été libéré ou s'il a
        déjà été confié : il appartient alors à un autre port, qui seul peut le libérer.
        Aucun appel de fonction ici, pas même `tenu` : une interruption ne peut tomber
        qu'à l'entrée, avant que le verrou ne soit marqué.
        """
        if self._fd < 0:
            raise ErreurOptimaAir(
                _("The lock has already been released: it can no longer be given to a port.")
            )
        if self._confie:
            raise ErreurOptimaAir(
                _("This lock already belongs to a port: it cannot be given to a second one.")
            )
        self._confie = True

    def liberer(self) -> None:
        """Relâche le verrou. Sans effet s'il l'est déjà."""
        descripteur, self._fd = self._fd, -1
        if descripteur < 0:
            return
        try:
            try:
                fcntl.flock(descripteur, fcntl.LOCK_UN)
            finally:
                # La fermeture relâche le verrou de toute façon.
                os.close(descripteur)
        except OSError as erreur:
            raise ErreurOptimaAir(
                _("Cannot release the lock: {error}").format(error=erreur)
            ) from erreur


_VIDAGE = 0x40045731  # SNDRV_RAWMIDI_IOCTL_DRAIN = _IOW('W', 0x31, int)
_SORTIE = 0  # SNDRV_RAWMIDI_STREAM_OUTPUT
_ENTREE = 1  # SNDRV_RAWMIDI_STREAM_INPUT
# SNDRV_RAWMIDI_IOCTL_STATUS = _IOWR('W', 0x20, struct snd_rawmidi_status), d'après
# /usr/include/sound/asound.h (le noyau le nomme SNDRV_RAWMIDI_IOCTL_STATUS64). Sur x86-64,
# la structure fait 56 octets : int stream, 4 octets d'alignement, struct timespec
# (2 × 8 octets), size_t avail, size_t xruns (offset 32), 16 octets réservés. Sur une
# architecture où la taille ou le codage diffèrent, le numéro ne correspond à rien, le
# noyau répond ENOTTY, et lire() refuse de continuer.
_ETAT = 0xC0385720
_FORMAT_ETAT = struct.Struct("=i4xqqQQ16x")
_DEMANDE_D_ETAT = _FORMAT_ETAT.pack(_ENTREE, 0, 0, 0, 0)
# Le tampon d'entrée du noyau fait PAGE_SIZE octets (4096 sur x86-64). Une lecture au moins
# aussi grande le vide entièrement : c'est ce qui situe un trou juste après elle.
_TAILLE_LECTURE = max(4096, os.sysconf("SC_PAGE_SIZE"))
_ATTENTE_MAX_S = 3600.0
_REPRISES_MAX_DU_VIDAGE = 100
_SONDAGE_EN_ERREUR = select.POLLERR | select.POLLHUP | select.POLLNVAL
_ERRNO_DEBRANCHE = (errno.ENODEV, errno.EIO, errno.EBADFD)


# EIO n'est pas forcément un débranchement : un délai dépassé pendant le vidage du port
# le rend aussi, pédale branchée.
def _message_debranche() -> str:
    return _("The pedal no longer answers: unplugged or stuck.")


class PortRawmidi:
    """Port rawmidi réel. Le pilote du noyau reste en place.

    Aucune OSError ne s'échappe : les erreurs système sont traduites en erreurs nommées,
    ou enveloppées dans ErreurOptimaAir.

    Pas sûr entre fils d'exécution : un seul fil le pilote, `fermer` compris.

    `debordements` compte les débordements détectés du tampon d'entrée du noyau (des
    octets envoyés par la pédale ont été perdus).

    Le port prend possession du Verrou qu'on lui confie (Verrou.confier), dès que celui-ci
    l'accepte : `fermer` le libère, et un constructeur en échec aussi. L'appelant ne le
    libère pas lui-même. Un verrou ne se confie qu'à un seul port. Ce verrou ne protège
    que contre une autre instance d'optima-air : contre un programme tiers, c'est le noyau
    qui refuse la seconde ouverture de la sortie (PortOccupe).
    """

    def __init__(self, chemin: str, verrou: Verrou) -> None:
        """Ouvre le port, sous la protection de `verrou`, dont il prend possession.

        Le verrou doit être tenu, et n'avoir été confié à aucun port : sinon
        ErreurOptimaAir est levée sans que le port soit ouvert, et sans que le verrou soit
        libéré (il n'y a rien à libérer, ou il appartient à un autre port). Une fois le
        verrou accepté, si la construction échoue, pour quelque raison que ce soit
        (KeyboardInterrupt comprise), les descripteurs déjà ouverts sont fermés, puis le
        verrou est libéré, avant que l'erreur ne remonte.
        """
        # En tout premier, avant tout appel système sur le port, et hors du try : si le
        # verrou est refusé, fermer ne doit pas le libérer. Dans le doute (interruption à
        # l'entrée de confier), mieux vaut un verrou qui reste pris qu'un verrou retiré au
        # port qui le tient.
        verrou.confier()
        # Avant le try : fermer, appelé en cas d'échec, en a besoin. Des affectations
        # seules, sans appel : CPython n'y traite pas de signal, aucune interruption ne
        # tombe entre confier et le try.
        self._verrou = verrou
        self._lecture = -1
        self._ecriture = -1
        try:
            self._chemin = chemin
            self._decoupeur = midi.Decoupeur()
            self.debordements = 0
            # La première ouverture est dans le try : une interruption qui tombe une fois
            # le descripteur affecté ne le fait pas fuir (fermer ignore les -1).
            self._lecture = self._ouvrir(chemin, os.O_RDONLY | os.O_NONBLOCK)
            # Ouverture non bloquante : si un autre programme tient la sortie,
            # l'appel échoue au lieu de se figer dans le noyau.
            self._ecriture = self._ouvrir(chemin, os.O_WRONLY | os.O_NONBLOCK)
            try:
                os.set_blocking(self._ecriture, True)
            except OSError as erreur:
                raise ErreurOptimaAir(
                    _("Cannot configure the port {port}: {error}").format(port=chemin, error=erreur)
                ) from erreur
            # Armement de l'entrée : le noyau ne met en tampon ce que la pédale envoie
            # qu'à partir du premier poll ou read sur le descripteur de lecture ; avant,
            # le pilote USB le jette. Sans cela, la réponse à la toute première requête
            # pourrait être perdue. Le résultat importe peu : ce qui est déjà arrivé reste
            # dans le tampon pour lire().
            try:
                self._sonder(0)
            except OSError as erreur:
                raise self._traduire(
                    erreur, _("Cannot arm the input of port {port}: {error}")
                ) from erreur
        except BaseException:
            # L'erreur d'origine prime sur un éventuel échec de la fermeture. fermer
            # libère aussi le verrou, après les descripteurs, même si l'un d'eux résiste.
            with contextlib.suppress(ErreurOptimaAir):
                self.fermer()
            raise

    @staticmethod
    def _ouvrir(chemin: str, drapeaux: int) -> int:
        try:
            return os.open(chemin, drapeaux)
        except FileNotFoundError:
            raise PedaleAbsente(_("The port {port} does not exist.").format(port=chemin)) from None
        except PermissionError:
            raise AccesRefuse(
                _(
                    "Access denied to {port}. Check that you are in the “audio” group "
                    "(command: id)."
                ).format(port=chemin)
            ) from None
        except OSError as erreur:
            if erreur.errno in (errno.EBUSY, errno.EAGAIN):
                raise PortOccupe(
                    _("The port {port} is already used by another program.").format(port=chemin)
                ) from None
            # ENXIO : la carte n'est plus disponible.
            if erreur.errno in (errno.ENODEV, errno.ENXIO):
                raise PedaleAbsente(
                    _("The port {port} is no longer available.").format(port=chemin)
                ) from None
            raise ErreurOptimaAir(
                _("Cannot open the port {port}: {error}").format(port=chemin, error=erreur)
            ) from erreur

    def _traduire(self, erreur: OSError, message: str) -> ErreurOptimaAir:
        """`message` : phrase entière, déjà traduite, avec les champs {port} et {error}."""
        if erreur.errno in _ERRNO_DEBRANCHE:
            return PedaleDebranchee(_message_debranche())
        return ErreurOptimaAir(message.format(port=self._chemin, error=erreur))

    def ecrire(self, octets: bytes) -> None:
        """Écrit une trame entière puis attend qu'elle soit partie sur le fil.

        PedaleDebranchee : la pédale est débranchée, ou figée. Sur délai dépassé (30 s en
        écriture, 10 s au vidage), le noyau rend EIO et jette les octets non partis : la
        pédale a pu recevoir une trame tronquée, son état est alors inconnu.
        """
        if self._ecriture < 0:
            raise ErreurOptimaAir(_("Cannot write: the port is closed."))
        try:
            reste = memoryview(octets)
            while reste:
                ecrits = os.write(self._ecriture, reste)
                if ecrits <= 0:
                    # Jamais vu en écriture bloquante : on refuse de boucler sans fin.
                    raise ErreurOptimaAir(
                        _("Writing to port {port} failed: no byte accepted.").format(
                            port=self._chemin
                        )
                    )
                reste = reste[ecrits:]
            self._vider()
        except OSError as erreur:
            # EIO donne PedaleDebranchee : le noyau a jeté la fin de la trame, l'état de la
            # pédale est inconnu.
            raise self._traduire(erreur, _("Writing to port {port} failed: {error}")) from erreur

    def _vider(self) -> None:
        """Attend que la sortie soit partie sur le fil.

        Un signal interrompt le vidage (EINTR, que Python ne relance pas pour ioctl) sans
        que le noyau jette la sortie : la trame part quand même. Le vidage est idempotent
        et la trame est déjà dans le noyau : on reprend le vidage seul, sans la réécrire.
        KeyboardInterrupt, elle, remonte telle quelle.
        """
        reprises = 0
        while True:
            try:
                fcntl.ioctl(self._ecriture, _VIDAGE, struct.pack("i", _SORTIE))
                return
            except InterruptedError as erreur:
                if reprises >= _REPRISES_MAX_DU_VIDAGE:
                    raise ErreurOptimaAir(
                        _(
                            "Draining the port {port} was interrupted {count} times in a row: "
                            "the message is handed to the kernel, but its departure could not "
                            "be awaited."
                        ).format(port=self._chemin, count=reprises + 1)
                    ) from erreur
                reprises += 1

    @property
    def sysex_abandonnes(self) -> int:
        """Nombre de SysEx abandonnés par le découpeur depuis l'ouverture (diagnostic)."""
        return self._decoupeur.sysex_abandonnes

    def lire(self, attente_s: float) -> list[midi.Evenement]:
        """Événements reçus, en attendant au plus attente_s s'il n'y a rien.

        Une attente négative vaut 0 ; non finie ou au-delà de 3600 s, elle lève
        ErreurBornes. Si le noyau a perdu des octets faute de place dans son tampon
        d'entrée, les événements complets reçus avant le trou sont rendus, le découpeur
        est resynchronisé et `debordements` augmente de 1.
        """
        if not math.isfinite(attente_s) or attente_s > _ATTENTE_MAX_S:
            raise ErreurBornes(
                _("Wait out of range: {wait} s (at most {max} s).").format(
                    wait=attente_s, max=f"{_ATTENTE_MAX_S:g}"
                )
            )
        if self._lecture < 0:
            raise ErreurOptimaAir(_("Cannot read: the port is closed."))
        # Pour poll, un délai négatif veut dire « infini » : plancher à 0. Millisecondes
        # entières arrondies vers le haut, pour ne pas tourner à vide sur une attente
        # inférieure à la milliseconde.
        delai_ms = math.ceil(max(attente_s, 0.0) * 1000)
        try:
            if not self._sonder(delai_ms):
                return []
            octets = os.read(self._lecture, _TAILLE_LECTURE)
        except BlockingIOError:
            return []
        except OSError as erreur:
            raise self._traduire(erreur, _("Reading from port {port} failed: {error}")) from erreur
        if not octets:
            # Branche défensive : read sur un rawmidi rend plus de 0 octet ou EAGAIN, jamais
            # 0. Un 0 voudrait dire que ce n'est plus le port attendu.
            raise PedaleDebranchee(_message_debranche())
        try:
            perdus = self._octets_perdus()
            evenements = self._decoupeur.alimenter(octets)
        except BaseException:
            # Les octets lus sont perdus pour l'appelant : pour le découpeur, c'est un trou.
            self._decoupeur.resynchroniser()
            raise
        if perdus > 0:
            # Le noyau ne perd des octets que tampon plein, et la lecture vient de le vider
            # entièrement : le trou est après les octets lus. Ils passent donc d'abord au
            # découpeur, puis on le resynchronise. Seul cas contraire, négligeable : un
            # tampon de nouveau plein à ras bord entre read et l'ioctl d'état.
            self._decoupeur.resynchroniser()
            self.debordements += 1
        return evenements

    def _sonder(self, delai_ms: int) -> bool:
        """Attend au plus delai_ms des octets à lire. Vrai s'il y en a.

        Lève PedaleDebranchee si le port signale une erreur sans rien à lire (le noyau
        répond POLLERR | POLLNVAL sur une carte déconnectée). Les OSError remontent.
        """
        sondeur = select.poll()
        sondeur.register(self._lecture, select.POLLIN)
        revenus = 0
        for _descripteur, evenements in sondeur.poll(delai_ms):
            revenus |= evenements
        if revenus & select.POLLIN:
            return True
        if revenus & _SONDAGE_EN_ERREUR:
            raise PedaleDebranchee(_message_debranche())
        return False

    def _octets_perdus(self) -> int:
        """Octets perdus par le noyau, faute de place dans le tampon d'entrée, depuis la
        dernière lecture d'état. Cette lecture remet le compteur du noyau à 0."""
        try:
            etat = fcntl.ioctl(self._lecture, _ETAT, _DEMANDE_D_ETAT)
        except OSError as erreur:
            if erreur.errno in (errno.ENOTTY, errno.EINVAL):
                raise ErreurOptimaAir(
                    _(
                        "The port {port} does not report the state of its input ({error}): "
                        "byte loss cannot be detected, stopping here."
                    ).format(port=self._chemin, error=erreur)
                ) from erreur
            raise self._traduire(
                erreur, _("Reading the state of port {port} failed: {error}")
            ) from erreur
        *_champs, perdus = _FORMAT_ETAT.unpack(etat)
        return int(perdus)

    def fermer(self) -> None:
        """Ferme le port, puis libère le verrou dont il a pris possession. Sans effet
        s'il l'est déjà : un second appel ne ferme rien et ne libère rien.

        Le verrou n'est libéré qu'après la fermeture des deux descripteurs, jamais avant :
        une autre instance ne peut pas prendre la pédale tant que ce port la tient. Il
        l'est même si la fermeture d'un descripteur échoue ou est interrompue ; c'est
        alors cette erreur d'origine qui remonte, pas un éventuel échec de la libération.

        Peut bloquer jusqu'à 10 s si la pédale est figée : à la fermeture, le noyau
        attend que la sortie soit vidée.

        Chaque descripteur est oublié avant d'être fermé : Linux le libère même quand
        close échoue, et le refermer plus tard pourrait fermer un descripteur réattribué
        entre-temps (celui du Verrou, par exemple).
        """
        lecture, self._lecture = self._lecture, -1
        ecriture, self._ecriture = self._ecriture, -1
        try:
            try:
                self._fermer_descripteur(lecture)
            finally:
                self._fermer_descripteur(ecriture)
        except BaseException:
            # L'erreur d'origine prime sur un éventuel échec de la libération.
            with contextlib.suppress(ErreurOptimaAir):
                self._verrou.liberer()
            raise
        # Sans effet si le verrou est déjà libéré : fermer reste idempotent.
        self._verrou.liberer()

    def _fermer_descripteur(self, descripteur: int) -> None:
        if descripteur < 0:
            return
        try:
            os.close(descripteur)
        except OSError as erreur:
            raise ErreurOptimaAir(
                _("Closing the port {port} failed: {error}").format(port=self._chemin, error=erreur)
            ) from erreur
