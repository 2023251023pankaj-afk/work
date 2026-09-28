"""
The Audit Master deployment-plan template.

One workbook, one tab per portfolio (Kiosk, POS, Menu item, McValue,
Restaurant, Other), plus tabs for the deployment's details, its steps and who
does what. The tabs and their columns were drawn from the team's own plans —
the daypart button grid, the menu-item-per-screen tables, the assignment
sheets — and the dropdowns from the fields those deployments change in the
audit logs. This module is the single definition of that layout:
``tools/make_plan_template.py`` writes the blank workbook from it, and
:func:`read_template` reads a filled-in copy back into a :class:`Plan`.

Nothing is guessed when reading. Every column's meaning is fixed here, so a
plan written on the template reaches the validator exactly as its author wrote
it — which is the point of having a template at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .plan_schema import Assignment, Change, Plan, Target, fold
from .readers import Sheet, Workbook

PROFILE = "audit_master_template"
PROFILE_LABEL = "Audit Master plan template"

# -- dropdown lists ----------------------------------------------------------
LANGUAGES = ("All", "English", "SpanishUS", "French", "Cantonese", "Mandarin", "Deutsch")
DAYPARTS = ("Breakfast", "Lunch", "Dinner", "Latenight", "All day")
YES_NO = ("Yes", "No")

#: The "What to change" lists use the audit log's own field names, so what the
#: plan says and what the log records can be compared word for word.
KIOSK_WHAT = ("Button state", "Menu item number", "Caption", "Image name", "On click", "Width",
              "Localize", "Background normal color", "Foreground normal color",
              "Foreground pressed color", "Button assigned to")
POS_WHAT = ("Menu item number", "Button state", "Caption", "Image name", "Width")
MENU_WHAT = ("Display Order", "Name", "DT Name", "CSO Name", "CSO Generic Name", "Short Name",
             "Long Name", "Caption: Line 1", "Caption: Line 2", "Caption: Line 3",
             "Sell Location(Mobile)", "Sell Location(CSO)", "Sell Location(Delivery)",
             "Default Value(KioskBitMapName)", "Default Value(removeFromMOT)",
             "Image(Media)", "Grill Image(Media)", "CSO Small Image(Media)",
             "CSO Large Image(Media)", "CSO Cart Image(Media)", "Change Approval Status To",
             "Order Components", "Add Remove Components", "Status", "Auto Grill?",
             "Auto Condiment?", "Display Numbers Instead of Modifiers")
MCVALUE_CHANNELS = ("Kiosk", "POS", "Menu item", "Price")
MCVALUE_WHAT = ("Button state", "Menu item number", "Caption", "Image name", "On click", "Width",
                "Background normal color", "Price Set Name", "Status", "Price",
                "Display Order", "Name")
RESTAURANT_WHAT = ("Kiosk Breakfast Hours", "Kiosk Lunch Hours", "Non CYT Hours", "SP9-Kiosk6",
                   "Time Zone", "Phone Number", "Feature Assignment", "Set Assignment",
                   "Status", "Price Set Name")
OTHER_TYPES = ("User", "Package schedule", "Media file", "Price set", "Other")
OTHER_WHAT = ("Status", "Hierarchy Assignment", "Level", "Phone", "Restaurant", "Name")

#: Written in "Should become" to say the value is cleared.
BLANK_WORDS = {"blank", "empty", "clear", "cleared", "removed"}


@dataclass(frozen=True)
class Column:
    header: str
    key: str
    width: int = 16
    required: bool = False
    choices: tuple[str, ...] = ()
    hint: str = ""


@dataclass(frozen=True)
class Tab:
    name: str
    intro: str
    columns: tuple[Column, ...]
    kind: str = ""          # fixed channel for every row of this tab

    @property
    def headers(self) -> list[str]:
        return [c.header for c in self.columns]


def _change_cols(*cols: Column) -> tuple[Column, ...]:
    """Every change tab ends with the same four columns."""
    return cols + (
        Column("Was", "was", 20, hint="The value before the change, if you know it."),
        Column("Should become", "becomes", 24, True,
               hint="The value after the change. Write (blank) if it should be cleared."),
        Column("Notes", "notes", 30),
    )


GRID_DAYPARTS = ("Breakfast", "Lunch", "Dinner", "Latenight")

KIOSK_GRID = Tab(
    "Kiosk button grid",
    "The usual clean-up layout: one row per screen set, the button number under each daypart. "
    "Every button listed is set to Should become. Use Kiosk plan for anything else.",
    (
        Column("Screen set", "where", 22, True, hint="As the audit log names it, e.g. 07 - DSOA."),
    ) + tuple(Column(d, d, 11, hint=f"The {d.lower()} button number. Blank if none.") for d in GRID_DAYPARTS) + (
        Column("Should become", "becomes", 14, True, ("Disable", "Enable"),
               hint="Blank = the Action on Deployment details."),
        Column("Screen number", "screen_number", 15, hint="e.g. 61000"),
        Column("Screen name", "screen", 26, hint="e.g. Left hand navigation"),
        Column("Notes", "notes", 30),
    ),
    kind="Kiosk",
)

KIOSK = Tab(
    "Kiosk plan",
    "One row per kiosk button. To turn a button off: What to change = Button state, "
    "Should become = Disable. Caption and image changes on that button count with it.",
    _change_cols(
        Column("Screen set", "where", 22, True, hint="As the audit log names it, e.g. 02 - TSMOA."),
        Column("Store number", "store", 13, hint="Only for a change at one store. Blank for HQ / coop."),
        Column("Screen number", "screen_number", 15, hint="e.g. 61000"),
        Column("Screen name", "screen", 30, hint="e.g. Left hand navigation"),
        Column("Daypart", "group", 12, choices=DAYPARTS),
        Column("Button", "button", 9, True),
        Column("Menu item number", "menu_item", 14),
        Column("Menu item name", "name", 26),
        Column("What to change", "what", 20, True, KIOSK_WHAT),
        Column("Language", "language", 12, choices=LANGUAGES, hint="Blank or All = every language."),
    ),
    kind="Kiosk",
)

POS = Tab(
    "POS plan",
    "One row per POS button. To take an item off a button: What to change = Menu item number, "
    "Was = the item number, Should become = (blank).",
    _change_cols(
        Column("Screen set", "where", 22, True, hint="The coop / screen set, e.g. 04 - GAAF."),
        Column("Store number", "store", 13, hint="Only for a change at one store."),
        Column("Screen number", "screen_number", 15, hint="e.g. 384"),
        Column("Screen name", "screen", 26, hint="e.g. Collectors Meal"),
        Column("Button", "button", 9, True),
        Column("Menu item number", "menu_item", 14),
        Column("Menu item name", "name", 26),
        Column("What to change", "what", 20, True, POS_WHAT),
        Column("Language", "language", 12, choices=LANGUAGES),
    ),
    kind="POS",
)

MENU_ITEM = Tab(
    "Menu item plan",
    "One row per menu item setting. Leave Menu item set blank for the national master list.",
    _change_cols(
        Column("Menu item number", "menu_item", 14, True),
        Column("Menu item name", "name", 28),
        Column("Menu item set", "where", 20, hint="Blank = HQ master list. Otherwise e.g. 57 - GUAM."),
        Column("Store number", "store", 13, hint="Only for Status at one store."),
        Column("What to change", "what", 28, True, MENU_WHAT,
               hint="Pick from the list. For one component of a choice item write e.g. "
                    "Order Components 25638."),
    ),
    kind="Menu item",
)

MCVALUE = Tab(
    "McValue plan",
    "Everything for a McValue launch in one place: prices, menu items, kiosk tiles and POS "
    "buttons. Pick the Channel for each row.",
    _change_cols(
        Column("Channel", "kind", 12, True, MCVALUE_CHANNELS),
        Column("Screen set / price set", "where", 22, hint="Blank = HQ."),
        Column("Store number", "store", 12),
        Column("Screen name", "screen", 24, hint="Kiosk / POS rows only."),
        Column("Button", "button", 9, hint="Kiosk / POS rows only."),
        Column("Menu item number", "menu_item", 14),
        Column("Menu item name", "name", 26),
        Column("What to change", "what", 20, True, MCVALUE_WHAT),
        Column("Language", "language", 12, choices=LANGUAGES),
        Column("Effective from", "effective", 14, hint="e.g. 04-Sep-2026"),
    ),
)

RESTAURANT = Tab(
    "Restaurant plan",
    "One row per store setting: profile settings, hours, features, price sets, or a menu "
    "item's status at that store.",
    _change_cols(
        Column("Store number", "store", 12, True),
        Column("Store name", "name", 24),
        Column("What to change", "what", 26, True, RESTAURANT_WHAT),
        Column("Menu item number", "menu_item", 14, hint="Only when What to change = Status."),
    ),
    kind="Restaurant",
)

OTHER = Tab(
    "Other changes",
    "Anything else the audit log records: users, package schedules, media files.",
    _change_cols(
        Column("Type of change", "kind", 18, True, OTHER_TYPES),
        Column("Name or ID", "where", 26, hint="User ID, file name or price set. Blank for a package schedule."),
        Column("Store number", "store", 12),
        Column("What to change", "what", 24, True, OTHER_WHAT),
    ),
)

CHANGE_TABS = (KIOSK, POS, MENU_ITEM, MCVALUE, RESTAURANT, OTHER)
#: Every fill-in tab, in workbook order.
PLAN_TABS = (KIOSK_GRID,) + CHANGE_TABS

# -- the non-change tabs -----------------------------------------------------
DETAILS_TAB = "Deployment details"
STEPS_TAB = "Steps"
SCOPE_TAB = "Scope & assignment"
GUIDE_TAB = "Start here"
EXAMPLES_TAB = "Examples"


@dataclass(frozen=True)
class Detail:
    label: str
    key: str
    required: bool = False
    hint: str = ""
    choices: tuple[str, ...] = ()


DETAILS = (
    Detail("Deployment name", "name", True, "The short name everyone uses, e.g. FIFA Cleanup - Kiosk."),
    Detail("Request / ticket number", "ticket", False, "e.g. CHG0012345"),
    Detail("Portfolio", "portfolio", True, "Which tab(s) you filled in.",
           ("Kiosk", "POS", "Menu item", "McValue", "Restaurant", "Other", "More than one")),
    Detail("Environment", "environment", True, "Where the work is done.", ("Prod", "Pre-Prod", "Prod and Pre-Prod")),
    Detail("Level", "level", True, "The level the work is done at.", ("HQ / Market", "Coop", "Store")),
    Detail("What this deployment does", "summary", True, "One sentence, e.g. Remove FIFA meals from Collectors Meal."),
    Detail("Action", "action", True, "The main action.", ("Disable", "Enable", "Add", "Remove", "Update", "Replace")),
    Detail("Screen number", "screen_number", False, "If the work is on one screen, e.g. 61000."),
    Detail("Screen name", "screen_name", False, "e.g. Left hand navigation."),
    Detail("Tile or item being changed", "tile_label", False,
           "The caption as it reads today, e.g. NEW McCafé Specialty Drinks."),
    Detail("Raised by", "raised_by", False, "Who asked for this work."),
    Detail("Plan written by", "author", True, "Your name."),
    Detail("Deployment date", "date", True, "e.g. 17-Jun-2026"),
    Detail("Effective from", "effective", False, "If different from the deployment date."),
    Detail("Package generation needed?", "package", False, "", YES_NO),
    Detail("Rollback plan", "rollback", False, "What to do if it has to be undone."),
    Detail("How it will be validated", "validation", False, "",
           ("Audit log", "Audit log + P2P", "Audit log + screenshots")),
    Detail("Notes", "notes", False, ""),
)

SIGN_OFF = (
    Detail("Deployed by", "deployed_by"),
    Detail("Deployed on", "deployed_on"),
    Detail("Validated by", "validated_by"),
    Detail("Validated on", "validated_on"),
    Detail("Result", "result", choices=("Pass", "Pass with notes", "Fail")),
    Detail("Comments", "comments"),
)

STEP_COLUMNS = (
    Column("Stage", "stage", 14, True, ("Pre-work", "Deployment", "Validation", "Rollback")),
    Column("Step", "step", 7),
    Column("What to do", "text", 70, True),
    Column("Screenshot needed?", "shot", 12, choices=YES_NO),
)

SCOPE_COLUMNS = (
    Column("Screen set, store or item", "where", 24, True,
           hint="One per row: a screen set (02 - TSMOA), a store number, or a menu item number."),
    Column("In scope?", "scope", 20, True, ("Yes", "No - do not touch")),
    Column("Assignee", "assignee", 16),
    Column("Assignee status", "a_status", 14, choices=("Not started", "In progress", "Done")),
    Column("Validator", "validator", 16),
    Column("Validator status", "v_status", 14, choices=("Not started", "In progress", "Done")),
    Column("Notes", "notes", 30),
)

# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _key(text: str) -> str:
    return " ".join((text or "").replace("*", " ").replace("\xa0", " ").lower().split())


def _tab_for(sheet_name: str, tabs) -> Tab | None:
    """The template tab a sheet is, allowing Excel's "Kiosk plan (2)" copies."""
    k = _key(sheet_name)
    for tab in tabs:
        if k == _key(tab.name) or k.startswith(_key(tab.name) + " ("):
            return tab
    return None


