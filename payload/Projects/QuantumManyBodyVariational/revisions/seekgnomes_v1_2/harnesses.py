"""Auditable company-to-native-harness selection. No API calls occur here."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import re
from typing import Any


REGISTRY_PATH = Path(__file__).with_name("harness_registry.json")


class HarnessSelectionError(ValueError):
    pass


class ProtocolMismatch(HarnessSelectionError):
    pass


@dataclass(frozen=True)
class Selection:
    name: str
    harbor_agent: str
    company: str
    model: str
    version: str
    api_protocol: str
    fallback_reason: str | None
    evidence: tuple[str, ...]
    harbor_integration: str
    supported: bool
    unsupported_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_registry() -> dict[str, Any]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def harness_record(selection: Selection | str) -> dict[str, Any]:
    name = selection.name if isinstance(selection, Selection) else selection
    try:
        return load_registry()["harnesses"][name]
    except KeyError as exc:
        raise HarnessSelectionError(f"Unknown harness: {name}") from exc


_FAMILIES = {
    "openai": r"(?:gpt(?:[-_.]|$)|o[1-9](?:[-_.]|$)|codex(?:[-_.]|$))",
    "anthropic": r"(?:claude(?:[-_.]|$)|fable(?:[-_.]|\d|$))",
    "moonshot": r"(?:kimi(?:[-_.]|\d|$)|moonshot(?:[-_.]|$))",
    "google": r"(?:gemini(?:[-_.]|$)|gemma(?:[-_.]|\d|$))",
    "xai": r"grok(?:[-_.]|\d|$)",
    "alibaba": r"qwen(?:[-_.]|\d|$)",
    "deepseek": r"deepseek(?:[-_.]|$)",
    "zai": r"(?:chatglm|glm)(?:[-_.]|\d|$)",
    "xiaomi": r"mimo(?:[-_.]|\d|$)",
}


def infer_company(model: str) -> str | None:
    """Identify model families, never the arbitrary router path prefix.

    For example ``openai/deepseek-v4`` is DeepSeek: an OpenAI-compatible
    router namespace does not make the underlying model an OpenAI model.
    """
    segments = model.lower().split("/")
    candidates = {
        company
        for company, expression in _FAMILIES.items()
        if any(re.match(expression, segment) for segment in segments)
    }
    if len(candidates) > 1:
        raise HarnessSelectionError(
            "Ambiguous model families in route; supply an unambiguous model ID: "
            + ", ".join(sorted(candidates))
        )
    return next(iter(candidates), None)


def validate_api_protocol(selection: Selection, requested: str = "auto") -> str:
    """Return the native wire protocol or reject an incompatible override."""
    value = requested.lower().replace("-", "_")
    value = {"chat": "chat_completions", "anthropic": "messages"}.get(value, value)
    if value == "auto":
        return selection.api_protocol
    allowed = harness_record(selection)["allowed_api_protocols"]
    if value not in allowed:
        raise ProtocolMismatch(
            f"{selection.name} requires one of {allowed}; requested {value}. "
            "Configure a compatible endpoint; this does not change the harness."
        )
    return value


def resolve_harness(
    model: str,
    company: str | None = None,
    harness: str | None = None,
    api_protocol: str = "auto",
) -> Selection:
    """Select the official vendor harness and preserve the exact model ID.

    Explicit company names are useful for opaque endpoint aliases. Unknown
    companies select Codex with a recorded fallback reason, rather than
    fabricating a vendor attribution. An override cannot bypass the company
    policy. A custom Harbor integration is not a reason to select Codex.
    """
    if not isinstance(model, str) or not model.strip() or any(ord(c) < 32 for c in model):
        raise HarnessSelectionError("model must be a nonempty model ID without control characters")
    registry = load_registry()
    inferred = infer_company(model)
    explicit = company.lower().strip() if company and company.strip() else None
    canonical = registry["company_aliases"].get(explicit, explicit) if explicit else inferred
    if explicit and inferred and canonical != inferred:
        raise HarnessSelectionError(
            f"Company {canonical} conflicts with recognizable model family {inferred}"
        )
    canonical = canonical or "unknown"
    selected_name = registry["companies"].get(canonical, registry["fallback"])
    reason = None
    if canonical not in registry["companies"]:
        reason = "unknown_company" if canonical == "unknown" else "no_verified_official_harness"
    if harness and harness != "auto":
        override = registry["harness_aliases"].get(harness, harness)
        if override != selected_name:
            raise HarnessSelectionError(
                f"Company policy selects {selected_name} for {canonical}, not {override}"
            )
    record = registry["harnesses"][selected_name]
    result = Selection(
        name=selected_name,
        harbor_agent=record["harbor_agent"],
        company=canonical,
        model=model,
        version=record["version"],
        api_protocol=record["api_protocol"],
        fallback_reason=reason,
        evidence=tuple(record["evidence"]),
        harbor_integration=record["harbor_integration"],
        supported=record["supported"],
        unsupported_reason=record["unsupported_reason"],
    )
    return replace(result, api_protocol=validate_api_protocol(result, api_protocol))


def installation_manifest() -> dict[str, Any]:
    """Pinned upstream assets, including integrity, without running installers."""
    return {
        name: {"binary": row["binary"], **row["install"]}
        for name, row in load_registry()["harnesses"].items()
    }
