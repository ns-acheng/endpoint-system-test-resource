-- MIRROR NOTICE (added for endpoint-system-test-resource): point-in-time copy.
-- Canonical source (private repo): claude-resource/grs_tools/system_test_db/schema.sql
-- Do not edit this copy directly -- changes land upstream first, then get re-synced here.

-- ============================================================================
-- GRS SystemTest results DB — schema
-- Target: PostgreSQL 14+.  db/user/host live outside the repo (see the DB VM
-- connection note in team memory / your local env, NOT here — no credentials
-- belong in source).
--
-- Source artifacts (all ALREADY produced by the suite — the ingest tool reads
-- them, nothing new is collected):
--   system_test_artifacts/iterations_<run_id>.ndjson  -> run / test / iteration records
--   resource_samples/<test>.jsonl                     -> per-sample per-process
--                                                        cpu/mem/handles + dump paths
--   health-gate summary                               -> recomputed by ingest from the jsonl
--                                                        (it is only logged, never persisted)
--
-- Grain:
--   release  1─< test_run (one Jenkins build = one pytest session)
--                  1─< test_result (one test case)
--                         1─< iteration_result  (one --iterations rep: PASS/FAIL + duration)
--                         1─< process_summary   (per-process breakthrough: peak / sustained / growth)
--                         1─< gate_violation    (parsed: which gate failed the case)
--                         1─< dump              (crash / BSOD / live dump)
--   product_issue  (manual triage: ticket + summary, linked to the first sighting)
--   gate_ticket    (manual triage: ticket <-> one gate breakdown row, keyed by
--                   job+build+test so it survives a rebuild)
--   db_growth      (weekly size snapshot)
--
-- Idempotent ingest: test_run.run_id is UNIQUE — re-ingesting a build is a no-op
-- (or an upsert). Re-running this file is NOT idempotent (plain CREATE TABLE);
-- use apply_schema (guarded) for that.
-- ============================================================================

