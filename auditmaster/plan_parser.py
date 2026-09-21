"""
Deployment-plan parser: arbitrary spreadsheet -> :class:`Plan` (the NDP).

The parser never assumes fixed cell addresses. It *locates* structure:

1. Assign a role to each sheet from its name, falling back to content.
2. In the matrix sheet, find the header band by scoring rows on how many
   grouping aliases they contain, then find the entity column by looking in
   and above that band. Two-row headers (a spanning "Button Number" title
   above the daypart names) are handled by this scoring rather than special-cased.
3. Read every data row below the header into one :class:`Target` per populated cell.
4. Harvest context (screen number, tile label, action, workflows) from the
   instruction and validation sheets by pattern, in whatever row they appear.

Detection is attempted in four passes, most specific first, and the first that
yields targets wins:

  1. **Named matrix** — a row of recognised group names (dayparts) beside an
     entity column.
  2. **Shape matrix** — no group names recognised, so one text column beside
     two or more button-valued columns. Guarded by :func:`_axes_look_swapped`,
     because shape alone cannot tell a grid from the same grid pivoted.
  3. **Flat list** — an entity column beside a button column, across every
     sheet that has one. When a sheet has buttons but no entity column, the
     sheet's own name is the entity, which covers one-tab-per-screen-set plans.
  4. **Transposed** — each sheet flipped and retried, which rescues both a
     pivoted grid and the vertical ``group | button`` key/value stanza.

Button cells are read leniently by :func:`_button_values`: ``12``, ``12.0``
(Excel numerics), ``Button 12``, ``#12``, ``12 (new)`` and ``"12, 30, 53, 76"``
all work, and one cell may expand into several targets. It stays strict enough
that a ticket reference like ``T-2`` is never mistaken for a button.
"""

from __future__ import annotations

import json
import re

from .evidence import extract_claims
from .plan_schema import Assignment, Plan, PlanMeta, Target, norm_label
from .profiles import (
    ASSIGNEE_ALIASES,
    BUTTON_ALIASES,
    GENERIC_SHEET_NAMES,
    PROFILES,
    STATUS_ALIASES,
    VALIDATOR_ALIASES,
    detect_action,
    matches_alias,
    matches_alias_exact,
    sheet_role,
    which_alias,
)
from .readers import ReadError, Sheet, Workbook, read_any

HEADER_SCAN_ROWS = 25          # how deep to look for a header band
_MAX_TRANSPOSE_ROWS = 40       # taller than this and the sheet is a record table
MAX_BUTTON = 9999              # a button number above this is not a button


class PlanParseError(Exception):
    """Raised when a file cannot be understood as a deployment plan."""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_plan(raw: bytes, filename: str) -> Plan:
    """Read and normalize a deployment plan from raw uploaded bytes."""
    # A previously exported NDP comes back in as JSON and is trusted as-is.
    ndp = _try_ndp_json(raw)
    if ndp is not None:
        ndp.notes.append("Loaded from a normalized plan template (NDP JSON).")
        return ndp

    try:
        wb = read_any(raw, filename)
    except ReadError as exc:
        raise PlanParseError(str(exc)) from exc

    # Structured parsing is the precise path, but it only understands screen-set
    # button plans. When it cannot apply, fall back to harvesting the plan's
    # values so the operation-agnostic engine still has something to match.
    try:
        plan = build_plan(wb)
    except PlanParseError as exc:
        plan = _evidence_only_plan(wb, str(exc))

    plan.claims = extract_claims(wb)
    if not plan.targets and not plan.claims:
        raise PlanParseError(
            "Nothing checkable was found in this plan. No screen-set/button "
            "table was located, and no captions, image names, menu-item numbers "
            "or button numbers could be harvested either. If the plan's content "
            "is inside embedded images or screenshots rather than cells, it "
            "cannot be read."
        )
    return plan


def _evidence_only_plan(wb: Workbook, reason: str) -> Plan:
    """A plan with no structured targets, carrying only harvested values."""
    plan = Plan(
        plan_name=_plan_name(wb.filename),
        profile="evidence_only",
        profile_label="Value evidence (no button grid found)",
        source_filename=wb.filename,
        source_format=wb.fmt,
        source_encoding=wb.encoding,
        sheet_names=[s.name for s in wb.sheets],
        notes=list(wb.notes),
    )
    roles = _assign_roles(wb)
    plan.sheet_roles = dict(roles)
    plan.notes.append(
        "No screen-set / button grid was found, so this plan was checked by "
        "matching the values it names against the audit log. " + reason
    )
    try:
        _harvest_meta(plan, wb, roles)
    except Exception:  # pragma: no cover - context is best-effort
        pass
    return plan


