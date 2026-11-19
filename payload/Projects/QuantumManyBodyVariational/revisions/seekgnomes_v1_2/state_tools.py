"""Typed state-format handoffs, with one frozen source for proof and energy."""

from __future__ import annotations

import json
import math
import re
from signal import SIGABRT

from revisions.seekgnomes_v1_1_1.scoring import (
    public_reference_metadata, score_parameters, task_metrics,
)
from revisions.seekgnomes_v1_1_1.tools import (
    FORMALIZER_ACTIONS, DefinitionRoleBroker, DefinitionTools, MAX_FILE_BYTES, _json, _new,
    _regular_bytes, _relative, _sha,
)
from src.state_format import StateProgram, runtime_sha256

from .state_verifier import (
    BITS, KIND, LOCK, PRECISION, PRECISION_BITS, SAFE_MODULES, SPEC, TARGET, TERMS, THEOREM,
)

STATE_FORMALIST_ACTIONS = tuple(tool for tool in FORMALIZER_ACTIONS if tool != "finish")

_ACCEPTANCE_FLAGS = (
    "ok", "verified", "proof_verified", "compile_passed", "goal_matched", "kernel_verified",
)
_PROOF_DIGESTS = (
    "source_sha256", "candidate_source_sha256", "generated_state_sha256", "runtime_sha256", "lock_sha256",
)
_DIAGNOSTICS = (
    "error", "detail", "problems", "diagnostics", "proof_manifest", "technical_diagnostic",
    "diagnostic_path", "evidence_write_error", "retryable", "recoverable",
    "compile_passed", "goal_matched", "kernel_verified", "numerical_failure",
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_GENERIC_STAGES = {f"compile-generic-{module}" for module in SAFE_MODULES}
_THREAD_CREATION_FAILURE = b"lean::exception: failed to create thread: Resource temporarily unavailable"


def _lock_digest():
    return _sha(LOCK.read_bytes())


def _proof_verified(value):
    if not isinstance(value, dict) or any(value.get(key) is not True for key in _ACCEPTANCE_FLAGS):
        return False
    certificate = value.get("certificate")
    if not isinstance(certificate, dict):
        return False
    if (certificate.get("kind") != KIND or certificate.get("target") != TARGET
            or certificate.get("theorem") != THEOREM):
        return False
    for key in (*_PROOF_DIGESTS, "candidate_export_sha256"):
        digest = value.get(key)
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest) or certificate.get(key) != digest:
            return False
    try:
        return (value["lock_sha256"] == _lock_digest()
                and value["runtime_sha256"] == runtime_sha256())
    except (OSError, ValueError):
        return False


def is_verified(value):
    """Recognize final score receipts for the frozen state and submitted proof."""
    if not _proof_verified(value):
        return False
    energy = value.get("energy_density")
    energy_digest = value.get("energy_report_sha256")
    try:
        return (type(energy) in (float, int) and math.isfinite(energy)
                and value.get("energy_mode") == "approximate_float"
                and value.get("energy_normalization") == "per_qubit"
                and isinstance(value.get("energy_report"), str) and bool(value["energy_report"])
                and isinstance(energy_digest, str) and bool(_DIGEST.fullmatch(energy_digest))
                and value.get("definition_sha256") == value["source_sha256"]
                and value.get("proof_sha256") == value["candidate_source_sha256"])
    except OverflowError:
        return False


def _failed_check(result):
    """Expose verifier diagnostics without allowing an unaccepted energy into feedback."""
    failure = {key: result[key] for key in _DIAGNOSTICS if key in result}
    diagnostic = failure.get("technical_diagnostic")
    if isinstance(diagnostic, dict) and diagnostic.get("kind") == "strict_verifier_environment_failure":
        # Reconstruct this diagnostic from trusted verifier evidence below.
        failure.pop("technical_diagnostic")
    return {**failure, "ok": False, "verified": False, "proof_verified": False}


def _transient_thread_failure(stage, check, evidence):
    if (stage not in _GENERIC_STAGES or check.get("timed_out") is not False
            or check.get("exit_code") not in (-SIGABRT, 128 + SIGABRT)):
        return False
    log_path = evidence / f"{stage}.log"
    if check.get("log") != str(log_path):
        return False
    try:
        log = _regular_bytes(log_path, MAX_FILE_BYTES)
    except (OSError, ValueError):
        return False
    return _sha(log) == check.get("log_sha256") and _THREAD_CREATION_FAILURE in log


