#!/usr/bin/env python3
"""Translate a restricted Python correlator, or evaluate its energy density.

No candidate Python is exec'd or imported. All paths use the same parsed
program. The optional proof gate checks legality and translation invariance
against a host-generated fixed Lean target before evaluating energy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import stat
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from src.benchmark50_models import load_task
from src.correlator_energy import evaluate_energy, normalize_task
from src.python_correlator_compiler import CorrelatorProgram as CorrelatorProgramV1


def sha256(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def read_source(path: Path) -> str:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Correlator source must be a regular file")
    if info.st_size > 65536:
        raise ValueError("Correlator source must be at most 64 KiB")
    with path.open("rb") as handle:
        source = handle.read(65537)
    if len(source) > 65536:
        raise ValueError("Correlator source must be at most 64 KiB")
    return source.decode("utf-8")


def write_new(path: Path, content: str) -> None:
    """Keep writes in the project, without overwriting prior evidence."""
    resolved = path.resolve()
    if not resolved.is_relative_to(PROJECT):
        raise ValueError("Output must be within the QuantumManyBodyVariational project")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    if resolved.exists():
        if resolved.read_text(encoding="utf-8") == content:
            return
        raise ValueError(f"Refusing to overwrite different content: {resolved}")
    with resolved.open("x", encoding="utf-8") as handle:
        handle.write(content)


def compiler_files(language: str) -> dict[str, str]:
    names = ["src/python_correlator_compiler.py"]
    if language in ("v2", "rational", "hybrid"):
        names += ["src/python_correlator_v2.py", "src/correlator_float_guard.py"]
    if language in ("rational", "hybrid"):
        names += ["src/python_correlator_rational.py", "src/correlator_rational_math.py",
                  "src/rational_text.py",
                  "lean/QuantumManyBodyVariational/RationalMath.lean"]
    if language == "hybrid":
        names += ["src/python_correlator_hybrid.py", "src/correlator_hybrid_branch.py",
                  "lean/QuantumManyBodyVariational/HybridMath.lean"]
    return {name: hashlib.sha256((PROJECT/name).read_bytes()).hexdigest() for name in names}


def compiler_sha(language: str = "v1") -> str:
    files = compiler_files(language)
    if language == "v1":
        return files["src/python_correlator_compiler.py"]
    return sha256(json.dumps(files, sort_keys=True))


def select_program(source: str, dimension: int, qubits: int, args):
    previous = json.loads(args.manifest.read_text()) if getattr(args, "manifest", None) else None
    version = previous.get("program", {}).get("language_version", "") if previous else ""
    inferred = ("v1" if version == "python-correlator-v1" else "v2" if version == "python-correlator-v2" else
                "rational" if version == "python-correlator-rational-v1" else "hybrid")
    language = args.language or inferred
    if language in ("rational", "hybrid"):
        if args.branch_margin is not None:
            raise ValueError("Rational branch decisions do not use --branch-margin")
        if language == "hybrid":
            from src.python_correlator_hybrid import CorrelatorProgram
        else:
            from src.python_correlator_rational import CorrelatorProgram
        config = previous.get("program", {}).get("rational_math", {}) if previous else {}
        bits = args.sqrt_bits if args.sqrt_bits is not None else config.get("bits", config.get("precision_bits", 40))
        terms = args.series_terms if args.series_terms is not None else config.get("terms", config.get("series_terms", 32))
        return CorrelatorProgram.from_source(source, dimension, qubits, bits=bits, terms=terms), language
    if language == "v1":
        if args.branch_margin is not None:
            raise ValueError("--branch-margin applies to language v2")
        return CorrelatorProgramV1.from_source(source, dimension, qubits), language
    from src.python_correlator_v2 import CorrelatorProgram
    margin = args.branch_margin
    if margin is None and previous:
        margin = previous.get("program", {}).get("branch_margin")
    if margin is None:
        margin = "1e-10"
    return CorrelatorProgram.from_source(source, dimension, qubits, branch_margin=margin), language


def make_manifest(program, definition: str, output: Path, namespace: str, language: str) -> dict:
    return {
        "schema_version": "python-correlator-translation-v1",
        "program": program.manifest(),
        "namespace": namespace,
        "definition_file": output.name,
        "definition_sha256": sha256(definition),
        "compiler_sha256": compiler_sha(language),
        "compiler_files": compiler_files(language),
        "language": language,
        "lean_toolchain": (PROJECT / "lean/lean-toolchain").read_text().strip(),
        "proof_status": "not_checked_here",
        "numerical_relationship": ("Exact rational algorithms define fRat; f is its real embedding" if language == "rational" else
                                   "Real value expressions with identical rationalized branch decisions in Python and Lean; values evaluated approximately" if language == "hybrid" else
                                   "Python float evaluation approximates the generated Lean real expression; no certified rounding bound"),
    }


def check_manifest(path: Path, program, language: str) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "python-correlator-translation-v1":
        raise ValueError("Unsupported translation manifest")
    if manifest.get("program") != program.manifest():
        raise ValueError("Python source, lattice shape, or program semantics changed since translation")
    if manifest.get("compiler_sha256") != compiler_sha(language):
        raise ValueError("Compiler changed since translation; generate a fresh definition and manifest")
    definition = program.lean_definition(namespace=manifest["namespace"])
    if sha256(definition) != manifest.get("definition_sha256"):
        raise ValueError("Regenerated Lean definition does not match the manifest")
    basename = manifest.get("definition_file")
    if not isinstance(basename, str) or Path(basename).name != basename:
        raise ValueError("Manifest definition_file must be a basename")
    if sha256((path.parent / basename).read_text(encoding="utf-8")) != manifest["definition_sha256"]:
        raise ValueError("Generated Lean definition file has changed")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    translate = commands.add_parser("translate", help="Generate a Lean f definition and a source-binding manifest")
    translate.add_argument("--source", type=Path, required=True)
    translate.add_argument("--dimension", type=int, required=True)
    translate.add_argument("--qubits-per-cell", type=int, required=True)
    translate.add_argument("--namespace", default="PythonCorrelator")
    translate.add_argument("--output", type=Path, required=True)
    translate.add_argument("--language", choices=("v1", "v2", "rational", "hybrid"), help="Default: hybrid; legacy interfaces remain available")
    translate.add_argument("--branch-margin", help="Positive branch separation for v2, default 1e-10")
    translate.add_argument("--sqrt-bits", type=int, help="Rational sqrt dyadic resolution, default 40")
    translate.add_argument("--series-terms", type=int, help="Rational elementary-function series terms, default 32")
    energy = commands.add_parser("energy", help="Compute energy assuming the same f has been proved legal and TI")
    energy.add_argument("--source", type=Path, required=True)
    target = energy.add_mutually_exclusive_group(required=True)
    target.add_argument("--task-id", type=int, choices=range(1,51), help="Existing frozen catalogue ID, 1..50")
    target.add_argument("--task-json", type=Path, help="A single finite h0 task JSON")
    energy.add_argument("--manifest", type=Path, help="Require source and generated definition to match a prior translation")
    energy.add_argument("--output", type=Path, help="Save JSON; always print it as well")
    energy.add_argument("--language", choices=("v1", "v2", "rational", "hybrid"), help="Infer from manifest, otherwise hybrid")
    energy.add_argument("--branch-margin", help="Infer from manifest, otherwise 1e-10 for v2")
    energy.add_argument("--sqrt-bits", type=int, help="Infer rational sqrt precision from manifest")
    energy.add_argument("--series-terms", type=int, help="Infer rational series terms from manifest")
    energy.add_argument("--proof", type=Path, help="Verify this Lean proof against the fixed generated f before evaluating energy")
    energy.add_argument("--proof-timeout", type=int, default=180)
    prepare = commands.add_parser("prepare-proof", help="Generate the fixed GeneratedState module and a Lean proof template")
    check = commands.add_parser("check-proof", help="Independently kernel-check a proof of the fixed generated state condition")
    for proof_command in (prepare, check):
        proof_command.add_argument("--source", type=Path, required=True)
        proof_command.add_argument("--dimension", type=int, required=True)
        proof_command.add_argument("--qubits-per-cell", type=int, required=True)
        proof_command.add_argument("--sqrt-bits", type=int)
        proof_command.add_argument("--series-terms", type=int)
        proof_command.set_defaults(language="hybrid", branch_margin=None)
    prepare.add_argument("--output-dir", type=Path, required=True)
    check.add_argument("--proof", type=Path, required=True)
    check.add_argument("--manifest", type=Path, help="Also require this source/definition binding")
    check.add_argument("--timeout", type=int, default=180)
    check.add_argument("--output", type=Path, help="Save the verification report")
    approx = commands.add_parser("approx", help="Compute a deterministic rational approximation outside f")
    approx.add_argument("--function", required=True, choices=("sqrt","exp","sin","cos","log","tanh","pi","e"))
    approx.add_argument("--argument", default="0", help="Exact integer, decimal or n/d input")
    approx.add_argument("--sqrt-bits", type=int, default=40)
    approx.add_argument("--series-terms", type=int, default=32)
    approx.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "approx":
            from src.correlator_rational_math import RationalMath
            from src.correlator_float_guard import _fraction
            from src.correlator_rational_energy import display
            from src.rational_text import rational_text, integer_text
            backend = RationalMath(bits=args.sqrt_bits, terms=args.series_terms)
            argument = _fraction(args.argument)
            value = backend.constant(args.function) if args.function in ("pi","e") else backend.approx(args.function, argument)
            result = {"function": args.function, "argument_exact": rational_text(argument),
                      "result_exact": rational_text(value), "numerator": integer_text(value.numerator),
                      "denominator": integer_text(value.denominator), "display": display(value),
                      "rational_math": backend.manifest(),
                      "meaning": "This rational value defines the approximation used in fRat; it is not the exact transcendental value"}
            if args.output:
                write_new(args.output, json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)+"\n")
            print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
            return 0
        source = read_source(args.source)
        if args.command in ("prepare-proof", "check-proof"):
            program, language = select_program(source, args.dimension, args.qubits_per_cell, args)
            if language != "hybrid":
                raise ValueError("The fixed-module proof verifier currently targets hybrid states")
            if args.command == "prepare-proof":
                output = args.output_dir.resolve()
                definition = program.lean_definition(namespace="GeneratedState")
                lean_file = output / "GeneratedState.lean"
                manifest_path = output / "GeneratedState.manifest.json"
                manifest = make_manifest(program, definition, lean_file, "GeneratedState", language)
                template = ("module\npublic import GeneratedState\n"
                            "public import QuantumManyBodyVariational.TrustedSpecExamplesV2\n"
                            "@[expose] public section\nnoncomputable section\nnamespace Submission\n\n"
                            "-- Replace skip with a proof. This unfinished template is not a certificate.\n"
                            "-- sorry and additional axioms are rejected by the verifier.\n"
                            "theorem accepted : GeneratedState.ProofObligation := by\n  skip\n\n"
                            "end Submission\n")
                write_new(lean_file, definition)
                write_new(manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False)+"\n")
                write_new(output/"Candidate.lean", template)
                result = {"ok": True, "generated_state": str(lean_file), "manifest": str(manifest_path),
                          "proof_template": str(output/"Candidate.lean"),
                          "required_theorem": "Submission.accepted", "fixed_target": "GeneratedState.ProofObligation",
                          "proof_status": "template_not_proved"}
            else:
                if args.manifest:
                    check_manifest(args.manifest, program, language)
                from src.hybrid_proof_verifier import verify
                result = verify(source, args.proof, args.dimension, args.qubits_per_cell,
                                bits=program.maths.maths.bits, terms=program.maths.maths.terms,
                                timeout=args.timeout)
                if args.output:
                    write_new(args.output, json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False)+"\n")
                print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
                return 0 if result.get("passed") is True else 1
            print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
            return 0
        if args.command == "translate":
            program, language = select_program(source, args.dimension, args.qubits_per_cell, args)
            definition = program.lean_definition(namespace=args.namespace)
            output = args.output.resolve()
            manifest_path = output.with_suffix(".manifest.json")
            if output == manifest_path:
                raise ValueError("Use a .lean suffix for the generated definition")
            manifest = make_manifest(program, definition, output, args.namespace, language)
            write_new(output, definition)
            write_new(manifest_path, json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
            result = {"ok": True, "definition": str(output), "manifest": str(manifest_path),
                      "proof_status": "not_checked_here", "program": program.manifest()}
        else:
            task = load_task(args.task_id) if args.task_id is not None else json.loads(args.task_json.read_text())
            normalized = normalize_task(task)
            program, language = select_program(source, normalized["dimension"], normalized["qubits_per_cell"], args)
            binding = check_manifest(args.manifest, program, language) if args.manifest else None
            proof_result = None
            if args.proof:
                if language != "hybrid":
                    raise ValueError("--proof currently supports hybrid language")
                from src.hybrid_proof_verifier import verify
                proof_result = verify(source, args.proof, normalized["dimension"], normalized["qubits_per_cell"],
                                      bits=program.maths.maths.bits, terms=program.maths.maths.terms,
                                      timeout=args.proof_timeout)
                if proof_result.get("passed") is not True:
                    failed = {"passed": False, "proof_verification": proof_result,
                              "energy_density": None, "reason": "Proof was not verified; energy was not evaluated"}
                    if args.output:
                        write_new(args.output, json.dumps(failed, indent=2, ensure_ascii=False, allow_nan=False)+"\n")
                    print(json.dumps(failed, indent=2, ensure_ascii=False, allow_nan=False))
                    return 1
            if language == "rational":
                from src.correlator_rational_energy import evaluate_energy as rational_energy
                result = rational_energy(task, program.evaluate, f_manifest=program.manifest())
            else:
                result = evaluate_energy(task, program.evaluate, f_manifest=program.manifest())
            result["translation_manifest_matched"] = binding is not None
            if proof_result is not None:
                result["proof_verification"] = proof_result
                result["energy_evaluator_sha256"] = hashlib.sha256((PROJECT/"src/correlator_energy.py").read_bytes()).hexdigest()
                result["preconditions"] = {"legality": "verified_by_fixed_goal_kernel_replay",
                                           "translation_invariance": "verified_by_fixed_goal_kernel_replay",
                                           "numerical_preconditions": "verified_by_fixed_goal_kernel_replay",
                                           "proof_checked_here": True}
            if binding is not None:
                result["lean_definition_sha256"] = binding["definition_sha256"]
            if args.output:
                write_new(args.output, json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, TypeError, KeyError, IndexError, OSError, SyntaxError, ArithmeticError, RecursionError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
