"""One independent bounded-constructor/unbounded-formalizer GPT experiment."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import traceback

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
from revisions.physics_first_v1_1.run_campaign import atomic_json, digest, new_json, utc
from revisions.physics_first_v1_1.references import exploration_references
from revisions.hybrid_agent_v1.run_trial import public_references
from revisions.hybrid_agent_v1.local_numeric import LocalNumericalBroker
from revisions.hybrid_web_v1.run_gpt_trial import official_endpoint
from src.benchmark50_models import load_task

HERE = Path(__file__).resolve().parent
MODEL = "gpt-6.1-terra"
CONSTRUCTOR_TOOLS = ("list_files", "read", "write", "server", "create_handoff", "finish")
FORMALIZER_TOOLS = ("list_files", "read", "write", "prepare", "run_lean", "score", "report_issue", "finish")


def references(task: dict) -> tuple[dict, dict]:
    constructor = exploration_references(task)
    constructor["correlator_contract.md"] = """# Infinite-state contract
f(P) returns the real expectation of any canonical finite Pauli word. P is a
tuple of (cell_tuple,qubit_index,axis); axis X/Y/Z; identity is (). The submitted
proof must establish the normalized positive infinite state, invariance under
every cell translation, and the expression-domain obligations. The energy is
sum_j c_j*f(P_j)/Q, per qubit, and is returned by the official interface only
after the exact candidate passes the fixed Lean proof obligation. Numerical
exploration is provisional. State families are open; do not replace the task.
"""
    constructor["tools.md"] = constructor["dual_tools.md"] = """# Constructor tools
All local calls use benchmark_action {tool:NAME,arguments:{...}}.
list_files {}, read {path,start_line:1,max_lines:240},
write {path,content,append:false} use private relative files or read-only reference/.
server {candidate:"experiment.py",cpus:1,wall_seconds:120,arguments:[]} runs the
named private Python FILE in a local networkless numerical sandbox, not a server.
create_handoff {source_path:"source.py",proof_path:"proof.md"} freezes your f and
mathematical proof and pauses for formalization. No numerical pre-evaluation is
required. finish {reason:"..."} ends the constructor. No explore_energy tool.
"""
    formalizer = public_references(task)
    formalizer["dual_tools.md"] = """# Formalizer tools
list_files/read/write have the standard private-file interface; write only .lean
and .md. reference/handoff/source.py and reference/handoff/proof.md are immutable.
prepare {} generates the exact Lean definition and template for the frozen f.
run_lean {proof_path:"Candidate.lean"} or score {proof_path:"Candidate.lean"}
checks the original fixed goal and, only on success, computes and hands back energy.
source_path is optional; if supplied it must exactly match the frozen source.
report_issue {kind:"math_error"|"missing_argument"|"representation_error"|"infrastructure",
detail:"specific step and reason"} returns the problem to the constructor.
No server, web, alternate construction or parameter optimization is allowed.
Write formalization_map.md linking mathematical proof steps to Lean declarations.
"""
    formalizer["submission_api.md"] = formalizer["dual_tools.md"] + """
