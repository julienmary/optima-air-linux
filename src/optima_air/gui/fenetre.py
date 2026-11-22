"""La fenêtre principale. Étape 1 : elle affiche ce que le fil de travail publie.

Disposition reprise de l'appli officielle : Preamp et USB Audio en haut, IR en bas. Les
slots sont numérotés à partir de 1, comme sur la pédale et dans l'appli officielle.
"""

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from optima_air import echelles, ir  # noqa: E402
from optima_air.device import Connexion, Constat, LectureSlot  # noqa: E402
from optima_air.erreurs import ErreurOptimaAir  # noqa: E402
from optima_air.gui.dialogues import (  # noqa: E402
    BoutonCouleur,
    DialogueChargement,
    FenetreSauvegardes,
)
from optima_air.gui.travail import (  # noqa: E402
    NIVEAU_MAX,
    Connecte,
    Deconnecte,
    EcritureCommencee,
    EcritureEchouee,
    EcritureTerminee,
    EtatLu,
    IrLue,
    Message,
    Niveaux,
    Progres,
    RepriseConstatee,
    RestaurationEchouee,
    RestaurationTerminee,
    SauvegardeEchouee,
    SauvegardeTerminee,
    TableauLu,
)
from optima_air.gui.widgets import Barre, Courbe, Potard  # noqa: E402
from optima_air.messages import Tableau  # noqa: E402
from optima_air.messages_reception import Etat, InfoSlot  # noqa: E402
from optima_air.store import Magasin  # noqa: E402

PRESETS = (
    "Travis",
    "Paradise",
    "Circles",
    "Sunset",
    "Warn",
    "Stageman",
    "Classic",
    "Vintage",
    "3 Shades",
)
ROUTINGS = ("Reamp", "Normal", "Dry Out")
POTARDS = (
    ("REVERB", "reverb"),
    ("IR LEVEL", "ir_level"),
    ("GAIN", "gain"),
    ("BASS", "bass"),
    ("MIDDLE", "middle"),
    ("TREBLE", "treble"),
)


def _nom_preset(preset: int) -> str:
    if preset == 0x7F:
        return "MANUAL"
    if 0 <= preset < len(PRESETS):
        return f"{preset + 1}. {PRESETS[preset]}"
    return f"inconnu ({preset})"


def _panneau(titre: str, contenu: Gtk.Widget) -> Gtk.Frame:
    cadre = Gtk.Frame(label=titre)
    cadre.set_child(contenu)
    return cadre


