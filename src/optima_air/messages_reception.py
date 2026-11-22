"""Décodage des messages envoyés par la pédale, forme exacte vérifiée.

Chaque contrôle rend une raison qui lui est propre : un écart dans une réponse de la pédale
signifie que notre compréhension du protocole est fausse, et le message doit dire lequel. Une
réponse refusée lève ErreurReponse. La description du protocole partagée avec l'émission est
dans messages.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import IntEnum

from optima_air import codec, messages
from optima_air.erreurs import ErreurBornes, ErreurCodec, ErreurReponse
from optima_air.i18n import _

NB_MESSAGES_LECTURE = 87

# Octet de type, en [6], propre à la réception.
_TYPE_REPONSE_VERSION = 0x10  # réponse de version, 9 octets, version en [7]

# Octet de commande, en [7], des messages de type 70 propres à la réception.
_CMD_ETAT = 0x27  # réponse d'état
_CMD_ETAT_BIS = 0x28  # même réponse d'état, aussi observée

_SLOT_FIN_LECTURE = 0  # en lecture, la pédale n'y met pas le slot
_LONGUEUR_ETAT = 55

# En lecture, la pédale envoie un en-tête au corps entièrement nul.
_CORPS_ENTETE_LECTURE = bytes(messages.TAILLE_CORPS)


def _refus_nux(message: bytes) -> str | None:
    """Encadrement SysEx NUX et octets de 7 bits, comme controler_trame à l'émission."""
    if message[: len(messages.ENTETE)] != messages.ENTETE:
        return _("NUX SysEx header missing")
    if message[-1] != messages.FIN:
        return _("no final F7")
    if any(octet > 0x7F for octet in message[1:-1]):
        return _("byte outside 7 bits")
    return None


def _refus_forme(
    message: bytes, longueur: int, type_: int, commandes: Collection[int] | None
) -> str | None:
    """Encadrement, longueur exacte, type et, s'il y a lieu, commande parmi celles attendues."""
    refus = _refus_nux(message)
    if refus is not None:
        return refus
    if len(message) != longueur:
        return _("{size} bytes instead of {expected}").format(size=len(message), expected=longueur)
    if message[messages.TYPE] != type_:
        return _("type {found} instead of {expected}").format(
            found=f"{message[messages.TYPE]:02X}", expected=f"{type_:02X}"
        )
    if commandes is not None and message[messages.COMMANDE] not in commandes:
        attendues = "/".join(f"{commande:02X}" for commande in sorted(commandes))
        return _("command {found} instead of {expected}").format(
            found=f"{message[messages.COMMANDE]:02X}", expected=attendues
        )
    return None


class Statut(IntEnum):
    """Statut d'un accusé d'upload (textes de l'appli officielle entre parenthèses)."""

    OK = 0x66  # « cab ok »
    ERREUR = 0x44  # « cab error »
    PAQUET_FINI = 0x56  # « cab finish one of pkg »
    FLASH_OK = 0x76  # « flash save ok »


LONGUEUR_ACCUSE = 17
_POSITION_STATUT = 11


def est_accuse(message: bytes) -> bool:
    """Accusé d'un message d'upload : 17 octets, commande du message acquitté en écho."""
    commandes = (messages.CMD_CADRE, messages.CMD_BLOC)
    return _refus_forme(message, LONGUEUR_ACCUSE, messages.TYPE_NUX, commandes) is None


def decoder_accuse(message: bytes) -> tuple[int, Statut]:
    """Commande acquittée et statut. Un statut inconnu lève ErreurReponse."""
    if not est_accuse(message):
        raise ErreurReponse(_("unexpected upload acknowledgement"))
    try:
        statut = Statut(message[_POSITION_STATUT])
    except ValueError:
        raise ErreurReponse(
            _("unknown acknowledgement status: {status}").format(
                status=f"{message[_POSITION_STATUT]:#04x}"
            )
        ) from None
    return message[messages.COMMANDE], statut


def _refus_version(message: bytes) -> str | None:
    return _refus_forme(message, messages.LONGUEUR_VERSION, _TYPE_REPONSE_VERSION, None)


