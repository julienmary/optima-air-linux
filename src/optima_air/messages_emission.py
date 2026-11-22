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
        raise ErreurBornes(f"MD5 de {len(md5)} octets au lieu de {_MD5.stop - _MD5.start}")
    corps = bytearray(messages.TAILLE_CORPS)
    corps[_ENTETE_SLOT] = slot
    corps[_ENTETE_UN] = 1
    corps[_ENTETE_SLOT_BIS] = slot
    corps[_MD5] = md5
    return bytes(corps)


def _corps_bloc(k: int, encode: bytes) -> bytes:
    if len(encode) != codec.TAILLE_BLOC_ENCODE:
        raise ErreurBornes(
            f"bloc encodé de {len(encode)} octets au lieu de {codec.TAILLE_BLOC_ENCODE}"
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
        raise ErreurBornes(f"trame « {trame.nom} » : Control Change mal formé")
    if trame.categorie is not Categorie.REGLAGE or trame.essai is not None:
        raise ErreurBornes(f"trame « {trame.nom} » : un Control Change est un réglage")
    maximum = _REGLAGES_CC.get(octets[1])
    if maximum is None:
        raise ErreurBornes(f"trame « {trame.nom} » : Control Change absent de la liste fermée")
    if octets[2] > maximum:
        raise ErreurBornes(f"trame « {trame.nom} » : valeur {octets[2]} supérieure à {maximum}")


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
        raise ErreurBornes(f"trame « {trame.nom} » mal encadrée")
    if any(octet > 0x7F for octet in octets[1:-1]):
        raise ErreurBornes(f"trame « {trame.nom} » : octet hors des 7 bits")
    if octets[messages.TYPE] == _TYPE_VERSION:
        cle: tuple[int, int | None] = (_TYPE_VERSION, None)
    else:
        cle = (octets[messages.TYPE], octets[messages.COMMANDE])
    attendu = _FORMES.get(cle)
    if attendu is None:
        raise ErreurBornes(f"trame « {trame.nom} » absente de la liste fermée")
    if len(octets) != attendu:
        raise ErreurBornes(f"trame « {trame.nom} » : {len(octets)} octets au lieu de {attendu}")
    refus = _refus_du_contenu(trame)
    if refus is not None:
        raise ErreurBornes(f"trame « {trame.nom} » : {refus}")


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
            else "demande de version inattendue"
        )
    commande = octets[messages.COMMANDE]
    if commande == _CMD_ANNONCE:
        conforme = corps == _CORPS_ANNONCE
        if conforme and trame.categorie is Categorie.ANNONCE and trame.essai == "annonce":
            return None
        return "seule l'annonce 70 05 01, à l'essai, est permise"
    if commande == _CMD_LIRE_ETAT:
        if lecture and corps == _CORPS_LIRE_ETAT:
            return None
        return "seule la lecture 70 26 7F est permise"
    if commande in _COMMANDES_TABLEAU:
        return None if lecture and not any(corps) else "demande de tableau au corps non nul"
    if commande in _COMMANDES_ECRITURE_TABLEAU:
        if trame.categorie is Categorie.REGLAGE and max(corps) <= REGLAGE_SLOT_MAX:
            return None
        return "écriture de tableau : réglage attendu, valeurs de 0 à 100"
    if commande in (_CMD_LIRE_IR, messages.CMD_INFOS):
        source = _corps54(octets)
        if source is None or not lecture:
            return "demande de lecture mal formée"
        slot = int.from_bytes(source[messages.INFO_SLOT], "little")
        if slot >= messages.NB_SLOTS or any(source[messages.INFO_SLOT.stop :]):
            return "slot hors bornes ou corps non nul"
        return None
    if commande not in _COMMANDES_UPLOAD:
        # Garde inatteignable : chaque commande de _FORMES a sa règle ci-dessus. Une forme
        # ajoutée à _FORMES sans règle de contenu est refusée ici par défaut.
        return f"commande {commande:02X} sans règle de contenu"
    if not ecriture:
        return "trame d'upload hors de la catégorie écriture"
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
    return f"commande d'écriture {commande:02X} sans règle de contenu"


