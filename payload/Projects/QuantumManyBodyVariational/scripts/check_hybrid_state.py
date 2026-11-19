#!/usr/bin/env python3
"""Check a complete Lean proof against a fixed hybrid Python correlator."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from src.hybrid_proof_verifier import verify, _old


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--proof", type=Path, required=True)
    parser.add_argument("--dimension", type=int, required=True)
    parser.add_argument("--qubits-per-cell", type=int, required=True)
    parser.add_argument("--sqrt-bits", type=int, default=40)
    parser.add_argument("--series-terms", type=int, default=32)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--output", type=Path,
                        help="Optional new report path inside the project; never overwritten")
    args = parser.parse_args()
    try:
        with _old.regular_input(args.source.absolute(), 65536) as incoming:
            source = incoming.read().decode()
        result = verify(source, args.proof, args.dimension, args.qubits_per_cell,
                        bits=args.sqrt_bits, terms=args.series_terms, timeout=args.timeout)
        if args.output is not None:
            target = args.output.absolute()
            if not target.is_relative_to(PROJECT) or target.parent.resolve(strict=True) != target.parent:
                raise ValueError("--output must be a new regular file inside the project without path aliases")
            _old.write_new(target, (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode())
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["passed"] else 1
    except (ValueError, OSError, UnicodeError) as error:
        print(json.dumps({"passed": False, "problems": [str(error)]}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