def est_reponse_version(message: bytes) -> bool:
    return _refus_version(message) is None


def decoder_version(message: bytes) -> int:
    refus = _refus_version(message)
    if refus is not None:
        raise ErreurReponse(_("unexpected version answer: {error}").format(error=refus))
    return message[messages.COMMANDE]


@dataclass(frozen=True)
class InfoSlot:
    slot: int
    identifiant: int
    nom_brut: bytes

    @property
    def nom(self) -> str:
        """Nom affichable sans danger : coupé au premier octet nul, tout octet hors ASCII
        imprimable remplacé par « ? »."""
        nom = self.nom_brut.partition(b"\x00")[0]
        return "".join(chr(octet) if messages.imprimable(octet) else "?" for octet in nom)

    @property
    def nom_conforme(self) -> bool:
        """Vrai si le nom pourrait être réémis tel quel : 1 à 32 caractères ASCII imprimables,
        puis uniquement des zéros, sur 32 octets."""
        return (
            len(self.nom_brut) == messages.LONGUEUR_NOM
            and messages.refus_zone_nom(self.nom_brut) is None
        )


def _commande_infos(commande: int) -> int:
    if commande not in (messages.CMD_INFOS, messages.CMD_NOM):
        raise ErreurBornes(
            _(
                "command {command}: only 0x5c (info read) and 0x5b (name echo) carry slot info"
            ).format(command=f"{commande:#04x}")
        )
    return commande


def _refus_infos(message: bytes, commande: int) -> str | None:
    refus = _refus_forme(message, messages.LONGUEUR_TRAME_54, messages.TYPE_NUX, (commande,))
    if refus is not None:
        return refus
    if message[messages.CORPS] != messages.LONGUEUR_ENCODEE_54:
        return _("encoded length {found} instead of {expected}").format(
            found=message[messages.CORPS], expected=messages.LONGUEUR_ENCODEE_54
        )
    return None


def est_reponse_infos(message: bytes, commande: int = messages.CMD_INFOS) -> bool:
    """commande : 0x5C pour une lecture d'infos, 0x5B pour l'écho d'écriture du nom."""
    return _refus_infos(message, _commande_infos(commande)) is None


def decoder_infos_slot(message: bytes, commande: int = messages.CMD_INFOS) -> InfoSlot:
    """commande : 0x5C pour une lecture d'infos, 0x5B pour l'écho d'écriture du nom."""
    refus = _refus_infos(message, _commande_infos(commande))
    if refus is not None:
        raise ErreurReponse(_("unexpected slot info answer: {error}").format(error=refus))
    try:
        source = codec.dec7(message[messages.ZONE_54])
    except ErreurCodec as erreur:
        raise ErreurReponse(_("unreadable slot info: {error}").format(error=erreur)) from erreur
    slot = int.from_bytes(source[messages.INFO_SLOT], "little")
    if slot >= messages.NB_SLOTS:
        raise ErreurReponse(_("the pedal announces slot {slot}").format(slot=slot + 1))
    identifiant = int.from_bytes(source[messages.INFO_IDENTIFIANT], "little")
    return InfoSlot(slot, identifiant, source[messages.INFO_NOM])


def _refus_tableau(message: bytes, tableau: messages.Tableau, ecriture: bool) -> str | None:
    commande = tableau.commande_ecriture if ecriture else tableau.value
    return _refus_forme(message, messages.LONGUEUR_TABLEAU, messages.TYPE_NUX, (commande,))


def est_reponse_tableau(
    message: bytes, tableau: messages.Tableau, *, ecriture: bool = False
) -> bool:
    """Réponse à une lecture du tableau, ou écho de son écriture si ecriture est vrai."""
    return _refus_tableau(message, tableau, ecriture) is None


