# Claude Code Skill: nplan-systest-gen

A lightweight, two-input system test plan generator for a single NPLAN. Writes a
brand-new xlsx -- never touches any existing workbook.

## What it does

Given (1) an NPLAN's own design/test-plan markdown and (2) the architecture chapters from
[`ns-klin/endpoint_system_test_plan`](https://github.com/ns-klin/endpoint_system_test_plan),
Claude Code will:

1. Read the NPLAN doc and extract scope, flags, phases, requirements, platforms
2. Pick only the 2-5 architecture chapters that are actually relevant (not all 22)
3. Judge priority (P0/P1/P2) by whether the feature touches a documented state machine /
   callback cascade -- no incident database required
4. Write 4-10 system-level test cases (+ a few disruption variants where justified)
5. Save them to a **brand-new** `nplans/sysplan-<nplan-id>.xlsx` -- it will refuse to
   overwrite or append to an existing file

This is the simplified sibling of the full `system-test-gen` / `testplan-gen` skills (which
also mine an IMF/escalation-bug/ticket database and maintain a cross-feature baseline
suite, both in `claude-resource`). This one has exactly two inputs and no other
dependency, so it runs in any clone with no index-building step.

## Installation

**Option A -- Claude Code Skill (recommended):**

```bash
mkdir -p ~/.claude/skills/nplan-systest-gen
cp skill/nplan-systest-gen.md ~/.claude/skills/nplan-systest-gen/skill.md
```

It will then be available as `/nplan-systest-gen` in any Claude Code session.

**Option B -- project-scoped slash command** (same pattern as this repo's sibling,
`ns-klin/endpoint_system_test_plan`'s `write-chapter` skill):

```bash
mkdir -p .claude/commands
cp skill/nplan-systest-gen.md .claude/commands/nplan-systest-gen.md
```

Available as `/project:nplan-systest-gen` inside this repo only. The YAML frontmatter at
the top of the file is harmless if left in -- Claude Code's commands reader ignores
unrecognized leading `---` blocks, but you may strip it for a cleaner command file if you
prefer.

## Usage

```
/nplan-systest-gen path/to/nplan-7032.md
```

The argument is a path to the NPLAN's own markdown (design doc or functional test plan).
If you don't pass one, Claude Code will ask you for it -- it never guesses or looks one up
from an index.

## Prerequisites

1. A local clone of [`ns-klin/endpoint_system_test_plan`](https://github.com/ns-klin/endpoint_system_test_plan)
   with its `chapters/*.md` present. The skill will ask for this path the first time if it
   doesn't already know it -- **it never hardcodes a path**, since your clone location
   will differ from whoever generated this file.
2. `openpyxl` available in whatever Python environment Claude Code shells out to
   (`pip install openpyxl` if missing).
3. This repo (`endpoint-system-test-resource`) cloned, so `nplans/` exists as the output
   directory.

## Output safety

The skill is hard-coded (in its own instructions) to **never** call
`openpyxl.load_workbook()` on an existing file and to refuse to overwrite an existing
output path. It only ever creates a brand-new workbook. If you ask it to regenerate a plan
for an NPLAN it already produced a file for, it will write a new filename (e.g.
`sysplan-nplan-7032-v2.xlsx`) rather than touch the old one. It also never opens any of the
cross-feature baseline xlsx files (`system_test_v0.xlsx`, `system_test_new.xlsx`,
`Windows_System_Test_Plan_Enhance.xlsx`) that the full `system-test-gen` /
`system-res-test-gen` skills maintain elsewhere.

## Customization

The skill file is plain markdown -- edit `nplan-systest-gen.md` to fit your workflow:
- **Chapter-matching keywords**: Stage 2's table maps chapter -> trigger keywords; add
  rows if `endpoint_system_test_plan` gains new chapters.
- **Column schema**: Stage 5 defines a 9-column schema distinct from the master
  workbooks' schema (no IMF column, since this skill has no incident data). Edit the
  `HEADERS` list if your team wants different columns -- just don't add a column you
  can't honestly populate.
- **Case cap**: Stage 4's "4-10 base cases" guidance is a judgment anchor, not a hard
  limit -- adjust if your NPLANs are consistently larger/smaller in systemic-risk surface.

## Related docs

- [`nplan-systest-gen.md`](nplan-systest-gen.md) -- the skill itself
- Full (IMF-driven) version: `claude-resource/skills/system-test-gen` and
  `claude-resource/skills/testplan-gen` (not in this repo)
- Architecture chapters this skill reads: `ns-klin/endpoint_system_test_plan/chapters/`
