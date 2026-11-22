"""Liste fermée des trames émises, leurs constructeurs et le contrôle des bornes avant écriture.

Toute erreur de bornes lève ErreurBornes. La description du protocole partagée avec la
réception est dans messages.
"""

import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from optima_air import codec, messages
from optima_air.erreurs import ErreurBornes, ErreurCodec
from optima_air.i18n import _

# Identifiant de slot : le bit 31 marque les IR d'usine. Leur écriture n'est pas encore
# ouverte (fonction à l'essai) : seuls les identifiants utilisateur, bit 31 à zéro, passent.
_IDENTIFIANT_MAX = 0x7FFF_FFFF
_HEXA_MINUSCULE = frozenset(b"0123456789abcdef")

# Octet de type, en [6], propre à l'émission.
_TYPE_VERSION = 0x00  # demande de version, 9 octets

# Octet de commande, en [7], des messages de type 70 propres à l'émission.
_CMD_ANNONCE = 0x05
_CMD_LIRE_ETAT = 0x26
_CMD_LIRE_IR = 0x59

# Trames de 54 octets : nombre d'octets source portés par la zone encodée.
_LONGUEUR_SOURCE_54 = 38

# Positions dans le corps de 119 octets de l'en-tête d'upload.
_ENTETE_SLOT = 3  # en-tête d'upload : slot, répété en 8
_ENTETE_UN = 5  # en-tête d'upload : toujours 1 chez l'appli officielle (rôle inconnu)
_ENTETE_SLOT_BIS = 8
_MD5 = slice(65, 97)  # en-tête d'upload : MD5 en hexadécimal ASCII minuscule

_CORPS_ANNONCE = bytes([0x01, 0x00, 0x00, 0x00, 0x00])
_CORPS_LIRE_ETAT = b"\x7f"

# Pauses minimales après chaque trame de l'upload, relevées sur la capture USB de l'appli
# officielle : 12 ms après l'en-tête, 1 ms après chaque bloc (bloc de fin compris), 125 ms
# après le pied (avant 5B), 65 ms après le nom (avant 5C).
_PAUSE_APRES_ENTETE_S = 0.012
_PAUSE_APRES_BLOC_S = 0.001
_PAUSE_APRES_PIED_S = 0.125
_PAUSE_APRES_NOM_S = 0.065


class Categorie(Enum):
    LECTURE = "lecture"
    ANNONCE = "annonce"
    REGLAGE = "réglage"
    ECRITURE = "écriture"


_COMMANDES_TABLEAU = frozenset(tableau.value for tableau in messages.Tableau)
_COMMANDES_ECRITURE_TABLEAU = frozenset(tableau.commande_ecriture for tableau in messages.Tableau)
REGLAGE_SLOT_MAX = 100
_COMMANDES_UPLOAD = frozenset({messages.CMD_CADRE, messages.CMD_BLOC, messages.CMD_NOM})


@dataclass(frozen=True)
class Trame:
    octets: bytes
    categorie: Categorie
    nom: str
    essai: str | None = None


# (type, commande) -> longueur totale. Seules ces formes peuvent être émises.
_FORMES: dict[tuple[int, int | None], int] = {
    (_TYPE_VERSION, None): messages.LONGUEUR_VERSION,
    (messages.TYPE_NUX, _CMD_ANNONCE): messages.CORPS + len(_CORPS_ANNONCE) + 1,
    (messages.TYPE_NUX, _CMD_LIRE_ETAT): messages.CORPS + len(_CORPS_LIRE_ETAT) + 1,
    (messages.TYPE_NUX, messages.CMD_CADRE): messages.LONGUEUR_TRAME_128,
    (messages.TYPE_NUX, messages.CMD_BLOC): messages.LONGUEUR_TRAME_128,
    (messages.TYPE_NUX, _CMD_LIRE_IR): messages.LONGUEUR_TRAME_54,
    (messages.TYPE_NUX, messages.CMD_NOM): messages.LONGUEUR_TRAME_54,
    (messages.TYPE_NUX, messages.CMD_INFOS): messages.LONGUEUR_TRAME_54,
    (messages.TYPE_NUX, messages.Tableau.VOLUME.value): messages.LONGUEUR_TABLEAU,
    (messages.TYPE_NUX, messages.Tableau.LOW_CUT.value): messages.LONGUEUR_TABLEAU,
    (messages.TYPE_NUX, messages.Tableau.HIGH_CUT.value): messages.LONGUEUR_TABLEAU,
    **{
        (messages.TYPE_NUX, commande): messages.LONGUEUR_TABLEAU
        for commande in _COMMANDES_ECRITURE_TABLEAU
    },
}


