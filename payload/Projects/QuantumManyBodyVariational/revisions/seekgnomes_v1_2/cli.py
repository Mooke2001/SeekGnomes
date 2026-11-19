"""Harbor-backed SeekGnomes command line. Credentials never enter run configs."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

PROJECT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
VERSION = "1.3.0"
HARBOR_VERSION = "0.22.0"
API_PROTOCOLS = ("auto", "responses", "messages", "chat_completions", "gemini")
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))
if not __package__:
    __package__ = "revisions.seekgnomes_v1_2"


def parse_tasks(value):
    if value == "all":
        return tuple(f"{number:02d}" for number in range(1, 51))
    selected = set()
    for part in value.split(","):
        if not re.fullmatch(r"\d{1,2}(?:-\d{1,2})?", part):
            raise argparse.ArgumentTypeError("Use all or task IDs/ranges, for example 1,7,13-15")
        limits = [int(number) for number in part.split("-")]
        first, last = limits[0], limits[-1]
        if not 1 <= first <= last <= 50:
            raise argparse.ArgumentTypeError("Task IDs must be between 1 and 50 in ascending ranges")
        selected.update(range(first, last + 1))
    if not selected:
        raise argparse.ArgumentTypeError("Select at least one task")
    return tuple(f"{number:02d}" for number in sorted(selected))


def validate_endpoint(value, *, allow_local_http=False):
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        raise ValueError("API base URL must be a single valid URL")
    parsed = urlsplit(value)
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ValueError("API base URL must have a host and no credentials, query, or fragment")
    local = parsed.hostname.lower() in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme != "https" and not (allow_local_http and local and parsed.scheme == "http"):
        raise ValueError("HTTPS is required; local HTTP needs --allow-local-http")
    _ = parsed.port
    return value.rstrip("/")


def canonical_output(value):
    path = Path(os.path.abspath(value))
    if path.resolve() != path or not path.is_relative_to(PROJECT / "runs"):
        raise ValueError("Output must be a new, unaliased directory under the mounted runs directory")
    return path


def _dump(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), flush=True)


def positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("A positive integer is required") from error
    if number <= 0 or str(number) != str(value).strip():
        raise argparse.ArgumentTypeError("A positive integer is required")
    return number


def _selection(args):
    from .harnesses import resolve_harness
    return resolve_harness(model=args.model, company=args.company,
                           harness=args.harness, api_protocol=args.api_protocol)


def _selection_dict(selection):
    from dataclasses import asdict, is_dataclass
    if hasattr(selection, "to_dict"):
        return selection.to_dict()
    return asdict(selection) if is_dataclass(selection) else dict(selection)


def inspect_environment(selection=None, *, requested_cpus=2):
    from revisions.seekgnomes_v1_1_1.preflight import inspect, MIN_AVAILABLE_MEMORY
    result = inspect(check_resources=False)
    result["schema"] = "seekgnomes-harbor-preflight/v1.2"
    result["release_version"] = VERSION
    failures = result.setdefault("failures", [])
    resources = result.get("resources", {})
    effective_cpus = resources.get("effective_cpu_count")
    available_memory = resources.get("available_memory_bytes")
    result.setdefault("requirements", {})["min_effective_cpus"] = requested_cpus
    result["requirements"]["requested_cpus"] = requested_cpus
    if effective_cpus is None or effective_cpus < requested_cpus:
        failures.append(f"At least {requested_cpus} effective CPUs are required by this configuration (including cgroup quota)")
    if available_memory is None or available_memory < MIN_AVAILABLE_MEMORY:
        failures.append("At least 12 GiB available RAM is required; allocate 16 GiB or more")
    try:
        observed = importlib.metadata.version("harbor")
    except importlib.metadata.PackageNotFoundError:
        observed = None
    result["harbor"] = {"expected_version": HARBOR_VERSION,
                        "observed_version": observed, "ok": observed == HARBOR_VERSION}
    if observed != HARBOR_VERSION:
        failures.append("Harbor 0.22.0 is required; missing or different installed version")
    from .sandbox import probe_role_sandbox
    result["native_role_sandbox"] = probe_role_sandbox()
    if result["native_role_sandbox"].get("ok") is not True:
        failures.extend(result["native_role_sandbox"].get("failures") or ["Native role sandbox probe failed"])
    if selection is not None:
        result["harness_selection"] = _selection_dict(selection)
        if not getattr(selection, "supported", True):
            failures.append(getattr(selection, "unsupported_reason", None) or "Selected official harness is unsupported")
        else:
            from .native import probe_harness
            result["native_harness"] = probe_harness(selection)
            if result["native_harness"].get("ok") is not True:
                failures.extend(result["native_harness"].get("failures") or ["Native harness preflight failed"])
    result["ok"] = result.get("ok") is True and not failures
    result["external_api_calls"] = 0
    return result


def self_test():
    """Check strict proofs, state scoring and offline Harbor execution."""
    from .self_test import run_self_test
    return run_self_test()


def _selection_arguments(parser, *, with_model=True):
    if with_model:
        parser.add_argument("--model", default=os.environ.get("SEEKGNOMES_MODEL"))
    parser.add_argument("--company", default=os.environ.get("SEEKGNOMES_COMPANY"),
                        help="Model's company, independent of router/deployment prefixes")
    parser.add_argument("--harness", default=os.environ.get("SEEKGNOMES_HARNESS"),
                        help="Explicitly confirm the company-selected harness; does not override company policy")
    parser.add_argument("--api-protocol", choices=API_PROTOCOLS,
                        default=os.environ.get("SEEKGNOMES_API_PROTOCOL", "auto"))
    parser.add_argument("--model-context-tokens", type=positive_int,
                        default=os.environ.get("SEEKGNOMES_MODEL_CONTEXT_TOKENS"),
                        help="Explicit positive model context window; required by Kimi Code")
    parser.add_argument("--cpus", type=positive_int, default=os.environ.get("SEEKGNOMES_CPUS", "2"),
                        help="Positive CPU allocation, matched by the container launcher")


def _parser():
    parser = argparse.ArgumentParser(prog="seekgnomes", description="SeekGnomes: Harbor-backed infinite-state quantum benchmark")
    parser.add_argument("--version", action="version", version="SeekGnomes " + VERSION)
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="Run fresh independent Harbor trials")
    _selection_arguments(run)
    run.add_argument("--api-base-url", default=os.environ.get("SEEKGNOMES_API_BASE_URL"))
    run.add_argument("--tasks", type=parse_tasks, default=parse_tasks("all"))
    run.add_argument("--out", type=Path)
    run.add_argument("--completion-token-field", choices=("max_tokens", "max_completion_tokens", "both"), default="max_tokens")
    run.add_argument("--budget-mode", choices=("combined", "tokens", "time"), default="combined",
                     help="Use both Constructor limits, only output tokens, or only active time")
    run.add_argument("--constructor-output-tokens", type=positive_int,
                     default=os.environ.get("SEEKGNOMES_CONSTRUCTOR_OUTPUT_TOKENS", "128000"),
                     help="Main-phase Constructor completion-token budget; one fixed 50000-token closing phase applies only if no complete handoff was created")
    run.add_argument("--constructor-active-seconds", type=positive_int,
                     default=os.environ.get("SEEKGNOMES_CONSTRUCTOR_ACTIVE_SECONDS", "14400"),
                     help="Main-phase active wall time; a qualifying closing phase receives the same separate time allowance")
    run.add_argument("--constructor-input-tokens", type=positive_int,
                     default=os.environ.get("SEEKGNOMES_CONSTRUCTOR_INPUT_TOKENS") or None,
                     help="Optional input-token limit for each eligible Constructor phase; no default hidden input cap")
    run.add_argument("--no-web", action="store_true", help="Record a separate closed-book protocol")
    run.add_argument("--allow-local-http", action="store_true", help="Explicitly allow HTTP to a localhost API")
    run.add_argument("--print-config", action="store_true", help="Inspect public selection and configuration without API use")
    doctor = sub.add_parser("doctor", help="Check frozen checker, Harbor and optional selected native harness; no API")
    _selection_arguments(doctor)
    sub.add_parser("self-test", help="Run real strict-checker and Harbor fixture smoke tests; no API")
    sub.add_parser("tasks", help="List the fifty fixed Hamiltonians and scoring groups")
    sub.add_parser("harnesses", help="Show the versioned company-to-harness registry; no API")
    report = sub.add_parser("report", help="Recompute score tables from stored evidence; no API")
    report.add_argument("run_dir", type=Path)
    resume = sub.add_parser("resume", help="Resume a saved campaign after its supervisor was interrupted")
    resume.add_argument("run_dir", type=Path)
    trial = sub.add_parser("_trial", help=argparse.SUPPRESS)
    trial.add_argument("--request", type=Path, required=True)
    return parser


def _trial(request):
    from .runtime import run_trial
    request = canonical_output(request)
    if not request.is_file() or request.stat().st_size > 65536:
        raise ValueError("Invalid bounded trial request")
    config = json.loads(request.read_text())
    allowed = {"task_id", "out", "experiment_id", "budget_multiplier", "model", "endpoint",
               "completion_token_field", "web", "company", "harness", "api_protocol", "model_context_tokens",
               "constructor_output_tokens", "constructor_active_seconds", "constructor_input_tokens", "cpus"}
    if not isinstance(config, dict) or set(config) != allowed:
        raise ValueError("Unexpected trial request fields")
    config["out"] = canonical_output(config["out"])
    key = os.environ.get("SEEKGNOMES_API_KEY", "")
    if not key:
        raise ValueError("SEEKGNOMES_API_KEY is required for a child trial")
    result = run_trial(**config, api_key=key)
    _dump({"status": result.get("status"), "out": str(config["out"])})
    return 0 if result.get("status") in ("completed", "final_submission_not_verified") else 1


def main(argv=None, *, campaign_launcher=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        if not sys.stdin.isatty():
            _parser().print_help()
            return 2
        argv = ["run"]
    args = _parser().parse_args(argv)
    key = ""
    try:
        if args.command == "_trial":
            return _trial(args.request)
        if args.command == "doctor":
            result = inspect_environment(_selection(args) if args.model else None, requested_cpus=args.cpus)
            _dump(result)
            return 0 if result.get("ok") else 1
        if args.command == "self-test":
            result = self_test()
            _dump(result)
            return 0 if result.get("ok") else 1
        if args.command == "harnesses":
            _dump(json.loads((HERE / "harness_registry.json").read_text()))
            return 0
        if args.command == "tasks":
            from src.benchmark50_models import load_task
            groups = json.loads((HERE.parent / "seekgnomes_v1_1_1/task_groups.json").read_text())
            _dump({"tasks": [load_task(i) for i in range(1, 51)], "groups": groups})
            return 0
        if args.command == "report":
            from revisions.seekgnomes_v1_1_1.scoring import write_results
            _dump(write_results(canonical_output(args.run_dir)))
            return 0
        if args.command == "resume":
            from .campaign import resume_campaign
            out = canonical_output(args.run_dir)
            key = _read_api_key()
            result = resume_campaign(out, api_key=key)
            _dump({"status": result["status"], "out": str(out), "results": str(out / "results")})
            return 0 if result["status"] == "finished" else 1
        if args.command != "run":
            _parser().print_help()
            return 2
        if not args.model:
            if not sys.stdin.isatty():
                raise ValueError("Supply --model or SEEKGNOMES_MODEL")
            args.model = input("Exact model ID returned by your API: ").strip()
        if not args.model or any(ord(char) < 32 for char in args.model):
            raise ValueError("Supply a nonempty, single-line model ID")
        selection = _selection(args)
        if selection.company == "unknown" and not args.company:
            if not sys.stdin.isatty():
                raise ValueError("Model company is unknown; supply --company before selecting its official harness")
            args.company = input("Model company (for example openai, moonshot, deepseek): ").strip()
            if not args.company:
                raise ValueError("A model company is required for an opaque model ID")
            selection = _selection(args)
        if not getattr(selection, "supported", True):
            raise ValueError(getattr(selection, "unsupported_reason", None) or "Selected official harness is unsupported")
        if selection.name == "kimi-code" and args.model_context_tokens is None:
            if not sys.stdin.isatty():
                raise ValueError("Kimi Code requires --model-context-tokens; do not infer a model window from its name")
            args.model_context_tokens = positive_int(input("Model context window in tokens (positive integer): ").strip())
        if not args.api_base_url:
            if not sys.stdin.isatty():
                raise ValueError("Supply --api-base-url or SEEKGNOMES_API_BASE_URL")
            args.api_base_url = input("API base URL implementing the selected harness protocol: ").strip()
        endpoint = validate_endpoint(args.api_base_url, allow_local_http=args.allow_local_http)
        output_limit = args.constructor_output_tokens if args.budget_mode != "time" else None
        active_limit = args.constructor_active_seconds if args.budget_mode != "tokens" else None
        environment_key = os.environ.get("SEEKGNOMES_API_KEY", "")
        if environment_key and (environment_key in endpoint or environment_key in args.model):
            raise ValueError("The API key must not appear in public route fields")
        from .task_export import STATE_PROTOCOL

        public = {"schema": "seekgnomes-harbor-request/v1.2", "version": VERSION,
                  "protocol": STATE_PROTOCOL, "candidate_format": "state",
                  "model": args.model, "endpoint": endpoint, "task_ids": args.tasks,
                  "harness_selection": _selection_dict(selection), "web": not args.no_web,
                  "model_context_tokens": args.model_context_tokens,
                  "budget_mode": args.budget_mode, "constructor_output_tokens": output_limit,
                  "closing_output_tokens": 50000,
                  "closing_active_seconds": active_limit,
                  "closing_input_tokens": args.constructor_input_tokens,
                  "constructor_active_seconds": active_limit,
                  "constructor_input_tokens": args.constructor_input_tokens,
                  "cpus": args.cpus,
                  "completion_token_field": args.completion_token_field}
        if args.print_config:
            _dump(public)
            return 0
        preflight = inspect_environment(selection, requested_cpus=args.cpus)
        _dump(preflight)
        if not preflight.get("ok"):
            return 1
        key = _read_api_key()
        if not key or key in endpoint or key in args.model:
            raise ValueError("Invalid credential configuration; key must not appear in public route fields")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        out = canonical_output(args.out if args.out else PROJECT / "runs" / ("seekgnomes-harbor-" + stamp))
        from .campaign import run_campaign
        result = (campaign_launcher or run_campaign)(out, model=args.model, endpoint=endpoint, api_key=key,
            task_ids=args.tasks, company=args.company, harness=args.harness,
            api_protocol=args.api_protocol, web=not args.no_web,
            model_context_tokens=args.model_context_tokens,
            constructor_output_tokens=output_limit,
            constructor_active_seconds=active_limit,
            constructor_input_tokens=args.constructor_input_tokens, cpus=args.cpus,
            completion_token_field=args.completion_token_field, preflight_record=preflight)
        _dump({"status": result["status"], "out": str(out), "results": str(out / "results")})
        return 0 if result["status"] == "finished" else 1
    except (ValueError, OSError, ImportError, EOFError, json.JSONDecodeError, argparse.ArgumentTypeError) as error:
        detail = str(error)
        for secret in (key, os.environ.get("SEEKGNOMES_API_KEY", "")):
            if secret:
                detail = detail.replace(secret, "[REDACTED]")
        print("SeekGnomes: " + detail, file=sys.stderr)
        return 2


def _read_api_key():
    key = os.environ.get("SEEKGNOMES_API_KEY", "")
    if not key and sys.stdin.isatty():
        key = getpass.getpass("API key (hidden; never saved to results): ").strip()
    if not key:
        raise ValueError("Set SEEKGNOMES_API_KEY or use the interactive terminal")
    return key


if __name__ == "__main__":
    raise SystemExit(main())