def _trusted_environment_failure(result, invocation):
    path = invocation / "verification" / "manifest.json"
    if result.get("proof_manifest") != str(path):
        return None
    try:
        report = json.loads(_regular_bytes(path, MAX_FILE_BYTES))
    except (OSError, ValueError):
        return None
    if (not isinstance(report, dict) or report.get("spec") != SPEC
            or report.get("manifest") != str(path) or report.get("passed") is not False):
        return None
    checks, problems = report.get("checks"), report.get("problems")
    if not isinstance(checks, dict) or not isinstance(problems, list):
        return None

    for stage, check in checks.items():
        if isinstance(check, dict) and check.get("passed") is False and (
            stage in {"lock-before", "lock-after", "mathlib-revision", "mathlib-clean"}
            or stage in _GENERIC_STAGES
        ):
            break
    else:
        # Git can succeed while returning a revision that fails the pin.
        check = checks.get("mathlib-revision")
        if (problems != ["mathlib revision differs from frozen revision"]
                or not isinstance(check, dict) or check.get("passed") is not True
                or "mathlib-clean" in checks):
            return None
        stage = "mathlib-revision"
    diagnostic = {
        "kind": "strict_verifier_environment_failure",
        "stage": stage,
        "reason": (problems[0] if problems and isinstance(problems[0], str)
                   else f"Trusted verifier stage failed: {stage}"),
        "timed_out": check.get("timed_out") is True,
    }
    if _transient_thread_failure(stage, check, path.parent):
        diagnostic["retryable"] = True
    return diagnostic


class StateDefinitionTools(DefinitionTools):
    """Use the legacy transaction/storage machinery for a complete textual .state source."""

    handoff_schema = "seekgnomes-state-handoff/v1"
    definition_filename = "f.state"
    private_return_filenames = ()

    def __init__(self, task, trial_dir, *, callback_factory=None, **kwargs):
        from . import state_callbacks

        super().__init__(task, trial_dir, callback_factory=callback_factory or state_callbacks.callbacks, **kwargs)

    def _role_broker(self, role, path, **kwargs):
        return StateDefinitionRoleBroker(self, role, path, **kwargs)

    def _validate_definition(self, definition):
        super()._validate_definition(definition)
        StateProgram(definition.decode(), self.task["dimension"], self.task["qubits_per_cell"],
                     bits=BITS, terms=TERMS, precision_bits=PRECISION_BITS)