# Gabarits de corps de 119 octets propres à l'émission (les communs sont dans messages).
# Chaque constructeur contrôle la longueur de ses champs variables : affectée à une tranche
# de bytearray, une valeur de mauvaise longueur décalerait les octets suivants sans erreur.


def _corps_entete_upload(slot: int, md5: bytes) -> bytes:
    if len(md5) != _MD5.stop - _MD5.start:
        raise ErreurBornes(
            _("MD5 of {size} bytes instead of {expected}").format(
                size=len(md5), expected=_MD5.stop - _MD5.start
            )
        )
    corps = bytearray(messages.TAILLE_CORPS)
    corps[_ENTETE_SLOT] = slot
    corps[_ENTETE_UN] = 1
    corps[_ENTETE_SLOT_BIS] = slot
    corps[_MD5] = md5
    return bytes(corps)


def _corps_bloc(k: int, encode: bytes) -> bytes:
    if len(encode) != codec.TAILLE_BLOC_ENCODE:
        raise ErreurBornes(
            _("encoded block of {size} bytes instead of {expected}").format(
                size=len(encode), expected=codec.TAILLE_BLOC_ENCODE
            )
        )
    corps = bytearray(messages.TAILLE_CORPS)
    corps[messages.DRAPEAU] = 0
    corps[messages.ADRESSE] = messages.adresse_bloc(k)
    corps[messages.ZONE_ENCODEE] = encode
    corps[messages.COMPTEUR] = k
    return bytes(corps)


# Control Change permis, canal 1 : numéro -> valeur maximale. La liste ne s'allonge qu'après
# un essai concluant sur la pédale.
STATUT_CONTROL_CHANGE = 0xB0
CC_SLOT_ACTIF = 71  # vérifié sur la pédale : elle change de slot


@dataclass(frozen=True)
class _Reglage:
    numero: int  # numéro de Control Change, lu dans l'exe
    maximum: int  # valeur maximale côté utilisateur
    sur_127: bool  # vrai : valeur 0..100 convertie par v × 127 // 100 ; faux : valeur brute


# Réglages par Control Change, désignés par le nom du champ correspondant de l'état.
REGLAGES: dict[str, _Reglage] = {
    "gain": _Reglage(79, 100, True),
    "bass": _Reglage(13, 100, True),
    "middle": _Reglage(14, 100, True),
    "treble": _Reglage(15, 100, True),
    "ir_level": _Reglage(23, 100, True),
    "reverb": _Reglage(87, 100, True),
    "usb_in": _Reglage(7, 100, True),
    "usb_out": _Reglage(47, 100, True),
    "routing": _Reglage(11, 2, False),
    "ir_active": _Reglage(101, 1, False),
    "preamp_actif": _Reglage(102, 1, False),
}
_REGLAGES_CC: dict[int, int] = {
    CC_SLOT_ACTIF: messages.NB_SLOTS - 1,
    **{r.numero: 127 if r.sur_127 else r.maximum for r in REGLAGES.values()},
}
_LONGUEUR_CONTROL_CHANGE = 3


def _controler_control_change(trame: Trame) -> None:
    octets = trame.octets
    if len(octets) != _LONGUEUR_CONTROL_CHANGE or any(octet > 0x7F for octet in octets[1:]):
        raise ErreurBornes(
            _("message “{message}”: malformed Control Change").format(message=trame.nom)
        )
    if trame.categorie is not Categorie.REGLAGE or trame.essai is not None:
        raise ErreurBornes(
            _("message “{message}”: a Control Change is a setting").format(message=trame.nom)
        )
    maximum = _REGLAGES_CC.get(octets[1])
    if maximum is None:
        raise ErreurBornes(
            _("message “{message}”: Control Change not in the closed list").format(
                message=trame.nom
            )
        )
    if octets[2] > maximum:
        raise ErreurBornes(
            _("message “{message}”: value {value} above {max}").format(
                message=trame.nom, value=octets[2], max=maximum
            )
        )


