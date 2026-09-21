# Audit Master

A Flask web app that validates deployment work by reconciling a **deployment
plan** (what should have changed) against an **audit-log export** (what actually
changed).

Everything runs locally. There is no API key, no model call and no outbound
network request — every judgement comes from rules in `auditmaster/validator.py`.

```bash
./run.sh          # macOS / Linux
run.bat           # Windows
```

First run creates `.venv` and installs Flask, the only dependency. Your browser
opens automatically; close the window to stop the server.

### A double-clickable build

```bat
build.bat         REM on Windows  -> dist\AuditMaster.exe
./build.sh        # on macOS      -> dist/AuditMaster
```

One file, about 12 MB, no Python needed on the machine that runs it. Because the
only dependency is Flask and everything else is standard library, there are no
binary wheels to go wrong.

**PyInstaller cannot cross-compile**, so a Windows `.exe` must be built on
Windows — `build.bat` does it in one step. Worth knowing before you hand it
round: the first launch takes about ten seconds (the single file unpacks
itself), an unsigned build triggers a SmartScreen warning the first time, and
one-file builds occasionally trip corporate antivirus. If that friction is not
worth it, run `run.bat` on one machine with `--host 0.0.0.0` and send your team
the URL instead.

```bash
.venv/bin/python -m unittest discover -s tests -v      # 137 tests
```

---

## What is in this folder

```
run.bat / run.sh        start it (creates the environment on first run)
build.bat / build.sh    make a double-clickable AuditMaster.exe / AuditMaster
launch.py               the entry point both of the above use
app.py                  web routes
auditmaster/            the engine — parsing, validation, reporting
templates/  static/     the interface
tests/                  137 tests
samples/                real plans and audit logs to try it with
```

`run.sh` / `run.bat` start the app with the debugger **off**. Werkzeug's
debugger offers an interactive Python console to anyone who can reach the port,
so it is opt-in for development only (`AUDITMASTER_DEBUG=1 python app.py`) and
never on for a shared run.

Nothing else is needed. `.venv/` appears on first run and is disposable —
delete it before sharing the folder and `run.bat` will rebuild it.

---

## Two modes

**Audit log on its own** — upload just the log and get a description of what it
contains: how many rows, which screen sets and buttons, which fields and
languages, who made the changes and when, the busiest screen sets, and a list of
*observations* (several users, repeated rows, changes that changed nothing).
Nothing here is a verdict — a log alone cannot be right or wrong, only
consistent or unusual.

**Audit log + deployment plan** — the full reconciliation below: was the planned
work done, what is missing, and what was changed that nobody asked for.

One upload form serves both. The plan is optional; the button tells you which
mode you are about to run.

## What it answers

Upload the two files and you get a verdict, a scorecard, and a plain-language
summary that accounts for **every row** of the audit log.

The reconciliation is three-way, and the third way is the point:

| | |
|---|---|
| **Confirmed** | The plan asked for it, the audit log proves it happened. |
| **Missing** | The plan asked for it, the audit log does not show it. |
| **Unauthorized** | The audit log shows it, the plan never asked for it. |

A human reading an audit report reliably catches the first two and misses the
third — which is the dangerous one: a button disabled on a screen set nobody
planned to touch. That is check `C2`, and it is exactly the rule the sample
plan states for itself: *"Only these buttons should be disabled for your
assigned coops."*

### Checks

| Check | Question | On failure |
|---|---|---|
| `C1` | Did every button in the plan reach its planned state? | critical |
| `C2` | Was anything changed the plan never asked for? | critical |
| `C3` | Did a button move to the wrong state? | critical |
| `C4` | Was the tile that got cleaned up the one the plan names? | warning |
| `C5` | Was the work done on the screen the plan targets? | warning¹ |
| `C6` | Was tile content cleared evenly across languages? | observation¹ |
| `C7` | Did each disabled button also have its content blanked? | observation¹ |
| `C8` | Did the work touch a screen set outside the plan entirely? | critical |
| `C9` | Were extra properties changed **on a button the plan covers**? | warning |
| `C10` | Which planned screen sets does this log not cover? | observation¹ |
| `C11`–`C13` | Operation type, effective dates, provenance. | warning / info |
| `C14` | Any audit row that cannot be tied to anything in the plan. | **critical** |
| `C15` | A property changed that belongs to no button the plan asks for. | **critical** |
| `C16` | Changed by someone the plan does not name as assignee or validator. | warning² |
| `C17` | Work in this export belonging to a different task entirely. | information |