def decoder_tableau(
    message: bytes, tableau: messages.Tableau, *, ecriture: bool = False
) -> tuple[int, ...]:
    refus = _refus_tableau(message, tableau, ecriture)
    if refus is not None:
        raise ErreurReponse(
            _("unexpected answer for table {table}: {error}").format(
                table=tableau.name, error=refus
            )
        )
    valeurs = tuple(message[messages.CORPS : -1])
    for valeur in valeurs:
        if valeur > 100:
            raise ErreurReponse(
                _("table {table}: value {value} above 100").format(table=tableau.name, value=valeur)
            )
    return valeurs


@dataclass(frozen=True)
class Etat:
    preset: int
    nom_brut: bytes
    ir_active: bool
    slot_actif: int
    preamp_actif: bool
    ir_level: int
    gain: int
    bass: int
    middle: int
    treble: int
    octet49: int
    reverb: int
    routing: int
    usb_in: int
    usb_out: int


def _refus_etat(message: bytes) -> str | None:
    return _refus_forme(message, _LONGUEUR_ETAT, messages.TYPE_NUX, (_CMD_ETAT, _CMD_ETAT_BIS))


def est_reponse_etat(message: bytes) -> bool:
    return _refus_etat(message) is None


def decoder_etat(message: bytes) -> Etat:
    refus = _refus_etat(message)
    if refus is not None:
        raise ErreurReponse(_("unexpected state answer: {error}").format(error=refus))
    return Etat(
        preset=message[8],
        nom_brut=message[9:41],
        ir_active=message[41] != 0,
        slot_actif=message[42],
        preamp_actif=message[43] != 0,
        ir_level=message[44],
        gain=message[45],
        bass=message[46],
        middle=message[47],
        treble=message[48],
        octet49=message[49],
        reverb=message[50],
        routing=message[51],
        usb_in=message[52],
        usb_out=message[53],
    )


def _refus_transfert(message: bytes) -> str | None:
    return _refus_forme(
        message,
        messages.LONGUEUR_TRAME_128,
        messages.TYPE_NUX,
        (messages.CMD_CADRE, messages.CMD_BLOC),
    )


def est_message_de_transfert(message: bytes) -> bool:
    return _refus_transfert(message) is None


def est_pied_de_transfert(message: bytes) -> bool:
    """Reconnaît la fin d'un flux de lecture au fanion. La forme exacte du pied (zéros après
    le fanion) est exigée par assembler_lecture."""
    return (
        est_message_de_transfert(message)
        and message[messages.COMMANDE] == messages.CMD_CADRE
        and message[messages.CORPS : -1][messages.FANION] == messages.FANION_FIN
    )


def _exiger_entete_de_lecture(message: bytes) -> None:
    if message[messages.COMMANDE] != messages.CMD_CADRE:
        raise ErreurReponse(
            _("read header: command {found} instead of {expected}").format(
                found=f"{message[messages.COMMANDE]:02X}", expected=f"{messages.CMD_CADRE:02X}"
            )
        )
    corps = message[messages.CORPS : -1]
    if corps[messages.FANION] == messages.FANION_FIN:
        raise ErreurReponse(_("read header carries the end flag"))
    if corps != _CORPS_ENTETE_LECTURE:
        raise ErreurReponse(_("non-zero read header, never seen in this form"))


def _bloc_de_lecture(k: int, message: bytes) -> bytes:
    """Les 98 octets bruts du bloc k, après contrôle exact de sa forme et de sa somme."""
    if message[messages.COMMANDE] != messages.CMD_BLOC:
        raise ErreurReponse(
            _("block {block}: command {found} instead of {expected}").format(
                block=k,
                found=f"{message[messages.COMMANDE]:02X}",
                expected=f"{messages.CMD_BLOC:02X}",
            )
        )
    corps = message[messages.CORPS : -1]
    if corps[messages.DRAPEAU] != 0:
        raise ErreurReponse(
            _("block {block}: non-zero flag {flag}").format(
                block=k, flag=f"{corps[messages.DRAPEAU]:02X}"
            )
        )
    if corps[messages.ADRESSE] != messages.adresse_bloc(k):
        raise ErreurReponse(
            _("block {block}: address {found} instead of {expected}").format(
                block=k,
                found=corps[messages.ADRESSE].hex().upper(),
                expected=messages.adresse_bloc(k).hex().upper(),
            )
        )
    if corps[messages.COMPTEUR] != k:
        raise ErreurReponse(
            _("block {block}: counter {found} instead of {expected}").format(
                block=k, found=corps[messages.COMPTEUR], expected=k
            )
        )
    try:
        return codec.dec_msb(corps[messages.ZONE_ENCODEE])
    except ErreurCodec as erreur:
        raise ErreurReponse(
            _("block {block} unreadable: {error}").format(block=k, error=erreur)
        ) from erreur


