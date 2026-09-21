"""
Audit Master — a Flask app that validates deployment work from audit logs.

Upload a deployment plan (any format) and an audit-log export; the app
normalizes the plan, decomposes the audit log, reconciles the two, and reports
whether the work was done correctly, what is missing, and — the part a human
reviewer usually misses — what was changed that nobody asked for.

Everything runs locally. There is no API key, no model call and no outbound
network request anywhere in this application; every judgement comes from the
rules in :mod:`auditmaster.validator`.

Run:
    ./run.sh                      (or)
    .venv/bin/python app.py
"""

from __future__ import annotations

import secrets
import sys
import traceback
from pathlib import Path
from collections import OrderedDict
from dataclasses import dataclass

from flask import (
    Flask,
    Response,
    abort,
    redirect,
    render_template,
    request,
    url_for,
)

from auditmaster import log_summary, report
from auditmaster.evidence import KIND_LABEL as EV_KIND_LABEL
from auditmaster.evidence import OUTCOME_LABEL as EV_OUTCOME_LABEL
from auditmaster.audit_parser import AuditParseError, is_audit_log, parse_audit_log
from auditmaster.plan_parser import PlanParseError, parse_plan
from auditmaster.readers import SUPPORTED_SUFFIXES, ReadError, read_any
from auditmaster.validator import (
    ATTR_HELP,
    ATTR_LABEL,
    ATTR_SUPPORTING,
    ATTR_UNAUTHORIZED,
    ATTR_UNATTRIBUTED,
    CONFIRMED,
    FAIL,
    MISSING,
    PASS,
    PASS_WARN,
    UNAUTHORIZED,
    Options,
    Result,
    validate,
)

MAX_UPLOAD_MB = 32
MAX_RESULTS_HELD = 24          # in-memory result cache, oldest evicted
#: Audit rows rendered in the browser. A 13k-row export becomes a ~10MB page
#: that locks the tab up, so the table is capped and the cap is stated on it.
MAX_ROWS_RENDERED = 500
#: Issues that get their full audit rows inline; beyond this, row numbers only.
MAX_ISSUES_DETAILED = 25

def _resource_dir() -> Path:
    """Where ``templates/`` and ``static/`` live.

    PyInstaller unpacks bundled data into a temporary directory and points
    ``sys._MEIPASS`` at it, so a frozen build must look there rather than
    beside the executable.
    """
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent


BASE_DIR = _resource_dir()

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024


# ---------------------------------------------------------------------------
# Result store
# ---------------------------------------------------------------------------


@dataclass
class Held:
    """One in-memory item: a validation result, or a standalone log summary."""

    kind: str          # "result" | "summary"
    payload: object


_RESULTS: "OrderedDict[str, Held]" = OrderedDict()


def _hold(kind: str, payload: object) -> str:
    token = secrets.token_urlsafe(12)
    _RESULTS[token] = Held(kind=kind, payload=payload)
    while len(_RESULTS) > MAX_RESULTS_HELD:
        _RESULTS.popitem(last=False)
    return token


def _get(token: str, kind: str = "result"):
    held = _RESULTS.get(token)
    if held is None or held.kind != kind:
        abort(404, "That result has expired. Please upload the file(s) again.")
    _RESULTS.move_to_end(token)
    return held.payload


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

_STATUS_CLASS = {
    "pass": "ok", "fail": "bad", "warn": "warn", "info": "info",
}
_BUTTON_CLASS = {
    CONFIRMED: "ok", MISSING: "bad", UNAUTHORIZED: "bad", "wrong_direction": "bad",
}
_ATTR_CLASS = {
    "expected": "ok",
    ATTR_SUPPORTING: "support",
    ATTR_UNAUTHORIZED: "bad",
    "other_work": "other",
    "out_of_scope": "bad",
    ATTR_UNATTRIBUTED: "warn",
}
_VERDICT_CLASS = {PASS: "ok", PASS_WARN: "warn", FAIL: "bad"}


#: An audit value is whatever the audited system recorded, which can be
#: arbitrarily long. Showing it whole would wreck the table layout, so it is
#: clipped for display and carried in full in the cell's tooltip.
MAX_CELL_CHARS = 300


