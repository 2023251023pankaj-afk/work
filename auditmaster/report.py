"""
Summary and export builders.

The validator produces structured findings; this module turns them into the
things a human or a ticket actually needs: a one-line verdict, a plain-language
narrative that accounts for the whole audit log, and JSON / CSV / plain-text
exports.
"""

from __future__ import annotations

import csv
import io
import json

from .audit_parser import AuditLog
from .plan_schema import Plan
from .validator import (
    ATTR_LABEL,
    CONFIRMED,
    FAIL,
    PASS,
    PASS_WARN,
    Result,
)


# ---------------------------------------------------------------------------
# Narrative
# ---------------------------------------------------------------------------


def headline(res: Result) -> str:
    """One sentence answering "is the work correct?"."""
    if not res.plan.targets:
        return _evidence_headline(res)

    n_sets = len(res.entities)
    sets_word = "screen set" if n_sets == 1 else "screen sets"

    if res.verdict == PASS:
        return (
            f"All {res.total_confirmed} planned change"
            f"{'' if res.total_confirmed == 1 else 's'} confirmed, and nothing was "
            f"changed that the plan did not ask for."
        )
    if res.verdict == PASS_WARN:
        return (
            f"All {res.total_confirmed} planned change"
            f"{'' if res.total_confirmed == 1 else 's'} confirmed, but "
            f"{len(res.warnings)} thing{'' if len(res.warnings) == 1 else 's'} "
            f"below need a look."
        )
    parts = []
    if res.total_missing:
        parts.append(
            f"{res.total_missing} planned change"
            f"{' is' if res.total_missing == 1 else 's are'} missing"
        )
    if res.total_unauthorized:
        parts.append(
            f"{res.total_unauthorized} change"
            f"{' was' if res.total_unauthorized == 1 else 's were'} made that the "
            f"plan did not ask for"
        )
    # Anything else that failed is described by the rows it flagged, because
    # "2 other checks failed" tells a reader nothing they can act on.
    counted = {"C1", "C2", "C3"}
    other = [f for f in res.failures if f.check_id not in counted]
    if other:
        rows = sorted({r for f in other for r in f.rows})
        if rows:
            parts.append(
                f"{len(rows)} audit row"
                f"{' is' if len(rows) == 1 else 's are'} not accounted for by the plan"
            )
        else:
            n = len(other)
            parts.append(f"{n} other check{'' if n == 1 else 's'} failed")
    if not parts:
        return "Some checks did not pass — see below."
    if len(parts) == 1:
        return parts[0].capitalize() + "."
    return (", ".join(parts[:-1]) + " and " + parts[-1]).capitalize() + "."


def _evidence_headline(res: Result) -> str:
    """Headline for a plan checked by value matching rather than a button grid."""
    ev = res.evidence
    if ev is None or not ev.claims:
        return "Nothing checkable could be read from this plan."
    n = len(ev.matched)
    unexplained = len(ev.unexplained(res.log))
    if not n:
        return (
            f"None of the {len(ev.claims)} value(s) this plan names appear in the "
            f"audit log — the two files look unrelated."
        )
    if unexplained:
        return (
            f"{n} of the plan's value(s) are confirmed in the audit log, but "
            f"{unexplained} change(s) in the log correspond to nothing the plan "
            f"asks for."
        )
    return (
        f"{n} value(s) the plan names are confirmed in the audit log, and all "
        f"{len(res.log.entries)} change(s) recorded there are accounted for."
    )


