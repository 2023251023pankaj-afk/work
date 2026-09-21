"""
Audit-log parser.

The audit report's column set is fixed, but its *content* changes with every
export, and the interesting information is packed inside two free-text columns:

    Field        "Button(53)(Button(53))" / "Image Name(76-French)"
    Description  "Current Settings: Screen Kiosk 6 Left Hand Navigation of
                  screen set Fairway - Portfolio A has been updated."

So the work here is decomposition: turn each row into an addressable
:class:`AuditEntry` carrying (screen set, screen, property, button, language)
plus a classified change, which the validator can then reconcile against plan
targets. Columns are still located by header name rather than position, so a
reordered or re-exported report keeps working.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field

from .readers import ReadError, Sheet, Workbook, read_any

# ---------------------------------------------------------------------------
# Column model
# ---------------------------------------------------------------------------

#: Canonical column -> accepted header spellings.
COLUMNS: dict[str, list[str]] = {
    "id": ["id", "audit id", "transaction id"],
    "operation": ["operation"],
    "activity": ["activity", "action"],
    "level": ["level"],
    "level_details": ["level details", "level detail"],
    "date": ["date", "date time", "timestamp"],
    "user": ["user", "changed by", "modified by"],
    "description": ["description"],
    "field": ["field", "field name"],
    "old": ["old setting", "old value", "from"],
    "new": ["new setting", "new value", "to"],
    "effective_from": ["effective from", "effective date"],
    "package": ["package generated or not", "package generated", "package"],
}

#: Without these an audit report cannot be validated at all.
REQUIRED = ("field", "old", "new")

# Change classifications.
DISABLED = "disabled"
ENABLED = "enabled"
CLEARED = "cleared"
SET = "set"
CHANGED = "changed"
UNCHANGED = "unchanged"


class AuditParseError(Exception):
    """Raised when a file cannot be understood as an audit log."""


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------


@dataclass
class AuditEntry:
    """One decomposed audit-log row."""

    row_no: int                  # 1-based row in the source file, for traceability
    raw: dict[str, str] = field(default_factory=dict)

    # Straight from the columns
    audit_id: str = ""
    operation: str = ""
    activity: str = ""
    level: str = ""
    level_details: str = ""
    date_text: str = ""
    user: str = ""
    description: str = ""
    field_text: str = ""
    old: str = ""
    new: str = ""
    effective_from: str = ""
    package: str = ""

    # Decomposed
    screen_set: str = ""         # from Description
    screen: str = ""             # from Description
    target_kind: str = ""        # what was changed: screen set, menu item, user…
    target_name: str = ""        # which one
    prop: str = ""               # "Button" / "Caption" / "Image Name"
    button: str = ""             # "53"
    language: str = ""           # "French" (blank for non-localised properties)
    timestamp: _dt.datetime | None = None
    change: str = CHANGED

    @property
    def is_button_toggle(self) -> bool:
        return self.prop.lower() == "button" and not self.language

    @property
    def where(self) -> str:
        """Where this change landed, in one line.

        The Description column is the field that answers "where", so it gets a
        first-class rendering rather than being left as a sentence to re-read.
        """
        if self.screen_set:
            return f"{self.screen_set} · {self.screen}" if self.screen else self.screen_set
        if self.target_name:
            return f"{self.target_kind} {self.target_name}".strip()
        return self.target_kind or ""

    @property
    def label(self) -> str:
        """Human-readable identity of what changed."""
        bits = self.prop or self.field_text
        if self.button:
            bits += f" {self.button}"
        if self.language:
            bits += f" ({self.language})"
        return bits

    #: Longest each side of a change is allowed to be when shown to a person.
    #: The untouched values stay on ``old``/``new`` for exports; this is the
    #: display form, and it is interpolated into finding messages, so an
    #: unbounded value here would put tens of thousands of characters into a
    #: sentence and into every page that renders it.
    DISPLAY_LIMIT = 120

    @staticmethod
    def _shorten(value: str) -> str:
        value = value or "(empty)"
        limit = AuditEntry.DISPLAY_LIMIT
        return value if len(value) <= limit else value[:limit] + "…"

    @property
    def change_text(self) -> str:
        return f"{self._shorten(self.old)} → {self._shorten(self.new)}"


@dataclass
class AuditLog:
    """A parsed audit report: header metadata plus decomposed entries."""

    filename: str = ""
    fmt: str = ""
    encoding: str | None = None
    report_effective_date: str = ""
    generated_on: str = ""
    generated_by: str = ""
    entries: list[AuditEntry] = field(default_factory=list)
    columns_found: dict[str, int] = field(default_factory=dict)
    missing_columns: list[str] = field(default_factory=list)
    skipped_rows: int = 0
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # -- views ------------------------------------------------------------
    @property
    def screen_sets(self) -> list[str]:
        seen: list[str] = []
        for e in self.entries:
            if e.screen_set and e.screen_set not in seen:
                seen.append(e.screen_set)
        return seen

    @property
    def users(self) -> list[str]:
        return sorted({e.user for e in self.entries if e.user})

    @property
    def audit_ids(self) -> list[str]:
        return sorted({e.audit_id for e in self.entries if e.audit_id})

    @property
    def operations(self) -> list[str]:
        return sorted({e.operation for e in self.entries if e.operation})

    def entries_for_set(self, screen_set: str) -> list[AuditEntry]:
        from .plan_schema import norm_entity
        want = norm_entity(screen_set)
        return [e for e in self.entries if norm_entity(e.screen_set) == want]

    def timespan(self) -> tuple[_dt.datetime, _dt.datetime] | None:
        stamps = [e.timestamp for e in self.entries if e.timestamp]
        return (min(stamps), max(stamps)) if stamps else None


# ---------------------------------------------------------------------------
# Field decomposition
# ---------------------------------------------------------------------------

#: "Button(53)(Button(53))", "Image Name(76-French)", "Caption(30-SpanishUS)".
#: Group 1 = property, 2 = index, 3 = optional language/qualifier.
_FIELD_RE = re.compile(
    r"^\s*(?P<prop>[A-Za-z][A-Za-z0-9 _/&.-]*?)\s*"
    r"\(\s*(?P<idx>\d{1,4})\s*(?:[-–—:]\s*(?P<lang>[^)]*?))?\s*\)",
    re.UNICODE,
)

#: A property with a language but no button: "Caption(French)".
_FIELD_LANG_ONLY_RE = re.compile(
    r"^\s*(?P<prop>[A-Za-z][A-Za-z0-9 _/&.-]*?)\s*\(\s*(?P<lang>[A-Za-z][A-Za-z ]*?)\s*\)\s*$",
    re.UNICODE,
)


def parse_field(text: str) -> tuple[str, str, str]:
    """Split a ``Field`` cell into ``(property, button, language)``.

    Unparseable text is returned as the property with empty button/language, so
    an unrecognised field is still reported rather than dropped.
    """
    t = (text or "").strip()
    if not t:
        return "", "", ""
    m = _FIELD_RE.match(t)
    if m:
        prop = " ".join(m.group("prop").split())
        lang = (m.group("lang") or "").strip()
        return prop, m.group("idx"), lang
    m = _FIELD_LANG_ONLY_RE.match(t)
    if m:
        return " ".join(m.group("prop").split()), "", m.group("lang").strip()
    # Strip any trailing parenthetical and keep what is left as the property.
    base = re.sub(r"\s*\(.*\)\s*$", "", t).strip()
    return (base or t), "", ""


#: "Screen Kiosk 6 Left Hand Navigation of screen set Fairway - Portfolio A
#:  has been updated."
_DESC_FULL_RE = re.compile(
    r"screen\s+(?P<screen>.+?)\s+of\s+screen\s*set\s+(?P<set>.+?)"
    r"(?:\s+has\s+been\b|\s*[.;]|\s*$)",
    re.I,
)
_DESC_SET_RE = re.compile(
    r"screen\s*set\s+(?P<set>.+?)(?:\s+has\s+been\b|\s*[.;]|\s*$)", re.I
)


# ---------------------------------------------------------------------------
# Where the change happened
# ---------------------------------------------------------------------------
#
# The Description column is the one that says *where* a change landed, and it
# does so for several different kinds of target — not only screen sets. Reading
# only the screen-set wording left one row in eight with no location at all.

KIND_SCREEN_SET = "Screen set"
KIND_MENU_ITEM = "Menu item"
KIND_RESTAURANT = "Restaurant"
KIND_PARAMETER_SET = "Parameter set"
KIND_USER = "User"
KIND_MEDIA = "Media asset"
KIND_PACKAGE = "Package schedule"

#: (pattern, kind, how to build the name from the match). Ordered: the first
#: hit wins, so the more specific wording comes first.
_TARGET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bstatus for menu item\s+(?P<id>\d+)\s+at\s+restaurant\s+"
                r"(?P<rest>[^.]+?)\s+has\s+been\b", re.I), KIND_MENU_ITEM),
    (re.compile(r"\bfor\s+menu\s+item\s+(?P<id>\d+)", re.I), KIND_MENU_ITEM),
    (re.compile(r"\brestaurant\s+profile\s+(?P<name>.+?)\s*(?:has\s+been|$)", re.I),
     KIND_RESTAURANT),
    (re.compile(r"\bcustom\s+parameter\s+set\s+(?P<name>.+?)\s+has\s+been\b", re.I),
     KIND_PARAMETER_SET),
    (re.compile(r"\buser\s+(?P<name>\S+)\s+has\s+been\b", re.I), KIND_USER),
    (re.compile(r"\bmedia\s+asset\s+(?P<name>.+?)\s+file\s+has\s+been\b", re.I),
     KIND_MEDIA),
    (re.compile(r"\bpackage\s+schedule\b.*?(?:of\s+type\s+(?P<name>[^.]+))?", re.I),
     KIND_PACKAGE),
]


def parse_target(description: str, operation: str = "") -> tuple[str, str]:
    """``(kind, name)`` — what the change was made to, and which one.

    A screen set is only one of the things an audit log records against. The
    same column also names menu items, restaurants, parameter sets, users,
    media assets and package schedules, and each is somebody's "where".
    """
    text = (description or "").strip()
    if not text:
        return ("", "")

    screen_set, _ = parse_description(text)
    if screen_set:
        return (KIND_SCREEN_SET, screen_set)

    for pattern, kind in _TARGET_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        groups = m.groupdict()
        if kind == KIND_MENU_ITEM:
            name = groups.get("id") or ""
            rest = (groups.get("rest") or "").strip()
            if rest:
                name = f"{name} at {rest}"
            return (kind, name)
        name = _tidy(groups.get("name") or "")
        if name or kind == KIND_PACKAGE:
            return (kind, name)

    # Nothing matched: the Operation column still names the kind of thing.
    return ((operation or "").strip(), "")


def parse_description(text: str) -> tuple[str, str]:
    """Pull ``(screen_set, screen)`` out of a Description cell."""
    t = (text or "").strip()
    if not t:
        return "", ""
    # Drop a leading "Current Settings:" style prefix.
    body = re.sub(r"^[^:]{0,40}:\s*", "", t, count=1) if ":" in t[:42] else t
    m = _DESC_FULL_RE.search(body)
    if m:
        return _plausible_set(m.group("set")), _tidy(m.group("screen"))
    m = _DESC_SET_RE.search(body)
    if m:
        return _plausible_set(m.group("set")), ""
    return "", ""


#: Wording that means the sentence was never naming a screen set. "Screen Set
#: Assignation in the Current Settings of ... has been updated" describes an
#: assignment operation; capturing that clause as a screen-set *name* invents a
#: screen set and then reports it as out of the plan's scope.
_NOT_A_SET_RE = re.compile(
    r"\b(assignation|assignment|current settings of|restaurant profile|"
    r"in the\s+\w+\s+settings)\b",
    re.I,
)

#: A screen-set name is a label, not a sentence.
_MAX_SET_NAME = 60


def _plausible_set(raw: str) -> str:
    """A screen-set name, or "" when the sentence was not naming one."""
    name = _tidy(raw)
    if not name or len(name) > _MAX_SET_NAME or _NOT_A_SET_RE.search(name):
        return ""
    return name


def _tidy(s: str) -> str:
    return " ".join(s.replace("\xa0", " ").split()).strip(" .;:")


# ---------------------------------------------------------------------------
# Change classification
# ---------------------------------------------------------------------------

_ENABLE_WORDS = {"enable", "enabled", "active", "on", "true", "yes", "y", "visible"}
_DISABLE_WORDS = {"disable", "disabled", "inactive", "off", "false", "no", "n", "hidden"}


def classify(old: str, new: str) -> str:
    o, n = (old or "").strip(), (new or "").strip()
    ol, nl = o.lower(), n.lower()
    if ol == nl:
        return UNCHANGED
    if ol in _ENABLE_WORDS and nl in _DISABLE_WORDS:
        return DISABLED
    if ol in _DISABLE_WORDS and nl in _ENABLE_WORDS:
        return ENABLED
    if o and not n:
        return CLEARED
    if not o and n:
        return SET
    return CHANGED


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

_TS_FORMATS = (
    "%b %d, %Y %I:%M:%S %p",
    "%b %d, %Y %I:%M %p",
    "%b %d, %Y %H:%M:%S",
    "%b %d, %Y",
    "%B %d, %Y %I:%M:%S %p",
    "%B %d, %Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y",
    "%d-%m-%Y %H:%M:%S",
    "%d/%m/%Y",
)


def parse_timestamp(text: str) -> _dt.datetime | None:
    t = " ".join((text or "").replace("\xa0", " ").split())
    if not t:
        return None
    for fmt in _TS_FORMATS:
        try:
            return _dt.datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_PREAMBLE = {
    "report_effective_date": re.compile(r"effective\s*date\s*[\[\(:]\s*([^\]\)]+)", re.I),
    "generated_on": re.compile(r"report\s+generated\s+on\s*[-:]\s*(.+)", re.I),
    "generated_by": re.compile(r"report\s+generated\s+by\s*[-:]\s*(.+)", re.I),
}


def is_audit_log(wb: Workbook) -> bool:
    """True if this workbook carries an audit-report header row.

    Used to spot a plan and an audit log uploaded the wrong way round.
    """
    return _locate_header(wb) is not None


def parse_audit_log(raw: bytes, filename: str) -> AuditLog:
    """Read and decompose an audit report from raw uploaded bytes."""
    try:
        wb = read_any(raw, filename)
    except ReadError as exc:
        raise AuditParseError(str(exc)) from exc
    return build_audit_log(wb)


def build_audit_log(wb: Workbook) -> AuditLog:
    log = AuditLog(
        filename=wb.filename, fmt=wb.fmt, encoding=wb.encoding, notes=list(wb.notes)
    )

    found = _locate_header(wb)
    if found is None:
        raise AuditParseError(
            "No audit-log header row was found. The report must contain a row of "
            "column names including at least Field, Old Setting and New Setting. "
            "Sheets seen: "
            + ", ".join(f"{s.name!r} ({s.n_rows}x{s.n_cols})" for s in wb.sheets)
        )
    sheet, header_row, colmap = found
    log.columns_found = dict(colmap)
    log.missing_columns = [c for c in COLUMNS if c not in colmap]

    _read_preamble(log, sheet, header_row)
    _read_entries(log, sheet, header_row, colmap)

    if not log.entries:
        raise AuditParseError(
            "The audit-log header was found but it has no data rows below it."
        )
    _post_checks(log)
    return log


def _locate_header(wb: Workbook) -> tuple[Sheet, int, dict[str, int]] | None:
    """Find the sheet/row holding column names, and map canonical name -> column."""
    best: tuple[int, Sheet, int, dict[str, int]] | None = None
    for sheet in wb.sheets:
        s = sheet.normalized()
        for r in range(min(s.n_rows, 40)):
            colmap: dict[str, int] = {}
            for c in range(s.n_cols):
                cell = s.cell(r, c).strip().lower()
                if not cell:
                    continue
                for canon, spellings in COLUMNS.items():
                    if canon in colmap:
                        continue
                    if cell in spellings:
                        colmap[canon] = c
                        break
            if all(k in colmap for k in REQUIRED):
                score = len(colmap)
                if best is None or score > best[0]:
                    best = (score, sheet, r, colmap)
    if best is None:
        return None
    return best[1].normalized(), best[2], best[3]


def _read_preamble(log: AuditLog, sheet: Sheet, header_row: int) -> None:
    """Harvest report-level metadata from the rows above the header."""
    for r in range(header_row):
        line = " ".join(c for c in sheet.row(r) if c).strip()
        if not line:
            continue
        for attr, rx in _PREAMBLE.items():
            if getattr(log, attr):
                continue
            m = rx.search(line)
            if m:
                setattr(log, attr, _tidy(m.group(1)))


def _read_entries(log: AuditLog, sheet: Sheet, header_row: int, colmap: dict[str, int]) -> None:
    def get(row: list[str], key: str) -> str:
        c = colmap.get(key)
        if c is None or c >= len(row):
            return ""
        return row[c].strip()

    for r in range(header_row + 1, sheet.n_rows):
        row = sheet.row(r)
        if not any(c.strip() for c in row):
            continue
        field_text = get(row, "field")
        if not field_text:
            # A row with no Field carries no verifiable change.
            log.skipped_rows += 1
            continue

        e = AuditEntry(row_no=r + 1)
        e.audit_id = get(row, "id")
        e.operation = get(row, "operation")
        e.activity = get(row, "activity")
        e.level = get(row, "level")
        e.level_details = get(row, "level_details")
        e.date_text = get(row, "date")
        e.user = get(row, "user")
        e.description = get(row, "description")
        e.field_text = field_text
        e.old = get(row, "old")
        e.new = get(row, "new")
        e.effective_from = get(row, "effective_from")
        e.package = get(row, "package")
        e.raw = {k: get(row, k) for k in colmap}

        e.prop, e.button, e.language = parse_field(field_text)
        e.screen_set, e.screen = parse_description(e.description)
        e.target_kind, e.target_name = parse_target(e.description, e.operation)
        # Fall back to Level Details when the description carries no screen set.
        if not e.screen_set and e.level_details:
            e.screen_set = ""
        e.timestamp = parse_timestamp(e.date_text)
        e.change = classify(e.old, e.new)

        log.entries.append(e)


def _post_checks(log: AuditLog) -> None:
    """Structural observations about the log as a whole."""
    unparsed = [e for e in log.entries if not e.prop]
    if unparsed:
        log.warnings.append(
            f"{len(unparsed)} row(s) had a Field value that could not be decomposed "
            f"(e.g. row {unparsed[0].row_no}: {unparsed[0].field_text!r})."
        )

    no_set = [e for e in log.entries if not e.screen_set]
    if no_set and len(no_set) != len(log.entries):
        log.warnings.append(
            f"{len(no_set)} row(s) have a Description with no identifiable screen set."
        )
    elif no_set:
        log.warnings.append(
            "No row carries an identifiable screen set in its Description, so "
            "changes cannot be attributed to a specific screen set. Validation "
            "will fall back to matching on button numbers alone."
        )

    if log.skipped_rows:
        log.notes.append(f"{log.skipped_rows} row(s) with an empty Field were ignored.")

    if log.missing_columns:
        log.notes.append(
            "Columns not present in this report: " + ", ".join(log.missing_columns)
        )

    # Same field changed more than once in the same report.
    seen: dict[tuple[str, str], AuditEntry] = {}
    dupes: list[str] = []
    for e in log.entries:
        from .plan_schema import norm_entity
        key = (norm_entity(e.screen_set), e.field_text)
        if key in seen:
            dupes.append(f"{e.field_text} (rows {seen[key].row_no} and {e.row_no})")
        else:
            seen[key] = e
    if dupes:
        log.warnings.append(
            f"{len(dupes)} field(s) were changed more than once in this report: "
            + "; ".join(dupes[:5]) + ("…" if len(dupes) > 5 else "")
        )