def controler_trame(trame: Trame) -> None:
    """Second contrôle, indépendant des constructeurs, fait juste avant l'écriture sur le port."""
    octets = trame.octets
    if octets[:1] == bytes([STATUT_CONTROL_CHANGE]):
        _controler_control_change(trame)
        return
    if (
        len(octets) < messages.LONGUEUR_VERSION
        or octets[: len(messages.ENTETE)] != messages.ENTETE
        or octets[-1] != messages.FIN
    ):
        raise ErreurBornes(_("message “{message}” badly framed").format(message=trame.nom))
    if any(octet > 0x7F for octet in octets[1:-1]):
        raise ErreurBornes(_("message “{message}”: byte outside 7 bits").format(message=trame.nom))
    if octets[messages.TYPE] == _TYPE_VERSION:
        cle: tuple[int, int | None] = (_TYPE_VERSION, None)
    else:
        cle = (octets[messages.TYPE], octets[messages.COMMANDE])
    attendu = _FORMES.get(cle)
    if attendu is None:
        raise ErreurBornes(
            _("message “{message}” not in the closed list").format(message=trame.nom)
        )
    if len(octets) != attendu:
        raise ErreurBornes(
            _("message “{message}”: {size} bytes instead of {expected}").format(
                message=trame.nom, size=len(octets), expected=attendu
            )
        )
    refus = _refus_du_contenu(trame)
    if refus is not None:
        raise ErreurBornes(_("message “{message}”: {error}").format(message=trame.nom, error=refus))


def _corps54(octets: bytes) -> bytes | None:
    """Les 38 octets d'une trame de 54, ou None si elle n'a pas la forme attendue."""
    if octets[messages.CORPS] != messages.LONGUEUR_ENCODEE_54:
        return None
    try:
        # strict : refuse des bits de bourrage non nuls
        return codec.dec7(octets[messages.ZONE_54])
    except ErreurCodec:
        return None


def _refus_du_contenu(trame: Trame) -> str | None:
    """Bornes et catégorie redérivées des octets seuls. Rend la raison du refus, ou None.

    Appelée après le contrôle de forme : la clé (type, commande) est dans _FORMES et la
    longueur est exacte.
    """
    octets = trame.octets
    corps = octets[messages.CORPS : -1]
    lecture = trame.categorie is Categorie.LECTURE
    ecriture = trame.categorie is Categorie.ECRITURE
    if octets[messages.TYPE] == _TYPE_VERSION:
        # La demande de version n'a pas de commande : son second octet est toujours nul.
        return (
            None
            if lecture and octets[messages.TYPE + 1] == 0x00
            else _("unexpected version request")
        )
    commande = octets[messages.COMMANDE]
    if commande == _CMD_ANNONCE:
        conforme = corps == _CORPS_ANNONCE
        if conforme and trame.categorie is Categorie.ANNONCE and trame.essai == "annonce":
            return None
        return _("only the announcement 70 05 01, on trial, is allowed")
    if commande == _CMD_LIRE_ETAT:
        if lecture and corps == _CORPS_LIRE_ETAT:
            return None
        return _("only the read 70 26 7F is allowed")
    if commande in _COMMANDES_TABLEAU:
        return None if lecture and not any(corps) else _("table request with a non-zero body")
    if commande in _COMMANDES_ECRITURE_TABLEAU:
        if trame.categorie is Categorie.REGLAGE and max(corps) <= REGLAGE_SLOT_MAX:
            return None
        return _("table write: setting expected, values from 0 to 100")
    if commande in (_CMD_LIRE_IR, messages.CMD_INFOS):
        source = _corps54(octets)
        if source is None or not lecture:
            return _("malformed read request")
        slot = int.from_bytes(source[messages.INFO_SLOT], "little")
        if slot >= messages.NB_SLOTS or any(source[messages.INFO_SLOT.stop :]):
            return _("slot out of range or non-zero body")
        return None
    if commande not in _COMMANDES_UPLOAD:
        # Garde inatteignable : chaque commande de _FORMES a sa règle ci-dessus. Une forme
        # ajoutée à _FORMES sans règle de contenu est refusée ici par défaut.
        return _("command {command} without a content rule").format(command=f"{commande:02X}")
    if not ecriture:
        return _("upload message outside the write category")
    if commande == messages.CMD_NOM:
        return _refus_nom(octets)
    if commande == messages.CMD_CADRE:
        if corps[messages.FANION] == messages.FANION_FIN:
            return _refus_pied(corps)
        return _refus_entete(corps)
    if commande == messages.CMD_BLOC:
        if corps[messages.FANION] == messages.FANION_FIN:
            return _refus_bloc_de_fin(corps)
        return _refus_bloc_de_donnees(corps)
    # Garde inatteignable : chaque commande de _COMMANDES_UPLOAD est traitée ci-dessus. Une
    # commande qu'on y ajouterait sans règle de contenu est refusée ici par défaut.
    return _("write command {command} without a content rule").format(command=f"{commande:02X}")


