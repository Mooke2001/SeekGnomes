"""Company policy, router attribution and pinned-asset contracts."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from revisions.seekgnomes_v1_2.harnesses import (
    HarnessSelectionError, ProtocolMismatch, installation_manifest,
    resolve_harness, validate_api_protocol,
)


class SelectionTests(unittest.TestCase):
    def test_all_requested_companies_select_their_official_harness(self):
        cases = {
            "openai/gpt-6.1-terra": ("openai", "codex", "responses"),
            "ora/claude-fable-5-2[1m]": ("anthropic", "claude-code", "messages"),
            "kimi-k3.1-pro": ("moonshot", "kimi-code", "chat_completions"),
            "bh2/gemini-4-argon": ("google", "gemini-cli", "gemini"),
            "grok-4.7": ("xai", "grok-build", "responses"),
            "infi/deepseek-v4.1-flash": ("deepseek", "dsh", "messages"),
            "bh2/glm-5.3": ("zai", "zcode", "chat_completions"),
            "mimo-v2.6-pro": ("xiaomi", "mimo-code", "chat_completions"),
            "qwen3-coder": ("alibaba", "qwen-code", "chat_completions"),
        }
        for model, expected in cases.items():
            with self.subTest(model=model):
                selection = resolve_harness(model)
                self.assertEqual((selection.company, selection.name, selection.api_protocol), expected)
                self.assertEqual(selection.model, model)
                self.assertIsNone(selection.fallback_reason)
                self.assertTrue(selection.evidence)

    def test_router_namespace_is_not_a_model_company(self):
        self.assertEqual(resolve_harness("openai/deepseek-v4.1-flash").company, "deepseek")
        self.assertEqual(resolve_harness("anthropic/glm-5.3").company, "zai")
        unknown = resolve_harness("infi/opaque-model")
        self.assertEqual(unknown.company, "unknown")
        self.assertEqual(unknown.fallback_reason, "unknown_company")

    def test_explicit_company_handles_opaque_route_and_preserves_policy(self):
        self.assertEqual(resolve_harness("production-route", company="Z.ai").name, "zcode")
        self.assertEqual(resolve_harness("production-route", company="MoonshotAI").name, "kimi-code")
        with self.assertRaises(HarnessSelectionError):
            resolve_harness("deepseek-v4", company="openai")
        with self.assertRaises(HarnessSelectionError):
            resolve_harness("deepseek-v4", harness="codex")
        self.assertEqual(resolve_harness("opaque", company="unregistered-company").fallback_reason,
                         "no_verified_official_harness")

    def test_missing_harbor_builtin_does_not_trigger_codex_fallback(self):
        for model in ("glm-5.3", "deepseek-v4.1-flash"):
            selected = resolve_harness(model)
            self.assertEqual(selected.harbor_integration, "custom")
            self.assertNotEqual(selected.name, "codex")
            self.assertIsNone(selected.fallback_reason)

    def test_protocol_override_must_be_supported_by_native_cli(self):
        with self.assertRaises(ProtocolMismatch):
            resolve_harness("gpt-6.1-terra", api_protocol="chat_completions")
        with self.assertRaises(ProtocolMismatch):
            resolve_harness("gemini-4-argon", api_protocol="responses")
        self.assertEqual(resolve_harness("grok-4.7", api_protocol="messages").api_protocol, "messages")
        self.assertEqual(validate_api_protocol(resolve_harness("gpt-6.1-terra")), "responses")

    def test_selection_is_immutable_and_models_cannot_inject_flags(self):
        selection = resolve_harness("gpt-6.1-terra")
        with self.assertRaises(FrozenInstanceError):
            selection.model = "another-model"
        self.assertEqual(selection.to_dict()["version"], "0.167.1")
        for invalid in ("", "\n", "gpt-6.1\x00--bad"):
            with self.assertRaises(HarnessSelectionError):
                resolve_harness(invalid)

    def test_install_manifest_never_floats_to_latest(self):
        for name, record in installation_manifest().items():
            with self.subTest(harness=name):
                self.assertNotIn(record["version"], ("latest", "main", "master", "*"))
                if record["kind"] == "npm":
                    self.assertTrue(record["integrity"].startswith("sha512-"))
                    self.assertTrue(record["tarball"].startswith("https://registry.npmjs.org/"))
                else:
                    self.assertEqual(record["kind"], "bundled-source-build")
                    self.assertEqual(len(record["commit"]), 40)
                    self.assertEqual(len(record["runtime_sha256"]), 64)
                    self.assertEqual(record["node"]["version"], "24.14.0")


if __name__ == "__main__":
    unittest.main()
