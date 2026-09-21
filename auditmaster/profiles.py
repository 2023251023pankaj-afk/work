"""
Plan profiles — the extension point for new deployment-plan shapes.

A profile is pure declaration: which words identify the entity column, which
identify the grouping columns, which sheet names carry which role. The parser
in :mod:`auditmaster.plan_parser` is generic and reads these tables, so
supporting a new plan layout usually means adding a profile here rather than
writing new parsing code.

Matching is always alias-based and fuzzy (case-insensitive, punctuation
ignored, substring allowed), because plan authors rename headers freely.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------

#: Words that identify the column holding the *thing being changed* — the
#: screen set / co-op / market / store whose name also appears in the audit
#: log's Description or Level Details.
ENTITY_ALIASES = [
    "screenset", "screen set", "screensets", "screen sets",
    "coop", "co-op", "co op", "coops",
    "market", "markets", "region", "portfolio",
    "store", "stores", "restaurant", "restaurants",
    "site", "sites", "location", "locations",
    "entity", "target", "nsn", "instance",
]

#: Words that identify a grouping column whose cells hold button numbers.
#: In McDonald's kiosk plans these are dayparts.
GROUP_ALIASES = [
    "breakfast", "lunch", "dinner", "latenight", "late night", "late-night",
    "allday", "all day", "all-day", "snack", "supper", "brunch", "evening",
    "morning", "afternoon", "overnight", "daypart", "day part",
]

#: Words in a header that mean "this cell holds a button number".
BUTTON_ALIASES = [
    "button", "button number", "button no", "button #", "btn", "button id",
    "tile", "tile number", "position", "slot",
]

#: Words that identify an action column in a flat plan.
ACTION_ALIASES = ["action", "operation", "change", "task", "activity", "state"]

#: Words that identify assignment columns.
ASSIGNEE_ALIASES = ["assignee", "assigned to", "owner", "developer", "resource", "deployer"]
VALIDATOR_ALIASES = ["validator", "validated by", "reviewer", "qa", "checker", "verifier"]
STATUS_ALIASES = ["status", "state", "progress", "completion"]


#: Verb -> canonical action. Order matters: the first hit in a line wins.
ACTION_KEYWORDS = [
    ("disable", ["disable", "disabled", "disabling", "cleanup", "clean up", "clean-up",
                 "remove", "removal", "hide", "turn off", "switch off", "deactivate"]),
    ("enable", ["enable", "enabled", "enabling", "activate", "turn on", "switch on",
                "add tile", "add button", "show"]),
]

#: Sheet names that are section headings, never the name of a screen set.
#: A sheet may stand in for a missing entity column, but only if its tab name
#: plausibly *is* an entity — "29 - MOCNI" yes, "Manual Configuration" no.
GENERIC_SHEET_NAMES = [
    "manual configuration", "configuration", "config", "manual instruction",
    "manual changes", "instruction", "instructions", "validation", "assignment",
    "steps", "process", "notes", "note", "summary", "cover", "index", "readme",
    "data", "template", "checklist", "pre work", "prework", "sheet1", "sheet2",
    "sheet3", "screenset", "screen set", "screensets", "master", "reference",
]

#: Sheet-name fragments -> the role that sheet plays.
SHEET_ROLES = [
    ("matrix", ["screenset", "screen set", "button", "matrix", "data", "coop", "market", "detail"]),
    ("instructions", ["instruction", "cleanup", "clean up", "step", "how to", "process", "deployment"]),
    ("validation", ["validation", "validate", "verify", "qa", "check"]),
    ("assignment", ["assignment", "assignee", "owner", "allocation", "tracker", "resource"]),
]


# ---------------------------------------------------------------------------

PROFILES: dict[str, dict] = {
    "screenset_button_matrix": {
        "label": "Screen-set / daypart button matrix",
        "description": (
            "An entity column (screen sets / co-ops) crossed with grouping columns "
            "(dayparts), where each cell holds the button number to change for that "
            "entity and group. One matrix cell becomes one expected audit-log change."
        ),
        "shape": "matrix",
        "entity_aliases": ENTITY_ALIASES,
        "group_aliases": GROUP_ALIASES,
        "expected_operation": "Manage Screen Set",
        "priority": 10,
    },
    "explicit_target_list": {
        "label": "Flat target list",
        "description": (
            "One row per expected change, with an entity column and a button column "
            "side by side (plus optional daypart, action and workflow columns). Used "
            "when the plan is already a flat list rather than a cross-tab."
        ),
        "shape": "flat",
        "entity_aliases": ENTITY_ALIASES,
        "button_aliases": BUTTON_ALIASES,
        "group_aliases": GROUP_ALIASES + ["daypart", "group", "segment"],
        "action_aliases": ACTION_ALIASES,
        "expected_operation": "",
        "priority": 20,
    },
}


# ---------------------------------------------------------------------------
# Alias matching helpers
# ---------------------------------------------------------------------------


def _key(text: str) -> str:
    """Collapse a header cell to a comparison key."""
    if not text:
        return ""
    s = text.replace("\xa0", " ").lower()
    s = "".join(ch if ch.isalnum() else " " for ch in s)
    return " ".join(s.split())


def matches_alias(text: str, aliases: list[str]) -> bool:
    """True if ``text`` looks like one of ``aliases``.

    Exact key match first, then containment in either direction, so both
    ``"Daypart"`` and ``"Breakfast Daypart Button"`` match the daypart aliases.
    """
    k = _key(text)
    if not k:
        return False
    keys = [_key(a) for a in aliases]
    if k in keys:
        return True
    for ak in keys:
        if not ak:
            continue
        if ak in k or (len(ak) > 4 and k in ak):
            return True
    return False


def matches_alias_exact(text: str, aliases: list[str]) -> bool:
    """True only if ``text`` *is* one of ``aliases``, ignoring case/punctuation.

    Used to reject a header row that has been re-printed inside the data body.
    Containment must not be used there: a real entity called
    ``"Fairway - Portfolio A"`` contains the alias ``"portfolio"`` and would be
    thrown away as a header.
    """
    k = _key(text)
    return bool(k) and k in {_key(a) for a in aliases}


def which_alias(text: str, aliases: list[str]) -> str | None:
    """The alias that ``text`` matched, or None."""
    k = _key(text)
    if not k:
        return None
    for a in aliases:
        ak = _key(a)
        if ak and (k == ak or ak in k or (len(ak) > 4 and k in ak)):
            return a
    return None


def detect_action(text: str) -> str | None:
    """Canonical action implied by a line of instruction text."""
    low = " " + _key(text) + " "
    best: tuple[int, str] | None = None
    for canon, words in ACTION_KEYWORDS:
        for w in words:
            pos = low.find(_key(w))
            if pos >= 0 and (best is None or pos < best[0]):
                best = (pos, canon)
    return best[1] if best else None


def sheet_role(name: str) -> str | None:
    """The role implied by a sheet's name."""
    for role, frags in SHEET_ROLES:
        if matches_alias(name, frags):
            return role
    return None
