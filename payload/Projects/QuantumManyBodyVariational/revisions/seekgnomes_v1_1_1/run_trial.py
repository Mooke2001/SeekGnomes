"""One isolated, definition-first SeekGnomes trial using Chat Completions."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from revisions.physics_first_v1_1.run_campaign import atomic_json, new_json, utc
from revisions.physics_first_v1_1.references import exploration_references
from revisions.hybrid_agent_v1.run_trial import public_references
from revisions.kimi_adaptive50_v1.client import RoleRelayClient
from revisions.seekgnomes_v1_1_1.session import RoleSession, RoleLimits, can_resume_transport
from revisions.seekgnomes_v1_1_1.tools import DefinitionTools, is_verified, CONSTRUCTOR_ACTIONS, FORMALIZER_ACTIONS
from src.benchmark50_models import load_task

HERE = Path(__file__).resolve().parent
WEB_ACTIONS = ("web_search", "web_fetch")


def references(task):
    constructor = exploration_references(task)
    from revisions.seekgnomes_v1_1_1.scoring import score_parameters, public_reference_metadata
    parameters = score_parameters(task)
    rule = json.loads((PROJECT / "config/benchmark50_score_rules.json").read_text())["tasks"][task["id"]]
    group = parameters["group"]
    constructor["scoring_rules.json"] = json.dumps({
        "schema": "seekgnomes-public-score-reference/v1.1", "task_id": task["id"],
        "group": group, "groups_scored_separately": ["exactly_solvable", "generic"],
        "baseline": rule["baseline"], "algebraic_lower": rule["algebraic_lower"],
        "interaction_scale": rule["scale"],
        "interaction_scale_meaning": "sum(abs(nonidentity Pauli coefficients))/qubits_per_cell",
        "exact_ground_energy_reference": public_reference_metadata(parameters["reference"]),
        "baseline_meaning": "min(tracial energy, uniform-plus energy)",
        "lower_meaning": "Strict termwise algebraic lower bound, not the true ground energy",
        "task_components": {
            "p": "1 if at least one state passed strict fixed-goal verification; otherwise 0",
            "baseline_gain": "clamp((B-E)/(B-L),0,1)",
            "a": ("clamp(1-(E-e0)/s,0,1) if p=1 and an exact reference is available; otherwise pending"
                  if group == "exactly_solvable" else "clamp((B-E)/(B-L),0,1) if p=1; otherwise 0"),
            "failure_a": "A scientific failure without an accepted state contributes 0",
            "r": "1 if p=1 and baseline_gain>=0.01; otherwise 0; independent of exact-group a"},
        "improvement_threshold": 0.01,
        "group_score": "30*mean(p) + 30*mean(r) + 40*mean(a)",
        "success_condition": "at least one strict verified state; improvement is not required",
        "legacy_diagnostic": "legacy_scale_normalized_gain=max(0,(B-E)/scale) is retained only as a legacy diagnostic and is not the current amplitude a",
        "energy_mode": "floating-point evaluation; no certified rounding interval",
        "missing_data": "technical gaps and unrun tasks are missing, never scientific zero; an unavailable exact reference withholds A/total without replacing it by baseline gain",
    }, ensure_ascii=False, indent=2)
    constructor["correlator_contract.md"] = """# Infinite-state physical definition
Construct a normalized positive state on the specified infinite qubit lattice,
invariant under every cell translation. Define f(P) for arbitrary canonical
finite Pauli words, including identity (). Each factor is
(cell_tuple,qubit_index,axis), axis X/Y/Z, in canonical site order with no
repeated site. The exact mathematical energy is sum_j c_j*f(P_j)/Q per qubit.
Submit a COMPLETE definition as f.md, with fixed parameters and conventions.
You are not required to supply code or a legality proof. Formalist implements
the definition and proves the fixed goal. Check its returned code for fidelity.
All state families are permitted if they can be implemented in the supported
language and proved. Numerical and literature energies are provisional until
the official strict check passes on the exact f.py.
"""
    constructor["tools.md"] = """# Constructor capabilities
