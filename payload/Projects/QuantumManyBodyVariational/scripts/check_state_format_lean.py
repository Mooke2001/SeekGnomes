#!/usr/bin/env python3
"""Check generated Lean, a rational decision, and a full state proof."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from src.state_format import StateProgram


BRANCH_SOURCE = """def f(P: Word) -> Real:
    threshold = Real(10 ** 20)
    offset = Real(1 / 10 ** 50)
    return 1 if threshold + offset > threshold else 0
"""

BRANCH_CHECK = """
namespace GeneratedState
open QuantumManyBodyVariational
example : (dsl0_f 0).value.rat = 1 := by
  norm_num [dsl0_f, Checked.bind, Checked.pure, HybridMath.ofInt,
    HybridMath.ofRat, HybridMath.add]
end GeneratedState
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--lean", type=Path, default=PROJECT / "tmp/lean-runtime/lean-4.35.0-linux/bin/lean"
    )
    args = parser.parse_args()
    lean = str(args.lean.resolve())
    lake = PROJECT / "lean/.lake"
    libraries = [lake / "build/lib/lean", *sorted((lake / "packages").glob("*/.lake/build/lib/lean"))]
    examples = PROJECT / "examples/state_format"
    performed = {}
    with tempfile.TemporaryDirectory(dir=PROJECT / "tmp", prefix="state-format-native-") as temp:
        for name, dimension in (("tracial", 1), ("square_tfim", 2), ("product_limit", 1), ("rational_branch", 1)):
            work = Path(temp) / name
            work.mkdir()
            source = BRANCH_SOURCE if name == "rational_branch" else (examples / f"{name}.state").read_text()
            state = StateProgram(source, dimension, 1)
            generated = state.lean_source()
            if name == "rational_branch":
                if state.evaluate(()) != 1:
                    raise RuntimeError("Python selected the wrong branch")
                generated += BRANCH_CHECK
            env = dict(os.environ, LEAN_PATH=":".join(str(path) for path in [work, *libraries]))
            start = time.monotonic()
            (work / "GeneratedState.lean").write_text(generated, encoding="utf-8")
            try:
                compiled = subprocess.run(
                    [lean, "-j2", "GeneratedState.lean", "-o", "GeneratedState.olean"],
                    cwd=work, env=env, check=True, capture_output=True, text=True, timeout=120,
                )
                print(compiled.stdout + compiled.stderr, end="", file=sys.stderr)
                if name == "tracial":
                    proof = (examples / "TracialProof.lean").read_text(encoding="utf-8")
                    (work / "Proof.lean").write_text(proof + "\n#print axioms Submission.accepted\n", encoding="utf-8")
                    audit = subprocess.run(
                        [lean, "-j2", "Proof.lean"], cwd=work, env=env, check=True,
                        capture_output=True, text=True, timeout=120,
                    )
                    if "[propext, Classical.choice, Quot.sound]" not in audit.stdout:
                        raise RuntimeError(f"Unexpected axioms:\n{audit.stdout}\n{audit.stderr}")
            except subprocess.CalledProcessError as exc:
                parser.exit(1, f"{name}: {exc}\n{exc.stdout}\n{exc.stderr}\n")
            except (OSError, subprocess.SubprocessError) as exc:
                parser.exit(1, f"{name}: {exc}\n")
            performed[name] = {
                "lean_bytes": len(generated.encode("utf-8")),
                "seconds": round(time.monotonic() - start, 3),
                "state_proof_checked": name == "tracial",
                "rational_branch_checked": name == "rational_branch",
                "source_sha256": state.source_sha256,
            }
    print(json.dumps(performed, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
