"""Harvest the ingest-relevant slice of Jenkins SystemTest artifacts into a
small, durable cold store — so the DB can be rebuilt LATER, after the artifacts
have rotated out of Jenkins.

WHY THIS EXISTS
---------------
The results DB is *derived* data: every table is recomputed from two artifact
kinds the suite already produces.

    iterations_<run_id>.ndjson              -> test_run / test_result / iteration_result
    **/<test_case>/resource_samples.jsonl   -> process_summary / gate_violation / dump /
    **/resource_samples/<test_case>.jsonl      resource_sample

So losing the DB is recoverable — but ONLY while Jenkins still has the builds.
Retention is 7 days (REG, REG-02) / 14 days (LOCALTEST), so the recovery window
closes build-by-build every day. This tool decouples "keep the inputs" (urgent,
must happen before rotation) from "stand up PostgreSQL and ingest" (not urgent).

The backup zip is ~46 MB per build but 99.96% of it is STAgent_backup.msi, certutil
DLLs and debug logs that ingest never opens. Keeping only the two file kinds above,
gzipped, is ~2-25 KB per build — verified byte-identical ingest output vs the full
unzipped tree (dry-run parity check, REG-02 build 48).

USAGE
-----
    python harvest.py pull reg reg2 localtest        # download + slim + gzip into the store
    python harvest.py pull reg --limit 5             # newest 5 CPU-valid builds of one job
    python harvest.py list                           # what's in the store
    python harvest.py verify                         # re-parse every stored build (no DB needed)
    python harvest.py ingest-all                     # replay the store into PostgreSQL

`pull` is incremental and idempotent: a build already in the store is skipped
unless --replace. Safe to re-run daily; that is the intended mode (a scheduled
task keeps the window from ever closing again).

Store layout (one dir per build, mirroring what ingest expects):

    <store>/<job_alias>/<build>/iterations_<run_id>.ndjson.gz
    <store>/<job_alias>/<build>/<test_case>/resource_samples.jsonl.gz
    <store>/<job_alias>/<build>/harvest.json          provenance: url, result, timestamps

Default store: claude-resource/systest_db_store/  — inside a git repo on purpose,
so `git push` gives offsite replication of the recovery inputs for free. It is
small enough for that (see `list --bytes`).
"""

import os as _nw_os
import sys as _nw_sys

_nw_sys.path.insert(0, _nw_os.path.dirname(_nw_os.path.dirname(_nw_os.path.abspath(__file__))))
import nowindow  # noqa: E402,F401  (pythonw: no console windows from child processes)

import argparse
import glob
import gzip

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

# Console is cp1252; force UTF-8. Use reconfigure(), NOT a fresh TextIOWrapper:
# db.py (imported below) wraps sys.stdout.buffer itself, which would leave our
# wrapper unreferenced -> GC -> the real stdout buffer gets closed under us.
# sys.__stdout__ keeps the reconfigured object alive, so this composes safely.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Reuse db.py's job aliases + CPU-validity rule rather than restating them: one
# definition of "which builds count" for both the live poller and the harvester.
import db as _db  # noqa: E402

DEFAULT_STORE = os.path.abspath(os.path.join(_HERE, "..", "..", "systest_db_store"))

# Filenames ingest.py actually reads (ingest._load_samples + load_build globs).
# Anything else in the backup zip is irrelevant to the DB.
_SAMPLE_BASENAME = "resource_samples.jsonl"


def _store_dir(a):
    return os.path.abspath(a.store or os.environ.get("GRS_DB_STORE") or DEFAULT_STORE)


# A build whose artifacts carry no iterations_*.ndjson can NEVER be replayed into
# the DB, and that verdict is permanent (a finished build's artifacts never grow).
# Until 2026-08-12 the fact was printed and thrown away: the daily driver
# re-downloaded the same 24 dead zips every run, and `db.py lanes` counted them as
# a harvest gap forever ("BEHIND 23" when the true gap was 6). An alarm that
# over-counts by 4x gets ignored, which is the whole failure class this toolchain
# exists to prevent. So remember it — one small file per lane, deliberately NOT a
# build directory, so cmd_list / verify / ingest-all keep seeing only real builds.
_NODATA_BASENAME = "no_data.json"