def clip(value: object, limit: int = MAX_CELL_CHARS) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit] + "…"


@app.context_processor
def _helpers() -> dict:
    return {
        "status_class": lambda s: _STATUS_CLASS.get(s, "info"),
        "button_class": lambda s: _BUTTON_CLASS.get(s, "warn"),
        "attr_class": lambda s: _ATTR_CLASS.get(s, "info"),
        "verdict_class": lambda v: _VERDICT_CLASS.get(v, "info"),
        "clip": clip,
        "attr_label": ATTR_LABEL,
        "attr_help": ATTR_HELP,
        "ev_kind": EV_KIND_LABEL,
        "ev_outcome": EV_OUTCOME_LABEL,
        "PASS": PASS,
        "FAIL": FAIL,
        "PASS_WARN": PASS_WARN,
        "CONFIRMED": CONFIRMED,
        "supported": ", ".join(SUPPORTED_SUFFIXES),
        "max_mb": MAX_UPLOAD_MB,
        "row_cap": MAX_ROWS_RENDERED,
        "detail_cap": MAX_ISSUES_DETAILED,
        "change_label": log_summary.CHANGE_LABEL,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
def index():
    return render_template("index.html", error=None)


@app.post("/validate")
def do_validate():
    plan_file = request.files.get("plan")
    log_file = request.files.get("audit")

    if not log_file or not log_file.filename:
        return render_template("index.html", error="Please choose an audit log file."), 400

    log_raw, log_name = log_file.read(), log_file.filename
    has_plan = bool(plan_file and plan_file.filename)
    plan_raw, plan_name = (plan_file.read(), plan_file.filename) if has_plan else (b"", "")

    # No plan given: describe the audit log on its own.
    if not has_plan:
        try:
            log = parse_audit_log(log_raw, log_name)
        except AuditParseError as exc:
            return render_template(
                "index.html",
                error=_explain_audit_failure(log_raw, log_name, exc),
            ), 400
        except Exception:  # pragma: no cover - defensive
            app.logger.error("Unexpected failure:\n%s", traceback.format_exc())
            return render_template(
                "index.html",
                error="Something went wrong reading that file.",
            ), 500
        token = _hold("summary", log_summary.summarize(log))
        return redirect(url_for("show_summary", token=token))

    options = Options(
        require_full_plan_coverage=bool(request.form.get("require_full_plan_coverage")),
        require_supporting_evidence=bool(request.form.get("require_supporting_evidence")),
        require_language_parity=bool(request.form.get("require_language_parity")),
        strict_screen_match=bool(request.form.get("strict_screen_match")),
    )

    swapped = False
    try:
        # Uploading the two files the wrong way round is an easy slip; detect it
        # rather than failing with a confusing parse error.
        if _looks_like_log(plan_raw, plan_name) and not _looks_like_log(log_raw, log_name):
            plan_raw, plan_name, log_raw, log_name = log_raw, log_name, plan_raw, plan_name
            swapped = True

        plan = parse_plan(plan_raw, plan_name)
        log = parse_audit_log(log_raw, log_name)
    except PlanParseError as exc:
        return render_template(
            "index.html",
            error=f"\u201c{plan_name}\u201d could not be read as a deployment plan. {exc}",
        ), 400
    except AuditParseError as exc:
        return render_template(
            "index.html",
            error=_explain_audit_failure(log_raw, log_name, exc),
        ), 400
    except Exception:  # pragma: no cover - defensive
        app.logger.error("Unexpected failure:\n%s", traceback.format_exc())
        return render_template(
            "index.html",
            error=(
                "Something went wrong reading those files. Check that the plan is a "
                "spreadsheet with a screen-set / button table and that the audit log "
                "is the standard export."
            ),
        ), 500

    result = validate(plan, log, options)
    if swapped:
        result.log.notes.append(
            "The two uploads appeared to be the wrong way round and were swapped "
            "automatically."
        )
    token = _hold("result", result)
    return redirect(url_for("show_result", token=token))


def _explain_audit_failure(raw: bytes, name: str, exc: Exception) -> str:
    """Why an audit log would not read, in terms of what the user did.

    The parser's own message explains the file format. The likeliest cause is
    simpler than that — the two files went into the wrong boxes — so check for
    it and say so before falling back to the technical detail.
    """
    try:
        parse_plan(raw, name)
        looks_like_a_plan = True
    except Exception:
        looks_like_a_plan = False

    if looks_like_a_plan:
        return (
            f"\u201c{name}\u201d looks like a deployment plan, not an audit log. "
            f"Put the audit log in the first box and this file in the second."
        )
    return (
        f"\u201c{name}\u201d could not be read as an audit log. It should be the "
        f"export with Field, Old Setting and New Setting columns. "
        f"Technical detail: {exc}"
    )


def _looks_like_log(raw: bytes, filename: str) -> bool:
    try:
        return is_audit_log(read_any(raw, filename))
    except (ReadError, Exception):
        return False


@app.get("/result/<token>")
def show_result(token: str):
    res = _get(token, "result")
    return render_template(
        "result.html",
        r=res,
        token=token,
        headline=report.headline(res),
        narrative=report.narrative(res),
        next_steps=report.next_steps(res),
        attribution_counts=res.attribution_counts(),
    )


@app.get("/result/<token>/export.<fmt>")
def export(token: str, fmt: str):
    res = _get(token, "result")
    stem = "audit-validation"
    builders = {
        "json": (report.to_json, "application/json", f"{stem}.json"),
        "csv": (report.to_csv, "text/csv", f"{stem}-rows.csv"),
        "txt": (report.to_text, "text/plain", f"{stem}.txt"),
    }
    if fmt not in builders:
        abort(404)
    build, mime, name = builders[fmt]
    return Response(
        build(res),
        mimetype=f"{mime}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/result/<token>/plan-template.json")
def plan_template(token: str):
    """Download the normalized plan, for hand-correcting a mis-parsed layout."""
    res = _get(token, "result")
    return Response(
        report.plan_template_json(res.plan),
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="plan-template.json"'},
    )


@app.get("/summary/<token>")
def show_summary(token: str):
    s = _get(token, "summary")
    return render_template(
        "summary.html",
        s=s,
        token=token,
        headline=log_summary.headline(s),
        narrative=log_summary.narrative(s),
    )


@app.get("/summary/<token>/export.txt")
def summary_export(token: str):
    s = _get(token, "summary")
    lines = [
        f"Audit log summary - {s.log.filename}",
        "=" * 72, "",
        *log_summary.narrative(s), "",
    ]
    if s.observations:
        lines += ["Observations", "-" * 72]
        for o in s.observations:
            lines.append(f"[{o.level.upper()}] {o.title}: {o.message}")
            if o.rows:
                lines.append(f"        rows: {', '.join(str(r) for r in o.rows[:30])}")
        lines.append("")
    lines += ["Screen sets", "-" * 72]
    for b in s.screen_sets:
        lines.append(
            f"  {b.name} - {b.rows} row(s)"
            + (f", buttons {', '.join(b.buttons)}" if b.buttons else "")
            + (f", {len(b.languages)} language(s)" if b.languages else "")
        )
    body = "\n".join(lines) + "\n"
    return Response(
        body, mimetype="text/plain",
        headers={"Content-Disposition": 'attachment; filename="audit-log-summary.txt"'},
    )


@app.get("/health")
def health():
    return {"status": "ok", "results_held": len(_RESULTS)}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@app.errorhandler(404)
def not_found(exc):
    return render_template("error.html", code=404, message=getattr(exc, "description", "Not found")), 404


@app.errorhandler(413)
def too_large(exc):
    return render_template(
        "error.html",
        code=413,
        message=f"That file is larger than the {MAX_UPLOAD_MB} MB limit.",
    ), 413


if __name__ == "__main__":
    # Developer entry point only. Everyone else goes through launch.py, which
    # is what run.sh / run.bat and the packaged build use.
    #
    # Debug mode is opt-in rather than the default: Werkzeug's debugger hands
    # an interactive Python console to anyone who can reach the port and
    # trigger an exception, which must never be the case for a shared tool.
    import os

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=os.environ.get("AUDITMASTER_DEBUG") == "1",
    )