def _header_map(s: Sheet, columns: tuple[Column, ...]) -> tuple[int, dict[str, int]] | None:
    """The header row and column positions, wherever the author moved them."""
    wanted = {_key(c.header): c.key for c in columns}
    need = [c for c in columns if c.required]
    for r in range(min(s.n_rows, 6)):
        found: dict[str, int] = {}
        for c in range(s.n_cols):
            key = wanted.get(_key(s.cell(r, c)))
            if key and key not in found:
                found[key] = c
        if need and sum(1 for c in need if c.key in found) * 2 >= len(need):
            return r, found
    return None


def looks_like_template(wb: Workbook) -> bool:
    names = {_key(s.name) for s in wb.sheets}
    if _key(DETAILS_TAB) in names:
        return True
    return any(_tab_for(s.name, PLAN_TABS) for s in wb.sheets)


def read_template(wb: Workbook, plan: Plan) -> None:
    """Fill ``plan`` from a workbook written on the template."""
    plan.profile, plan.profile_label = PROFILE, PROFILE_LABEL
    # Details first: a grid row with no "Should become" falls back to its Action.
    for sheet in wb.sheets:
        if _key(sheet.name) == _key(DETAILS_TAB):
            _read_details(plan, sheet.normalized())
            plan.sheet_roles[sheet.name] = "details"
    for sheet in wb.sheets:
        s = sheet.normalized()
        name = _key(sheet.name)
        if name == _key(DETAILS_TAB):
            continue
        if _tab_for(sheet.name, (KIOSK_GRID,)):
            if _read_grid(plan, s, sheet.name):
                plan.sheet_roles[sheet.name] = "changes"
        elif name == _key(STEPS_TAB):
            _read_steps(plan, s)
            plan.sheet_roles[sheet.name] = "instructions"
        elif name == _key(SCOPE_TAB):
            _read_scope(plan, s)
            plan.sheet_roles[sheet.name] = "assignment"
        else:
            tab = _tab_for(sheet.name, CHANGE_TABS)
            if tab is not None:
                if _read_changes(plan, s, sheet.name, tab):
                    plan.sheet_roles[sheet.name] = "changes"
    # The same button written on the grid and on Kiosk plan is one change.
    unique: dict[tuple[str, str], Target] = {}
    for t in plan.targets:
        unique.setdefault(t.key, t)
    plan.targets = list(unique.values())
    plan.groups = list(dict.fromkeys(t.group for t in plan.targets if t.group))


