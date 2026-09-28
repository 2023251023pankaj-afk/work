"""
Write DEPLOYMENT-PLAN-TEMPLATE.xlsx — the one workbook the team fills in for
any deployment: kiosk, POS, menu item, McValue, restaurant or anything else.

    python tools/make_plan_template.py            # -> DEPLOYMENT-PLAN-TEMPLATE.xlsx

The layout (tabs, columns, dropdown lists) is defined once, in
auditmaster/plan_template.py, which is also what reads a filled-in copy back.
Change a column there and both the blank template and the reader follow.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auditmaster.plan_schema import Plan                                      # noqa: E402
from auditmaster.plan_template import (                                       # noqa: E402
    DETAILS, DETAILS_TAB, EXAMPLES_TAB, GUIDE_TAB, KIOSK, KIOSK_GRID, MCVALUE, MENU_ITEM,
    OTHER, PLAN_TABS, POS, RESTAURANT, SCOPE_COLUMNS, SCOPE_TAB, SIGN_OFF, STEP_COLUMNS,
    STEPS_TAB, Column, Tab,
)
from tools.xlsx_writer import Dropdown, SheetSpec, col, write_xlsx           # noqa: E402

INPUT_ROWS = 300          # bordered blank rows on each fill-in tab
BLUE, GREEN, GREY = "2F5D8A", "2E7D32", "6B7280"

#: One or two filled-in rows per tab, each a real change from the sample audit
#: logs, keyed by column. A test re-checks every one against those logs, so an
#: example can never drift into something the system would not record.
EXAMPLES: dict[str, list[dict[str, str]]] = {
    KIOSK_GRID.name: [
        {"where": "Fairway - Portfolio A", "Breakfast": "12", "Lunch": "53", "Dinner": "30",
         "Latenight": "76", "becomes": "Disable", "screen_number": "61000",
         "screen": "Left hand navigation", "notes": "McCafé Specialty Drinks clean-up"},
        {"where": "07 - DSOA", "Breakfast": "11", "Lunch": "53", "Dinner": "30", "Latenight": "76",
         "becomes": "Disable", "screen_number": "61000", "screen": "Left hand navigation",
         "notes": "EVM realignment"},
    ],
    KIOSK.name: [
        {"where": "02 - TSMOA", "screen_number": "62003",
         "screen": "Kiosk 6 Breakfast Menu - Limited Time Promotion", "group": "Breakfast",
         "button": "15", "menu_item": "25702", "name": "FIFA World Cup Sausage Egg McMuffin Meal",
         "what": "Menu item number", "was": "25702", "becomes": "(blank)",
         "notes": "FIFA clean-up: take the item off the button"},
        {"where": "Fairway - Portfolio A", "screen_number": "61000", "screen": "Left hand navigation",
         "group": "Breakfast", "button": "12", "what": "Caption", "language": "All",
         "was": "NEW McCafé Specialty Drinks", "becomes": "new btn"},
        {"where": "01 - WWOA", "screen_number": "61000", "screen": "Left hand navigation",
         "group": "Dinner", "button": "31", "what": "On click",
         "becomes": "WF_CHF_ShowScreen 61125 false", "notes": "Tile opens the dinner LTO screen"},
    ],
    POS.name: [
        {"where": "02 - TSMOA", "screen_number": "384", "screen": "Collectors Meal", "button": "44",
         "menu_item": "25882", "name": "FIFA World Cup Mix & Match Snack Wrap Meal",
         "what": "Menu item number", "was": "25882", "becomes": "(blank)",
         "notes": "The caption and image on the button are cleared with it"},
        {"where": "02 - TSMOA", "screen_number": "384", "screen": "Collectors Meal", "button": "44",
         "what": "Caption", "language": "All", "becomes": "new btn"},
    ],
    MENU_ITEM.name: [
        {"menu_item": "1", "name": "Hamburger", "what": "Display Order", "was": "751", "becomes": "120",
         "notes": "Display order HQ level change"},
        {"menu_item": "25638", "name": "Godzilla x Hello Kitty Toy", "what": "Name",
         "was": "Happy Meal Toy 7", "becomes": "Godzilla x Hello Kitty Toy", "notes": "New toy"},
        {"menu_item": "25638", "name": "Godzilla x Hello Kitty Toy", "what": "Sell Location(Mobile)",
         "was": "False", "becomes": "True"},
        {"menu_item": "25637", "name": "BT21 Toy", "what": "Default Value(removeFromMOT)",
         "was": "false", "becomes": "true", "notes": "Previous toy taken off the menu"},
        {"menu_item": "10000014", "name": "Happy Meal toy choice", "what": "Order Components 25638",
         "was": "5", "becomes": "1", "notes": "New toy first in the choice"},
    ],
    MCVALUE.name: [
        {"kind": "Kiosk", "where": "04 - GAAF", "screen": "Kiosk 6 Dinner Menu - McValue Menu",
         "button": "85", "menu_item": "462", "name": "4 Spicy Chicken McNuggets",
         "what": "Menu item number", "becomes": "462", "notes": "New McValue button"},
        {"kind": "Kiosk", "where": "04 - GAAF", "screen": "Kiosk 6 Dinner Menu - McValue Menu",
         "button": "85", "what": "Button state", "was": "Disable", "becomes": "Enable"},
        {"kind": "Price", "where": "44126 Price List 090426", "store": "44126", "what": "Status",
         "effective": "04-Sep-2026", "was": "Inactive", "becomes": "Active",
         "notes": "New price list goes live"},
    ],
    RESTAURANT.name: [
        {"store": "44126", "name": "Winder", "what": "Kiosk Breakfast Hours",
         "was": "04:00|10:29", "becomes": "05:00|10:29"},
        {"store": "44126", "name": "Winder", "what": "Status", "menu_item": "50",
         "was": "Inactive", "becomes": "Active", "notes": "Small Unsweetened Iced Tea back on sale"},
    ],
    OTHER.name: [
        {"kind": "User", "where": "ed046072", "what": "Status", "was": "Active", "becomes": "Inactive"},
        {"kind": "Package schedule", "store": "453", "what": "Restaurant", "becomes": "453",
         "notes": "Ad hoc package for store 453"},
    ],
}

DEFAULT_STEPS = [
    ["Pre-work", "1", "Take a screenshot of each screen before changing it.", "Yes"],
    ["Deployment", "1", "Open the instance named on Deployment details (Prod or Pre-Prod).", ""],
    ["Deployment", "2", "Open your assigned screen set or store at the level on Deployment details.", ""],
    ["Deployment", "3", "Make every change listed on this plan's tabs, one row at a time.", ""],
    ["Deployment", "4", "Save, and take a screenshot of the result.", "Yes"],
    ["Validation", "1", "Export the audit log covering your screen sets or stores.", ""],
    ["Validation", "2", "Upload this plan and the audit log to Audit Master. It should say "
                        "“Work matches the plan”.", ""],
    ["Rollback", "1", "If the work has to be undone, follow the Rollback plan on Deployment details.", ""],
]


# ---------------------------------------------------------------------------
# Sheets
# ---------------------------------------------------------------------------


def _table_sheet(name: str, intro: str, columns: tuple[Column, ...], data: list[list[str]],
                 color: str, input_rows: int = INPUT_ROWS) -> SheetSpec:
    """A fill-in tab: an intro line, a heading row, then bordered input rows."""
    n = len(columns)
    last = 2 + max(input_rows, len(data))
    sh = SheetSpec(
        name,
        rows=[[intro] + [""] * (n - 1),
              [c.header + (" *" if c.required else "") for c in columns]] + data,
        row_styles={0: "intro", 1: "head"},
        cell_styles={(1, i): "head_req" for i, c in enumerate(columns) if c.required},
        grid=(2, last - 2, n, "cell"),
        widths=[c.width for c in columns],
        heights={0: 36, 1: 32},
        merges=[f"A1:{col(n - 1)}1"],
        freeze_rows=2,
        tab_color=color,
    )
    for i, c in enumerate(columns):
        prompt = c.hint
        if c.choices and not prompt:
            prompt = "Pick from the list, or type it exactly as the audit log's Field column shows it."
        if c.choices or prompt:
            sh.dropdowns.append(Dropdown(f"{col(i)}3:{col(i)}{last}", c.choices, c.header, prompt))
    return sh


def start_here() -> SheetSpec:
    tabs = {
        KIOSK_GRID.name: ("The usual kiosk clean-up: one row per screen set, a button number "
                          "under each daypart.",
                          "07 - DSOA: 11 / 53 / 30 / 76 → Disable."),
        KIOSK.name: ("Any other kiosk change, one row per button: captions, images, menu items, "
                     "On click.",
                     "Take item 25702 off button 15 on the breakfast LTO screen."),
        POS.name: ("POS screens: taking menu items off buttons or putting them on.",
                   "Take item 25882 off button 44 on Collectors Meal."),
        MENU_ITEM.name: ("The master menu item list and menu item sets: display order, names, "
                         "sell locations, images, removeFromMOT, choice components.",
                         "A new Happy Meal toy: its names, sell locations and place in the choice."),
        MCVALUE.name: ("A McValue launch: kiosk buttons, menu items, price lists — all in one "
                       "tab, with a Channel per row.",
                       "Turn on button 85 on the McValue Menu screen with item 462."),
        RESTAURANT.name: ("One store's settings: kiosk hours, features, time zone, price sets, "
                          "a menu item's status at that store.",
                          "Kiosk breakfast hours of store 44126 to 05:00|10:29."),
        OTHER.name: ("Anything else in the audit log: users, package schedules, media files.",
                     "Set user ed046072 to Inactive."),
    }
    rows: list[list[str]] = [
        ["Deployment plan template", "", ""],
        ["Fill in one copy of this workbook for each deployment. Audit Master reads it exactly "
         "as written and checks every row against the audit log.", "", ""],
        ["", "", ""],
        ["How to fill it in", "", ""],
        ["1", DETAILS_TAB, "Fill in every row marked Required."],
        ["2", SCOPE_TAB, "List every screen set or store, who does it and who checks it. "
                         "Mark any that must not be changed as “No - do not touch”."],
        ["3", STEPS_TAB, "The steps the deployer follows, and how the work is validated."],
        ["4", "Your portfolio tab", "One row per change, on the tab for that kind of work. A "
                                    "deployment that spans several kinds uses several tabs. "
                                    "Leave the others empty."],
        ["5", "Upload", "Save, then upload this workbook with the audit log to Audit Master."],
        ["", "", ""],
        ["Which tab do I use?", "", ""],
    ]
    for name, (use, example) in tabs.items():
        rows.append(["", name, f"{use}  e.g. {example}"])
    rows += [
        ["", EXAMPLES_TAB, "A filled-in row for every tab. Copy the pattern, not the rows — "
                           "Audit Master never reads that tab."],
        ["", "", ""],
        ["Rules that stop things being missed", "", ""],
        ["•", "One row per change", "Two buttons are two rows."],
        ["•", "Names as the audit log shows them", "Write 02 - TSMOA, not Coop 2. Prod and "
                                                       "Pre-Prod are different places."],
        ["•", "What to change", "Pick from the list. The list uses the audit log's own "
                                     "field names; if yours is missing, type it exactly as the "
                                     "log's Field column shows it."],
        ["•", "Should become", "The value after the change. Write (blank) if it should be "
                                    "cleared."],
        ["•", "Language", "Leave blank, or write All, when every language changes the "
                               "same way."],
        ["•", "Headings", "Don't rename or delete the column headings. Add as many rows as "
                               "you need."],
        ["•", "Orange headings", "Required. Blue headings are optional but make the check "
                                      "more precise."],
        ["", "", ""],
        ["Before you send it", "", ""],
        ["☐", "", "Every Required row on Deployment details is filled."],
        ["☐", "", "Every screen set or store is on Scope & assignment, with an assignee "
                       "and a validator."],
        ["☐", "", "Every change row has What to change and Should become."],
        ["☐", "", "Screen set and store names match the audit log exactly."],
        ["☐", "", "Tabs you don't need are empty."],
    ]
    sections = {r for r, row in enumerate(rows) if row[0] and not row[1] and not row[2]}
    sh = SheetSpec(GUIDE_TAB, rows, widths=[5, 30, 100], tab_color=GREY,
                   merges=["A1:C1", "A2:C2"], heights={1: 34})
    sh.row_styles = {0: "title", 1: "intro"}
    for r in sections - {0, 1}:
        sh.row_styles[r] = "section"
        sh.merges.append(f"A{r + 1}:C{r + 1}")
    for r, row in enumerate(rows):
        if r > 2 and r not in sections:
            sh.cell_styles[(r, 1)] = "label"
            sh.cell_styles[(r, 2)] = "wrap"
    return sh


def details_sheet(plan: Plan | None) -> SheetSpec:
    values: dict[str, str] = {}
    if plan is not None:
        m = plan.meta
        # What the plan already states wins; the rest is filled from what was
        # read out of an older-style plan.
        values = {d.key: m.details[d.label] for d in DETAILS + SIGN_OFF if m.details.get(d.label)}
        derived = {
            "name": plan.plan_name, "environment": m.environment,
            "screen_number": m.screen_number, "screen_name": m.screen_name,
            "tile_label": m.tile_label, "action": (m.action or "").title(),
            "portfolio": "Kiosk" if plan.targets else "",
        }
        for k, v in derived.items():
            if v and not values.get(k):
                values[k] = v

    rows = [["Deployment details", "", "", ""],
            ["Fill in column B. Rows marked Required must be filled before the plan is sent.", "", "", ""],
            ["Item", "Detail", "Required?", "What to write"]]
    sh = SheetSpec(DETAILS_TAB, rows, widths=[30, 46, 11, 62], freeze_rows=3, tab_color=BLUE,
                   merges=["A1:D1", "A2:D2"], row_styles={0: "title", 1: "intro", 2: "head"})

    def add(d, required_text: str) -> None:
        r = len(rows)
        rows.append([d.label, values.get(d.key, ""), required_text, d.hint])
        sh.cell_styles.update({(r, 0): "label", (r, 1): "input",
                               (r, 2): "req" if required_text == "Required" else "hint",
                               (r, 3): "hint"})
        if d.choices or d.hint:
            sh.dropdowns.append(Dropdown(f"B{r + 1}", d.choices, d.label, d.hint))

    for d in DETAILS:
        add(d, "Required" if d.required else "Optional")
    rows.append(["", "", "", ""])
    rows.append(["Sign-off — fill in after the work is done", "", "", ""])
    sh.row_styles[len(rows) - 1] = "section"
    for d in SIGN_OFF:
        add(d, "")
    return sh


def scope_sheet(plan: Plan | None) -> SheetSpec:
    data: list[list[str]] = []
    if plan is not None:
        for a in plan.assignments:
            data.append([a.entity, "Yes", a.assignee, a.assignee_status, a.validator,
                         a.validator_status, ""])
        for place in plan.do_not_touch:
            data.append([place, "No - do not touch", "", "", "", "", ""])
    return _table_sheet(
        SCOPE_TAB,
        "Every screen set or store in this deployment, one per row. Anything marked "
        "“No - do not touch” is flagged if the audit log shows a change there.",
        SCOPE_COLUMNS, data, BLUE, input_rows=120)


def steps_sheet(plan: Plan | None) -> SheetSpec:
    if plan is None:
        data = [list(r) for r in DEFAULT_STEPS]
    else:
        data = [["Deployment", "", line, ""] for line in plan.meta.instructions]
        data += [["Validation", "", line, ""] for line in plan.meta.validation_rules]
    return _table_sheet(
        STEPS_TAB,
        "The steps the deployer follows, in order, and how the work is validated. They "
        "travel with the plan; Audit Master checks the changes, not these steps.",
        STEP_COLUMNS, data, BLUE, input_rows=40)


def _rows_for(tab: Tab, plan: Plan | None) -> list[list[str]]:
    if plan is None:
        return []
    keyed: list[dict[str, str]] = []
    if tab is KIOSK:
        def order(t):
            return (t.entity, int(t.button) if t.button.isdigit() else 10**9, t.button)
        for t in sorted(plan.targets, key=order):
            keyed.append({
                "where": t.entity, "screen_number": t.workflow, "screen": plan.meta.screen_name,
                "group": t.group, "button": t.button, "what": "Button state",
                "was": t.from_state, "becomes": t.expect_state,
            })
    for ch in plan.changes:
        if ch.tab.lower().startswith(tab.name.lower()):
            keyed.append({c.key: getattr(ch, c.key, "") for c in tab.columns})
    return [[k.get(c.key, "") for c in tab.columns] for k in keyed]


def examples_sheet() -> SheetSpec:
    rows: list[list[str]] = [
        ["Examples", ""],
        ["How each tab is filled in. Audit Master never reads this tab — copy the pattern, "
         "not the rows.", ""],
    ]
    sh = SheetSpec(EXAMPLES_TAB, rows, widths=[22, 22, 16, 30, 16, 12, 16, 28, 26, 12, 24, 28, 34],
                   tab_color=GREY, merges=["A1:H1", "A2:M2"], row_styles={0: "title", 1: "intro"},
                   heights={1: 22})
    for tab in PLAN_TABS:
        rows.append([""])
        rows.append([tab.name])
        sh.row_styles[len(rows) - 1] = "section"
        rows.append([c.header + (" *" if c.required else "") for c in tab.columns])
        sh.row_styles[len(rows) - 1] = "head"
        for i, c in enumerate(tab.columns):
            if c.required:
                sh.cell_styles[(len(rows) - 1, i)] = "head_req"
        for ex in EXAMPLES[tab.name]:
            rows.append([ex.get(c.key, "") for c in tab.columns])
            for i in range(len(tab.columns)):
                sh.cell_styles[(len(rows) - 1, i)] = "example"
    return sh


def template_sheets(plan: Plan | None = None) -> list[SheetSpec]:
    """The whole workbook: blank, or filled from an already-parsed plan."""
    sheets = [start_here(), details_sheet(plan), scope_sheet(plan), steps_sheet(plan)]
    for tab in PLAN_TABS:
        sheets.append(_table_sheet(tab.name, tab.intro, tab.columns, _rows_for(tab, plan), GREEN))
    sheets.append(examples_sheet())
    return sheets


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", type=Path, default=Path("DEPLOYMENT-PLAN-TEMPLATE.xlsx"))
    args = ap.parse_args(argv)
    write_xlsx(args.out, template_sheets())
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