The fixed GeneratedState.ProofObligation is StateCondition D Q f AND
NumericalPreconditions. Read prepare's generated_paths. Import GeneratedState
and expose Submission.accepted of EXACTLY that type with module/public import/
@[expose] public section/noncomputable section as in the returned template.
Do not redefine GeneratedState, change trusted definitions, assume the result,
or introduce sorry/admit/new axioms. Generic state-library theorems are optional
formalization aids. source_path always denotes the unchanged current handoff.
Only the constructor can submit another source or mathematical proof.
"""
    formalizer.pop("scoring_rules.json", None)
    return constructor, formalizer


def client_for(endpoint: str, *, allow_web: bool):
    from revisions.dual_agent_v1.runtime import RoleResponsesClient
    return RoleResponsesClient(base_url=endpoint, api_key=os.environ.get("EUREKA_API_KEY", ""),
        allow_web=allow_web, timeout=600, connect_timeout=60, persistent_session=True,
        max_connect_retries=2, response_body_limit_bytes=16*1024**2,
        completion_token_field="max_completion_tokens",
        reasoning_usage_policy="provider_reported_completion")


def is_verified(value):
    return (isinstance(value, dict) and value.get("ok") is True
        and value.get("verified") is True and value.get("proof_verified") is True
        and type(value.get("energy_density")) in (int, float)
        and math.isfinite(value["energy_density"]))


def run_trial(task_id: int, out: Path, experiment_id: str) -> dict:
    from revisions.dual_agent_v1.runtime import RoleSession, RoleLimits
    from revisions.dual_agent_v1.tools import DualAgentTools
    from revisions.dual_agent_v1.run_campaign import source_bindings
    endpoint = official_endpoint()
    out = Path(os.path.abspath(out))
    if not out.is_relative_to(PROJECT) or out.parent.resolve(strict=True) != out.parent:
        raise ValueError("Fresh unaliased project-local trial directory required")
    out.mkdir(exist_ok=False, mode=0o700)
    task = load_task(task_id)
    constructor_refs, formalizer_refs = references(task)
    bindings = source_bindings()
    top = {"schema":"dual-agent-trial/v1", "created_utc":utc(), "model":MODEL,
        "status":"constructor_running", "experiment_id":experiment_id,
        "task_sha256":hashlib.sha256(json.dumps(task,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest(),
        "runtime_sources":bindings, "provenance":{"provider":"openai","official_api":True,
            "api_base_url":endpoint,"roles":["constructor","formalizer"],
            "constructor_network_tools":True,"formalizer_network_tools":False,
            "formalizer_total_token_limit":None,"formalizer_total_api_limit":None,
            "formalizer_total_wall_limit":None,"checker_seconds":300},
        "constructor_limits":asdict(RoleLimits.constructor()),
        "formalizer_limits":asdict(RoleLimits.formalizer()),
        "handoffs":[],"feedback":[],"best_verified_result":None,"verified_result":None}
    new_json(out/"task.json",task)
    atomic_json(out/"manifest.json",top)
    coordinator = DualAgentTools(task,out,constructor_references=constructor_refs,
        formalizer_references=formalizer_refs,server=LocalNumericalBroker(),timeout=300)
    clients = []
    constructor = None
    formalizers = []

    def progress(role):
        def report(kind,value):
            print(json.dumps({"created_utc":utc(),"role":role,"event":kind,
                **{k:value[k] for k in ("turn","tool","ok","error","action","energy_density")
                   if isinstance(value,dict) and k in value}},ensure_ascii=False),flush=True)
        return report

    def save():
        sessions = ([constructor] if constructor else []) + formalizers
        top["roles"] = [{"role":s.role,"manifest_path":str(s.run_dir/"manifest.json")}
                        for s in sessions]
        for key in ("turns","input_tokens_charged","output_tokens_charged","server_cpu_seconds_charged"):
            top[key] = sum(s.manifest.get(key,0) for s in sessions)
        top["constructor_usage"] = dict(constructor.manifest) if constructor else None
        top["formalizer_usage"] = {key:sum(s.manifest.get(key,0) for s in formalizers)
            for key in ("turns","input_tokens_charged","output_tokens_charged","server_cpu_seconds_charged")}
        top["handoffs"] = coordinator.handoffs
        top["last_updated_utc"] = utc()
        atomic_json(out/"manifest.json",top)

    try:
        client = client_for(endpoint,allow_web=True)
        clients.append(client)
        constructor = RoleSession(client,MODEL,"constructor",out/"constructor",
            references=constructor_refs,system_prompt=(HERE/"constructor_prompt.md").read_text(),
            limits=RoleLimits.constructor(),tools=CONSTRUCTOR_TOOLS,
            tool_handler=coordinator.constructor.dispatch,progress=progress("constructor"))
        constructor.add_user_message("Solve this one infinite-lattice task. Read reference/ first.\n"
            +json.dumps(task,ensure_ascii=False,indent=2))
        save()
        while True:
            top["status"] = "constructor_running"
            save()
            event = constructor.run_until_handoff()
            save()
            if event.get("action") != "handoff":
                top["status"] = ("completed" if top["best_verified_result"] else "final_submission_not_verified")
                top["constructor_terminal"] = event
                if event.get("session_status") in ("infrastructure_failure","provider_usage_exceeded_budget"):
                    top["status"] = "infrastructure_failure"
                break
            handoff = coordinator.handoff
            if not handoff:
                raise RuntimeError("handoff action has no frozen candidate")
            top["verified_result"] = None
            top["status"] = "formalizer_running"
            save()
            formal_client = client_for(endpoint,allow_web=False)
            clients.append(formal_client)
            broker = coordinator.formalizer
            formalizer = RoleSession(formal_client,MODEL,"formalizer",broker.run_dir,
                references=broker.references,system_prompt=(HERE/"formalizer_prompt.md").read_text(),
                limits=RoleLimits.formalizer(),tools=FORMALIZER_TOOLS,
                tool_handler=broker.dispatch,progress=progress("formalizer"))
            formalizers.append(formalizer)
            formalizer.add_user_message("Formalize ONLY this immutable constructor submission. Read "
                "reference/handoff/source.py, reference/handoff/proof.md and reference/dual_tools.md.\n"
                +json.dumps(handoff,ensure_ascii=False,indent=2))
            save()
            feedback = formalizer.run_until_handoff()
            formal_client.close()
            # Only host-verified checker results can populate scored endpoints.
            verified = broker.last_verified
            if is_verified(verified):
                if verified.get("source_sha256") != handoff.get("source_sha256"):
                    raise RuntimeError("Accepted source differs from immutable handoff")
                top["verified_result"] = verified
                best = top["best_verified_result"]
                if best is None or verified["energy_density"] < best["energy_density"]:
                    top["best_verified_result"] = verified
                response = {"kind":"verified_state_energy","handoff_id":handoff.get("handoff_id"),
                    "source_sha256":handoff["source_sha256"],"result":verified,
                    "instruction":"This exact f passed. Improve within your remaining budget or finish."}
            else:
                response = {"kind":"formalizer_feedback","handoff_id":handoff.get("handoff_id"),
                    "source_sha256":handoff["source_sha256"],"feedback":feedback,
                    "proof_verified":False,"energy_density":None,
                    "instruction":"You must repair mathematical/representation issues yourself. "
                        "No candidate has been accepted from this handoff."}
                if (feedback.get("session_status") == "infrastructure_failure"
                        or feedback.get("issue_kind") == "infrastructure"):
                    top["status"] = "infrastructure_failure"
                    top["formalizer_terminal"] = feedback
                    top["feedback"].append(response)
                    save()
                    break
            top["feedback"].append(response)
            new_json(out/f"feedback-{len(top['feedback']):04d}.json",response)
            constructor.add_user_message(json.dumps(response,ensure_ascii=False,indent=2))
            save()
    except Exception as error:
        top.update(status="infrastructure_failure",infrastructure_error=type(error).__name__+": "+str(error),
            traceback=traceback.format_exc())
    finally:
        for client in clients:
            client.close()
        top["ended_utc"] = utc()
        save()
    return top


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id",type=int,required=True)
    parser.add_argument("--model",choices=[MODEL],default=MODEL)
    parser.add_argument("--experiment-id",required=True)
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    result=run_trial(args.task_id,args.out,args.experiment_id)
    print(json.dumps({"status":result["status"],"out":str(args.out)},ensure_ascii=False),flush=True)
    return 1 if result["status"]=="infrastructure_failure" else 0


if __name__=="__main__":
    raise SystemExit(main())
