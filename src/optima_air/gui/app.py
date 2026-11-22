"""L'application GTK : la fenêtre, le thème, et le fil de travail qui parle à la pédale."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from optima_air import preferences  # noqa: E402
from optima_air.cli import ouvrir_session  # noqa: E402
from optima_air.gui import widgets  # noqa: E402
from optima_air.gui.fenetre import Fenetre  # noqa: E402
from optima_air.gui.travail import Message, Travailleur  # noqa: E402
from optima_air.store import Magasin  # noqa: E402

THEME = """
window { background-color: #0b0b0b; color: #eeeeee; }
frame { border: 2px solid ACCENT; border-radius: 8px; padding: 8px; }
frame > label { color: ACCENT; font-weight: bold; font-size: 15px; margin: 0 6px; }
label.etat { color: ACCENT; font-weight: bold; }
label.etat.deconnecte { color: #ff6b5a; }
label.avertissement { color: #ff6b5a; }
list { background-color: transparent; color: #eeeeee; }
list row { padding: 2px 8px; border-radius: 4px; }
list row:drop(active) { box-shadow: inset 0 0 0 2px ACCENT; }
list row.actif { background-color: ACCENT; color: SUR_ACCENT; font-weight: bold; }
scrolledwindow { border: 1px solid #555555; border-radius: 6px; }
switch:checked { background-color: ACCENT; background-image: none; }
"""


def _redessiner(widget: Gtk.Widget) -> None:
    """Redessine le widget et tous ses descendants (les widgets dessinés à la main ne
    suivent pas le thème tout seuls)."""
    widget.queue_draw()
    enfant = widget.get_first_child()
    while enfant is not None:
        _redessiner(enfant)
        enfant = enfant.get_next_sibling()


class Application(Gtk.Application):
    def __init__(self, magasin: Magasin, firmware_inconnu: bool) -> None:
        super().__init__(application_id="io.github.julienmary.OptimaAirLinux")
        self._magasin = magasin
        self._firmware_inconnu = firmware_inconnu
        self._fenetre: Fenetre | None = None
        self._theme = Gtk.CssProvider()
        self._preferences = preferences.dossier_par_defaut()
        self._travailleur: Travailleur | None = None

    def do_activate(self) -> None:
        if self._fenetre is None:
            Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), self._theme, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
            couleur = preferences.lire_couleur(self._preferences)
            self._appliquer_couleur(couleur)
            self._travailleur = Travailleur(
                lambda: ouvrir_session(self._magasin, self._firmware_inconnu), self._publier
            )
            self._fenetre = Fenetre(
                self, self._travailleur, self._magasin, couleur, self._sur_couleur
            )
            self._travailleur.start()
        self._fenetre.present()

    def _appliquer_couleur(self, couleur: str) -> None:
        self._theme.load_from_string(
            THEME.replace("SUR_ACCENT", preferences.texte_lisible_sur(couleur)).replace(
                "ACCENT", couleur
            )
        )
        widgets.fixer_accent(*preferences.composantes(couleur))

    def _sur_couleur(self, couleur: str) -> None:
        """L'utilisateur a choisi une couleur : on l'applique et on la retient."""
        self._appliquer_couleur(couleur)
        if self._fenetre is not None:
            _redessiner(self._fenetre)
        preferences.ecrire_couleur(self._preferences, couleur)

    def _publier(self, message: Message) -> None:
        """Appelé depuis le fil de travail : renvoie le message au fil d'affichage."""
        GLib.idle_add(self._recevoir, message)

    def _recevoir(self, message: Message) -> bool:
        if self._fenetre is not None:
            self._fenetre.recevoir(message)
        return GLib.SOURCE_REMOVE

    def do_shutdown(self) -> None:
        if self._travailleur is not None:
            self._travailleur.arreter()
            # Le fil finit le slot en cours (une écriture et sa relecture) avant de rendre la main.
            self._travailleur.join(10.0)
        Gtk.Application.do_shutdown(self)


def lancer(magasin: Magasin, firmware_inconnu: bool) -> int:
    return int(Application(magasin, firmware_inconnu).run(None))
