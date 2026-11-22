"""Description du protocole partagée par l'émission et la réception.

Encadrement, positions dans un message et dans un corps, octets de type et de commande, gabarits
de corps et règle de la zone de nom communs aux deux sens. Aucune logique d'émission
(messages_emission) ni de réception (messages_reception) : ces deux modules importent
celui-ci, jamais l'un l'autre.
"""

from enum import Enum

from optima_air import codec
from optima_air.erreurs import ErreurBornes

ENTETE = bytes([0xF0, 0x00, 0x11, 0x22, 0x61, 0x00])
FIN = 0xF7
NB_SLOTS = 23
FANION_FIN = b"\x7f\x7f\x7f"
LONGUEUR_NOM = 32

# Positions dans un message : ENTETE, type, commande (messages de type 70), corps, F7.
TYPE = 6
COMMANDE = 7
CORPS = 8

# Octet de type, en [6].
TYPE_NUX = 0x70  # tous les messages autres que la demande et la réponse de version

# Octet de commande, en [7], des messages de type 70.
CMD_CADRE = 0x50  # transfert d'IR : en-tête et pied
CMD_BLOC = 0x58  # transfert d'IR : blocs de données et bloc de fin
CMD_NOM = 0x5B  # écriture du nom et de l'identifiant d'un slot, et son écho
CMD_INFOS = 0x5C  # lecture des infos d'un slot, et sa réponse

# Trames de 54 octets (59, 5B, 5C) : ENTETE, 70, commande, longueur encodée (44), puis 44
# octets de 7 bits portant 38 octets source (enc7), puis F7.
LONGUEUR_ENCODEE_54 = 44
ZONE_54 = slice(CORPS + 1, CORPS + 1 + LONGUEUR_ENCODEE_54)
LONGUEUR_TRAME_54 = ZONE_54.stop + 1
# Dans les 38 octets source : slot (2 octets, petit-boutiste), identifiant (4), nom (32).
INFO_SLOT = slice(0, 2)
INFO_IDENTIFIANT = slice(2, 6)
INFO_NOM = slice(6, 6 + LONGUEUR_NOM)

# Trames de 128 octets (50, 58) : ENTETE, 70, commande, corps de 119 octets, F7.
TAILLE_CORPS = 119
LONGUEUR_TRAME_128 = CORPS + TAILLE_CORPS + 1
# Positions dans le corps de 119 octets.
FANION = slice(0, len(FANION_FIN))  # bloc de fin et pied : 7F 7F 7F
DRAPEAU = 0  # bloc de données : toujours 0
ADRESSE = slice(1, 3)  # bloc de données : k × 98 sur 2 octets de 7 bits, poids fort d'abord
_DEBUT_ZONE = 3  # bloc de données : début des octets produits par enc_msb
_FIN_ZONE = _DEBUT_ZONE + codec.TAILLE_BLOC_ENCODE  # 118, exclu
ZONE_ENCODEE = slice(_DEBUT_ZONE, _FIN_ZONE)
SOMME_FIN = slice(3, 7)  # bloc de fin : somme pondérée, 4 octets de 7 bits (enc28)
SLOT_FIN = 17  # bloc de fin d'upload : slot
COMPTEUR = _FIN_ZONE  # bloc de données : k ; bloc de fin : 84

LONGUEUR_VERSION = 9
LONGUEUR_TABLEAU = CORPS + NB_SLOTS + 1


class Tableau(Enum):
    """Commande de LECTURE de chaque tableau de réglages par slot."""

    VOLUME = 0x5E
    LOW_CUT = 0x60
    HIGH_CUT = 0x62

    @property
    def commande_ecriture(self) -> int:
        """Commande d'ÉCRITURE du même tableau (5D, 5F, 61), que la pédale renvoie en écho."""
        return self.value - 1


# Forme de chaque corps de 119 octets, décrite une seule fois : le constructeur d'upload
# l'emploie pour émettre, le contrôle d'émission et l'assembleur de lecture pour comparer.
# Les gabarits propres à un seul sens sont dans son module : en-tête et bloc de données
# d'upload dans messages_emission, en-tête de lecture dans messages_reception.
# Chaque constructeur contrôle la longueur de ses champs variables : affectée à une tranche
# de bytearray, une valeur de mauvaise longueur décalerait les octets suivants sans erreur.


def adresse_bloc(k: int) -> bytes:
    """Adresse du bloc k, k × 98, sur 2 octets de 7 bits, poids fort d'abord."""
    adresse = k * codec.TAILLE_BLOC
    return bytes([adresse // 128, adresse % 128])


def corps_bloc_de_fin(somme: bytes, slot: int) -> bytes:
    """somme : les 4 octets de 7 bits de la somme pondérée (enc28)."""
    taille = SOMME_FIN.stop - SOMME_FIN.start
    if len(somme) != taille:
        raise ErreurBornes(f"somme de fin de {len(somme)} octets au lieu de {taille}")
    corps = bytearray(TAILLE_CORPS)
    corps[FANION] = FANION_FIN
    corps[SOMME_FIN] = somme
    corps[SLOT_FIN] = slot
    corps[COMPTEUR] = codec.NB_BLOCS
    return bytes(corps)


# Le pied est le même à l'upload et en lecture : le fanion, puis des zéros.
CORPS_PIED = FANION_FIN + bytes(TAILLE_CORPS - len(FANION_FIN))


def imprimable(octet: int) -> bool:
    return 0x20 <= octet <= 0x7E


def refus_zone_nom(zone: bytes) -> str | None:
    """Zone de nom de 32 octets : 1 à 32 caractères ASCII imprimables, puis des zéros."""
    nom, _, suite = zone.partition(b"\x00")
    if not nom or not all(imprimable(octet) for octet in nom):
        return "nom de slot vide ou hors ASCII imprimable"
    if any(suite):
        return "octets non nuls après la fin du nom de slot"
    return None
