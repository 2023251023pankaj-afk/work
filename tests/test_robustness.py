"""
Robustness, safety and performance properties.

These are the failure modes that do not show up on a well-formed sample file:
state leaking between validations, a spreadsheet export that executes on the
reviewer's machine, a malformed upload, and accidental quadratic behaviour.
"""

from __future__ import annotations

import csv
import io
import sys
import time
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from auditmaster import report                                      # noqa: E402
from auditmaster.audit_parser import AuditParseError, parse_audit_log  # noqa: E402
from auditmaster.plan_parser import PlanParseError, parse_plan      # noqa: E402
from auditmaster.readers import ReadError                           # noqa: E402
from auditmaster.validator import Options, validate                 # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
PLAN_XLSX = FIXTURES / "plan_sample.xlsx"
AUDIT_CSV = FIXTURES / "audit_sample.csv"

#: Where the sample audit logs live; None when they have not been shipped.
_LOGS = ROOT / "samples" / "audit-logs"
CORPUS = _LOGS if _LOGS.is_dir() else None


def rows_and_header() -> tuple[list[list[str]], int]:
    text = AUDIT_CSV.read_text(encoding="utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    hdr = next(i for i, r in enumerate(rows) if r and r[0].strip() == "Id")
    return rows, hdr


def to_csv(rows, eol="\n") -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator=eol).writerows(rows)
    return buf.getvalue()


def plan():
    return parse_plan(PLAN_XLSX.read_bytes(), "plan.xlsx")


def audit(raw: bytes = None):
    return parse_audit_log(raw if raw is not None else AUDIT_CSV.read_bytes(), "a.csv")


class TestResultsDoNotLeakIntoEachOther(unittest.TestCase):
    """A plan is reused across validations; a result must not be rewritten.

    The app keeps recent results in memory. Filling the outcome in on the
    plan's own claim objects let a later validation silently rewrite the
    evidence of an earlier result that was still reachable by URL.
    """

    def _second_log(self) -> bytes:
        rows, hdr = rows_and_header()
        other = [list(r) for r in rows]
        for r in other[hdr + 1:]:
            if len(r) > 10:
                r[9], r[10] = "Something Else", "Totally Different"
        return to_csv(other).encode()

    def test_an_earlier_result_is_not_rewritten(self):
        p = plan()
        first = validate(p, audit(), Options())
        before = (len(first.evidence.matched), sorted(first.evidence.explained_rows))
        validate(p, audit(self._second_log()), Options())
        after = (len(first.evidence.matched), sorted(first.evidence.explained_rows))
        self.assertEqual(before, after)

    def test_the_plan_itself_is_left_clean(self):
        p = plan()
        validate(p, audit(), Options())
        self.assertTrue(
            all(c.outcome == "absent" and not c.entries for c in p.claims),
            "validation wrote its findings back onto the plan",
        )

    def test_order_does_not_change_the_answer(self):
        p, other = plan(), self._second_log()
        a1 = validate(p, audit(), Options()).attribution_counts()
        b1 = validate(p, audit(other), Options()).attribution_counts()
        p2 = plan()
        b2 = validate(p2, audit(other), Options()).attribution_counts()
        a2 = validate(p2, audit(), Options()).attribution_counts()
        self.assertEqual((a1, b1), (a2, b2))


class TestSpreadsheetExportIsSafe(unittest.TestCase):
    """A value from the audited system must not execute in Excel.

    The CSV export exists to be opened in a spreadsheet, so a cell beginning
    `=`, `+`, `-`, `@` or a control character is a code-execution vector.
    """

    DANGEROUS = ("=", "+", "-", "@", "\t", "\r")
    PAYLOADS = ['=cmd|"/c calc"!A1', "+1+1", "-2+3", "@SUM(1:9)", "\t=1+1"]

    def _export_with(self, payload: str) -> str:
        rows, hdr = rows_and_header()
        rows[hdr + 1][9] = payload
        res = validate(plan(), audit(to_csv(rows).encode()), Options())
        return report.to_csv(res)

    def test_no_exported_cell_can_execute(self):
        for payload in self.PAYLOADS:
            with self.subTest(payload=payload):
                out = self._export_with(payload)
                for row in csv.reader(io.StringIO(out)):
                    for cell in row:
                        self.assertFalse(
                            cell[:1] in self.DANGEROUS,
                            f"{cell[:24]!r} would execute in a spreadsheet",
                        )

    def test_the_value_is_still_present(self):
        out = self._export_with("=cmd|\"/c calc\"!A1")
        self.assertIn("cmd", out)

    def test_ordinary_values_are_untouched(self):
        out = self._export_with("Enable")
        self.assertIn("Enable", out)
        self.assertNotIn("'Enable", out)


