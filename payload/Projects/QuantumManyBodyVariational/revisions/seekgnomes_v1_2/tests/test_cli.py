"""CLI contract checks: selection, dry configuration, credentials and dispatch."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

PROJECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT))
from revisions.seekgnomes_v1_2 import cli


class ParsingTests(unittest.TestCase):
    def test_tasks_fixed_unique_and_sorted(self):
        self.assertEqual(cli.parse_tasks("7,1,7,13-15"), ("01", "07", "13", "14", "15"))
        self.assertEqual(len(cli.parse_tasks("all")), 50)
        for invalid in ("", "0", "51", "5-2", "1;pwd", "a"):
            with self.subTest(invalid=invalid), self.assertRaises(Exception):
                cli.parse_tasks(invalid)

    def test_endpoint_cannot_embed_credentials(self):
        self.assertEqual(cli.validate_endpoint("https://api.example.com/v1/"), "https://api.example.com/v1")
        for invalid in ("https://user:secret@example.com/v1", "https://example.com/v1?key=secret",
                        "https://example.com/#secret", "http://example.com/v1", "https://example.com/\n"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                cli.validate_endpoint(invalid)
        self.assertEqual(cli.validate_endpoint("http://localhost:8080/v1", allow_local_http=True),
                         "http://localhost:8080/v1")

    def test_default_is_all_fifty_and_native_auto_protocol(self):
        args = cli._parser().parse_args(["run", "--model", "gpt-test"])
        self.assertEqual(args.tasks, tuple(f"{n:02d}" for n in range(1, 51)))
        self.assertEqual(args.api_protocol, "auto")
        self.assertEqual(args.constructor_output_tokens, 128000)
        self.assertEqual(args.constructor_active_seconds, 14400)
        self.assertIsNone(args.constructor_input_tokens)
        self.assertEqual(args.cpus, 2)

    def test_context_window_requires_a_positive_integer(self):
        for invalid in ("0", "-2", "1.5", "01", "none"):
            with self.subTest(invalid=invalid), self.assertRaises(Exception):
                cli.positive_int(invalid)
        self.assertEqual(cli.positive_int("262144"), 262144)

    def test_constructor_output_budget_is_configurable_and_positive(self):
        args = cli._parser().parse_args(["run", "--model", "gpt-test", "--constructor-output-tokens", "64000"])
        self.assertEqual(args.constructor_output_tokens, 64000)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli._parser().parse_args(["run", "--model", "gpt-test", "--constructor-output-tokens", "0"])


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.errors = io.StringIO()
        self.environment = patch.dict(os.environ, {
            "SEEKGNOMES_API_KEY": "FAKE_SECRET_CLI_TEST_ONLY",
            "SEEKGNOMES_MODEL": "", "SEEKGNOMES_COMPANY": "",
            "SEEKGNOMES_HARNESS": "", "SEEKGNOMES_API_PROTOCOL": "auto",
            "SEEKGNOMES_API_BASE_URL": "",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def invoke(self, args):
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.errors):
            return cli.main(args)

    def test_resume_uses_saved_campaign_without_fresh_preflight(self):
        path = PROJECT / "runs/saved-checkpoint"
        with patch.object(cli, "inspect_environment", side_effect=AssertionError("resume reads the saved plan")), \
                patch("revisions.seekgnomes_v1_2.campaign.resume_campaign", return_value={"status": "finished"}) as resume:
            self.assertEqual(self.invoke(["resume", str(path)]), 0)
        resume.assert_called_once_with(path, api_key="FAKE_SECRET_CLI_TEST_ONLY")
        self.assertNotIn("FAKE_SECRET_CLI_TEST_ONLY", self.output.getvalue())

    def test_print_config_never_checks_runtime_or_prompts_for_key(self):
        with patch.object(cli, "inspect_environment", side_effect=AssertionError("dry config must not inspect runtime")), \
                patch.object(cli.getpass, "getpass", side_effect=AssertionError("dry config must not prompt")):
            code = self.invoke(["run", "--model", "router/gpt-example", "--company", "openai",
                                "--api-base-url", "https://api.example.com/v1", "--tasks", "2", "--print-config"])
        self.assertEqual(code, 0)
        data = json.loads(self.output.getvalue())
        self.assertEqual(data["model"], "router/gpt-example")
        self.assertEqual(data["protocol"], "seekgnomes-harbor-state/v1")
        self.assertEqual(data["candidate_format"], "state")
        self.assertEqual(data["harness_selection"]["name"], "codex")
        self.assertEqual(data["harness_selection"]["api_protocol"], "responses")
        self.assertEqual(data["task_ids"], ["02"])
        self.assertEqual(data["constructor_output_tokens"], 128000)
        self.assertEqual(data["closing_output_tokens"], 50000)
        self.assertIsNone(data["constructor_input_tokens"])
        self.assertEqual(data["constructor_active_seconds"], 14400)
        self.assertEqual(data["cpus"], 2)
        self.assertNotIn("FAKE_SECRET_CLI_TEST_ONLY", self.output.getvalue())

    def test_mismatched_native_protocol_rejected_before_any_run(self):
        with patch.object(cli, "inspect_environment", side_effect=AssertionError("selection must reject first")):
            code = self.invoke(["run", "--model", "gpt-example", "--api-protocol", "messages",
                                "--api-base-url", "https://api.example.com/v1", "--print-config"])
        self.assertEqual(code, 2)
        self.assertIn("requires", self.errors.getvalue())

    def test_opaque_model_requires_company_before_harness_fallback(self):
        with patch.object(sys.stdin, "isatty", return_value=False), \
                patch.object(cli, "inspect_environment", side_effect=AssertionError("company selection must precede API setup")):
            code = self.invoke(["run", "--model", "deployment/science-model-123",
                                "--api-base-url", "https://api.example.com/v1", "--print-config"])
        self.assertEqual(code, 2)
        self.assertIn("--company", self.errors.getvalue())

    def test_kimi_requires_explicit_context_in_noninteractive_mode(self):
        with patch.object(sys.stdin, "isatty", return_value=False):
            code = self.invoke(["run", "--model", "kimi-k3.1-pro",
                                "--api-base-url", "https://api.example.com/v1", "--print-config"])
        self.assertEqual(code, 2)
        self.assertIn("--model-context-tokens", self.errors.getvalue())

    def test_kimi_context_is_preserved_in_public_plan(self):
        code = self.invoke(["run", "--model", "kimi-k3.1-pro", "--model-context-tokens", "262144",
                            "--api-base-url", "https://api.example.com/v1", "--print-config"])
        self.assertEqual(code, 0)
        data = json.loads(self.output.getvalue())
        self.assertEqual(data["model_context_tokens"], 262144)

    def test_secret_in_public_model_field_is_redacted_even_for_dry_config(self):
        code = self.invoke(["run", "--model", "gpt-FAKE_SECRET_CLI_TEST_ONLY", "--company", "openai",
                            "--api-base-url", "https://api.example.com/v1", "--print-config"])
        self.assertEqual(code, 2)
        self.assertNotIn("FAKE_SECRET_CLI_TEST_ONLY", self.output.getvalue() + self.errors.getvalue())

    def test_run_dispatches_to_new_campaign_with_selection_controls(self):
        runner = Mock(return_value={"status": "finished"})
        module = types.ModuleType("revisions.seekgnomes_v1_2.campaign")
        module.run_campaign = runner
        out = PROJECT / "runs/cli-v12-test-nonexistent"
        with patch.object(cli, "inspect_environment", return_value={"ok": True, "external_api_calls": 0}), \
                patch.dict(sys.modules, {module.__name__: module}):
            code = self.invoke(["run", "--model", "opaque-model", "--company", "acme",
                                "--api-base-url", "https://api.example.com/v1", "--tasks", "1,2",
                                "--out", str(out), "--no-web", "--constructor-output-tokens", "96000",
                                "--cpus", "4", "--constructor-active-seconds", "7200",
                                "--constructor-input-tokens", "1000000"])
        self.assertEqual(code, 0)
        kwargs = runner.call_args.kwargs
        self.assertEqual(kwargs["model"], "opaque-model")
        self.assertEqual(kwargs["company"], "acme")
        self.assertEqual(kwargs["task_ids"], ("01", "02"))
        self.assertFalse(kwargs["web"])
        self.assertEqual(kwargs["constructor_output_tokens"], 96000)
        self.assertEqual(kwargs["cpus"], 4)
        self.assertEqual(kwargs["constructor_active_seconds"], 7200)
        self.assertEqual(kwargs["constructor_input_tokens"], 1000000)
        self.assertEqual(kwargs["api_key"], "FAKE_SECRET_CLI_TEST_ONLY")
        self.assertNotIn("FAKE_SECRET_CLI_TEST_ONLY", self.output.getvalue())
        self.assertFalse(out.exists())

    def test_failed_preflight_does_not_dispatch_or_request_key(self):
        with patch.object(cli, "inspect_environment", return_value={"ok": False, "failures": ["missing harbor"]}), \
                patch.object(cli.getpass, "getpass", side_effect=AssertionError("preflight rejects first")):
            code = self.invoke(["run", "--model", "gpt-example", "--api-base-url", "https://api.example.com/v1"])
        self.assertEqual(code, 1)

    def test_self_test_combines_real_checker_and_real_harbor_receipts(self):
        module = types.ModuleType("revisions.seekgnomes_v1_2.self_test")
        module.run_self_test = Mock(return_value={"ok": True, "external_api_calls": 0,
            "scientific_trial": False, "strict_checker": {"ok": True}, "harbor": {"ok": True}})
        with patch.dict(sys.modules, {module.__name__: module}):
            code = self.invoke(["self-test"])
        self.assertEqual(code, 0)
        data = json.loads(self.output.getvalue())
        self.assertFalse(data["scientific_trial"])
        self.assertEqual(data["external_api_calls"], 0)
        self.assertTrue(data["strict_checker"]["ok"])
        self.assertTrue(data["harbor"]["ok"])


if __name__ == "__main__":
    unittest.main()
