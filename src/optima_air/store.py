"""Dossier de données : façade du registre des fonctions à l'essai et des archives de slot.

Ce module ne parle jamais à la pédale. La classe Magasin ne fait que déléguer : le registre
est dans registre, les archives dans archives, les briques de disque communes dans disque.

Erreurs :
- ArchiveAbimee : un fichier relu est illisible, incohérent ou a été modifié. Jamais une
  valeur fausse : tout ce qui est relu est contrôlé, et refusé au moindre doute.
- ErreurDisque : impossible de lire, d'écrire ou de créer dans le dossier de données
  (droits, disque plein, système en lecture seule, chemin qui n'est pas un dossier, verrou
  pris). L'OSError d'origine est attachée (raise ... from).
- ErreurBornes : une valeur à écrire est refusée, avant que le moindre octet soit écrit.

Le format du registre, des archives et des fiches, les modes des fichiers et la règle de
l'heure (`quand`) sont décrits dans les docstrings de registre et d'archives.
"""

import os
from datetime import datetime
from pathlib import Path

from optima_air import archives, registre

# Réexports voulus, et les seuls : la ligne de commande importe ces trois noms depuis
# store. Ils sont définis dans registre et archives.
from optima_air.archives import MOTIFS as MOTIFS
from optima_air.archives import FicheSlot as FicheSlot
from optima_air.registre import FONCTIONS_A_L_ESSAI as FONCTIONS_A_L_ESSAI


def racine_par_defaut() -> Path:
    """$XDG_DATA_HOME/optima-air, ou ~/.local/share/optima-air si la variable est vide,
    absente ou relative (la spécification XDG demande d'ignorer un chemin relatif)."""
    base = os.environ.get("XDG_DATA_HOME", "")
    if not base or not Path(base).is_absolute():
        base = str(Path.home() / ".local" / "share")
    return Path(base) / "optima-air"


class Magasin:
    """Dossier de données. Aucune méthode ne laisse sortir d'exception sans nom, hormis
    KeyboardInterrupt et MemoryError (voir la docstring du module)."""

    def __init__(self, racine: Path) -> None:
        self.racine = racine

    # --- registre des fonctions à l'essai ---

    def fonctions_validees(self) -> frozenset[str]:
        """Fonctions à l'essai validées. Un registre absent n'en valide aucune ; un
        registre illisible, ou un lien symbolique à sa place (même cassé), lève
        ArchiveAbimee : il n'en valide aucune non plus."""
        return registre.lire_registre(self.racine)

    def est_validee(self, nom: str) -> bool:
        registre.verifier_fonction(nom)
        return nom in registre.lire_registre(self.racine)

    def valider(self, nom: str) -> None:
        registre.modifier_registre(self.racine, nom, valider=True)

    def invalider(self, nom: str) -> None:
        registre.modifier_registre(self.racine, nom, valider=False)

    # --- archives de slot ---

    def creer_archive(self, motif: str, quand: datetime) -> Path:
        """Crée un dossier d'archive neuf, suffixé -2, -3… s'il en existe déjà un. Le nom
        porte l'heure qu'exprime `quand`, dans son propre fuseau : passer l'heure locale
        (datetime.now().astimezone()), et le même `quand` à ecrire_slot. `quand` sans
        fuseau : ErreurBornes."""
        return archives.creer_archive(self.racine, motif, quand)

    def ecrire_slot(
        self,
        archive: Path,
        *,
        slot: int,
        nom_brut: bytes,
        identifiant: int,
        volume: int,
        low_cut: int,
        high_cut: int,
        firmware: int | None,
        motif: str,
        quand: datetime,
        donnees: bytes,
    ) -> FicheSlot:
        """Archive un slot : ses données, puis sa fiche. Tout est contrôlé et la fiche
        sérialisée avant que rien ne soit écrit (ErreurBornes) : bornes, motif et `quand`
        identiques à ceux de l'archive, slot pas encore présent dans l'archive. Le
        firmware ne peut manquer (None) que pour le motif « import ». L'identifiant des
        données est recalculé ici."""
        return archives.ecrire_slot(
            archive,
            donnees,
            slot=slot,
            nom_brut=nom_brut,
            identifiant=identifiant,
            volume=volume,
            low_cut=low_cut,
            high_cut=high_cut,
            firmware=firmware,
            motif=motif,
            quand=quand,
        )

    def lister_archives(self) -> list[Path]:
        """Les dossiers d'archive, du plus récent au plus ancien."""
        return archives.lister(self.racine)

    def slots_presents(self, archive: Path) -> tuple[int, ...]:
        """Numéros des slots que l'archive contient (sans rien vérifier de leur contenu)."""
        return archives.slots_presents(archive)

    def lire_slot(self, archive: Path, slot: int) -> tuple[FicheSlot, bytes]:
        """Fiche et données du slot demandé. ArchiveAbimee si quoi que ce soit ne colle
        pas : fiche illisible, retouchée (sceau), d'un autre format, d'une autre archive
        ou d'un autre slot, valeurs hors bornes ; données absentes, de mauvaise taille,
        d'une autre empreinte ou d'un autre identifiant que la fiche ; fiche ou données
        remplacées par un lien symbolique, qui n'est jamais suivi."""
        return archives.lire_slot(archive, slot)