class StateDefinitionRoleBroker(DefinitionRoleBroker):
    formalizer_suffixes = {".lean", ".md"}
    formalizer_write_error = "Formalist may write Lean proofs and Markdown issue reports"

    def dispatch(self, action, session=None):
        if self.role == "formalizer" and isinstance(action, dict):
            tool, args = action.get("tool"), action.get("arguments")
            if tool == "finish" or (
                tool == "report_issue" and isinstance(args, dict)
                and args.get("kind") in ("unresolved", "infrastructure")
            ):
                return {
                    "ok": False,
                    "error": "formalist_proof_required",
                    "detail": "Continue proving the frozen state and submit it with score. "
                              "Use report_issue only for concrete defects in the frozen definition.",
                    "proof_verified": False,
                }
        return super().dispatch(action, session)

    def _formal(self, tool, args):
        if tool == "prepare":
            self._keys(args, set())
            proof = None
        else:
            self._keys(args, set(), {"proof", "proof_path"})
            if len(args) != 1:
                raise ValueError("A check requires exactly one Lean proof path")
            name = next(iter(args.values()))
            if _relative(name).suffix != ".lean":
                raise ValueError("Expected a .lean proof file")
            proof = self._read_private(name)

        source, notes = self.owner._assert_frozen(self._handoff)
        result = self._formal_attempt(tool, source, notes, proof)
        diagnostic = result.get("technical_diagnostic")
        if (proof is not None and isinstance(diagnostic, dict)
                and diagnostic.get("kind") == "strict_verifier_environment_failure"
                and diagnostic.get("retryable") is True):
            # Keep both invocation records. The role's editable proof path
            # need not still contain the bytes that were originally submitted.
            return self._formal_attempt(tool, source, notes, proof, retry_of=result["invocation_dir"])
        return result

    def _formal_attempt(self, tool, source, notes, proof, *, retry_of=None):
        invocation = self._invocation(tool)
        source_path = invocation / self.owner.definition_filename
        _new(source_path, source)
        _new(invocation / "notes.md", notes)
        binding = {
            "handoff_id": self._handoff.index,
            "definition_sha256": self._handoff.definition_sha256,
            "source_sha256": self._handoff.definition_sha256,
            "runtime_sha256": runtime_sha256(),
            "lock_sha256": _lock_digest(),
        }
        if retry_of is not None:
            binding["retry_of_invocation"] = retry_of
        if proof is not None:
            proof_path = invocation / "Candidate.lean"
            _new(proof_path, proof)
            proof_digest = _sha(proof)
            binding.update(candidate_source_sha256=proof_digest, proof_sha256=proof_digest,
                           invocation_dir=str(invocation), source_path=str(source_path), proof_path=str(proof_path))
        prepared = self._callback(self.owner.prepare_callback, invocation, source_path=source_path)
        if prepared.get("ok") is not True:
            failure = {**_failed_check(prepared), **binding}
            _new(invocation / "result.json", _json(failure))
            return failure
        self._check_prepared(prepared, binding)

        references = {}
        for key, filename in (("generated_state", "GeneratedState.lean"),
                              ("proof_template", "Candidate.lean.txt")):
            name = f"generated/{binding['source_sha256']}/{filename}"
            contents = prepared[key]
            _new(invocation / filename, contents.encode())
            self.generated[name] = contents
            references[key] = name
        files = {self.owner.definition_filename: source, "GeneratedState.lean": prepared["generated_state"].encode()}

        if proof is None:
            result = {"ok": True, "proof_verified": False, "generated_paths": references,
                      "target": f"{THEOREM} : {TARGET}", "dimension": self.owner.task["dimension"],
                      "qubits_per_cell": self.owner.task["qubits_per_cell"], **PRECISION}
        else:
            files["Candidate.lean"] = proof
            result = self._check_proof(tool, invocation, source_path, proof_path, binding)

        for filename, contents in files.items():
            if _regular_bytes(invocation / filename, MAX_FILE_BYTES) != contents:
                raise ValueError("A frozen proof or state input changed during verification")
        result.update(binding)
        with self.owner._lock():
            self.owner._recover()
            self.owner._assert_frozen(self._handoff)
            _new(invocation / "result.json", _json(result))
            self.return_files = files
            if result.get("action") == "feedback" and is_verified(result):
                self.last_verified = dict(result)
                previous = self.owner.last_verified
                if previous is None or result["energy_density"] < previous["energy_density"]:
                    self.owner.last_verified = dict(result)
        return result

    @staticmethod
    def _check_prepared(prepared, binding):
        for key in ("generated_state", "proof_template"):
            contents = prepared.get(key)
            if not isinstance(contents, str) or len(contents.encode()) > MAX_FILE_BYTES:
                raise ValueError("Invalid host-generated Lean reference")
        binding["generated_state_sha256"] = _sha(prepared["generated_state"].encode())
        for key in ("source_sha256", "generated_state_sha256", "runtime_sha256", "lock_sha256"):
            if prepared.get(key) != binding[key]:
                raise ValueError(f"Host-generated Lean reference has a mismatched {key}")

    def _check_proof(self, tool, invocation, source_path, proof_path, binding):
        callback = self.owner.score_callback if tool == "score" else self.owner.lean_callback
        result = self._callback(callback, invocation, source_path=source_path, proof_path=proof_path)
        if not _proof_verified(result):
            failure = _failed_check(result)
            technical = _trusted_environment_failure(result, invocation)
            if technical is not None:
                failure["technical_diagnostic"] = technical
            return failure
        certificate = result["certificate"]
        if (any(result[key] != binding[key] for key in _PROOF_DIGESTS)
                or certificate.get("dimension") != self.owner.task["dimension"]
                or certificate.get("qubits_per_cell") != self.owner.task["qubits_per_cell"]):
            return _failed_check({**result, "error": "proof_binding_mismatch"})

        if tool == "run_lean":
            return {**{key: True for key in _ACCEPTANCE_FLAGS}, "certificate": certificate,
                    **{key: result[key] for key in ("candidate_export_sha256", "diagnostics", "proof_manifest")
                       if key in result}}

        result = {**result, **binding}
        result["energy_report_sha256"] = self._check_energy_report(result, invocation)
        if not is_verified(result):
            return _failed_check({**result, "error": "missing_verified_energy"})
        if "numerical_normalized_gain" in result:
            result["legacy_scale_normalized_gain"] = result.pop("numerical_normalized_gain")
            result["legacy_scale_normalized_gain_meaning"] = (
                "max(0,(B-E)/scale); legacy diagnostic, not current score amplitude")
        try:
            metrics = task_metrics(result["energy_density"], **score_parameters(self.owner.task))
        except (ValueError, KeyError, OSError, ArithmeticError) as error:
            metrics = {"pass_rate": 1.0, "improvement_rate": None, "amplitude": None, "score": None,
                       "score_complete": False, "score_status": "scoring_error", "scoring_error": str(error)}
        _new(invocation / "score-components.json", _json(metrics))
        result["score_components"] = {**metrics, "ground_energy_reference": public_reference_metadata(
            metrics.get("ground_energy_reference"))}
        result["score_rule"] = ("30*P+30*R+40*A; exact a=clamp(1-(E-e0)/s,0,1); "
                                "generic a=baseline_gain=clamp((B-E)/(B-L),0,1); "
                                "R=mean(baseline_gain>=0.01); groups scored separately")
        return {**result, "action": "feedback", "issue_kind": None}

    @staticmethod
    def _check_energy_report(result, invocation):
        """Read only the host report for this verification invocation."""
        path = invocation / "energy.json"
        if result.get("energy_report") != str(path):
            raise ValueError("The energy report is outside this verification invocation")
        data = _regular_bytes(path, MAX_FILE_BYTES)
        report = json.loads(data)
        verification = report["verification"]
        if (report.get("energy_is_llm_reported") is not False
                or report["preconditions"].get("proof_checked_here") is not True
                or report.get("energy_density") != result.get("energy_density")
                or report.get("task_physics_sha256") != result.get("task_physics_sha256")
                or report.get("mode") != result.get("energy_mode")
                or report.get("energy_normalization") != result.get("energy_normalization")
                or any(verification.get(key) != result[key] for key in _PROOF_DIGESTS)):
            raise ValueError("The energy report differs from the verified state or score receipt")
        return _sha(data)


__all__ = ["STATE_FORMALIST_ACTIONS", "StateDefinitionTools", "is_verified"]
