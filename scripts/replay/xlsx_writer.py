"""A small .xlsx writer using only the standard library (zipfile + XML text). No openpyxl, no xlsxwriter.

What it supports (what the replay report needs): several sheets, a bold header row with a fill, frozen panes,
an autofilter, column widths, wrapped text, cell fills (PASS green / FAIL red / KNOWN GAP amber / SKIPPED grey),
internal hyperlinks from one sheet to a cell of another, numbers, booleans and text (inline strings, so there is
no shared-string table to keep in step). Text is XML-escaped and characters XML cannot hold are dropped.

    book = Workbook()
    sheet = book.add_sheet("Turns", widths=[8, 40], freeze=(1, 0), autofilter=True)
    sheet.append([Cell("PASS", style=PASS), Cell("go", link=("Turns", 5))], header=False)
    book.save("out.xlsx")

The result opens in Excel, Numbers and LibreOffice (see tests/test_replay_harness.py, which reads a produced file
back with zipfile + ElementTree and checks the structure).
"""

import re
import zipfile
from collections import namedtuple
from xml.sax.saxutils import escape

MAX_CELL_CHARS = 32000
_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")

Style = namedtuple("Style", ["bold", "fill", "wrap", "color", "underline", "size", "halign"])


def style(bold=False, fill=None, wrap=False, color=None, underline=False, size=None, halign=None):
    return Style(bold, fill, wrap, color, underline, size, halign)


DEFAULT = style()
WRAP = style(wrap=True)
HEADER = style(bold=True, fill="1F3A5F", color="FFFFFF", wrap=True)
TITLE = style(bold=True, size=14)
BOLD = style(bold=True)
BOLD_WRAP = style(bold=True, wrap=True)
LINK = style(color="0563C1", underline=True)
PASS = style(fill="C6EFCE", color="006100", wrap=True)
FAIL = style(fill="FFC7CE", color="9C0006", wrap=True)
GAP = style(fill="FFEB9C", color="7F6000", wrap=True)
SKIP = style(fill="D9D9D9", color="404040", wrap=True)
VERDICT_STYLES = {"PASS": PASS, "FAIL": FAIL, "KNOWN GAP": GAP, "SKIPPED": SKIP}


class Cell:
    """A value with an optional style and an optional internal link ((sheet name, row number) of the target)."""

    def __init__(self, value, style=None, link=None):
        self.value, self.style, self.link = value, style, link


def column_letter(index):
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def clean_text(value):
    text = _ILLEGAL.sub("", str(value))
    return text if len(text) <= MAX_CELL_CHARS else text[:MAX_CELL_CHARS - 1] + "…"


class Sheet:
    def __init__(self, name, widths=None, freeze=None, autofilter=False):
        if not name or len(name) > 31 or re.search(r"[\[\]:*?/\\]", name):
            raise ValueError("bad sheet name: {!r}".format(name))
        self.name = name
        self.widths = list(widths or [])
        self.freeze = freeze                  # (rows, cols) kept in view, e.g. (1, 0) for a header row
        self.autofilter = autofilter
        self.rows = []                        # list of (list of Cell, is_header)

    def append(self, values, header=False, default_style=None):
        cells = []
        for value in values:
            cell = value if isinstance(value, Cell) else Cell(value)
            if cell.style is None:
                cell.style = HEADER if header else (default_style or DEFAULT)
            cells.append(cell)
        self.rows.append((cells, header))
        return len(self.rows)                 # the 1-based number of the row just added

    def next_row(self):
        return len(self.rows) + 1

    def width(self):
        return max((len(cells) for cells, _ in self.rows), default=1)


