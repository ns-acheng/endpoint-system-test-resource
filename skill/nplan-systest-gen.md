---
name: nplan-systest-gen
description: Generate a per-NPLAN system test plan (standalone xlsx) from ONLY the NPLAN's own markdown + the architecture chapters in ns-klin/endpoint_system_test_plan. No IMF/ticket database, no doc/index.
---

You are generating a system test plan for one NPLAN, as a **brand-new, standalone xlsx**.
Target NPLAN doc: $ARGUMENTS

Lightweight sibling of `claude-resource/skills/system-test-gen` + `testplan-gen` (those mine
an IMF/escalation/ticket database and maintain a cross-feature baseline suite). This one
has exactly two inputs and no other dependency.

## Inputs (exactly two)

1. **NPLAN doc** -- a design/test-plan markdown, path given as `$ARGUMENTS`. If missing, ask.
2. **Architecture chapters** -- `chapters/*.md` from a local clone of
   [`ns-klin/endpoint_system_test_plan`](https://github.com/ns-klin/endpoint_system_test_plan).
   Chapters only, never `bugs/`/`docs/`. If you don't know the clone path, ask -- never
   hardcode `C:\git\...`.

Nothing else. Wanting a third input (xlsx, IMF list, ticket) = wrong skill; use the full one.

## Steps

1. **Read the NPLAN doc.** Extract: title/description, scope/out-of-scope, feature flags,
   phases if any, "Scenario N" cases, "must/should/verify" statements, platforms actually named.
2. **Pick only relevant chapters** (typically 2-5, not all 22). Read `00_overview.md` first,
   then match extracted topics to chapter subjects (tunnel->07, FailClose->11, config->04,
   steering->05, service lifecycle->03, IPC->17, security->18, etc. -- match by keyword, not
   by guessing).
3. **Judge priority, no incident database:**
   - P0 = sits on a documented state machine / callback cascade / crash-recovery path
   - P1 = cross-component but recoverable / precondition-gated
   - P2 = isolated to the new feature's own path
   Never cite an IMF/ENG number unless the NPLAN doc itself has one.
4. **Build 4-10 test cases** (+ variants only when the doc/chapter implies a disruption
   risk -- reboot, network loss, crash; cap 2 variants per case). Each case needs: ID,
   Priority, Platforms (only ones the doc names), Objective/Risk (cite the chapter
   mechanism), Steps, Pass Criteria, Failure Indicators, Source (`nplan file + chapter file`).
5. **Write a brand-new xlsx**: `nplans/sysplan-<nplan-id>.xlsx`.

## Hard rule -- no exceptions

Always `openpyxl.Workbook()`. **NEVER `load_workbook()` an existing file, never append.**
- Output path already exists -> write a new name (`-v2.xlsx`); never overwrite/merge.
- Never open `resilience_systest/system_test_v0.xlsx`, `system_test_new.xlsx`,
  `Windows_System_Test_Plan_Enhance.xlsx`, or anything else under `nplans/`/`resilience_systest/`
  -- those belong to the full skills, not this one, not even read-only.

```python
import openpyxl, os
from openpyxl.styles import Alignment, Font

HEADERS = ["Test ID","Test Item","Priority","Platforms","Objective / Risk",
           "Execution Steps","Pass Criteria","Failure Indicators","Source"]
wb = openpyxl.Workbook()          # ALWAYS fresh
ws = wb.active; ws.title = "SYSPLAN"
ws.append(HEADERS)
for c in ws[1]: c.font = Font(bold=True)
ws.freeze_panes = "A2"
for row in rows:  # list[dict] in HEADERS order
    ws.append([row[h] for h in HEADERS])
for col in ws.columns:
    if col[0].column_letter in {"E","F","G","H"}:
        for cell in col: cell.alignment = Alignment(wrap_text=True, vertical="top")
out_path = "nplans/sysplan-<nplan-id>.xlsx"
if os.path.exists(out_path):
    raise Exception(f"{out_path} exists -- pick a new name")
wb.save(out_path)
```

## Before reporting done

- [ ] Used `Workbook()`, never `load_workbook()` -- zero exceptions
- [ ] Didn't overwrite an existing file
- [ ] Every row traces to the NPLAN doc + cites a chapter; no invented IMF/ENG numbers
- [ ] Platforms = only what the doc names; 4-10 base cases (or justify otherwise)

Report: file path, chapters used + why, anything skipped.
