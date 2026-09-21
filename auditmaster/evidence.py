"""
Operation-agnostic reconciliation.

The structured validator in :mod:`auditmaster.validator` is precise but narrow:
it only knows how to ask "did this button reach state X". Real plans ask for
many other things — replace a caption, swap an image, change a display order,
localize a menu item — and writing a bespoke rule per operation does not scale.

This module takes the other route. It never tries to understand what the plan
*means*. It harvests the distinctive **values** a plan expects to see —
captions, asset names, menu-item numbers, button numbers, workflows — each
tagged with the header that labelled it, and then asks a much simpler question:

    does this value appear in the audit log, and where?

An audit log records ``Old Setting`` and ``New Setting`` for every change, so a
value the plan asks for either turns up as a new setting (the work was done),
turns up as an old setting (the thing was replaced), or does not turn up at all.
Symmetrically, an audit change whose values appear nowhere in the plan is a
change nobody asked for.

That reconciliation needs no knowledge of the operation, which is what lets one
engine cover plans it has never seen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .audit_parser import AuditEntry, AuditLog
from .plan_schema import fold
from .readers import Sheet, Workbook

# -- claim kinds ------------------------------------------------------------
ASSET = "asset"            # Kiosk_Category_202604_...  /  MO_202607_6975_....jpg
CAPTION = "caption"        # a display label the plan wants shown
BUTTON = "button"          # a button/tile position
MENU_ITEM = "menu_item"    # an MI number
WORKFLOW = "workflow"      # a workflow / screen number
ENTITY = "entity"          # a screen set / co-op name

KIND_LABEL = {
    ASSET: "Image / asset name",
    CAPTION: "Caption text",
    BUTTON: "Button number",
    MENU_ITEM: "Menu item number",
    WORKFLOW: "Workflow / screen number",
    ENTITY: "Screen set",
}

# -- match outcomes ---------------------------------------------------------
SET_TO = "set_to"          # appears as a New Setting: the work applied it
REPLACED = "replaced"      # appears as an Old Setting: it was cleared/replaced
TOUCHED = "touched"        # the field itself was changed (button/field address)
ABSENT = "absent"          # nowhere in this audit log

OUTCOME_LABEL = {
    SET_TO: "applied",
    REPLACED: "replaced",
    TOUCHED: "touched",
    ABSENT: "not in this log",
}


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------


@dataclass
class Claim:
    """One value the plan expects to see reflected somewhere in an audit log."""

    value: str
    kind: str
    label: str = ""          # the header/row label that gave it its meaning
    source: str = ""         # sheet!RxCy
    outcome: str = ABSENT
    entries: list[AuditEntry] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return self.outcome != ABSENT

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, fold(self.value) if self.kind != BUTTON else self.value)


# ---------------------------------------------------------------------------
# Value recognisers
# ---------------------------------------------------------------------------

#: "Kiosk_Category_202604_Q2JUNEBR_396x396", "MO_202607_6975_Hamburger....jpg"
_ASSET_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9]*(?:[_\-][A-Za-z0-9]+){2,}(?:\.(?:jpg|jpeg|png|gif|bmp|svg))?$"
)
_FILENAME_RE = re.compile(r"^[\w\-. ]{4,}\.(?:jpg|jpeg|png|gif|bmp|svg)$", re.I)

#: "08 - EL MAC", "29 - MOCNI (Pre-Prod)", "16 - GREAT PLAINS (362)"
_ENTITY_RE = re.compile(r"^\d{1,3}\s*[-–—]\s*[A-Za-z][A-Za-z0-9 &'./-]{2,}(\s*\([^)]*\))?$")

_PURE_NUM_RE = re.compile(r"^\d{1,6}$")

#: Header words that say what a numeric column holds. Daypart names count as
#: button headers: a column headed "Breakfast" in a kiosk plan holds buttons.
_LABEL_KINDS: list[tuple[str, list[str]]] = [
    (BUTTON, ["button number", "button no", "button #", "button", "btn", "tile number", "position",
              "breakfast", "lunch", "dinner", "latenight", "late night", "allday", "all day",
              "snack", "brunch", "supper", "evening", "overnight", "daypart"]),
    (MENU_ITEM, ["menu item number", "menu item no", "mi number", "mi no", "mi#", "menu item", "item number"]),
    (WORKFLOW, ["workflow", "work flow", "wf", "screen set number", "screenset number", "screen number", "screen"]),
    (CAPTION, ["caption", "label", "button text", "display name", "tile name", "description text"]),
    (ASSET, ["image name", "image", "asset", "mop name", "generic mop name", "picture", "graphic", "file name"]),
]

#: Prose giveaways — a cell containing these is an instruction, not a value.
_PROSE_RE = re.compile(
    r"\b(open|go to|click|navigate|please|make sure|note|should be|take (a )?screenshot|"
    r"update the|check the|use \"|refer|kindly|ensure|verify|apply|save)\b",
    re.I,
)


def _label_kind(text: str) -> str | None:
    """The claim kind implied by a header/label cell."""
    t = " ".join((text or "").replace("\xa0", " ").lower().split()).strip(" :#*")
    if not t or len(t) > 40:
        return None
    for kind, words in _LABEL_KINDS:
        for w in words:
            if t == w or t.startswith(w) or t.endswith(w) or f" {w} " in f" {t} ":
                return kind
    return None


def _looks_like_prose(text: str) -> bool:
    if len(text) > 90:
        return True
    return bool(_PROSE_RE.search(text))


#: Values that are a state flip rather than content a plan could quote.
_STATE_VALUES = {
    "enable", "enabled", "disable", "disabled", "true", "false", "yes", "no",
    "y", "n", "on", "off", "checked", "unchecked", "active", "inactive",
    "visible", "hidden", "", "n/a",
}


def _is_state(value: str) -> bool:
    return (value or "").strip().lower() in _STATE_VALUES


def _clean_value(text: str) -> str:
    s = (text or "").replace("\xa0", " ").replace("\u202f", " ")
    # Plans carry formatting escapes inside caption strings.
    s = re.sub(r"\\fsize[+\-]\d*", " ", s)
    s = s.replace("\\r", " ").replace("\\n", " ").replace("\\t", " ")
    return " ".join(s.split()).strip(" \"'*:;")


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract_claims(wb: Workbook, max_per_kind: int = 400) -> list[Claim]:
    """Harvest every value in the workbook that an audit log could corroborate.

    Meaning comes from position, not from parsing sentences: a cell is
    interpreted through the nearest header above it in its own column and the
    nearest label to its left in its own row. That is enough to tell a menu-item
    number from a button number without knowing anything about the operation.
    """
    claims: list[Claim] = []
    seen: set[tuple[str, str]] = set()
    # Counted rather than recomputed: scanning every claim on every add made
    # extraction quadratic on a few-thousand-row sheet.
    per_kind: dict[str, int] = {}

    def add(value: str, kind: str, label: str, source: str) -> None:
        v = _clean_value(value)
        if not v or len(v) < 2:
            return
        if per_kind.get(kind, 0) >= max_per_kind:
            return
        c = Claim(value=v, kind=kind, label=label, source=source)
        if c.key in seen:
            return
        seen.add(c.key)
        per_kind[kind] = per_kind.get(kind, 0) + 1
        claims.append(c)

    for sheet in wb.sheets:
        s = sheet.normalized()
        col_labels = _column_labels(s)
        for r in range(s.n_rows):
            row_label = _row_label(s, r)
            for c in range(s.n_cols):
                raw = s.cell(r, c)
                if not raw:
                    continue
                src = f"{sheet.name}!R{r + 1}C{c + 1}"
                # A cell sits at the intersection of two labels. Prefer the
                # column header, but fall back to the row label when the column
                # header cannot explain this value — a caption written beside a
                # "Caption" row label inside a daypart column needs the latter.
                for label in (col_labels.get((r, c), ""), row_label):
                    kind = _classify(raw, label)
                    if kind:
                        add(raw, kind, label, src)
                        break

    return claims


def _is_banner(s: Sheet, r: int, c: int) -> bool:
    """True if this cell is part of a merged title spanning several columns.

    Unmerging copies a section banner such as "Generic National Associations
    MOP Name" across every column it covered, which would otherwise make it
    look like the column header for unrelated cells below it. A banner is
    recognised by that duplication and is never allowed to label anything.
    """
    cell = s.cell(r, c)
    if not cell:
        return False
    return s.cell(r, c - 1) == cell if c > 0 else s.cell(r, c + 1) == cell


def _header_like(text: str) -> bool:
    """A short, digit-free text cell — the shape of a column heading."""
    t = text.strip()
    return bool(t) and len(t) <= 30 and not any(ch.isdigit() for ch in t)


def _column_labels(s: Sheet) -> dict[tuple[int, int], str]:
    """For each populated cell, the nearest labelled header above it.

    A merged banner ("Button Number" spanning the four daypart columns) may
    label a column, but any real per-column header below it wins — including an
    unrecognised one. That is what stops a section title like "Generic National
    Associations MOP Name" from labelling the unrelated column beside it.
    """
    out: dict[tuple[int, int], str] = {}
    for c in range(s.n_cols):
        current = ""
        from_banner = False
        for r in range(s.n_rows):
            cell = s.cell(r, c)
            if not cell:
                continue
            banner = _is_banner(s, r, c)
            if _label_kind(cell):
                current, from_banner = cell, banner
                continue
            if from_banner and _header_like(cell):
                # This column has its own heading, and it is not one we know.
                current, from_banner = "", False
                continue
            out[(r, c)] = current
    return out


def _row_label(s: Sheet, r: int) -> str:
    """The first labelled cell in this row, which labels the cells after it."""
    for c in range(min(s.n_cols, 6)):
        cell = s.cell(r, c)
        if cell and not _is_banner(s, r, c) and _label_kind(cell):
            return cell
    return ""


def _classify(raw: str, label: str) -> str | None:
    """The claim kind for one cell, given the header that labels it."""
    v = _clean_value(raw)
    if not v or len(v) < 2:
        return None

    label_kind = _label_kind(label)

    # A label never becomes its own claim.
    if _label_kind(v):
        return None

    # Filenames and underscore-joined asset tokens are unambiguous.
    if _FILENAME_RE.match(v) or (_ASSET_RE.match(v) and len(v) >= 12 and any(ch.isdigit() for ch in v)):
        return ASSET

    if _ENTITY_RE.match(v):
        return ENTITY

    if _PURE_NUM_RE.match(v):
        # A bare number means nothing without a header to say what it is.
        if label_kind in (BUTTON, MENU_ITEM, WORKFLOW):
            return label_kind
        return None

    # "25702 - FIFA World Cup™ Meal - Sausage McMuffin®" under an MI header.
    m = re.match(r"^(\d{3,6})\s*[-–—]\s*\S", v)
    if m and label_kind == MENU_ITEM:
        return MENU_ITEM

    if label_kind in (CAPTION, ASSET):
        return None if _looks_like_prose(v) else label_kind

    return None


# ---------------------------------------------------------------------------
# Audit index
# ---------------------------------------------------------------------------


class AuditIndex:
    """Every value in an audit log, indexed for lookup by the plan's claims."""

    def __init__(self, log: AuditLog):
        self.log = log
        self.new_values: dict[str, list[AuditEntry]] = {}
        self.old_values: dict[str, list[AuditEntry]] = {}
        self.buttons: dict[str, list[AuditEntry]] = {}
        self.screen_sets: dict[str, list[AuditEntry]] = {}

        for e in log.entries:
            if e.new:
                self.new_values.setdefault(fold(e.new), []).append(e)
            if e.old:
                self.old_values.setdefault(fold(e.old), []).append(e)
            if e.button:
                self.buttons.setdefault(e.button, []).append(e)
            if e.screen_set:
                self.screen_sets.setdefault(fold(e.screen_set), []).append(e)

    def lookup(self, claim: Claim) -> tuple[str, list[AuditEntry]]:
        """Where, if anywhere, this claim's value shows up."""
        if claim.kind == BUTTON:
            hits = self.buttons.get(claim.value, [])
            return (TOUCHED, hits) if hits else (ABSENT, [])

        if claim.kind == ENTITY:
            key = fold(claim.value)
            hits = self.screen_sets.get(key, [])
            if not hits:
                # Plans append a qualifier the audit log omits: "16 - GREAT
                # PLAINS (362)" against "16 - GREAT PLAINS".
                for k, v in self.screen_sets.items():
                    if k and (k.startswith(key) or key.startswith(k)):
                        hits = v
                        break
            return (TOUCHED, hits) if hits else (ABSENT, [])

        key = fold(claim.value)
        if not key:
            return (ABSENT, [])
        if key in self.new_values:
            return (SET_TO, self.new_values[key])
        if key in self.old_values:
            return (REPLACED, self.old_values[key])

        # A menu-item claim may be written "25702 - FIFA ..." while the audit
        # log carries only the number, or vice versa.
        if claim.kind == MENU_ITEM:
            num = re.match(r"^(\d{3,6})", claim.value)
            if num:
                for table, outcome in ((self.new_values, SET_TO), (self.old_values, REPLACED)):
                    for k, v in table.items():
                        if k.startswith(num.group(1)):
                            return (outcome, v)
        return (ABSENT, [])


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


