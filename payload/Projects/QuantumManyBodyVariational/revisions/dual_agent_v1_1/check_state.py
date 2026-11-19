"""Offline prepare/strict verification entry for the current agent revision."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys

PROJECT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(PROJECT))
from revisions.dual_agent_v1_1.callbacks import callbacks,read_source
from src.benchmark50_models import load_task


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode",choices=["prepare","verify"])
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--proof",type=Path)
    parser.add_argument("--task-id",type=int,required=True)
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    if args.mode=="verify" and args.proof is None:
        parser.error("verify requires --proof")
    out=Path(os.path.abspath(args.out))
    if not out.is_relative_to(PROJECT) or out.parent.resolve(strict=True)!=out.parent:
        parser.error("Use a fresh nonaliased output directory inside this project")
    out.mkdir(exist_ok=False,mode=0o700)
    prepare,lean,score=callbacks(load_task(args.task_id),timeout=300)
    try:
        if args.mode=="prepare":
            result=prepare(source_path=args.source,invocation_dir=out)
            (out/"GeneratedState.lean").write_text(result.pop("generated_state"))
            (out/"Candidate.template.lean").write_text(result.pop("proof_template"))
        else:
            result=score(source_path=args.source,proof_path=args.proof,invocation_dir=out)
    except Exception as error:
        result={"ok":False,"verified":False,"proof_verified":False,
                "energy_density":None,"error":type(error).__name__,"detail":str(error)}
    (out/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+"\n")
    print(json.dumps(result,ensure_ascii=False,allow_nan=False))
    return 0 if result.get("ok") else 1


if __name__=="__main__":
    raise SystemExit(main())