def next_steps(res: Result) -> list[str]:
    """What the reader should actually do, in order.

    A verdict on its own leaves someone asking "so what do I do now?" — this
    turns the findings into the two or three concrete actions that follow, in
    the words they would use themselves.
    """
    steps: list[str] = []

    missing = [(er.entity, b) for er in res.entities for b in er.missing]
    if missing:
        where = ", ".join(f"button {b.button} on {ent}" for ent, b in missing[:4])
        steps.append(
            f"Finish the work that is still outstanding: {where}"
            + (f" and {len(missing) - 4} more." if len(missing) > 4 else ".")
        )

    wrong = [(er.entity, b) for er in res.entities for b in er.wrong_direction]
    if wrong:
        steps.append(
            f"{len(wrong)} button(s) ended up in the wrong state — set them back "
            f"to what the plan asks for."
        )

    unauth = [(er.entity, b) for er in res.entities for b in er.unauthorized]
    if unauth:
        sets = sorted({ent for ent, _ in unauth})
        steps.append(
            f"Check {len(unauth)} change(s) on {', '.join(sets[:3])} that the plan "
            f"does not cover. They were made in the same save as the planned work, "
            f"so ask whoever did that screen set whether they were intended."
        )

    outside = [er for er in res.entities if not er.in_plan]
    if outside:
        steps.append(
            f"{len(outside)} screen set(s) were changed that this plan says nothing "
            f"about: {', '.join(er.entity for er in outside[:3])}. Confirm they "
            f"belong to a different piece of work."
        )

    if res.not_attempted:
        covered = len(res.plan.entities) - len(res.not_attempted)
        steps.append(
            f"This log covers {covered} of the plan's {len(res.plan.entities)} "
            f"screen set(s), so the other {len(res.not_attempted)} are neither "
            f"confirmed nor contradicted here. That is normal when each person "
            f"exports their own log — check those separately."
        )

    if not steps:
        steps.append(
            "Nothing needs doing. Every change the plan asked for is in the log, "
            "and nothing else was touched alongside it."
        )
    return steps


def narrative(res: Result) -> list[str]:
    """A few paragraphs summarising the entire audit log."""
    plan, log = res.plan, res.log
    out: list[str] = [headline(res)]

    # -- what was compared -------------------------------------------------
    scope = (
        f"Checked {len(plan.targets)} change(s) the plan asks for across "
        f"{len(plan.entities)} screen set(s), against {len(log.entries):,} row(s) "
        f"in the audit log."
    )
    if plan.meta.tile_label:
        scope += (
            f" The plan's subject is the {plan.meta.tile_label!r} tile, to be "
            f"{plan.meta.action}d"
        )
        if plan.meta.screen_number or plan.meta.screen_name:
            scope += (
                f" on screen {plan.meta.screen_number} "
                f"{plan.meta.screen_name!r}".rstrip()
            )
        scope += "."
    out.append(scope)

    # -- per screen set ----------------------------------------------------
    # A plan can cover 50+ screen sets. Printing a paragraph for every clean one
    # buries the handful that need attention, so the clean ones are counted and
    # only those with something to say are described.
    noteworthy = [
        er for er in res.entities
        if not er.in_plan or er.missing or er.wrong_direction or er.unauthorized
        or any(f.is_failure or f.is_warning for f in er.findings)
    ]
    clean = [er for er in res.entities if er not in noteworthy]
    if clean:
        names = ", ".join(er.entity for er in clean[:8])
        out.append(
            f"{len(clean)} screen set(s) are fully confirmed with nothing unexpected: "
            + names
            + (f", and {len(clean) - 8} more." if len(clean) > 8 else ".")
        )

    for er in noteworthy:
        if not er.in_plan:
            out.append(
                f"{er.entity}: this screen set is not in the plan at all, yet "
                f"{er.entry_count} audit row(s) record changes to it. Every one of "
                f"those changes is unauthorized."
            )
            continue
        bits = [
            f"{er.entity}: {len(er.confirmed)} of {er.expected_count} planned "
            f"button(s) confirmed"
        ]
        if er.missing:
            bits.append(
                "missing " + ", ".join(f"{b.button} ({b.group})" for b in er.missing)
            )
        if er.wrong_direction:
            bits.append(
                "wrong final state on "
                + ", ".join(f"{b.button}" for b in er.wrong_direction)
            )
        if er.unauthorized:
            bits.append(
                "button(s) changed that the plan did not ask for: "
                + ", ".join(b.button for b in er.unauthorized)
            )
        detail = "; ".join(bits) + "."
        confirmed = er.confirmed
        if confirmed:
            detail += " Confirmed: " + ", ".join(
                f"{b.group} → button {b.button}" for b in confirmed
            ) + "."
        if er.languages:
            detail += (
                f" Tile content was cleared across {len(er.languages)} language(s): "
                f"{', '.join(er.languages)}."
            )
        out.append(detail)

    # -- whole-log accounting ---------------------------------------------
    counts = res.attribution_counts()
    accounted = " · ".join(
        f"{ATTR_LABEL[k]}: {v}" for k, v in counts.items() if v
    )
    unexplained = counts.get("unattributed", 0)
    if unexplained:
        out.append(
            f"{unexplained} of the {len(log.entries)} audit row(s) could not be tied "
            f"to anything the plan describes — {accounted}. Those rows are the ones "
            f"to look at first."
        )
    else:
        out.append(f"Every row of the audit log is accounted for — {accounted}.")

    if res.not_attempted:
        out.append(
            f"{len(res.not_attempted)} screen set(s) in the plan have no rows in "
            f"this audit report and are therefore neither confirmed nor "
            f"contradicted: "
            + ", ".join(res.not_attempted[:10])
            + ("…" if len(res.not_attempted) > 10 else "")
            + "."
        )

    if res.document_findings:
        out.append(
            f"Separately, {len(res.document_findings)} issue(s) were noticed in how "
            f"the source documents were authored. These do not affect the verdict."
        )
    return out


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------


