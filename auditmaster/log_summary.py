"""
Standalone audit-log summary — what a log contains, with no plan to check it against.

:mod:`auditmaster.validator` answers "was the planned work done?". This module
answers the prior question: "what is in this file?" — useful before a plan
exists, when triaging an unfamiliar export, or when the plan is on paper.

It is pure description. Nothing here is a verdict: a log cannot be right or
wrong on its own, only consistent or odd. Anything surprising is reported as an
*observation* with the rows that prompted it, so a reader can judge for
themselves.
"""

from __future__ import annotations

import datetime as _dt
from collections import Counter
from dataclasses import dataclass, field

from .audit_parser import CHANGED, CLEARED, DISABLED, ENABLED, SET, AuditEntry, AuditLog

#: Human wording for the change classifications.
CHANGE_LABEL = {
    DISABLED: "Disabled",
    ENABLED: "Enabled",
    CLEARED: "Cleared",
    SET: "Set",
    CHANGED: "Changed",
    "unchanged": "No change",
}

#: How many distinct values to list per "most common" table.
TOP_N = 8


@dataclass
class ScreenSetSummary:
    """What happened to one screen set."""

    name: str
    rows: int = 0
    screens: list[str] = field(default_factory=list)
    buttons: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    properties: Counter = field(default_factory=Counter)
    changes: Counter = field(default_factory=Counter)
    first_row: int = 0

    @property
    def buttons_disabled(self) -> int:
        return self.changes.get(DISABLED, 0)

    @property
    def buttons_enabled(self) -> int:
        return self.changes.get(ENABLED, 0)


@dataclass
class Observation:
    """Something a reader should look at, with the rows that prompted it."""

    title: str
    message: str
    level: str = "info"          # info | warn
    rows: list[int] = field(default_factory=list)


@dataclass
class LogSummary:
    """A complete description of one audit log."""

    log: AuditLog
    operations: Counter = field(default_factory=Counter)
    activities: Counter = field(default_factory=Counter)
    properties: Counter = field(default_factory=Counter)
    changes: Counter = field(default_factory=Counter)
    languages: Counter = field(default_factory=Counter)
    users: Counter = field(default_factory=Counter)
    levels: Counter = field(default_factory=Counter)
    #: What the changes were made to — screen sets, menu items, restaurants…
    targets: Counter = field(default_factory=Counter)
    effective_dates: Counter = field(default_factory=Counter)
    screen_sets: list[ScreenSetSummary] = field(default_factory=list)
    transactions: list[tuple[str, int]] = field(default_factory=list)
    top_new: list[tuple[str, int]] = field(default_factory=list)
    top_old: list[tuple[str, int]] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    span: tuple[_dt.datetime, _dt.datetime] | None = None

    # -- headline figures --------------------------------------------------
    @property
    def total_rows(self) -> int:
        return len(self.log.entries)

    @property
    def button_toggles(self) -> int:
        return self.changes.get(DISABLED, 0) + self.changes.get(ENABLED, 0)

    @property
    def distinct_buttons(self) -> int:
        return len({(e.screen_set, e.button) for e in self.log.entries if e.button})

    @property
    def span_text(self) -> str:
        if not self.span:
            return ""
        lo, hi = self.span
        if lo.date() == hi.date():
            same = lo.strftime("%d %b %Y")
            return f"{same}, {lo:%H:%M:%S} – {hi:%H:%M:%S}"
        return f"{lo:%d %b %Y %H:%M} – {hi:%d %b %Y %H:%M}"

    @property
    def duration_text(self) -> str:
        if not self.span:
            return ""
        secs = int((self.span[1] - self.span[0]).total_seconds())
        if secs < 60:
            return f"{secs} seconds"
        if secs < 3600:
            return f"{secs // 60} min {secs % 60} sec"
        return f"{secs // 3600} h {(secs % 3600) // 60} min"

    @property
    def warnings(self) -> list[Observation]:
        return [o for o in self.observations if o.level == "warn"]


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def summarize(log: AuditLog) -> LogSummary:
    """Describe everything an audit log contains."""
    s = LogSummary(log=log)
    by_set: dict[str, ScreenSetSummary] = {}

    for e in log.entries:
        if e.operation:
            s.operations[e.operation] += 1
        if e.activity:
            s.activities[e.activity] += 1
        if e.level:
            s.levels[e.level] += 1
        if e.user:
            s.users[e.user] += 1
        if e.language:
            s.languages[e.language] += 1
        if e.effective_from:
            s.effective_dates[e.effective_from] += 1
        if e.target_kind:
            s.targets[e.target_kind] += 1
        s.properties[e.prop or "(unrecognised field)"] += 1
        s.changes[e.change] += 1

        name = e.screen_set or "(no screen set named)"
        block = by_set.get(name)
        if block is None:
            block = by_set[name] = ScreenSetSummary(name=name, first_row=e.row_no)
        block.rows += 1
        block.properties[e.prop or "(unrecognised field)"] += 1
        block.changes[e.change] += 1
        if e.screen and e.screen not in block.screens:
            block.screens.append(e.screen)
        if e.button and e.button not in block.buttons:
            block.buttons.append(e.button)
        if e.language and e.language not in block.languages:
            block.languages.append(e.language)

    for block in by_set.values():
        block.buttons.sort(key=lambda b: (len(b), b))
        block.languages.sort()
    s.screen_sets = sorted(by_set.values(), key=lambda b: (-b.rows, b.name))

    s.transactions = Counter(e.audit_id for e in log.entries if e.audit_id).most_common()
    s.top_new = Counter(e.new for e in log.entries if e.new).most_common(TOP_N)
    s.top_old = Counter(e.old for e in log.entries if e.old).most_common(TOP_N)
    s.span = log.timespan()

    _observe(s, log)
    return s