class Fenetre(Gtk.ApplicationWindow):
    def __init__(
        self, application: Gtk.Application, travail, magasin: Magasin, couleur: str, sur_couleur
    ) -> None:
        """`travail` est le fil de travail : la fenêtre ne parle à la pédale qu'à travers
        ses méthodes demander_…"""
        super().__init__(application=application, title="Optima Air")
        self._travail = travail
        self._magasin = magasin
        self._demander_slot = travail.demander_slot
        self._demander_reglage = travail.demander_reglage
        self._demander_reglage_slot = travail.demander_reglage_slot
        self._slots: list[InfoSlot] = []
        self._etiquettes: list[Gtk.Label] = []
        self._lecture_active: LectureSlot | None = None
        self._restauration: tuple[Path, int] | None = None
        self._tableaux: dict[Tableau, list[int]] = {}
        self._maj_en_cours = False
        self.set_default_size(900, 640)
        self._connexion: Connexion | None = None
        self._slot_actif: int | None = None
        self._lignes: list[Gtk.ListBoxRow] = []

        self._etat = Gtk.Label(label="Recherche de la pédale…", xalign=0)
        self._etat.add_css_class("etat")

        self._info = Gtk.Label(xalign=0, wrap=True)
        self._restaurer = Gtk.Button(label="Restaurer l'état d'avant")
        self._restaurer.set_halign(Gtk.Align.START)
        self._restaurer.set_visible(False)
        self._restaurer.connect("clicked", self._sur_restaurer)
        grille = Gtk.Grid(row_spacing=8, column_spacing=8)
        self._grille = grille
        grille.attach(_panneau("Preamp", self._construire_preamp()), 0, 0, 1, 1)
        grille.attach(_panneau("USB Audio", self._construire_usb()), 1, 0, 1, 1)
        grille.attach(_panneau("IR", self._construire_ir()), 0, 1, 1, 1)
        grille.attach(_panneau("OUTPUT", self._construire_sortie()), 1, 1, 1, 1)

        boite = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for marge in ("top", "bottom", "start", "end"):
            getattr(boite, f"set_margin_{marge}")(10)
        # Ce qui concerne toute la pédale, et non le slot actif, est sur la ligne du haut.
        sauvegarder = Gtk.Button(label="Sauvegarder tout")
        sauvegarder.connect("clicked", lambda _b: self._travail.demander_sauvegarde())
        sauvegardes = Gtk.Button(label="Sauvegardes…")
        sauvegardes.connect("clicked", self._sur_sauvegardes)
        self._boutons_du_haut = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._boutons_du_haut.append(sauvegarder)
        self._boutons_du_haut.append(sauvegardes)
        self._boutons_du_haut.set_sensitive(False)
        self._etat.set_hexpand(True)
        haut = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        haut.append(self._etat)
        haut.append(self._boutons_du_haut)
        # La couleur de l'interface : un choix d'affichage, disponible même sans pédale.
        haut.append(BoutonCouleur(couleur, sur_couleur))
        boite.append(haut)
        boite.append(self._info)
        boite.append(self._restaurer)
        boite.append(grille)
        self.set_child(boite)

    # --- construction ---

    def _construire_preamp(self) -> Gtk.Widget:
        self._potards = {
            attribut: Potard(
                nom, sur_changement=lambda valeur, a=attribut: self._sur_reglage(a, valeur)
            )
            for nom, attribut in POTARDS
        }
        rangee = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        for _, attribut in POTARDS:
            rangee.append(self._potards[attribut])
        self._preset = Gtk.Label(xalign=0)
        self._preamp_actif = self._interrupteur("preamp_actif")
        lignes = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        lignes.append(rangee)
        lignes.append(self._preset)
        lignes.append(self._ligne("Preamp", self._preamp_actif))
        return lignes

    def _construire_usb(self) -> Gtk.Widget:
        self._routing = Gtk.DropDown.new_from_strings(list(ROUTINGS))
        self._routing.connect(
            "notify::selected", lambda liste, _p: self._sur_reglage("routing", liste.get_selected())
        )
        self._usb_in = Potard("IN VOL", sur_changement=lambda v: self._sur_reglage("usb_in", v))
        self._usb_out = Potard("OUT VOL", sur_changement=lambda v: self._sur_reglage("usb_out", v))
        volumes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        volumes.append(self._usb_in)
        volumes.append(self._usb_out)
        boite = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        boite.append(self._ligne("Routing", self._routing))
        boite.append(volumes)
        return boite

    def _interrupteur(self, nom: str) -> Gtk.Switch:
        bouton = Gtk.Switch(valign=Gtk.Align.CENTER)
        bouton.connect("notify::active", lambda b, _p: self._sur_reglage(nom, int(b.get_active())))
        return bouton

    @staticmethod
    def _ligne(texte: str, controle: Gtk.Widget) -> Gtk.Box:
        ligne = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        ligne.append(Gtk.Label(label=texte, xalign=0))
        ligne.append(controle)
        return ligne

    def _construire_sortie(self) -> Gtk.Widget:
        # Niveaux bruts de 0 à 127 : leur correspondance en dB n'est pas connue.
        self._dry = Barre("DRY", NIVEAU_MAX)
        self._wet = Barre("WET", NIVEAU_MAX)
        for barre in (self._dry, self._wet):
            barre.set_vexpand(True)
        barres = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        barres.append(self._dry)
        barres.append(self._wet)
        return barres

    def _construire_ir(self) -> Gtk.Widget:
        self._liste = Gtk.ListBox()
        self._liste.set_selection_mode(Gtk.SelectionMode.NONE)
        self._liste.set_activate_on_single_click(True)
        self._liste.connect("row-activated", self._sur_clic_slot)
        defilement = Gtk.ScrolledWindow(min_content_width=230, vexpand=True)
        defilement.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        defilement.set_child(self._liste)

        self._volume = Potard(
            "VOLUME",
            sur_changement=lambda v: self._sur_reglage_slot(Tableau.VOLUME, v),
            afficher=lambda v: f"{echelles.volume_db(v):+.1f} dB",
        )
        self._low_cut = Potard(
            "LOW CUT",
            sur_changement=lambda v: self._sur_reglage_slot(Tableau.LOW_CUT, v),
            afficher=lambda v: f"{echelles.low_cut_hz(v)} Hz",
        )
        self._high_cut = Potard(
            "HIGH CUT",
            sur_changement=lambda v: self._sur_reglage_slot(Tableau.HIGH_CUT, v),
            afficher=lambda v: f"{echelles.high_cut_hz(v) / 1000:.1f} kHz",
        )
        self._potards_slot = {
            Tableau.VOLUME: self._volume,
            Tableau.LOW_CUT: self._low_cut,
            Tableau.HIGH_CUT: self._high_cut,
        }
        reglages = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        for potard in (self._volume, self._low_cut, self._high_cut):
            reglages.append(potard)
        # Un nom de slot peut faire 32 caractères : il est abrégé plutôt que d'élargir la fenêtre.
        self._ir_active = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self._ir_marche = self._interrupteur("ir_active")
        self._avertissement = Gtk.Label(xalign=0, wrap=True)
        self._avertissement.add_css_class("avertissement")
        entete = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        entete.append(reglages)
        entete.append(self._ir_active)
        entete.append(self._ligne("IR", self._ir_marche))
        self._courbe = Courbe()
        self._accepter_les_fichiers(self._courbe, None)  # sur la courbe : le slot actif
        charger = Gtk.Button(label="Charger un WAV…")
        charger.connect("clicked", self._sur_charger)
        exporter = Gtk.Button(label="Exporter en WAV…")
        exporter.connect("clicked", self._sur_exporter)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.append(charger)
        actions.append(exporter)
        droite = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        droite.append(entete)
        droite.append(actions)
        droite.append(self._avertissement)
        droite.append(self._courbe)

        boite = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10, vexpand=True)
        boite.append(defilement)
        boite.append(droite)
        return boite

    def _sur_reglage(self, attribut: str, valeur: int) -> None:
        # _maj_en_cours : le contrôle change parce qu'on affiche l'état de la pédale, pas
        # parce que l'utilisateur l'a touché. Ne rien renvoyer à la pédale dans ce cas.
        if self._connexion is not None and not self._maj_en_cours:
            self._demander_reglage(attribut, valeur)

    def _sur_reglage_slot(self, tableau: Tableau, valeur: int) -> None:
        if self._connexion is not None and self._slot_actif is not None:
            self._demander_reglage_slot(tableau, self._slot_actif, valeur)

    def _sur_clic_slot(self, _liste: Gtk.ListBox, ligne: Gtk.ListBoxRow) -> None:
        """Un clic rend le slot actif sur la pédale. Le surlignage ne bouge que lorsque la
        pédale l'annonce : elle reste la seule source de vérité."""
        if self._connexion is not None:
            self._demander_slot(ligne.get_index())

    # --- chargement, export, restauration ---

    def _rendre_disponible(self, disponible: bool) -> None:
        """Grise tout ce qui parle à la pédale pendant une écriture, une sauvegarde ou une
        restauration, et tant qu'elle n'est pas connectée."""
        self._grille.set_sensitive(disponible)
        self._boutons_du_haut.set_sensitive(disponible)

    def _dire(self, texte: str, erreur: bool = False) -> None:
        if erreur:
            self._info.add_css_class("avertissement")
        else:
            self._info.remove_css_class("avertissement")
        self._info.set_label(texte)

    def _sur_charger(self, _bouton: Gtk.Button) -> None:
        if self._connexion is None or self._slot_actif is None:
            return
        filtre = Gtk.FileFilter()
        filtre.set_name("Fichiers WAV")
        filtre.add_pattern("*.wav")
        filtre.add_pattern("*.WAV")
        filtres = Gio.ListStore.new(Gtk.FileFilter)
        filtres.append(filtre)
        dialogue = Gtk.FileDialog(title="Choisir un fichier WAV")
        dialogue.set_filters(filtres)
        dialogue.open(self, None, self._fichier_choisi)

    def _fichier_choisi(self, dialogue: Gtk.FileDialog, resultat) -> None:
        try:
            fichier = dialogue.open_finish(resultat)
        except GLib.Error:
            return  # annulé
        if self._slot_actif is not None and fichier.get_path() is not None:
            self._proposer_chargement(self._slot_actif, Path(fichier.get_path()))

    def _proposer_chargement(self, slot: int, chemin: Path) -> None:
        """Prépare le fichier et ouvre la confirmation, qui nomme le slot visé. Rien n'est
        écrit tant que l'utilisateur n'a pas confirmé."""
        if self._connexion is None or not 0 <= slot < len(self._slots):
            return
        try:
            preparee = ir.preparer_wav(chemin)
        except ErreurOptimaAir as erreur:
            self._dire(str(erreur), erreur=True)
            return
        DialogueChargement(
            self,
            slot,
            self._slots[slot].nom,
            chemin,
            preparee.avertissements,
            lambda nom: self._travail.demander_ecriture(slot, preparee.donnees, nom),
        ).present()

    def _accepter_les_fichiers(self, widget: Gtk.Widget, slot: int | None) -> None:
        """Rend le widget capable de recevoir un fichier glissé depuis le gestionnaire de
        fichiers. `slot` : le slot visé, ou None pour le slot actif au moment du dépôt."""
        cible = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        cible.connect("drop", self._sur_depot, slot)
        widget.add_controller(cible)

    def _sur_depot(
        self, _cible: Gtk.DropTarget, fichiers: Gdk.FileList, _x: float, _y: float, slot: int | None
    ) -> bool:
        vise = self._slot_actif if slot is None else slot
        chemins = [f.get_path() for f in fichiers.get_files() if f.get_path() is not None]
        if vise is None or self._connexion is None or not chemins:
            return False
        if len(chemins) > 1:
            self._dire("Un seul fichier à la fois : un slot ne contient qu'une IR.", erreur=True)
            return False
        # La confirmation s'ouvre une fois le glisser terminé, pas pendant.
        GLib.idle_add(self._apres_depot, vise, Path(chemins[0]))
        return True

    def _apres_depot(self, slot: int, chemin: Path) -> bool:
        self._proposer_chargement(slot, chemin)
        return GLib.SOURCE_REMOVE

    def _sur_exporter(self, _bouton: Gtk.Button) -> None:
        lecture = self._lecture_active
        if lecture is None:
            return
        dialogue = Gtk.FileDialog(title="Exporter en WAV")
        dialogue.set_initial_name(f"{lecture.infos.nom or 'slot'}.wav")
        dialogue.save(self, None, self._cible_choisie)

    def _cible_choisie(self, dialogue: Gtk.FileDialog, resultat) -> None:
        try:
            fichier = dialogue.save_finish(resultat)
        except GLib.Error:
            return  # annulé
        lecture = self._lecture_active
        if lecture is None:
            return
        cible = Path(fichier.get_path())
        try:
            cible.write_bytes(ir.contenu_wav(lecture.donnees))
        except OSError as erreur:
            self._dire(f"Impossible d'écrire {cible} : {erreur}", erreur=True)
            return
        self._dire(f"Slot {lecture.infos.slot + 1} « {lecture.infos.nom} » exporté dans {cible}")

    def _sur_sauvegardes(self, _bouton: Gtk.Button) -> None:
        if self._connexion is not None:
            FenetreSauvegardes(
                self, self._magasin, self._travail.demander_restauration_archive
            ).present()

    def _afficher_restauration(self, message: RestaurationTerminee) -> None:
        def liste(slots: tuple[int, ...]) -> str:
            return ", ".join(str(slot + 1) for slot in slots) or "aucun"

        bilan = message.bilan
        self._restauration = None
        self._restaurer.set_visible(False)
        self._dire(
            f"Restauration terminée. Slots réécrits puis relus à l'identique : "
            f"{liste(bilan.reecrits)}. Réglages seuls remis : {liste(bilan.regles)}. "
            f"Déjà identiques à l'archive : {len(bilan.inchanges)}."
        )

    def _sur_restaurer(self, _bouton: Gtk.Button) -> None:
        if self._restauration is not None and self._connexion is not None:
            archive, slot = self._restauration
            self._travail.demander_restauration(archive, slot)

    def _afficher_constat(self, constat: Constat) -> None:
        numero = constat.slot + 1
        if constat.issue == "aboutie":
            self._dire(f"Slot {numero} : l'écriture interrompue avait abouti. Rien à faire.")
        elif constat.issue == "inchange":
            self._dire(
                f"Slot {numero} : après l'écriture interrompue, il contient toujours ce "
                "qu'il y avait avant. Rien à faire."
            )
        else:
            self._dire(
                f"Slot {numero} : après l'écriture interrompue, son contenu n'est ni l'ancien "
                "ni le nouveau. Tant qu'il n'est pas remis d'aplomb, c'est le seul slot "
                "qu'on puisse écrire.",
                erreur=True,
            )
            self._restauration = (constat.archive, constat.slot)
            self._restaurer.set_visible(True)

    def _afficher_ecriture_terminee(self, message: EcritureTerminee) -> None:
        infos = message.lecture.infos
        self._rendre_disponible(True)
        self._restauration = None
        self._restaurer.set_visible(False)
        if 0 <= message.slot < len(self._slots):
            self._slots[message.slot] = infos
            self._etiquettes[message.slot].set_label(f"{infos.slot + 1}. {infos.nom}")
        if message.slot == self._slot_actif:
            self._ir_active.set_label(f"Slot {infos.slot + 1} : {infos.nom}")
            self._afficher_ir(IrLue(message.lecture))
        self._dire(
            f"Slot {infos.slot + 1} : « {infos.nom} » écrit, puis relu à l'identique. "
            f"Ancien contenu archivé dans {message.archive}"
        )

    # --- mise à jour ---

    def recevoir(self, message: Message) -> None:
        if isinstance(message, Connecte):
            self._afficher_connexion(message.connexion)
        elif isinstance(message, Deconnecte):
            self._afficher_deconnexion(message.raison)
        elif isinstance(message, EtatLu):
            self._afficher_etat(message.etat)
        elif isinstance(message, IrLue):
            self._afficher_ir(message)
        elif isinstance(message, TableauLu):
            self._tableaux[message.tableau] = list(message.valeurs)
            if self._slot_actif is not None:
                self._potards_slot[message.tableau].fixer(message.valeurs[self._slot_actif])
        elif isinstance(message, Niveaux):
            self._dry.fixer(message.dry)
            self._wet.fixer(message.wet)
        elif isinstance(message, EcritureCommencee):
            self._rendre_disponible(False)
            self._dire(f"Écriture du slot {message.slot + 1} en cours…")
        elif isinstance(message, EcritureTerminee):
            self._afficher_ecriture_terminee(message)
        elif isinstance(message, EcritureEchouee):
            self._dire(
                f"L'écriture du slot {message.slot + 1} a échoué : {message.raison} La pédale "
                "est laissée tranquille 35 s, puis l'état du slot sera constaté.",
                erreur=True,
            )
        elif isinstance(message, RepriseConstatee):
            self._afficher_constat(message.constat)
        elif isinstance(message, Progres):
            self._rendre_disponible(False)
            self._dire(f"{message.operation} : {message.fait + 1} sur {message.total}…")
        elif isinstance(message, SauvegardeTerminee):
            self._rendre_disponible(True)
            self._dire(f"Les 23 slots sont sauvegardés dans {message.sauvegarde.archive}")
        elif isinstance(message, SauvegardeEchouee):
            self._dire(f"La sauvegarde a échoué : {message.raison}", erreur=True)
        elif isinstance(message, RestaurationTerminee):
            self._afficher_restauration(message)
        elif isinstance(message, RestaurationEchouee):
            self._dire(
                f"La restauration s'est arrêtée : {message.raison} Les slots déjà traités "
                "sont en ordre ; l'état de la pédale va être relu.",
                erreur=True,
            )

    def _afficher_connexion(self, connexion: Connexion) -> None:
        self._connexion = connexion
        self._rendre_disponible(True)
        self._slots = list(connexion.slots)
        self._tableaux = {
            Tableau.VOLUME: list(connexion.volumes),
            Tableau.LOW_CUT: list(connexion.low_cuts),
            Tableau.HIGH_CUT: list(connexion.high_cuts),
        }
        self._etat.remove_css_class("deconnecte")
        self._etat.set_label(f"Connectée, firmware {echelles.version_texte(connexion.firmware)}")
        for ligne in self._lignes:
            self._liste.remove(ligne)
        self._lignes = []
        self._etiquettes = []
        for infos in connexion.slots:
            etiquette = Gtk.Label(label=f"{infos.slot + 1}. {infos.nom}", xalign=0)
            ligne = Gtk.ListBoxRow()
            ligne.set_child(etiquette)
            self._accepter_les_fichiers(ligne, infos.slot)
            self._etiquettes.append(etiquette)
            self._liste.append(ligne)
            self._lignes.append(ligne)
        self._slot_actif = None
        self._afficher_etat(connexion.etat)

    def _afficher_deconnexion(self, raison: str) -> None:
        self._connexion = None
        self._lecture_active = None
        self._rendre_disponible(False)
        self._etat.add_css_class("deconnecte")
        self._etat.set_label(f"Déconnectée : {raison}")
        for potard in (*self._potards.values(), self._volume, self._low_cut, self._high_cut):
            potard.fixer(None)
        self._usb_in.fixer(None)
        self._usb_out.fixer(None)
        self._dry.fixer(None)
        self._wet.fixer(None)
        self._courbe.vider()
        self._avertissement.set_label("")

    def _afficher_etat(self, etat: Etat) -> None:
        self._maj_en_cours = True
        try:
            for _, attribut in POTARDS:
                self._potards[attribut].fixer(getattr(etat, attribut))
            self._preset.set_label(f"Preset : {_nom_preset(etat.preset)}")
            self._preamp_actif.set_active(etat.preamp_actif)
            if 0 <= etat.routing < len(ROUTINGS):
                self._routing.set_selected(etat.routing)
            self._usb_in.fixer(etat.usb_in)
            self._usb_out.fixer(etat.usb_out)
            self._ir_marche.set_active(etat.ir_active)
            self._marquer_slot(etat.slot_actif)
        finally:
            self._maj_en_cours = False

    def _marquer_slot(self, slot: int) -> None:
        if self._connexion is None or not 0 <= slot < len(self._slots):
            return
        if self._slot_actif is not None and self._slot_actif < len(self._lignes):
            self._lignes[self._slot_actif].remove_css_class("actif")
        if slot != self._slot_actif:
            self._lecture_active = None  # l'IR du nouveau slot n'est pas encore lue
        self._lignes[slot].add_css_class("actif")
        self._slot_actif = slot
        nom = self._slots[slot].nom
        self._ir_active.set_label(f"Slot {slot + 1} : {nom}")
        for tableau, potard in self._potards_slot.items():
            potard.fixer(self._tableaux[tableau][slot])

    def _afficher_ir(self, message: IrLue) -> None:
        lecture = message.lecture
        if lecture.infos.slot == self._slot_actif:
            self._lecture_active = lecture
        frequences, decibels = ir.reponse_en_frequence(lecture.donnees)
        self._courbe.fixer(frequences, decibels, lecture.infos.nom)
        if lecture.concorde:
            self._avertissement.set_label("")
        else:
            self._avertissement.set_label(
                "Attention : l'identifiant annoncé par la pédale ne correspond pas aux "
                "données lues."
            )