def to_json(res: Result) -> str:
    """Full machine-readable result."""
    doc = {
        "verdict": res.verdict,
        "verdict_label": res.verdict_label,
        "headline": headline(res),
        "narrative": narrative(res),
        "summary": {
            "expected": res.total_expected,
            "confirmed": res.total_confirmed,
            "missing": res.total_missing,
            "unauthorized": res.total_unauthorized,
            "match_rate_percent": res.match_rate,
            "audit_rows": len(res.log.entries),
            "screen_sets_audited": len(res.entities),
            "plan_screen_sets": len(res.plan.entities),
            "not_attempted": len(res.not_attempted),
            "failures": len(res.failures),
            "warnings": len(res.warnings),
        },
        "plan": {
            "name": res.plan.plan_name,
            "profile": res.plan.profile,
            "source_file": res.plan.source_filename,
            "source_format": res.plan.source_format,
            "source_encoding": res.plan.source_encoding,
            "tile_label": res.plan.meta.tile_label,
            "screen_number": res.plan.meta.screen_number,
            "screen_name": res.plan.meta.screen_name,
            "action": res.plan.meta.action,
            "groups": res.plan.groups,
            "expected_changes": len(res.plan.targets),
        },
        "audit_log": {
            "file": res.log.filename,
            "format": res.log.fmt,
            "encoding": res.log.encoding,
            "effective_date": res.log.report_effective_date,
            "generated_on": res.log.generated_on,
            "generated_by": res.log.generated_by,
            "rows": len(res.log.entries),
            "transaction_ids": res.log.audit_ids,
            "users": res.log.users,
            "operations": res.log.operations,
        },
        "options": {
            "require_full_plan_coverage": res.options.require_full_plan_coverage,
            "require_supporting_evidence": res.options.require_supporting_evidence,
            "require_language_parity": res.options.require_language_parity,
            "strict_screen_match": res.options.strict_screen_match,
        },
        "screen_sets": [
            {
                "entity": er.entity,
                "in_plan": er.in_plan,
                "verdict": er.verdict,
                "screen": er.screen,
                "audit_rows": er.entry_count,
                "languages": er.languages,
                "buttons": [
                    {
                        "group": b.group,
                        "button": b.button,
                        "workflow": b.workflow,
                        "status": b.status,
                        "expected_state": b.expected_state,
                        "change": b.toggle.change_text if b.toggle else None,
                        "audit_row": b.toggle.row_no if b.toggle else None,
                        "caption_languages": sorted(b.caption_langs),
                        "image_languages": sorted(b.image_langs),
                        "evidence_rows": b.evidence_count,
                    }
                    for b in er.buttons
                ],
                "unauthorized": [
                    {
                        "button": b.button,
                        "toggled": b.toggle.change_text if b.toggle else None,
                        "rows": [e.row_no for e in b.support_rows],
                    }
                    for b in er.unauthorized
                ],
            }
            for er in res.entities
        ],
        "findings": [
            {
                "check": f.check_id,
                "title": f.title,
                "status": f.status,
                "severity": f.severity,
                "entity": f.entity,
                "subject": f.subject,
                "message": f.message,
                "audit_rows": f.rows,
            }
            for f in res.all_findings
        ],
        "document_findings": [
            {"check": f.check_id, "title": f.title, "message": f.message}
            for f in res.document_findings
        ],
        "not_attempted": res.not_attempted,
        "audit_rows": [
            {
                "row": e.row_no,
                "attribution": res.entry_attribution(e),
                "screen_set": e.screen_set,
                "property": e.prop,
                "button": e.button,
                "language": e.language,
                "field": e.field_text,
                "old": e.old,
                "new": e.new,
                "change": e.change,
                "user": e.user,
                "timestamp": e.date_text,
            }
            for e in res.log.entries
        ],
    }
    return json.dumps(doc, indent=2, ensure_ascii=False)


