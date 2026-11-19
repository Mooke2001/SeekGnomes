#!/usr/bin/env python3
"""Build both state artifacts or evaluate a candidate on a local Hamiltonian."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from src.benchmark50_models import load_task
from src.state_format import StateProgram, source_from_markdown


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def json_text(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("build", "energy", "evaluate"):
        action = sub.add_parser(command)
        action.add_argument("source", type=Path, help="A .state program or f.md handoff")
        action.add_argument("--sqrt-bits", type=int, default=40)
        action.add_argument("--series-terms", type=int, default=32)
        action.add_argument("--precision-bits", type=int, default=40, help="Requested limit truncation bits")
        if command == "energy":
            hamiltonian = action.add_mutually_exclusive_group(required=True)
            hamiltonian.add_argument("--task", type=Path, help="JSON local Hamiltonian task")
            hamiltonian.add_argument("--task-id", help="Identifier in the existing benchmark catalog")
        else:
            action.add_argument("--dimension", type=int, required=True)
            action.add_argument("--qubits-per-cell", type=int, default=1)
            if command == "evaluate":
                action.add_argument("--word", type=Path, required=True, help="JSON Pauli word")
            else:
                action.add_argument("--output", type=Path, required=True, help="Output directory")

    args = parser.parse_args(argv)
    try:
        source = args.source.read_text(encoding="utf-8")
        if args.source.suffix.lower() == ".md":
            source = source_from_markdown(source)
        if args.command == "energy":
            task = read_json(args.task) if args.task else load_task(args.task_id)
            dimension, qubits = task["dimension"], task["qubits_per_cell"]
        else:
            dimension, qubits = args.dimension, args.qubits_per_cell
        state = StateProgram(source, dimension, qubits, bits=args.sqrt_bits, terms=args.series_terms,
                             precision_bits=args.precision_bits)
        if args.command == "energy":
            response = state.energy(task)
        elif args.command == "evaluate":
            response = {"expectation": state.evaluate(read_json(args.word)), "program": state.manifest()}
        else:
            output = args.output.resolve()
            if not output.is_relative_to(PROJECT):
                raise ValueError("Output must be within the QuantumManyBodyVariational project")
            files = {"f.py": state.python_source(), "GeneratedState.lean": state.lean_source()}
            response = {
                **state.manifest(),
                "artifacts": {
                    name: {"bytes": len(data.encode("utf-8")),
                           "sha256": hashlib.sha256(data.encode("utf-8")).hexdigest()}
                    for name, data in files.items()
                },
            }
            files["manifest.json"] = json_text(response)
            output.mkdir(parents=True, exist_ok=True)
            for name, data in files.items():
                path = output / name
                if path.is_symlink() or (path.exists() and path.read_text(encoding="utf-8") != data):
                    raise ValueError(f"Output already exists with different content: {path}")
            for name, data in files.items():
                path = output / name
                if not path.exists():
                    with path.open("x", encoding="utf-8") as handle:
                        handle.write(data)
        print(json_text(response), end="")
        return 0
    except StopIteration:
        parser.exit(2, f"{parser.prog}: no benchmark task {args.task_id!r}\n")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"{parser.prog}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