def _nodata_path(store, job):
    return os.path.join(store, job, _NODATA_BASENAME)


SAME_BUILD_TOL_S = 300  # the same tolerance ingest.py uses to tell a reused build number from a re-ingest of the same run


def _same_build(stored_ts, jenkins_ts):
    """True when the store slot holds the build Jenkins now has under this number. Both stamps are epoch SECONDS (what
    _list_builds_full returns and what harvest.json stores as build_ts). A missing stamp counts as a different build."""
    try:
        return abs(float(stored_ts) - float(jenkins_ts)) <= SAME_BUILD_TOL_S
    except (TypeError, ValueError):
        return False


def _supersede(store, job, num, stored_ts):
    """Move a slot that holds ANOTHER execution out of the way instead of overwriting it: Jenkins keeps only 100 builds, so
    for old numbers the store copy is the only one left. Same idea as ingest.py's [reclaim], which renames the old DB row."""
    src = os.path.join(store, job, str(num))
    dst_root = os.path.join(store, job, "_superseded")
    os.makedirs(dst_root, exist_ok=True)
    tag = str(int(float(stored_ts))) if stored_ts else "unknown"
    dst = os.path.join(dst_root, f"{num}-{tag}")
    k = 1
    while os.path.exists(dst):
        k += 1
        dst = os.path.join(dst_root, f"{num}-{tag}-{k}")
    shutil.move(src, dst)
    return dst


