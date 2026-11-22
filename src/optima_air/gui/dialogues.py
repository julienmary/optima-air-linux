"""Boîtes de dialogue de la fenêtre."""

from collections.abc import Callable, Sequence
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from optima_air import messages_emission as emission  # noqa: E402
from optima_air import preferences  # noqa: E402
from optima_air.erreurs import ErreurBornes, ErreurOptimaAir  # noqa: E402
from optima_air.store import Magasin  # noqa: E402

MOTIFS = {
    "avant-ecriture": "avant une écriture",
    "manuelle": "lecture d'un slot",
    "complete": "sauvegarde complète",
    "import": "import",
    "altere": "slot altéré",
}


class DialogueChargement(Gtk.Window):
    """Confirmation avant d'écrire une IR : nomme le slot et ce qui va être écrasé, montre
    les avertissements, et laisse corriger le nom."""

    def __init__(
        self,
        parent: Gtk.Window,
        slot: int,
        nom_actuel: str,
        fichier: Path,
        avertissements: Sequence[str],
        sur_accord: Callable[[str], None],
    ) -> None:
        super().__init__(title="Charger un WAV", transient_for=parent, modal=True)
        self._sur_accord = sur_accord
        boite = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for marge in ("top", "bottom", "start", "end"):
            getattr(boite, f"set_margin_{marge}")(16)
        question = Gtk.Label(xalign=0, wrap=True)
        question.set_markup(
            f"Écraser le slot <b>{slot + 1}</b> « {_echapper(nom_actuel)} » par "
            f"<b>{_echapper(fichier.name)}</b> ?"
        )
        boite.append(question)
        boite.append(
            Gtk.Label(
                label="Le contenu actuel du slot sera archivé avant l'écriture.",
                xalign=0,
                wrap=True,
            )
        )
        for avertissement in avertissements:
            etiquette = Gtk.Label(label=f"Attention : {avertissement}", xalign=0, wrap=True)
            etiquette.set_max_width_chars(60)
            etiquette.add_css_class("avertissement")
            boite.append(etiquette)
        self._nom = Gtk.Entry(text=emission.proposer_nom_slot(fichier.stem), max_length=32)
        self._nom.set_hexpand(True)
        self._nom.connect("activate", self._valider)
        ligne = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        ligne.append(Gtk.Label(label="Nom du slot"))
        ligne.append(self._nom)
        boite.append(ligne)
        self._erreur = Gtk.Label(xalign=0, wrap=True)
        self._erreur.add_css_class("avertissement")
        boite.append(self._erreur)
        annuler = Gtk.Button(label="Annuler")
        annuler.connect("clicked", lambda _b: self.close())
        ecrire = Gtk.Button(label="Écrire dans la pédale")
        ecrire.add_css_class("suggested-action")
        ecrire.connect("clicked", self._valider)
        boutons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        boutons.set_halign(Gtk.Align.END)
        boutons.append(annuler)
        boutons.append(ecrire)
        boite.append(boutons)
        self.set_child(boite)

    def _valider(self, _source: Gtk.Widget) -> None:
        nom = self._nom.get_text()
        try:
            emission.valider_nom_slot(nom)
        except ErreurBornes as erreur:
            self._erreur.set_label(str(erreur))
            return
        self.close()
        self._sur_accord(nom)


def _echapper(texte: str) -> str:
    return texte.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _nom(nom_hex: str) -> str:
    return bytes.fromhex(nom_hex).rstrip(b"\x00").decode("latin-1")


def _date_et_motif(nom_archive: str) -> str:
    """« 20261121-120511-avant-ecriture » devient « 21/11/2026 12:05:11, avant une écriture »."""
    jour, heure, motif = nom_archive.split("-", 2)
    for connu, libelle in MOTIFS.items():
        if motif == connu or motif.startswith(connu + "-"):
            motif = libelle
            break
    return f"{jour[6:8]}/{jour[4:6]}/{jour[0:4]} {heure[0:2]}:{heure[2:4]}:{heure[4:6]}, {motif}"


