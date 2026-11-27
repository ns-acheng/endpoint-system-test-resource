"""The roster of processes the SystemTest sampler is configured to monitor.

The report shows every process of this roster in its own table, also the ones with no data in the release, so that everybody
can see what is monitored. The report server has no GRS checkout, so the roster is a file in this repo
(monitored_processes.json) that `sync` writes from the suite's own constants; `check` says whether the file still equals them.

    python monitored.py sync  [--repo C:/git/nsclient_golden_regression_suite] [--ref origin/main]
    python monitored.py check [--repo ...] [--ref origin/main]     exit 1 when the file differs from the suite
    python monitored.py show

Sources, read as text (nothing of the suite is imported or run): workflow.py `DEFAULT_PROCESSES` (Windows) and
`DEFAULT_PROCESSES_MAC`, health_gate.py `OBSERVE_ONLY_PROCESSES` and `PROCESS_MEM_CEILING_MB`.
"""

import argparse
import ast
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROSTER = os.path.join(HERE, "monitored_processes.json")
GRS_DEFAULT = os.environ.get("GRS_REPO", "C:/git/nsclient_golden_regression_suite")
WORKFLOW = "golden_regression/workflows/system_test/workflow.py"
HEALTH_GATE = "golden_regression/workflows/system_test/health_gate.py"


def _strings(tree):
    """Module-level NAME = "text" assignments, so a list item that is a NAME resolves to its text."""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                out[node.targets[0].id] = node.value.value
    return out


def _value(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name:
            return node.value
    return None


def _resolve(item, names):
    if isinstance(item, ast.Constant) and isinstance(item.value, str):
        return item.value
    if isinstance(item, ast.Name) and item.id in names:
        return names[item.id]
    raise ValueError(f"cannot resolve a list item: {ast.dump(item)[:80]}")


def _list(tree, name, names):
    node = _value(tree, name)
    if not isinstance(node, (ast.List, ast.Tuple)):
        raise ValueError(f"{name} is not a list or tuple")
    return [_resolve(i, names) for i in node.elts]


def extract(workflow_text, health_text):
    """The roster from the two source texts. Raises ValueError when a constant is missing or not a plain list."""
    wf, hg = ast.parse(workflow_text), ast.parse(health_text)
    wf_names = _strings(wf)
    hg_names = _strings(hg)
    ceilings = {}
    node = _value(hg, "PROCESS_MEM_CEILING_MB")
    if isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values):
            ceilings[_resolve(k, hg_names)] = ast.literal_eval(v)
    return {
        "windows": _list(wf, "DEFAULT_PROCESSES", wf_names),
        "mac": _list(wf, "DEFAULT_PROCESSES_MAC", wf_names),
        "observe_only": _list(hg, "OBSERVE_ONLY_PROCESSES", hg_names),
        "mem_ceiling_mb": ceilings,
    }


def _git_show(repo, ref, path):
    p = subprocess.run(["git", "-C", repo, "show", f"{ref}:{path}"], capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        raise ValueError(f"git show {ref}:{path} failed: {p.stderr.strip()[:200]}")
    return p.stdout


def from_repo(repo, ref):
    data = extract(_git_show(repo, ref, WORKFLOW), _git_show(repo, ref, HEALTH_GATE))
    sha = subprocess.run(["git", "-C", repo, "rev-parse", ref], capture_output=True, text=True).stdout.strip()
    data["source"] = {"ref": ref, "sha": sha, "synced": time.strftime("%Y-%m-%d")}
    return data


def load(path=None):
    """The roster file as a dict, or None when it is missing or unreadable (the report then says so)."""
    try:
        with open(path or ROSTER, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) and "windows" in data and "mac" in data else None
    except (OSError, ValueError):
        return None


def _same(a, b):
    keys = ("windows", "mac", "observe_only", "mem_ceiling_mb")
    return all(a.get(k) == b.get(k) for k in keys)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("sync", "check", "show"))
    ap.add_argument("--repo", default=GRS_DEFAULT)
    ap.add_argument("--ref", default="origin/main")
    a = ap.parse_args(argv)
    if a.action == "show":
        print(json.dumps(load(), indent=2, ensure_ascii=False))
        return 0
    try:
        data = from_repo(a.repo, a.ref)
    except ValueError as exc:
        print(f"monitored: {exc}", file=sys.stderr)
        return 2
    if a.action == "sync":
        with open(ROSTER, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print(f"monitored: wrote {ROSTER} from {a.ref} {data['source']['sha'][:8]}: {len(data['windows'])} windows, {len(data['mac'])} mac")
        return 0
    current = load()
    if current is None:
        print("monitored: the roster file is missing or unreadable", file=sys.stderr)
        return 1
    if _same(current, data):
        print(f"monitored: roster equals {a.ref} {data['source']['sha'][:8]}")
        return 0
    print(f"monitored: roster differs from {a.ref} {data['source']['sha'][:8]}; run `monitored.py sync`", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
