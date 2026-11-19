"""Trusted container entry point for a local console campaign and its workers."""
import argparse
from functools import partial
import hashlib
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
PROJECT = Path("/home/ubuntu/Mooke/Eureka/Projects/QuantumManyBodyVariational")


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--budget-mode", choices=("tokens", "time"), required=True)
    parser.add_argument("--budget-limit", type=int, required=True)
    parser.add_argument("--read-api-key-stdin", action="store_true", required=True)
    parser.add_argument("--parallel-tasks", type=int, default=1)
    parser.add_argument("--source-manifest-sha256")
    parser.add_argument("--parallel-worker", action="store_true", help=argparse.SUPPRESS)
    args, remaining = parser.parse_known_args()
    if args.budget_limit < 1 or args.parallel_tasks < 1:
        parser.error("Expected positive budget and parallel-task limits")
    if args.parallel_worker and (remaining or args.parallel_tasks != 1):
        parser.error("Unexpected worker arguments")
    if not args.parallel_worker:
        if not remaining or remaining[0] != "run":
            parser.error("Expected run and a positive budget limit")
        manifest = PROJECT / "SEEKGNOMES_RELEASE.json"
        if (not args.source_manifest_sha256 or
                hashlib.sha256(manifest.read_bytes()).hexdigest() != args.source_manifest_sha256):
            parser.error("The container source does not match this console's release source manifest")

    raw_key = sys.stdin.buffer.readline(8194)
    if not raw_key.endswith(b"\n") or len(raw_key) > 8193:
        parser.error("Expected the model API key through stdin")
    key = raw_key[:-1].decode("ascii", errors="replace")
    if not key or any(not 33 <= ord(char) <= 126 for char in key):
        parser.error("Invalid model API key")

    sys.path.insert(0, str(PROJECT))
    from revisions.seekgnomes_v1_2 import cli
    from revisions.seekgnomes_v1_2.campaign import run_campaign
    from revisions.seekgnomes_v1_2.gateway import RoleGateway

    launcher = None
    if args.parallel_tasks > 1:
        from parallel_campaign import run_parallel, validate_parallelism

        worker_argv = [sys.executable, "-B", str(HERE / "runtime_entry.py"), "--parallel-worker",
                       "--budget-mode", args.budget_mode, "--budget-limit", str(args.budget_limit),
                       "--read-api-key-stdin"]
        parallel = partial(run_parallel, parallel_tasks=args.parallel_tasks, worker_argv=worker_argv)
        launcher = partial(run_campaign, _parallel_launcher=parallel)

    # A provider sometimes returns diagnostic payloads. Reject a response that
    # echoes the runtime credential before it can appear in a native transcript.
    exchange = RoleGateway._exchange

    def checked_exchange(gateway, *args):
        secret = gateway.key.encode()
        try:
            status, media, body = exchange(gateway, *args)
        except Exception as error:
            partial = getattr(error, "native_partial_response", b"")
            if secret in partial:
                error.native_partial_response = bytes(partial).replace(secret, b"[REDACTED]")
            raise
        if secret in body:
            raise ValueError("The API response echoed the credential")
        return status, media, body

    RoleGateway._exchange = checked_exchange
    if args.parallel_worker:
        from parallel_campaign import run_worker
        return run_worker(sys.stdin.buffer, key)

    limit_option = "--constructor-output-tokens" if args.budget_mode == "tokens" else "--constructor-active-seconds"
    remaining.extend(["--budget-mode", args.budget_mode, limit_option, str(args.budget_limit)])
    if launcher is not None:
        campaign_args = cli._parser().parse_args(remaining)
        try:
            validate_parallelism(args.parallel_tasks, campaign_args.tasks, campaign_args.cpus)
        except ValueError as error:
            parser.error(str(error))
    os.environ["SEEKGNOMES_API_KEY"] = key
    try:
        return cli.main(remaining, campaign_launcher=launcher)
    finally:
        os.environ.pop("SEEKGNOMES_API_KEY", None)


if __name__ == "__main__":
    raise SystemExit(main())
