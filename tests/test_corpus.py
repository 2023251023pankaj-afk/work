"""
Robustness sweep over every real plan and audit log available locally.

This is the "it must not fall over on whatever turns up next" test. It does not
assert that any particular plan yields particular targets — those files change.
It asserts the properties that must hold for *every* file:

  * reading a plan either succeeds or raises PlanParseError — never anything else;
  * a plan that parses structurally never invents a screen set (a number, a
    column heading such as "Button", or a section title);
  * every audit log parses;
  * every plan validated against every log produces a result and a readable
    report without raising.

Drop new plans and logs into the corpus directory and they are covered.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from auditmaster import report                                    # noqa: E402
from auditmaster.audit_parser import AuditParseError, parse_audit_log   # noqa: E402
from auditmaster.plan_parser import PlanParseError, parse_plan    # noqa: E402
from auditmaster.validator import Options, validate               # noqa: E402

#: Folders scanned for real-world files. `samples/` holds the plans and logs
#: shipped with the project; any other folder dropped beside it is picked up
#: too, so adding files needs no code change.
SAMPLES = ROOT / "samples"
CORPUS_DIRS = [ROOT, SAMPLES] + sorted(
    d for d in ([*SAMPLES.iterdir()] if SAMPLES.is_dir() else []) if d.is_dir()
)

PLAN_SUFFIXES = (".xlsx", ".xlsm")
LOG_SUFFIXES = (".csv",)

#: Entity names that mean the parser invented structure rather than read it.
BAD_ENTITY_WORDS = {
    "button", "buttons", "daypart", "dayparts", "screen number", "screen set",
    "screenset", "menu item", "menu item number", "menu item name", "name",
    "number", "display order", "caption", "image name", "manual configuration",
    "instruction", "instructions", "validation", "assignment",
}


def _collect() -> tuple[list[Path], list[Path]]:
    plans: list[Path] = []
    logs: list[Path] = []
    for d in CORPUS_DIRS:
        for f in sorted(d.iterdir()):
            if not f.is_file() or f.name.startswith("~$"):
                continue
            if f.suffix.lower() in PLAN_SUFFIXES:
                plans.append(f)
            elif f.suffix.lower() in LOG_SUFFIXES:
                logs.append(f)
    return plans, logs


PLANS, LOGS = _collect()


class TestCorpusAuditLogs(unittest.TestCase):
    def test_every_audit_log_parses(self):
        if not LOGS:
            self.skipTest("no audit logs available")
        for path in LOGS:
            with self.subTest(log=path.name):
                try:
                    log = parse_audit_log(path.read_bytes(), path.name)
                except AuditParseError as exc:
                    self.fail(f"{path.name}: {exc}")
                self.assertGreater(len(log.entries), 0, path.name)


class TestCorpusPlans(unittest.TestCase):
    def test_every_plan_parses_or_refuses_cleanly(self):
        if not PLANS:
            self.skipTest("no plans available")
        for path in PLANS:
            with self.subTest(plan=path.name):
                try:
                    parse_plan(path.read_bytes(), path.name)
                except PlanParseError:
                    pass          # a clear refusal is a valid outcome
                except Exception as exc:                      # noqa: BLE001
                    self.fail(f"{path.name} raised {type(exc).__name__}: {exc}")

    def test_no_plan_invents_a_screen_set(self):
        """A structured parse must not turn headings or numbers into entities.

        This is the guard against the failure that matters most: not an error,
        but a confident, wrong answer built out of a lookup table.
        """
        if not PLANS:
            self.skipTest("no plans available")
        for path in PLANS:
            with self.subTest(plan=path.name):
                try:
                    plan = parse_plan(path.read_bytes(), path.name)
                except PlanParseError:
                    continue
                for entity in plan.entities:
                    key = " ".join(entity.split()).strip().lower()
                    self.assertFalse(
                        key.isdigit(),
                        f"{path.name}: numeric screen set {entity!r}",
                    )
                    self.assertNotIn(
                        key, BAD_ENTITY_WORDS,
                        f"{path.name}: {entity!r} is a column heading, not a screen set",
                    )


class TestCorpusCrossProduct(unittest.TestCase):
    def test_every_plan_against_every_log(self):
        if not PLANS or not LOGS:
            self.skipTest("corpus incomplete")
        parsed = []
        for path in PLANS:
            try:
                parsed.append((path.name, parse_plan(path.read_bytes(), path.name)))
            except PlanParseError:
                continue
        logs = [(p.name, parse_audit_log(p.read_bytes(), p.name)) for p in LOGS]

        for plan_name, plan in parsed:
            for log_name, log in logs:
                with self.subTest(plan=plan_name, log=log_name):
                    try:
                        res = validate(plan, log, Options())
                        self.assertIn(res.verdict, ("PASS", "PASS_WITH_WARNINGS", "FAIL"))
                        self.assertTrue(report.headline(res))
                        report.narrative(res)
                        report.to_text(res)
                        report.to_csv(res)
                        report.to_json(res)
                    except Exception as exc:                  # noqa: BLE001
                        self.fail(
                            f"{plan_name} x {log_name} raised "
                            f"{type(exc).__name__}: {exc}"
                        )


if __name__ == "__main__":
    unittest.main()