¹ Promotable to a hard failure with a checkbox on the upload form.
² Only runs when the plan actually lists assignees; a check that fires on
missing data is worse than no check.

`C14` and `C15` are deliberately critical. An audit row the plan cannot explain
is precisely what this tool exists to surface, so it counts against the verdict
rather than being noted in passing. An earlier version relabelled such rows as
*supporting evidence*, which made a change nobody requested look like
corroboration of one that was — the opposite of the truth.

**Verdict** is `PASS` / `PASS_WITH_WARNINGS` / `FAIL`. Defects in how the two
*documents* were authored are reported separately and never move the verdict —
a mislabelled row in a plan says nothing about whether the work was done right.

### An export is a time window, not a deployment

A single audit export routinely contains several unrelated pieces of work. On a
real pair from this corpus, a plan's 206 expected changes sat alongside **213
other transactions** doing something else entirely. Reporting all of that as
"unauthorized" produced 3,625 findings and buried the one that mattered.

The discriminator is the **transaction id**:

| Where the change was made | Treated as |
|---|---|
| In a save that also did this plan's work | **Unauthorized** — a real deviation, fails |
| In a save that did none of this plan's work | **Other work** — reported, does not fail |

On that same pair this turns 3,625 findings into **8**, all on one screen set in
one transaction: someone carrying out the plan also edited eight tiles they were
not asked to touch. That is the finding worth having.

Because unrelated saves are separated first, the remaining checks can afford to
be strict — editing tile content on an unplanned button in the planned save is a
failure, where before it was only a warning to avoid drowning the report.

### Robustness

Tested against every real log in the corpus plus deliberately hostile input.
Four defects this found, each now pinned by a test:

| Defect | Why it mattered |
|---|---|
| Validating a plan twice **rewrote the earlier result** | Results are kept in memory and reachable by URL; the first one silently showed the second log's evidence. `reconcile` filled its findings in on the plan's own claim objects. |
| **CSV injection** in the audit-row export | A value beginning `=`, `+`, `-`, `@` or a tab executes when the export is opened in Excel — and this export exists to be opened in Excel. Such values are now prefixed so the spreadsheet reads them as text. |
| Parsing a few-thousand-row sheet took **20 seconds** | `Sheet.n_cols` recomputed the widest row on every access — 139 million inner iterations on one real plan. Now cached; that plan parses in 0.8s. |
| A 60,000-character value produced a **9.5 MB page** | Display values are clipped (the full value stays in `old`/`new` and in the CSV export), so the same input now renders in 47 KB. |

Also verified: markup in a value is escaped, an XXE payload in a workbook
discloses nothing, malformed uploads (truncated, empty, not-a-zip, zip without a
workbook) are refused with a message rather than a traceback, and every row of
every corpus log lands in exactly one bucket, deterministically, regardless of
row order or encoding.

---

## Reading a deployment plan in any format

> "the parsing of that deployment file which can be in any format in any
> encoding be done in such a way that it can automatically be changed in a
> structured way"

Every plan is normalized into a **Normalized Deployment Plan (NDP)** — the
template defined in `auditmaster/plan_schema.py` — before anything is compared.
A plan becomes a flat list of `Target` rows; one target is one thing that must be
true in the audit log, addressed as `(entity, group, button)`:

