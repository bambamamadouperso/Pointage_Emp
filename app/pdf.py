"""Exports PDF (suivi journalier, rapports) : A4 paysage, bandeau d'en-tête, filtres en pastilles, cartes
d'indicateurs et tableaux aérés (sans grille), statuts et durées en pastilles colorées comme à l'écran."""
from datetime import datetime

from fpdf import FPDF

from . import pointage

# Polices standard PDF (latin-1) : on remplace les quelques caractères typographiques hors latin-1.
_REPLACE = str.maketrans({"—": "-", "–": "-", "’": "'", "‘": "'", "“": '"', "”": '"', "…": "...", "œ": "oe", "Œ": "OE",
                          "→": "->", " ": " ", " ": " ", "≥": ">=", "≤": "<="})

# Charte (identique au tableau de bord et aux mails).
NAVY, TEAL, INK, MUTED = (27, 79, 138), (14, 124, 110), (11, 26, 43), (122, 136, 152)
LINE, ZEBRA, CHIP = (230, 236, 243), (248, 250, 252), (238, 243, 248)
GREEN, BLUE, RED, ORANGE, GREY, VIOLET = (16, 185, 129), (59, 130, 246), (225, 29, 72), (249, 115, 22), (100, 116, 139), (124, 58, 237)


def _hex(value: str) -> tuple:
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))


_CSS_PILLS = {
    "st-ok": ("#d1fae5", "#047857"), "st-late": ("#ffedd5", "#c2410c"), "st-abs": ("#ffe4e6", "#be123c"),
    "st-inc": ("#e2e8f0", "#475569"), "st-off": ("#e0f2fe", "#0369a1"), "st-leave": ("#e4f5d3", "#3f6212"),
    "st-remote": ("#cdeee7", "#0f6b5f"), "st-field": ("#dbeafe", "#1d4ed8"), "st-sick": ("#ece8e5", "#57534e"),
    "st-train": ("#ede9fe", "#6d28d9"), "st-mission": ("#f3e8ff", "#7e22ce"), "st-mater": ("#fce7f3", "#be185d"),
}
# Pastilles (fond, texte) par statut, objectif atteint ou non, niveau d'un taux.
STATUS_PILLS = {code: tuple(map(_hex, _CSS_PILLS.get(css, _CSS_PILLS["st-inc"])))
                for code, (_, css) in pointage.STATUTS.items()}
GOOD, BAD = tuple(map(_hex, _CSS_PILLS["st-ok"])), tuple(map(_hex, _CSS_PILLS["st-abs"]))
WARN = tuple(map(_hex, _CSS_PILLS["st-late"]))


def rate_pill(value, good: float = 95, warn: float = 85, higher_is_better: bool = True):
    """Pastille d'un taux : vert, orange ou rouge selon les seuils."""
    if value is None:
        return None
    v = float(value) if higher_is_better else 100 - float(value)
    return GOOD if v >= good else WARN if v >= warn else BAD


def text(value) -> str:
    if value is None:
        return ""
    return str(value).translate(_REPLACE).encode("latin-1", "replace").decode("latin-1")


