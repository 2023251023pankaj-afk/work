"""
Validation engine: reconcile a :class:`Plan` against an :class:`AuditLog`.

The reconciliation is three-way, and that third way is the point of the tool:

  * **Confirmed**  — the plan asked for it and the audit log proves it happened.
  * **Missing**    — the plan asked for it and the audit log does not show it.
  * **Unauthorized** — the audit log shows it but the plan never asked for it.

A checker eyeballing an audit report reliably catches the first two and misses
the third, which is the dangerous one: a button disabled on a screen set that
nobody planned to touch.

Every audit row is also *attributed* to exactly one bucket, so the summary can
account for the whole report rather than only the rows it happened to look for.
No external service or API is involved; every judgement below is local logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .audit_parser import CLEARED, DISABLED, ENABLED, AuditEntry, AuditLog
from .evidence import KIND_LABEL as EV_KIND_LABEL
from .evidence import EvidenceResult, reconcile
from .plan_schema import Plan, Target, fold, norm_entity

# -- verdicts ---------------------------------------------------------------
PASS = "PASS"
PASS_WARN = "PASS_WITH_WARNINGS"
FAIL = "FAIL"

VERDICT_LABEL = {
    PASS: "Validated — work matches the plan",
    PASS_WARN: "Validated with observations",
    FAIL: "Not validated — discrepancies found",
}

# -- statuses ---------------------------------------------------------------
CONFIRMED = "confirmed"
MISSING = "missing"
UNAUTHORIZED = "unauthorized"
WRONG_DIRECTION = "wrong_direction"

# -- severities -------------------------------------------------------------
CRITICAL = "critical"
MAJOR = "major"
MINOR = "minor"
INFO = "info"

_SEVERITY_ORDER = {CRITICAL: 0, MAJOR: 1, MINOR: 2, INFO: 3}

# -- audit-row attribution --------------------------------------------------
ATTR_EXPECTED = "expected"          # the planned button toggle itself
ATTR_SUPPORTING = "supporting"      # caption/image evidence for a planned button
ATTR_UNAUTHORIZED = "unauthorized"  # a change the plan never asked for
ATTR_OUT_OF_SCOPE = "out_of_scope"  # a screen set the plan does not cover
ATTR_OTHER_WORK = "other_work"      # a separate task that shares this export
ATTR_UNATTRIBUTED = "unattributed"  # could not be tied to anything

#: What each bucket is called on screen. Written for someone checking a
#: deployment, not for someone who built this: "Supporting evidence" and
#: "Unattributed" mean nothing to a reader seeing the page for the first time.
ATTR_LABEL = {
    ATTR_EXPECTED: "The planned change",
    ATTR_SUPPORTING: "Part of a planned change",
    ATTR_UNAUTHORIZED: "Not in the plan",
    ATTR_OTHER_WORK: "A different task",
    ATTR_OUT_OF_SCOPE: "A screen set the plan doesn't cover",
    ATTR_UNATTRIBUTED: "Couldn't be explained",
}

#: One line of plain English per bucket, shown as help under the breakdown.
ATTR_HELP = {
    ATTR_EXPECTED: "The button the plan told you to change.",
    ATTR_SUPPORTING: "The caption and image rows that go with a planned button.",
    ATTR_UNAUTHORIZED: "Changed in the same save as the planned work, but the plan never asked for it. Worth checking.",
    ATTR_OTHER_WORK: "Changed in a save that did none of this plan's work — somebody else's task that happens to be in the same export.",
    ATTR_OUT_OF_SCOPE: "A screen set this plan says nothing about.",
    ATTR_UNATTRIBUTED: "Could not be matched to anything in the plan.",
}


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@dataclass
class Options:
    """Validation strictness, surfaced as checkboxes in the UI."""

    #: Treat a plan entity with no audit activity as a failure rather than
    #: "not attempted". Off by default: one audit export usually covers one
    #: screen set out of a plan's many.
    require_full_plan_coverage: bool = False

    #: Require at least one caption/image row per confirmed button.
    require_supporting_evidence: bool = False

    #: Require every button of a screen set to cover the same languages as the
    #: best-covered button in that screen set.
    require_language_parity: bool = False

    #: Fail when the audit log's screen does not match the plan's screen.
    strict_screen_match: bool = False


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    check_id: str
    title: str
    status: str                 # pass | fail | warn | info
    severity: str
    message: str
    entity: str = ""
    subject: str = ""
    evidence: list[str] = field(default_factory=list)
    rows: list[int] = field(default_factory=list)

    @property
    def is_failure(self) -> bool:
        return self.status == "fail"

    @property
    def is_warning(self) -> bool:
        return self.status == "warn"


@dataclass
class ButtonResult:
    """Outcome for one (entity, button) pair."""

    entity: str
    button: str
    group: str = ""
    expected_state: str = "Disable"
    status: str = MISSING
    toggle: AuditEntry | None = None
    workflow: str = ""
    caption_langs: dict[str, AuditEntry] = field(default_factory=dict)
    image_langs: dict[str, AuditEntry] = field(default_factory=dict)
    other: list[AuditEntry] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def support_rows(self) -> list[AuditEntry]:
        return list(self.caption_langs.values()) + list(self.image_langs.values()) + self.other

    @property
    def languages(self) -> list[str]:
        return sorted(set(self.caption_langs) | set(self.image_langs))

    @property
    def evidence_count(self) -> int:
        return len(self.support_rows) + (1 if self.toggle else 0)


@dataclass
class EntityResult:
    """Outcome for one screen set."""

    entity: str
    plan_entity: str = ""
    in_plan: bool = True
    buttons: list[ButtonResult] = field(default_factory=list)
    unauthorized: list[ButtonResult] = field(default_factory=list)
    #: Buttons changed in transactions that did none of this plan's work — a
    #: different task that happens to share the export window.
    other_work: list[ButtonResult] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    screen: str = ""
    languages: list[str] = field(default_factory=list)
    entry_count: int = 0

    @property
    def confirmed(self) -> list[ButtonResult]:
        return [b for b in self.buttons if b.status == CONFIRMED]

    @property
    def missing(self) -> list[ButtonResult]:
        return [b for b in self.buttons if b.status == MISSING]

    @property
    def wrong_direction(self) -> list[ButtonResult]:
        return [b for b in self.buttons if b.status == WRONG_DIRECTION]

    @property
    def verdict(self) -> str:
        if any(f.is_failure for f in self.findings):
            return FAIL
        if any(f.is_warning for f in self.findings):
            return PASS_WARN
        return PASS

    @property
    def expected_count(self) -> int:
        return len(self.buttons)


@dataclass
class Result:
    """The complete validation outcome."""

    plan: Plan
    log: AuditLog
    options: Options
    entities: list[EntityResult] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    #: Concerns about how the two *documents* were authored (a mislabelled
    #: daypart row, a duplicated audit field). Reported separately because they
    #: say nothing about whether the deployment work itself was correct, and so
    #: must not move the verdict.
    document_findings: list[Finding] = field(default_factory=list)
    not_attempted: list[str] = field(default_factory=list)
    attribution: dict[int, str] = field(default_factory=dict)   # row_no -> ATTR_*
    #: Operation-agnostic value matching, present for every plan.
    evidence: EvidenceResult | None = None

    # -- rollups ----------------------------------------------------------
    @property
    def all_findings(self) -> list[Finding]:
        """Validation findings only — the ones that decide the verdict."""
        out = list(self.findings)
        for e in self.entities:
            out.extend(e.findings)
        return sorted(out, key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.check_id))

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.all_findings if f.is_failure]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.all_findings if f.is_warning]

    @property
    def verdict(self) -> str:
        if self.failures:
            return FAIL
        if self.warnings:
            return PASS_WARN
        return PASS

    @property
    def verdict_label(self) -> str:
        return VERDICT_LABEL[self.verdict]

    # -- counters ---------------------------------------------------------
    @property
    def total_expected(self) -> int:
        return sum(e.expected_count for e in self.entities)

    @property
    def total_confirmed(self) -> int:
        return sum(len(e.confirmed) for e in self.entities)

    @property
    def total_missing(self) -> int:
        return sum(len(e.missing) for e in self.entities)

    @property
    def total_unauthorized(self) -> int:
        return sum(len(e.unauthorized) for e in self.entities)

    @property
    def match_rate(self) -> int:
        if not self.total_expected:
            return 0
        return round(self.total_confirmed / self.total_expected * 100)

    def attribution_counts(self) -> dict[str, int]:
        counts = {k: 0 for k in ATTR_LABEL}
        for attr in self.attribution.values():
            counts[attr] = counts.get(attr, 0) + 1
        return counts

    def entries_for(self, row_nos: list[int]) -> list[AuditEntry]:
        """The audit rows behind a finding, in file order, for showing in full."""
        wanted = set(row_nos)
        return [e for e in self.log.entries if e.row_no in wanted]

    def entry_attribution(self, entry: AuditEntry) -> str:
        return self.attribution.get(entry.row_no, ATTR_UNATTRIBUTED)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def validate(plan: Plan, log: AuditLog, options: Options | None = None) -> Result:
    """Reconcile ``log`` against ``plan``."""
    opts = options or Options()
    res = Result(plan=plan, log=log, options=opts)

    # Default every row to unattributed; each check claims the rows it explains.
    for e in log.entries:
        res.attribution[e.row_no] = ATTR_UNATTRIBUTED

    # Operation-agnostic pass: match the plan's stated values against the log.
    # This runs for every plan, and is the *only* engine when the plan has no
    # button grid to check structurally.
    if plan.claims:
        res.evidence = reconcile(plan.claims, log)

    if not plan.targets:
        _validate_by_evidence(res, plan, log)
        _check_provenance(res, log)
        _carry_parse_warnings(res, plan, log)
        return res

    audited_sets = log.screen_sets
    if not audited_sets:
        _validate_without_screen_set(res, plan, log, opts)
    else:
        for name in audited_sets:
            res.entities.append(_validate_entity(res, plan, log, name, opts))

    _separate_unrelated_work(res, log)
    _check_not_attempted(res, plan, opts, audited_sets)
    _check_operation(res, plan, log)
    _check_dates(res, log)
    _check_provenance(res, log)
    _check_assignees(res, plan, log)
    _check_unattributed(res, log)
    _carry_parse_warnings(res, plan, log)
    return res


# ---------------------------------------------------------------------------
# Per-entity validation
# ---------------------------------------------------------------------------


def _validate_entity(res: Result, plan: Plan, log: AuditLog, name: str, opts: Options) -> EntityResult:
    entries = log.entries_for_set(name)
    er = EntityResult(entity=name, entry_count=len(entries))
    er.screen = next((e.screen for e in entries if e.screen), "")
    er.languages = sorted({e.language for e in entries if e.language})

    plan_entity = plan.find_entity(name)
    er.plan_entity = plan_entity or ""
    er.in_plan = plan_entity is not None

    if plan_entity is None:
        # The audit log touched something the plan never mentions.
        for e in entries:
            res.attribution[e.row_no] = ATTR_OUT_OF_SCOPE
        buttons = sorted({e.button for e in entries if e.button}, key=_btn_key)
        er.findings.append(
            Finding(
                check_id="C8",
                title="Change outside the plan's scope",
                status="fail",
                severity=CRITICAL,
                entity=name,
                message=(
                    f"The audit log records changes to screen set {name!r}, which does "
                    f"not appear anywhere in the deployment plan. "
                    + (f"Buttons touched: {', '.join(buttons)}. " if buttons else "")
                    + f"{len(entries)} audit row(s) are affected."
                ),
                rows=[e.row_no for e in entries],
            )
        )
        return er

    targets = plan.targets_for(plan_entity)
    _build_button_results(er, targets, entries)
    _claim_rows(res, er)

    _check_planned_buttons(er, plan)
    _check_unauthorized(er, plan)
    _check_tile_identity(er, plan, entries)
    _check_screen(er, plan, opts)
    _check_language_coverage(er, opts)
    _check_supporting_evidence(er, opts)
    _check_other_properties(res, er, entries)
    return er


def _btn_key(b: str) -> tuple[int, str]:
    return (int(b), b) if b.isdigit() else (10**9, b)


def _build_button_results(er: EntityResult, targets: list[Target], entries: list[AuditEntry]) -> None:
    """Bucket audit entries under the planned buttons, then under extras."""
    by_button: dict[str, list[AuditEntry]] = {}
    for e in entries:
        if e.button:
            by_button.setdefault(e.button, []).append(e)

    planned = {t.button for t in targets}

    for t in targets:
        br = ButtonResult(
            entity=er.entity,
            button=t.button,
            group=t.group,
            expected_state=t.expect_state,
            workflow=t.workflow,
        )
        _fill_button(br, by_button.get(t.button, []))
        br.status = _button_status(br, t)
        er.buttons.append(br)

    # Anything the audit log touched that the plan did not list.
    for btn, rows in sorted(by_button.items(), key=lambda kv: _btn_key(kv[0])):
        if btn in planned:
            continue
        br = ButtonResult(entity=er.entity, button=btn, group="(not in plan)")
        _fill_button(br, rows)
        br.status = UNAUTHORIZED
        er.unauthorized.append(br)


def _fill_button(br: ButtonResult, rows: list[AuditEntry]) -> None:
    """Attach every audit row for this button, losing none of them.

    The per-language dictionaries answer "which languages were covered?", so
    they hold one row each. A button can legitimately be worked on twice in one
    export, though, and an overwritten row must not vanish — it would resurface
    later as an unexplained change and be reported as a defect that never
    happened. Displaced rows are therefore kept alongside.
    """
    for e in rows:
        prop = e.prop.lower()
        if e.prop and not e.language and prop == "button":
            # Keep the most decisive toggle if a button appears twice.
            if br.toggle is None:
                br.toggle = e
            elif e.change == DISABLED and br.toggle.change != DISABLED:
                br.other.append(br.toggle)
                br.toggle = e
            else:
                br.other.append(e)
        elif prop == "caption":
            _place(br.caption_langs, br, e)
        elif prop in ("image name", "image", "imagename"):
            _place(br.image_langs, br, e)
        else:
            br.other.append(e)


def _place(langs: dict[str, AuditEntry], br: ButtonResult, e: AuditEntry) -> None:
    """Record a localised row, keeping any row it displaces."""
    key = e.language or "—"
    existing = langs.get(key)
    if existing is None:
        langs[key] = e
    else:
        # Keep the earlier row as the representative and park the repeat.
        br.other.append(e)


def _button_status(br: ButtonResult, target: Target) -> str:
    if br.toggle is None:
        return MISSING
    want = target.expect_state.strip().lower()
    got = (br.toggle.new or "").strip().lower()
    if got == want:
        return CONFIRMED
    # The button moved, but not to the state the plan asked for.
    return WRONG_DIRECTION


def _claim_rows(res: Result, er: EntityResult) -> None:
    for br in er.buttons:
        if br.toggle:
            res.attribution[br.toggle.row_no] = ATTR_EXPECTED
        for e in br.support_rows:
            res.attribution[e.row_no] = ATTR_SUPPORTING
    for br in er.unauthorized:
        for e in br.support_rows + ([br.toggle] if br.toggle else []):
            res.attribution[e.row_no] = ATTR_UNAUTHORIZED


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _check_planned_buttons(er: EntityResult, plan: Plan) -> None:
    """C1/C3 — every planned button reached its planned state."""
    for br in er.buttons:
        subject = f"{br.group} · button {br.button}"
        if br.status == CONFIRMED:
            er.findings.append(
                Finding(
                    check_id="C1",
                    title="Planned button change confirmed",
                    status="pass",
                    severity=INFO,
                    entity=er.entity,
                    subject=subject,
                    message=(
                        f"Button {br.button} ({br.group}) was set to "
                        f"{br.toggle.new} — {br.toggle.change_text}."
                    ),
                    evidence=[f"row {br.toggle.row_no}: {br.toggle.field_text}"],
                    rows=[br.toggle.row_no],
                )
            )
        elif br.status == WRONG_DIRECTION:
            er.findings.append(
                Finding(
                    check_id="C3",
                    title="Button changed to the wrong state",
                    status="fail",
                    severity=CRITICAL,
                    entity=er.entity,
                    subject=subject,
                    message=(
                        f"The plan requires button {br.button} ({br.group}) to end up "
                        f"{br.expected_state}, but the audit log records "
                        f"{br.toggle.change_text}."
                    ),
                    evidence=[f"row {br.toggle.row_no}: {br.toggle.field_text}"],
                    rows=[br.toggle.row_no],
                )
            )
        else:
            support = len(br.support_rows)
            extra = (
                f" {support} caption/image row(s) for this button are present, so the "
                f"tile content was edited but the button itself was never disabled."
                if support else
                " There is no audit activity for this button at all."
            )
            er.findings.append(
                Finding(
                    check_id="C1",
                    title="Planned button change missing",
                    status="fail",
                    severity=CRITICAL,
                    entity=er.entity,
                    subject=subject,
                    message=(
                        f"The plan requires button {br.button} ({br.group}) to be "
                        f"{br.expected_state}d, but the audit log contains no "
                        f"Button({br.button}) entry reaching {br.expected_state}."
                        + extra
                    ),
                    rows=[e.row_no for e in br.support_rows],
                )
            )


def _check_unauthorized(er: EntityResult, plan: Plan) -> None:
    """C2 — nothing outside the plan's button list was touched.

    This is the check the plan itself asks for: "Only these buttons should be
    disabled for your assigned coops."
    """
    for br in er.unauthorized:
        toggled = br.toggle is not None and br.toggle.change in (DISABLED, ENABLED)
        planned = ", ".join(b.button for b in er.buttons)
        if toggled:
            er.findings.append(
                Finding(
                    check_id="C2",
                    title="Unauthorized button change",
                    status="fail",
                    severity=CRITICAL,
                    entity=er.entity,
                    subject=f"button {br.button}",
                    message=(
                        f"Button {br.button} was {br.toggle.change} on {er.entity}, but "
                        f"the plan only authorises buttons {planned} for this screen "
                        f"set. {br.toggle.change_text}."
                    ),
                    evidence=[f"row {br.toggle.row_no}: {br.toggle.field_text}"],
                    rows=[br.toggle.row_no],
                )
            )
        else:
            er.findings.append(
                Finding(
                    check_id="C2",
                    title="Content changed on an unplanned button",
                    status="fail",
                    severity=CRITICAL,
                    entity=er.entity,
                    subject=f"button {br.button}",
                    message=(
                        f"Button {br.button} on {er.entity} was not disabled, but "
                        f"{len(br.support_rows)} of its caption/image field(s) were "
                        f"edited in the same save as this plan's work. The plan only "
                        f"lists buttons {planned}, so this content change was not "
                        f"requested. (Edits made in saves that did none of this "
                        f"plan's work are reported separately as other work.)"
                    ),
                    rows=[e.row_no for e in br.support_rows],
                )
            )


def _check_tile_identity(er: EntityResult, plan: Plan, entries: list[AuditEntry]) -> None:
    """C4 — the caption that was replaced is the tile the plan names."""
    expected = plan.meta.tile_label
    if not expected:
        return
    want = fold(expected)
    captions = [e for e in entries if e.prop.lower() == "caption" and e.old]
    if not captions:
        return

    mismatched = [e for e in captions if fold(e.old) != want]
    if not mismatched:
        er.findings.append(
            Finding(
                check_id="C4",
                title="Correct tile was cleaned up",
                status="pass",
                severity=INFO,
                entity=er.entity,
                message=(
                    f"All {len(captions)} caption change(s) replaced "
                    f"{expected!r}, which is the tile named in the plan."
                ),
                rows=[e.row_no for e in captions],
            )
        )
        return

    others = sorted({e.old for e in mismatched})
    er.findings.append(
        Finding(
            check_id="C4",
            title="A different tile's caption was changed",
            status="warn",
            severity=MAJOR,
            entity=er.entity,
            message=(
                f"{len(mismatched)} of {len(captions)} caption change(s) replaced a "
                f"caption other than the plan's tile {expected!r}. Replaced instead: "
                + "; ".join(repr(o) for o in others[:4])
                + ("…" if len(others) > 4 else "")
                + ". Confirm the right tile was cleaned up."
            ),
            rows=[e.row_no for e in mismatched],
        )
    )


def _check_screen(er: EntityResult, plan: Plan, opts: Options) -> None:
    """C5 — the change was made on the screen the plan targets."""
    plan_screen = plan.meta.screen_name
    if not plan_screen or not er.screen:
        return
    # The audit log prefixes the screen with a device name ("Kiosk 6 Left Hand
    # Navigation" vs the plan's "Left hand navigation"), so containment either
    # way counts as a match.
    a, b = fold(er.screen), fold(plan_screen)
    if a and b and (b in a or a in b):
        er.findings.append(
            Finding(
                check_id="C5",
                title="Change made on the planned screen",
                status="pass",
                severity=INFO,
                entity=er.entity,
                message=(
                    f"Audit screen {er.screen!r} matches the plan's screen "
                    f"{plan_screen!r}"
                    + (f" (screen {plan.meta.screen_number})" if plan.meta.screen_number else "")
                    + "."
                ),
            )
        )
        return
    er.findings.append(
        Finding(
            check_id="C5",
            title="Screen does not match the plan",
            status="fail" if opts.strict_screen_match else "warn",
            severity=MAJOR,
            entity=er.entity,
            message=(
                f"The plan targets screen {plan.meta.screen_number or ''} "
                f"{plan_screen!r}, but the audit log records changes on "
                f"{er.screen!r}."
            ),
        )
    )


def _check_language_coverage(er: EntityResult, opts: Options) -> None:
    """C6 — how evenly the tile content was blanked across languages.

    The set of languages a market actually configures is not knowable from
    either document, so the best-covered button in the same screen set is used
    as the yardstick. Reported as an observation unless parity is demanded.
    """
    confirmed = [b for b in er.buttons if b.status == CONFIRMED]
    if not confirmed or not er.languages:
        return
    best = max(len(b.languages) for b in confirmed)
    if best == 0:
        return
    uneven = [b for b in confirmed if len(b.languages) < best]
    total_langs = len(er.languages)

    if not uneven:
        er.findings.append(
            Finding(
                check_id="C6",
                title="Language coverage is consistent",
                status="pass",
                severity=INFO,
                entity=er.entity,
                message=(
                    f"Every confirmed button has tile content cleared for the same "
                    f"{best} language(s): {', '.join(er.languages)}."
                ),
            )
        )
        return

    detail = "; ".join(
        f"button {b.button} covers {len(b.languages)}/{total_langs}"
        + (f" ({', '.join(b.languages)})" if b.languages else " (none)")
        for b in uneven
    )
    er.findings.append(
        Finding(
            check_id="C6",
            title="Uneven language coverage",
            status="fail" if opts.require_language_parity else "info",
            severity=MINOR if opts.require_language_parity else INFO,
            entity=er.entity,
            message=(
                f"Caption/image rows are not evenly spread across languages. The "
                f"best-covered button reaches {best} of {total_langs} language(s), "
                f"while: {detail}. This is normal when a language was already blank "
                f"for that button — the audit log only records fields that actually "
                f"changed — so it is reported as an observation, not a defect."
            ),
            rows=[e.row_no for b in uneven for e in b.support_rows],
        )
    )


def _check_supporting_evidence(er: EntityResult, opts: Options) -> None:
    """C7 — a disabled button also had its tile content blanked."""
    bare = [b for b in er.buttons if b.status == CONFIRMED and not b.support_rows]
    if not bare:
        return
    er.findings.append(
        Finding(
            check_id="C7",
            title="Button disabled with no content change",
            status="fail" if opts.require_supporting_evidence else "info",
            severity=MINOR if opts.require_supporting_evidence else INFO,
            entity=er.entity,
            message=(
                "Button(s) "
                + ", ".join(b.button for b in bare)
                + " were disabled without any accompanying caption or image change. "
                "That is expected when the tile was already blank, but worth a look "
                "if the plan also required the content to be cleared."
            ),
        )
    )


def _check_other_properties(res: Result, er: EntityResult, entries: list[AuditEntry]) -> None:
    """C9 — properties that are neither the button nor its tile content.

    These split into two very different cases, and conflating them used to hide
    the serious one. A property hanging off a *planned* button (a workflow on
    button 12, say) accompanies work that was asked for, and is worth no more
    than a warning. A property that belongs to no planned button at all —
    including a Field name the parser cannot tie to anything — is a change
    nobody requested, which is the whole point of this tool, so it fails.
    """
    known = {"button", "caption", "image name", "image", "imagename"}
    odd = [e for e in entries if e.prop and e.prop.lower() not in known]
    if not odd:
        return

    # By this point every row tied to a planned button is already SUPPORTING and
    # every row on an unplanned button is UNAUTHORIZED. What is still
    # unattributed belongs to nothing the plan describes.
    attached = [e for e in odd if res.attribution.get(e.row_no) == ATTR_SUPPORTING]
    stray = [e for e in odd if res.attribution.get(e.row_no) == ATTR_UNATTRIBUTED]

    if attached:
        props = sorted({e.prop for e in attached})
        er.findings.append(
            Finding(
                check_id="C9",
                title="Other properties were changed",
                status="warn",
                severity=MINOR,
                entity=er.entity,
                message=(
                    f"{len(attached)} audit row(s) changed properties beyond the button "
                    f"and its tile content, on buttons the plan does cover: "
                    f"{', '.join(props)}. Confirm these were intended."
                ),
                rows=[e.row_no for e in attached],
            )
        )

    if stray:
        for e in stray:
            res.attribution[e.row_no] = ATTR_UNAUTHORIZED
        props = sorted({e.prop for e in stray})
        er.findings.append(
            Finding(
                check_id="C15",
                title="Changes the plan does not account for",
                status="fail",
                severity=CRITICAL,
                entity=er.entity,
                message=(
                    f"{len(stray)} audit row(s) on {er.entity} changed "
                    f"{', '.join(props[:6])}, which belongs to no button this plan "
                    f"asks for. Each is a change nobody requested, or a Field the "
                    f"system records in a form this parser does not recognise."
                ),
                rows=[e.row_no for e in stray],
            )
        )


# ---------------------------------------------------------------------------
# Global checks
# ---------------------------------------------------------------------------


def _separate_unrelated_work(res: Result, log: AuditLog) -> None:
    """C17 — tell a stray change apart from somebody else's task.

    An audit export is a time window, not a deployment. It routinely contains
    several unrelated pieces of work, and calling all of it "unauthorized"
    buries the one case that matters under hundreds that do not.

    The discriminator is the transaction id. A change made in the *same save*
    as planned work was almost certainly made by the person carrying out this
    plan — that is a real defect. A change made in a transaction that did none
    of this plan's work is a different task, reported for information only.
    """
    planned_tx = {
        e.audit_id
        for e in log.entries
        if e.audit_id and res.attribution.get(e.row_no) in (ATTR_EXPECTED, ATTR_SUPPORTING)
    }
    if not planned_tx:
        return

    def touches_planned_tx(br: ButtonResult) -> bool:
        rows = br.support_rows + ([br.toggle] if br.toggle else [])
        return any(e.audit_id in planned_tx for e in rows if e.audit_id)

    moved: list[ButtonResult] = []
    for er in res.entities:
        stays, goes = [], []
        for br in er.unauthorized:
            (stays if touches_planned_tx(br) else goes).append(br)
        if not goes:
            continue
        er.unauthorized, er.other_work = stays, goes
        moved.extend(goes)
        # Their C2 findings no longer apply: they are not this deployment.
        moved_buttons = {b.button for b in goes}
        er.findings = [
            f for f in er.findings
            if not (f.check_id == "C2" and f.subject.replace("button ", "") in moved_buttons)
        ]
        for br in goes:
            for e in br.support_rows + ([br.toggle] if br.toggle else []):
                res.attribution[e.row_no] = ATTR_OTHER_WORK

    # The same reasoning applies to rows that never reached a button at all —
    # a colour, a width, a menu-item number. If their save did none of this
    # plan's work, they belong to that other task too, and leaving them
    # "unexplained" would swamp the one finding that matters.
    stray_rows = [
        e for e in log.entries
        if res.attribution.get(e.row_no) == ATTR_UNATTRIBUTED
        and e.audit_id
        and e.audit_id not in planned_tx
    ]
    for e in stray_rows:
        res.attribution[e.row_no] = ATTR_OTHER_WORK

    if not moved and not stray_rows:
        return

    rows = [e for br in moved for e in br.support_rows + ([br.toggle] if br.toggle else [])]
    rows += stray_rows
    txs = {e.audit_id for e in rows if e.audit_id}
    users = sorted({e.user for e in rows if e.user})
    sets = sorted({br.entity for br in moved})
    res.findings.append(
        Finding(
            check_id="C17",
            title="Other work shares this audit export",
            status="info",
            severity=INFO,
            message=(
                f"{len(rows)} row(s) across {len(txs)} transaction(s) changed "
                f"{len(moved)} button(s) on {len(sets)} screen set(s), in saves that "
                f"did none of this plan's work. That is a separate task sharing the "
                f"export window, not a deviation from this plan"
                + (f" (by {', '.join(users[:3])}" + (", …)" if len(users) > 3 else ")")
                   if users else "")
                + ". Anything changed in the same save as the planned work is "
                "reported as unauthorized instead."
            ),
            rows=[e.row_no for e in rows][:40],
        )
    )


def _check_not_attempted(res: Result, plan: Plan, opts: Options, audited: list[str]) -> None:
    """C10 — plan entities the audit log says nothing about."""
    audited_keys = {norm_entity(a) for a in audited}
    resolved = {norm_entity(e.plan_entity) for e in res.entities if e.plan_entity}
    pending = [
        e for e in plan.entities
        if norm_entity(e) not in audited_keys and norm_entity(e) not in resolved
    ]
    res.not_attempted = pending
    if not pending:
        return

    if opts.require_full_plan_coverage:
        res.findings.append(
            Finding(
                check_id="C10",
                title="Plan not fully covered",
                status="fail",
                severity=CRITICAL,
                message=(
                    f"{len(pending)} of {len(plan.entities)} screen sets in the plan "
                    f"have no audit activity: "
                    + ", ".join(pending[:8]) + ("…" if len(pending) > 8 else "")
                ),
            )
        )
    else:
        res.findings.append(
            Finding(
                check_id="C10",
                title="Plan entities not in this audit log",
                status="info",
                severity=INFO,
                message=(
                    f"This audit report covers {len(res.entities)} of the plan's "
                    f"{len(plan.entities)} screen sets. The remaining {len(pending)} "
                    f"are neither confirmed nor contradicted here — validate them "
                    f"with their own audit exports."
                ),
            )
        )


def _check_operation(res: Result, plan: Plan, log: AuditLog) -> None:
    """C11 — the audit rows are the kind of operation the plan describes."""
    expected = (plan.meta.operation or "").strip()
    if not expected or not log.operations:
        return
    want = fold(expected)
    odd = [o for o in log.operations if fold(o) != want]
    if not odd:
        return
    res.findings.append(
        Finding(
            check_id="C11",
            title="Unexpected audit operation",
            status="warn",
            severity=MINOR,
            message=(
                f"The plan describes a {expected!r} change, but the audit log also "
                f"contains: {', '.join(odd)}."
            ),
        )
    )


def _check_dates(res: Result, log: AuditLog) -> None:
    """C12 — the effective dates in the report agree with each other."""
    eff = {e.effective_from for e in log.entries if e.effective_from}
    if len(eff) > 1:
        res.findings.append(
            Finding(
                check_id="C12",
                title="Mixed effective dates",
                status="warn",
                severity=MINOR,
                message=(
                    "Audit rows carry more than one Effective From date: "
                    + ", ".join(sorted(eff))
                    + ". A single deployment normally shares one effective date."
                ),
            )
        )


def _check_provenance(res: Result, log: AuditLog) -> None:
    """C13 — who made the changes, and in how many transactions."""
    span = log.timespan()
    bits: list[str] = []
    if log.audit_ids:
        bits.append(
            f"{len(log.audit_ids)} transaction id"
            + ("s" if len(log.audit_ids) > 1 else "")
            + f" ({', '.join(log.audit_ids[:4])}{'…' if len(log.audit_ids) > 4 else ''})"
        )
    if log.users:
        bits.append(f"changed by {', '.join(log.users)}")
    if span:
        bits.append(
            f"at {span[0]:%d %b %Y %H:%M:%S}"
            if span[0] == span[1]
            else f"between {span[0]:%d %b %Y %H:%M:%S} and {span[1]:%d %b %Y %H:%M:%S}"
        )
    if not bits:
        return
    res.findings.append(
        Finding(
            check_id="C13",
            title="Change provenance",
            status="info",
            severity=INFO,
            message="; ".join(bits) + ".",
        )
    )


def _check_assignees(res: Result, plan: Plan, log: AuditLog) -> None:
    """C16 — the work was done by someone the plan does not name.

    Only meaningful when the plan actually lists assignees; most do not, and a
    check that fires on missing data is worse than no check. Matching is by
    surname/first-name token, because a plan writes "Yugdeep" where the audit
    log writes "er924071 - Yugdeep Parihar".
    """
    named = {
        tok
        for a in plan.assignments
        for field_value in (a.assignee, a.validator)
        if field_value
        for tok in fold(field_value).split()
        if len(tok) > 2
    }
    if not named:
        return

    unknown = []
    for user in log.users:
        tokens = {t for t in fold(user).split() if len(t) > 2}
        if tokens and not (tokens & named):
            unknown.append(user)
    if not unknown:
        return

    res.findings.append(
        Finding(
            check_id="C16",
            title="Changed by someone the plan does not name",
            status="warn",
            severity=MAJOR,
            message=(
                f"{', '.join(unknown)} made changes in this log, but "
                f"{'is' if len(unknown) == 1 else 'are'} not listed as an assignee "
                f"or validator anywhere in the plan. That may simply mean the plan's "
                f"assignment sheet is out of date."
            ),
            rows=[e.row_no for e in log.entries if e.user in unknown][:40],
        )
    )


def _check_unattributed(res: Result, log: AuditLog) -> None:
    """C14 — no audit row is left unexplained by this report."""
    left = [e for e in log.entries if res.attribution.get(e.row_no) == ATTR_UNATTRIBUTED]
    if not left:
        return
    res.findings.append(
        Finding(
            check_id="C14",
            title="Audit rows that cannot be explained",
            status="fail",
            severity=CRITICAL,
            message=(
                f"{len(left)} audit row(s) cannot be tied to anything the plan "
                f"describes. An unexplained change is exactly what this check "
                f"exists to surface, so it counts against the verdict rather than "
                f"being noted in passing."
            ),
            rows=[e.row_no for e in left],
        )
    )


def _carry_parse_warnings(res: Result, plan: Plan, log: AuditLog) -> None:
    """Collect document-authoring concerns, kept out of the verdict."""
    for w in plan.warnings:
        res.document_findings.append(
            Finding(
                check_id="P1", title="Deployment plan", status="warn",
                severity=MINOR, message=w,
            )
        )
    for w in log.warnings:
        res.document_findings.append(
            Finding(
                check_id="P2", title="Audit log", status="warn",
                severity=MINOR, message=w,
            )
        )


def _validate_by_evidence(res: Result, plan: Plan, log: AuditLog) -> None:
    """Verdict for a plan with no button grid, from value matching alone.

    The question becomes: does every change in the audit log correspond to
    something this plan actually names, and did the values the plan names
    actually turn up? That is answerable without knowing the operation.
    """
    ev = res.evidence
    if ev is None:
        res.findings.append(
            Finding(
                check_id="E0", title="Nothing to match", status="fail", severity=CRITICAL,
                message="No checkable values could be harvested from this plan.",
            )
        )
        return

    for c in ev.matched:
        for e in c.entries:
            if res.attribution.get(e.row_no) == ATTR_UNATTRIBUTED:
                res.attribution[e.row_no] = ATTR_EXPECTED

    unexplained = ev.unexplained(log)
    for e in unexplained:
        res.attribution[e.row_no] = ATTR_UNAUTHORIZED

    matched_kinds: dict[str, int] = {}
    for c in ev.matched:
        matched_kinds[c.kind] = matched_kinds.get(c.kind, 0) + 1

    if ev.matched:
        res.findings.append(
            Finding(
                check_id="E1",
                title="Plan values found in the audit log",
                status="pass",
                severity=INFO,
                message=(
                    f"{len(ev.matched)} of the {len(ev.claims)} value(s) named by this "
                    f"plan appear in the audit log — "
                    + ", ".join(
                        f"{n} {EV_KIND_LABEL.get(k, k).lower()}" for k, n in sorted(matched_kinds.items())
                    )
                    + ". These are the changes the plan asked for and the log confirms."
                ),
                rows=sorted(res.evidence.explained_rows)[:40],
            )
        )
    else:
        res.findings.append(
            Finding(
                check_id="E1",
                title="Plan and audit log do not correspond",
                status="fail",
                severity=CRITICAL,
                message=(
                    f"None of the {len(ev.claims)} value(s) named by this plan appear "
                    f"anywhere in this audit log. The two files are most likely for "
                    f"different pieces of work."
                ),
            )
        )

    if unexplained:
        props = sorted({e.prop for e in unexplained if e.prop})
        res.findings.append(
            Finding(
                check_id="E2",
                title="Changes the plan does not account for",
                status="fail" if ev.matched else "warn",
                severity=CRITICAL if ev.matched else MAJOR,
                message=(
                    f"{len(unexplained)} audit row(s) change values this plan never "
                    f"mentions"
                    + (f" (fields: {', '.join(props[:6])})" if props else "")
                    + ". Each one is a change nobody asked for, or a value the plan "
                    "records differently from the system."
                ),
                rows=[e.row_no for e in unexplained],
            )
        )
    elif ev.matched:
        res.findings.append(
            Finding(
                check_id="E2",
                title="Every audit change is accounted for",
                status="pass",
                severity=INFO,
                message=(
                    f"All {len(log.entries)} audit row(s) correspond to values named "
                    f"in the plan. Nothing was changed that the plan does not mention."
                ),
            )
        )

    if ev.absent:
        by_kind: dict[str, list] = {}
        for c in ev.absent:
            by_kind.setdefault(c.kind, []).append(c)
        detail = "; ".join(
            f"{len(v)} {EV_KIND_LABEL.get(k, k).lower()}" for k, v in sorted(by_kind.items())
        )
        res.findings.append(
            Finding(
                check_id="E3",
                title="Plan values not present in this audit log",
                status="info",
                severity=INFO,
                message=(
                    f"{len(ev.absent)} value(s) named by the plan do not appear here "
                    f"({detail}). For a plan covering many screen sets this is "
                    f"expected — one export covers one slice of the work."
                ),
            )
        )


def _validate_without_screen_set(res: Result, plan: Plan, log: AuditLog, opts: Options) -> None:
    """Fallback when no Description names a screen set.

    Button numbers alone cannot identify which screen set was changed, so the
    log is checked against every plan entity and the best match wins. The
    result is reported as low-confidence.
    """
    disabled = {e.button for e in log.entries if e.is_button_toggle and e.change == DISABLED}
    best_entity, best_score = None, -1
    for entity in plan.entities:
        planned = {t.button for t in plan.targets_for(entity)}
        score = len(planned & disabled) - len(disabled - planned)
        if score > best_score:
            best_entity, best_score = entity, score

    res.findings.append(
        Finding(
            check_id="C0",
            title="Screen set inferred from button numbers",
            status="warn",
            severity=MAJOR,
            message=(
                "No audit row names a screen set, so the target could not be read "
                "from the log. "
                + (
                    f"Buttons {', '.join(sorted(disabled, key=_btn_key))} best match "
                    f"{best_entity!r}, which was used for validation. Treat this "
                    f"result as indicative only."
                    if best_entity else
                    "No screen set could be inferred."
                )
            ),
        )
    )
    if best_entity:
        er = EntityResult(entity=best_entity, plan_entity=best_entity, entry_count=len(log.entries))
        er.screen = next((e.screen for e in log.entries if e.screen), "")
        er.languages = sorted({e.language for e in log.entries if e.language})
        _build_button_results(er, plan.targets_for(best_entity), log.entries)
        _claim_rows(res, er)
        _check_planned_buttons(er, plan)
        _check_unauthorized(er, plan)
        _check_tile_identity(er, plan, log.entries)
        _check_language_coverage(er, opts)
        _check_other_properties(res, er, log.entries)
        res.entities.append(er)