```json
{
  "schema_version": "1.0",
  "profile": "screenset_button_matrix",
  "meta": {
    "screen_number": "61000",
    "screen_name": "Left hand navigation",
    "tile_label": "NEW McCafé Specialty Drinks",
    "action": "disable"
  },
  "targets": [
    { "entity": "Fairway - Portfolio A", "group": "Breakfast",
      "button": "12", "expect_state": "Disable", "source": "Screenset!R52C2" }
  ]
}
```

`source` records the originating cell, so any conclusion traces back to a
spreadsheet coordinate.

### Nothing is addressed by fixed position

The parser *locates* structure instead of assuming it, in four passes — most
specific first, first one to yield targets wins:

1. **Named matrix** — a row of recognised daypart names beside an entity column.
   The header band is found by scoring rows on how many grouping aliases they
   contain, so **two-row headers** (a spanning `Button Number` title above
   `Breakfast / Dinner / Lunch / Latenight`) fall out of the scoring rather than
   being special-cased.
2. **Shape matrix** — no daypart names recognised, so: one text column beside
   two or more button-valued columns. Guarded, because shape alone cannot tell a
   grid from the same grid pivoted (see below).
3. **Flat list** — an entity column beside a button column, gathered across
   *every* sheet that has one. If a sheet has buttons but no entity column, the
   sheet's own name becomes the entity — that covers one-tab-per-screen-set plans.
4. **Transposed** — each sheet flipped and retried, which rescues both a pivoted
   grid and the vertical `group | button` stanza.

Context (screen number, tile label, action, daypart→workflow map) is harvested
by pattern from the prose sheets, wherever it happens to sit.

Button cells are read leniently — `12`, `12.0` (Excel numerics), `Button 12`,
`#12`, `12 (new)` and `"12, 30, 53, 76"` all work, and one cell may expand into
several targets — while staying strict enough that a ticket reference like `T-2`
is never mistaken for buttons 1 and 2.

### Shapes that are covered

Each of these expresses the same four expected changes and is a test case:

| Layout | |
|---|---|
| Screen sets down, dayparts across (the sample plan) | ✅ |
| **Transposed** — dayparts down, screen sets across | ✅ |
| Vertical `Screenset \| name` then `daypart \| button` stanzas | ✅ |
| All buttons in one cell: `12, 30, 53, 76` | ✅ |
| `Button 12` / `#53` / `76 (new)` as text | ✅ |
| `12.0` Excel float numerics | ✅ |
| Renamed columns (`Location Code`) + junk columns (`Notes`, `Ticket`, `Owner`) | ✅ |
| Long title/preamble block above the real header | ✅ |
| Entity spelled differently than the audit log (`Fairway Portfolio A`) | ✅ |
| Flat one-row-per-button, with or without a daypart column | ✅ |
| One sheet per screen set, tab name = screen set | ✅ |
| A plan asking to **enable** what the log **disabled** | ✅ reported as a mismatch, not a pass |

What is *not* covered: a plan about something other than buttons (prices, menu
items, store hours) needs a new profile — the checks in `validator.py` are
written against button state. That is the real boundary.

Add a new plan layout by adding a profile to `auditmaster/profiles.py` — the
aliases and sheet-role tables are pure declaration, and the parser reads them.

### The escape hatch

Auto-detection can still be wrong on a sufficiently strange plan. Every result
page offers **Normalized plan template (.json)** — the plan as the app
understood it. Correct that file by hand and upload it as the plan; the parser
accepts an NDP JSON directly, bypassing detection entirely.

### Formats and encodings

`.xlsx` `.xlsm` `.csv` `.tsv` `.txt` `.json` `.html` `.md` — and NDP JSON.

Content sniffing beats the file extension, because plans get passed around with
the wrong suffix. `.xlsx` is read straight out of its zip container with
`ElementTree` (no pandas, no openpyxl); merged cells are unmerged so header
detection works; date-styled cells are converted from Excel serials.

Encoding is detected without `chardet`: BOM sniffing, then a BOM-less UTF-16
check, then candidates scored on replacement characters, stray control bytes and
mojibake signatures. **Mojibake is repaired** — UTF-8 read as cp1252 turns
`McCafé` into `McCafÃ©`, and that round trip is undone when doing so measurably
improves the text. Label comparison is also accent-folded, so `McCafé` and
`McCafe` match across two systems that disagree about diacritics.

