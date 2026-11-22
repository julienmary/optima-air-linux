"""Fonctions pures d'encodage du protocole NUX NAI-5. Aucune entrée-sortie."""

import hashlib

from optima_air.erreurs import ErreurCodec

TAILLE_IR = 8192
TAILLE_BLOC = 98
NB_BLOCS = 84
GROUPE = 7
# Un octet de poids forts par groupe de 7 octets de données (98 = 14 × 7).
NB_POIDS_FORTS = TAILLE_BLOC // GROUPE
# Somme du bloc : 7 bits hauts, 7 bits bas, puis un octet toujours nul.
TAILLE_SOMME = 3
TAILLE_BLOC_ENCODE = NB_POIDS_FORTS + TAILLE_BLOC + TAILLE_SOMME
# La somme des 98 octets peut atteindre 98 × 255 = 24990 (15 bits), mais le format n'en
# transmet que 14. Aucune donnée réelle n'a dépassé 15177 : on ignore ce que fait la pédale
# au-delà (troncature ? rejet ?). Refusé à l'encodage tant que ce n'est pas vérifié.
SOMME_BLOC_MAX = 0x3FFF

# CRC maison de l'identifiant de slot, relevé sur l'appli officielle.
CRC_POLYNOME = 0x09823B6E
CRC_VALEUR_INITIALE = 0xFFFFFFFE
# L'appli officielle ne hache que les 2048 premiers OCTETS de l'IR (et non les 2048
# échantillons). Bogue reproduit volontairement, pour un en-tête identique à l'officiel.
MD5_NB_OCTETS = 2048


def enc7(source: bytes) -> bytes:
    """Flux de bits, poids faible d'abord, découpé en octets de 7 bits."""
    accumulateur = 0
    nb_bits = 0
    sortie = bytearray()
    for octet in source:
        accumulateur |= octet << nb_bits
        nb_bits += 8
        while nb_bits >= 7:
            sortie.append(accumulateur & 0x7F)
            accumulateur >>= 7
            nb_bits -= 7
    if nb_bits:
        sortie.append(accumulateur & 0x7F)
    return bytes(sortie)


def dec7(encode: bytes) -> bytes:
    """Inverse exact de enc7 : refuse toute entrée qu'enc7 ne peut pas produire.

    n octets de 7 bits portent 7n bits, soit n * 7 // 8 octets plus un reste de bourrage.
    enc7 ne laisse jamais plus de 6 bits de bourrage, et toujours à zéro. Un octet de
    bourrage entier, ou un bit de bourrage à 1, trahit une trame corrompue ou mal découpée :
    refusé plutôt que tronqué en silence. Toute entrée acceptée vérifie enc7(dec7(x)) == x.
    """
    accumulateur = 0
    nb_bits = 0
    sortie = bytearray()
    for octet in encode:
        if octet > 0x7F:
            raise ErreurCodec(f"octet {octet:#04x} hors des 7 bits")
        accumulateur |= octet << nb_bits
        nb_bits += 7
        while nb_bits >= 8:
            sortie.append(accumulateur & 0xFF)
            accumulateur >>= 8
            nb_bits -= 8
    if nb_bits >= 7:
        raise ErreurCodec(
            f"{len(encode)} octets de 7 bits laissent {nb_bits} bits de bourrage, "
            "enc7 n'en produit jamais plus de 6"
        )
    if accumulateur:
        raise ErreurCodec("bits de bourrage non nuls en fin de flux de 7 bits")
    return bytes(sortie)


def _somme_encodee(somme: int) -> bytes:
    """Les 14 bits bas de la somme d'un bloc, sur 2 octets de 7 bits, puis un octet nul."""
    return bytes([(somme >> 7) & 0x7F, somme & 0x7F, 0])


