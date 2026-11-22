"""Widgets dessinés avec cairo : potard, barre de niveau, courbe de réponse.

Étape 1 : affichage seul, aucun de ces widgets ne réagit à la souris.
"""

import math

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

ACCENT = (0.17, 0.46, 1.0)  # bleu électrique, #2c75ff ; changé par fixer_accent


def fixer_accent(rouge: float, vert: float, bleu: float) -> None:
    """Change la couleur d'accent des widgets dessinés. À faire suivre d'un queue_draw."""
    global ACCENT
    ACCENT = (rouge, vert, bleu)


GRIS = (0.33, 0.33, 0.33)
GRIS_CLAIR = (0.62, 0.62, 0.62)
TEXTE = (0.93, 0.93, 0.93)
POLICE = "Sans"

DEBUT_ARC = 0.75 * math.pi  # 7 h 30, sens horaire
COURSE_ARC = 1.5 * math.pi  # 270 degrés
PIXELS_PAR_POINT = 1.5  # glisser de 150 pixels parcourt toute la course
DELAI_AVANT_DE_LACHER_MS = 400


def _texte_centre(cr, texte: str, x: float, y: float, taille: float, couleur, gras=False) -> None:
    cr.select_font_face(POLICE, 0, 1 if gras else 0)
    cr.set_font_size(taille)
    etendue = cr.text_extents(texte)
    cr.set_source_rgb(*couleur)
    cr.move_to(x - etendue.width / 2 - etendue.x_bearing, y)
    cr.show_text(texte)


class Potard(Gtk.DrawingArea):
    """Un potard avec son nom au-dessus et sa valeur au-dessous."""

    def __init__(self, nom: str, maximum: int = 100, sur_changement=None, afficher=str) -> None:
        """Avec sur_changement(valeur), le potard se règle à la souris : glisser vers le
        haut ou le bas, ou molette. Sans, il ne fait qu'afficher."""
        super().__init__()
        self._nom = nom
        self._maximum = maximum
        self._valeur: int | None = None
        self._texte = "–"
        self._sur_changement = sur_changement
        self._afficher = afficher  # valeur brute -> texte montré sous le potard
        self._en_main = False
        self._depart = 0
        self._en_attente: tuple[int, str | None] | None = None
        self._cumul_molette = 0.0
        self._minuterie = 0
        self.set_content_width(92)
        self.set_content_height(112)
        self.set_draw_func(self._dessiner)
        if sur_changement is not None:
            glisser = Gtk.GestureDrag()
            glisser.connect("drag-begin", self._debut_glisser)
            glisser.connect("drag-update", self._glisser)
            glisser.connect("drag-end", self._fin_glisser)
            self.add_controller(glisser)
            molette = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
            molette.connect("scroll", self._molette)
            self.add_controller(molette)

    def _debut_glisser(self, _geste, _x: float, _y: float) -> None:
        if self._valeur is not None:
            self._en_main = True
            self._depart = self._valeur

    def _glisser(self, _geste, _dx: float, dy: float) -> None:
        if self._en_main:
            self._regler(self._depart - round(dy / PIXELS_PAR_POINT))

    def _fin_glisser(self, _geste, _dx: float, _dy: float) -> None:
        self._lacher_bientot()

    def _molette(self, _controleur, _dx: float, dy: float) -> bool:
        if self._valeur is None:
            return True
        # Les pavés tactiles envoient des fractions de cran : on les cumule.
        self._cumul_molette += dy
        crans = int(self._cumul_molette)
        if crans:
            self._cumul_molette -= crans
            self._en_main = True
            self._regler(self._valeur - crans)
            self._lacher_bientot()
        return True

    def _lacher_bientot(self) -> None:
        """Rend la main à la pédale un court instant après le dernier geste : le temps que
        sa réponse au dernier envoi soit arrivée."""
        if self._minuterie:
            GLib.source_remove(self._minuterie)
        self._minuterie = GLib.timeout_add(DELAI_AVANT_DE_LACHER_MS, self._lacher)

    def _lacher(self) -> bool:
        self._minuterie = 0
        self._en_main = False
        self._cumul_molette = 0.0
        if self._en_attente is not None:
            valeur, texte = self._en_attente
            self._en_attente = None
            self.fixer(valeur, texte)
        return GLib.SOURCE_REMOVE

    def _regler(self, valeur: int) -> None:
        valeur = max(0, min(self._maximum, valeur))
        if valeur != self._valeur:
            self._valeur = valeur
            self._texte = self._afficher(valeur)
            self.queue_draw()
            self._sur_changement(valeur)

    def fixer(self, valeur: int | None, texte: str | None = None) -> None:
        """Valeur brute (0 à maximum) et texte affiché ; None quand rien n'est connu.
        Pendant que l'utilisateur tient le potard, la valeur est mise de côté : elle est
        affichée quand il le lâche, la pédale restant l'arbitre."""
        if self._en_main and valeur is not None:
            self._en_attente = (valeur, texte)
            return
        self._en_attente = None
        self._valeur = valeur
        if valeur is None:
            self._texte = "–"
        else:
            self._texte = texte if texte is not None else self._afficher(valeur)
        self.queue_draw()

    def _dessiner(self, _zone, cr, largeur: int, hauteur: int) -> None:
        cx = largeur / 2
        cy = hauteur / 2 + 2
        rayon = min(largeur, hauteur - 40) / 2 - 5
        _texte_centre(cr, self._nom, cx, 13, 11, ACCENT, gras=True)
        cr.set_line_width(5)
        cr.set_line_cap(1)
        cr.set_source_rgb(*GRIS)
        cr.new_path()  # sans cela, cairo relie la fin du texte au début de l'arc
        cr.arc(cx, cy, rayon, DEBUT_ARC, DEBUT_ARC + COURSE_ARC)
        cr.stroke()
        if self._valeur is not None:
            part = max(0.0, min(1.0, self._valeur / self._maximum))
            angle = DEBUT_ARC + part * COURSE_ARC
            cr.set_source_rgb(*ACCENT)
            cr.new_path()
            cr.arc(cx, cy, rayon, DEBUT_ARC, angle)
            cr.stroke()
            cr.set_source_rgb(*TEXTE)
            cr.set_line_width(3)
            cr.move_to(cx + 0.35 * rayon * math.cos(angle), cy + 0.35 * rayon * math.sin(angle))
            cr.line_to(cx + 0.85 * rayon * math.cos(angle), cy + 0.85 * rayon * math.sin(angle))
            cr.stroke()
        _texte_centre(cr, self._texte, cx, hauteur - 6, 12, TEXTE)


