"""Verify the frozen state specification and judge proof exports against it.

Default: build the reviewed library and run positive and rejection fixtures.
With --solution-export: read a third-party NDJSON proof export, never import
its olean or execute its Lean source. The challenge always comes from our
locked source. Candidate judging runs inside a network-disabled namespace.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


PROJECT = Path(__file__).resolve().parents[1]
LEAN = PROJECT / "lean"
BIN = PROJECT / "tmp/lean-runtime/lean-4.35.0-linux/bin"
LOCK = PROJECT / "STATE_SPEC_V1.lock.json"
CONFIG = LEAN / "state-spec-comparator.json"
PREFIX = "QuantumManyBodyVariational.SpecChecks."
CASES = {"Challenge": 0, "Solution": 0, "WrongGoal": 1, "WrongTask": 1,
         "ExtraAssumption": 1, "ForbiddenAxiom": 1, "RewrittenSpec": 1}
PRIMITIVES = [
    "Quot", "Quot.mk", "Quot.lift", "Quot.ind", "Submission.accepted",
    "Submission.f", "Submission.bound", "propext", "Quot.sound", "Classical.choice",
    "Nat.add", "Nat.sub", "Nat.mul", "Nat.pow", "Nat.gcd", "Nat.div", "Nat.mod",
    "Nat.beq", "Nat.ble", "Nat.land", "Nat.lor", "Nat.xor", "Nat.shiftLeft",
    "Nat.shiftRight", "String.ofList", "Char.ofNat", "List", "eagerReduce", "Nat",
    "String", "String.mk", "Char", "optParam", "autoParam", "semiOutParam", "outParam",
]


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solution-export", type=Path,
                        help="Judge this NDJSON instead of running the regression fixtures")
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    evidence = PROJECT / "tmp" / f"state-spec-check-{stamp}"
    evidence.mkdir(parents=True)
    (evidence / "sandbox-tmp").mkdir()
    report: dict = {"spec": "infinite-state-spec/v1", "created_utc": stamp,
                    "lock_sha256": digest(LOCK), "checks": {}, "problems": []}
    lock = json.loads(LOCK.read_text())
    env = {k: v for k, v in os.environ.items()
           if not re.search(r"key|token|secret|authorization|password", k, re.I)
           and not k.startswith(("OPENAI_", "DEEPSEEK_", "RELAY_", "EUREKA_API_"))}
    env.update(PATH=f"{BIN}:/usr/bin:/bin", TMPDIR=str(evidence / "sandbox-tmp"),
               ELAN_HOME=str(PROJECT / "tmp/lean-runtime/elan"),
               XDG_CACHE_HOME=str(PROJECT / "tmp/lean-runtime/cache"),
               XDG_CONFIG_HOME=str(PROJECT / "tmp/lean-runtime/config"),
               XDG_DATA_HOME=str(PROJECT / "tmp/lean-runtime/data"),
               XDG_STATE_HOME=str(PROJECT / "tmp/lean-runtime/state"),
               MATHLIB_NO_CACHE_ON_UPDATE="1", GIT_TERMINAL_PROMPT="0")

    def check_lock(label: str) -> None:
        failures = []
        for relative, expected in lock["source_sha256"].items():
            path = (PROJECT / relative).resolve()
            if not path.is_relative_to(PROJECT) or not path.is_file() or digest(path) != expected:
                failures.append(relative)
        for binary, expected in lock["toolchain_binary_sha256"].items():
            if digest(BIN / binary) != expected:
                failures.append(f"toolchain/{binary}")
        if digest(LOCK) != report["lock_sha256"]:
            failures.append("STATE_SPEC_V1.lock.json changed during verification")
        report["checks"][label] = {"passed": not failures, "mismatches": failures}
        if failures:
            raise ValueError(f"Frozen specification changed: {failures}")

    def run(label: str, cmd: list[str], expected: int = 0, *, export: Path | None = None,
            timeout: int = 600) -> str:
        log = evidence / f"{label}.log"
        if export is None:
            p = subprocess.run(cmd, cwd=LEAN, env=env, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=timeout)
            output = p.stdout
            log.write_text(output)
        else:
            with export.open("w") as stdout:
                p = subprocess.run(cmd, cwd=LEAN, env=env, text=True, stdout=stdout,
                                   stderr=subprocess.PIPE, timeout=timeout)
            output = p.stderr
            log.write_text(output)
        passed = p.returncode == expected
        item = {"command": cmd, "exit_code": p.returncode, "expected_exit_code": expected,
                "passed": passed, "log": str(log.relative_to(PROJECT)), "log_sha256": digest(log)}
        if export is not None:
            item.update(export=str(export.relative_to(PROJECT)), export_sha256=digest(export))
        report["checks"][label] = item
        if not passed:
            raise ValueError(f"{label}: expected {expected}, received {p.returncode}; see {log}")
        return output

    def judge(label: str, solution: Path, expected: int) -> None:
        # Both exports are supplied: comparator skips dependency resolution and
        # compilation. Only this evidence directory is writable in the outer
        # namespace, and all its child processes have no network.
        cmd = ["/usr/bin/bwrap", "--ro-bind", "/", "/", "--bind", str(evidence),
               str(evidence), "--tmpfs", "/tmp", "--dev", "/dev", "--proc", "/proc",
               "--unshare-net", "--die-with-parent", "--", str(BIN / "lake"), "comparator",
               "--config", str(CONFIG), "--challenge-from-export", str(evidence / "Challenge.ndjson"),
               "--solution-from-export", str(solution)]
        output = run(label, cmd, expected)
        name = label.removeprefix("judge-")
        if expected == 0:
            required = ["Lean default kernel accepts the solution", "Your solution is okay!"]
        elif name == "ForbiddenAxiom":
            required = ["Illegal axiom detected: 'Submission.fake'"]
        elif name == "RewrittenSpec":
            required = ["Const does not match between challenge and target",
                        "QuantumManyBodyVariational.InfiniteState.TrustedTasks.tfim2D"]
        else:
            required = ["Challenge and solution theorem statement do not match: 'Submission.accepted'"]
        diagnostic_ok = all(fragment in output for fragment in required)
        report["checks"][label]["diagnostic_passed"] = diagnostic_ok
        if not diagnostic_ok:
            raise ValueError(f"{label}: exit code matched but the expected proof-checking reason did not")

    try:
        check_lock("lock-before")
        revision = run("mathlib-revision", ["/usr/bin/git", "-C", str(LEAN / ".lake/packages/mathlib"),
                                            "rev-parse", "HEAD"]).strip()
        if revision != lock["mathlib_revision"]:
            raise ValueError("mathlib revision differs from the frozen specification")
        run("mathlib-tracked-clean", ["/usr/bin/git", "-C", str(LEAN / ".lake/packages/mathlib"),
                                      "diff", "--exit-code", "HEAD", "--"])
        names = ["Challenge"] if args.solution_export else list(CASES)
        run("build", [str(BIN / "lake"), "build", "QuantumManyBodyVariational",
                      *[PREFIX + name for name in names]])
        axioms = run("axioms", [str(BIN / "lake"), "env", "lean", "InfiniteStateAudit.lean"])
        lists = re.findall(r"depends on axioms:\s*\[([^\]]*)\]", axioms)
        expected_count = (LEAN / "InfiniteStateAudit.lean").read_text().count("#print axioms")
        if len(lists) != expected_count:
            raise ValueError("Incomplete axiom report")
        allowed = set(lock["permitted_axioms"])
        for axiom_list in lists:
            if {s.strip() for s in axiom_list.split(",") if s.strip()} - allowed:
                raise ValueError(f"Unexpected axioms: {axiom_list}")
        for name in names:
            run(f"export-{name}", [str(BIN / "lake"), "env", "leanexport", PREFIX + name,
                                    "--", *PRIMITIVES], export=evidence / f"{name}.ndjson")
        if args.solution_export:
            source = args.solution_export.resolve(strict=True)
            # Copy input before judging, bind the exact bytes to the report,
            # and make them available even if the caller's path later changes.
            target = evidence / "Submitted.ndjson"
            with source.open("rb") as incoming, target.open("wb") as outgoing:
                for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                    outgoing.write(chunk)
            report["submitted_export_sha256"] = digest(target)
            judge("submitted", target, 0)
        else:
            for name, expected in CASES.items():
                judge(f"judge-{name}", evidence / f"{name}.ndjson", expected)
        check_lock("lock-after")
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        report["problems"].append(str(error))
    report["passed"] = not report["problems"]
    target = evidence / "manifest.json"
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"passed": report["passed"], "manifest": str(target),
                      "problems": report["problems"]}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
