---
nplan: null
type: regression-suite
categories:
  - 64-bit Migration
  - Auto Upgrade
  - Config Download
  - FailClose
  - NPA Integration
  - Self-Protection / TamperProof
  - Watchdog
related_nplans:
  - NPLAN-3211
  - NPLAN-4534
  - NPLAN-watchdog
status: active
source: frontmatter
---

# Regression Suite — NSClient Endpoint Cumulative

This file accumulates regression-worthy cases across releases. The `/system-res-test-gen` agent appends new rows during each release; **retirement is user-triggered only** (agent never modifies or removes rows).

## Purpose

Each release contributes its most regression-worthy P0 cases from per-NPLAN sysplans. The accumulated suite re-runs every release at 30 reps per case, catching regressions of past-shipped features that new code might have broken.

## Selection Gate

A case enters regression ONLY if at least ONE of:
1. Critical or High IMF link
2. Day-1 escalation bug link
3. Security boundary (FailClose, self-protection / TamperProof, WFP driver, kernel callout)
4. Permanent-broken-state failure mode (nsclient gone, persistent block, data loss, deviceUid lost)

P1 / P2 sysplan cases never qualify.

## Caps

- Per NPLAN: 3-5 cases
- Per release total: 8 cases

## Iteration Count

30 reps per case per release run.

## Retirement Process (User-Triggered)

When the user wants to retire a case:
1. Identify the `Case ID` (`SYSTEST-REG-NNN`)
2. Edit the row's `Status` column from `ACTIVE` to `RETIRED — RNNN — <reason>` where RNNN is the release that triggered retirement
3. Retired rows stay in the file as audit history; they're not run

The agent does NOT auto-retire. It only appends new rows.

---

## Active Suite

| Case ID | Source NPLAN | Source SYS-XX | Added in Release | Selection Reason | Test Summary | Stability Properties | Reps | Status |
|---|---|---|---|---|---|---|---|---|
| SYSTEST-REG-001 | NPLAN-3211 + NPLAN-watchdog | sysplan-nplan-3211 SYS-001 V2 (cross-source: sysplan-nplan-watchdog SYS-001 V2) | R138 | IMF-1335 user-supplied + IMF-919 High + permanent-broken-state | Cross-bitness 32→64 upgrade × watchdog FF on × reboot mid-RemoveExistingProducts; verify nsclient never permanently absent | P-STAB, P-RES, P-CRASH, P-NET | 30 | ACTIVE |
| SYSTEST-REG-002 | NPLAN-3211 | sysplan-nplan-3211 SYS-002 | R138 | ENG-466704 Day-1 + permanent-broken-state (re-enrollment storm) | 32→64→32 cross-bitness churn; verify PR #8105 token backup/restore preserves AuthToken + EncToken byte-for-byte | P-STAB, P-CRASH | 30 | ACTIVE |
| SYSTEST-REG-003 | NPLAN-3211 | sysplan-nplan-3211 SYS-003 | R138 | IMF-919 High + security boundary (WFP driver) | WFP driver bitness swap × steering parity verification; 20 representative flows match 32-bit baseline | P-STAB, P-NET | 30 | ACTIVE |
| SYSTEST-REG-004 | NPLAN-watchdog | sysplan-nplan-watchdog SYS-006 | R138 | ENG-895081 Day-1 Critical + ENG-991833 Day-1 + security boundary (FailClose) | Force-crash stAgentSvc with FC ON; verify watchdog-triggered restart preserves FailClose policy correctness | P-STAB, P-CRASH, P-NET | 30 | ACTIVE |
| SYSTEST-REG-005 | NPLAN-watchdog | sysplan-nplan-watchdog SYS-007 | R138 | Security boundary (TamperProof on new stwatchdog service — new attack surface) | TamperProof bypass attempts on stwatchdog: kill, sc stop/delete, file delete, registry delete; all must fail | P-STAB | 30 | ACTIVE |
| SYSTEST-REG-006 | NPLAN-watchdog | sysplan-nplan-watchdog SYS-002 | R138 | IMF-919-related + PR #7930 explicit regression guard | Kill stAgentSvc 100x; verify watchdog uses IsServiceStopped() not !IsServiceRunning(); zero PENDING-state restarts | P-STAB, P-DEAD | 30 | ACTIVE |
| SYSTEST-REG-007 | NPLAN-4534 | sysplan-nplan-4534 SYS-001 | R138 | IMF-1073 Critical + IMF-1136 High (5 of 6 config IMFs trace here) | Config download integrity via REST APIv2; verify all artifacts + digest valid; nsconfig.json complete | P-STAB, P-NET | 30 | ACTIVE |
| SYSTEST-REG-008 | NPLAN-4534 | sysplan-nplan-4534 SYS-008 | R138 | IMF-1043 Critical (NPA tunnel break) | Push config v=N+1 via APIv2 with no tunnel-affecting delta; SWG + NPA must NOT spuriously disconnect | P-STAB, P-NET | 30 | ACTIVE |
| SYSTEST-REG-009 | NPLAN-4534 | sysplan-nplan-4534 SYS-003 | R139 | IMF-1073 Critical + permanent-broken-state (clients disabled on transient 5xx mishandled) | APIv2 HTTP error code handling: each error class (200, 304, 4xx, 5xx, timeout) drives correct backoff/retry; nsconfig.json never corrupted | P-STAB, P-NET | 30 | ACTIVE |
| SYSTEST-REG-010 | NPLAN-4534 | sysplan-nplan-4534 SYS-006 | R139 | IMF-1116 High + ENG-624953 Day-1 + multi-user delivery | Per-VDI-session config-ready callback fires for each sessId; per-user steering applies independently; FC isolation maintained | P-STAB, P-NET | 30 | ACTIVE |
| SYSTEST-REG-011 | NPLAN-4534 | sysplan-nplan-4534 SYS-009 | R139 | IMF-1116 High + permanent-broken-state (cold start failure = unenrollable client) | Cold-start initial config download via APIv2 (fresh enroll → empty cache → first sync); IMF-1116 reproduction surface for new users | P-STAB, P-NET | 30 | ACTIVE |

---

## Retired Cases (Audit History)

| Case ID | Source NPLAN | Source SYS-XX | Added | Retired | Retirement Reason |
|---|---|---|---|---|---|

(No retirements yet.)

---

## Notes for Reviewers

- Active row count caps the per-release execution budget. At 8 cases/release × 30 reps × 5 min/rep = 20 hours added per release (cumulative).
- After 6 releases (~48 cases × 30 reps = 1440 runs ≈ 120 hours), consider whether some cases can be retired even if they still pass.
- Cross-reference each Source SYS-XX with the originating sysplan in `systest_plans/sysplan-nplan-XXXX.md` to see the full case body, mermaid, and evidence requirements.