-- ─────────────────────────── release (compare axis) ────────────────────────
CREATE TABLE release (
    id          SERIAL PRIMARY KEY,
    label       TEXT NOT NULL UNIQUE,          -- 'release-140' / 'R140'
    ordinal     INTEGER NOT NULL,              -- 140 (numeric sort + compare)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─────────────────── one Jenkins build = one pytest session ─────────────────
-- Maps 1:1 to the NDJSON "run" record.
CREATE TABLE test_run (
    id                   BIGSERIAL PRIMARY KEY,
    run_id               TEXT NOT NULL UNIQUE,   -- ndjson run_id / BUILD_TAG (dedupe key)
    jenkins_job          TEXT,                   -- job_name  e.g. 'GRS-SYSTEMTEST-REG'
    jenkins_build        INTEGER,                -- build_number  e.g. 84
    build_url            TEXT,                   -- derived job+build
    node_name            TEXT,                   -- ndjson node_name
    vm_ip                TEXT,                   -- ndjson vm_ip
    release_id           INTEGER REFERENCES release(id),
    product_version      TEXT,                   -- ndjson test.client_version (installed)
    previous_release     TEXT,                   -- ndjson previous_release (upgrade baseline)
    platform             TEXT,                   -- ndjson test_platform
    tenant               TEXT,                   -- ndjson tenant
    dc                   TEXT,                   -- ndjson dc
    -- Bitness, two columns on purpose: the CLI flag is the EXPECTED arch and can
    -- disagree with what actually landed (auto-upgrade saves STAgent.msi for both
    -- arches), so the install-PATH reading is the one to trust. NULL on runs
    -- recorded before the fields existed / when the probe could not read them.
    is_64_bit_cli        BOOLEAN,                -- ndjson run.is_64_bit (--is_64_bit)
    is_64_bit_installed  BOOLEAN,                -- ndjson test.installed_is_64bit (install path = ground truth)
    iterations_cli       INTEGER,                -- ndjson iterations_cli (--iterations)
    started_at           TIMESTAMPTZ,            -- ndjson started_at (epoch)
    finished_at          TIMESTAMPTZ,            -- max iteration end (derived)
    n_pass               INTEGER,                -- session rollup (derived from test rows)
    n_fail               INTEGER,
    n_skip               INTEGER,
    raw_ndjson_path      TEXT,                   -- provenance
    ingested_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_run_release ON test_run(release_id);
CREATE INDEX idx_run_started ON test_run(started_at);

-- ─────────────────────── one test case within a run ────────────────────────
-- Maps to the NDJSON "test" record.
CREATE TABLE test_result (
    id                 BIGSERIAL PRIMARY KEY,
    run_id             BIGINT NOT NULL REFERENCES test_run(id) ON DELETE CASCADE,
    test_name          TEXT NOT NULL,     -- ndjson test_case
    suite              TEXT,              -- derived: STRESS/IPC/POWER/UPGRADE
    verdict            TEXT NOT NULL,     -- ndjson test_status: PASS|FAIL (SKIP/ERROR if seen)
    started_at         TIMESTAMPTZ,
    duration_s         NUMERIC,           -- sum of iteration durations (derived)
    iterations_target  INTEGER,           -- ndjson iterations_target
    iterations_total   INTEGER,           -- ndjson iterations_total (executed)
    iterations_passed  INTEGER,           -- ndjson iterations_passed
    iterations_failed  INTEGER,           -- ndjson iterations_failed
    iterations_undone  INTEGER GENERATED ALWAYS AS
                         (GREATEST(COALESCE(iterations_target,0) - COALESCE(iterations_total,0), 0)) STORED,
    first_failed_iter  INTEGER,           -- ndjson first_failed_iter
    fail_reason        TEXT,              -- first failing iteration's fail_reason
    failing_gate       TEXT,             -- which gate failed: cpu_sustained|mem_growth|
                                          --   mem_ceiling|handles|crash_dump|bsod|reboot|null
    has_dump           BOOLEAN NOT NULL DEFAULT FALSE,  -- any crash/bsod/live dump seen
    gate_violated      BOOLEAN NOT NULL DEFAULT FALSE,  -- health gate raised >=1 violation
    is_product_issue   BOOLEAN NOT NULL DEFAULT FALSE,  -- set when linked in product_issue
    UNIQUE (run_id, test_name)
);
CREATE INDEX idx_res_run     ON test_result(run_id);
CREATE INDEX idx_res_name    ON test_result(test_name);
CREATE INDEX idx_res_verdict ON test_result(verdict);
CREATE INDEX idx_res_dump    ON test_result(has_dump)      WHERE has_dump;
CREATE INDEX idx_res_gate    ON test_result(gate_violated) WHERE gate_violated;

-- ─────────────────────── per --iterations rep ──────────────────────────────
-- Maps to the NDJSON "iteration" record. This IS the red->green->red sequence
-- AND the "passed through iter N, failed at iter N+1, with durations" answer.
CREATE TABLE iteration_result (
    id             BIGSERIAL PRIMARY KEY,
    test_result_id BIGINT NOT NULL REFERENCES test_result(id) ON DELETE CASCADE,
    iter_index     INTEGER NOT NULL,   -- ndjson iter_index (1-based)
    iter_total     INTEGER,            -- ndjson iter_total
    status         TEXT NOT NULL,      -- ndjson status: PASS|FAIL
    duration_s     NUMERIC,            -- ndjson duration_s
    fail_reason    TEXT,               -- ndjson fail_reason
    UNIQUE (test_result_id, iter_index)
);

-- ─────────── per-process resource SUMMARY (the "breakthrough" table) ────────
-- Recomputed by ingest from resource_samples jsonl (summarize_samples()).
-- This is what powers per-case baselines + release compare. Small: 3 rows/test.
CREATE TABLE process_summary (
    id              BIGSERIAL PRIMARY KEY,
    test_result_id  BIGINT NOT NULL REFERENCES test_result(id) ON DELETE CASCADE,
    process_name    TEXT NOT NULL,     -- stAgentSvc.exe / stAgentUI.exe / stAgentSvcMon.exe
    cpu_max_pct     NUMERIC,           -- summary cpu_max (peak %)
    cpu_sustained_s NUMERIC,           -- seconds CPU held >= sustain threshold
    cpu_threshold   NUMERIC,           -- the sustain threshold in force (e.g. 30 or per-marker)
    mem_baseline_mb NUMERIC,           -- summary mem_baseline
    mem_max_mb      NUMERIC,           -- summary mem_max
    mem_growth_mb   NUMERIC,           -- summary mem_growth
    handles_max     INTEGER,           -- summary handles_max
    alive_pct       NUMERIC,           -- summary alive_pct
    sample_count    INTEGER,           -- summary sample_count (for confidence)
    UNIQUE (test_result_id, process_name)
);
CREATE INDEX idx_psum_test ON process_summary(test_result_id);
CREATE INDEX idx_psum_proc ON process_summary(process_name);

-- ─────────────────── structured gate violations (parsed) ────────────────────
-- Parsed from check_thresholds() list[str]. One row per violation message.
CREATE TABLE gate_violation (
    id              BIGSERIAL PRIMARY KEY,
    test_result_id  BIGINT NOT NULL REFERENCES test_result(id) ON DELETE CASCADE,
    violation_type  TEXT NOT NULL,   -- cpu_sustained|mem_growth|mem_ceiling|handles|
                                     --   crash_dump|bsod_dump|bsod_event|reboot
    process_name    TEXT,            -- null for reboot/bsod
    observed        NUMERIC,         -- observed value (e.g. sustained 40, handles 1620)
    threshold       NUMERIC,         -- limit crossed
    detail          TEXT             -- full original message string
);
CREATE INDEX idx_gv_test ON gate_violation(test_result_id);
CREATE INDEX idx_gv_type ON gate_violation(violation_type);

-- ─────────────────────── crash / BSOD / live dumps ─────────────────────────
CREATE TABLE dump (
    id              BIGSERIAL PRIMARY KEY,
    test_result_id  BIGINT NOT NULL REFERENCES test_result(id) ON DELETE CASCADE,
    dump_type       TEXT NOT NULL,   -- crash | bsod | live | bsod_event
    process_name    TEXT,            -- from live-dump filename tag / crash path; null for bsod
    signal_tag      TEXT,            -- live-dump: cpu-sustained-pre-stop / kill-fail-pre-stop
    file_name       TEXT,
    file_path       TEXT,            -- VM/artifact path
    size_bytes      BIGINT,          -- bsod_dumps.size
    event_id        INTEGER,         -- bsod_event: 1001 / 41 / 6008
    captured_at     TIMESTAMPTZ,     -- mtime / ts
    note            TEXT             -- bsod_event detail
);
CREATE INDEX idx_dump_test ON dump(test_result_id);
CREATE INDEX idx_dump_type ON dump(dump_type);

-- ─────────────────── product issue (manual triage) ─────────────────────────
-- A real product defect surfaced by testing. Linked to the FIRST test that saw it.
CREATE TABLE product_issue (
    id                    SERIAL PRIMARY KEY,
    ticket_id             TEXT NOT NULL,     -- JIRA/ticket
    summary               TEXT NOT NULL,
    first_test_result_id  BIGINT REFERENCES test_result(id),  -- first sighting
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (ticket_id)
);

-- ────────────── gate-violation triage ticket (manual, durable keys) ─────────
-- Annotates one Health-gate breakdown row with the bug it was filed as
-- (`db.py issue set`), so the report can show a ticket column next to the
-- violation instead of the mapping living in someone's head.
--
-- Keyed by (jenkins_job, jenkins_build, test_name) ON PURPOSE, not by
-- test_result.id: every other table is rebuildable from the artifacts
-- (`harvest.py ingest-all` / rebuild_db.py) and a rebuild hands out NEW
-- surrogate ids, so a link on test_result.id would silently point at the wrong
-- row — or nothing — after the next rebuild. Like product_issue, this table has
-- NO artifact source, so `db.py backup` is the only thing that can restore it.
-- test_name NULL = the ticket covers every case in that build.
-- migrate-block: gate_ticket
CREATE TABLE IF NOT EXISTS gate_ticket (
    id             SERIAL PRIMARY KEY,
    ticket_id      TEXT NOT NULL,        -- 'ENG-1180143'
    jenkins_job    TEXT NOT NULL,        -- test_run.jenkins_job, stored verbatim
    jenkins_build  INTEGER NOT NULL,     -- test_run.jenkins_build
    test_name      TEXT,                 -- NULL = whole build
    note           TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Two PARTIAL unique indexes, not one plain UNIQUE: on PG 14 a plain unique
-- constraint treats NULL test_name values as distinct, so the whole-build form
-- could be inserted twice.
CREATE UNIQUE INDEX IF NOT EXISTS uq_gt_case
    ON gate_ticket(ticket_id, jenkins_job, jenkins_build, test_name)
    WHERE test_name IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_gt_build
    ON gate_ticket(ticket_id, jenkins_job, jenkins_build)
    WHERE test_name IS NULL;
CREATE INDEX IF NOT EXISTS idx_gt_run ON gate_ticket(jenkins_job, jenkins_build);
-- end migrate-block

-- ─────────────── raw per-sample time-series (10s ticks per process) ─────────
-- One row per (sample tick × process). This is the source for arbitrary-threshold
-- queries the summary cannot answer, e.g. "how many seconds did CPU stay > 50
-- (below the 120 gate) in R140 stress_05?" — you need the raw series for that.
-- ~585 B/sample in jsonl becomes ~3 rows/tick here; a 30-min run ≈ 180 ticks ≈
-- 540 rows. 2-year worst case a few million rows / ~1-2 GB with index — trivial
-- for PG on the 98 GB volume. BRIN keeps range scans cheap on an append-only,
-- naturally time-ordered table.
CREATE TABLE resource_sample (
    id                BIGSERIAL PRIMARY KEY,
    test_result_id    BIGINT NOT NULL REFERENCES test_result(id) ON DELETE CASCADE,
    process_name      TEXT NOT NULL,     -- stAgentSvc.exe / stAgentUI.exe / stAgentSvcMon.exe
    elapsed_s         INTEGER,           -- seconds since the test's first sample (test-relative)
    sampled_at        TIMESTAMPTZ,       -- absolute wall clock (ts)
    boot_id           TEXT,              -- boot segment (mem growth / sustained reset on reboot)
    cpu_pct           NUMERIC,           -- Task-Manager-parity total-capacity % (may be null)
    cpu_pct_per_core  NUMERIC,           -- per-core (100 = 1 core); the gated unit (may be null)
    mem_mb            NUMERIC,
    handles           INTEGER,
    alive             BOOLEAN,
    -- Machine-level NIC throughput for the tick (tick-level, denormalized onto
    -- each process row). Collection-only telemetry — NO gate reads these (owner
    -- decision 2026-07-24: gather a few weeks, then set a data-driven bar).
    net_mbps_in       NUMERIC,           -- MB/s received over the tick interval (null on first tick)
    net_mbps_out      NUMERIC,           -- MB/s sent over the tick interval (null on first tick)
    load_phase        TEXT               -- 'under_load' | 'idle' | null — isolates flood samples
);
-- BRIN on (test_result_id, elapsed_s): rows are inserted in test+time order, so
-- the block-range index is tiny and fast for "one test's timeline" scans.
CREATE INDEX idx_rs_brin ON resource_sample USING BRIN (test_result_id, elapsed_s);
CREATE INDEX idx_rs_proc ON resource_sample (test_result_id, process_name);

-- ─────────────────────── DB growth tracking (weekly tool) ───────────────────
CREATE TABLE db_growth (
    id            SERIAL PRIMARY KEY,
    captured_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    db_size_bytes BIGINT,
    n_runs        BIGINT,
    n_test_result BIGINT,
    n_iteration   BIGINT,
    n_psummary    BIGINT
);