@dataclass
class EvidenceResult:
    claims: list[Claim] = field(default_factory=list)
    explained_rows: set[int] = field(default_factory=set)

    @property
    def matched(self) -> list[Claim]:
        return [c for c in self.claims if c.matched]

    @property
    def absent(self) -> list[Claim]:
        return [c for c in self.claims if not c.matched]

    def by_kind(self, matched: bool = True) -> dict[str, list[Claim]]:
        out: dict[str, list[Claim]] = {}
        for c in self.claims:
            if c.matched is matched:
                out.setdefault(c.kind, []).append(c)
        return out

    def unexplained(self, log: AuditLog) -> list[AuditEntry]:
        return [e for e in log.entries if e.row_no not in self.explained_rows]

    @property
    def match_rate(self) -> int:
        if not self.claims:
            return 0
        return round(len(self.matched) / len(self.claims) * 100)


def reconcile(claims: list[Claim], log: AuditLog) -> EvidenceResult:
    """Match every plan claim against the audit log, both directions.

    The claims handed in belong to the :class:`~auditmaster.plan_schema.Plan`
    and are reused every time that plan is checked, so they are copied here
    rather than filled in. Writing the outcome back onto the shared objects
    would let a later validation silently rewrite the evidence of an earlier
    result that is still being displayed.
    """
    index = AuditIndex(log)
    claims = [
        Claim(value=c.value, kind=c.kind, label=c.label, source=c.source)
        for c in claims
    ]
    res = EvidenceResult(claims=claims)

    for claim in claims:
        outcome, entries = index.lookup(claim)
        claim.outcome = outcome
        claim.entries = entries
        if claim.kind in (ENTITY, BUTTON):
            # Screen sets and buttons say *where* work happened, not *what* was
            # asked for. A screen-set name matches every row of that screen set,
            # and a button matches every field on that button — so letting them
            # explain rows would hide an unrequested change made in a requested
            # place, which is precisely the thing worth catching.
            continue
        for e in entries:
            res.explained_rows.add(e.row_no)

    # A state flip (Enable -> Disable) carries no value a plan could name, so it
    # is explained by its button being named instead.
    claimed_buttons = {c.value for c in claims if c.kind == BUTTON and c.matched}
    for e in log.entries:
        if e.row_no in res.explained_rows:
            continue
        if e.button and e.button in claimed_buttons and _is_state(e.new) and _is_state(e.old):
            res.explained_rows.add(e.row_no)

    return res
