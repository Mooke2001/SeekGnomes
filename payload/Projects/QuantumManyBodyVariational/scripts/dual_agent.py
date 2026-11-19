"""Stable project-local dispatcher for the selected dual-agent revision."""
import argparse
import hashlib
import json
from pathlib import Path
import runpy
import sys

PROJECT=Path(__file__).resolve().parents[1]
CONFIG=PROJECT/"config/active_agent_revision.json"


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=["run-trial","run-campaign","prepare","verify","analyze"])
    args,remaining=parser.parse_known_args()
    config=json.loads(CONFIG.read_text())
    relative=Path(config["revision"])
    if relative.is_absolute() or ".." in relative.parts or relative.parts[0]!="revisions":
        raise ValueError("Invalid active revision path")
    revision=PROJECT/relative
    lock=PROJECT/config["proof_lock"]
    if revision.resolve(strict=True)!=revision or lock.resolve(strict=True)!=lock or lock.parent!=revision:
        raise ValueError("Active revision/lock must use canonical project paths")
    if hashlib.sha256(lock.read_bytes()).hexdigest()!=config["proof_lock_sha256"]:
        raise ValueError("Active proof-lock binding changed")
    targets={"run-trial":"run_trial.py","run-campaign":"run_campaign.py",
             "prepare":"check_state.py","verify":"check_state.py","analyze":"analyze.py"}
    target=revision/targets[args.command]
    if target.resolve(strict=True)!=target:
        raise ValueError("Active entry point is aliased")
    sys.path.insert(0,str(PROJECT))
    sys.argv=[str(target),*([args.command] if args.command in ("prepare","verify") else []),*remaining]
    runpy.run_path(str(target),run_name="__main__")


if __name__=="__main__":
    main()