def _read_details(plan: Plan, s: Sheet) -> None:
    by_label = {_key(d.label): d for d in DETAILS + SIGN_OFF}
    meta = plan.meta
    for r in range(s.n_rows):
        d = by_label.get(_key(s.cell(r, 0)))
        value = s.cell(r, 1).strip()
        if d is None or not value:
            continue
        meta.details[d.label] = value
        if d.key == "name":
            plan.plan_name = value
        elif d.key == "environment":
            meta.environment = value
        elif d.key == "screen_number":
            digits = re.search(r"\d{3,6}", value)
            meta.screen_number = digits.group(0) if digits else value
        elif d.key == "screen_name":
            meta.screen_name = value
        elif d.key == "tile_label":
            meta.tile_label = value
        elif d.key == "action":
            meta.action = value.strip().lower()
    empty = [d.label for d in DETAILS if d.required and d.label not in meta.details]
    if empty:
        plan.warnings.append(
            f"{DETAILS_TAB}: {', '.join(empty)} "
            f"{'is' if len(empty) == 1 else 'are'} not filled in."
        )


def _read_steps(plan: Plan, s: Sheet) -> None:
    found = _header_map(s, STEP_COLUMNS)
    if not found:
        return
    head, cols = found
    for r in range(head + 1, s.n_rows):
        text = s.cell(r, cols["text"]) if "text" in cols else ""
        if not text:
            continue
        stage = s.cell(r, cols["stage"]) if "stage" in cols else ""
        step = s.cell(r, cols["step"]) if "step" in cols else ""
        line = f"{step}. {text}" if step else text
        if fold(stage) == "validation":
            plan.meta.validation_rules.append(line)
        else:
            plan.meta.instructions.append(f"[{stage}] {line}" if stage else line)