# Les contrôles d'écriture qui suivent sont EXACTS : chaque octet est soit imposé, soit pris
# dans l'ensemble des valeurs que construire_upload peut produire (et que l'appli officielle
# émet). Seuls restent libres le MD5, les données de l'IR et la somme de fin, que l'on ne peut
# pas recouper sans l'IR entière.


def _refus_nom(octets: bytes) -> str | None:
    source = _corps54(octets)
    if source is None or int.from_bytes(source[messages.INFO_SLOT], "little") >= messages.NB_SLOTS:
        return _("malformed slot name")
    if int.from_bytes(source[messages.INFO_IDENTIFIANT], "little") > _IDENTIFIANT_MAX:
        return _("identifier with bit 31 set (factory IR): function not yet open")
    return messages.refus_zone_nom(source[messages.INFO_NOM])


def _refus_entete(corps: bytes) -> str | None:
    slot = corps[_ENTETE_SLOT]
    if slot >= messages.NB_SLOTS:
        return _("upload header: slot out of range")
    if any(octet not in _HEXA_MINUSCULE for octet in corps[_MD5]):
        return _("upload header: MD5 outside lowercase hexadecimal digits")
    if corps != _corps_entete_upload(slot, corps[_MD5]):
        return _("upload header outside the observed form")
    return None


def _refus_pied(corps: bytes) -> str | None:
    if corps != messages.CORPS_PIED:
        return _("upload footer: non-zero byte after the flag")
    return None


def _refus_bloc_de_fin(corps: bytes) -> str | None:
    if corps[messages.SLOT_FIN] >= messages.NB_SLOTS:
        return _("end block: slot out of range")
    # La somme pondérée (4 octets de 7 bits, déjà contrôlés) est reprise telle quelle.
    if corps != messages.corps_bloc_de_fin(corps[messages.SOMME_FIN], corps[messages.SLOT_FIN]):
        return _("end block outside the observed form")
    return None


def _refus_bloc_de_donnees(corps: bytes) -> str | None:
    # Les octets sont de 7 bits (contrôlé avant) : l'adresse k × 98 a une seule écriture sur
    # 2 octets, et la comparaison au corps reconstruit pour le compteur k impose à la fois le
    # drapeau nul, l'adresse et le compteur.
    k = corps[messages.COMPTEUR]
    if k >= codec.NB_BLOCS or corps != _corps_bloc(k, corps[messages.ZONE_ENCODEE]):
        return _("data block: address or counter out of range")
    encode = corps[messages.ZONE_ENCODEE]
    try:
        # dec_msb vérifie la somme sur 14 bits ; enc_msb refuse une somme au-delà. L'égalité
        # garantit que la charge utile est exactement ce que le constructeur aurait produit.
        brut = codec.dec_msb(encode)
        exact = codec.enc_msb(brut) == encode
    except ErreurCodec as erreur:
        return _("data block: {error}").format(error=erreur)
    if not exact:
        return _("data block: payload outside the form produced by enc_msb")
    # Le dernier bloc déborde de l'IR : le constructeur le complète par des zéros. L'adresse
    # vaut au plus 83 × 98 = 8134 < 8192 : la borne de coupe n'est jamais négative.
    if any(brut[codec.TAILLE_IR - k * codec.TAILLE_BLOC :]):
        return _("data block: non-zero padding beyond the {size} bytes of the IR").format(
            size=codec.TAILLE_IR
        )
    return None


def _slot(slot: int) -> int:
    if not 0 <= slot < messages.NB_SLOTS:
        raise ErreurBornes(
            _("slot {slot} out of 1..{count}").format(slot=slot + 1, count=messages.NB_SLOTS)
        )
    return slot


def _corps_info(slot: int, identifiant: int, nom: bytes) -> bytes:
    if not 0 <= identifiant <= _IDENTIFIANT_MAX:
        raise ErreurBornes(
            _(
                "identifier {value} out of 0..{max}: bit 31 is reserved for factory IRs, a "
                "function not yet open"
            ).format(value=f"{identifiant:#x}", max=f"{_IDENTIFIANT_MAX:#x}")
        )
    if len(nom) > messages.LONGUEUR_NOM:
        raise ErreurBornes(
            _("name of {size} bytes, {max} at most").format(
                size=len(nom), max=messages.LONGUEUR_NOM
            )
        )
    source = bytearray(_LONGUEUR_SOURCE_54)
    source[messages.INFO_SLOT] = slot.to_bytes(2, "little")
    source[messages.INFO_IDENTIFIANT] = identifiant.to_bytes(4, "little")
    source[messages.INFO_NOM.start : messages.INFO_NOM.start + len(nom)] = nom
    return bytes(source)