def _exiger_bloc_de_fin_de_lecture(message: bytes, blocs: bytes) -> None:
    if message[messages.COMMANDE] != messages.CMD_BLOC:
        raise ErreurReponse(
            _("end block: command {found} instead of {expected}").format(
                found=f"{message[messages.COMMANDE]:02X}", expected=f"{messages.CMD_BLOC:02X}"
            )
        )
    corps = message[messages.CORPS : -1]
    if corps[messages.FANION] != messages.FANION_FIN:
        raise ErreurReponse(_("end block without flag"))
    if corps[messages.COMPTEUR] != codec.NB_BLOCS:
        raise ErreurReponse(
            _("end block: counter {found} instead of {expected}").format(
                found=corps[messages.COMPTEUR], expected=codec.NB_BLOCS
            )
        )
    if corps != messages.corps_bloc_de_fin(corps[messages.SOMME_FIN], _SLOT_FIN_LECTURE):
        raise ErreurReponse(_("end block: stray byte outside the flag, the sum and the counter"))
    try:
        annoncee = codec.dec28(corps[messages.SOMME_FIN])
    except ErreurCodec as erreur:
        # Garde inatteignable : dec28 ne refuse qu'un octet de plus de 7 bits, déjà refusé
        # par _refus_nux. Gardée pour qu'une ErreurCodec ne s'échappe jamais d'ici.
        raise ErreurReponse(_("unreadable end sum: {error}").format(error=erreur)) from erreur
    if annoncee != codec.somme_ponderee(blocs):
        raise ErreurReponse(_("wrong end-of-read sum"))


def _exiger_pied_de_lecture(message: bytes) -> None:
    if message[messages.COMMANDE] != messages.CMD_CADRE:
        raise ErreurReponse(
            _("footer: command {found} instead of {expected}").format(
                found=f"{message[messages.COMMANDE]:02X}", expected=f"{messages.CMD_CADRE:02X}"
            )
        )
    corps = message[messages.CORPS : -1]
    if corps[messages.FANION] != messages.FANION_FIN:
        raise ErreurReponse(_("footer without flag"))
    if corps != messages.CORPS_PIED:
        raise ErreurReponse(_("footer: stray byte after the flag"))


def assembler_lecture(recus: Sequence[bytes]) -> bytes:
    """Les 87 messages d'une lecture d'IR vers les 8192 octets, forme et sommes vérifiées.

    Chaque message doit avoir exactement la forme observée sur la pédale. Le dernier bloc
    porte 40 octets de queue non nuls (reste de tampon de la pédale) : ils entrent dans la
    somme de fin, puis sont écartés.
    """
    if len(recus) != NB_MESSAGES_LECTURE:
        raise ErreurReponse(
            _("{count} messages received instead of {expected}").format(
                count=len(recus), expected=NB_MESSAGES_LECTURE
            )
        )
    for numero, message in enumerate(recus):
        refus = _refus_transfert(message)
        if refus is not None:
            raise ErreurReponse(
                _("message {number} foreign to an IR read: {error}").format(
                    number=numero, error=refus
                )
            )
    _exiger_entete_de_lecture(recus[0])
    blocs = b"".join(_bloc_de_lecture(k, message) for k, message in enumerate(recus[1:-2]))
    _exiger_bloc_de_fin_de_lecture(recus[-2], blocs)
    _exiger_pied_de_lecture(recus[-1])
    return blocs[: codec.TAILLE_IR]