class Barre(Gtk.DrawingArea):
    """Une barre verticale de 0 à maximum, avec son nom au-dessous."""

    def __init__(self, nom: str, maximum: int = 100) -> None:
        super().__init__()
        self._nom = nom
        self._maximum = maximum
        self._valeur: int | None = None
        self.set_content_width(64)
        self.set_content_height(132)
        self.set_draw_func(self._dessiner)

    def fixer(self, valeur: int | None) -> None:
        self._valeur = valeur
        self.queue_draw()

    def _dessiner(self, _zone, cr, largeur: int, hauteur: int) -> None:
        haut, bas = 6.0, hauteur - 36.0
        x = largeur / 2
        cr.set_line_width(6)
        cr.set_line_cap(1)
        cr.set_source_rgb(*GRIS)
        cr.move_to(x, haut)
        cr.line_to(x, bas)
        cr.stroke()
        texte = "–"
        if self._valeur is not None:
            part = max(0.0, min(1.0, self._valeur / self._maximum))
            cr.set_source_rgb(*ACCENT)
            cr.move_to(x, bas)
            cr.line_to(x, bas - part * (bas - haut))
            cr.stroke()
            texte = str(self._valeur)
        _texte_centre(cr, texte, x, hauteur - 20, 12, TEXTE)
        _texte_centre(cr, self._nom, x, hauteur - 5, 10, ACCENT, gras=True)


class Courbe(Gtk.DrawingArea):
    """Réponse en fréquence : 20 Hz à 20 kHz en échelle logarithmique, -30 à +30 dB (les IR
    réelles montent jusqu'à +21 dB : l'échelle de l'appli officielle, -50 à +10, les coupait)."""

    F_MIN, F_MAX = 20.0, 20000.0
    DB_MIN, DB_MAX = -30.0, 30.0
    FREQUENCES = (
        (20, "20"),
        (50, "50"),
        (100, "100"),
        (200, "200"),
        (500, "500"),
        (1000, "1 k"),
        (2000, "2 k"),
        (5000, "5 k"),
        (10000, "10 k"),
        (20000, "20 k"),
    )

    def __init__(self) -> None:
        super().__init__()
        self._frequences: list[float] = []
        self._decibels: list[float] = []
        self._titre = ""
        self.set_content_width(420)
        self.set_content_height(220)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_draw_func(self._dessiner)

    def fixer(self, frequences: list[float], decibels: list[float], titre: str) -> None:
        self._frequences, self._decibels, self._titre = frequences, decibels, titre
        self.queue_draw()

    def vider(self) -> None:
        self.fixer([], [], "")

    def _dessiner(self, _zone, cr, largeur: int, hauteur: int) -> None:
        gauche, droite, haut, bas = 40.0, largeur - 10.0, 10.0, hauteur - 22.0

        def x_de(frequence: float) -> float:
            part = math.log(frequence / self.F_MIN) / math.log(self.F_MAX / self.F_MIN)
            return gauche + part * (droite - gauche)

        def y_de(decibel: float) -> float:
            borne = max(self.DB_MIN, min(self.DB_MAX, decibel))
            return haut + (self.DB_MAX - borne) / (self.DB_MAX - self.DB_MIN) * (bas - haut)

        cr.set_line_width(1)
        for frequence, etiquette in self.FREQUENCES:
            x = round(x_de(frequence)) + 0.5
            cr.set_source_rgb(*GRIS)
            cr.move_to(x, haut)
            cr.line_to(x, bas)
            cr.stroke()
            _texte_centre(cr, etiquette, x, hauteur - 7, 10, GRIS_CLAIR)
        for decibel in range(int(self.DB_MIN), int(self.DB_MAX) + 1, 10):
            y = round(y_de(decibel)) + 0.5
            cr.set_source_rgb(*(GRIS_CLAIR if decibel == 0 else GRIS))
            cr.move_to(gauche, y)
            cr.line_to(droite, y)
            cr.stroke()
            _texte_centre(
                cr, f"{decibel:+d}" if decibel else "0", gauche - 18, y + 4, 10, GRIS_CLAIR
            )
        if self._frequences:
            cr.save()
            cr.rectangle(gauche, haut, droite - gauche, bas - haut)
            cr.clip()
            cr.set_source_rgb(*ACCENT)
            cr.set_line_width(2)
            for i, (frequence, decibel) in enumerate(
                zip(self._frequences, self._decibels, strict=True)
            ):
                if i:
                    cr.line_to(x_de(frequence), y_de(decibel))
                else:
                    cr.move_to(x_de(frequence), y_de(decibel))
            cr.stroke()
            cr.restore()
        if self._titre:
            _texte_centre(cr, self._titre, (gauche + droite) / 2, haut + 14, 11, TEXTE)
