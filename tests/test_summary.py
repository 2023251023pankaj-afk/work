"""
Tests for the standalone audit-log summary (no deployment plan involved).

The summary makes no judgement, so these assert that it *describes* a log
correctly, that its observations fire on the conditions they claim to detect,
and that both upload modes work through the web app.
"""

from __future__ import annotations

import csv
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from auditmaster.audit_parser import parse_audit_log          # noqa: E402
from auditmaster.log_summary import headline, narrative, summarize  # noqa: E402

AUDIT_CSV = ROOT / "tests" / "fixtures" / "audit_sample.csv"
PLAN_XLSX = ROOT / "tests" / "fixtures" / "plan_sample.xlsx"
AUDIT_TEXT = AUDIT_CSV.read_text(encoding="utf-8-sig")


def rows_and_header() -> tuple[list[list[str]], int]:
    rows = list(csv.reader(io.StringIO(AUDIT_TEXT)))
    hdr = next(i for i, r in enumerate(rows) if r and r[0].strip() == "Id")
    return rows, hdr


def to_csv(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode("utf-8")


def summary_of(raw: bytes = None):
    raw = raw if raw is not None else AUDIT_CSV.read_bytes()
    return summarize(parse_audit_log(raw, "audit.csv"))


class TestDescribesTheLog(unittest.TestCase):
    def setUp(self):
        self.s = summary_of()

    def test_headline_figures(self):
        self.assertEqual(self.s.total_rows, 34)
        self.assertEqual(len(self.s.screen_sets), 1)
        self.assertEqual(self.s.screen_sets[0].name, "Fairway - Portfolio A")
        self.assertEqual(self.s.button_toggles, 4)
        self.assertEqual(self.s.distinct_buttons, 4)

    def test_fields_and_changes_are_counted(self):
        self.assertEqual(self.s.properties["Button"], 4)
        self.assertEqual(self.s.properties["Caption"], 14)
        self.assertEqual(self.s.properties["Image Name"], 16)
        self.assertEqual(self.s.changes["disabled"], 4)

    def test_screen_set_block(self):
        block = self.s.screen_sets[0]
        self.assertEqual(sorted(block.buttons, key=int), ["12", "30", "53", "76"])
        self.assertEqual(block.buttons_disabled, 4)
        self.assertIn("French", block.languages)

    def test_one_user_one_transaction(self):
        self.assertEqual(len(self.s.users), 1)
        self.assertEqual(len(self.s.transactions), 1)

    def test_prose_mentions_the_real_numbers(self):
        text = " ".join(narrative(self.s))
        self.assertIn("34", text)
        self.assertIn("Fairway - Portfolio A", text)
        self.assertTrue(headline(self.s).endswith("."))

    def test_a_clean_log_raises_no_warnings(self):
        self.assertEqual(self.s.warnings, [])


class TestObservations(unittest.TestCase):
    """Each observation must fire on the condition it describes."""

    def _mutated(self, mutate) -> object:
        rows, hdr = rows_and_header()
        mutate(rows, hdr)
        return summary_of(to_csv(rows))

    def test_several_users_is_flagged(self):
        def mutate(rows, hdr):
            rows[hdr + 1][6] = "zz999999 - Someone Else"
        s = self._mutated(mutate)
        self.assertTrue(any("more than one person" in o.title.lower() for o in s.warnings))

    def test_a_row_that_changed_nothing_is_flagged(self):
        def mutate(rows, hdr):
            rows[hdr + 1][10] = rows[hdr + 1][9]     # new := old
        s = self._mutated(mutate)
        obs = [o for o in s.warnings if "nothing actually changed" in o.title]
        self.assertTrue(obs)
        self.assertTrue(obs[0].rows)

    def test_duplicate_rows_are_flagged(self):
        def mutate(rows, hdr):
            rows.append(list(rows[hdr + 1]))
        s = self._mutated(mutate)
        self.assertTrue(any("Repeated identical rows" == o.title for o in s.warnings))

    def test_a_row_with_no_field_is_reported_as_skipped(self):
        # The parser drops such a row rather than surfacing it, so the summary
        # must account for it through the skipped-rows count.
        def mutate(rows, hdr):
            rows[hdr + 1][8] = ""
        s = self._mutated(mutate)
        self.assertEqual(s.total_rows, 33)
        self.assertEqual(s.log.skipped_rows, 1)
        self.assertTrue(any("skipped" in o.title.lower() for o in s.warnings))

    def test_a_non_button_field_is_noted_but_not_a_warning(self):
        # "Display Order" names no button. That is normal, so it is reported
        # as information, never as something wrong.
        def mutate(rows, hdr):
            rows[hdr + 1][8] = "Display Order"
        s = self._mutated(mutate)
        noted = [o for o in s.observations if "not tied to a button" in o.title]
        self.assertTrue(noted)
        self.assertEqual(noted[0].level, "info")
        self.assertNotIn(noted[0], s.warnings)

    def test_a_log_with_no_rows_is_refused_at_parse_time(self):
        from auditmaster.audit_parser import AuditParseError
        rows, hdr = rows_and_header()
        with self.assertRaises(AuditParseError):
            summary_of(to_csv(rows[: hdr + 1]))


class TestWebModes(unittest.TestCase):
    """The one upload form serves both modes."""

    def setUp(self):
        import app as web
        web.app.config.update(TESTING=True)
        self.client = web.app.test_client()

    def test_audit_log_alone_gives_a_summary(self):
        data = {"audit": (io.BytesIO(AUDIT_CSV.read_bytes()), "audit.csv")}
        r = self.client.post("/validate", data=data,
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/summary/", r.headers["Location"])
        page = self.client.get(r.headers["Location"])
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn("Audit log summary", body)
        self.assertIn("Fairway - Portfolio A", body)

    def test_summary_export_downloads(self):
        data = {"audit": (io.BytesIO(AUDIT_CSV.read_bytes()), "audit.csv")}
        r = self.client.post("/validate", data=data,
                             content_type="multipart/form-data")
        txt = self.client.get(r.headers["Location"] + "/export.txt")
        self.assertEqual(txt.status_code, 200)
        self.assertIn("Audit log summary", txt.get_data(as_text=True))

    def test_plan_plus_log_still_validates(self):
        data = {
            "audit": (io.BytesIO(AUDIT_CSV.read_bytes()), "audit.csv"),
            "plan": (io.BytesIO(PLAN_XLSX.read_bytes()), "plan.xlsx"),
        }
        r = self.client.post("/validate", data=data,
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/result/", r.headers["Location"])

    def test_missing_audit_log_is_rejected(self):
        r = self.client.post("/validate", data={},
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        self.assertIn("audit log", r.get_data(as_text=True).lower())

    def test_a_summary_token_is_not_a_result_token(self):
        data = {"audit": (io.BytesIO(AUDIT_CSV.read_bytes()), "audit.csv")}
        r = self.client.post("/validate", data=data,
                             content_type="multipart/form-data")
        token = r.headers["Location"].rsplit("/", 1)[-1]
        self.assertEqual(self.client.get(f"/result/{token}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
