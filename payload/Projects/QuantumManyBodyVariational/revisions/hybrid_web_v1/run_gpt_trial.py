"""Run the same local hybrid trial with optional hosted web search enabled."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from revisions.hybrid_agent_v1.run_trial import (
    BITS, TERMS, HERE, callbacks, digest, locked_sources, public_references,
)
from revisions.hybrid_agent_v1.local_numeric import LocalNumericalBroker
from revisions.hybrid_agent_v1.runtime.api_client import REASONING_USAGE_POLICIES
from revisions.hybrid_web_v1.responses_client import ResponsesClient
from revisions.hybrid_web_v1.agent import (
    AgentLimits, run_agent, runtime_revision,
)
from src.benchmark50_models import load_task

WEB_HERE = Path(__file__).resolve().parent


def official_endpoint() -> str:
    """Reject a relay launch before reading or constructing any API client."""
    if os.environ.get("EUREKA_API_PROVIDER") != "openai":
        raise ValueError("Launch through Environment/API/run.sh openai; relay credentials are not accepted")
    endpoint = os.environ.get("EUREKA_API_BASE_URL", "").rstrip("/")
    url = urlsplit(endpoint)
    if (url.scheme != "https" or url.hostname != "api.openai.com"
            or url.port not in (None, 443) or url.username or url.password
            or url.query or url.fragment or url.path != "/v1"
            or any(character.isspace() for character in endpoint)):
        raise ValueError("This wrapper requires the official https://api.openai.com/v1 endpoint")
    return endpoint


def runtime_sources() -> dict[str, str]:
    """Bind both the shared wrapper and this official-provider entry point."""
    sources = locked_sources()
    sources.update(runtime_revision()["source_sha256"])
    sources["revisions/hybrid_web_v1/run_gpt_trial.py"] = digest(Path(__file__))
    sources["revisions/hybrid_web_v1/responses_client.py"] = digest(WEB_HERE / "responses_client.py")
    sources["revisions/hybrid_web_v1/system_prompt.md"] = digest(WEB_HERE / "system_prompt.md")
    sources["revisions/hybrid_agent_v1/responses_client.py"] = digest(HERE / "responses_client.py")
    sources["src/benchmark50_models.py"] = digest(PROJECT / "src/benchmark50_models.py")
    return sources


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--task-id", type=int, required=True, choices=range(1, 51))
    result.add_argument("--model", required=True, choices=("gpt-6.1-terra",),
                        help="Exact model pinned by the user; no automatic substitution")
    result.add_argument("--out", type=Path, required=True)
    result.add_argument("--experiment-id", required=True)
    result.add_argument("--resume", action="store_true")
    result.add_argument("--checker-seconds", type=int, default=300)
    result.add_argument("--request-seconds", type=int, default=600)
    result.add_argument("--max-turns", type=int, default=64)
    result.add_argument("--output-tokens", type=int, default=128000)
    result.add_argument("--wall-seconds", type=int, default=14400)
    result.add_argument("--reasoning-usage-policy", choices=REASONING_USAGE_POLICIES,
                        default="provider_reported_completion")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    endpoint = official_endpoint()
    output = Path(os.path.abspath(args.out))
    if not output.is_relative_to(PROJECT) or output.resolve() != output:
        raise ValueError("Trial output must be a non-aliased path inside this project")
    for label, value in (("checker-seconds", args.checker_seconds),
                         ("request-seconds", args.request_seconds),
                         ("max-turns", args.max_turns),
                         ("output-tokens", args.output_tokens),
                         ("wall-seconds", args.wall_seconds)):
        if value <= 0:
            raise ValueError(label + " must be positive")
    sources = runtime_sources()
    task = load_task(args.task_id)
    prepare, lean, score = callbacks(task, timeout=args.checker_seconds)

    def progress(kind, value):
        if kind == "api":
            record = {"event": kind, **value}
        elif kind == "tool_request":
            record = {"event": kind, "tool": value.get("tool")}
        else:
            fields = ("ok", "verified", "proof_verified", "energy_density", "finished", "error")
            record = {"event": kind, **{key: value[key] for key in fields if key in value}}
        print(json.dumps(record, ensure_ascii=False, allow_nan=False), flush=True)

    # The existing agent speaks a normalized tool protocol; this adapter sends
    # the actual model calls to /v1/responses and retains the original records.
    client = ResponsesClient(
        base_url=endpoint, api_key=os.environ.get("EUREKA_API_KEY", ""),
        timeout=args.request_seconds, connect_timeout=60,
        persistent_session=True, max_connect_retries=2,
        response_body_limit_bytes=16 * 1024**2, completion_token_field="max_completion_tokens",
        reasoning_usage_policy=args.reasoning_usage_policy,
    )
    try:
        manifest = run_agent(
            client, args.model, task, output,
            references=public_references(task), prepare=prepare, run_lean=lean,
            score=score, server=LocalNumericalBroker(),
            limits=AgentLimits(
                max_turns=args.max_turns, max_tokens_per_turn=16000,
                max_total_output_tokens=args.output_tokens,
                max_total_input_tokens=2000000, max_verifier_calls=64,
                max_server_cpu_seconds=3600, final_output_reserve=16000,
                wall_seconds=args.wall_seconds,
            ),
            system_prompt=(WEB_HERE / "system_prompt.md").read_text(),
            tool_protocol="native_function", transport_reconnect=True,
            max_transport_reconnects=2, resume=args.resume,
            normal_request_tokens=16000, normal_output_safety_margin=4096,
            closing_request_tokens=8192, progress=progress,
            provenance={
                "family": "gpt", "provider": "openai",
                "official_api": True, "api_base_url": endpoint,
                "requested_model": "gpt-6.1-terra",
                "api_protocol": "responses",
                "agent_completion_token_field": "max_completion_tokens",
                "wire_completion_token_field": "max_output_tokens",
                "experiment_id": args.experiment_id, "trial": 1,
                "scope": "local_hybrid_five_task_web_ablation",
                "network_tools_exposed_to_model": True,
                "network_policy": "hosted_web_search_only; local_numerical_tools_remain_networkless",
                "web_search_policy": "optional_auto; live_access; provider_default_context_budget",
                "comparison_control": "gpt61-terra-hybrid-five-local-20261115T075100Z",
                "runtime_sources": sources,
                "energy_endpoint": "approximate_float_for_kernel_verified_state",
                "compute_location": "local_no_ssh", "sqrt_bits": BITS,
                "series_terms": TERMS,
                "transport_limits": {"checker_seconds": args.checker_seconds},
                "usage_policy": args.reasoning_usage_policy,
                "usage_measure": (
                    "provider_reported_completion_once_not_billing_or_physical_generation"
                    if args.reasoning_usage_policy == "provider_reported_completion"
                    else "explicit_runtime_policy_not_billing_or_physical_generation"
                ),
            },
        )
        print(json.dumps({
            "status": manifest["status"], "turns": manifest["turns"],
            "model": manifest["model"], "out": str(output),
            "provider": "openai", "api_base_url": endpoint,
            "best_verified_result": manifest.get("best_verified_result"),
            "verified_result": manifest.get("verified_result"),
        }, ensure_ascii=False, allow_nan=False))
        return 0 if manifest["status"] == "completed" else 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
