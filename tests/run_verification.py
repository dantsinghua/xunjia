"""Run all offline gates and reject evidence if source files drift during them."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.process_runner import run_command


def run_logged(command, cwd, env, budget, log_path, *, expected_nodeids=None, details=None):
    is_pytest = any(command[i:i+2] == ['-m','pytest'] for i in range(len(command)-1))
    mode = ('collect' if '--collect-only' in command else 'run') if is_pytest else None
    result = run_command(command,cwd=cwd,env=env,timeout=budget,log_path=log_path,
                         pytest_mode=mode,expected_nodeids=expected_nodeids)
    if details is not None:
        details.update(result)
    return result['exit_code'],round(result['elapsed_seconds'],2)


def collected_nodeids(log_path):
    """Keep pytest's actual collected IDs, including parametrized cases."""
    nodes = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        value = line.strip()
        if "::" in value and value.split("::", 1)[0].endswith(".py"):
            nodes.append(value)
    return sorted(set(nodes))


def manifest():
    listed = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT,
    ).decode().split("\0")
    result = {}
    for relative in sorted(set(listed)):
        path = ROOT / relative
        if not relative or not path.is_file():
            continue
        runtime_prompt = path.suffix == ".md" and Path(relative).parts[0] == "dify"
        if path.suffix not in {".py", ".ts", ".vue", ".js", ".mjs", ".json", ".ini", ".toml", ".txt", ".yml", ".yaml", ".sql", ".sh", ".ps1", ".psm1", ".cmd", ".bat", ".lock"} and not runtime_prompt:
            continue
        if any(part in {"data", "state", "node_modules", "dist", ".venv", "test-logs", "runs"} for part in path.relative_to(ROOT).parts):
            continue
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--browsers-path", default=os.environ.get("PLAYWRIGHT_BROWSERS_PATH"))
    parser.add_argument("--mysql", action="store_true", help="Include the explicitly isolated MySQL container gate")
    parser.add_argument("--load", action="store_true", help="Include R32 5-owner synthetic SQLite and isolated MySQL load acceptance")
    parser.add_argument('--output-root',type=Path,default=Path.home()/'.local/state/xunjia-codex/verification',
                        help='Persistent evidence parent; each invocation creates a new run directory')
    args = parser.parse_args()
    if not args.browsers_path:
        parser.error("--browsers-path must name the isolated installed browser directory")
    args.output_root.mkdir(parents=True,exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="xunjia-verification-",dir=args.output_root))
    before = manifest()
    (folder / "source-before.json").write_text(json.dumps(before, indent=2), encoding="utf-8")
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=args.browsers_path, PYTHONUNBUFFERED="1")
    commands = [
        ("pytest", ROOT, [sys.executable, "-m", "pytest", "-q", "--tb=short"], 1800),
        ("web-unit", ROOT / "web", ["node", "--experimental-strip-types", "--test", "tests/quoteFilter.test.ts", "tests/rfqLimit.test.ts"], 300),
        ("web-developer", ROOT / "web", ["node", "tests/developer/run.mjs"], 300),
        ("web-python-developer", ROOT, [sys.executable, "-m", "pytest", "web/tests/developer", "-q", "--tb=short"], 300),
        ("web-build", ROOT / "web", ["npm", "run", "build"], 1800),
        ("production-web", ROOT, [sys.executable, "-m", "pytest", "tests/acceptance/web_assets_contract.py", "-q", "--tb=short"], 300),
        ("web-browser", ROOT, [sys.executable, "-m", "pytest", "web/tests/acceptance", "-q", "--tb=short"], 1200),
    ]
    if args.mysql:
        commands.insert(1, ("mysql-compat", ROOT, [sys.executable, "-m", "pytest", "tests/acceptance/mysql_contract.py", "-q", "-s", "--tb=short"], 1800))
    if args.load:
        commands.append(("load-acceptance", ROOT, [sys.executable, "-m", "pytest", "tests/acceptance/load_contract.py", "-q", "-s", "--tb=short"], 1800))
    results = []
    inventory = {}
    print(f"Evidence directory: {folder}", flush=True)
    for name, cwd, command, budget in commands:
        log_path = folder / f"{name}.log"
        collection = None
        if command[1:3] == ["-m", "pytest"]:
            collect_command = [*command, "--collect-only"]
            collect_log = folder / f"{name}.collect.log"
            print(f"Collecting {name}: {shlex.join(collect_command)}", flush=True)
            collected = {}
            collect_code, collect_seconds = run_logged(collect_command, cwd, env, min(budget, 300), collect_log,
                                                       details=collected)
            inventory[name] = collected.get('pytest', {}).get('nodeids', [])
            collection = {"command": collect_command, "exit_code": collect_code, "seconds": collect_seconds,
                          "log": str(collect_log), "count": len(inventory[name]),
                          "execution_evidence": collected.get('evidence')}
            (folder / "pytest-inventory.json").write_text(json.dumps(inventory, indent=2), encoding="utf-8")
            if collect_code != 0 or not collected.get('passed') or not inventory[name]:
                item = {"name": name, "cwd": str(cwd), "command": command, "exit_code": collect_code,
                        "seconds": collect_seconds, "log": str(collect_log), "collection": collection,
                        "execution_evidence": collected.get('evidence'), "passed": False,
                        "run_started": False, "cancelled": collected.get('cancelled', False)}
                results.append(item)
                print(json.dumps(item), flush=True)
                break
        print(f"Running {name}: {shlex.join(command)}", flush=True)
        execution = {}
        code, seconds = run_logged(command, cwd, env, budget, log_path,
                                   expected_nodeids=inventory.get(name),details=execution)
        item = {"name": name, "cwd": str(cwd), "command": command, "exit_code": code,
                "seconds": seconds, "log": str(log_path), "collection": collection,
                "execution_evidence":execution.get('evidence'),
                "actual_pytest":execution.get('pytest'),
                "passed": code == 0 and (collection is None or collection["exit_code"] == 0 and collection["count"] > 0)}
        results.append(item)
        print(json.dumps(item), flush=True)
        if execution.get('cancelled'):
            break
    after = manifest()
    drift = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    node_groups = {}
    for name, nodes in inventory.items():
        for node in nodes:
            node_groups.setdefault(node, []).append(name)
    report = {"run_root": str(ROOT), "results": results, "source_drift": drift,
              "python_coverage": {"inventory": str(folder / "pytest-inventory.json"),
                                  "unique_tests": len(node_groups),
                                  "group_counts": {name: len(nodes) for name, nodes in inventory.items()},
                                  "duplicate_tests": {node: names for node, names in node_groups.items() if len(names) > 1}},
              "passed": not drift and all(item["passed"] for item in results)}
    (folder / "source-after.json").write_text(json.dumps(after, indent=2), encoding="utf-8")
    (folder / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "source_drift": drift, "report": str(folder / "result.json")}), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