class FenetreSauvegardes(Gtk.Window):
    """Les archives du dossier de données : à gauche la liste, à droite les slots de celle
    qu'on a choisie, et de quoi restaurer un slot ou l'archive entière."""

    def __init__(
        self,
        parent: Gtk.Window,
        magasin: Magasin,
        sur_restauration: Callable[[Path, tuple[int, ...] | None], None],
    ) -> None:
        super().__init__(title="Sauvegardes", transient_for=parent, modal=True)
        self.set_default_size(760, 520)
        self._magasin = magasin
        self._sur_restauration = sur_restauration
        self._archives: list[Path] = []
        self._slots: tuple[int, ...] = ()
        self._noms: dict[int, str] = {}

        self._liste = Gtk.ListBox()
        self._liste.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._liste.connect("row-selected", self._sur_archive)
        gauche = Gtk.ScrolledWindow(min_content_width=400, vexpand=True)
        gauche.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        gauche.set_child(self._liste)

        self._contenu = Gtk.ListBox()
        self._contenu.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._contenu.connect("row-selected", lambda _l, _r: self._ajuster_boutons())
        droite = Gtk.ScrolledWindow(min_content_width=260, vexpand=True, hexpand=True)
        droite.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        droite.set_child(self._contenu)

        listes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        listes.append(gauche)
        listes.append(droite)

        self._message = Gtk.Label(xalign=0, wrap=True)
        self._un = Gtk.Button(label="Restaurer ce slot")
        self._un.connect("clicked", lambda _b: self._demander(tout=False))
        self._tout = Gtk.Button(label="Restaurer toute l'archive")
        self._tout.connect("clicked", lambda _b: self._demander(tout=True))
        fermer = Gtk.Button(label="Fermer")
        fermer.connect("clicked", lambda _b: self.close())
        boutons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        boutons.set_halign(Gtk.Align.END)
        for bouton in (self._un, self._tout, fermer):
            boutons.append(bouton)

        boite = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for marge in ("top", "bottom", "start", "end"):
            getattr(boite, f"set_margin_{marge}")(14)
        boite.append(listes)
        boite.append(self._message)
        boite.append(boutons)
        self.set_child(boite)
        self._remplir()
        self._ajuster_boutons()

    def _remplir(self) -> None:
        try:
            self._archives = self._magasin.lister_archives()
        except ErreurOptimaAir as erreur:
            self._dire(str(erreur), erreur=True)
            return
        if not self._archives:
            self._dire("Aucune sauvegarde pour l'instant.")
        for archive in self._archives:
            ligne = Gtk.ListBoxRow()
            ligne.set_child(Gtk.Label(label=self._libelle(archive), xalign=0))
            self._liste.append(ligne)

    def _libelle(self, archive: Path) -> str:
        texte = _date_et_motif(archive.name)
        try:
            slots = self._magasin.slots_presents(archive)
            if len(slots) == 1:
                fiche, _ = self._magasin.lire_slot(archive, slots[0])
                return f"{texte} : slot {slots[0] + 1} « {_nom(fiche.nom_hex)} »"
            return f"{texte} : {len(slots)} slots"
        except ErreurOptimaAir:
            return f"{texte} : illisible"

    def _dire(self, texte: str, erreur: bool = False) -> None:
        if erreur:
            self._message.add_css_class("avertissement")
        else:
            self._message.remove_css_class("avertissement")
        self._message.set_label(texte)

    def _archive_choisie(self) -> Path | None:
        ligne = self._liste.get_selected_row()
        return None if ligne is None else self._archives[ligne.get_index()]

    def _slot_choisi(self) -> int | None:
        ligne = self._contenu.get_selected_row()
        return None if ligne is None else self._slots[ligne.get_index()]

    def _sur_archive(self, _liste: Gtk.ListBox, _ligne: Gtk.ListBoxRow | None) -> None:
        self._contenu.remove_all()
        self._slots = ()
        self._noms = {}
        self._dire("")
        archive = self._archive_choisie()
        if archive is not None:
            try:
                # Tout le contenu est relu et contrôlé (sceaux, empreintes) dès maintenant.
                slots = self._magasin.slots_presents(archive)
                for slot in slots:
                    fiche, _ = self._magasin.lire_slot(archive, slot)
                    self._noms[slot] = _nom(fiche.nom_hex)
                self._slots = slots
            except ErreurOptimaAir as erreur:
                self._dire(f"Cette archive n'est pas utilisable : {erreur}", erreur=True)
            for slot in self._slots:
                ligne = Gtk.ListBoxRow()
                ligne.set_child(Gtk.Label(label=f"{slot + 1}. {self._noms[slot]}", xalign=0))
                self._contenu.append(ligne)
            if len(self._slots) == 1:
                self._contenu.select_row(self._contenu.get_row_at_index(0))
        self._ajuster_boutons()

    def _ajuster_boutons(self) -> None:
        self._un.set_sensitive(self._slot_choisi() is not None)
        self._tout.set_sensitive(len(self._slots) > 1)

    def _demander(self, tout: bool) -> None:
        archive = self._archive_choisie()
        slot = self._slot_choisi()
        if archive is None or (not tout and slot is None):
            return
        if tout:
            slots = None
            question = (
                f"Remettre les {len(self._slots)} slots de la pédale dans l'état de cette archive ?"
            )
        else:
            assert slot is not None
            slots = (slot,)
            question = (
                f"Remettre le slot {slot + 1} de la pédale dans l'état de cette archive "
                f"(« {self._noms[slot]} ») ?"
            )
        alerte = Gtk.AlertDialog(
            message=question,
            detail=(
                f"{_date_et_motif(archive.name)}.\n\nSeul un slot qui diffère de l'archive "
                "est réécrit, et son contenu actuel est archivé juste avant."
            ),
            buttons=["Annuler", "Restaurer"],
            cancel_button=0,
            default_button=0,
            modal=True,
        )
        alerte.choose(self, None, self._reponse, (archive, slots))

    def _reponse(self, alerte: Gtk.AlertDialog, resultat, demande) -> None:
        try:
            choix = alerte.choose_finish(resultat)
        except Exception:  # GLib.Error : la boîte a été fermée
            return
        if choix == 1:
            archive, slots = demande
            self.close()
            self._sur_restauration(archive, slots)