#: Column order for the row-by-row CSV export.
_CSV_COLUMNS = [
    "audit_row", "attribution", "screen_set", "in_plan", "daypart", "button",
    "property", "language", "field", "old_setting", "new_setting", "change",
    "button_status", "user", "timestamp", "transaction_id",
]


#: Excel and LibreOffice treat a cell starting with any of these as a formula,
#: so a value carried over from the audited system could execute on the
#: reviewer's machine when they open the export. Prefixing with an apostrophe
#: forces it to be read as text; the spreadsheet does not display the prefix.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: object) -> object:
    """Neutralise a value that a spreadsheet would execute as a formula."""
    if not isinstance(value, str) or not value:
        return value
    return "'" + value if value.startswith(_FORMULA_LEAD) else value


def to_csv(res: Result) -> str:
    """One row per audit-log row, annotated with how it was attributed.

    This is the export to hand to a reviewer: it is the original audit log plus
    the verdict for each line.
    """
    # Map button -> its status, per screen set, to annotate each row.
    status_by: dict[tuple[str, str], str] = {}
    in_plan: dict[str, bool] = {}
    daypart_by: dict[tuple[str, str], str] = {}
    for er in res.entities:
        in_plan[er.entity] = er.in_plan
        for b in er.buttons + er.unauthorized:
            status_by[(er.entity, b.button)] = b.status
            daypart_by[(er.entity, b.button)] = b.group

    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(_CSV_COLUMNS)
    for e in res.log.entries:
        key = (e.screen_set, e.button)
        w.writerow([_csv_safe(v) for v in [
            e.row_no,
            res.entry_attribution(e),
            e.screen_set,
            "yes" if in_plan.get(e.screen_set, False) else "no",
            daypart_by.get(key, ""),
            e.button,
            e.prop,
            e.language,
            e.field_text,
            e.old,
            e.new,
            e.change,
            status_by.get(key, ""),
            e.user,
            e.date_text,
            e.audit_id,
        ]])
    return buf.getvalue()