def _observe(s: LogSummary, log: AuditLog) -> None:
    """Note anything a reader should look at. Never a verdict — only a flag."""
    entries = log.entries

    if len(s.users) > 1:
        top = ", ".join(f"{u} ({n})" for u, n in s.users.most_common(4))
        s.observations.append(Observation(
            "More than one person made these changes", level="warn",
            message=(f"{len(s.users)} users appear in this log: {top}. A single "
                     f"deployment is usually the work of one person."),
            rows=[e.row_no for e in entries if e.user != s.users.most_common(1)[0][0]][:40],
        ))

    # A field with no button ("Status", "Display Order") is perfectly normal —
    # it simply is not tile content. Worth stating, never a warning.
    no_button = [e for e in entries if not e.button]
    if no_button:
        props = ", ".join(
            sorted({e.prop for e in no_button if e.prop})[:6]
        )
        s.observations.append(Observation(
            "Changes not tied to a button", level="info",
            message=(f"{len(no_button)} of {len(entries)} row(s) change a property "
                     f"that names no button number{': ' + props if props else ''}. "
                     f"These are screen- or item-level settings rather than tile content."),
            rows=[e.row_no for e in no_button][:40],
        ))

    noop = [e for e in entries if (e.old or "").strip() == (e.new or "").strip()]
    if noop:
        s.observations.append(Observation(
            "Rows where nothing actually changed", level="warn",
            message=(f"{len(noop)} row(s) record an old and new setting that are "
                     f"identical, so they changed nothing."),
            rows=[e.row_no for e in noop][:40],
        ))

    seen: dict[tuple, list[int]] = {}
    for e in entries:
        seen.setdefault((e.screen_set, e.field_text, e.old, e.new), []).append(e.row_no)
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    if dupes:
        n = sum(len(v) - 1 for v in dupes.values())
        s.observations.append(Observation(
            "Repeated identical rows", level="warn",
            message=(f"{len(dupes)} change(s) appear more than once with the same "
                     f"screen set, field and values ({n} extra row(s)). Usually a "
                     f"re-save rather than separate work."),
            rows=sorted(r for v in dupes.values() for r in v[1:])[:40],
        ))

    if len(s.effective_dates) > 1:
        top = ", ".join(f"{d} ({n})" for d, n in s.effective_dates.most_common(4))
        s.observations.append(Observation(
            "Several effective dates", level="warn",
            message=(f"Changes in this log take effect on {len(s.effective_dates)} "
                     f"different dates: {top}."),
        ))

    if len(s.transactions) > 1:
        s.observations.append(Observation(
            "Several transactions", level="info",
            message=(f"{len(s.transactions)} transaction id(s) are present, so this "
                     f"export covers more than one save."),
        ))

    if log.skipped_rows:
        s.observations.append(Observation(
            "Rows skipped while reading", level="warn",
            message=f"{log.skipped_rows} row(s) could not be read and were skipped.",
        ))

    for w in log.warnings:
        s.observations.append(Observation("Reading the file", w, level="warn"))