def _read_scope(plan: Plan, s: Sheet) -> None:
    found = _header_map(s, SCOPE_COLUMNS)
    if not found:
        return
    head, cols = found

    def get(r: int, key: str) -> str:
        return s.cell(r, cols[key]) if key in cols else ""

    for r in range(head + 1, s.n_rows):
        where = get(r, "where")
        if not where:
            continue
        if fold(get(r, "scope")).startswith("no"):
            plan.do_not_touch.append(where)
            continue
        plan.assignments.append(Assignment(
            entity=where,
            assignee=get(r, "assignee"),
            assignee_status=get(r, "a_status"),
            validator=get(r, "validator"),
            validator_status=get(r, "v_status"),
        ))


_BUTTON_SPLIT = re.compile(r"[,;/&]+|\s+and\s+", re.I)


def _buttons(cell: str) -> list[str]:
    """One button per row is the rule, but "12, 13" is forgiven."""
    parts = [p.strip() for p in _BUTTON_SPLIT.split(cell or "")]
    out = [p for p in parts if p]
    return out or [""]


def _state(value: str) -> str | None:
    v = fold(value)
    if v.startswith("dis"):
        return "Disable"
    if v.startswith("en"):
        return "Enable"
    return None


def _read_changes(plan: Plan, s: Sheet, sheet_name: str, tab: Tab) -> bool:
    found = _header_map(s, tab.columns)
    if not found:
        plan.warnings.append(
            f"Tab {sheet_name!r} has lost its column headings, so it was not read. "
            f"Copy the heading row back from a blank template."
        )
        return False
    head, cols = found
    required = [c for c in tab.columns if c.required and c.key != "becomes"]
    read_any = False

    for r in range(head + 1, s.n_rows):
        values = {key: s.cell(r, c).strip() for key, c in cols.items()}
        if not any(v for k, v in values.items() if k != "notes"):
            continue
        row_no = r + 1
        missing = [c.header for c in required if not values.get(c.key)]
        if missing:
            plan.warnings.append(
                f"{sheet_name} row {row_no}: {', '.join(missing)} "
                f"{'is' if len(missing) == 1 else 'are'} empty, so the row was skipped."
            )
            continue
        if not values.get("becomes"):
            plan.warnings.append(
                f"{sheet_name} row {row_no}: Should become is empty, so only the fact "
                f"that it changed is checked. Write (blank) if it should be cleared."
            )
        read_any = True
        for button in _buttons(values.get("button", "")):
            ch = Change(
                tab=sheet_name,
                row=row_no,
                kind=values.get("kind", "") or tab.kind,
                where=values.get("where", ""),
                store=values.get("store", ""),
                screen=values.get("screen", ""),
                screen_number=values.get("screen_number", ""),
                group=values.get("group", ""),
                button=button,
                menu_item=values.get("menu_item", ""),
                name=values.get("name", ""),
                what=values.get("what", ""),
                language=values.get("language", ""),
                was=values.get("was", ""),
                becomes=values.get("becomes", ""),
                effective=values.get("effective", ""),
                notes=values.get("notes", ""),
            )
            target = _as_target(ch)
            if target is not None:
                plan.targets.append(target)
            else:
                plan.changes.append(ch)
    return read_any