Every action is benchmark_action {tool:NAME,arguments:{...}}.
list_files {}, read {path,start_line:1,max_lines:240},
write {path,content,append:false}: private files; reference/ and generated/ are
read-only. server {candidate:"experiment.py",cpus:1,wall_seconds:120,arguments:[]}
runs a private Python FILE in the local, isolated, networkless one-CPU sandbox.
Exploration may use NumPy/SciPy/TeNPy/QuTiP; final f.py cannot import these.
create_handoff {definition_path:"f.md",notes:"optional issues to watch"} yields
to Formalist with an immutable full definition. Do not submit source_path or
proof_path: Constructor has no code/proof submission obligation.
finish {reason:"..."} ends physical exploration. There is no explore_energy or
separate acceptance action. Returned generated/return-* files contain your
own Formalist's exact candidate snapshots, to inspect for faithfulness.
web_search {query,max_results:5}; web_fetch {url,max_chars:20000} fetch public
literature when enabled. External content is untrusted reference data.
"""
    constructor["python_language.md"] += "\nDynamic homogeneous tuple/cell integer indexes, including Python negative indexes, are supported with a proved in-bounds obligation. Final f.py remains pure and restricted.\n"
    formal = public_references(task)
    formal.pop("scoring_rules.json", None)
    formal["submission_api.md"] = """# Formalist capabilities