# Les contrôles d'écriture qui suivent sont EXACTS : chaque octet est soit imposé, soit pris
# dans l'ensemble des valeurs que construire_upload peut produire (et que l'appli officielle
# émet). Seuls restent libres le MD5, les données de l'IR et la somme de fin, que l'on ne peut
# pas recouper sans l'IR entière.


def _refus_nom(octets: bytes) -> str | None:
    source = _corps54(octets)
    if source is None or int.from_bytes(source[messages.INFO_SLOT], "little") >= messages.NB_SLOTS:
        return "nom de slot mal formé"
    if int.from_bytes(source[messages.INFO_IDENTIFIANT], "little") > _IDENTIFIANT_MAX:
        return "identifiant au bit 31 à 1 (IR d'usine) : fonction pas encore ouverte"
    return messages.refus_zone_nom(source[messages.INFO_NOM])


def _refus_entete(corps: bytes) -> str | None:
    slot = corps[_ENTETE_SLOT]
    if slot >= messages.NB_SLOTS:
        return "en-tête d'upload : slot hors bornes"
    if any(octet not in _HEXA_MINUSCULE for octet in corps[_MD5]):
        return "en-tête d'upload : MD5 hors des chiffres hexadécimaux minuscules"
    if corps != _corps_entete_upload(slot, corps[_MD5]):
        return "en-tête d'upload hors de la forme observée"
    return None


def _refus_pied(corps: bytes) -> str | None:
    if corps != messages.CORPS_PIED:
        return "pied d'upload : octet non nul après le fanion"
    return None


def _refus_bloc_de_fin(corps: bytes) -> str | None:
    if corps[messages.SLOT_FIN] >= messages.NB_SLOTS:
        return "bloc de fin : slot hors bornes"
    # La somme pondérée (4 octets de 7 bits, déjà contrôlés) est reprise telle quelle.
    if corps != messages.corps_bloc_de_fin(corps[messages.SOMME_FIN], corps[messages.SLOT_FIN]):
        return "bloc de fin hors de la forme observée"
    return None


def _refus_bloc_de_donnees(corps: bytes) -> str | None:
    # Les octets sont de 7 bits (contrôlé avant) : l'adresse k × 98 a une seule écriture sur
    # 2 octets, et la comparaison au corps reconstruit pour le compteur k impose à la fois le
    # drapeau nul, l'adresse et le compteur.
    k = corps[messages.COMPTEUR]
    if k >= codec.NB_BLOCS or corps != _corps_bloc(k, corps[messages.ZONE_ENCODEE]):
        return "bloc de données : adresse ou compteur hors bornes"
    encode = corps[messages.ZONE_ENCODEE]
    try:
        # dec_msb vérifie la somme sur 14 bits ; enc_msb refuse une somme au-delà. L'égalité
        # garantit que la charge utile est exactement ce que le constructeur aurait produit.
        brut = codec.dec_msb(encode)
        exact = codec.enc_msb(brut) == encode
    except ErreurCodec as erreur:
        return f"bloc de données : {erreur}"
    if not exact:
        return "bloc de données : charge utile hors de la forme produite par enc_msb"
    # Le dernier bloc déborde de l'IR : le constructeur le complète par des zéros. L'adresse
    # vaut au plus 83 × 98 = 8134 < 8192 : la borne de coupe n'est jamais négative.
    if any(brut[codec.TAILLE_IR - k * codec.TAILLE_BLOC :]):
        return "bloc de données : bourrage non nul au-delà des 8192 octets de l'IR"
    return None


def _slot(slot: int) -> int:
    if not 0 <= slot < messages.NB_SLOTS:
        raise ErreurBornes(f"slot {slot} hors de 0..{messages.NB_SLOTS - 1}")
    return slot


def _corps_info(slot: int, identifiant: int, nom: bytes) -> bytes:
    if not 0 <= identifiant <= _IDENTIFIANT_MAX:
        raise ErreurBornes(
            f"identifiant {identifiant:#x} hors de 0..{_IDENTIFIANT_MAX:#x} : le bit 31 est "
            "réservé aux IR d'usine, fonction pas encore ouverte"
        )
    if len(nom) > messages.LONGUEUR_NOM:
        raise ErreurBornes(f"nom de {len(nom)} octets, {messages.LONGUEUR_NOM} au plus")
    source = bytearray(_LONGUEUR_SOURCE_54)
    source[messages.INFO_SLOT] = slot.to_bytes(2, "little")
    source[messages.INFO_IDENTIFIANT] = identifiant.to_bytes(4, "little")
    source[messages.INFO_NOM.start : messages.INFO_NOM.start + len(nom)] = nom
    return bytes(source)