def _try_ndp_json(raw: bytes) -> Plan | None:
    head = raw[:512].lstrip()
    if not head.startswith(b"{"):
        return None
    try:
        doc = json.loads(raw.decode("utf-8-sig", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(doc, dict) and "targets" in doc and "schema_version" in doc:
        try:
            return Plan.from_dict(doc)
        except (TypeError, ValueError):
            return None
    return None


def build_plan(wb: Workbook) -> Plan:
    """Normalize an already-read :class:`Workbook` into a :class:`Plan`."""
    plan = Plan(
        plan_name=_plan_name(wb.filename),
        source_filename=wb.filename,
        source_format=wb.fmt,
        source_encoding=wb.encoding,
        sheet_names=[s.name for s in wb.sheets],
        notes=list(wb.notes),
    )

    roles = _assign_roles(wb)
    plan.sheet_roles = {name: role for name, role in roles.items()}

    matrix_sheet = next((s for s in wb.sheets if roles.get(s.name) == "matrix"), None)
    layout = _find_matrix(matrix_sheet) if matrix_sheet else None

    # If the sheet we picked has no usable matrix, try every other sheet.
    if layout is None:
        for s in wb.sheets:
            if s is matrix_sheet:
                continue
            layout = _find_matrix(s)
            if layout is not None:
                matrix_sheet = s
                plan.sheet_roles[s.name] = "matrix"
                break

    if layout is not None:
        _read_matrix(plan, matrix_sheet, layout)
        prof = PROFILES["screenset_button_matrix"]
    else:
        flat = _find_flat_targets(wb, roles)
        if flat is not None:
            groups: list[str] = []
            for sheet, layout_flat in flat:
                _read_flat(plan, sheet, layout_flat, groups)
                plan.sheet_roles.setdefault(sheet.name, "matrix")
                if layout_flat.entity_col is None:
                    plan.notes.append(
                        f"Sheet {sheet.name!r} has no screen-set column, so the "
                        f"sheet name was used as the screen set."
                    )
            plan.groups = groups
            prof = PROFILES["explicit_target_list"]
        else:
            # Last resort: the sheet may be pivoted the other way round, with
            # entities across the top and groups down the side.
            flipped = _find_transposed(wb)
            if flipped is None:
                raise PlanParseError(_no_targets_message(wb))
            source_name, flipped_sheet, layout = flipped
            _read_matrix(plan, flipped_sheet, layout)
            plan.sheet_roles[source_name] = "matrix"
            plan.notes.append(
                f"Sheet {source_name!r} was read transposed — it lists groups down "
                f"the side and screen sets across the top."
            )
            prof = PROFILES["screenset_button_matrix"]

    plan.profile = next(k for k, v in PROFILES.items() if v is prof)
    plan.profile_label = prof["label"]

    _harvest_meta(plan, wb, roles)
    _read_assignments(plan, wb, roles)
    _sanity_check(plan)
    return plan


def _plan_name(filename: str) -> str:
    # A browser may hand over a full path; Windows ones use backslashes.
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return base.replace("_", " ").strip() or "Deployment plan"


# ---------------------------------------------------------------------------
# Sheet roles
# ---------------------------------------------------------------------------


def _assign_roles(wb: Workbook) -> dict[str, str]:
    roles: dict[str, str] = {}
    for s in wb.sheets:
        role = sheet_role(s.name)
        if role:
            roles[s.name] = role

    # A sheet named for the matrix but holding only prose is really instructions.
    for s in wb.sheets:
        if roles.get(s.name) == "matrix" and _find_matrix(s) is None:
            roles[s.name] = "instructions"

    if "matrix" not in roles.values():
        for s in wb.sheets:
            if _find_matrix(s) is not None:
                roles[s.name] = "matrix"
                break

    for s in wb.sheets:
        roles.setdefault(s.name, "context")
    return roles


# ---------------------------------------------------------------------------
# Matrix location
# ---------------------------------------------------------------------------


class _MatrixLayout:
    def __init__(self, header_row: int, entity_col: int, groups: dict[int, str], first_data_row: int):
        self.header_row = header_row
        self.entity_col = entity_col
        self.groups = groups              # column index -> group name
        self.first_data_row = first_data_row


def _find_matrix(sheet: Sheet | None) -> _MatrixLayout | None:
    """Locate an entity column and grouping columns in ``sheet``."""
    if sheet is None or sheet.n_rows < 2 or sheet.n_cols < 2:
        return None
    s = sheet.normalized()
    prof = PROFILES["screenset_button_matrix"]
    limit = min(s.n_rows, HEADER_SCAN_ROWS)

    # -- pass 1: a row whose cells are recognised group names ---------------
    best_row, best_hits = -1, 0
    for r in range(limit):
        hits = sum(
            1 for c in range(s.n_cols)
            if _is_group_header(s.cell(r, c), prof["group_aliases"])
        )
        if hits > best_hits:
            best_row, best_hits = r, hits

    if best_hits >= 2:
        groups = {
            c: s.cell(best_row, c)
            for c in range(s.n_cols)
            if _is_group_header(s.cell(best_row, c), prof["group_aliases"])
        }
        entity_col = _find_entity_col(s, best_row, set(groups))
        if entity_col is not None:
            first = _first_data_row(s, best_row, entity_col, groups)
            if first is not None:
                return _MatrixLayout(best_row, entity_col, groups, first)

    # -- pass 2: shape only. Text column + integer columns ------------------
    return _find_matrix_by_shape(s)


def _find_entity_col(s: Sheet, header_row: int, group_cols: set[int]) -> int | None:
    """Entity column: an alias hit in the header band, else the leftmost text column."""
    prof = PROFILES["screenset_button_matrix"]
    # Look in the header row and up to 4 rows above it (spanning titles).
    for r in range(max(0, header_row - 4), header_row + 1):
        for c in range(s.n_cols):
            if c in group_cols:
                continue
            if s.cell(r, c) and matches_alias(s.cell(r, c), prof["entity_aliases"]):
                return c
    # Fall back to the leftmost column left of the groups that holds text below.
    ceiling = min(group_cols) if group_cols else s.n_cols
    for c in range(ceiling):
        texts = sum(
            1 for r in range(header_row + 1, min(s.n_rows, header_row + 40))
            if s.cell(r, c) and not _is_button_cell(s.cell(r, c))
        )
        if texts >= 2:
            return c
    return None


def _first_data_row(s: Sheet, header_row: int, entity_col: int, groups: dict[int, str]) -> int | None:
    """First row below the header that has an entity name and a button number."""
    for r in range(header_row + 1, s.n_rows):
        if not s.cell(r, entity_col):
            continue
        if any(_is_button_cell(s.cell(r, c)) for c in groups):
            return r
    return None


#: A daypart header is a short word ("Breakfast", "Latenight"). A screen-set
#: name such as "Kiosk 6 Dinner Menu - Limited Time Promotion" merely *contains*
#: one, and must not be counted as a daypart column.
_MAX_GROUP_HEADER = 24


def _is_group_header(cell: str, aliases) -> bool:
    if not cell:
        return False
    if matches_alias_exact(cell, aliases):
        return True
    return len(cell.strip()) <= _MAX_GROUP_HEADER and matches_alias(cell, aliases)


def _find_matrix_by_shape(s: Sheet) -> _MatrixLayout | None:
    """Detect a cross-tab with unrecognised group names, purely from shape."""
    limit = min(s.n_rows, HEADER_SCAN_ROWS)
    for header_row in range(limit):
        body = range(header_row + 1, min(s.n_rows, header_row + 200))
        rows_in_body = [r for r in body if any(s.cell(r, c) for c in range(s.n_cols))]
        if len(rows_in_body) < 2:
            continue

        int_cols, text_cols = [], []
        for c in range(s.n_cols):
            vals = [s.cell(r, c) for r in rows_in_body if s.cell(r, c)]
            if len(vals) < 2:
                continue
            ints = sum(1 for v in vals if _is_button_cell(v))
            if ints >= max(2, int(len(vals) * 0.8)):
                int_cols.append(c)
            elif ints == 0:
                text_cols.append(c)

        # Need a name column and at least two numeric columns to be a matrix.
        if len(int_cols) >= 2 and text_cols:
            entity_col = min(text_cols)
            if _axes_look_swapped(s, entity_col, rows_in_body):
                continue
            if not _entity_col_is_plausible(s, header_row, entity_col, rows_in_body):
                continue
            named = {c: (s.cell(header_row, c) or f"Group {i + 1}")
                     for i, c in enumerate(int_cols) if c != entity_col}
            if len(named) >= 2:
                first = _first_data_row(s, header_row, entity_col, named)
                if first is not None:
                    return _MatrixLayout(header_row, entity_col, named, first)
    return None


#: A screen set reads like "01 - WWOA" or "Fairway - Portfolio A": a code or
#: name joined by a dash. Menu-item names ("Hamburger") do not.
_ENTITY_VALUE_RE = re.compile(r"^\s*\d{1,3}\s*[-\u2013\u2014]\s*\S|\S+\s+[-\u2013\u2014]\s+\S+")


def _entity_col_is_plausible(s: Sheet, header_row: int, entity_col: int, rows_in_body) -> bool:
    """Whether a shape-detected entity column really holds screen-set names.

    Shape alone cannot tell a screen-set grid from any other table with one
    text column beside numeric ones — a menu-item display-order sheet
    ("Number | Name | Display Order") fits the same shape and would silently
    become a button grid with display-order values as button numbers. So when
    the daypart names were *not* recognised, the entity column must corroborate
    itself: either its header names an entity, or its values look like one.
    """
    prof = PROFILES["screenset_button_matrix"]
    header = s.cell(header_row, entity_col)
    if header and matches_alias(header, prof["entity_aliases"]):
        return True

    vals = [s.cell(r, entity_col) for r in rows_in_body if s.cell(r, entity_col)]
    if not vals:
        return False
    hits = sum(1 for v in vals if _ENTITY_VALUE_RE.match(v))
    return hits >= len(vals) * 0.5


def _axes_look_swapped(s: Sheet, entity_col: int, rows_in_body) -> bool:
    """True if the would-be entity column is actually a list of group names.

    Shape alone cannot tell a screen-set/daypart grid from the same grid
    pivoted: both are "one text column beside several numeric ones". The tell is
    the *content* — if the entity column reads Breakfast/Lunch/Dinner, the sheet
    is pivoted and the real entities are across the top, so this orientation is
    rejected and the transposed pass gets its turn.
    """
    aliases = PROFILES["screenset_button_matrix"]["group_aliases"]
    vals = [s.cell(r, entity_col) for r in rows_in_body if s.cell(r, entity_col)]
    if len(vals) < 2:
        return False
    hits = sum(1 for v in vals if matches_alias(v, aliases))
    return hits >= max(2, len(vals) * 0.6)


def _transpose(sheet: Sheet) -> Sheet:
    """Flip rows and columns, keeping the sheet's name."""
    s = sheet.normalized()
    return Sheet(sheet.name, [[s.cell(r, c) for r in range(s.n_rows)] for c in range(s.n_cols)])


def _find_transposed(wb: Workbook) -> tuple[str, Sheet, _MatrixLayout] | None:
    """Find a matrix in a sheet that is pivoted the other way round.

    Also rescues the vertical key/value shape — ``Screenset | <name>`` followed
    by one ``group | button`` row per line — since transposing that yields an
    ordinary one-row matrix.
    """
    for sheet in wb.sheets:
        # A genuinely pivoted grid is short and wide — groups down the side,
        # screen sets across the top. Flipping a tall record table instead
        # turns its column headers into "screen sets" ("Screen Number",
        # "Button") and invents a grid out of a report, so leave those alone.
        norm = sheet.normalized()
        if norm.n_rows > _MAX_TRANSPOSE_ROWS and norm.n_rows > norm.n_cols:
            continue
        flipped = _transpose(sheet)
        layout = _find_matrix(flipped)
        if layout is None:
            continue
        body = [r for r in range(layout.first_data_row, flipped.n_rows)
                if flipped.cell(r, layout.entity_col)]
        if not _entity_col_is_plausible(flipped, layout.header_row, layout.entity_col, body):
            continue
        return sheet.name, flipped, layout
    return None


def _no_targets_message(wb: Workbook) -> str:
    return (
        "No expected changes could be located in this plan. The parser looked "
        "for a screen-set column beside daypart columns of button numbers, a "
        "flat screen-set/button list, a sheet per screen set, and the "
        "transposed form of each — and found none of them. Sheets seen: "
        + ", ".join(f"{s.name!r} ({s.n_rows}x{s.n_cols})" for s in wb.sheets)
        + ". If the layout is unusual, export a plan template from any result "
        "page, fill it in, and upload that JSON instead."
    )


def _read_matrix(plan: Plan, sheet: Sheet, layout: _MatrixLayout) -> None:
    s = sheet.normalized()
    plan.groups = [layout.groups[c] for c in sorted(layout.groups)]
    seen: set[tuple[str, str]] = set()

    for r in range(layout.first_data_row, s.n_rows):
        entity = s.cell(r, layout.entity_col)
        if not entity:
            continue
        # A repeat of the header inside the body (frozen panes, re-printed
        # headers) must not become an entity.
        if matches_alias_exact(entity, PROFILES["screenset_button_matrix"]["entity_aliases"]):
            continue
        for c in sorted(layout.groups):
            cell = s.cell(r, c)
            if not cell:
                continue
            buttons = _button_values(cell)
            if not buttons:
                if not _BLANK_WORDS.match(cell):
                    plan.warnings.append(
                        f"{sheet.name}!R{r + 1}C{c + 1}: {cell!r} is not a button "
                        f"number and was ignored ({entity} / {layout.groups[c]})."
                    )
                continue
            for btn in buttons:
                key = (entity, btn)
                if key in seen:
                    plan.warnings.append(
                        f"{entity}: button {btn} is listed more than once in the plan "
                        f"(also under {layout.groups[c]}); counted once."
                    )
                    continue
                seen.add(key)
                plan.targets.append(
                    Target(
                        entity=entity,
                        group=layout.groups[c],
                        button=btn,
                        source=f"{sheet.name}!R{r + 1}C{c + 1}",
                    )
                )


#: Separators between several button numbers packed into one cell.
_MULTI_SPLIT = re.compile(r"[,;/|&\n]+|\s+and\s+", re.I)

#: Cell values that explicitly mean "nothing to do here".
_BLANK_WORDS = re.compile(r"^(n/?a|none|nil|tbd|tba|skip|no change|-{1,2}|—|\.)$", re.I)


def _one_button(part: str) -> str | None:
    """One button number from a single token, or None.

    Deliberately strict: it accepts ``12``, ``12.0`` (Excel numerics),
    ``Button 12``, ``#12`` and ``12 (new)``, but rejects anything else so that
    a ticket reference like ``T-2`` or a date fragment never becomes a button.
    """
    part = part.strip()
    part = re.sub(r"\s*\([^)]*\)\s*$", "", part)      # drop a trailing "(new)"
    part = part.strip().strip("()[]{}#:")
    if not part or _BLANK_WORDS.match(part):
        return None
    if re.fullmatch(r"\d{1,4}(?:\.0+)?", part):
        n = int(float(part))
    else:
        m = re.fullmatch(
            r"(?:button|btn|tile|slot|position|pos)\s*(?:no\.?|number|#)?\s*[:\-]?\s*"
            r"(\d{1,4})(?:\.0+)?",
            part, re.I,
        )
        if not m:
            return None
        n = int(m.group(1))
    return str(n) if 0 < n <= MAX_BUTTON else None


def _button_values(cell: str) -> list[str]:
    """Every button number in a cell.

    A single cell may legitimately carry a list — ``"12, 30, 53, 76"`` — so a
    matrix cell can expand into several expected changes rather than one.
    """
    txt = (cell or "").strip()
    if not txt or _BLANK_WORDS.match(txt):
        return []
    out: list[str] = []
    for part in _MULTI_SPLIT.split(txt):
        n = _one_button(part)
        if n and n not in out:
            out.append(n)
    return out


def _is_button_cell(cell: str) -> bool:
    return bool(_button_values(cell))


# ---------------------------------------------------------------------------
# Flat plan fallback
# ---------------------------------------------------------------------------


class _FlatLayout:
    def __init__(self, header_row, entity_col, button_col, group_col, action_col):
        self.header_row = header_row
        self.entity_col = entity_col
        self.button_col = button_col
        self.group_col = group_col
        self.action_col = action_col


def _find_flat_targets(
    wb: Workbook, roles: dict[str, str]
) -> list[tuple[Sheet, _FlatLayout]] | None:
    """Every sheet that holds a flat list of expected changes.

    Returns a list because a plan is often split one tab per screen set, with
    the tab name carrying the entity instead of a column.
    """
    prof = PROFILES["explicit_target_list"]
    found: list[tuple[Sheet, _FlatLayout]] = []
    for sheet in wb.sheets:
        s = sheet.normalized()
        for r in range(min(s.n_rows, HEADER_SCAN_ROWS)):
            entity_col = button_col = group_col = action_col = None
            for c in range(s.n_cols):
                head = s.cell(r, c)
                if not head:
                    continue
                if entity_col is None and matches_alias(head, prof["entity_aliases"]):
                    entity_col = c
                elif button_col is None and matches_alias(head, BUTTON_ALIASES):
                    button_col = c
                elif group_col is None and matches_alias(head, prof["group_aliases"]):
                    group_col = c
                elif action_col is None and matches_alias(head, prof["action_aliases"]):
                    action_col = c
            if button_col is None:
                continue
            has_data = any(
                _is_button_cell(s.cell(rr, button_col))
                and (entity_col is None or s.cell(rr, entity_col))
                for rr in range(r + 1, s.n_rows)
            )
            if not has_data:
                continue
            if entity_col is None:
                # No entity column: the sheet itself names the screen set. Only
                # trust that when the tab name plausibly *is* an entity — a
                # section heading like "Manual Configuration" is not, and taking
                # it would invent a screen set that does not exist.
                if roles.get(sheet.name) in ("instructions", "validation", "assignment"):
                    continue
                if matches_alias_exact(sheet.name, prof["entity_aliases"]):
                    continue
                if matches_alias(sheet.name, GENERIC_SHEET_NAMES):
                    continue
            found.append((sheet, _FlatLayout(r, entity_col, button_col, group_col, action_col)))
            break
    return found or None


def _read_flat(plan: Plan, sheet: Sheet, ly: _FlatLayout, groups: list[str]) -> None:
    s = sheet.normalized()
    for r in range(ly.header_row + 1, s.n_rows):
        # With no entity column the sheet name is the entity for every row.
        entity = s.cell(r, ly.entity_col) if ly.entity_col is not None else sheet.name
        buttons = _button_values(s.cell(r, ly.button_col))
        if not entity or not buttons:
            continue
        group = s.cell(r, ly.group_col) if ly.group_col is not None else ""
        action = detect_action(s.cell(r, ly.action_col)) if ly.action_col is not None else None
        if group and group not in groups:
            groups.append(group)
        for btn in buttons:
            plan.targets.append(
                Target(
                    entity=entity,
                    group=group or "—",
                    button=btn,
                    expect_state="Enable" if action == "enable" else "Disable",
                    from_state="Disable" if action == "enable" else "Enable",
                    source=f"{sheet.name}!R{r + 1}",
                )
            )


# ---------------------------------------------------------------------------
# Context harvesting
# ---------------------------------------------------------------------------

_SCREEN_RE = re.compile(r"screen\s*(?:no\.?|number|#)?\s*[:\-]?\s*(\d{4,6})", re.I)
_PAREN_RE = re.compile(r"\(([^)]{4,60})\)")
#: "Cleanup for NEW McCafé Specialty Drinks" -> the tile label.
#: The connector (for/of/:) is mandatory, otherwise a banner heading such as
#: "CLEANUP INSTRUCTION(PROD)" would be captured as the tile name.
_ACTION_LINE_RE = re.compile(
    r"^\s*(?:cleanup|clean\s*up|clean|disable|remove|enable|add|activate|deactivate|hide|show)"
    r"\s*(?:for|of|on|the)\s+(.+?)\s*$",
    re.I,
)

#: Words that mean a captured string is plan boilerplate, not a tile name.
_LABEL_NOISE = (
    "instruction", "prod)", "(prod", "note:", "kindly refer", "sheet for",
    "assigned coop", "validation way", "audit log", "laptop load", "p2p",
    "your assigned", "workflow", "daypart",
)


def _harvest_meta(plan: Plan, wb: Workbook, roles: dict[str, str]) -> None:
    meta = plan.meta
    prof = PROFILES.get(plan.profile, {})
    meta.operation = prof.get("expected_operation", "") or ""

    instr_lines: list[str] = []
    valid_lines: list[str] = []

    for s in wb.sheets:
        role = roles.get(s.name)
        if role == "matrix":
            continue
        lines = _text_lines(s)
        if role == "validation":
            valid_lines.extend(lines)
        elif role == "instructions":
            instr_lines.extend(lines)

    # If no sheet was tagged, treat every non-matrix line as instruction text.
    if not instr_lines and not valid_lines:
        for s in wb.sheets:
            if roles.get(s.name) != "matrix":
                instr_lines.extend(_text_lines(s))

    meta.instructions = instr_lines
    meta.validation_rules = valid_lines
    all_lines = instr_lines + valid_lines

    # -- screen number + name ---------------------------------------------
    for ln in all_lines:
        m = _SCREEN_RE.search(ln)
        if m and not meta.screen_number:
            meta.screen_number = m.group(1)
            tail = ln[m.end():]
            p = _PAREN_RE.search(tail) or _PAREN_RE.search(ln)
            if p:
                meta.screen_name = p.group(1).strip()
            break

    # -- action + tile label ----------------------------------------------
    for ln in all_lines:
        act = detect_action(ln)
        if not act:
            continue
        if not meta.action or meta.action == "disable":
            meta.action = act
        if meta.tile_label:
            continue
        m = _ACTION_LINE_RE.match(ln)
        if not m:
            continue
        label = _clean_label(m.group(1))
        if _plausible_label(label):
            meta.tile_label = label
            meta.action = act

    if not meta.tile_label:
        # Last resort: the longest quoted string anywhere in the prose.
        cands = [
            _clean_label(q) for ln in all_lines
            for q in re.findall(r'"([^"]{3,80})"', ln)
        ]
        cands = [c for c in cands if _plausible_label(c)]
        if cands:
            meta.tile_label = max(cands, key=len)

    # -- group -> workflow map --------------------------------------------
    meta.group_workflows, wf_conflicts = _find_workflow_map(wb, roles, plan.groups)
    for group, values in wf_conflicts.items():
        plan.warnings.append(
            f"The plan's workflow table lists {group!r} more than once, with "
            f"different workflows ({', '.join(values)}). The first ({values[0]}) "
            f"was used. This usually means a daypart row was mislabelled."
        )
    for t in plan.targets:
        t.workflow = meta.group_workflows.get(t.group, "")


def _plausible_label(label: str) -> bool:
    """Reject plan boilerplate that happens to follow an action verb."""
    if not label or not (2 < len(label) < 90):
        return False
    low = label.lower()
    if any(n in low for n in _LABEL_NOISE):
        return False
    if _SCREEN_RE.search(label):
        return False
    # A label needs at least one real word, not just numbers and punctuation.
    return any(ch.isalpha() for ch in label)


def _clean_label(text: str) -> str:
    """Tidy a tile/label caption pulled out of instruction prose."""
    s = text.replace("\\r", " ").replace("\\n", " ").replace("\xa0", " ")
    s = s.strip(" \t:-–—\"'")
    # Drop a trailing noise word that plans append to the product name.
    s = re.sub(r"\s+(tile|button|tiles|buttons|category)\s*$", "", s, flags=re.I)
    return " ".join(s.split())


def _text_lines(sheet: Sheet) -> list[str]:
    """Every distinct non-empty cell of a prose sheet, in reading order."""
    out: list[str] = []
    seen: set[str] = set()
    for _, row in sheet.non_empty_rows():
        cells: list[str] = []
        for c in row:
            v = c.strip()
            # Unmerging a title cell repeats its text across the span; collapse it.
            if v and (not cells or cells[-1] != v):
                cells.append(v)
        if not cells:
            continue
        line = "  ".join(cells)
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out


def _find_workflow_map(
    wb: Workbook, roles: dict[str, str], groups: list[str]
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Find a two-column ``group name -> workflow/screen number`` table.

    Returns the map plus any group that was listed twice with *different*
    workflow numbers, which is a plan-authoring slip worth reporting.
    """
    collected: dict[str, list[str]] = {}
    group_keys = {norm_label(g): g for g in groups}
    for s in wb.sheets:
        if roles.get(s.name) == "matrix":
            continue
        grid = s.normalized()
        for r in range(grid.n_rows):
            cells = [grid.cell(r, c) for c in range(grid.n_cols) if grid.cell(r, c)]
            # Merged title cells duplicate a value across the row; collapse them.
            deduped: list[str] = []
            for v in cells:
                if not deduped or deduped[-1] != v:
                    deduped.append(v)
            if len(deduped) < 2:
                continue
            left, right = deduped[0], deduped[1]
            key = norm_label(left)
            if key in group_keys and re.match(r"^\d{3,6}$", right):
                bucket = collected.setdefault(group_keys[key], [])
                if right not in bucket:
                    bucket.append(right)

    out = {g: vals[0] for g, vals in collected.items()}
    conflicts = {g: vals for g, vals in collected.items() if len(vals) > 1}
    return out, conflicts


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------


def _read_assignments(plan: Plan, wb: Workbook, roles: dict[str, str]) -> None:
    sheet = next((s for s in wb.sheets if roles.get(s.name) == "assignment"), None)
    if sheet is None:
        return
    s = sheet.normalized()
    prof = PROFILES["screenset_button_matrix"]

    for r in range(min(s.n_rows, HEADER_SCAN_ROWS)):
        cols: dict[str, int] = {}
        status_cols: list[int] = []
        for c in range(s.n_cols):
            head = s.cell(r, c)
            if not head:
                continue
            if "entity" not in cols and matches_alias(head, prof["entity_aliases"]):
                cols["entity"] = c
            elif "assignee" not in cols and matches_alias(head, ASSIGNEE_ALIASES):
                cols["assignee"] = c
            elif "validator" not in cols and matches_alias(head, VALIDATOR_ALIASES):
                cols["validator"] = c
            elif matches_alias(head, STATUS_ALIASES):
                status_cols.append(c)
        if "entity" not in cols:
            continue

        # A Status column belongs to whichever people-column precedes it.
        def status_for(owner: str) -> int | None:
            base = cols.get(owner)
            if base is None:
                return None
            after = [c for c in status_cols if c > base]
            return min(after) if after else None

        a_status, v_status = status_for("assignee"), status_for("validator")
        if a_status is not None and a_status == v_status:
            v_status = None

        for rr in range(r + 1, s.n_rows):
            entity = s.cell(rr, cols["entity"])
            if not entity or matches_alias_exact(entity, prof["entity_aliases"]):
                continue
            plan.assignments.append(
                Assignment(
                    entity=entity,
                    assignee=s.cell(rr, cols["assignee"]) if "assignee" in cols else "",
                    assignee_status=s.cell(rr, a_status) if a_status is not None else "",
                    validator=s.cell(rr, cols["validator"]) if "validator" in cols else "",
                    validator_status=s.cell(rr, v_status) if v_status is not None else "",
                )
            )
        return


# ---------------------------------------------------------------------------
# Sanity checks on the plan itself
# ---------------------------------------------------------------------------


def _reject_implausible(plan: Plan) -> None:
    """Refuse a parse that produced structurally nonsense targets.

    Structure detection is deliberately permissive, which means a plan built
    around something *other* than screen-set buttons — menu-item display orders,
    image-name tables, sell-location flags — can still satisfy the shape rules
    and yield a confident, wrong answer. Reporting a screen set called "25710"
    with buttons "3228, 3171" is worse than reporting nothing, so the result is
    sanity-checked before it is handed to the validator.
    """
    entities = plan.entities
    if not entities:
        return

    numeric = [e for e in entities if re.fullmatch(r"\d{1,6}(\.\d+)?", e.strip())]
    if len(numeric) >= max(1, len(entities) * 0.6):
        raise PlanParseError(
            "This plan does not look like a screen-set button plan. The parser "
            "ended up treating numbers as screen-set names ("
            + ", ".join(repr(e) for e in numeric[:4])
            + "), which usually means the sheet is a menu-item or image table "
            "rather than a screen-set / button grid."
        )

    if plan.profile == "screenset_button_matrix" and plan.groups:
        num_groups = [g for g in plan.groups if re.fullmatch(r"\d{1,6}", g.strip())]
        if len(num_groups) == len(plan.groups):
            raise PlanParseError(
                "This plan does not look like a screen-set button plan. The "
                "columns taken as dayparts are all numbers ("
                + ", ".join(repr(g) for g in plan.groups[:4])
                + "), so the sheet is probably a lookup table, not a button grid."
            )

    boilerplate = [e for e in entities if matches_alias(e, GENERIC_SHEET_NAMES)]
    if boilerplate:
        raise PlanParseError(
            "This plan does not look like a screen-set button plan. A section "
            "heading was taken as a screen-set name ("
            + ", ".join(repr(e) for e in boilerplate[:3])
            + ")."
        )


def _sanity_check(plan: Plan) -> None:
    if not plan.targets:
        raise PlanParseError("The plan parsed successfully but contains no expected changes.")

    _reject_implausible(plan)

    # Duplicate group names pointing at the same button for one entity is
    # already handled; here we flag plan-side data-entry slips.
    by_entity: dict[str, list[Target]] = {}
    for t in plan.targets:
        by_entity.setdefault(t.entity, []).append(t)

    counts = {len(v) for v in by_entity.values()}
    if len(counts) > 1 and plan.profile == "screenset_button_matrix":
        expected = max(counts, key=lambda n: sum(1 for v in by_entity.values() if len(v) == n))
        odd = [e for e, v in by_entity.items() if len(v) != expected]
        if odd:
            plan.warnings.append(
                f"{len(odd)} entit{'y' if len(odd) == 1 else 'ies'} do not have the "
                f"usual {expected} button(s): "
                + ", ".join(f"{e} ({len(by_entity[e])})" for e in odd[:6])
                + ("…" if len(odd) > 6 else "")
            )

    # A group named in the matrix but missing from the workflow table, or a
    # workflow table that repeats a group, is worth surfacing.
    if plan.meta.group_workflows:
        missing = [g for g in plan.groups if g not in plan.meta.group_workflows]
        if missing:
            plan.warnings.append(
                "The plan's workflow table does not cover: " + ", ".join(missing)
                + ". Those groups are still validated; only the workflow number is unknown."
            )
