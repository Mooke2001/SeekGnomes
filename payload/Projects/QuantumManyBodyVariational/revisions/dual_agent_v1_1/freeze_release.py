"""Bind this independent revision without changing any historical proof lock."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys

PROJECT=Path(__file__).resolve().parents[2]
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(PROJECT))
from revisions.dual_agent_v1_1.compiler import COMPILER_REVISION
from revisions.dual_agent_v1_1.verifier import REQUIRED_SOURCES


def sha(path):
    if path.resolve(strict=True)!=path:
        raise ValueError("Release sources cannot traverse aliases")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--select-active",action="store_true")
    parser.add_argument("--check",action="store_true")
    args=parser.parse_args()
    old_path=PROJECT/"revisions/physics_first_v1_1/PROOF_SPEC.lock.json"
    target=HERE/"PROOF_SPEC.lock.json"
    old=json.loads(old_path.read_text())
    for name,value in old["source_sha256"].items():
        if sha(PROJECT/name)!=value:
            raise ValueError("Historical source drift: "+name)
    if args.check:
        lock=json.loads(target.read_text())
        mismatches=[name for name,value in lock["source_sha256"].items() if sha(PROJECT/name)!=value]
        if mismatches or not REQUIRED_SOURCES<=set(lock["source_sha256"]):
            raise ValueError("Release binding mismatch: "+str(mismatches))
        print(json.dumps({"ok":True,"source_files":len(lock["source_sha256"]),"proof_lock_sha256":sha(target)}))
        return
    lock=dict(old)
    lock.update(compiler_revision=COMPILER_REVISION,derived_from_lock_sha256=sha(old_path),
        release_revision="dual-agent-v1.1",handoff_policy="persistent_unique_attempts_and_atomic_commit")
    names=set(old["source_sha256"])
    names.update(str(p.relative_to(PROJECT)) for p in HERE.glob("*.py"))
    names.update(str(p.relative_to(PROJECT)) for p in HERE.glob("*.md"))
    names.add("revisions/dual_agent_schema_fix_v1/responses_client.py")
    names.add("scripts/dual_agent.py")
    lock["source_sha256"]={name:sha(PROJECT/name) for name in sorted(names)}
    if not REQUIRED_SOURCES<=set(lock["source_sha256"]):
        raise ValueError("Required verifier sources not fully bound")
    target.write_text(json.dumps(lock,ensure_ascii=False,indent=2,sort_keys=True)+"\n")
    if args.select_active:
        config={"schema":"quantum-agent-active-revision/v1","revision":"revisions/dual_agent_v1_1",
            "proof_lock":"revisions/dual_agent_v1_1/PROOF_SPEC.lock.json",
            "proof_lock_sha256":sha(target),"entrypoint":"scripts/dual_agent.py"}
        path=PROJECT/"config/active_agent_revision.json"
        if path.exists() and (path.is_symlink() or path.stat().st_nlink!=1):
            raise ValueError("Active configuration must be a private regular file")
        path.write_text(json.dumps(config,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"ok":True,"revision":lock["release_revision"],"source_files":len(names),
        "proof_lock_sha256":sha(target),"active":args.select_active}))


if __name__=="__main__":
    main()