def _trame54(commande: int, source: bytes) -> bytes:
    if len(source) != _LONGUEUR_SOURCE_54:
        raise ErreurBornes(f"corps de {len(source)} octets au lieu de {_LONGUEUR_SOURCE_54}")
    entete = messages.ENTETE + bytes([messages.TYPE_NUX, commande, messages.LONGUEUR_ENCODEE_54])
    return entete + codec.enc7(source) + bytes([messages.FIN])


def _trame128(commande: int, corps: bytes) -> bytes:
    if len(corps) != messages.TAILLE_CORPS:
        raise ErreurBornes(f"corps de {len(corps)} octets au lieu de {messages.TAILLE_CORPS}")
    return messages.ENTETE + bytes([messages.TYPE_NUX, commande]) + corps + bytes([messages.FIN])


def demande_version() -> Trame:
    return Trame(
        messages.ENTETE + bytes([_TYPE_VERSION, 0x00, messages.FIN]), Categorie.LECTURE, "version"
    )


def demande_infos_slot(slot: int) -> Trame:
    octets = _trame54(messages.CMD_INFOS, _corps_info(_slot(slot), 0, b""))
    return Trame(octets, Categorie.LECTURE, f"infos du slot {slot}")


def demande_tableau(tableau: messages.Tableau) -> Trame:
    octets = (
        messages.ENTETE
        + bytes([messages.TYPE_NUX, tableau.value])
        + bytes(messages.NB_SLOTS)
        + bytes([messages.FIN])
    )
    return Trame(octets, Categorie.LECTURE, f"tableau {tableau.name}")


def demande_etat() -> Trame:
    octets = (
        messages.ENTETE
        + bytes([messages.TYPE_NUX, _CMD_LIRE_ETAT])
        + _CORPS_LIRE_ETAT
        + bytes([messages.FIN])
    )
    return Trame(octets, Categorie.LECTURE, "état courant")


def demande_ir(slot: int) -> Trame:
    octets = _trame54(_CMD_LIRE_IR, _corps_info(_slot(slot), 0, b""))
    return Trame(octets, Categorie.LECTURE, f"IR du slot {slot}")


def activer_slot(slot: int) -> Trame:
    """Rend un slot actif sur la pédale (elle change d'IR, donc de son). Sans accusé."""
    octets = bytes([STATUT_CONTROL_CHANGE, CC_SLOT_ACTIF, _slot(slot)])
    return Trame(octets, Categorie.REGLAGE, f"activation du slot {slot}")


def ecrire_tableau(tableau: messages.Tableau, valeurs: Sequence[int]) -> Trame:
    """Écrit les 23 valeurs (0 à 100) d'un tableau de réglages par slot. La pédale renvoie
    la même trame en écho."""
    if len(valeurs) != messages.NB_SLOTS:
        raise ErreurBornes(f"un tableau compte {messages.NB_SLOTS} valeurs, reçu {len(valeurs)}")
    for valeur in valeurs:
        if type(valeur) is not int or not 0 <= valeur <= REGLAGE_SLOT_MAX:
            raise ErreurBornes(
                f"tableau {tableau.name} : valeur {valeur!r} hors de 0..{REGLAGE_SLOT_MAX}"
            )
    octets = (
        messages.ENTETE
        + bytes([messages.TYPE_NUX, tableau.commande_ecriture])
        + bytes(valeurs)
        + bytes([messages.FIN])
    )
    return Trame(octets, Categorie.REGLAGE, f"écriture du tableau {tableau.name}")


def regler(nom: str, valeur: int) -> Trame:
    """Un réglage par Control Change : potards et volumes USB de 0 à 100 (convertis comme
    le fait l'appli officielle, v × 127 // 100), routing de 0 à 2, marche/arrêt 0 ou 1."""
    reglage = REGLAGES.get(nom)
    if reglage is None:
        raise ErreurBornes(f"réglage « {nom} » inconnu")
    if type(valeur) is not int or not 0 <= valeur <= reglage.maximum:
        raise ErreurBornes(f"réglage « {nom} » : valeur {valeur!r} hors de 0..{reglage.maximum}")
    emise = valeur * 127 // reglage.maximum if reglage.sur_127 else valeur
    octets = bytes([STATUT_CONTROL_CHANGE, reglage.numero, emise])
    return Trame(octets, Categorie.REGLAGE, f"réglage {nom} à {valeur}")


