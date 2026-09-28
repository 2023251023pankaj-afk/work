"""
Row-by-row checking for plans written on the Audit Master template.

Each :class:`~auditmaster.plan_schema.Change` names a place, a field and the
value that field should end up with. This module finds the audit rows for that
place and field and says whether the final value is the planned one.

Nothing here depends on the operation. A display order, a caption, a store's
time zone and a user's status are all "this field of this thing should now
read X", and the audit log records every one of them the same way: a
Description saying where, a Field saying what, and Old / New Settings.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from .audit_parser import AuditEntry, parse_timestamp
from .plan_schema import ENVIRONMENT_WORDS, Change, fold
from .plan_template import BLANK_WORDS

CONFIRMED = "confirmed"
WRONG_VALUE = "wrong_value"
MISSING = "missing"
NOT_IN_LOG = "not_in_log"

STATUS_LABEL = {
    CONFIRMED: "done",
    WRONG_VALUE: "different value",
    MISSING: "not done",
    NOT_IN_LOG: "not in this log",
}

#: "Where" values that mean the national level, which every HQ row is at.
_HQ_WORDS = {"hq", "national", "all", "market", "us country office", "country office"}

#: Template wording -> the audit log's own name for the field.
_FIELD_ALIASES = {
    "button state": "button",
    "menu item": "menu item number",
    "image": "image name",
    "kiosk image": "default value kioskbitmapname",
    "removefrommot": "default value removefrommot",
}

#: Values that say the same thing in different words.
_SAME = {
    "enabled": "enable", "disabled": "disable",
    "true": "yes", "y": "yes", "checked": "yes",
    "false": "no", "n": "no", "unchecked": "no",
}


@dataclass
class ChangeResult:
    change: Change
    status: str = MISSING
    #: The rows that made this change (or made it wrongly).
    rows: list[AuditEntry] = field(default_factory=list)
    #: Other fields of the same button changed in the same save — a caption or
    #: image that goes with it.
    supporting: list[AuditEntry] = field(default_factory=list)
    got: str = ""                 # the value the log ended on, when it is wrong
    note: str = ""                # a plain-English aside for the reader
    started_elsewhere: bool = False   # "Was" did not match the log's old value
    date_differs: bool = False

    @property
    def status_label(self) -> str:
        return STATUS_LABEL[self.status]


def check_changes(changes: list[Change], entries: list[AuditEntry]) -> list[ChangeResult]:
    return [_check_one(ch, entries) for ch in changes]


def rows_at(place: str, entries: list[AuditEntry]) -> list[AuditEntry]:
    """Every audit row made at a named place (screen set, store, user…)."""
    needle = fold(place).split()
    return [e for e in entries if needle and _place_in(needle, e)]


# ---------------------------------------------------------------------------


def _check_one(ch: Change, entries: list[AuditEntry]) -> ChangeResult:
    res = ChangeResult(change=ch)

    # 1. The place: which thing was changed. If the log never touches it, this
    #    export simply does not cover that part of the plan.
    place = [e for e in entries
             if _kind_ok(ch, e) and _where_ok(ch, e) and _store_ok(ch, e) and _item_ok(ch, e)]
    if not place:
        res.status = NOT_IN_LOG
        return res

    # 2. The spot: which field of it.
    spot = [e for e in place if _button_ok(ch, e) and _language_ok(ch, e)]
    spot = _field_rows(ch.what, spot)
    on_screen = [e for e in spot if _screen_ok(ch, e)]
    if not on_screen:
        res.status = MISSING
        if spot:
            screens = sorted({e.screen for e in spot if e.screen})
            res.rows = spot
            res.note = f"Changed on {', '.join(screens)} instead of {ch.screen}."
        return res

    # 3. The value: what the field ended up as.
    final = _final_rows(on_screen, ch.becomes)
    wrong = [e for e in final if not _value_ok(e.new, ch.becomes)]
    res.rows = on_screen
    if wrong:
        res.status = WRONG_VALUE
        res.got = wrong[0].new
        if len(final) > 1:
            res.note = f"{len(wrong)} of {len(final)} field(s) ended on a different value."
    else:
        res.status = CONFIRMED
        if ch.was and any(not _same(e.old, _wanted(ch.was)) for e in final):
            res.started_elsewhere = True
            res.note = f"Was {final[0].old or '(blank)'!r} before, not {ch.was!r} as the plan says."
        if ch.effective and _date_differs(ch.effective, final):
            res.date_differs = True
            res.note = (res.note + " " if res.note else "") + (
                f"Effective from {final[0].effective_from}, not {ch.effective}."
            )

    # 4. What goes with it: the other fields of the same button, same save.
    if ch.button:
        done = {e.row_no for e in on_screen}
        saves = {e.audit_id for e in on_screen if e.audit_id}
        res.supporting = [
            e for e in place
            if e.row_no not in done and e.button == ch.button
            and (not saves or e.audit_id in saves)
        ]
    return res


# -- filters ----------------------------------------------------------------


def _run_at(hay: list[str], needle: list[str]) -> bool:
    """``needle`` appears in ``hay`` as whole words, not as another place's prefix.

    "29 - MOCNI" must not match "29 - MOCNI (Pre-Prod)": an environment word
    straight after the name makes it a different place.
    """
    n = len(needle)
    for i in range(len(hay) - n + 1):
        if hay[i:i + n] == needle:
            nxt = hay[i + n] if i + n < len(hay) else ""
            if nxt not in ENVIRONMENT_WORDS:
                return True
    return False


def _place_in(needle: list[str], e: AuditEntry) -> bool:
    for text in (e.screen_set, e.target_name, e.level_details, e.description):
        if text and _run_at(fold(text).split(), needle):
            return True
    return False


def _kind_ok(ch: Change, e: AuditEntry) -> bool:
    k = fold(ch.kind)
    if not k or k in ("other", "restaurant"):
        return True
    wanted = {
        "kiosk": "screen set", "pos": "screen set", "price": "price set",
        "user": "user", "media file": "media", "menu item": "menu item",
    }.get(k, k)
    return wanted in fold(e.operation) or wanted in fold(e.target_kind)


def _where_ok(ch: Change, e: AuditEntry) -> bool:
    w = fold(ch.where)
    if not w or w in _HQ_WORDS:
        return True
    return _place_in(w.split(), e)


def _store_ok(ch: Change, e: AuditEntry) -> bool:
    store = fold(ch.store)
    if not store:
        return True
    if fold(e.new) == store:           # a package schedule names its store as the value
        return True
    return any(store in fold(t).split() for t in (e.level_details, e.target_name, e.description) if t)


def _item_ok(ch: Change, e: AuditEntry) -> bool:
    """For menu-item work, the row must be about this menu item.

    On a kiosk or POS row the menu item is only there to help the reader —
    the button is the address — so it is not used to filter.
    """
    mi = fold(ch.menu_item).split()[:1]
    if not mi or ch.button:
        return True
    mi = mi[0]
    if fold(e.target_kind) == "menu item":
        # "1", "50 at 44126 - Winder": the item number leads the name.
        return fold(e.target_name).split()[:1] == [mi]
    # A screen-set row that puts this item on, or takes it off, a button.
    return fold(e.prop) == "menu item number" and mi in (fold(e.new), fold(e.old))


def _button_ok(ch: Change, e: AuditEntry) -> bool:
    return not ch.button or e.button == ch.button


def _language_ok(ch: Change, e: AuditEntry) -> bool:
    lang = fold(ch.language)
    if not lang or lang == "all" or not e.language:
        return True
    return fold(e.language).startswith(lang)


def _screen_ok(ch: Change, e: AuditEntry) -> bool:
    a, b = fold(e.screen), fold(ch.screen)
    return not (a and b) or b in a or a in b


def _field_rows(what: str, rows: list[AuditEntry]) -> list[AuditEntry]:
    """Rows whose Field is ``what``: exact name first, then as whole words."""
    w = fold(what)
    w = _FIELD_ALIASES.get(w, w)
    if not w:
        return rows
    exact = [e for e in rows if w in (fold(e.prop), fold(e.field_text))]
    if exact:
        return exact
    return [e for e in rows if _run_at(fold(e.field_text).split(), w.split())]


# -- values -----------------------------------------------------------------


def _wanted(text: str) -> str:
    v = fold(text)
    return "" if v in BLANK_WORDS else v


def _same(got: str, want: str) -> bool:
    g = fold(got)
    return _SAME.get(g, g) == _SAME.get(want, want)


def _value_ok(got: str, becomes: str) -> bool:
    # An empty "Should become" was already reported as a plan problem; all
    # that can be checked is that the field changed.
    return True if not fold(becomes) else _same(got, _wanted(becomes))


def _final_rows(rows: list[AuditEntry], becomes: str) -> list[AuditEntry]:
    """The last change to each field, which is the value it was left on.

    A field changed twice in one export (a mistake, then the fix) is judged by
    its final value. Rows without a timestamp, or tied, give the plan the
    benefit of the doubt.
    """
    latest: dict[tuple, AuditEntry] = {}
    for e in rows:
        key = (e.screen_set or e.target_name, e.level_details, e.field_text)
        cur = latest.get(key)
        if cur is None:
            latest[key] = e
            continue
        a, b = e.timestamp, cur.timestamp
        if a and b and a != b:
            if a > b:
                latest[key] = e
        elif _value_ok(e.new, becomes) and not _value_ok(cur.new, becomes):
            latest[key] = e
    return list(latest.values())


_DATE_FORMATS = ("%d-%b-%y", "%d-%b-%Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d")


def _date(text: str) -> _dt.date | None:
    t = " ".join((text or "").split())
    ts = parse_timestamp(t)
    if ts:
        return ts.date()
    for fmt in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def _date_differs(planned: str, rows: list[AuditEntry]) -> bool:
    want = _date(planned)
    if want is None:
        return False
    got = [_date(e.effective_from) for e in rows]
    return any(d and d != want for d in got)