def enc_msb(brut: bytes) -> bytes:
    """98 octets bruts vers 115 : 14 octets de poids forts, 98 données, 3 de somme."""
    if len(brut) != TAILLE_BLOC:
        raise ErreurCodec(f"un bloc fait {TAILLE_BLOC} octets, reçu {len(brut)}")
    somme = sum(brut)
    if somme > SOMME_BLOC_MAX:
        raise ErreurCodec(
            f"somme de bloc {somme} ({somme:#06x}) au-delà des 14 bits transmis "
            f"(max {SOMME_BLOC_MAX:#06x}) : cas jamais observé sur la pédale, "
            "refusé tant qu'il n'est pas vérifié"
        )
    poids_forts = bytearray(NB_POIDS_FORTS)
    donnees = bytearray(TAILLE_BLOC)
    for i, octet in enumerate(brut):
        donnees[i] = octet & 0x7F
        if octet & 0x80:
            poids_forts[i // GROUPE] |= 1 << (GROUPE - 1 - i % GROUPE)
    return bytes(poids_forts) + bytes(donnees) + _somme_encodee(somme)


def dec_msb(encode: bytes) -> bytes:
    """Inverse de enc_msb. Vérifie la somme du bloc."""
    if len(encode) != TAILLE_BLOC_ENCODE:
        raise ErreurCodec(f"un bloc encodé fait {TAILLE_BLOC_ENCODE} octets, reçu {len(encode)}")
    if any(octet > 0x7F for octet in encode):
        raise ErreurCodec("octet hors des 7 bits dans un bloc encodé")
    poids_forts = encode[:NB_POIDS_FORTS]
    brut = bytearray(encode[NB_POIDS_FORTS : NB_POIDS_FORTS + TAILLE_BLOC])
    for i in range(TAILLE_BLOC):
        if poids_forts[i // GROUPE] & (1 << (GROUPE - 1 - i % GROUPE)):
            brut[i] |= 0x80
    if encode[NB_POIDS_FORTS + TAILLE_BLOC :] != _somme_encodee(sum(brut)):
        raise ErreurCodec("somme de contrôle du bloc incorrecte")
    return bytes(brut)


def somme_ponderee(blocs: bytes) -> int:
    """Somme de fin de transfert, sur un ou plusieurs blocs complets de 98 octets. 28 bits."""
    if not blocs:
        raise ErreurCodec("la somme de fin de transfert porte sur au moins un bloc")
    if len(blocs) % TAILLE_BLOC:
        raise ErreurCodec(f"longueur {len(blocs)} non multiple de {TAILLE_BLOC}")
    total = 0
    for debut in range(0, len(blocs), TAILLE_BLOC):
        cumul = 0
        for k in range(0, TAILLE_BLOC, GROUPE):
            cumul += sum(blocs[debut + k : debut + k + GROUPE])
            # Sans effet ici (cumul ≤ 98 × 255 = 24990 < 0x10000), mais c'est le calcul
            # de la pédale sur 16 bits : on le garde pour rester fidèle au relevé.
            total += cumul & 0xFFFF
    return total & 0x0FFFFFFF


def enc28(valeur: int) -> bytes:
    """Entier de 28 bits sur 4 octets de 7 bits, poids fort d'abord."""
    if not 0 <= valeur <= 0x0FFFFFFF:
        raise ErreurCodec(f"valeur {valeur} hors des 28 bits")
    return bytes(
        [(valeur >> 21) & 0x7F, (valeur >> 14) & 0x7F, (valeur >> 7) & 0x7F, valeur & 0x7F]
    )


def dec28(octets: bytes) -> int:
    """Inverse de enc28 : 4 octets de 7 bits, poids fort d'abord, vers un entier de 28 bits."""
    if len(octets) != 4:
        raise ErreurCodec(f"une somme de 28 bits tient sur 4 octets, reçu {len(octets)}")
    for octet in octets:
        if octet > 0x7F:
            raise ErreurCodec(f"octet {octet:#04x} hors des 7 bits dans une somme de 28 bits")
    return (octets[0] << 21) | (octets[1] << 14) | (octets[2] << 7) | octets[3]


def _table_crc() -> tuple[int, ...]:
    table = []
    for i in range(256):
        registre = i << 24
        for _ in range(8):
            if registre & 0x80000000:
                registre = ((registre << 1) ^ CRC_POLYNOME) & 0xFFFFFFFF
            else:
                registre = (registre << 1) & 0xFFFFFFFF
        table.append(registre)
    return tuple(table)


_TABLE_CRC = _table_crc()


def identifiant(ir: bytes) -> int:
    """Identifiant de slot posé par l'appli : CRC maison sur les 8192 octets, bit 31 à zéro."""
    if len(ir) != TAILLE_IR:
        raise ErreurCodec(f"une IR fait {TAILLE_IR} octets, reçu {len(ir)}")
    registre = CRC_VALEUR_INITIALE
    for octet in ir:
        registre = ((registre << 8) & 0xFFFFFFFF) ^ _TABLE_CRC[(registre >> 24) ^ octet]
    return ((~registre) & 0xFFFFFFFF) >> 1


def md5_entete(ir: bytes) -> bytes:
    """MD5 en hexadécimal ASCII des 2048 premiers OCTETS d'une IR complète.

    Bogue de l'appli officielle, reproduit volontairement (voir MD5_NB_OCTETS).
    """
    if len(ir) != TAILLE_IR:
        raise ErreurCodec(f"une IR fait {TAILLE_IR} octets, reçu {len(ir)}")
    return hashlib.md5(ir[:MD5_NB_OCTETS]).hexdigest().encode("ascii")
