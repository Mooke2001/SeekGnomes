"""Supervise three independent dual-agent GPT trials, without formalizer caps."""
from __future__ import annotations
import argparse
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

PROJECT=Path(__file__).resolve().parents[2]
EUREKA=PROJECT.parents[1]
sys.path.insert(0,str(PROJECT))
from revisions.physics_first_v1_1.run_campaign import (
    atomic_json,digest,new_json,utc,source_bindings as old_bindings,source_drift)
from src.benchmark50_models import load_task

MODEL="gpt-6.1-terra"
IDS=("01","13","25")
HERE=Path(__file__).resolve().parent


def source_bindings():
    bindings=old_bindings()
    for name in ("__init__.py","runtime.py","tools.py","run_trial.py","run_campaign.py",
                 "constructor_prompt.md","formalizer_prompt.md","PROTOCOL.md"):
        path=HERE/name
        if path.resolve(strict=True)!=path:
            raise ValueError("Aliased experiment source")
        bindings[str(path.relative_to(PROJECT))]=digest(path)
    return bindings


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError,ValueError):
        return None


def accepted(value):
    from revisions.dual_agent_v1.run_trial import is_verified
    return value if is_verified(value) else None


def summary(out,plan,queue):
    rules=json.loads((PROJECT/"config/benchmark50_score_rules.json").read_text())["tasks"]
    rows=[]
    for entry in queue["entries"]:
        manifest=read_json(Path(entry["trial_dir"])/"manifest.json") or {}
        roles=[]
        # Role receipts continue to update while a constructor/formalizer is active.
        role_paths=[Path(entry["trial_dir"])/"constructor/manifest.json"]
        role_paths.extend(sorted(Path(entry["trial_dir"]).glob("formalizer/handoff-*/manifest.json")))
        for path in role_paths:
            data=read_json(path) or {}
            if data.get("role") in ("constructor","formalizer"):
                roles.append({"role":data["role"],"path":str(path),"status":data.get("status"),
                    **{k:data.get(k,0) for k in ("turns","input_tokens_charged","output_tokens_charged")}})
        rule=rules[entry["task_id"]]
        best=accepted(manifest.get("best_verified_result"))
        final=accepted(manifest.get("verified_result"))
        row={"task_id":entry["task_id"],"task_name":load_task(int(entry["task_id"]))["name"],
            "process_status":entry["status"],"runtime_status":manifest.get("status"),
            "best_verified":best is not None,"final_verified":final is not None,
            "best_energy_per_qubit":best["energy_density"] if best else None,
            "final_energy_per_qubit":final["energy_density"] if final else None,
            "baseline":float(Fraction(rule["baseline"])),"roles":roles,
            "handoffs":len(manifest.get("handoffs",[])),"feedback":len(manifest.get("feedback",[])),
            "manifest":str(Path(entry["trial_dir"])/"manifest.json")}
        row["baseline_improved"]=(best is not None and best["energy_density"]<row["baseline"]-1e-10)
        rows.append(row)
    result={"schema":"dual-agent-campaign-summary/v1","created_utc":utc(),
        "model":MODEL,"requested_trials":3,"rows":rows,
        "scientific_terminal_count":sum(e["status"]=="scientific_terminal" for e in queue["entries"]),
        "best_verified_count":sum(r["best_verified"] for r in rows),
        "baseline_improvement_count":sum(r["baseline_improved"] for r in rows),
        "source_drift":source_drift(plan["source_sha256"]),
        "interpretation":"state legality kernel-verified; energy approximate float; no formalizer token cap"}
    atomic_json(out/"summary.json",result)
    lines=["# 双agent三题运行记录","",f"更新(UTC): {result['created_utc']}","",
        f"科学终态: {result['scientific_terminal_count']}/3. best合法态: {result['best_verified_count']}/3.","",
        "| 题目 | 进程状态 | 当前阶段 | best能量/qubit | 基线 | 交接数 |",
        "|---|---|---|---:|---:|---:|"]
    for r in rows:
        lines.append(f"| {r['task_id']} {r['task_name']} | {r['process_status']} | {r['runtime_status']} | {r['best_energy_per_qubit']} | {r['baseline']} | {r['handoffs']} |")
    (out/"SUMMARY.md").write_text("\n".join(lines)+"\n")
    return result