class Document(FPDF):
    BAND = 21  # hauteur du bandeau d'en-tête (mm)

    def __init__(self, title: str, subtitle: str = "", user: str = ""):
        super().__init__(orientation="L", unit="mm", format="A4")
        self.doc_title, self.subtitle, self.user = text(title), text(subtitle), text(user)
        self.stamp = datetime.now()
        self.set_title(self.doc_title)
        self.set_author(self.user or "Pointage")
        self.set_auto_page_break(True, margin=14)
        self.set_margins(12, self.BAND + 7, 12)
        self.alias_nb_pages()
        self.add_page()

    # ------------------------------------------------------------------ cadre de page

    def header(self):
        self.set_fill_color(*NAVY)
        self.rect(0, 0, self.w, self.BAND, style="F")
        self.set_fill_color(*TEAL)
        self.rect(0, self.BAND, self.w, 1.3, style="F")
        self.set_xy(self.l_margin, 5)
        self.set_font("Helvetica", "B", 16)
        self.set_text_color(255, 255, 255)
        self.cell(0, 7, self.doc_title)
        if self.subtitle:
            self.set_xy(self.l_margin, 12.5)
            self.set_font("Helvetica", "", 9)
            self.set_text_color(199, 219, 240)
            self.cell(0, 5, self.subtitle)
        self.set_xy(self.l_margin, 6)
        self.set_font("Helvetica", "B", 8)
        self.set_text_color(255, 255, 255)
        self.cell(0, 4, "POINTAGE", align="R")
        self.set_xy(self.l_margin, 11)
        self.set_font("Helvetica", "", 7.5)
        self.set_text_color(199, 219, 240)
        self.cell(0, 4, text(f"Édité le {self.stamp:%d/%m/%Y à %H:%M}"), align="R")
        self.set_xy(self.l_margin, self.BAND + 7)
        self.set_text_color(*INK)

    def footer(self):
        self.set_draw_color(*LINE)
        self.set_line_width(0.2)
        self.line(self.l_margin, self.h - 10.5, self.w - self.r_margin, self.h - 10.5)
        self.set_xy(self.l_margin, self.h - 9.5)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(*MUTED)
        self.cell(0, 4, text(self.doc_title + (f"  ·  exporté par {self.user}" if self.user else "")))
        self.set_x(self.l_margin)
        self.cell(0, 4, f"Page {self.page_no()} / {{nb}}", align="R")

    @property
    def inner(self) -> float:
        return self.w - self.l_margin - self.r_margin

    # ------------------------------------------------------------------ blocs

    def section(self, title: str, note: str = ""):
        if self.get_y() > self.page_break_trigger - 25:  # titre jamais seul en bas de page
            self.add_page()
        self.ln(3)
        y = self.get_y()
        self.set_fill_color(*TEAL)
        self.rect(self.l_margin, y + 0.8, 1.4, 5, style="F", round_corners=True, corner_radius=0.7)
        self.set_xy(self.l_margin + 3.5, y)
        self.set_font("Helvetica", "B", 12)
        self.set_text_color(*INK)
        self.cell(0, 6.5, text(title), new_x="LMARGIN", new_y="NEXT")
        if note:
            self.set_x(self.l_margin + 3.5)
            self.set_font("Helvetica", "", 8)
            self.set_text_color(*MUTED)
            self.multi_cell(self.inner - 3.5, 4, text(note), new_x="LMARGIN", new_y="NEXT")
        self.ln(1.5)
        self.set_text_color(*INK)

    def filters(self, items):
        """Filtres appliqués en pastilles « Libellé  valeur », renvoyées à la ligne au besoin."""
        if not items:
            return
        x, y, h, gap = self.l_margin, self.get_y(), 5.6, 1.8
        for label, value in items:
            label, value = text(label), text(value)
            self.set_font("Helvetica", "", 7.5)
            lw = self.get_string_width(label + "  ")
            self.set_font("Helvetica", "B", 7.5)
            vw = min(self.get_string_width(value), self.inner - lw - 8)
            w = lw + vw + 6
            if x + w > self.w - self.r_margin:
                x, y = self.l_margin, y + h + gap
            self.set_fill_color(*CHIP)
            self.rect(x, y, w, h, style="F", round_corners=True, corner_radius=2.8)
            self.set_xy(x + 3, y)
            self.set_font("Helvetica", "", 7.5)
            self.set_text_color(*MUTED)
            self.cell(lw, h, label)
            self.set_font("Helvetica", "B", 7.5)
            self.set_text_color(*INK)
            self.cell(vw, h, self._fit(value, vw))
            x += w + gap
        self.set_xy(self.l_margin, y + h + 3)

    def kpis(self, items):
        """Cartes d'indicateurs (libellé, valeur, précision[, couleur]) sur une ligne, liseré de couleur à gauche."""
        palette = [GREEN, RED, BLUE, ORANGE, TEAL, GREY]
        gap = 3
        width = (self.inner - gap * (len(items) - 1)) / len(items)
        x, y, h = self.l_margin, self.get_y(), 21
        for n, item in enumerate(items):
            label, value, hint = item[:3]
            color = item[3] if len(item) > 3 else palette[n % len(palette)]
            self.set_draw_color(*LINE)
            self.set_line_width(0.25)
            self.set_fill_color(255, 255, 255)
            self.rect(x, y, width, h, style="DF", round_corners=True, corner_radius=2.2)
            self.set_fill_color(*color)
            self.rect(x, y + 3, 1.3, h - 6, style="F", round_corners=True, corner_radius=0.6)
            self.set_xy(x + 4, y + 2.5)
            self.set_font("Helvetica", "B", 6.5)
            self.set_text_color(*MUTED)
            self.cell(width - 6, 4, text(label).upper())
            self.set_xy(x + 4, y + 7)
            self.set_font("Helvetica", "B", 16)
            self.set_text_color(*INK)
            self.cell(width - 6, 7.5, text(value))
            self.set_xy(x + 4, y + 15)
            self.set_font("Helvetica", "", 6.5)
            self.set_text_color(*MUTED)
            self.cell(width - 6, 4, self._fit(text(hint), width - 6))
            x += width + gap
        self.set_text_color(*INK)
        self.set_xy(self.l_margin, y + h + 3)

    def _fit(self, value: str, width: float) -> str:
        if self.get_string_width(value) <= width:
            return value
        while value and self.get_string_width(value + "..") > width:
            value = value[:-1]
        return value + ".."

    def table(self, headers, rows, widths, align=None, pills=None, colors=None, bold_last=False):
        """Tableau paginé, sans grille : en-tête teinté répété à chaque page, lignes alternées, séparateurs fins.
        `pills` : {(ligne, colonne): (fond, texte)} pour une pastille ; `colors` : {(ligne, colonne): texte}."""
        scale = self.inner / sum(widths)
        widths = [w * scale for w in widths]
        align = align or ["L"] * len(headers)
        pills, colors = pills or {}, colors or {}
        line_h, pad = 6.4, 1.8
        labels = [text(h).upper() for h in headers]
        self.set_font("Helvetica", "B", 6.5)
        # En-tête trop long pour sa colonne : sur deux lignes (coupé au mot) plutôt que tronqué.
        split = [self.get_string_width(label) > w - 2 * pad and " " in label for label, w in zip(labels, widths)]
        head_h = 9 if any(split) else 7

        def two_lines(label: str, w: float) -> tuple[str, str]:
            words = label.split(" ")
            for n in range(len(words) - 1, 0, -1):
                if self.get_string_width(" ".join(words[:n])) <= w - 2 * pad:
                    return " ".join(words[:n]), " ".join(words[n:])
            return words[0], " ".join(words[1:])

        def head():
            y = self.get_y()
            self.set_fill_color(235, 241, 248)
            self.rect(self.l_margin, y, self.inner, head_h, style="F", round_corners=True, corner_radius=1.5)
            x = self.l_margin
            self.set_text_color(*NAVY)
            for w, label, a, cut in zip(widths, labels, align, split):
                inner = w - 2 * pad
                self.set_font("Helvetica", "B", 6.5)
                lines = two_lines(label, w) if cut else (label,)
                size = 6.5
                while size > 4.6 and max(self.get_string_width(t) for t in lines) > inner:  # police réduite avant de tronquer
                    size -= 0.2
                    self.set_font_size(size)
                if cut:
                    self.set_xy(x + pad, y + head_h / 2 - 3.2)
                    self.cell(inner, 3.2, self._fit(lines[0], inner), align=a)
                    self.set_xy(x + pad, y + head_h / 2)
                    self.cell(inner, 3.2, self._fit(lines[1], inner), align=a)
                else:
                    self.set_xy(x + pad, y)
                    self.cell(inner, head_h, self._fit(label, inner), align=a)
                x += w
            self.set_xy(self.l_margin, y + head_h + 0.6)

        head()
        for i, row in enumerate(rows):
            if self.get_y() + line_h > self.page_break_trigger:
                self.add_page()
                head()
            y = self.get_y()
            last = bold_last and i == len(rows) - 1
            if last:
                self.set_fill_color(225, 242, 239)
                self.rect(self.l_margin, y, self.inner, line_h, style="F", round_corners=True, corner_radius=1.5)
            elif i % 2:
                self.set_fill_color(*ZEBRA)
                self.rect(self.l_margin, y, self.inner, line_h, style="F")
            x = self.l_margin
            for j, (w, value) in enumerate(zip(widths, row)):
                value = text(value)
                pill = pills.get((i, j))
                if pill and value:
                    bg, fg = pill
                    self.set_font("Helvetica", "B", 6.6)
                    value = self._fit(value, w - 2 * pad - 3)
                    pw = self.get_string_width(value) + 3.6
                    px = x + pad if align[j] == "L" else x + w - pad - pw if align[j] == "R" else x + (w - pw) / 2
                    self.set_fill_color(*bg)
                    self.rect(px, y + 1.15, pw, line_h - 2.3, style="F", round_corners=True, corner_radius=(line_h - 2.3) / 2)
                    self.set_text_color(*fg)
                    self.set_xy(px, y)
                    self.cell(pw, line_h, value, align="C")
                else:
                    self.set_font("Helvetica", "B" if last else "", 7.2)
                    self.set_text_color(*colors.get((i, j), INK if not last else TEAL))
                    self.set_xy(x + pad, y)
                    self.cell(w - 2 * pad, line_h, self._fit(value, w - 2 * pad), align=align[j])
                x += w
            if not last:
                self.set_draw_color(*LINE)
                self.set_line_width(0.15)
                self.line(self.l_margin, y + line_h, self.w - self.r_margin, y + line_h)
            self.set_xy(self.l_margin, y + line_h)
        self.set_text_color(*INK)
        self.ln(1)

    def output_bytes(self) -> bytes:
        return bytes(self.output())