class Workbook:
    def __init__(self):
        self.sheets = []
        self._styles = [DEFAULT]              # index 0 is the default style

    def add_sheet(self, name, **kwargs):
        if any(s.name == name for s in self.sheets):
            raise ValueError("duplicate sheet name: {}".format(name))
        sheet = Sheet(name, **kwargs)
        self.sheets.append(sheet)
        return sheet

    def _style_index(self, style):
        if style not in self._styles:
            self._styles.append(style)
        return self._styles.index(style)

    # -- XML parts --------------------------------------------------------------------------

    def _styles_xml(self):
        fonts, fills, xfs = [], ['<fill><patternFill patternType="none"/></fill>', '<fill><patternFill patternType="gray125"/></fill>'], []
        font_index, fill_index = {}, {}

        def font_of(s):
            key = (s.bold, s.color, s.underline, s.size)
            if key not in font_index:
                font_index[key] = len(fonts)
                parts = ""
                if s.bold:
                    parts += "<b/>"
                if s.underline:
                    parts += "<u/>"
                parts += '<sz val="{}"/>'.format(s.size or 11)
                parts += '<color rgb="FF{}"/>'.format(s.color) if s.color else '<color theme="1"/>'
                parts += '<name val="Calibri"/><family val="2"/>'
                fonts.append("<font>{}</font>".format(parts))
            return font_index[key]

        def fill_of(s):
            if not s.fill:
                return 0
            if s.fill not in fill_index:
                fill_index[s.fill] = len(fills)
                fills.append('<fill><patternFill patternType="solid"><fgColor rgb="FF{0}"/><bgColor indexed="64"/></patternFill></fill>'.format(s.fill))
            return fill_index[s.fill]

        for s in self._styles:
            font, fill = font_of(s), fill_of(s)
            align = '<alignment vertical="top"{}{}/>'.format(' wrapText="1"' if s.wrap else "",
                                                             ' horizontal="{}"'.format(s.halign) if s.halign else "")
            xfs.append('<xf numFmtId="0" fontId="{}" fillId="{}" borderId="0" xfId="0" applyFont="1" applyFill="{}" '
                       'applyAlignment="1">{}</xf>'.format(font, fill, 1 if fill else 0, align))
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<fonts count="{}">{}</fonts><fills count="{}">{}</fills>'
                '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                '<cellXfs count="{}">{}</cellXfs>'
                '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
                '</styleSheet>').format(len(fonts), "".join(fonts), len(fills), "".join(fills), len(xfs), "".join(xfs))

    def _sheet_xml(self, sheet):
        out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
               '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
               'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">']
        columns = sheet.width()
        last = "{}{}".format(column_letter(columns - 1), max(len(sheet.rows), 1))
        out.append('<dimension ref="A1:{}"/>'.format(last))
        view = '<sheetViews><sheetView workbookViewId="0"{}>'.format(' tabSelected="1"' if sheet is self.sheets[0] else "")
        if sheet.freeze and (sheet.freeze[0] or sheet.freeze[1]):
            rows, cols = sheet.freeze
            top_left = "{}{}".format(column_letter(cols), rows + 1)
            pane = "bottomRight" if rows and cols else ("bottomLeft" if rows else "topRight")
            view += '<pane{}{} topLeftCell="{}" activePane="{}" state="frozen"/>'.format(
                ' xSplit="{}"'.format(cols) if cols else "", ' ySplit="{}"'.format(rows) if rows else "", top_left, pane)
            view += '<selection pane="{}" activeCell="{}" sqref="{}"/>'.format(pane, top_left, top_left)
        out.append(view + "</sheetView></sheetViews>")
        out.append('<sheetFormatPr defaultRowHeight="15"/>')
        if sheet.widths:
            out.append("<cols>{}</cols>".format("".join(
                '<col min="{0}" max="{0}" width="{1}" customWidth="1"/>'.format(i + 1, w) for i, w in enumerate(sheet.widths))))
        out.append("<sheetData>")
        links = []
        for row_number, (cells, _) in enumerate(sheet.rows, 1):
            out.append('<row r="{}">'.format(row_number))
            for col, cell in enumerate(cells):
                ref = "{}{}".format(column_letter(col), row_number)
                style_index = self._style_index(cell.style)
                value = cell.value
                if cell.link:
                    links.append((ref, cell.link, cell.value))
                if value is None or value == "":
                    out.append('<c r="{}" s="{}"/>'.format(ref, style_index))
                elif isinstance(value, bool):
                    out.append('<c r="{}" s="{}" t="b"><v>{}</v></c>'.format(ref, style_index, 1 if value else 0))
                elif isinstance(value, (int, float)):
                    out.append('<c r="{}" s="{}"><v>{}</v></c>'.format(ref, style_index, repr(value)))
                else:
                    out.append('<c r="{}" s="{}" t="inlineStr"><is><t xml:space="preserve">{}</t></is></c>'.format(
                        ref, style_index, escape(clean_text(value))))
            out.append("</row>")
        out.append("</sheetData>")
        if sheet.autofilter and sheet.rows:
            out.append('<autoFilter ref="A1:{}"/>'.format(last))
        if links:
            out.append("<hyperlinks>")
            for ref, (target_sheet, target_row), shown in links:
                out.append('<hyperlink ref="{}" location="{}" display="{}"/>'.format(
                    ref, escape("'{}'!A{}".format(target_sheet.replace("'", "''"), target_row), {'"': "&quot;"}),
                    escape(clean_text(shown), {'"': "&quot;"})))
            out.append("</hyperlinks>")
        out.append("</worksheet>")
        return "".join(out)

    def _workbook_xml(self):
        sheets = "".join('<sheet name="{}" sheetId="{}" r:id="rId{}"/>'.format(escape(s.name, {'"': "&quot;"}), i, i)
                         for i, s in enumerate(self.sheets, 1))
        names = "".join(
            '<definedName name="_xlnm._FilterDatabase" localSheetId="{}" hidden="1">\'{}\'!$A$1:${}${}</definedName>'.format(
                i, escape(s.name.replace("'", "''")), column_letter(s.width() - 1), max(len(s.rows), 1))
            for i, s in enumerate(self.sheets) if s.autofilter and s.rows)
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<bookViews><workbookView xWindow="0" yWindow="0" windowWidth="28800" windowHeight="16000"/></bookViews>'
                '<sheets>{}</sheets>{}</workbook>').format(sheets, "<definedNames>{}</definedNames>".format(names) if names else "")

    def save(self, path):
        if not self.sheets:
            raise ValueError("a workbook needs at least one sheet")
        sheet_parts = [self._sheet_xml(s) for s in self.sheets]       # builds the style table as a side effect
        n = len(self.sheets)
        content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                         '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                         '<Default Extension="xml" ContentType="application/xml"/>'
                         '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                         + "".join('<Override PartName="/xl/worksheets/sheet{}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'.format(i) for i in range(1, n + 1))
                         + '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                         '</Types>')
        rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '</Relationships>')
        book_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                     '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                     + "".join('<Relationship Id="rId{0}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{0}.xml"/>'.format(i) for i in range(1, n + 1))
                     + '<Relationship Id="rId{}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'.format(n + 1)
                     + '</Relationships>')
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", content_types)
            z.writestr("_rels/.rels", rels)
            z.writestr("xl/workbook.xml", self._workbook_xml())
            z.writestr("xl/_rels/workbook.xml.rels", book_rels)
            z.writestr("xl/styles.xml", self._styles_xml())
            for i, part in enumerate(sheet_parts, 1):
                z.writestr("xl/worksheets/sheet{}.xml".format(i), part)
        return path