def run(out,parallel=2):
    out=Path(os.path.abspath(out))
    if out.parent.resolve(strict=True)!=out.parent or not out.is_relative_to(PROJECT):
        raise ValueError("Fresh unaliased project-local campaign required")
    out.mkdir(exist_ok=False,mode=0o700)
    bindings=source_bindings()
    entries=[]
    for task_id in IDS:
        task=load_task(int(task_id))
        entries.append({"task_id":task_id,"status":"pending",
            "trial_dir":str(out/f"gpt-task{task_id}-trial1"),
            "task_sha256":hashlib.sha256(json.dumps(task,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()})
    plan={"schema":"dual-agent-plan/v1","created_utc":utc(),"experiment_id":out.name,
        "model":MODEL,"provider":"openai","official_api":True,"task_ids":list(IDS),
        "parallel":parallel,"source_sha256":bindings,
        "constructor_limits":{"output":128000,"input":2000000,"api_attempts":64,
            "active_wall_seconds":14400,"numeric_cpu_seconds_reserved":3600},
        "formalizer_limits":{"output":None,"input":None,"api_attempts":None,"total_wall_seconds":None},
        "selection_basis":"01/13/25 selected from prior five before new answers; no old answers supplied",
        "protocol":"revisions/dual_agent_v1/PROTOCOL.md"}
    new_json(out/"plan.json",plan)
    queue={"schema":"dual-agent-queue/v1","created_utc":utc(),"status":"running","entries":entries,
        "plan_sha256":digest(out/"plan.json")}
    atomic_json(out/"queue.json",queue)
    live={}
    stopped=[False]
    def stop(signum,frame):
        stopped[0]=True
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    try:
        while not stopped[0]:
            drift=source_drift(bindings)
            if drift:
                queue.update(status="source_drift",source_drift=drift)
                break
            for key,(process,entry,stdout,stderr) in list(live.items()):
                code=process.poll()
                if code is None:
                    continue
                stdout.close();stderr.close()
                manifest=read_json(Path(entry["trial_dir"])/"manifest.json") or {}
                binding_ok=(manifest.get("model")==MODEL and manifest.get("task_sha256")==entry["task_sha256"]
                    and manifest.get("runtime_sources")==bindings)
                entry.update(ended_utc=utc(),returncode=code,
                    status="scientific_terminal" if binding_ok and code==0 and manifest.get("status") in
                        ("completed","final_submission_not_verified") else "technical_gap")
                del live[key]
            for entry in entries:
                if len(live)>=parallel or entry["status"]!="pending":
                    continue
                command=["bash",str(EUREKA/"Environment/API/run.sh"),"openai","--",
                    str(EUREKA/"Environment/Python/run.sh"),str(HERE/"run_trial.py"),
                    "--task-id",str(int(entry["task_id"])),"--model",MODEL,
                    "--experiment-id",out.name,"--out",entry["trial_dir"]]
                stdout=open(out/f"task{entry['task_id']}.stdout.log","xb")
                stderr=open(out/f"task{entry['task_id']}.stderr.log","xb")
                process=subprocess.Popen(command,cwd=EUREKA,stdout=stdout,stderr=stderr,start_new_session=True)
                entry.update(status="running",pid=process.pid,started_utc=utc(),command=command)
                live[entry["task_id"]]=(process,entry,stdout,stderr)
            queue["updated_utc"]=utc()
            atomic_json(out/"queue.json",queue)
            summary(out,plan,queue)
            if not live and all(e["status"]!="pending" for e in entries):
                queue["status"]="finished"
                break
            time.sleep(3)
    finally:
        for process,entry,stdout,stderr in live.values():
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGKILL);process.wait()
            entry.update(status="interrupted",returncode=process.returncode,ended_utc=utc())
            stdout.close();stderr.close()
        if stopped[0]:
            queue["status"]="interrupted"
        queue["ended_utc"]=utc()
        atomic_json(out/"queue.json",queue)
        summary(out,plan,queue)
    return 0 if queue["status"]=="finished" and all(e["status"]=="scientific_terminal" for e in entries) else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--model",choices=[MODEL],default=MODEL)
    parser.add_argument("--task-ids",nargs=3,default=list(IDS))
    parser.add_argument("--parallel",type=int,choices=[1,2],default=2)
    parser.add_argument("--observe",action="store_true")
    args=parser.parse_args()
    if tuple(f"{int(x):02d}" for x in args.task_ids)!=IDS:
        parser.error("This frozen campaign selects exactly 01/13/25")
    if args.observe:
        value=summary(args.out,read_json(args.out/"plan.json"),read_json(args.out/"queue.json"))
        print(json.dumps(value,ensure_ascii=False,indent=2))
        return 0
    return run(args.out,args.parallel)


if __name__=="__main__":
    raise SystemExit(main())