def _trame54(commande: int, source: bytes) -> bytes:
    if len(source) != _LONGUEUR_SOURCE_54:
        raise ErreurBornes(
            _("body of {size} bytes instead of {expected}").format(
                size=len(source), expected=_LONGUEUR_SOURCE_54
            )
        )
    entete = messages.ENTETE + bytes([messages.TYPE_NUX, commande, messages.LONGUEUR_ENCODEE_54])
    return entete + codec.enc7(source) + bytes([messages.FIN])


def _trame128(commande: int, corps: bytes) -> bytes:
    if len(corps) != messages.TAILLE_CORPS:
        raise ErreurBornes(
            _("body of {size} bytes instead of {expected}").format(
                size=len(corps), expected=messages.TAILLE_CORPS
            )
        )
    return messages.ENTETE + bytes([messages.TYPE_NUX, commande]) + corps + bytes([messages.FIN])


def demande_version() -> Trame:
    return Trame(
        messages.ENTETE + bytes([_TYPE_VERSION, 0x00, messages.FIN]), Categorie.LECTURE, "version"
    )


def demande_infos_slot(slot: int) -> Trame:
    octets = _trame54(messages.CMD_INFOS, _corps_info(_slot(slot), 0, b""))
    return Trame(octets, Categorie.LECTURE, _("info of slot {slot}").format(slot=slot + 1))


def demande_tableau(tableau: messages.Tableau) -> Trame:
    octets = (
        messages.ENTETE
        + bytes([messages.TYPE_NUX, tableau.value])
        + bytes(messages.NB_SLOTS)
        + bytes([messages.FIN])
    )
    return Trame(octets, Categorie.LECTURE, _("table {table}").format(table=tableau.name))


def demande_etat() -> Trame:
    octets = (
        messages.ENTETE
        + bytes([messages.TYPE_NUX, _CMD_LIRE_ETAT])
        + _CORPS_LIRE_ETAT
        + bytes([messages.FIN])
    )
    return Trame(octets, Categorie.LECTURE, _("current state"))


def demande_ir(slot: int) -> Trame:
    octets = _trame54(_CMD_LIRE_IR, _corps_info(_slot(slot), 0, b""))
    return Trame(octets, Categorie.LECTURE, _("IR of slot {slot}").format(slot=slot + 1))


def activer_slot(slot: int) -> Trame:
    """Rend un slot actif sur la pédale (elle change d'IR, donc de son). Sans accusé."""
    octets = bytes([STATUT_CONTROL_CHANGE, CC_SLOT_ACTIF, _slot(slot)])
    return Trame(octets, Categorie.REGLAGE, _("activation of slot {slot}").format(slot=slot + 1))


def ecrire_tableau(tableau: messages.Tableau, valeurs: Sequence[int]) -> Trame:
    """Écrit les 23 valeurs (0 à 100) d'un tableau de réglages par slot. La pédale renvoie
    la même trame en écho."""
    if len(valeurs) != messages.NB_SLOTS:
        raise ErreurBornes(
            _("a table has {count} values, got {got}").format(
                count=messages.NB_SLOTS, got=len(valeurs)
            )
        )
    for valeur in valeurs:
        if type(valeur) is not int or not 0 <= valeur <= REGLAGE_SLOT_MAX:
            raise ErreurBornes(
                _("table {table}: value {value!r} out of 0..{max}").format(
                    table=tableau.name, value=valeur, max=REGLAGE_SLOT_MAX
                )
            )
    octets = (
        messages.ENTETE
        + bytes([messages.TYPE_NUX, tableau.commande_ecriture])
        + bytes(valeurs)
        + bytes([messages.FIN])
    )
    return Trame(octets, Categorie.REGLAGE, _("write of table {table}").format(table=tableau.name))