def to_text(res: Result) -> str:
    """Plain-text report, suitable for pasting into a ticket or email."""
    L: list[str] = []
    rule = "=" * 74

    L += [rule, "AUDIT LOG VALIDATION REPORT", rule, ""]
    L.append(f"Verdict            : {res.verdict}  ({res.verdict_label})")
    L.append(f"Deployment plan    : {res.plan.source_filename}")
    L.append(f"Audit log          : {res.log.filename}")
    if res.log.report_effective_date:
        L.append(f"Effective date     : {res.log.report_effective_date}")
    if res.log.generated_by:
        L.append(f"Report generated by: {res.log.generated_by}")
    L.append("")

    L += ["-" * 74, "SUMMARY", "-" * 74]
    for para in narrative(res):
        L += _wrap(para) + [""]
    L.pop()

    L += ["", "-" * 74, "SCORECARD", "-" * 74]
    L.append(f"  Planned changes expected   : {res.total_expected}")
    L.append(f"  Confirmed in audit log     : {res.total_confirmed}")
    L.append(f"  Missing from audit log     : {res.total_missing}")
    L.append(f"  Unauthorized changes found : {res.total_unauthorized}")
    L.append(f"  Match rate                 : {res.match_rate}%")
    L.append(f"  Audit rows examined        : {len(res.log.entries)}")

    for er in res.entities:
        L += ["", "-" * 74, f"SCREEN SET: {er.entity}  [{er.verdict}]", "-" * 74]
        if er.screen:
            L.append(f"  Screen: {er.screen}")
        if er.languages:
            L.append(f"  Languages touched: {', '.join(er.languages)}")
        L.append("")
        L.append(f"  {'Daypart':<12} {'Button':<8} {'Status':<12} {'Change':<22} Evidence")
        L.append(f"  {'-' * 12} {'-' * 8} {'-' * 12} {'-' * 22} {'-' * 8}")
        for b in er.buttons:
            change = b.toggle.change_text if b.toggle else "— no entry —"
            mark = "OK " if b.status == CONFIRMED else "!! "
            L.append(
                f"  {mark}{b.group:<10} {b.button:<8} {b.status:<12} {change:<22} "
                f"{b.evidence_count} row(s)"
            )
        for b in er.unauthorized:
            change = b.toggle.change_text if b.toggle else "content only"
            L.append(
                f"  !! {'(unplanned)':<10} {b.button:<8} {'UNAUTHORIZED':<12} "
                f"{change:<22} {b.evidence_count} row(s)"
            )

    if res.failures:
        L += ["", "-" * 74, "FAILURES", "-" * 74]
        for f in res.failures:
            L += _finding_lines(f)
    if res.warnings:
        L += ["", "-" * 74, "WARNINGS", "-" * 74]
        for f in res.warnings:
            L += _finding_lines(f)

    info = [f for f in res.all_findings if f.status in ("pass", "info")]
    if info:
        L += ["", "-" * 74, "CHECKS PASSED / OBSERVATIONS", "-" * 74]
        for f in info:
            L += _finding_lines(f)

    if res.document_findings:
        L += ["", "-" * 74, "DOCUMENT QUALITY (does not affect the verdict)", "-" * 74]
        for f in res.document_findings:
            L += _finding_lines(f)

    L += ["", rule, "Generated locally by Audit Master. No external service was used.", rule]
    return "\n".join(L)


def _finding_lines(f) -> list[str]:
    head = f"  [{f.check_id}] {f.title}"
    if f.entity:
        head += f" — {f.entity}"
    if f.subject:
        head += f" / {f.subject}"
    return [head] + _wrap(f.message, indent="        ") + [""]


def _wrap(text: str, width: int = 74, indent: str = "  ") -> list[str]:
    import textwrap

    return textwrap.wrap(text, width=width, initial_indent=indent, subsequent_indent=indent) or [indent]


def plan_template_json(plan: Plan) -> str:
    """Export the normalized plan so a mis-parsed plan can be hand-corrected.

    Re-uploading the edited file is accepted by the plan parser, which makes
    this the escape hatch for a layout auto-detection cannot handle.
    """
    return json.dumps(plan.to_dict(), indent=2, ensure_ascii=False)