PALETTE = (
    ("Bleu électrique", "#2c75ff"),
    ("Cyan", "#00c8ff"),
    ("Vert", "#00e676"),
    ("Jaune", "#ffd400"),
    ("Orange", "#ff8a00"),
    ("Rouge", "#ff3b30"),
    ("Rose", "#ff4fa3"),
    ("Violet", "#a66bff"),
    ("Blanc cassé", "#e8e8e8"),
)


def _pastille(couleur: str, cote: int) -> Gtk.DrawingArea:
    """Un carré plein de la couleur donnée."""
    rouge, vert, bleu = preferences.composantes(couleur)

    def dessiner(_zone: Gtk.DrawingArea, cr, largeur: int, hauteur: int) -> None:
        cr.set_source_rgb(rouge, vert, bleu)
        cr.rectangle(0, 0, largeur, hauteur)
        cr.fill()

    zone = Gtk.DrawingArea(content_width=cote, content_height=cote)
    zone.set_draw_func(dessiner)
    return zone


class BoutonCouleur(Gtk.MenuButton):
    """Choix de la couleur de l'interface : une palette, et un champ pour un code
    « #rrggbb ». Volontairement sans le sélecteur de couleurs de GTK : sa pipette demande
    au bureau de capturer la souris, et le 2026-11-21 cette capture n'a jamais été rendue
    (bureau GNOME sous X11 bloqué jusqu'à la relance du Shell)."""

    def __init__(self, couleur: str, sur_couleur: Callable[[str], None]) -> None:
        super().__init__(tooltip_text="Couleur de l'interface")
        self._sur_couleur = sur_couleur
        self.set_child(_pastille(couleur, 18))
        grille = Gtk.Grid(row_spacing=6, column_spacing=6)
        for rang, (nom, valeur) in enumerate(PALETTE):
            bouton = Gtk.Button(tooltip_text=nom)
            bouton.set_child(_pastille(valeur, 28))
            bouton.connect("clicked", lambda _b, v=valeur: self._choisir(v))
            grille.attach(bouton, rang % 3, rang // 3, 1, 1)
        self._code = Gtk.Entry(text=couleur, max_length=7, width_chars=8)
        self._code.set_tooltip_text("Code de couleur, par exemple #ff8a00")
        self._code.connect("activate", lambda champ: self._choisir(champ.get_text()))
        self._erreur = Gtk.Label(xalign=0)
        self._erreur.add_css_class("avertissement")
        boite = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for marge in ("top", "bottom", "start", "end"):
            getattr(boite, f"set_margin_{marge}")(8)
        boite.append(grille)
        boite.append(self._code)
        boite.append(self._erreur)
        self._bulle = Gtk.Popover()
        self._bulle.set_child(boite)
        self.set_popover(self._bulle)

    def _choisir(self, texte: str) -> None:
        couleur = texte.strip().lower()
        if not preferences.est_couleur(couleur):
            self._erreur.set_label("Attendu : # et six chiffres hexadécimaux")
            return
        self._erreur.set_label("")
        self._code.set_text(couleur)
        self.set_child(_pastille(couleur, 18))
        self._bulle.popdown()
        self._sur_couleur(couleur)