---

## Reading the audit log

Columns are fixed in name but located by header, so a reordered or re-exported
report keeps working, and unknown extra columns are ignored. The real work is
decomposing the two free-text columns that carry the meaning:

```
Field        "Button(53)(Button(53))"   -> property=Button,     button=53, language=—
             "Image Name(76-French)"    -> property=Image Name, button=76, language=French
Description  "…Screen Kiosk 6 Left Hand Navigation of screen set
              Fairway - Portfolio A has been updated."
                                        -> screen_set + screen
```

Each row is then classified — `disabled`, `enabled`, `cleared`, `set`,
`changed`, `unchanged` — and **attributed to exactly one bucket**:

- `Planned change` — the planned button toggle itself
- `Supporting evidence` — caption/image fields blanked alongside a planned button
- `Unauthorized change` — a change the plan never asked for
- `Out of plan scope` — a screen set the plan does not cover
- `Unattributed` — could not be tied to anything (reported, never ignored)

So the summary accounts for the whole log rather than only the rows it went
looking for. The result page's row table is filterable by bucket.

---

## Result on the supplied files

The sample plan lists `Fairway - Portfolio A` → Breakfast **12**, Dinner **30**,
Lunch **53**, Latenight **76**. The audit log records exactly those four buttons
going `Enable → Disable` on `Kiosk 6 Left Hand Navigation`, in one transaction.

```
Verdict : PASS — work matches the plan
          4 of 4 planned changes confirmed · 0 missing · 0 unauthorized · 100%
          34 audit rows: 4 planned changes + 30 supporting evidence
```

Two observations that do **not** affect the verdict:

- The 30 caption/image rows are unevenly spread across languages (button 12
  covers 3 of 6, button 76 covers 6 of 6). This is normal — the audit log only
  records fields that actually changed, so a language already blank produces no
  row. Reported as `C6`, promotable to a failure with the parity checkbox.
- Two genuine defects in the plan itself, found while parsing: the
  daypart→workflow table lists **Dinner twice** (61180 and 62630) and never
  lists **Latenight**. Reported under *Document quality*.

### Exports

`Report (.txt)` for a ticket · `Annotated audit rows (.csv)` — the original log
plus a verdict on every line · `Full result (.json)` · `Normalized plan
template (.json)`.

---

## Layout

```
app.py                      Flask routes, in-memory result cache
auditmaster/
  readers.py                any format/encoding -> list[Sheet]
  plan_schema.py            the NDP: Plan / Target / PlanMeta, name normalisation
  profiles.py               alias + sheet-role tables — add new plan shapes here
  plan_parser.py            spreadsheet -> NDP (structure location, not fixed cells)
  audit_parser.py           audit export -> decomposed AuditEntry list
  validator.py              three-way reconciliation, the C1–C14 checks
  report.py                 narrative summary, txt/csv/json exports
templates/  static/         server-rendered UI, light/dark aware
tests/test_validation.py    plan layouts, checks and encodings
tests/test_summary.py       the audit-log-only summary and both upload modes
tests/test_corpus.py        sweeps every real plan and log available locally
tests/test_robustness.py    shared state, CSV injection, malformed input, performance
```

The test suite corrupts the known-good audit log one way at a time — drop a
planned button, add an unplanned one, reverse a toggle, retarget to an unknown
screen set, swap the tile caption — and asserts the matching check fires. It
also feeds the same log in as UTF-16, TSV, semicolon-delimited, mojibake'd
cp1252, `.xlsx`, HTML and reordered-column CSV, requiring an identical verdict
from each.

## Notes

- Results live in memory (24 most recent) and are lost on restart; nothing is
  written to disk and no database is used.
- Upload limit 32 MB.
- Legacy `.xls` and `.ods` are rejected with a message telling you to re-save.
- Uploading the two files the wrong way round is detected and corrected.
