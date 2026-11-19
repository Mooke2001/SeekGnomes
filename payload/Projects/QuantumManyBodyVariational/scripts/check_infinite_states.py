"""Build and audit the project-local Lean infinite-state formalization.

Run with Eureka's Environment/Python/run.sh. Uses the existing pinned toolchain;
does not install packages or make model/API requests. Logs and source hashes
are written inside this project, not to the external runtime environment.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone


PROJECT = Path(__file__).resolve().parents[1]
LEAN = PROJECT / "lean"
RUNTIME = PROJECT / "tmp/lean-runtime/lean-4.35.0-linux/bin"
MODULES = [
    "LocalPauli", "PauliRecovery", "LocalMarginals", "StateLimits",
    "CorrelatorStates", "GlobalMarginals", "StateExamples", "StateClosure",
    "InfiniteStateCertificates", "TrustedStateSpec", "TrustedTasks", "TrustedSpecExamples",
]
ALLOWED_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def without_comments(source: str) -> str:
    """Remove nested Lean block comments and line comments for the token scan."""
    out: list[str] = []
    i, depth = 0, 0
    while i < len(source):
        if source.startswith("/-", i):
            depth += 1
            i += 2
        elif depth and source.startswith("-/", i):
            depth -= 1
            i += 2
        elif depth:
            out.append("\n" if source[i] == "\n" else " ")
            i += 1
        elif source.startswith("--", i):
            j = source.find("\n", i)
            i = len(source) if j == -1 else j
        else:
            out.append(source[i])
            i += 1
    if depth:
        raise ValueError("Unterminated Lean block comment")
    return "".join(out)


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence = PROJECT / "tmp" / f"lean-infinite-state-check-{stamp}"
    evidence.mkdir(parents=True, exist_ok=False)
    sources = [LEAN / "QuantumManyBodyVariational" / f"{n}.lean" for n in MODULES]
    counterexample = LEAN / "QuantumManyBodyVariational/StateCounterexample.lean"
    if counterexample.exists():
        sources.append(counterexample)
    sources += [LEAN / "QuantumManyBodyVariational.lean", LEAN / "InfiniteStateAudit.lean",
                LEAN / "lakefile.toml", LEAN / "lean-toolchain", LEAN / "lake-manifest.json",
                Path(__file__).resolve()]
    hashes = {str(p.relative_to(PROJECT)): sha256(p) for p in sources}
    problems: list[str] = []
    for path in sources:
        if path.suffix == ".lean":
            code = without_comments(path.read_text())
            if re.search(r"\b(sorry|admit|native_decide)\b|(?m:^\s*(?:private\s+|protected\s+)?axiom\s)", code):
                problems.append(f"Forbidden proof escape in {path.relative_to(PROJECT)}")
    env = {k: v for k, v in os.environ.items()
           if not re.search(r"key|token|secret|authorization|password", k, re.I)
           and not k.startswith(("OPENAI_", "DEEPSEEK_", "RELAY_", "EUREKA_API_"))}
    env.update(PATH=f"{RUNTIME}:/usr/bin:/bin",
               ELAN_HOME=str(PROJECT / "tmp/lean-runtime/elan"),
               XDG_CACHE_HOME=str(PROJECT / "tmp/lean-runtime/cache"),
               XDG_CONFIG_HOME=str(PROJECT / "tmp/lean-runtime/config"),
               XDG_DATA_HOME=str(PROJECT / "tmp/lean-runtime/data"),
               XDG_STATE_HOME=str(PROJECT / "tmp/lean-runtime/state"),
               TMPDIR=str(PROJECT / "tmp/lean-runtime/tmp"),
               MATHLIB_NO_CACHE_ON_UPDATE="1", GIT_TERMINAL_PROMPT="0")
    results: dict[str, dict] = {}

    def run(name: str, args: list[str], timeout: int = 600) -> str:
        completed = subprocess.run(args, cwd=LEAN, env=env, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   timeout=timeout)
        log = evidence / f"{name}.log"
        log.write_text(completed.stdout)
        results[name] = {"command": args, "exit_code": completed.returncode,
                         "log": str(log.relative_to(PROJECT)), "sha256": sha256(log)}
        if completed.returncode:
            problems.append(f"{name} exited {completed.returncode}")
        return completed.stdout

    run("lean-version", [str(RUNTIME / "lean"), "--version"])
    run("mathlib-revision", ["/usr/bin/git", "-C", str(LEAN / ".lake/packages/mathlib"),
                              "rev-parse", "HEAD"])
    if not problems:
        run("build", [str(RUNTIME / "lake"), "build", "QuantumManyBodyVariational"])
    if not problems:
        axioms_output = run("axioms", [str(RUNTIME / "lake"), "env", "lean", "InfiniteStateAudit.lean"])
        axiom_lists = re.findall(r"depends on axioms:\s*\[([^\]]*)\]", axioms_output)
        expected = (LEAN / "InfiniteStateAudit.lean").read_text().count("#print axioms")
        if len(axiom_lists) != expected:
            problems.append(f"Expected {expected} axiom reports, found {len(axiom_lists)}")
        for item in axiom_lists:
            unexpected = {s.strip() for s in item.split(",") if s.strip()} - ALLOWED_AXIOMS
            if unexpected:
                problems.append(f"Unapproved axioms: {sorted(unexpected)}")
    changed = [name for name, digest in hashes.items() if sha256(PROJECT / name) != digest]
    if changed:
        problems.append(f"Sources changed during verification: {changed}")
    report = {"created_utc": stamp, "source_sha256": hashes, "checks": results,
              "allowed_axioms": sorted(ALLOWED_AXIOMS), "problems": problems,
              "passed": not problems}
    target = evidence / "manifest.json"
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"passed": report["passed"], "manifest": str(target),
                      "problems": problems}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