def _read_grid(plan: Plan, s: Sheet, sheet_name: str) -> bool:
    """Screen set x daypart -> button number, the layout most kiosk plans use."""
    found = _header_map(s, KIOSK_GRID.columns)
    if not found:
        plan.warnings.append(
            f"Tab {sheet_name!r} has lost its column headings, so it was not read."
        )
        return False
    head, cols = found
    fallback = _state(plan.meta.action)
    read_any = False
    for r in range(head + 1, s.n_rows):
        values = {key: s.cell(r, c).strip() for key, c in cols.items()}
        where = values.get("where", "")
        buttons = [(d, values.get(d, "")) for d in GRID_DAYPARTS if values.get(d)]
        if not where and not buttons:
            continue
        if not where or not buttons:
            plan.warnings.append(
                f"{sheet_name} row {r + 1}: "
                + ("Screen set is empty" if not where else "no button number under any daypart")
                + ", so the row was skipped."
            )
            continue
        want = _state(values.get("becomes", "")) or fallback
        if want is None:
            plan.warnings.append(
                f"{sheet_name} row {r + 1}: Should become is empty and the Action on "
                f"{DETAILS_TAB} is not Disable or Enable, so the row was skipped."
            )
            continue
        if values.get("screen") and not plan.meta.screen_name:
            plan.meta.screen_name = values["screen"]
        read_any = True
        for daypart, cell in buttons:
            for button in _buttons(cell):
                plan.targets.append(Target(
                    entity=where, group=daypart, button=button, expect_state=want,
                    from_state="Enable" if want == "Disable" else "Disable",
                    workflow=values.get("screen_number", ""),
                    source=f"{sheet_name}!R{r + 1}",
                ))
    return read_any


def _as_target(ch: Change) -> Target | None:
    """A plain button on/off at screen-set level goes to the button checker.

    That checker is the precise one for this job: it also recognises the
    caption and image rows that go with the button, and flags any other button
    touched on the same screen set.
    """
    if fold(ch.what) not in ("button state", "button") or ch.store or not ch.button:
        return None
    if fold(ch.kind) not in ("kiosk", "pos"):
        return None
    want = _state(ch.becomes)
    if want is None:
        return None
    was = _state(ch.was) or ("Enable" if want == "Disable" else "Disable")
    return Target(
        entity=ch.where,
        group=ch.group or ch.screen or ch.tab,
        button=ch.button,
        expect_state=want,
        from_state=was,
        workflow=ch.screen_number,
        source=f"{ch.tab}!R{ch.row}",
    )
