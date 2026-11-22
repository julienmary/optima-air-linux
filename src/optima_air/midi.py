"""Découpage d'un flux MIDI brut en événements.

Calcul pur : aucun appel système, aucune dépendance au port. Le port rawmidi (transport)
s'en sert pour lire ce que la pédale envoie.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SysEx:
    octets: bytes


@dataclass(frozen=True)
class ControlChange:
    canal: int
    numero: int
    valeur: int


@dataclass(frozen=True)
class ProgramChange:
    canal: int
    programme: int


type Evenement = SysEx | ControlChange | ProgramChange

TAILLE_MAX_SYSEX = 1024
"""Taille maximale d'un SysEx, F0 et F7 compris. La pédale n'en émet pas de plus de 128."""


class Decoupeur:
    """Découpe un flux MIDI brut en événements. Les messages autres que SysEx,
    Control Change et Program Change sont écartés.

    Un message de canal intercalé au milieu d'un SysEx n'a jamais été observé sur la
    pédale. S'il survient, le SysEx est abandonné, le message de canal est rendu, puis
    les octets de données qui suivent (la queue du SysEx) sont ignorés jusqu'au prochain
    statut explicite : sans cela, ils seraient pris pour du statut courant et
    fabriqueraient de faux événements. Un SysEx de plus de TAILLE_MAX_SYSEX octets est
    abandonné de la même façon. `sysex_abandonnes` compte ces abandons, pour le
    diagnostic. Après une perte d'octets dans le flux, `resynchroniser` applique la même
    règle.
    """

    def __init__(self) -> None:
        self.sysex_abandonnes = 0
        # SysEx en cours d'accumulation.
        self._sysex: bytearray | None = None
        # SysEx trop long en cours de réception : ses octets sont jetés jusqu'au
        # prochain statut, qui est traité comme s'il interrompait un SysEx.
        self._sysex_rejete = False
        self._statut: int | None = None
        self._donnees: list[int] = []
        # Le statut courant ne vaut que pour un message : il a interrompu un SysEx.
        self._statut_unique = False

    def alimenter(self, octets: bytes) -> list[Evenement]:
        sortie: list[Evenement] = []
        for octet in octets:
            if octet >= 0xF8:  # temps réel : peut tomber n'importe où, sans effet
                continue
            evenement = self._statut_recu(octet) if octet >= 0x80 else self._donnee_recue(octet)
            if evenement is not None:
                sortie.append(evenement)
        return sortie

    def resynchroniser(self) -> None:
        """Signale un trou dans le flux : des octets ont été perdus avant les prochains.

        Ce qui suit ne se raccorde pas à ce qui précède. Le SysEx en cours est abandonné
        (et compté dans `sysex_abandonnes`), le statut courant et les données en attente
        sont annulés, et les octets de données qui suivent sont ignorés jusqu'au prochain
        statut explicite, comme la queue d'un SysEx interrompu. Un SysEx trop long en cours
        de rejet a déjà été compté : il ne l'est pas une seconde fois.
        """
        if self._sysex is not None:
            self.sysex_abandonnes += 1
        self._sysex = None
        self._sysex_rejete = False
        # Sans statut, _donnee_recue ignore les octets de données.
        self._statut = None
        self._donnees = []
        self._statut_unique = False

    def _statut_recu(self, octet: int) -> Evenement | None:
        dans_un_sysex = self._sysex is not None or self._sysex_rejete
        sysex = self._sysex
        self._sysex = None
        self._sysex_rejete = False
        self._statut = None
        self._donnees = []
        self._statut_unique = False
        if octet == 0xF7:
            # Fin de SysEx ; isolé, il annule le statut courant (fait ci-dessus).
            if sysex is None:
                return None
            sysex.append(octet)
            return SysEx(bytes(sysex))
        if sysex is not None:  # tout autre statut interrompt le SysEx en cours
            self.sysex_abandonnes += 1
        if octet == 0xF0:
            self._sysex = bytearray([octet])
        elif octet < 0xF0:  # statut de canal
            self._statut = octet
            self._statut_unique = dans_un_sysex
        # Statut système commun (F1 à F6) : écarté, ses données éventuelles aussi.
        return None

    def _donnee_recue(self, octet: int) -> Evenement | None:
        if self._sysex is not None:
            # Il faut encore la place du F7 final.
            if len(self._sysex) >= TAILLE_MAX_SYSEX - 1:
                self._sysex = None
                self._sysex_rejete = True
                self.sysex_abandonnes += 1
            else:
                self._sysex.append(octet)
            return None
        if self._statut is None:
            return None
        self._donnees.append(octet)
        genre = self._statut & 0xF0
        attendu = 1 if genre in (0xC0, 0xD0) else 2
        if len(self._donnees) < attendu:
            return None
        canal = self._statut & 0x0F
        evenement: Evenement | None = None
        if genre == 0xB0:
            evenement = ControlChange(canal, self._donnees[0], self._donnees[1])
        elif genre == 0xC0:
            evenement = ProgramChange(canal, self._donnees[0])
        self._donnees = []
        if self._statut_unique:
            self._statut = None
            self._statut_unique = False
        return evenement
