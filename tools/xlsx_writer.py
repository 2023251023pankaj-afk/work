"""
A small .xlsx writer with just enough styling for a plan people fill in.

Standard library only, like the rest of the app: coloured headings, column
widths, frozen heading rows, merged title rows, and dropdown lists with a
hint that pops up when a cell is selected. Dropdown values live on a hidden
"Lists" sheet, so a list is never cut short by Excel's 255-character limit on
lists typed straight into a rule.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path

#: Named cell styles -> index into cellXfs in STYLES below.
STYLE = {
    "": 0, "title": 1, "intro": 2, "head": 3, "head_req": 4, "label": 5,
    "req": 6, "hint": 7, "input": 8, "cell": 9, "section": 10, "wrap": 11, "example": 12,
}

STYLES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<fonts count="7">'
    '<font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="15"/><color rgb="FF1F3A5F"/><name val="Calibri"/></font>'
    '<font><i/><sz val="11"/><color rgb="FF4A5563"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><color rgb="FFB42318"/><name val="Calibri"/></font>'
    '<font><sz val="10"/><color rgb="FF5B6573"/><name val="Calibri"/></font>'
    '</fonts>'
    '<fills count="7">'
    '<fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FF2F5D8A"/></patternFill></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFB45309"/></patternFill></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFFFF7D6"/></patternFill></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFF1F3F5"/></patternFill></fill>'
    '<fill><patternFill patternType="solid"><fgColor rgb="FFE3EDF7"/></patternFill></fill>'
    '</fills>'
    '<borders count="2">'
    '<border><left/><right/><top/><bottom/><diagonal/></border>'
    '<border><left style="thin"><color rgb="FFC9CED6"/></left><right style="thin"><color rgb="FFC9CED6"/></right>'
    '<top style="thin"><color rgb="FFC9CED6"/></top><bottom style="thin"><color rgb="FFC9CED6"/></bottom><diagonal/></border>'
    '</borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="13">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1">'
    '<alignment wrapText="1" vertical="top"/></xf>'
    '<xf numFmtId="0" fontId="4" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
    '<alignment wrapText="1" vertical="center"/></xf>'
    '<xf numFmtId="0" fontId="4" fillId="3" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
    '<alignment wrapText="1" vertical="center"/></xf>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1">'
    '<alignment vertical="top"/></xf>'
    '<xf numFmtId="0" fontId="5" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1">'
    '<alignment vertical="top"/></xf>'
    '<xf numFmtId="0" fontId="6" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1">'
    '<alignment wrapText="1" vertical="top"/></xf>'
    '<xf numFmtId="49" fontId="0" fillId="4" borderId="1" xfId="0" applyNumberFormat="1" applyFill="1" applyBorder="1" applyAlignment="1">'
    '<alignment wrapText="1" vertical="top"/></xf>'
    '<xf numFmtId="49" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/>'
    '<xf numFmtId="0" fontId="1" fillId="6" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">'
    '<alignment wrapText="1" vertical="top"/></xf>'
    '<xf numFmtId="49" fontId="0" fillId="5" borderId="1" xfId="0" applyNumberFormat="1" applyFill="1" applyBorder="1"/>'
    '</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>'
)


@dataclass
class Dropdown:
    ref: str                          # "I3:I1000"
    choices: tuple[str, ...] = ()
    title: str = ""                   # hint pop-up title, <= 32 chars
    prompt: str = ""                  # hint pop-up text, <= 255 chars


@dataclass
class SheetSpec:
    name: str
    rows: list[list[str]] = field(default_factory=list)
    #: Style per row, or per cell as {(row, col): style}; both 0-based.
    row_styles: dict[int, str] = field(default_factory=dict)
    cell_styles: dict[tuple[int, int], str] = field(default_factory=dict)
    #: Style for empty cells of the grid below the heading, so blank input
    #: rows still show borders.
    grid: tuple[int, int, int, str] | None = None   # first_row, n_rows, n_cols, style
    widths: list[int] = field(default_factory=list)
    heights: dict[int, int] = field(default_factory=dict)
    merges: list[str] = field(default_factory=list)
    freeze_rows: int = 0
    dropdowns: list[Dropdown] = field(default_factory=list)
    tab_color: str = ""
    hidden: bool = False


def col(i: int) -> str:
    name = ""
    i += 1
    while i:
        i, rem = divmod(i - 1, 26)
        name = chr(65 + rem) + name
    return name


def _esc(v: object) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _sheet_xml(sh: SheetSpec, list_refs: dict[tuple[str, ...], str], selected: bool) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8"?>'
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
    if sh.tab_color:
        out.append(f'<sheetPr><tabColor rgb="FF{sh.tab_color}"/></sheetPr>')
    sel = ' tabSelected="1"' if selected else ""
    view = f'<sheetView workbookViewId="0"{sel}>'
    if sh.freeze_rows:
        view += (f'<pane ySplit="{sh.freeze_rows}" topLeftCell="A{sh.freeze_rows + 1}" '
                 f'activePane="bottomLeft" state="frozen"/>')
    out.append(f"<sheetViews>{view}</sheetView></sheetViews>")
    if sh.widths:
        out.append("<cols>" + "".join(
            f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
            for i, w in enumerate(sh.widths)) + "</cols>")

    n_rows = len(sh.rows)
    if sh.grid:
        n_rows = max(n_rows, sh.grid[0] + sh.grid[1])
    body = []
    for r in range(n_rows):
        values = sh.rows[r] if r < len(sh.rows) else []
        width = len(values)
        if sh.grid and sh.grid[0] <= r < sh.grid[0] + sh.grid[1]:
            width = max(width, sh.grid[2])
        cells = []
        for c in range(width):
            v = values[c] if c < len(values) else ""
            style = sh.cell_styles.get((r, c)) or sh.row_styles.get(r, "")
            if not style and sh.grid and sh.grid[0] <= r < sh.grid[0] + sh.grid[1] and c < sh.grid[2]:
                style = sh.grid[3]
            s_attr = f' s="{STYLE[style]}"' if STYLE.get(style) else ""
            if v == "" and not s_attr:
                continue
            ref = f"{col(c)}{r + 1}"
            if v == "":
                cells.append(f'<c r="{ref}"{s_attr}/>')
            else:
                cells.append(f'<c r="{ref}"{s_attr} t="inlineStr"><is><t xml:space="preserve">'
                             f"{_esc(v)}</t></is></c>")
        ht = f' ht="{sh.heights[r]}" customHeight="1"' if r in sh.heights else ""
        if cells or ht:
            body.append(f'<row r="{r + 1}"{ht}>' + "".join(cells) + "</row>")
    out.append("<sheetData>" + "".join(body) + "</sheetData>")

    if sh.merges:
        out.append(f'<mergeCells count="{len(sh.merges)}">'
                   + "".join(f'<mergeCell ref="{m}"/>' for m in sh.merges) + "</mergeCells>")
    if sh.dropdowns:
        dv = []
        for d in sh.dropdowns:
            # Free typing stays allowed (showErrorMessage="0"): the lists
            # cover the common cases, not every field the system has.
            hint = ""
            if d.prompt:
                hint = (f' showInputMessage="1" promptTitle="{_esc(d.title[:32])}"'
                        f' prompt="{_esc(d.prompt[:255])}"')
            if d.choices:
                dv.append(f'<dataValidation type="list" allowBlank="1" showErrorMessage="0"{hint}'
                          f' sqref="{d.ref}"><formula1>{list_refs[d.choices]}</formula1></dataValidation>')
            elif hint:
                dv.append(f'<dataValidation allowBlank="1"{hint} sqref="{d.ref}"/>')
        out.append(f'<dataValidations count="{len(dv)}">' + "".join(dv) + "</dataValidations>")
    out.append('<pageMargins left="0.5" right="0.5" top="0.6" bottom="0.6" header="0.3" footer="0.3"/>')
    out.append("</worksheet>")
    return "".join(out)


def write_xlsx(path: Path, sheets: list[SheetSpec]) -> None:
    """Write ``sheets`` to ``path`` as one workbook."""
    # Gather every distinct dropdown list onto a hidden sheet.
    lists: list[tuple[str, ...]] = []
    for sh in sheets:
        for d in sh.dropdowns:
            if d.choices and d.choices not in lists:
                lists.append(d.choices)
    list_refs: dict[tuple[str, ...], str] = {}
    all_sheets = list(sheets)
    if lists:
        rows: list[list[str]] = [[""] * len(lists) for _ in range(max(len(l) for l in lists))]
        for i, choices in enumerate(lists):
            for r, v in enumerate(choices):
                rows[r][i] = v
            list_refs[choices] = f"Lists!${col(i)}$1:${col(i)}${len(choices)}"
        all_sheets.append(SheetSpec("Lists", rows, hidden=True))

    types, rels, decls = [], [], []
    parts: dict[str, str] = {}
    first_visible = next(i for i, s in enumerate(all_sheets) if not s.hidden)
    for i, sh in enumerate(all_sheets, start=1):
        types.append(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/'
                     f'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
        rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/'
                    f'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>')
        state = ' state="hidden"' if sh.hidden else ""
        decls.append(f'<sheet name="{_esc(sh.name[:31])}" sheetId="{i}"{state} r:id="rId{i}"/>')
        parts[f"xl/worksheets/sheet{i}.xml"] = _sheet_xml(sh, list_refs, i - 1 == first_visible)
    n = len(all_sheets)
    rels.append(f'<Relationship Id="rId{n + 1}" Type="http://schemas.openxmlformats.org/'
                f'officeDocument/2006/relationships/styles" Target="styles.xml"/>')

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            + "".join(types) + "</Types>")
        z.writestr("_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<bookViews><workbookView activeTab="{first_visible}"/></bookViews><sheets>'
            + "".join(decls) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(rels) + "</Relationships>")
        z.writestr("xl/styles.xml", STYLES)
        for name, body in parts.items():
            z.writestr(name, body)
