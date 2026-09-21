# Sample files

Real plans and audit-log exports, kept so the app can be tried straight away and
so the test suite has genuine material to run against. Nothing here is required
for the app to run — delete the folder and everything still works, the tests
that use it simply skip.

## Which pair actually matches

Upload a plan **and** its log together to see a validation. Most combinations
are mismatched and will report almost nothing as confirmed, which is correct but
not a useful demonstration.

| Plan | Matching log | What you should see |
|---|---|---|
| `EVM Realignment & Limited Time HBB & Caser Chicken Cleanup Plan Kiosk 1.xlsx` | `Audit_Log_Report_09_04_26_11_17_28 1.csv` | **FAIL** — 202 of 206 changes confirmed, but 8 tiles were edited on `38 - GAMOA` that the plan never asked for |
| `McCafe Speciality Tile - Kiosk Cleanup.xlsx` | `Audit_Log_Report_06_17_26_05_30_54.csv` | **PASS** — all 4 planned changes confirmed, nothing unexpected |

Or upload **any log on its own** for a summary of what it contains, with no plan
involved.

## audit-logs/

Seven exports, from 9 rows to 13,758. Worth knowing:

- `Audit_Log_Report_06_17_26_05_30_54.csv` — the small clean one. Best for a
  first look.
- `Audit_Log_Report_06_17_26_05_30_54 (edited, 3 extra test rows).csv` — the same
  file with three deliberately broken rows appended (a made-up field name, a
  repeat, and a change by a different user). Upload it with the McCafe plan to
  see the app **fail** a log rather than pass it.
- `Audit_Log_Report_08_24_26_08_23_25.csv` — 13,758 rows, 47 screen sets. The
  stress case.

## deployment-plans/

Twelve plans in deliberately different shapes. Five are screen-set/button plans
the app validates fully. The rest are other kinds of work — localisation,
menu-item display orders — which the app reads by matching the values they name
against the log, because it cannot verify per-button correctness for those.

One plan, `Display order HQ level change -Plan Assignment.xlsx`, is **refused**
on purpose: its content is inside embedded screenshots rather than cells, so
there is genuinely nothing to read. The app says so rather than guessing.