# ---------------------------------------------------------------------------
# Narrative
# ---------------------------------------------------------------------------


def headline(s: LogSummary) -> str:
    """One sentence describing the log."""
    if not s.total_rows:
        return "This audit log contains no change rows."

    sets = len(s.screen_sets)
    bits = [f"{s.total_rows:,} change row{'' if s.total_rows == 1 else 's'}"]
    if sets:
        bits.append(f"{sets} screen set{'' if sets == 1 else 's'}")
    if s.button_toggles:
        bits.append(f"{s.button_toggles} button state change{'' if s.button_toggles == 1 else 's'}")
    who = ""
    if len(s.users) == 1:
        who = f" by {next(iter(s.users))}"
    when = f" on {s.span[0]:%d %b %Y}" if s.span else ""
    return "This log records " + ", ".join(bits) + who + when + "."


def narrative(s: LogSummary) -> list[str]:
    """A few plain-language paragraphs describing the log."""
    if not s.total_rows:
        return ["This audit log contains no change rows."]

    out: list[str] = [headline(s)]

    if s.span:
        out.append(
            f"The changes span {s.span_text}"
            + (f" — {s.duration_text} of work" if s.duration_text else "")
            + (f", recorded under {len(s.transactions)} transaction id(s)."
               if len(s.transactions) > 1 else ".")
        )

    if s.targets:
        kinds = ", ".join(f"{k} ({n:,})" for k, n in s.targets.most_common(6))
        out.append(f"What was changed: {kinds}.")

    props = ", ".join(f"{p} ({n})" for p, n in s.properties.most_common(6))
    if props:
        out.append(f"By field: {props}.")

    kinds = ", ".join(
        f"{CHANGE_LABEL.get(k, k.title())} ({n})" for k, n in s.changes.most_common(6)
    )
    if kinds:
        out.append(f"By kind of change: {kinds}.")

    if s.languages:
        langs = ", ".join(f"{l} ({n})" for l, n in s.languages.most_common(8))
        out.append(
            f"Localised content carries {len(s.languages)} language/variant "
            f"qualifier(s): {langs}."
        )

    if s.screen_sets:
        top = s.screen_sets[0]
        if len(s.screen_sets) == 1:
            out.append(
                f"All of it is on {top.name}"
                + (f", screen {top.screens[0]}" if top.screens else "")
                + (f", touching button(s) {', '.join(top.buttons[:12])}."
                   if top.buttons else ".")
            )
        else:
            listed = ", ".join(f"{b.name} ({b.rows})" for b in s.screen_sets[:5])
            more = "" if len(s.screen_sets) <= 5 else f", and {len(s.screen_sets) - 5} more"
            out.append(f"Busiest screen sets: {listed}{more}.")

    if s.warnings:
        out.append(
            f"{len(s.warnings)} thing(s) are worth a look — see the observations below. "
            f"These are not errors; an audit log on its own cannot be right or wrong, "
            f"only consistent or unusual."
        )

    return out
