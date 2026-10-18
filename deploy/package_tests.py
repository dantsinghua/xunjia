"""Offline package QA with owned temporary work and retained verification logs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess
import sys
import tempfile
import time


SCOPES = ("tests/release", "tests/acceptance", "tests/cutover", "tests/negotiation")


def execute_stage(name, command, *, root, env, evidence, timeout):
    log_path = evidence / (name + ".log")
    print("PACKAGE_QA_STAGE_BEGIN " + name + " " + shlex.join(command), flush=True)
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        try:
            completed = subprocess.run(command, cwd=root, env=env, stdout=log,
                                       stderr=subprocess.STDOUT, timeout=timeout)
            code = completed.returncode
        except subprocess.TimeoutExpired:
            code = 124
            log.write("\nPACKAGE_QA_STAGE_TIMEOUT\n")
        except OSError as exc:
            code = 127 if isinstance(exc, FileNotFoundError) else 126
            log.write(type(exc).__name__ + ": " + str(exc) + "\n")
    result = {"name": name, "command": command, "exit_code": code,
              "seconds": round(time.monotonic() - started, 2), "log": str(log_path)}
    print("PACKAGE_QA_STAGE_END " + json.dumps(result), flush=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--evidence-parent", type=Path,
                        help="Parent for a fresh retained evidence directory; defaults to system temporary storage")
    args = parser.parse_args(argv)
    root = args.bundle.resolve()
    scopes = (*SCOPES, 'tests/benchmarks') if (root/'tests/benchmarks').is_dir() else SCOPES
    if args.evidence_parent is not None:
        args.evidence_parent.mkdir(parents=True, exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix="xunjia-package-evidence-", dir=args.evidence_parent))
    report = {"package_root": str(root), "evidence_root": str(evidence), "test_scopes": list(scopes), "results": []}
    exit_code, work = 0, None
    print("PACKAGE_QA_BEGIN " + json.dumps({"package_root": str(root), "evidence_root": str(evidence)}), flush=True)
    try:
        # This context owns exactly one newly created root. Logs live separately
        # so a failed wheel installation or test never deletes its own evidence.
        with tempfile.TemporaryDirectory(prefix="xunjia-package-qa-") as temporary:
            work = Path(temporary)
            report["work_root"] = str(work)
            scratch = work / "tmp"
            scratch.mkdir()
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1",
                       PYTHONPATH=os.pathsep.join((str(root / "app"), str(root))), TMPDIR=str(scratch))
            python = str(work / "venv" / "bin" / "python")
            pytest = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                      "--basetemp=" + str(work / "tests"), *scopes, "--disable-warnings", "--tb=short"]
            stages = [
                ("verify-package", [sys.executable, str(root / "deploy/manager.py"), "verify", "--package-only", "--bundle", str(root)], 60),
                ("create-venv", [sys.executable, "-m", "venv", str(work / "venv")], 300),
                ("install-offline", [python, "-m", "pip", "install", "--no-index", "--no-deps", "--disable-pip-version-check",
                    "--find-links=" + str(root / "wheels" / platform.machine()), "--find-links=" + str(root / "wheels/common"),
                    "-r", str(root / "requirements-dev.lock")], 1800),
                ("pip-check", [python, "-m", "pip", "check"], 60),
                ("pytest-collect", [*pytest, "--collect-only"], 300),
                ("pytest", pytest, 1800),
            ]
            for name, command, budget in stages:
                result = execute_stage(name, command, root=root, env=env, evidence=evidence, timeout=budget)
                report["results"].append(result)
                code = result["exit_code"]
                if code:
                    exit_code = code if code > 0 else 128 - code
                    break
                if name == "pytest-collect":
                    lines = Path(result["log"]).read_text(encoding="utf-8", errors="replace").splitlines()
                    nodes = sorted({line.strip() for line in lines
                                    if "::" in line and line.strip().split("::", 1)[0].endswith(".py")})
                    collected = evidence / "pytest-collected.json"
                    collected.write_text(json.dumps(nodes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    report["collection"] = {"count": len(nodes), "nodeids": str(collected)}
                    if not nodes:
                        report["error"] = "PYTEST_COLLECTION_EMPTY"
                        exit_code = 5
                        break
    except KeyboardInterrupt:
        report["error"] = "INTERRUPTED"
        exit_code = 130
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
        exit_code = exit_code or 1
    report["work_removed"] = work is None or not work.exists()
    if not report["work_removed"]:
        exit_code = exit_code or 1
        report.setdefault("error", "TEMPORARY_WORK_CLEANUP_FAILED")
    report.update(exit_code=exit_code, passed=exit_code == 0)
    result_path = evidence / "result.json"
    result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("PACKAGE_QA_END " + json.dumps({"passed": report["passed"], "exit_code": exit_code,
          "work_removed": report["work_removed"], "report": str(result_path)}), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