def annonce() -> Trame:
    octets = (
        messages.ENTETE
        + bytes([messages.TYPE_NUX, _CMD_ANNONCE])
        + _CORPS_ANNONCE
        + bytes([messages.FIN])
    )
    return Trame(octets, Categorie.ANNONCE, "annonce de connexion", essai="annonce")


@dataclass(frozen=True)
class EtapeUpload:
    etiquette: str
    trame: Trame
    pause_s: float


def valider_nom_slot(nom: str) -> bytes:
    """Nom de slot : 1 à 32 caractères ASCII imprimables."""
    if not 1 <= len(nom) <= messages.LONGUEUR_NOM:
        raise ErreurBornes(f"un nom de slot fait 1 à {messages.LONGUEUR_NOM} caractères")
    if any(not messages.imprimable(ord(caractere)) for caractere in nom):
        raise ErreurBornes("un nom de slot ne contient que des caractères ASCII imprimables")
    return nom.encode("ascii")


def proposer_nom_slot(texte: str) -> str:
    """Un nom de slot acceptable tiré d'un texte quelconque (un nom de fichier) : tout
    caractère hors ASCII imprimable devient « _ », le tout coupé à 32. Jamais vide."""
    propre = "".join(c if 0x20 <= ord(c) <= 0x7E else "_" for c in texte)
    return propre[: messages.LONGUEUR_NOM].strip() or "IR"


def valider_ir(ir: bytes) -> None:
    if len(ir) != codec.TAILLE_IR:
        raise ErreurBornes(f"une IR fait {codec.TAILLE_IR} octets, reçu {len(ir)}")
    if not all(math.isfinite(echantillon) for echantillon in struct.unpack("<2048f", ir)):
        raise ErreurBornes("l'IR contient un échantillon non fini")


def construire_upload(slot: int, ir: bytes, identifiant: int, nom: str) -> list[EtapeUpload]:
    """Les 89 trames d'un upload d'IR, dans l'ordre, avec la pause minimale après chacune."""
    _slot(slot)
    valider_ir(ir)
    nom_ascii = valider_nom_slot(nom)
    source_nom = _corps_info(slot, identifiant, nom_ascii)

    def ecriture(etiquette: str, octets: bytes, pause_s: float) -> EtapeUpload:
        trame = Trame(octets, Categorie.ECRITURE, f"upload du slot {slot}, {etiquette}")
        return EtapeUpload(etiquette, trame, pause_s)

    tete = _corps_entete_upload(slot, codec.md5_entete(ir))
    etapes = [ecriture("en-tête", _trame128(messages.CMD_CADRE, tete), _PAUSE_APRES_ENTETE_S)]

    complet = ir + bytes(codec.NB_BLOCS * codec.TAILLE_BLOC - codec.TAILLE_IR)
    for k in range(codec.NB_BLOCS):
        bloc = complet[k * codec.TAILLE_BLOC : (k + 1) * codec.TAILLE_BLOC]
        corps = _corps_bloc(k, codec.enc_msb(bloc))
        etapes.append(
            ecriture(f"bloc {k}", _trame128(messages.CMD_BLOC, corps), _PAUSE_APRES_BLOC_S)
        )

    fin = messages.corps_bloc_de_fin(codec.enc28(codec.somme_ponderee(complet)), slot)
    etapes.append(ecriture("fin", _trame128(messages.CMD_BLOC, fin), _PAUSE_APRES_BLOC_S))
    etapes.append(
        ecriture("pied", _trame128(messages.CMD_CADRE, messages.CORPS_PIED), _PAUSE_APRES_PIED_S)
    )
    etapes.append(ecriture("nom", _trame54(messages.CMD_NOM, source_nom), _PAUSE_APRES_NOM_S))
    etapes.append(EtapeUpload("infos", demande_infos_slot(slot), 0.0))
    return etapes