def _load_nodata(store, job):
    try:
        with open(_nodata_path(store, job), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_nodata(store, job, data):
    p = _nodata_path(store, job)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
    except OSError:
        pass


def _wanted_zip_members(zf):
    """Zip entries ingest would read: per-test resource_samples.jsonl (folder
    layout) or resource_samples/<test>.jsonl (flat layout)."""
    out = []
    for i in zf.infolist():
        fn = i.filename
        if fn.endswith("/") or i.is_dir():
            continue
        base = fn.rsplit("/", 1)[-1]
        if base == _SAMPLE_BASENAME:
            out.append(i)
        elif fn.endswith(".jsonl") and "/resource_samples/" in fn:
            out.append(i)
    return out


def _gz_write(data, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # mtime=0 keeps the gzip header byte-stable so re-harvesting an unchanged
    # build does not churn git history.
    with gzip.GzipFile(path, "wb", compresslevel=9, mtime=0) as f:
        f.write(data)
    return len(data)


def _slim_build(download_dir, out_dir):
    """Copy the ingest-relevant files out of a downloaded build into out_dir,
    gzipped. Returns (n_ndjson, n_jsonl, raw_bytes, stored_bytes)."""
    n_nd = n_js = raw = 0
    for nd in glob.glob(
        os.path.join(download_dir, "**", "iterations_*.ndjson"), recursive=True
    ):
        with open(nd, "rb") as f:
            data = f.read()
        raw += _gz_write(data, os.path.join(out_dir, os.path.basename(nd) + ".gz"))
        n_nd += 1
    for z in glob.glob(
        os.path.join(download_dir, "**", "backup_nsc_*.zip"), recursive=True
    ):
        try:
            with zipfile.ZipFile(z) as zf:
                for i in _wanted_zip_members(zf):
                    # Keep the last two path components (<test_case>/<file>) so
                    # ingest's test-folder matching still works; drop the rest.
                    parts = i.filename.split("/")
                    rel = os.path.join(*parts[-2:]) if len(parts) >= 2 else parts[-1]
                    data = zf.read(i)
                    raw += _gz_write(data, os.path.join(out_dir, rel + ".gz"))
                    n_js += 1
        except zipfile.BadZipFile:
            print(f"    [warn] bad zip, skipped: {os.path.basename(z)}")
    # Some jobs archive the jsonl loose (not inside the backup zip).
    for js in glob.glob(os.path.join(download_dir, "**", "*.jsonl"), recursive=True):
        base = os.path.basename(js)
        if base != _SAMPLE_BASENAME and "resource_samples" not in js:
            continue
        rel = os.path.join(os.path.basename(os.path.dirname(js)), base)
        tgt = os.path.join(out_dir, rel + ".gz")
        if os.path.exists(tgt):
            continue
        with open(js, "rb") as f:
            data = f.read()
        raw += _gz_write(data, tgt)
        n_js += 1
    stored = sum(
        os.path.getsize(p)
        for p in glob.glob(os.path.join(out_dir, "**", "*.gz"), recursive=True)
    )
    return n_nd, n_js, raw, stored


def _inflate_to(build_store, work_dir):
    """Reverse of _slim_build: gunzip the store back into a dir ingest can read."""
    for gz in glob.glob(os.path.join(build_store, "**", "*.gz"), recursive=True):
        rel = os.path.relpath(gz, build_store)[: -len(".gz")]
        tgt = os.path.join(work_dir, rel)
        os.makedirs(os.path.dirname(tgt), exist_ok=True)
        with gzip.open(gz, "rb") as s, open(tgt, "wb") as d:
            shutil.copyfileobj(s, d)
    return work_dir


# ─────────────────────────── subcommands ───────────────────────────


def cmd_pull(a):
    store = _store_dir(a)
    gj = os.path.join(os.path.expanduser("~"), "grs_tools", "grs_jenkins.py")
    if not os.path.isfile(gj):
        gj = os.path.join(_HERE, "..", "grs_jenkins.py")

    print(f"[store] {store}")
    grand_new = grand_skip = grand_fail = grand_nodata = grand_stale = 0
    grand_bytes = 0
    for job in a.jobs:
        try:
            builds = _db._list_builds_full(job)
        except Exception as e:
            print(f"=== {job}: CANNOT LIST BUILDS: {type(e).__name__}: {e}")
            grand_fail += 1
            continue
        finished = [b for b in builds if not b["building"]]
        valid = finished if a.all else [b for b in finished if _db._cpu_valid(b)]
        valid.sort(key=lambda x: -x["number"])
        if a.limit:
            valid = valid[: a.limit]
        running = [b["number"] for b in builds if b["building"]]
        print(
            f"\n=== {job}: {len(valid)} candidate build(s)"
            + (f"  (running, skipped: {running})" if running else "")
            + " ==="
        )
        nodata = _load_nodata(store, job)
        for b in valid:
            num = b["number"]
            out_dir = os.path.join(store, job, str(num))
            marker = os.path.join(out_dir, "harvest.json")
            if os.path.isfile(marker) and not a.replace:
                try:
                    with open(marker, encoding="utf-8") as f:
                        stored_ts = json.load(f).get("build_ts")
                except (OSError, ValueError):
                    stored_ts = None
                if _same_build(stored_ts, b.get("ts")):
                    grand_skip += 1
                    continue
                # The slot holds ANOTHER execution (Jenkins reused this build number after a counter reset): park it, then
                # harvest the real build. The staleness rule decides before the nodata cache below.
                parked = _supersede(store, job, num, stored_ts)
                grand_stale += 1
                print(f"  #{num:<4} slot held a different build (build_ts {stored_ts}); moved to {parked}")
            entry = nodata.get(str(num))
            if entry is not None and not a.replace:
                if _same_build(entry.get("build_ts"), b.get("ts")):
                    # Known-unharvestable: skip the download entirely. --replace
                    # re-checks, which is the only way this cache can be wrong.
                    grand_nodata += 1
                    continue
                del nodata[str(num)]  # cached for another build that had this number
            work = tempfile.mkdtemp(prefix=f"harvest_{job}_{num}_")
            try:
                r = subprocess.run(
                    [sys.executable, gj, "artifacts", job, str(num), work],
                    capture_output=True,
                    text=True,
                )
                if r.returncode != 0:
                    print(
                        f"  #{num:<4} DOWNLOAD FAILED: {(r.stderr or r.stdout).strip()[:120]}"
                    )
                    grand_fail += 1
                    continue
                if os.path.isdir(out_dir):
                    shutil.rmtree(out_dir, ignore_errors=True)
                n_nd, n_js, raw, stored = _slim_build(work, out_dir)
                if n_nd == 0:
                    # No ndjson => ingest would raise NoData. Don't keep a dir that
                    # looks harvested but can never be replayed.
                    #
                    # Counted SEPARATELY from already-stored: "we already have it"
                    # and "this build can never be replayed" are opposite facts.
                    # Lumping them together made a job that produces NO ndjson at
                    # all (LOCAL2 builds 1-7) report `already-stored=7`, i.e. it
                    # looked fully covered when nothing had been captured.
                    shutil.rmtree(out_dir, ignore_errors=True)
                    nodata[str(num)] = {
                        "result": b.get("result"),
                        "build_ts": b.get("ts"),
                        "checked_at": time.time(),
                    }
                    _save_nodata(store, job, nodata)
                    print(
                        f"  #{num:<4} no iterations_*.ndjson — nothing to harvest "
                        f"(recorded, won't re-download)"
                    )
                    grand_nodata += 1
                    continue
                with open(marker, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "job": job,
                            "build": num,
                            "result": b.get("result"),
                            "build_ts": b.get("ts"),
                            "harvested_at": time.time(),
                            "n_ndjson": n_nd,
                            "n_jsonl": n_js,
                            "raw_bytes": raw,
                            "stored_bytes": stored,
                        },
                        f,
                        indent=2,
                    )
                grand_new += 1
                grand_bytes += stored
                print(
                    f"  #{num:<4} {str(b.get('result')):8} ndjson={n_nd} jsonl={n_js} "
                    f"raw={raw / 1024:.0f}KB stored={stored / 1024:.0f}KB"
                )
            finally:
                shutil.rmtree(work, ignore_errors=True)
    nodata = f"  no-ndjson={grand_nodata}" if grand_nodata else ""
    stale = f"  superseded-slots={grand_stale}" if grand_stale else ""
    print(
        f"\n[pull] new={grand_new}  already-stored={grand_skip}  failed={grand_fail}"
        f"{nodata}{stale}  added={grand_bytes / 1024 / 1024:.2f} MB"
    )
    if grand_nodata and not grand_new and not grand_skip:
        # Every candidate lacked an ndjson: the job runs but produces nothing
        # this tool can replay. Loud, because a silent 0 here reads as "up to
        # date" (see the LOCAL2 case in the comment above).
        print(
            f"[!] {grand_nodata} candidate build(s) had NO iterations_*.ndjson and NOTHING "
            f"was harvested -- the job is not emitting the per-iteration recorder file, "
            f"so none of its builds can reach the DB."
        )
    return 1 if grand_fail and not grand_new else 0


def cmd_list(a):
    store = _store_dir(a)
    if not os.path.isdir(store):
        print(f"[store] {store} — DOES NOT EXIST (nothing harvested yet)")
        return 1
    total = n = 0
    print(f"[store] {store}")
    for job in sorted(os.listdir(store)):
        jd = os.path.join(store, job)
        if not os.path.isdir(jd):
            continue
        builds = sorted((int(x) for x in os.listdir(jd) if x.isdigit()), reverse=True)
        sz = sum(
            os.path.getsize(p)
            for p in glob.glob(os.path.join(jd, "**", "*"), recursive=True)
            if os.path.isfile(p)
        )
        total += sz
        n += len(builds)
        rng = f"{builds[-1]}..{builds[0]}" if builds else "-"
        print(
            f"  {job:<28} builds={len(builds):3}  range={rng:<14} {sz / 1024 / 1024:.2f} MB"
        )
        if a.verbose:
            for b in builds:
                m = os.path.join(jd, str(b), "harvest.json")
                info = json.load(open(m, encoding="utf-8")) if os.path.isfile(m) else {}
                print(
                    f"      #{b:<5} {str(info.get('result')):8} jsonl={info.get('n_jsonl')}"
                )
    print(f"[total] {n} build(s)  {total / 1024 / 1024:.2f} MB")
    return 0


def _iter_stored(store):
    for job in sorted(os.listdir(store)):
        jd = os.path.join(store, job)
        if not os.path.isdir(jd):
            continue
        for b in sorted((int(x) for x in os.listdir(jd) if x.isdigit())):
            yield job, b, os.path.join(jd, str(b))


def cmd_verify(a):
    """Re-parse every stored build through ingest's own loader (--dry-run
    equivalent). Proves the store is replayable WITHOUT a database."""
    store = _store_dir(a)
    if not os.path.isdir(store):
        print(f"[store] {store} — DOES NOT EXIST")
        return 1
    import ingest

    ok = bad = stale = 0
    for job, num, bd in _iter_stored(store):
        work = tempfile.mkdtemp(prefix=f"vfy_{job}_{num}_")
        try:
            _inflate_to(bd, work)
            model = ingest.load_build(work, expect_build=num)
            tests = model["tests"]
            # raw_samples is the flattened (process x tick) list ingest writes to
            # resource_sample. Count THAT, not a guessed key name: a zero here
            # means the jsonl did not survive the harvest, which is the exact
            # failure this command exists to catch.
            samples = sum(len(t.get("raw_samples") or []) for t in tests)
            procs = sum(len(t.get("process_summary") or []) for t in tests)
            flag = "" if samples else "  [!] no resource samples"
            print(
                f"  {job}/#{num:<5} OK  run_id={model['run']['run_id']:<45} "
                f"tests={len(tests):2} procs={procs:2} samples={samples}{flag}"
            )
            ok += 1
        except ingest.NoData as e:
            # This build carries no ndjson of its OWN (died pre-test; the zip held
            # a neighbour's). Nothing to replay, but the store is not corrupt —
            # counting it as broken would make `verify` permanently red.
            print(f"  {job}/#{num:<5} --  no own data: {e}")
            stale += 1
        except Exception as e:
            print(f"  {job}/#{num:<5} FAIL {type(e).__name__}: {e}")
            bad += 1
        finally:
            shutil.rmtree(work, ignore_errors=True)
    print(f"\n[verify] replayable={ok}  no-own-data={stale}  broken={bad}")
    return 0 if bad == 0 else 1


def cmd_ingest_all(a):
    """Replay the whole store into PostgreSQL. Run this once the new DB VM is up."""
    store = _store_dir(a)
    if not os.path.isdir(store):
        print(f"[store] {store} — DOES NOT EXIST")
        return 1
    import ingest

    n_ok = n_skip = n_fail = n_stale = 0
    for job, num, bd in _iter_stored(store):
        work = tempfile.mkdtemp(prefix=f"ing_{job}_{num}_")
        try:
            _inflate_to(bd, work)
            # The store dir IS keyed by build number, so we always know which
            # build this is — pass it so a stored stale artifact (a build whose
            # zip only carried an earlier build's ndjson) is reported as no-data
            # rather than ingested under the wrong build number.
            argv = [work, "--expect-build", str(num)]
            if a.release:
                argv += ["--release", a.release]
            if a.replace:
                argv += ["--replace"]
            rc = ingest.main(argv)
            if rc == 0:
                n_ok += 1
            elif rc == 5:
                n_skip += 1
            elif rc == 3:
                # no data of its own (stale-artifact-only dir) — expected for
                # builds that died pre-test; not a failure of the replay.
                n_stale += 1
            else:
                n_fail += 1
                print(f"  {job}/#{num} rc={rc}")
        except Exception as e:
            n_fail += 1
            print(f"  {job}/#{num} FAIL {type(e).__name__}: {e}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
    print(
        f"\n[ingest-all] ingested={n_ok}  already-in-db={n_skip}  "
        f"stale-artifact-only={n_stale}  failed={n_fail}"
    )
    return 0 if n_fail == 0 else 1


def main(argv):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--store", help=f"cold-store dir (default {DEFAULT_STORE}, or $GRS_DB_STORE)"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser(
        "pull",
        help="download + slim + gzip Jenkins builds into the store (incremental)",
    )
    pl.add_argument(
        "jobs", nargs="+", help="job aliases (reg reg2 localtest) or full paths"
    )
    pl.add_argument(
        "--limit", type=int, help="only the newest N candidate builds per job"
    )
    pl.add_argument(
        "--replace", action="store_true", help="re-harvest builds already in the store"
    )
    pl.add_argument(
        "--all", action="store_true", help="include pre-PR263 builds (old CPU unit)"
    )
    pl.set_defaults(func=cmd_pull)

    ls = sub.add_parser("list", help="what is in the store")
    ls.add_argument("-v", "--verbose", action="store_true", help="per-build detail")
    ls.set_defaults(func=cmd_list)

    sub.add_parser(
        "verify", help="re-parse every stored build (no DB required)"
    ).set_defaults(func=cmd_verify)

    ia = sub.add_parser(
        "ingest-all", help="replay the store into PostgreSQL (needs a live DB)"
    )
    ia.add_argument("--release", help="release label to tag every ingested run")
    ia.add_argument("--replace", action="store_true")
    ia.set_defaults(func=cmd_ingest_all)

    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
