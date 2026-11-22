"""Erreurs nommées de optima-air.

Les messages des couches basses (ErreurCodec en particulier) sont techniques : ils décrivent
l'octet ou la somme en cause, pour le diagnostic. Les couches hautes les rattrapent et les
reformulent pour l'utilisateur ; ils ne doivent pas lui parvenir tels quels. Les erreurs
levées par les couches hautes portent, elles, des messages destinés à l'utilisateur.
"""


class ErreurOptimaAir(Exception):
    """Racine de toutes les erreurs de l'outil."""


class PedaleAbsente(ErreurOptimaAir):
    """Aucune carte « NAI5 » n'est branchée."""


class PedaleDebranchee(ErreurOptimaAir):
    """Le port a disparu en cours de session."""


class PortOccupe(ErreurOptimaAir):
    """Un autre processus tient déjà la pédale."""


class AccesRefuse(ErreurOptimaAir):
    """Le système refuse l'accès au port."""


class ErreurBornes(ErreurOptimaAir, ValueError):
    """Une valeur sort des bornes de la liste fermée des trames."""


class ErreurCodec(ErreurOptimaAir, ValueError):
    """Une donnée ne peut pas être encodée ou décodée."""


class ErreurReponse(ErreurOptimaAir):
    """La pédale a répondu autre chose que ce qui était attendu."""


class DelaiDepasse(ErreurOptimaAir):
    """La pédale n'a pas répondu dans le délai."""


class FirmwareInconnu(ErreurOptimaAir):
    """Le firmware n'est pas celui sur lequel le protocole a été vérifié."""


class FonctionALEssai(ErreurOptimaAir):
    """La fonction n'a pas encore été validée sur la pédale."""


class ArchiveAbimee(ErreurOptimaAir):
    """Un fichier du dossier de données est illisible, incohérent ou a été modifié."""


class ErreurDisque(ErreurOptimaAir):
    """Impossible de lire, d'écrire ou de créer dans le dossier de données."""


class EcritureInterrompue(ErreurOptimaAir):
    """Une écriture d'IR n'est pas allée au bout : l'état du slot visé est à constater."""
