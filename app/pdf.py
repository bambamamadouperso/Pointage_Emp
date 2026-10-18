"""Exports PDF (suivi journalier, rapports) : A4 paysage, en-tête, filtres, tableaux paginés."""
from datetime import datetime

from fpdf import FPDF

# Polices standard PDF (latin-1) : on remplace les quelques caractères typographiques hors latin-1.
_REPLACE = str.maketrans({"—": "-", "–": "-", "’": "'", "‘": "'", "“": '"', "”": '"', "…": "...", "œ": "oe", "Œ": "OE",
                          "→": "->", " ": " ", " ": " ", "≥": ">=", "≤": "<="})

COLORS = {"A_L_HEURE": (220, 252, 231), "RETARD": (255, 237, 213), "ABSENT": (254, 226, 226), "INCOMPLET": (229, 231, 235),
          "NON_OUVRE": (224, 242, 254), "CONGE_ANNUEL": (228, 245, 211), "CONGE_EXCEP": (228, 245, 211),
          "CONGE_MATERNITE": (252, 231, 243), "TELETRAVAIL": (205, 238, 231), "TERRAIN": (219, 234, 254),
          "ARRET_MALADIE": (231, 229, 228), "REPOS": (224, 242, 254), "FORMATION": (237, 233, 254), "MISSION": (243, 232, 255)}
OK, KO = (0, 97, 0), (156, 0, 6)


def text(value) -> str:
    if value is None:
        return ""
    return str(value).translate(_REPLACE).encode("latin-1", "replace").decode("latin-1")


class Document(FPDF):
    def __init__(self, title: str, subtitle: str = "", user: str = ""):
        super().__init__(orientation="L", unit="mm", format="A4")
        self.doc_title, self.subtitle, self.user = text(title), text(subtitle), text(user)
        self.set_title(self.doc_title)
        self.set_author(self.user or "Pointage")
        self.set_auto_page_break(True, margin=12)
        self.set_margins(10, 10, 10)
        self.alias_nb_pages()
        self.add_page()

    def header(self):
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(17, 24, 39)
        self.cell(0, 7, self.doc_title, new_x="LMARGIN", new_y="NEXT")
        if self.subtitle:
            self.set_font("Helvetica", "", 9)
            self.set_text_color(90, 98, 110)
            self.cell(0, 5, self.subtitle, new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(209, 213, 219)
        self.line(self.l_margin, self.get_y() + 1, self.w - self.r_margin, self.get_y() + 1)
        self.ln(3)
        self.set_text_color(0, 0, 0)

    def footer(self):
        self.set_y(-9)
        self.set_font("Helvetica", "", 7)
        self.set_text_color(120, 120, 120)
        stamp = f"Exporté le {datetime.now():%d/%m/%Y à %H:%M}" + (f" par {self.user}" if self.user else "")
        self.cell(0, 4, text(stamp), align="L")
        self.cell(0, 4, f"Page {self.page_no()}/{{nb}}", align="R")

    def section(self, title: str, note: str = ""):
        if self.get_y() > self.h - 35:
            self.add_page()
        self.ln(2)
        self.set_font("Helvetica", "B", 11)
        self.cell(0, 6, text(title), new_x="LMARGIN", new_y="NEXT")
        if note:
            self.set_font("Helvetica", "", 8)
            self.set_text_color(90, 98, 110)
            self.multi_cell(0, 4, text(note), new_x="LMARGIN", new_y="NEXT")
            self.set_text_color(0, 0, 0)
        self.ln(1)

    def filters(self, items):
        """Ligne(s) « Libellé : valeur » des filtres appliqués."""
        self.set_font("Helvetica", "", 8)
        self.set_text_color(55, 65, 81)
        self.multi_cell(0, 4, text("   ".join(f"{label} : {value}" for label, value in items)),
                        new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.ln(1)

    def kpis(self, items):
        """Cartes d'indicateurs (libellé, valeur, précision) sur une ligne."""
        width = (self.w - self.l_margin - self.r_margin - 2 * (len(items) - 1)) / len(items)
        x, y = self.l_margin, self.get_y()
        for label, value, hint in items:
            self.set_fill_color(243, 244, 246)
            self.rect(x, y, width, 18, style="F")
            self.set_xy(x + 2, y + 1.5)
            self.set_font("Helvetica", "", 7)
            self.set_text_color(90, 98, 110)
            self.cell(width - 4, 4, text(label))
            self.set_xy(x + 2, y + 5.5)
            self.set_font("Helvetica", "B", 13)
            self.set_text_color(17, 24, 39)
            self.cell(width - 4, 7, text(value))
            self.set_xy(x + 2, y + 12.5)
            self.set_font("Helvetica", "", 6.5)
            self.set_text_color(90, 98, 110)
            self.cell(width - 4, 4, text(hint))
            x += width + 2
        self.set_text_color(0, 0, 0)
        self.set_xy(self.l_margin, y + 20)

    def table(self, headers, rows, widths, align=None, fills=None, colors=None, bold_last=False):
        """Tableau paginé (en-tête répété). `fills`/`colors` : {(ligne, colonne): (r, g, b)}."""
        total = self.w - self.l_margin - self.r_margin
        scale = total / sum(widths)
        widths = [w * scale for w in widths]
        align = align or ["L"] * len(headers)
        fills, colors = fills or {}, colors or {}
        line_h = 5

        def head():
            self.set_font("Helvetica", "B", 7)
            self.set_fill_color(229, 231, 235)
            for w, h in zip(widths, headers):
                size = 7
                while size > 5 and self.get_string_width(text(h)) > w - 1:  # en-tête long : police réduite
                    size -= 0.5
                    self.set_font_size(size)
                self.cell(w, 6, text(h), border=1, align="C", fill=True)
                self.set_font_size(7)
            self.ln()

        head()
        for i, row in enumerate(rows):
            if self.get_y() + line_h > self.page_break_trigger:
                self.add_page()
                head()
            last = bold_last and i == len(rows) - 1
            for j, (w, value) in enumerate(zip(widths, row)):
                self.set_font("Helvetica", "B" if last else "", 7)
                fill = fills.get((i, j))
                if fill:
                    self.set_fill_color(*fill)
                elif i % 2:
                    self.set_fill_color(249, 250, 251)
                self.set_text_color(*colors.get((i, j), (0, 0, 0)))
                value = text(value)
                if self.get_string_width(value) > w - 1.5:  # tronqué à la largeur de la colonne
                    while value and self.get_string_width(value + "..") > w - 1.5:
                        value = value[:-1]
                    value += ".."
                self.cell(w, line_h, value, border=1, align=align[j], fill=bool(fill) or bool(i % 2))
            self.ln()
        self.set_text_color(0, 0, 0)

    def output_bytes(self) -> bytes:
        return bytes(self.output())