def regler(nom: str, valeur: int) -> Trame:
    """Un réglage par Control Change : potards et volumes USB de 0 à 100 (convertis comme
    le fait l'appli officielle, v × 127 // 100), routing de 0 à 2, marche/arrêt 0 ou 1."""
    reglage = REGLAGES.get(nom)
    if reglage is None:
        raise ErreurBornes(_("unknown setting “{name}”").format(name=nom))
    if type(valeur) is not int or not 0 <= valeur <= reglage.maximum:
        raise ErreurBornes(
            _("setting “{name}”: value {value!r} out of 0..{max}").format(
                name=nom, value=valeur, max=reglage.maximum
            )
        )
    emise = valeur * 127 // reglage.maximum if reglage.sur_127 else valeur
    octets = bytes([STATUT_CONTROL_CHANGE, reglage.numero, emise])
    return Trame(
        octets, Categorie.REGLAGE, _("setting {name} to {value}").format(name=nom, value=valeur)
    )


def annonce() -> Trame:
    octets = (
        messages.ENTETE
        + bytes([messages.TYPE_NUX, _CMD_ANNONCE])
        + _CORPS_ANNONCE
        + bytes([messages.FIN])
    )
    return Trame(octets, Categorie.ANNONCE, _("connection announcement"), essai="annonce")


@dataclass(frozen=True)
class EtapeUpload:
    etiquette: str
    trame: Trame
    pause_s: float


def valider_nom_slot(nom: str) -> bytes:
    """Nom de slot : 1 à 32 caractères ASCII imprimables."""
    if not 1 <= len(nom) <= messages.LONGUEUR_NOM:
        raise ErreurBornes(
            _("a slot name has 1 to {max} characters").format(max=messages.LONGUEUR_NOM)
        )
    if any(not messages.imprimable(ord(caractere)) for caractere in nom):
        raise ErreurBornes(_("a slot name only contains printable ASCII characters"))
    return nom.encode("ascii")


def proposer_nom_slot(texte: str) -> str:
    """Un nom de slot acceptable tiré d'un texte quelconque (un nom de fichier) : tout
    caractère hors ASCII imprimable devient « _ », le tout coupé à 32. Jamais vide."""
    propre = "".join(c if 0x20 <= ord(c) <= 0x7E else "_" for c in texte)
    return propre[: messages.LONGUEUR_NOM].strip() or "IR"


def valider_ir(ir: bytes) -> None:
    if len(ir) != codec.TAILLE_IR:
        raise ErreurBornes(
            _("an IR is {expected} bytes, got {got}").format(expected=codec.TAILLE_IR, got=len(ir))
        )
    if not all(math.isfinite(echantillon) for echantillon in struct.unpack("<2048f", ir)):
        raise ErreurBornes(_("the IR contains a non-finite sample"))


def construire_upload(slot: int, ir: bytes, identifiant: int, nom: str) -> list[EtapeUpload]:
    """Les 89 trames d'un upload d'IR, dans l'ordre, avec la pause minimale après chacune."""
    _slot(slot)
    valider_ir(ir)
    nom_ascii = valider_nom_slot(nom)
    source_nom = _corps_info(slot, identifiant, nom_ascii)

    def ecriture(etiquette: str, octets: bytes, pause_s: float) -> EtapeUpload:
        trame = Trame(
            octets,
            Categorie.ECRITURE,
            _("upload of slot {slot}, {step}").format(slot=slot + 1, step=etiquette),
        )
        return EtapeUpload(etiquette, trame, pause_s)

    tete = _corps_entete_upload(slot, codec.md5_entete(ir))
    etapes = [ecriture(_("header"), _trame128(messages.CMD_CADRE, tete), _PAUSE_APRES_ENTETE_S)]

    complet = ir + bytes(codec.NB_BLOCS * codec.TAILLE_BLOC - codec.TAILLE_IR)
    for k in range(codec.NB_BLOCS):
        bloc = complet[k * codec.TAILLE_BLOC : (k + 1) * codec.TAILLE_BLOC]
        corps = _corps_bloc(k, codec.enc_msb(bloc))
        etapes.append(
            ecriture(
                _("block {block}").format(block=k),
                _trame128(messages.CMD_BLOC, corps),
                _PAUSE_APRES_BLOC_S,
            )
        )

    fin = messages.corps_bloc_de_fin(codec.enc28(codec.somme_ponderee(complet)), slot)
    etapes.append(ecriture(_("end"), _trame128(messages.CMD_BLOC, fin), _PAUSE_APRES_BLOC_S))
    etapes.append(
        ecriture(
            _("footer"), _trame128(messages.CMD_CADRE, messages.CORPS_PIED), _PAUSE_APRES_PIED_S
        )
    )
    etapes.append(ecriture(_("name"), _trame54(messages.CMD_NOM, source_nom), _PAUSE_APRES_NOM_S))
    etapes.append(EtapeUpload(_("info"), demande_infos_slot(slot), 0.0))
    return etapes