class TestMalformedUploads(unittest.TestCase):
    """Every bad upload gets a clear refusal, never a traceback."""

    def _zip(self, parts: dict[str, str]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, body in parts.items():
                z.writestr(name, body)
        return buf.getvalue()

    def test_bad_plans_are_refused_cleanly(self):
        good = PLAN_XLSX.read_bytes()
        cases = {
            "truncated": good[: len(good) // 2],
            "empty": b"",
            "plain text": b"this is not a spreadsheet at all",
            "zip without workbook": self._zip({"hello.txt": "hi"}),
        }
        for name, data in cases.items():
            with self.subTest(case=name):
                with self.assertRaises((PlanParseError, ReadError)):
                    parse_plan(data, f"{name}.xlsx")

    def test_an_external_entity_is_not_resolved(self):
        """An XXE payload must not read a local file."""
        book = (
            '<?xml version="1.0"?><!DOCTYPE t [<!ENTITY xxe SYSTEM '
            '"file:///etc/passwd">]><workbook xmlns="http://schemas.openxmlformats.org'
            '/spreadsheetml/2006/main"><sheets><sheet name="&xxe;" sheetId="1"/>'
            "</sheets></workbook>"
        )
        data = self._zip({
            "[Content_Types].xml":
                '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org'
                '/package/2006/content-types"><Default Extension="xml" '
                'ContentType="application/xml"/><Override PartName="/xl/workbook.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.sheet.main+xml"/></Types>',
            "_rels/.rels":
                '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
                'openxmlformats.org/package/2006/relationships"><Relationship Id="r1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
            "xl/workbook.xml": book,
        })
        try:
            result = parse_plan(data, "xxe.xlsx")
        except (PlanParseError, ReadError):
            return                                  # refused outright: fine
        self.assertNotIn("root:", str(result.sheet_names))

    def test_a_log_with_no_rows_is_refused(self):
        rows, hdr = rows_and_header()
        with self.assertRaises(AuditParseError):
            parse_audit_log(to_csv(rows[: hdr + 1]).encode(), "empty.csv")


class TestCorruptedAuditLogs(unittest.TestCase):
    """Mangled logs still parse, and every row is still accounted for."""

    def _variants(self) -> dict[str, bytes]:
        rows, hdr = rows_and_header()
        body = rows[hdr + 1:]
        wide = [r + ["extra"] for r in rows]
        order = list(range(len(rows[hdr])))[::-1]
        flipped = [[r[i] if i < len(r) else "" for i in order] for r in rows]
        long_value = [list(r) for r in rows]
        long_value[hdr + 1][10] = "X" * 60000
        nulls = [list(r) for r in rows]
        nulls[hdr + 1][8] = "Button(12)\x00(Button(12))"
        return {
            "utf-8": to_csv(rows).encode("utf-8"),
            "utf-8 with BOM": b"\xef\xbb\xbf" + to_csv(rows).encode("utf-8"),
            "utf-16": to_csv(rows).encode("utf-16"),
            "crlf line endings": to_csv(rows, "\r\n").encode("utf-8"),
            "rows out of order": to_csv(rows[: hdr + 1] + body[::-1]).encode(),
            "every row duplicated": to_csv(rows + body).encode(),
            "truncated midway": to_csv(rows[: hdr + 1 + 17]).encode(),
            "single row": to_csv(rows[: hdr + 2]).encode(),
            "extra column": to_csv(wide).encode(),
            "columns reversed": to_csv(flipped).encode(),
            "60k-character value": to_csv(long_value).encode(),
            "embedded null byte": to_csv(nulls).encode(),
        }

    def test_each_variant_parses_and_is_fully_accounted_for(self):
        p = plan()
        for name, data in self._variants().items():
            with self.subTest(variant=name):
                log = parse_audit_log(data, "v.csv")
                res = validate(p, log, Options())
                self.assertEqual(
                    sum(res.attribution_counts().values()), len(log.entries),
                    "some rows fell out of the accounting",
                )
                self.assertTrue(report.headline(res))
                report.to_csv(res)
                report.to_text(res)

    def test_reordering_rows_does_not_change_the_verdict(self):
        rows, hdr = rows_and_header()
        body = rows[hdr + 1:]
        straight = validate(plan(), audit(to_csv(rows).encode()), Options())
        reversed_ = validate(
            plan(), audit(to_csv(rows[: hdr + 1] + body[::-1]).encode()), Options()
        )
        self.assertEqual(straight.verdict, reversed_.verdict)
        self.assertEqual(straight.total_confirmed, reversed_.total_confirmed)


@unittest.skipIf(CORPUS is None, "no corpus folder present")
class TestEveryRealLogHoldsTheInvariants(unittest.TestCase):
    def test_accounting_and_determinism(self):
        p = plan()
        for path in sorted(CORPUS.glob("*.csv")):
            with self.subTest(log=path.name):
                raw = path.read_bytes()
                log = parse_audit_log(raw, path.name)
                first = validate(p, log, Options())
                second = validate(p, parse_audit_log(raw, path.name), Options())
                self.assertEqual(
                    sum(first.attribution_counts().values()), len(log.entries)
                )
                self.assertEqual(first.verdict, second.verdict)
                self.assertEqual(
                    first.attribution_counts(), second.attribution_counts()
                )


class TestPerformance(unittest.TestCase):
    """Guards against a reintroduced quadratic, not a benchmark.

    The limits are deliberately loose — roughly ten times the observed cost —
    so they fail on an algorithmic regression, not on a slow machine.
    """

    def _sheet_of(self, n_rows: int) -> bytes:
        body = "".join(
            f'<row r="{i}">'
            f'<c r="A{i}" t="inlineStr"><is><t>01 - WWOA</t></is></c>'
            f'<c r="B{i}" t="inlineStr"><is><t>{i % 90 + 1}</t></is></c>'
            f"</row>"
            for i in range(1, n_rows + 1)
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml",
                '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org'
                '/package/2006/content-types"><Default Extension="xml" '
                'ContentType="application/xml"/><Override PartName="/xl/workbook.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.sheet.main+xml"/><Override '
                'PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.'
                'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
            z.writestr("_rels/.rels",
                '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
                'openxmlformats.org/package/2006/relationships"><Relationship Id="r1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
            z.writestr("xl/workbook.xml",
                '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.'
                'org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.'
                'org/officeDocument/2006/relationships"><sheets><sheet name="Screenset" '
                'sheetId="1" r:id="r1"/></sheets></workbook>')
            z.writestr("xl/_rels/workbook.xml.rels",
                '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
                'openxmlformats.org/package/2006/relationships"><Relationship Id="r1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                'relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
            z.writestr("xl/worksheets/sheet1.xml",
                '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.'
                f'org/spreadsheetml/2006/main"><sheetData>{body}</sheetData></worksheet>')
        return buf.getvalue()

    def test_a_large_sheet_does_not_blow_up(self):
        data = self._sheet_of(8000)
        start = time.time()
        try:
            parse_plan(data, "big.xlsx")
        except PlanParseError:
            pass
        elapsed = time.time() - start
        self.assertLess(elapsed, 25, f"parsing 8000 rows took {elapsed:.1f}s")

    def test_cost_grows_roughly_linearly(self):
        def cost(n: int) -> float:
            data = self._sheet_of(n)
            start = time.time()
            try:
                parse_plan(data, "big.xlsx")
            except PlanParseError:
                pass
            return time.time() - start

        small, large = cost(1000), cost(4000)
        # Four times the rows must not cost anything like sixteen times as much.
        self.assertLess(large, max(small * 10, 8), f"{small:.2f}s -> {large:.2f}s")


if __name__ == "__main__":
    unittest.main()


class TestHostileValuesInThePage(unittest.TestCase):
    """A value from the audited system must not break or inject the page."""

    def _log_with(self, col: int, value: str) -> bytes:
        rows, hdr = rows_and_header()
        rows[hdr + 1][col] = value
        return to_csv(rows).encode()

    def _render(self, raw: bytes) -> str:
        import app as web
        web.app.config.update(TESTING=True)
        client = web.app.test_client()
        data = {
            "audit": (io.BytesIO(raw), "a.csv"),
            "plan": (io.BytesIO(PLAN_XLSX.read_bytes()), "plan.xlsx"),
        }
        posted = client.post("/validate", data=data,
                             content_type="multipart/form-data")
        return client.get(posted.headers["Location"]).get_data(as_text=True)

    def test_markup_in_a_value_is_escaped(self):
        page = self._render(self._log_with(9, "<script>alert(1)</script>"))
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_an_enormous_value_does_not_bloat_the_page(self):
        page = self._render(self._log_with(10, "X" * 60000))
        longest = max(len(line) for line in page.splitlines())
        self.assertLess(longest, 5000, f"a {longest}-character line reached the page")
        self.assertLess(len(page), 2_000_000)

    def test_the_change_summary_is_clipped_but_the_value_is_not(self):
        log = parse_audit_log(self._log_with(10, "X" * 60000), "a.csv")
        entry = next(e for e in log.entries if len(e.new) > 1000)
        self.assertLess(len(entry.change_text), 400)
        self.assertEqual(len(entry.new), 60000, "the underlying value was truncated")

    def test_the_csv_export_keeps_the_whole_value(self):
        raw = self._log_with(10, "X" * 60000)
        res = validate(plan(), audit(raw), Options())
        self.assertIn("X" * 60000, report.to_csv(res))