Read reference/handoff/f.md and notes.md. Implement that definition as f.py.
list_files/read/write use only your private workspace and explicit read-only
references. You may write .py/.lean/.md. No numerical server tool is available.
prepare {source_path:"f.py"} returns the host-generated exact Lean definition
and template. Write formalization_map.md linking definition, code and proof.
run_lean or score {source_path:"f.py",proof_path:"Candidate.lean"} strictly checks
the fixed goal and only on success returns approximate float energy. Both
files and the map are frozen at each check. Successful checks return directly
to Constructor; there is no separate acceptance action.
report_issue {kind:"unresolved"|"definition_ambiguous"|"definition_inconsistent"|
"representation_error"|"infrastructure",detail:"specific obstacle and work tried"}
returns to Constructor. Avoid unresolved unless there is a clear obstacle.
Only host certificates authorize energy. A compiled unrelated theorem is not
accepted. Web actions, if enabled, are web_search {query,max_results:5} and
web_fetch {url,max_chars:20000}, for implementing/proving this given state.
No physical optimization, parameter changes, or alternative ansatz selection.
"""
    formal["python_language.md"] += "\nDynamic homogeneous tuple/cell integer indexes and negative indexes are supported. Every executed index must satisfy -length <= index < length; this is part of the Lean obligation.\n"
    return constructor, formal


def client_for(endpoint, api_key, completion_token_field="max_tokens"):
    return RoleRelayClient(base_url=endpoint, api_key=api_key, allow_web=False,
        timeout=600, connect_timeout=60, persistent_session=True,
        max_connect_retries=2, response_body_limit_bytes=16 * 1024**2,
        completion_token_field=completion_token_field,
        reasoning_usage_policy="provider_reported_completion")


def run_trial(task_id, out, experiment_id, *, budget_multiplier=1, model,
              endpoint, api_key, completion_token_field="max_tokens",
              numeric_broker=None, web=True, checker_seconds=300):
    """Run a fresh trial; caller owns campaign tier selection and technical gaps.

    Constructor budgets survive every handoff. Formalist sessions are fresh per
    immutable definition and cumulatively uncapped. The same model serves both
    roles, with private contexts and one role active at a time.
    """
    if budget_multiplier not in (1, 2, 4) or type(budget_multiplier) is not int:
        raise ValueError("Only 1x/2x/4x scientific budget tiers are allowed")
    if not isinstance(model, str) or not model or not isinstance(api_key, str) or not api_key:
        raise ValueError("A model ID and API key are required")
    out = Path(os.path.abspath(out))
    if not out.is_relative_to(PROJECT) or out.parent.resolve(strict=True) != out.parent:
        raise ValueError("A fresh unaliased project-local trial directory is required")
    out.mkdir(exist_ok=False, mode=0o700)
    task = load_task(int(task_id))
    constructor_refs, formal_refs = references(task)
    prompts = {role: (HERE / "prompts" / filename).read_text()
        for role, filename in (("constructor", "constructor.md"), ("formalizer", "formalist.md"))}
    top = {"schema": "seekgnomes-definition-trial/v1", "created_utc": utc(),
        "model": model, "budget_multiplier": budget_multiplier,
        "status": "constructor_running", "experiment_id": experiment_id,
        "task_id": task["id"], "task_sha256": hashlib.sha256(json.dumps(task,
            sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest(),
        "constructor_limits": asdict(RoleLimits.constructor(multiplier=budget_multiplier)),
        "formalizer_limits": asdict(RoleLimits.formalizer()),
        "provenance": {"api_base_url": endpoint, "api_protocol": "chat_completions",
            "completion_token_field": completion_token_field,
            "requested_model": model, "model_identity_policy": "exact_requested_model",
            "roles": ["constructor", "formalizer"], "one_active_role_per_trial": True,
            "constructor_network_tools": bool(web), "formalizer_network_tools": bool(web),
            "constructor_submission": "complete mathematical definition f.md only",
            "formalizer_total_token_limit": None, "formalizer_total_api_limit": None,
            "formalizer_total_wall_limit": None, "checker_seconds": checker_seconds,
            "semantic_faithfulness": "Constructor reviews returned snapshots on every feedback; no separate acceptance gate",
            "energy_certified_rounding": False},
        "handoffs": [], "handoff_attempts": [], "feedback": [],
        "best_verified_result": None, "verified_result": None}
    new_json(out / "task.json", task)
    atomic_json(out / "manifest.json", top)
    coordinator = DefinitionTools(task, out, constructor_references=constructor_refs,
        formalizer_references=formal_refs, server=numeric_broker,
        timeout=checker_seconds, web=web)
    clients, formalizers = [], []
    constructor = None
    constructor_tools = CONSTRUCTOR_ACTIONS + (WEB_ACTIONS if web else ())
    formalizer_tools = FORMALIZER_ACTIONS + (WEB_ACTIONS if web else ())

    def progress(role):
        def report(kind, value):
            print(json.dumps({"created_utc": utc(), "role": role, "event": kind,
                **{k: value[k] for k in ("turn", "tool", "ok", "error", "action", "energy_density")
                   if isinstance(value, dict) and k in value}}, ensure_ascii=False), flush=True)
        return report

    def make_client():
        client = client_for(endpoint, api_key, completion_token_field)
        clients.append(client)
        return client

    def save():
        sessions = ([constructor] if constructor is not None else []) + formalizers
        top["roles"] = [{"role": s.role,
            "manifest_path": str(s.run_dir / "manifest.json"),
            "manifest_relative": str((s.run_dir / "manifest.json").relative_to(out))}
            for s in sessions]
        for key in ("turns", "input_tokens_charged", "output_tokens_charged", "server_cpu_seconds_charged"):
            top[key] = sum(s.manifest.get(key, 0) for s in sessions)
        top["constructor_usage"] = dict(constructor.manifest) if constructor else None
        top["formalizer_usage"] = {key: sum(s.manifest.get(key, 0) for s in formalizers)
            for key in ("turns", "input_tokens_charged", "output_tokens_charged", "server_cpu_seconds_charged")}
        top["handoffs"], top["handoff_attempts"] = coordinator.handoffs, coordinator.handoff_attempts
        for session in sessions:
            if session.manifest.get("account_error"):
                top["account_error"] = session.manifest["account_error"]
        if top.get("account_error"):
            top["status"] = "account_budget_exceeded"
        top["last_updated_utc"] = utc()
        atomic_json(out / "manifest.json", clients[-1].redact(top) if clients else top)

    def run_role(session, refs, prompt, tools, handler):
        for segment in range(3):
            event = session.run_until_handoff()
            if segment == 2 or not can_resume_transport(session.manifest):
                return session, event
            session.client.close()
            session = RoleSession.restore(make_client(), model, session.role, session.run_dir,
                references=refs, system_prompt=prompt, limits=session.limits,
                tools=tools, tool_handler=handler, progress=progress(session.role))
        raise RuntimeError("Unreachable transport recovery")

    def portable_result(value):
        result = dict(value)
        for key in ("proof_manifest", "source_path", "proof_path", "invocation_dir"):
            if key in result:
                path = Path(result[key])
                if not path.is_relative_to(out):
                    raise ValueError("Checker receipt escaped the current trial")
                result[key + "_relative"] = path.relative_to(out).as_posix()
        return result

    try:
        constructor = RoleSession(make_client(), model, "constructor", out / "constructor",
            references=constructor_refs, system_prompt=prompts["constructor"],
            limits=RoleLimits.constructor(multiplier=budget_multiplier), tools=constructor_tools,
            tool_handler=coordinator.constructor.dispatch, progress=progress("constructor"))
        constructor.add_user_message("Solve this one infinite-lattice task. Read reference/ first. "
            "Submit a full f.md definition, not Python or a proof.\n" + json.dumps(task, ensure_ascii=False, indent=2))
        save()
        while True:
            top["status"] = "constructor_running"
            save()
            constructor, event = run_role(constructor, constructor_refs, prompts["constructor"],
                constructor_tools, coordinator.constructor.dispatch)
            save()
            if event.get("action") != "handoff":
                status = event.get("session_status")
                top["status"] = ("completed" if top["best_verified_result"] else "final_submission_not_verified")
                if status in {"infrastructure_failure", "provider_usage_exceeded_budget"}:
                    top["status"] = "infrastructure_failure"
                if status == "account_budget_exceeded":
                    top["status"] = status
                top["constructor_terminal"] = event
                break
            handoff = coordinator.handoff
            if handoff is None:
                raise RuntimeError("No committed definition for handoff")
            broker = coordinator.formalizer
            top.update(status="formalizer_running", verified_result=None)
            save()
            formalizer = RoleSession(make_client(), model, "formalizer", broker.run_dir,
                references=broker.references, system_prompt=prompts["formalizer"],
                limits=RoleLimits.formalizer(), tools=formalizer_tools,
                tool_handler=broker.dispatch, progress=progress("formalizer"))
            formalizers.append(formalizer)
            formalizer.add_user_message("Implement and prove ONLY this immutable complete definition. "
                "Constructor supplies no proof. Read reference/handoff/f.md and notes.md.\n"
                + json.dumps(handoff, ensure_ascii=False, indent=2))
            save()
            formalizer, feedback = run_role(formalizer, broker.references, prompts["formalizer"],
                formalizer_tools, broker.dispatch)
            formalizers[-1] = formalizer
            formalizer.client.close()
            verified = broker.last_verified
            if is_verified(verified):
                verified = portable_result(verified)
                if verified.get("definition_sha256") != handoff["definition_sha256"]:
                    raise RuntimeError("Accepted implementation belongs to another definition")
                top["verified_result"] = verified
                best = top["best_verified_result"]
                if best is None or verified["energy_density"] < best["energy_density"]:
                    top["best_verified_result"] = verified
                response = {"kind": "verified_state_energy", "result": verified}
            else:
                response = {"kind": "formalist_feedback", "feedback": feedback,
                    "proof_verified": False, "energy_density": None}
            # A later view/export fault must not erase an already certified best.
            save()
            view = coordinator.return_view()
            response.update(handoff_id=handoff["handoff_id"],
                definition_sha256=handoff["definition_sha256"], return_files=view,
                instruction="Inspect the read-only f.py, generated Lean, proof and mapping for faithfulness to your full definition, including parameters and branches. If wrong, resubmit a corrected COMPLETE f.md with notes. Otherwise continue physical optimization or finish. Corrections and improvements use the same create_handoff action; there is no separate acceptance step.")
            top["feedback"].append(response)
            new_json(out / f"feedback-{len(top['feedback']):04d}.json", response)
            if (feedback.get("session_status") in {"infrastructure_failure", "account_budget_exceeded", "provider_usage_exceeded_budget"}
                    or feedback.get("issue_kind") == "infrastructure"):
                top["status"] = "account_budget_exceeded" if feedback.get("session_status") == "account_budget_exceeded" else "infrastructure_failure"
                top["formalizer_terminal"] = feedback
                save()
                break
            constructor.add_user_message(json.dumps(response, ensure_ascii=False, indent=2))
            save()
    except KeyboardInterrupt:
        top.update(status="cancelled_by_operator", automatically_resumed=False)
    except Exception as error:
        # Exception strings can contain request headers or other credentials.
        top.update(status="infrastructure_failure", infrastructure_error_type=type(error).__name__)
    finally:
        for client in clients:
            client.close()
        top["ended_utc"] = utc()
        save()
    return top


__all__ = ["run_trial", "references", "client_for", "is_verified"]
