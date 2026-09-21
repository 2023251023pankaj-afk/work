"""
Test suite for Audit Master.

Two things are proved here:

1. The engine confirms correct work (the supplied plan + audit log pair).
2. The engine *catches* incorrect work. A validator that only ever says PASS is
   worthless, so most of these tests corrupt the known-good audit log in one
   specific way and assert the corresponding check fires.

Plus format/encoding robustness: the same audit log is fed in as UTF-16 CSV,
TSV, mojibake'd cp1252 and .xlsx, and must produce an identical verdict.

Run:  .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import csv
import io
import json
import unittest
import zipfile
from pathlib import Path

from auditmaster.audit_parser import (
    AuditParseError,
    classify,
    parse_audit_log,
    parse_description,
    parse_field,
)
from auditmaster.plan_parser import PlanParseError, parse_plan
from auditmaster.readers import decode_bytes, read_any, repair_mojibake
from auditmaster.report import headline, narrative, to_csv, to_json, to_text
from auditmaster.validator import (
    ATTR_SUPPORTING,
    ATTR_UNATTRIBUTED,
    CONFIRMED,
    FAIL,
    MISSING,
    PASS,
    PASS_WARN,
    UNAUTHORIZED,
    WRONG_DIRECTION,
    Options,
    validate,
)

ROOT = Path(__file__).resolve().parent.parent
# Frozen copies, so that editing the working files in the project root — to
# try out a discrepancy, say — never breaks the suite.
FIXTURES = ROOT / "tests" / "fixtures"
#: Real-world plans shipped with the project, used by the end-to-end tests.
PLANS_DIR = ROOT / "samples" / "deployment-plans"
PLAN_XLSX = FIXTURES / "plan_sample.xlsx"
AUDIT_CSV = FIXTURES / "audit_sample.csv"

#: Column positions in the sample audit CSV, looked up from its header.
COL_USER, COL_FIELD, COL_OLD, COL_NEW = 6, 8, 9, 10

PLAN_BYTES = PLAN_XLSX.read_bytes()
AUDIT_TEXT = AUDIT_CSV.read_text(encoding="utf-8-sig")


# ---------------------------------------------------------------------------
# Helpers for building modified audit logs
# ---------------------------------------------------------------------------


def audit_rows() -> tuple[list[list[str]], int]:
    """The audit CSV as rows, plus the index of the header row."""
    rows = list(csv.reader(io.StringIO(AUDIT_TEXT)))
    hdr = next(i for i, r in enumerate(rows) if r and r[0].strip() == "Id")
    return rows, hdr


def rows_to_csv(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue().encode("utf-8")


def rows_to_xlsx(rows: list[list[str]]) -> bytes:
    """Minimal single-sheet .xlsx writer, so the xlsx reader can be tested."""
    def esc(v: str) -> str:
        return (v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    body = []
    for ri, row in enumerate(rows, start=1):
        cells = "".join(
            f'<c r="{chr(65 + ci) if ci < 26 else "A" + chr(65 + ci - 26)}{ri}" '
            f't="inlineStr"><is><t xml:space="preserve">{esc(v)}</t></is></c>'
            for ci, v in enumerate(row) if v != ""
        )
        body.append(f'<row r="{ri}">{cells}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(body)}</sheetData></worksheet>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>")
        z.writestr("_rels/.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>")
        z.writestr("xl/workbook.xml",
            '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Audit" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>")
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def rows_to_xlsx_multi(sheets: list[tuple[str, list[list[str]]]]) -> bytes:
    """Multi-sheet .xlsx writer, for testing one-tab-per-screen-set plans."""
    def esc(v: str) -> str:
        return v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        types, rels, decls = [], [], []
        for i, (name, rows) in enumerate(sheets, start=1):
            types.append(
                f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType='
                '"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
            rels.append(
                f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/'
                f'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>')
            decls.append(f'<sheet name="{esc(name)}" sheetId="{i}" r:id="rId{i}"/>')
            body = "".join(
                f'<row r="{ri}">' + "".join(
                    f'<c r="{chr(65 + ci)}{ri}" t="inlineStr"><is><t>{esc(v)}</t></is></c>'
                    for ci, v in enumerate(row) if v
                ) + "</row>"
                for ri, row in enumerate(rows, start=1)
            )
            z.writestr(
                f"xl/worksheets/sheet{i}.xml",
                '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/'
                f'spreadsheetml/2006/main"><sheetData>{body}</sheetData></worksheet>')
        z.writestr("[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + "".join(types) + "</Types>")
        z.writestr("_rels/.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>")
        z.writestr("xl/workbook.xml",
            '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
            + "".join(decls) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(rels) + "</Relationships>")
    return buf.getvalue()


COL = {}  # canonical name -> column index, filled on first use


def col(name: str) -> int:
    if not COL:
        rows, hdr = audit_rows()
        for i, h in enumerate(rows[hdr]):
            COL[h.strip()] = i
    return COL[name]


def load_plan():
    return parse_plan(PLAN_BYTES, PLAN_XLSX.name)


def run(audit_bytes: bytes, name: str = "audit.csv", plan=None, **opts):
    plan = plan or load_plan()
    log = parse_audit_log(audit_bytes, name)
    return validate(plan, log, Options(**opts))


# ---------------------------------------------------------------------------


class TestPlanParsing(unittest.TestCase):
    def setUp(self):
        self.plan = load_plan()

    def test_profile_and_shape(self):
        self.assertEqual(self.plan.profile, "screenset_button_matrix")
        self.assertEqual(self.plan.groups, ["Breakfast", "Dinner", "Lunch", "Latenight"])
        self.assertEqual(len(self.plan.entities), 54)
        self.assertEqual(len(self.plan.targets), 216)

    def test_sheet_roles(self):
        self.assertEqual(
            self.plan.sheet_roles,
            {"Instructions": "instructions", "Validation": "validation",
             "Screenset": "matrix", "Assignment": "assignment"},
        )

    def test_fairway_buttons_match_the_spreadsheet(self):
        got = {t.group: t.button for t in self.plan.targets_for("Fairway - Portfolio A")}
        self.assertEqual(
            got, {"Breakfast": "12", "Dinner": "30", "Lunch": "53", "Latenight": "76"}
        )

    def test_entity_containing_an_alias_word_is_not_dropped(self):
        # "Fairway - Portfolio A" contains the entity alias "portfolio".
        self.assertIsNotNone(self.plan.find_entity("Fairway - Portfolio A"))
        self.assertIsNotNone(self.plan.find_entity("Fairway - Portfolio B"))

    def test_meta_harvested_from_prose(self):
        self.assertEqual(self.plan.meta.screen_number, "61000")
        self.assertEqual(self.plan.meta.screen_name, "Left hand navigation")
        self.assertEqual(self.plan.meta.tile_label, "NEW McCafé Specialty Drinks")
        self.assertEqual(self.plan.meta.action, "disable")

    def test_plan_defects_are_reported(self):
        joined = " ".join(self.plan.warnings)
        self.assertIn("Dinner", joined)     # listed twice with two workflows
        self.assertIn("Latenight", joined)  # missing from the workflow table

    def test_nbsp_entity_names_survive(self):
        self.assertIn("MHQ - Current Rotation 38", self.plan.entities)

    def test_empty_and_garbage_files_are_rejected(self):
        for raw in (b"", b"\x00\x01\x02not a spreadsheet"):
            with self.assertRaises(PlanParseError):
                parse_plan(raw, "x.csv")


class TestNormalizedPlanRoundTrip(unittest.TestCase):
    def test_ndp_json_reupload_is_equivalent(self):
        plan = load_plan()
        blob = json.dumps(plan.to_dict()).encode("utf-8")
        back = parse_plan(blob, "plan-template.json")
        self.assertEqual(len(back.targets), len(plan.targets))
        self.assertEqual(back.meta.tile_label, plan.meta.tile_label)
        # And it validates identically.
        a = run(AUDIT_CSV.read_bytes(), plan=plan)
        b = run(AUDIT_CSV.read_bytes(), plan=back)
        self.assertEqual(a.verdict, b.verdict)
        self.assertEqual(a.total_confirmed, b.total_confirmed)


class TestAuditParsing(unittest.TestCase):
    def setUp(self):
        self.log = parse_audit_log(AUDIT_CSV.read_bytes(), AUDIT_CSV.name)

    def test_row_count_and_metadata(self):
        self.assertEqual(len(self.log.entries), 34)
        self.assertEqual(self.log.report_effective_date, "06/17/2026")
        self.assertEqual(self.log.generated_by, "Yugdeep Parihar")
        self.assertEqual(self.log.audit_ids, ["432157744"])
        self.assertEqual(self.log.screen_sets, ["Fairway - Portfolio A"])

    def test_four_button_toggles_all_disable(self):
        toggles = {e.button: e.change for e in self.log.entries if e.is_button_toggle}
        self.assertEqual(toggles, {"12": "disabled", "30": "disabled",
                                   "53": "disabled", "76": "disabled"})

    def test_field_decomposition(self):
        cases = {
            "Button(53)(Button(53))": ("Button", "53", ""),
            "Image Name(76-French)": ("Image Name", "76", "French"),
            "Caption(30-SpanishUS)": ("Caption", "30", "SpanishUS"),
            "Caption(12-Mandarin)": ("Caption", "12", "Mandarin"),
        }
        for text, want in cases.items():
            self.assertEqual(parse_field(text), want, text)

    def test_field_decomposition_tolerates_unknown_shapes(self):
        self.assertEqual(parse_field("Screen Name"), ("Screen Name", "", ""))
        self.assertEqual(parse_field(""), ("", "", ""))
        self.assertEqual(parse_field("Caption(French)"), ("Caption", "", "French"))

    def test_description_decomposition(self):
        s, screen = parse_description(
            "Current Settings: Screen Kiosk 6 Left Hand Navigation of screen set "
            "Fairway - Portfolio A has been updated."
        )
        self.assertEqual(s, "Fairway - Portfolio A")
        self.assertEqual(screen, "Kiosk 6 Left Hand Navigation")

    def test_change_classification(self):
        self.assertEqual(classify("Enable", "Disable"), "disabled")
        self.assertEqual(classify("Disable", "Enable"), "enabled")
        self.assertEqual(classify("img.png", ""), "cleared")
        self.assertEqual(classify("", "img.png"), "set")
        self.assertEqual(classify("a", "b"), "changed")
        self.assertEqual(classify("a", "a"), "unchanged")

    def test_a_plan_file_is_not_an_audit_log(self):
        with self.assertRaises(AuditParseError):
            parse_audit_log(PLAN_BYTES, PLAN_XLSX.name)


class TestHappyPath(unittest.TestCase):
    def setUp(self):
        self.res = run(AUDIT_CSV.read_bytes(), AUDIT_CSV.name)

    def test_verdict_is_pass(self):
        self.assertEqual(self.res.verdict, PASS, [f.message for f in self.res.failures])

    def test_all_four_buttons_confirmed(self):
        self.assertEqual(self.res.total_expected, 4)
        self.assertEqual(self.res.total_confirmed, 4)
        self.assertEqual(self.res.total_missing, 0)
        self.assertEqual(self.res.total_unauthorized, 0)
        self.assertEqual(self.res.match_rate, 100)

    def test_every_audit_row_is_attributed(self):
        left = [
            e for e in self.res.log.entries
            if self.res.entry_attribution(e) == ATTR_UNATTRIBUTED
        ]
        self.assertEqual(left, [])
        self.assertEqual(sum(self.res.attribution_counts().values()), 34)

    def test_dayparts_map_to_the_right_buttons(self):
        er = self.res.entities[0]
        got = {b.group: (b.button, b.status) for b in er.buttons}
        self.assertEqual(got, {
            "Breakfast": ("12", CONFIRMED), "Dinner": ("30", CONFIRMED),
            "Lunch": ("53", CONFIRMED), "Latenight": ("76", CONFIRMED),
        })

    def test_plan_defects_do_not_change_the_verdict(self):
        self.assertTrue(self.res.document_findings)   # the plan has two
        self.assertEqual(self.res.verdict, PASS)      # yet the work passes

    def test_language_coverage_is_an_observation_not_a_failure(self):
        c6 = [f for f in self.res.all_findings if f.check_id == "C6"]
        self.assertTrue(c6)
        self.assertNotEqual(c6[0].status, "fail")

    def test_exports_render(self):
        self.assertIn("PASS", to_text(self.res))
        doc = json.loads(to_json(self.res))
        self.assertEqual(doc["summary"]["confirmed"], 4)
        self.assertEqual(len(doc["audit_rows"]), 34)
        self.assertEqual(len(to_csv(self.res).strip().splitlines()), 35)  # + header


class TestCatchesBrokenWork(unittest.TestCase):
    """Each test corrupts the good audit log one way and asserts a check fires."""

    def test_missing_button_is_caught(self):
        rows, hdr = audit_rows()
        kept = [
            r for i, r in enumerate(rows)
            if not (i > hdr and r and r[col("Field")].startswith("Button(30)"))
        ]
        res = run(rows_to_csv(kept))
        self.assertEqual(res.verdict, FAIL)
        self.assertEqual(res.total_missing, 1)
        self.assertEqual(res.total_confirmed, 3)
        bad = res.entities[0].missing[0]
        self.assertEqual((bad.button, bad.group, bad.status), ("30", "Dinner", MISSING))
        self.assertTrue(any(f.check_id == "C1" and f.is_failure for f in res.failures))

    def test_unauthorized_button_is_caught(self):
        rows, hdr = audit_rows()
        extra = list(rows[hdr + 1])
        extra[col("Field")] = "Button(99)(Button(99))"
        extra[col("Old Setting")], extra[col("New Setting")] = "Enable", "Disable"
        res = run(rows_to_csv(rows + [extra]))
        self.assertEqual(res.verdict, FAIL)
        self.assertEqual(res.total_confirmed, 4)       # planned work still fine
        self.assertEqual(res.total_unauthorized, 1)    # but something extra happened
        self.assertEqual(res.entities[0].unauthorized[0].button, "99")
        self.assertTrue(any(f.check_id == "C2" and f.is_failure for f in res.failures))

    def test_button_moved_the_wrong_way_is_caught(self):
        rows, hdr = audit_rows()
        out = []
        for i, r in enumerate(rows):
            r = list(r)
            if i > hdr and r and r[col("Field")].startswith("Button(30)"):
                r[col("Old Setting")], r[col("New Setting")] = "Disable", "Enable"
            out.append(r)
        res = run(rows_to_csv(out))
        self.assertEqual(res.verdict, FAIL)
        wd = res.entities[0].wrong_direction
        self.assertEqual([b.button for b in wd], ["30"])
        self.assertEqual(wd[0].status, WRONG_DIRECTION)
        self.assertTrue(any(f.check_id == "C3" for f in res.failures))

    def test_work_on_a_screen_set_outside_the_plan_is_caught(self):
        rows, hdr = audit_rows()
        out = []
        for i, r in enumerate(rows):
            r = list(r)
            if i > hdr and r:
                r[col("Description")] = (
                    "Current Settings: Screen Kiosk 6 Left Hand Navigation of "
                    "screen set 99 - NOWHERE has been updated."
                )
            out.append(r)
        res = run(rows_to_csv(out))
        self.assertEqual(res.verdict, FAIL)
        self.assertFalse(res.entities[0].in_plan)
        self.assertTrue(any(f.check_id == "C8" for f in res.failures))

    def test_wrong_tile_caption_is_flagged(self):
        rows, hdr = audit_rows()
        out = []
        for i, r in enumerate(rows):
            r = list(r)
            if i > hdr and r and r[col("Field")].startswith("Caption("):
                r[col("Old Setting")] = "Happy Meal Bundle"
            out.append(r)
        res = run(rows_to_csv(out))
        c4 = [f for f in res.all_findings if f.check_id == "C4"]
        self.assertTrue(c4)
        self.assertEqual(c4[0].status, "warn")
        self.assertIn("Happy Meal Bundle", c4[0].message)

    def test_wrong_screen_is_flagged_and_can_be_made_fatal(self):
        rows, hdr = audit_rows()
        out = []
        for i, r in enumerate(rows):
            r = list(r)
            if i > hdr and r:
                r[col("Description")] = (
                    "Current Settings: Screen Kiosk 3 Order Confirmation of "
                    "screen set Fairway - Portfolio A has been updated."
                )
            out.append(r)
        body = rows_to_csv(out)
        self.assertEqual(run(body).verdict, PASS_WARN)
        self.assertEqual(run(body, strict_screen_match=True).verdict, FAIL)

    def test_content_edited_on_an_unplanned_button_fails(self):
        # Editing a tile nobody asked you to touch, in the same save as the
        # planned work, is a deviation — not a note. Edits made in unrelated
        # saves are separated out by C17 before this check runs, so being strict
        # here no longer floods the report.
        rows, hdr = audit_rows()
        extra = list(rows[hdr + 1])
        extra[col("Field")] = "Caption(41-English)"
        extra[col("Old Setting")], extra[col("New Setting")] = "Some Tile", "blank"
        res = run(rows_to_csv(rows + [extra]))
        c2 = [f for f in res.all_findings if f.check_id == "C2"]
        self.assertTrue(c2)
        self.assertEqual(c2[0].status, "fail")
        self.assertEqual(res.verdict, FAIL)

    def test_unknown_property_warns(self):
        rows, hdr = audit_rows()
        extra = list(rows[hdr + 1])
        extra[col("Field")] = "Routing Target(12-English)"
        res = run(rows_to_csv(rows + [extra]))
        self.assertTrue(any(f.check_id == "C9" for f in res.all_findings))

    def test_full_coverage_option_fails_a_single_set_export(self):
        good = AUDIT_CSV.read_bytes()
        self.assertEqual(run(good).verdict, PASS)
        strict = run(good, require_full_plan_coverage=True)
        self.assertEqual(strict.verdict, FAIL)
        self.assertTrue(any(f.check_id == "C10" for f in strict.failures))
        self.assertEqual(len(strict.not_attempted), 53)

    def test_language_parity_option_fails_uneven_coverage(self):
        res = run(AUDIT_CSV.read_bytes(), require_language_parity=True)
        self.assertEqual(res.verdict, FAIL)
        self.assertTrue(any(f.check_id == "C6" for f in res.failures))

    def test_mixed_effective_dates_warn(self):
        rows, hdr = audit_rows()
        out = [list(r) for r in rows]
        out[hdr + 1][col("Effective From")] = "Jun 18, 2026"
        res = run(rows_to_csv(out))
        self.assertTrue(any(f.check_id == "C12" for f in res.all_findings))


class TestFormatAndEncodingRobustness(unittest.TestCase):
    """The same audit log in different containers must give the same verdict."""

    def _assert_good(self, raw: bytes, name: str):
        res = run(raw, name)
        self.assertEqual(res.verdict, PASS, f"{name}: {[f.message for f in res.failures]}")
        self.assertEqual(res.total_confirmed, 4, name)
        self.assertEqual(len(res.log.entries), 34, name)

    def test_utf8_csv(self):
        self._assert_good(AUDIT_CSV.read_bytes(), "audit.csv")

    def test_utf8_without_bom(self):
        self._assert_good(AUDIT_TEXT.encode("utf-8"), "audit.csv")

    def test_utf16_csv(self):
        self._assert_good(AUDIT_TEXT.encode("utf-16"), "audit.csv")

    def test_utf16_le_without_bom(self):
        self._assert_good(AUDIT_TEXT.encode("utf-16-le"), "audit.csv")

    def test_tab_separated(self):
        rows, _ = audit_rows()
        buf = io.StringIO()
        csv.writer(buf, delimiter="\t", lineterminator="\n").writerows(rows)
        self._assert_good(buf.getvalue().encode("utf-8"), "audit.tsv")

    def test_semicolon_separated(self):
        rows, _ = audit_rows()
        buf = io.StringIO()
        csv.writer(buf, delimiter=";", lineterminator="\n").writerows(rows)
        self._assert_good(buf.getvalue().encode("utf-8"), "audit.csv")

    def test_cp1252_mojibake_is_repaired(self):
        # UTF-8 bytes mislabelled as cp1252 turn "McCafé" into "McCafÃ©".
        broken = AUDIT_TEXT.encode("utf-8").decode("cp1252").encode("cp1252")
        res = run(broken, "audit.csv")
        self.assertEqual(res.verdict, PASS)
        # The tile-identity check still matched, which means the accent survived.
        c4 = [f for f in res.all_findings if f.check_id == "C4"]
        self.assertEqual(c4[0].status, "pass")

    def test_accent_stripped_caption_still_matches_the_plan(self):
        rows, hdr = audit_rows()
        out = []
        for i, r in enumerate(rows):
            r = list(r)
            if i > hdr and r and r[col("Field")].startswith("Caption("):
                r[col("Old Setting")] = r[col("Old Setting")].replace("é", "e")
            out.append(r)
        res = run(rows_to_csv(out))
        c4 = [f for f in res.all_findings if f.check_id == "C4"]
        self.assertEqual(c4[0].status, "pass", c4[0].message)

    def test_xlsx_audit_log(self):
        rows, _ = audit_rows()
        self._assert_good(rows_to_xlsx(rows), "audit.xlsx")

    def test_html_table_audit_log(self):
        rows, _ = audit_rows()
        def esc(v):
            return v.replace("&", "&amp;").replace("<", "&lt;")
        html = "<html><body><table>" + "".join(
            "<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>" for r in rows
        ) + "</table></body></html>"
        self._assert_good(html.encode("utf-8"), "audit.html")

    def test_reordered_columns_still_parse(self):
        rows, hdr = audit_rows()
        order = [col("Field"), col("Old Setting"), col("New Setting"),
                 col("Description"), col("Date"), col("User"), col("Id"),
                 col("Operation"), col("Level"), col("Level Details"),
                 col("Activity"), col("Effective From")]
        out = [[r[i] if i < len(r) else "" for i in order] for r in rows[hdr:]]
        self._assert_good(rows_to_csv(out), "audit.csv")

    def test_extra_unknown_column_is_ignored(self):
        rows, hdr = audit_rows()
        out = []
        for i, r in enumerate(rows[hdr:]):
            out.append(list(r) + ["Ticket-123" if i else "Ticket Ref"])
        self._assert_good(rows_to_csv(out), "audit.csv")


class TestFlatPlanProfile(unittest.TestCase):
    """A plan that is already a flat list, in a totally different layout."""

    FLAT = (
        "Coop,Daypart,Button Number,Action\n"
        "Fairway - Portfolio A,Breakfast,12,disable\n"
        "Fairway - Portfolio A,Dinner,30,disable\n"
        "Fairway - Portfolio A,Lunch,53,disable\n"
        "Fairway - Portfolio A,Latenight,76,disable\n"
    )

    def test_flat_csv_plan_validates(self):
        plan = parse_plan(self.FLAT.encode("utf-8"), "plan.csv")
        self.assertEqual(plan.profile, "explicit_target_list")
        self.assertEqual(len(plan.targets), 4)
        res = run(AUDIT_CSV.read_bytes(), plan=plan)
        self.assertEqual(res.verdict, PASS, [f.message for f in res.failures])
        self.assertEqual(res.total_confirmed, 4)

    def test_markdown_plan(self):
        md = (
            "# Cleanup\n\n"
            "| Coop | Daypart | Button Number |\n"
            "|---|---|---|\n"
            "| Fairway - Portfolio A | Breakfast | 12 |\n"
            "| Fairway - Portfolio A | Dinner | 30 |\n"
            "| Fairway - Portfolio A | Lunch | 53 |\n"
            "| Fairway - Portfolio A | Latenight | 76 |\n"
        )
        plan = parse_plan(md.encode("utf-8"), "plan.md")
        self.assertEqual(len(plan.targets), 4)
        self.assertEqual(run(AUDIT_CSV.read_bytes(), plan=plan).total_confirmed, 4)

    def test_json_plan(self):
        doc = [
            {"Screenset": "Fairway - Portfolio A", "Daypart": d, "Button": b}
            for d, b in [("Breakfast", 12), ("Dinner", 30), ("Lunch", 53), ("Latenight", 76)]
        ]
        plan = parse_plan(json.dumps(doc).encode("utf-8"), "plan.json")
        self.assertEqual(len(plan.targets), 4)
        self.assertEqual(run(AUDIT_CSV.read_bytes(), plan=plan).total_confirmed, 4)

    def test_matrix_with_unrecognised_group_names_falls_back_to_shape(self):
        # No daypart words at all — detection must use column shape instead.
        csv_text = (
            "Region,Slot Alpha,Slot Beta,Slot Gamma,Slot Delta\n"
            "Fairway - Portfolio A,12,30,53,76\n"
            "Other Place,1,2,3,4\n"
        )
        plan = parse_plan(csv_text.encode("utf-8"), "plan.csv")
        self.assertEqual(len(plan.targets_for("Fairway - Portfolio A")), 4)
        res = run(AUDIT_CSV.read_bytes(), plan=plan)
        self.assertEqual(res.total_confirmed, 4, [f.message for f in res.failures])


class TestDifferentPlanShapes(unittest.TestCase):
    """Structurally different plans that must all yield the same four targets.

    Each case expresses "Fairway - Portfolio A needs buttons 12/30/53/76
    disabled" in a different layout, and is validated against the real audit
    log. These are the shapes a plan author actually produces.
    """

    E = "Fairway - Portfolio A"

    def _assert_four(self, raw: bytes, name: str, msg: str = ""):
        plan = parse_plan(raw, name)
        self.assertEqual(len(plan.targets_for(self.E)), 4, f"{msg or name}: targets")
        res = run(AUDIT_CSV.read_bytes(), plan=plan)
        self.assertEqual(res.total_confirmed, 4,
                         f"{msg or name}: {[f.message for f in res.failures]}")
        self.assertEqual(res.total_unauthorized, 0, msg or name)
        return plan

    def _csv(self, rows) -> bytes:
        return rows_to_csv(rows)

    def test_transposed_screensets_across_the_top(self):
        plan = self._assert_four(self._csv([
            ["Daypart", "01 - WWOA", self.E, "Fairway - Portfolio B"],
            ["Breakfast", "13", "12", "12"],
            ["Dinner", "31", "30", "30"],
            ["Lunch", "54", "53", "53"],
            ["Latenight", "77", "76", "76"],
        ]), "plan.csv")
        self.assertTrue(any("transposed" in n for n in plan.notes))

    def test_vertical_key_value_stanza(self):
        self._assert_four(self._csv([
            ["Screenset", self.E], ["Breakfast", "12"], ["Dinner", "30"],
            ["Lunch", "53"], ["Latenight", "76"],
        ]), "plan.csv")

    def test_several_buttons_in_one_cell(self):
        self._assert_four(self._csv([
            ["Screenset", "Buttons to disable"],
            [self.E, "12, 30, 53, 76"],
            ["01 - WWOA", "13, 31, 54, 77"],
        ]), "plan.csv")

    def test_excel_float_button_numbers(self):
        self._assert_four(self._csv([
            ["Screenset", "Breakfast", "Dinner", "Lunch", "Latenight"],
            [self.E, "12.0", "30.0", "53.0", "76.0"],
        ]), "plan.csv")

    def test_button_written_as_text(self):
        self._assert_four(self._csv([
            ["Screenset", "Breakfast", "Dinner", "Lunch", "Latenight"],
            [self.E, "Button 12", "Button 30", "#53", "76 (new)"],
        ]), "plan.csv")

    def test_unfamiliar_column_names_and_junk_columns(self):
        self._assert_four(self._csv([
            ["Notes", "Location Code", "Ticket", "Breakfast", "Dinner", "Lunch", "Latenight", "Owner"],
            ["chk", "01 - WWOA", "T-1", "13", "31", "54", "77", "asha"],
            ["chk", self.E, "T-2", "12", "30", "53", "76", "raj"],
        ]), "plan.csv")

    def test_ticket_column_is_not_read_as_buttons(self):
        # "T-1"/"T-2" must never be mistaken for buttons 1 and 2.
        plan = parse_plan(self._csv([
            ["Screenset", "Ticket", "Breakfast", "Dinner", "Lunch", "Latenight"],
            [self.E, "T-2", "12", "30", "53", "76"],
        ]), "plan.csv")
        self.assertEqual(
            sorted(t.button for t in plan.targets_for(self.E)), ["12", "30", "53", "76"]
        )

    def test_long_preamble_before_the_header(self):
        self._assert_four(self._csv([
            ["MCDONALD'S KIOSK CLEANUP - PROD"], [""], ["Owner: Saumya"],
            ["Date: 17 Jun 2026"], ["Cleanup for NEW McCafe Specialty Drinks"], [""],
            ["Screenset", "Breakfast", "Dinner", "Lunch", "Latenight"],
            [self.E, "12", "30", "53", "76"],
        ]), "plan.csv")

    def test_entity_spelled_differently_than_the_audit_log(self):
        plan = parse_plan(self._csv([
            ["Screenset", "Breakfast", "Dinner", "Lunch", "Latenight"],
            ["Fairway Portfolio A", "12", "30", "53", "76"],   # no dash
        ]), "plan.csv")
        res = run(AUDIT_CSV.read_bytes(), plan=plan)
        self.assertEqual(res.total_confirmed, 4)

    def test_one_row_per_button_without_a_daypart_column(self):
        self._assert_four(self._csv([
            ["Screenset", "Button"],
            [self.E, "12"], [self.E, "30"], [self.E, "53"], [self.E, "76"],
        ]), "plan.csv")

    def test_one_sheet_per_screenset(self):
        wb = rows_to_xlsx_multi([
            ("Instructions", [["Disable NEW McCafe Specialty Drinks"],
                              ["Open screen 61000(Left hand navigation)"]]),
            (self.E, [["Daypart", "Button"], ["Breakfast", "12"], ["Dinner", "30"],
                      ["Lunch", "53"], ["Latenight", "76"]]),
            ("01 - WWOA", [["Daypart", "Button"], ["Breakfast", "13"], ["Dinner", "31"]]),
        ])
        plan = self._assert_four(wb, "plan.xlsx")
        self.assertIn("01 - WWOA", plan.entities)

    def test_plan_asking_for_enable_reports_wrong_direction(self):
        # The audit log disabled these buttons; a plan asking to enable them
        # must fail rather than quietly pass.
        plan = parse_plan(self._csv([
            ["Screenset", "Daypart", "Button", "Action"],
            [self.E, "Breakfast", "12", "enable"], [self.E, "Dinner", "30", "enable"],
            [self.E, "Lunch", "53", "enable"], [self.E, "Latenight", "76", "enable"],
        ]), "plan.csv")
        res = run(AUDIT_CSV.read_bytes(), plan=plan)
        self.assertEqual(res.verdict, FAIL)
        self.assertEqual(
            [b.status for b in res.entities[0].buttons],
            [WRONG_DIRECTION] * 4,
        )


class TestRealWorldPlansOfOtherKinds(unittest.TestCase):
    """Real plans that are *not* screen-set button plans.

    These pin the most important safety property: when a plan is about
    something else entirely, the parser must refuse rather than invent a
    confident wrong answer. Skipped if the sample files are not present.
    """

    def _plan(self, name: str) -> bytes:
        path = PLANS_DIR / name
        if not path.exists():
            self.skipTest(f"{name} not present")
        return path.read_bytes()

    def test_display_order_plan_is_refused(self):
        # Its substance lives in three embedded PNG screenshots, not in cells.
        raw = self._plan("Display order HQ level change -Plan Assignment.xlsx")
        with self.assertRaises(PlanParseError):
            parse_plan(raw, "display.xlsx")

    def test_fifa_localization_plan_falls_back_to_evidence(self):
        # Once produced a screen set called "25710" with buttons "3228, 3171" —
        # menu-item numbers misread as a button grid. It must now invent no
        # targets at all, and instead be checkable by value matching.
        raw = self._plan("FIFA Cleanup - Kiosk Plan.xlsx")
        plan = parse_plan(raw, "fifa.xlsx")
        self.assertEqual(plan.profile, "evidence_only")
        self.assertEqual(plan.targets, [])
        self.assertEqual(plan.entities, [])
        # The two values a FIFA audit log would actually carry.
        values = {c.value for c in plan.claims}
        self.assertIn("Kiosk_Category_202604_Q2JUNEBR_396x396", values)
        self.assertTrue(any("FIFA" in v and "Limited Time" in v for v in values))

    def test_fifa_plan_against_an_unrelated_audit_log_fails(self):
        raw = self._plan("FIFA Cleanup - Kiosk Plan.xlsx")
        plan = parse_plan(raw, "fifa.xlsx")
        res = run(AUDIT_CSV.read_bytes(), plan=plan)
        # The McCafe audit log is not this plan's work, and that must be said.
        self.assertEqual(res.verdict, FAIL)
        self.assertTrue(any(f.check_id == "E2" for f in res.failures))

    def test_godzilla_plan_structure_parses_correctly(self):
        raw = self._plan("Godzilla x Hello Kitty Toy Plan (1).xlsx")
        plan = parse_plan(raw, "godzilla.xlsx")
        # A section heading must never become a screen set.
        self.assertNotIn("Manual Configuration", plan.entities)
        self.assertIn("08 - EL MAC (362)", plan.entities)
        self.assertEqual({t.button for t in plan.targets}, {"52"})
        # Known gap: the plan says "localize", which this profile cannot
        # express, so the target state is still the button-plan default.
        self.assertEqual(plan.targets[0].expect_state, "Disable")


class TestImplausiblePlanRejection(unittest.TestCase):
    """A lookup table must never become a fake button grid.

    These no longer assert an exception — the parser now degrades to value
    matching instead — but the property that matters is unchanged: no invented
    screen sets, no invented targets.
    """

    def test_numeric_entities_produce_no_targets(self):
        raw = rows_to_csv([
            ["Menu item", "25", "36677"],
            ["25710", "25", "3228"],
            ["25711", "26", "3171"],
        ])
        plan = parse_plan(raw, "lookup.csv")
        self.assertEqual(plan.profile, "evidence_only")
        self.assertEqual(plan.targets, [])
        self.assertNotIn("25710", plan.entities)

    def test_section_heading_never_becomes_a_screenset(self):
        wb = rows_to_xlsx_multi([
            ("Manual Configuration",
             [["MI number", "Button number"], ["2794", "12"], ["3699", "30"]]),
        ])
        plan = parse_plan(wb, "plan.xlsx")
        self.assertEqual(plan.targets, [])
        self.assertNotIn("Manual Configuration", plan.entities)

    def test_a_plan_with_nothing_checkable_is_still_refused(self):
        with self.assertRaises(PlanParseError):
            parse_plan(b"just some prose\nwith no values at all\n", "notes.txt")

    def test_a_real_screenset_tab_name_is_still_accepted(self):
        wb = rows_to_xlsx_multi([
            ("29 - MOCNI",
             [["Daypart", "Button"], ["Breakfast", "12"], ["Dinner", "30"]]),
        ])
        plan = parse_plan(wb, "plan.xlsx")
        self.assertEqual(plan.entities, ["29 - MOCNI"])


class TestOperationAgnosticMatching(unittest.TestCase):
    """A plan the structured engine cannot read, checked by value matching.

    The FIFA plan is a localization plan: no button grid, its payload is a
    caption and an image name. These tests build the audit log such a plan
    would produce and require the engine to judge it correctly *without any
    localization-specific rules*.
    """

    CAPTION = "Limited Time: FIFA WorldCup™ Meal"
    IMAGE = "Kiosk_Category_202604_Q2JUNEBR_396x396"
    DESC = ("Current Settings: Screen Kiosk 6 Breakfast Menu - Limited Time "
            "Promotion of screen set 01 - WWOA has been updated.")

    def _fifa_plan(self):
        path = PLANS_DIR / "FIFA Cleanup - Kiosk Plan.xlsx"
        if not path.exists():
            self.skipTest("FIFA plan not present")
        return parse_plan(path.read_bytes(), "fifa.xlsx")

    def _log(self, extra: list[tuple[str, str, str]] | None = None) -> bytes:
        head = [
            ["Audit Log Report - Effective Date[06/20/2026]"],
            ["Report generated on - Jun 20, 2026 4:30:00 AM"],
            ["Report generated by - Asha Rao"], [],
            ["Id", "Operation", "Activity", "Level", "Level Details", "Date", "User",
             "Description", "Field", "Old Setting", "New Setting", "Effective From",
             "Package generated or not"],
        ]

        def row(fld, old, new):
            return ["500112233", "Manage Screen Set", "Update", "Market",
                    "US Country Office", " Jun 20, 2026 4:10:02 AM ",
                    "er777001 - Asha Rao", self.DESC, fld, old, new,
                    "Jun 20, 2026", "N/A"]

        rows = list(head)
        for lang in ("English", "French", "SpanishUS"):
            rows.append(row(f"Caption(15-{lang})", "Saja Boys Meal", self.CAPTION))
            rows.append(row(f"Image Name(15-{lang})", "Kiosk_Category_202601_OLD_396x396", self.IMAGE))
        for fld, old, new in (extra or []):
            rows.append(row(fld, old, new))
        return rows_to_csv(rows)

    def test_localization_work_is_confirmed_with_no_bespoke_rules(self):
        plan = self._fifa_plan()
        res = run(self._log(), "fifa_audit.csv", plan=plan)
        self.assertEqual(res.verdict, PASS, [f.message for f in res.failures])
        values = {c.value for c in res.evidence.matched}
        self.assertIn(self.CAPTION, values)
        self.assertIn(self.IMAGE, values)
        self.assertEqual(res.evidence.unexplained(res.log), [])

    def test_a_change_the_plan_never_mentions_is_caught(self):
        plan = self._fifa_plan()
        res = run(
            self._log([("Caption(88-English)", "Filet-O-Fish", "Discontinued Item")]),
            "fifa_audit.csv", plan=plan,
        )
        self.assertEqual(res.verdict, FAIL)
        self.assertTrue(any(f.check_id == "E2" for f in res.failures))
        left = res.evidence.unexplained(res.log)
        self.assertEqual([e.field_text for e in left], ["Caption(88-English)"])

    def test_wrong_image_name_is_not_silently_accepted(self):
        plan = self._fifa_plan()
        rows = [("Image Name(15-English)", "old.jpg", "Kiosk_Category_WRONG_396x396")]
        res = run(self._log(rows), "fifa_audit.csv", plan=plan)
        self.assertEqual(res.verdict, FAIL)
        self.assertIn(
            "Kiosk_Category_WRONG_396x396",
            {e.new for e in res.evidence.unexplained(res.log)},
        )

    def test_a_screenset_match_alone_does_not_explain_a_row(self):
        # "01 - WWOA" appears in every Description; if that counted as
        # explaining rows, unrequested changes would be invisible.
        plan = self._fifa_plan()
        res = run(
            self._log([("Caption(88-English)", "x", "y")]), "fifa_audit.csv", plan=plan
        )
        self.assertTrue(res.evidence.unexplained(res.log))


class TestButtonCellParsing(unittest.TestCase):
    def test_accepted_forms(self):
        from auditmaster.plan_parser import _button_values
        cases = {
            "12": ["12"], "12.0": ["12"], "Button 12": ["12"], "btn 12": ["12"],
            "#12": ["12"], "12 (new)": ["12"], "Tile 12": ["12"],
            "12, 30, 53, 76": ["12", "30", "53", "76"],
            "12/30": ["12", "30"], "12 and 30": ["12", "30"],
        }
        for text, want in cases.items():
            self.assertEqual(_button_values(text), want, text)

    def test_rejected_forms(self):
        from auditmaster.plan_parser import _button_values
        for text in ("", "N/A", "none", "TBD", "-", "T-2", "17 Jun 2026",
                     "Fairway - Portfolio A", "no change", "0", "99999"):
            self.assertEqual(_button_values(text), [], text)


class TestReaders(unittest.TestCase):
    def test_encoding_detection(self):
        for enc in ("utf-8", "utf-16", "cp1252", "latin-1"):
            text = "Café,Ünïcode,naïve\n1,2,3\n"
            raw = text.encode(enc)
            got, label = decode_bytes(raw)
            self.assertIn("Café", got, f"{enc} -> {label}")

    def test_mojibake_repair(self):
        self.assertEqual(repair_mojibake("McCafÃ©"), "McCafé")
        self.assertEqual(repair_mojibake("plain text"), "plain text")

    def test_xlsx_merged_cells_are_unmerged(self):
        wb = read_any(PLAN_BYTES, PLAN_XLSX.name)
        self.assertEqual([s.name for s in wb.sheets],
                         ["Instructions", "Validation", "Screenset", "Assignment"])

    def test_legacy_xls_gives_a_useful_message(self):
        with self.assertRaises(Exception) as ctx:
            read_any(b"\xd0\xcf\x11\xe0" + b"\x00" * 64, "old.xls")
        self.assertIn("re-save", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestUnexplainedChangesFail(unittest.TestCase):
    """An audit row the plan cannot explain must count against the verdict.

    These once passed as "PASS_WITH_WARNINGS": an unrecognised Field was
    relabelled as *supporting evidence* for the planned work, which made a
    change nobody requested look like corroboration of one that was.
    """

    def _log_with(self, field: str, old: str, new: str, user: str | None = None) -> bytes:
        rows, hdr = audit_rows()
        row = list(rows[hdr + 1])
        row[COL_FIELD], row[COL_OLD], row[COL_NEW] = field, old, new
        if user is not None:
            row[COL_USER] = user
        rows.append(row)
        return rows_to_csv(rows)

    def test_unrecognised_field_fails_the_verdict(self):
        res = run(self._log_with("sahjgs", "aghsn", "sganvxs", "er924071 - who know"))
        self.assertEqual(res.verdict, FAIL)
        self.assertTrue(any(f.check_id in ("C14", "C15") for f in res.failures))

    def test_such_a_row_is_never_called_supporting_evidence(self):
        res = run(self._log_with("sahjgs", "aghsn", "sganvxs"))
        entry = res.log.entries[-1]
        self.assertNotEqual(res.entry_attribution(entry), ATTR_SUPPORTING)

    def test_a_property_on_a_planned_button_is_only_a_warning(self):
        # A workflow on button 12 accompanies work the plan asked for.
        res = run(self._log_with("Workflow(12)", "61000", "61001"))
        self.assertEqual(res.verdict, PASS_WARN)
        self.assertTrue(any(f.check_id == "C9" for f in res.warnings))

    def test_the_headline_says_what_is_wrong(self):
        res = run(self._log_with("sahjgs", "aghsn", "sganvxs"))
        line = headline(res)
        self.assertIn("not accounted for", line)
        self.assertNotIn("other check", line)

    def test_the_narrative_does_not_claim_everything_is_accounted_for(self):
        res = run(self._log_with("sahjgs", "aghsn", "sganvxs"))
        text = " ".join(narrative(res))
        if any(v for k, v in res.attribution_counts().items() if k == "unattributed"):
            self.assertNotIn("Every row of the audit log is accounted for", text)

    def test_a_clean_log_still_passes(self):
        res = run(AUDIT_CSV.read_bytes())
        self.assertEqual(res.verdict, PASS)


class TestEnvironmentQualifiedEntities(unittest.TestCase):
    """A Pre-Prod screen set is not the Prod one wearing a longer name."""

    def _plan_with(self, entity: str):
        from auditmaster.plan_schema import Plan, Target
        p = Plan()
        p.targets = [Target(entity=entity, group="Breakfast", button="12")]
        return p

    def test_a_screen_number_suffix_still_matches(self):
        # "(362)" is a screen number, not a different co-op.
        plan = self._plan_with("16 - GREAT PLAINS (362)")
        self.assertEqual(plan.find_entity("16 - GREAT PLAINS"), "16 - GREAT PLAINS (362)")

    def test_preprod_does_not_match_prod(self):
        plan = self._plan_with("29 - MOCNI (Pre-Prod)")
        self.assertIsNone(plan.find_entity("29 - MOCNI"))

    def test_prod_does_not_match_preprod(self):
        plan = self._plan_with("29 - MOCNI")
        self.assertIsNone(plan.find_entity("29 - MOCNI (Pre-Prod)"))

    def test_an_exact_match_always_wins(self):
        from auditmaster.plan_schema import Plan, Target
        p = Plan()
        p.targets = [
            Target(entity="29 - MOCNI", group="Breakfast", button="12"),
            Target(entity="29 - MOCNI (Pre-Prod)", group="Breakfast", button="12"),
        ]
        self.assertEqual(p.find_entity("29 - MOCNI"), "29 - MOCNI")
        self.assertEqual(p.find_entity("29 - MOCNI (Pre-Prod)"), "29 - MOCNI (Pre-Prod)")


class TestUnrelatedWorkIsSeparated(unittest.TestCase):
    """An audit export is a time window, not a deployment.

    Exports routinely contain several unrelated tasks. Calling all of it
    "unauthorized" buries the one change that was made alongside the planned
    work. The transaction id is the discriminator.
    """

    def _with_extra(self, tx: str, field: str = "Caption(41-English)") -> bytes:
        rows, hdr = audit_rows()
        extra = list(rows[hdr + 1])
        extra[col("Field")] = field
        extra[col("Old Setting")], extra[col("New Setting")] = "Some Tile", "blank"
        extra[col("Id")] = tx
        return rows_to_csv(rows + [extra])

    def _planned_tx(self) -> str:
        rows, hdr = audit_rows()
        return rows[hdr + 1][col("Id")]

    def test_same_transaction_is_a_deviation(self):
        res = run(self._with_extra(self._planned_tx()))
        self.assertEqual(res.verdict, FAIL)
        self.assertTrue(any(f.check_id == "C2" and f.is_failure for f in res.all_findings))

    def test_different_transaction_is_other_work(self):
        res = run(self._with_extra("999999999"))
        self.assertTrue(any(f.check_id == "C17" for f in res.all_findings))
        self.assertFalse(any(f.check_id == "C2" for f in res.all_findings))

    def test_other_work_does_not_fail_the_verdict(self):
        # It may still raise a warning about the tile it touched; what matters
        # is that somebody else's task cannot fail this plan's validation.
        res = run(self._with_extra("999999999"))
        self.assertNotEqual(res.verdict, FAIL)

    def test_other_work_rows_are_still_accounted_for(self):
        res = run(self._with_extra("999999999"))
        counts = res.attribution_counts()
        self.assertTrue(counts.get("other_work"))
        self.assertFalse(counts.get("unattributed"))


class TestRepeatWorkOnAButton(unittest.TestCase):
    """A button worked on twice must not lose rows.

    The per-language maps hold one row each; an overwritten row used to vanish
    and reappear later as an "unexplained change" — a defect reported that
    never happened.
    """

    def test_a_second_pass_over_the_same_button_keeps_every_row(self):
        rows, hdr = audit_rows()
        dupe = [r for r in rows[hdr + 1:]
                if r and r[col("Field")] == "Caption(76-English)"]
        self.assertTrue(dupe, "expected a caption row to duplicate")
        repeat = list(dupe[0])
        repeat[col("Old Setting")] = "A Different Earlier Tile"
        res = run(rows_to_csv(rows + [repeat]))
        counts = res.attribution_counts()
        self.assertFalse(
            counts.get("unattributed"),
            f"a repeat edit went unexplained: {counts}",
        )

    def test_the_verdict_is_unaffected_by_a_repeat(self):
        rows, hdr = audit_rows()
        dupe = next(r for r in rows[hdr + 1:]
                    if r and r[col("Field")] == "Caption(76-English)")
        repeat = list(dupe)
        repeat[col("Old Setting")] = "A Different Earlier Tile"
        self.assertNotEqual(run(rows_to_csv(rows + [repeat])).verdict, FAIL)


class TestDescriptionDoesNotInventScreenSets(unittest.TestCase):
    def test_an_assignment_sentence_names_no_screen_set(self):
        from auditmaster.audit_parser import parse_description
        text = ("Screen Set Assignation in the Current Settings of the "
                "Restaurant Profile Winder(44126) has been updated.")
        self.assertEqual(parse_description(text), ("", ""))

    def test_a_real_description_still_parses(self):
        from auditmaster.audit_parser import parse_description
        text = ("Current Settings: Screen Kiosk 6 Left Hand Navigation of "
                "screen set Fairway - Portfolio A has been updated.")
        self.assertEqual(
            parse_description(text),
            ("Fairway - Portfolio A", "Kiosk 6 Left Hand Navigation"),
        )


class TestWhereTheChangeHappened(unittest.TestCase):
    """The Description column answers "where", for several kinds of target.

    Reading only the screen-set wording left one row in eight with no location
    at all — on the field a reviewer treats as the primary one.
    """

    CASES = [
        ("Current Settings: Screen Kiosk 6 Left Hand Navigation of screen set "
         "Fairway - Portfolio A has been updated.",
         "Manage Screen Set", "Screen set", "Fairway - Portfolio A"),
        ("Current Setting for Menu Item 25702 has been updated.",
         "Menu Item", "Menu item", "25702"),
        ("Status for menu item 25638 at restaurant 4321 - Winder has been updated.",
         "Menu Item", "Menu item", "25638 at 4321 - Winder"),
        ("The Current Settings of the Restaurant Profile CARY - HARRISON(11523) "
         "has been updated.",
         "Restaurant Profile", "Restaurant", "CARY - HARRISON(11523)"),
        ("User ed046072 has been updated.", "Users", "User", "ed046072"),
        ("Media Asset Kiosk_Category_20160330_Desserts File has been updated.",
         "Media File", "Media asset", "Kiosk_Category_20160330_Desserts"),
    ]

    def test_each_kind_of_target_is_recognised(self):
        from auditmaster.audit_parser import parse_target
        for description, operation, kind, name in self.CASES:
            with self.subTest(kind=kind):
                self.assertEqual(parse_target(description, operation), (kind, name))

    def test_an_unknown_description_falls_back_to_the_operation(self):
        from auditmaster.audit_parser import parse_target
        kind, name = parse_target("Something entirely new happened.", "Widget Thing")
        self.assertEqual(kind, "Widget Thing")
        self.assertEqual(name, "")

    def test_an_empty_description_yields_nothing(self):
        from auditmaster.audit_parser import parse_target
        self.assertEqual(parse_target("", "Users"), ("", ""))

    def test_every_row_of_the_sample_log_has_a_where(self):
        log = parse_audit_log(AUDIT_CSV.read_bytes(), "a.csv")
        missing = [e.row_no for e in log.entries if not e.where]
        self.assertEqual(missing, [], "rows with no location")

    def test_where_reads_as_a_sentence_not_a_code(self):
        log = parse_audit_log(AUDIT_CSV.read_bytes(), "a.csv")
        entry = log.entries[0]
        self.assertIn("Fairway - Portfolio A", entry.where)
        self.assertIn("Kiosk 6 Left Hand Navigation", entry.where)


@unittest.skipUnless((ROOT / "samples" / "audit-logs").is_dir(), "no samples")
class TestEveryRealLogRowHasALocation(unittest.TestCase):
    def test_no_row_anywhere_is_missing_its_where(self):
        logs = sorted((ROOT / "samples" / "audit-logs").glob("*.csv"))
        for path in logs:
            with self.subTest(log=path.name):
                log = parse_audit_log(path.read_bytes(), path.name)
                blank = [e.row_no for e in log.entries if not e.where]
                self.assertEqual(
                    blank[:5], [],
                    f"{len(blank)} of {len(log.entries)} rows have no location",
                )
