"""
Universal tabular reader.

Turns *any* supported upload into a common intermediate shape: a list of
``Sheet`` objects, each a rectangular grid of trimmed strings. Everything
downstream (plan parser, audit parser) only ever sees ``Sheet`` objects, so
adding a new input format means adding a loader here and nothing else.

Deliberately standard-library only: .xlsx is read straight out of its zip
container with ElementTree, so there is no pandas/openpyxl dependency.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser

# ---------------------------------------------------------------------------
# Sheet
# ---------------------------------------------------------------------------


@dataclass
class Sheet:
    """A rectangular grid of strings, plus where it came from."""

    name: str
    rows: list[list[str]] = field(default_factory=list)
    #: Cached width. Detection scans a sheet many times over, and recomputing
    #: the widest row on every access made parsing a few-thousand-row sheet
    #: quadratic — 20 seconds on a real plan, nearly all of it spent here.
    _n_cols: int | None = field(default=None, repr=False, compare=False)

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        if self._n_cols is None:
            self._n_cols = max((len(r) for r in self.rows), default=0)
        return self._n_cols

    def invalidate(self) -> None:
        """Forget the cached width after ``rows`` is changed in place."""
        self._n_cols = None

    def cell(self, r: int, c: int) -> str:
        if 0 <= r < len(self.rows) and 0 <= c < len(self.rows[r]):
            return self.rows[r][c]
        return ""

    def row(self, r: int) -> list[str]:
        return self.rows[r] if 0 <= r < len(self.rows) else []

    def non_empty_rows(self) -> list[tuple[int, list[str]]]:
        return [(i, r) for i, r in enumerate(self.rows) if any(c.strip() for c in r)]

    def normalized(self) -> Sheet:
        """Pad every row to the same width."""
        w = self.n_cols
        return Sheet(self.name, [list(r) + [""] * (w - len(r)) for r in self.rows])


@dataclass
class Workbook:
    """The result of reading one uploaded file."""

    filename: str
    fmt: str
    sheets: list[Sheet] = field(default_factory=list)
    encoding: str | None = None
    notes: list[str] = field(default_factory=list)

    def sheet_named(self, *needles: str) -> Sheet | None:
        """First sheet whose name contains any of ``needles`` (case-insensitive)."""
        for needle in needles:
            n = needle.lower()
            for s in self.sheets:
                if n in s.name.lower():
                    return s
        return None


class ReadError(Exception):
    """Raised when a file cannot be turned into sheets."""


# ---------------------------------------------------------------------------
# Text decoding: encoding sniffing + mojibake repair, no chardet needed
# ---------------------------------------------------------------------------

_BOMS = [
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
]

# Candidates tried in order of decreasing likelihood for this kind of export.
_CANDIDATES = ["utf-8", "cp1252", "cp1250", "iso-8859-15", "latin-1"]

# Sequences that betray UTF-8 bytes that were decoded as cp1252/latin-1.
_MOJIBAKE = ("Ã©", "Ã¨", "Ã¡", "Ã­", "Ã³", "Ãº", "Ã¼", "Ã±", "Ã§", "â€™",
             "â€œ", "â€\x9d", "â€“", "â€”", "Â\xa0", "Â°", "Ã\x89")


def _score(text: str) -> int:
    """Lower is better. Penalises replacement chars, stray controls, mojibake."""
    s = text.count("�") * 100
    s += sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\r\n") * 40
    s += sum(text.count(m) for m in _MOJIBAKE) * 25
    return s


def repair_mojibake(text: str) -> str:
    """Undo a single UTF-8-read-as-cp1252 round trip, when that clearly helps."""
    if not any(m in text for m in _MOJIBAKE):
        return text
    for enc in ("cp1252", "latin-1"):
        try:
            fixed = text.encode(enc, errors="strict").decode("utf-8", errors="strict")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if _score(fixed) < _score(text):
            return fixed
    return text


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """Decode ``raw`` to text, returning ``(text, encoding_label)``."""
    for bom, enc in _BOMS:
        if raw.startswith(bom):
            return raw.decode(enc, errors="replace"), enc

    # UTF-16 without a BOM shows up as a dense field of NUL bytes.
    head = raw[:4096]
    if head.count(b"\x00") > len(head) * 0.25 and len(head) > 8:
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                return raw.decode(enc, errors="strict"), enc
            except UnicodeDecodeError:
                pass

    best: tuple[int, str, str] | None = None
    for enc in _CANDIDATES:
        try:
            text = raw.decode(enc, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
        text = repair_mojibake(text)
        sc = _score(text)
        if best is None or sc < best[0]:
            best = (sc, text, enc)
        if sc == 0:
            break
    if best is None:
        return raw.decode("utf-8", errors="replace"), "utf-8 (lossy)"
    return best[1], best[2]


def clean(value: object) -> str:
    """Normalise one cell: collapse odd whitespace, strip, keep the text."""
    if value is None:
        return ""
    s = str(value)
    # Non-breaking / zero-width characters are rife in copy-pasted plans.
    s = s.replace("\xa0", " ").replace("​", "").replace("﻿", "")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


# ---------------------------------------------------------------------------
# XLSX (stdlib: zipfile + ElementTree)
# ---------------------------------------------------------------------------

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NSR = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# Built-in numFmt ids that mean "this is a date/time".
_DATE_BUILTINS = set(range(14, 23)) | set(range(45, 48)) | {27, 30, 36, 50, 57}
_EXCEL_EPOCH = _dt.datetime(1899, 12, 30)


def _col_index(ref: str) -> int:
    """``'BC12'`` -> 54 (zero-based column index)."""
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return max(n - 1, 0)


def _xlsx_shared_strings(z: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    except (KeyError, ET.ParseError):
        return []
    out = []
    for si in root.findall(_NS + "si"):
        # Rich text splits a single string across many <t> runs.
        out.append("".join(t.text or "" for t in si.iter(_NS + "t")))
    return out


def _xlsx_date_styles(z: zipfile.ZipFile) -> set[int]:
    """Indices into cellXfs whose number format renders as a date."""
    try:
        root = ET.fromstring(z.read("xl/styles.xml"))
    except (KeyError, ET.ParseError):
        return set()
    custom_date_ids = set()
    for nf in root.iter(_NS + "numFmt"):
        code = (nf.get("formatCode") or "").lower()
        stripped = re.sub(r"\[[^\]]*\]|\"[^\"]*\"", "", code)
        if re.search(r"[dmyh]", stripped) and "0.00" not in stripped:
            try:
                custom_date_ids.add(int(nf.get("numFmtId")))
            except (TypeError, ValueError):
                pass
    date_styles = set()
    cell_xfs = root.find(_NS + "cellXfs")
    if cell_xfs is not None:
        for i, xf in enumerate(cell_xfs.findall(_NS + "xf")):
            try:
                fmt_id = int(xf.get("numFmtId") or 0)
            except ValueError:
                continue
            if fmt_id in _DATE_BUILTINS or fmt_id in custom_date_ids:
                date_styles.add(i)
    return date_styles


def _serial_to_text(num: float) -> str:
    try:
        dt = _EXCEL_EPOCH + _dt.timedelta(days=float(num))
    except (OverflowError, ValueError):
        return str(num)
    if dt.hour or dt.minute or dt.second:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return dt.strftime("%Y-%m-%d")


def _num_text(raw: str) -> str:
    """Render a numeric cell without a pointless trailing ``.0``."""
    try:
        f = float(raw)
    except ValueError:
        return raw
    if f.is_integer() and abs(f) < 1e15:
        return str(int(f))
    return repr(round(f, 10)).rstrip("0").rstrip(".") if "." in raw else raw


def read_xlsx(raw: bytes, filename: str) -> Workbook:
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ReadError(
            "This does not look like a valid .xlsx file. If it is an old .xls "
            "workbook, open it in Excel and re-save as .xlsx or .csv."
        ) from exc

    wb = Workbook(filename=filename, fmt="xlsx")
    shared = _xlsx_shared_strings(z)
    date_styles = _xlsx_date_styles(z)

    try:
        wb_root = ET.fromstring(z.read("xl/workbook.xml"))
        rels = {
            r.get("Id"): r.get("Target")
            for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        }
        sheets_el = wb_root.find(_NS + "sheets")
        entries = [
            (sh.get("name") or f"Sheet{i + 1}", rels.get(sh.get(_NSR + "id")))
            for i, sh in enumerate(sheets_el if sheets_el is not None else [])
        ]
    except (KeyError, ET.ParseError):
        # No workbook part: fall back to whatever sheet parts exist.
        entries = [
            (n.rsplit("/", 1)[-1].removesuffix(".xml"), n.removeprefix("xl/"))
            for n in sorted(z.namelist())
            if n.startswith("xl/worksheets/sheet")
        ]

    for name, target in entries:
        if not target:
            continue
        path = target.lstrip("/")
        if not path.startswith("xl/"):
            path = "xl/" + path
        try:
            ws = ET.fromstring(z.read(path))
        except (KeyError, ET.ParseError):
            wb.notes.append(f"Sheet {name!r} could not be read and was skipped.")
            continue
        wb.sheets.append(_xlsx_sheet(ws, name, shared, date_styles))

    if not wb.sheets:
        raise ReadError("The workbook contains no readable sheets.")
    return wb


def _xlsx_sheet(ws, name, shared, date_styles) -> Sheet:
    grid: dict[int, dict[int, str]] = {}
    for row_el in ws.iter(_NS + "row"):
        try:
            r = int(row_el.get("r")) - 1
        except (TypeError, ValueError):
            r = len(grid)
        cells: dict[int, str] = {}
        for c_el in row_el.findall(_NS + "c"):
            ref = c_el.get("r") or ""
            ci = _col_index(ref)
            cells[ci] = _xlsx_cell_text(c_el, shared, date_styles)
        grid[r] = cells

    merges = _xlsx_merges(ws)
    if not grid:
        return Sheet(name, [])

    max_r = max(grid)
    max_c = max((max(cs) for cs in grid.values() if cs), default=0)
    rows = [
        [clean(grid.get(r, {}).get(c, "")) for c in range(max_c + 1)]
        for r in range(max_r + 1)
    ]
    _apply_merges(rows, merges)
    return Sheet(name, rows)


def _xlsx_cell_text(c_el, shared, date_styles) -> str:
    t = c_el.get("t")
    if t == "inlineStr":
        is_el = c_el.find(_NS + "is")
        return "".join(x.text or "" for x in is_el.iter(_NS + "t")) if is_el is not None else ""
    v_el = c_el.find(_NS + "v")
    if v_el is None or v_el.text is None:
        return ""
    v = v_el.text
    if t == "s":  # shared string
        try:
            return shared[int(v)]
        except (ValueError, IndexError):
            return v
    if t == "b":
        return "TRUE" if v.strip() in ("1", "true", "TRUE") else "FALSE"
    if t in ("str", "e"):
        return v
    # Numeric: a date style makes it a date, otherwise a plain number.
    try:
        s_idx = int(c_el.get("s") or -1)
    except ValueError:
        s_idx = -1
    if s_idx in date_styles:
        try:
            if float(v) > 0:
                return _serial_to_text(float(v))
        except ValueError:
            pass
    return _num_text(v)


def _xlsx_merges(ws) -> list[tuple[int, int, int, int]]:
    out = []
    for mc in ws.iter(_NS + "mergeCell"):
        ref = mc.get("ref") or ""
        if ":" not in ref:
            continue
        a, b = ref.split(":", 1)
        try:
            r1 = int(re.sub(r"[^0-9]", "", a)) - 1
            r2 = int(re.sub(r"[^0-9]", "", b)) - 1
        except ValueError:
            continue
        out.append((r1, _col_index(a), r2, _col_index(b)))
    return out


def _apply_merges(rows: list[list[str]], merges) -> None:
    """Copy a merged region's anchor value into every cell it spans.

    Plans lean on merged header cells constantly; unmerging makes header
    detection work without special-casing.
    """
    for r1, c1, r2, c2 in merges:
        if not (0 <= r1 < len(rows) and 0 <= c1 < len(rows[r1])):
            continue
        val = rows[r1][c1]
        if not val:
            continue
        for r in range(r1, min(r2, len(rows) - 1) + 1):
            for c in range(c1, min(c2, len(rows[r]) - 1) + 1):
                if not rows[r][c]:
                    rows[r][c] = val


# ---------------------------------------------------------------------------
# Delimited text
# ---------------------------------------------------------------------------


def _sniff_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:60])
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        pass
    # Fall back to whichever candidate is most consistent across lines.
    lines = [ln for ln in text.splitlines()[:60] if ln.strip()]
    best, best_score = ",", -1.0
    for d in (",", "\t", ";", "|"):
        counts = [ln.count(d) for ln in lines]
        if not counts or max(counts) == 0:
            continue
        mode = max(set(counts), key=counts.count)
        if mode == 0:
            continue
        score = counts.count(mode) / len(counts) * mode
        if score > best_score:
            best, best_score = d, score
    return best


def read_delimited(raw: bytes, filename: str) -> Workbook:
    text, enc = decode_bytes(raw)
    delim = _sniff_delimiter(text)
    rows = [
        [clean(c) for c in r]
        for r in csv.reader(io.StringIO(text), delimiter=delim)
    ]
    label = {",": "comma", "\t": "tab", ";": "semicolon", "|": "pipe"}.get(delim, delim)
    wb = Workbook(filename=filename, fmt="delimited", encoding=enc, sheets=[Sheet("Sheet1", rows)])
    wb.notes.append(f"Decoded as {enc}, {label}-delimited.")
    return wb


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def _records_to_rows(records: list[dict]) -> list[list[str]]:
    keys: list[str] = []
    for rec in records:
        for k in rec:
            if k not in keys:
                keys.append(k)
    rows = [keys]
    for rec in records:
        rows.append([clean(rec.get(k, "")) for k in keys])
    return rows


def read_json(raw: bytes, filename: str) -> Workbook:
    text, enc = decode_bytes(raw)
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReadError(f"Invalid JSON: {exc}") from exc

    wb = Workbook(filename=filename, fmt="json", encoding=enc)

    def add(name: str, value: object) -> None:
        if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
            wb.sheets.append(Sheet(name, _records_to_rows(value)))
        elif isinstance(value, list) and value and all(isinstance(x, list) for x in value):
            wb.sheets.append(Sheet(name, [[clean(c) for c in r] for r in value]))
        elif isinstance(value, dict):
            wb.sheets.append(Sheet(name, [["key", "value"]] + [[clean(k), clean(v)] for k, v in value.items()]))

    if isinstance(doc, dict):
        # A dict of tables -> one sheet per key; otherwise a single key/value sheet.
        tabular = {k: v for k, v in doc.items() if isinstance(v, (list, dict))}
        if tabular and all(isinstance(v, list) for v in tabular.values()):
            for k, v in tabular.items():
                add(str(k), v)
        else:
            add("Sheet1", doc)
    else:
        add("Sheet1", doc)

    if not wb.sheets:
        raise ReadError("The JSON contains no table-shaped data.")
    return wb


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._spans: list[int] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
            self._spans = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            try:
                self._spans.append(max(1, int(d.get("colspan", "1"))))
            except ValueError:
                self._spans.append(1)
        elif tag == "br" and self._cell is not None:
            self._cell.append("\n")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            text = clean("".join(self._cell))
            span = self._spans[-1] if self._spans else 1
            self._row.extend([text] + [""] * (span - 1))
            self._cell = None
        elif tag == "tr" and self._table is not None and self._row is not None:
            self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def read_html(raw: bytes, filename: str) -> Workbook:
    text, enc = decode_bytes(raw)
    p = _TableParser()
    p.feed(text)
    if not p.tables:
        raise ReadError("No <table> elements found in the HTML.")
    wb = Workbook(filename=filename, fmt="html", encoding=enc)
    for i, t in enumerate(p.tables):
        wb.sheets.append(Sheet(f"Table{i + 1}", t))
    return wb


# ---------------------------------------------------------------------------
# Markdown / plain text
# ---------------------------------------------------------------------------

_MD_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")


def read_markdown(raw: bytes, filename: str) -> Workbook:
    text, enc = decode_bytes(raw)
    lines = text.splitlines()
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for ln in lines:
        if ln.count("|") >= 2:
            if _MD_SEP.match(ln):
                continue
            cells = [clean(c) for c in ln.strip().strip("|").split("|")]
            current.append(cells)
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    if not tables:
        return read_delimited(raw, filename)
    wb = Workbook(filename=filename, fmt="markdown", encoding=enc)
    for i, t in enumerate(tables):
        wb.sheets.append(Sheet(f"Table{i + 1}", t))
    return wb


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_ZIP_MAGIC = b"PK\x03\x04"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0"


def read_any(raw: bytes, filename: str) -> Workbook:
    """Read ``raw`` into a :class:`Workbook`, choosing the loader by sniffing.

    Content sniffing wins over the file extension, because plans are routinely
    passed around with the wrong suffix.
    """
    if not raw:
        raise ReadError("The uploaded file is empty.")

    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""

    if raw.startswith(_ZIP_MAGIC):
        names = _zip_names(raw)
        if any(n.startswith("xl/") for n in names):
            return read_xlsx(raw, filename)
        if any(n.startswith("content.xml") for n in names):
            raise ReadError(
                "OpenDocument (.ods) is not supported. Re-save as .xlsx or .csv."
            )
        raise ReadError("Zip archive that is not a spreadsheet.")

    if raw.startswith(_OLE_MAGIC) or ext == ".xls":
        raise ReadError(
            "Legacy .xls (Excel 97-2003) is not supported. Open it in Excel and "
            "re-save as .xlsx or .csv, then upload again."
        )

    head = raw[:2048].lstrip()
    if ext == ".json" or head[:1] in (b"{", b"["):
        try:
            return read_json(raw, filename)
        except ReadError:
            if ext == ".json":
                raise

    low = head.lower()
    if ext in (".html", ".htm") or b"<table" in low or low.startswith((b"<!doctype", b"<html")):
        try:
            return read_html(raw, filename)
        except ReadError:
            if ext in (".html", ".htm"):
                raise

    if ext in (".md", ".markdown"):
        return read_markdown(raw, filename)

    if ext == ".xml":
        raise ReadError("Generic .xml is not supported. Export as .csv or .xlsx.")

    wb = read_delimited(raw, filename)
    # A single fat column usually means a Markdown/pipe table sneaked through.
    if wb.sheets and wb.sheets[0].n_cols <= 1:
        try:
            md = read_markdown(raw, filename)
            if md.sheets and md.sheets[0].n_cols > 1:
                return md
        except ReadError:
            pass
    return wb


def _zip_names(raw: bytes) -> list[str]:
    try:
        return zipfile.ZipFile(io.BytesIO(raw)).namelist()
    except zipfile.BadZipFile:
        return []


SUPPORTED_SUFFIXES = (
    ".xlsx", ".xlsm", ".csv", ".tsv", ".txt", ".json", ".html", ".htm", ".md", ".markdown",
)
