"""Offline launch/configuration checks; these do not claim paid API success."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT))
from revisions.seekgnomes_v1_2.harnesses import resolve_harness
from revisions.seekgnomes_v1_2.native import (
    MCP_TOOL_TIMEOUT_MS, ROLE_TOKEN_PLACEHOLDER,
    build_invocation, cancel_owned_process_group, dsh_session_id_from_events, probe_harness,
)

try:
    import tomllib
except ImportError:
    tomllib = None


MODELS = ("gpt-6.1-terra", "claude-fable-5-2", "kimi-k3.1-pro", "gemini-4-argon",
          "grok-4.7", "qwen3-coder", "deepseek-v4.1-flash", "glm-5.3", "mimo-v2.6-pro")


class NativeContractTests(unittest.TestCase):
    def invocation(self, model, **kwargs):
        return build_invocation(resolve_harness(model), "/work", "/home/agent", "/work/task.md",
                                "http://127.0.0.1:8080/v1", model_context_size=1048576, **kwargs)

    def test_all_launches_have_private_configs_and_only_the_fixed_mcp_server(self):
        for model in MODELS:
            with self.subTest(model=model):
                invocation = self.invocation(model)
                self.assertEqual(invocation.cwd, "/work")
                self.assertEqual(invocation.env["HOME"], "/home/agent")
                self.assertTrue(invocation.config_files)
                for path, body in invocation.config_files.items():
                    self.assertTrue(path.startswith("/home/agent/"))
                    if path.endswith((".json", ".yml")):
                        json.loads(body)
                    elif path.endswith(".toml") and tomllib:
                        tomllib.loads(body)
                combined = "\n".join(invocation.config_files.values())
                self.assertIn("seekgnomes", combined)
                self.assertIn("/opt/seekgnomes/role_bridge.py", combined)
                self.assertNotIn("https://api.", combined)

    def test_upstream_credentials_and_global_settings_are_not_inherited(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "upstream-key-must-not-leak", "HTTPS_PROXY": "global-proxy", "CODEX_HOME": "/private-user-home"}):
            invocation = self.invocation("gpt-6.1-terra")
            rendered_env = invocation.environment("sk-role-capability")
            self.assertNotIn("OPENAI_API_KEY", rendered_env)
            self.assertNotIn("HTTPS_PROXY", rendered_env)
            self.assertEqual(rendered_env["CODEX_HOME"], "/home/agent/.codex")
            self.assertNotIn("upstream-key-must-not-leak", json.dumps(invocation.snapshot()))
            self.assertNotIn("sk-role-capability", json.dumps(invocation.snapshot()))
            self.assertEqual(rendered_env["SEEKGNOMES_ROLE_TOKEN"], "sk-role-capability")

    def test_zcode_renders_capability_as_literal_escaped_json_not_env_placeholder(self):
        invocation = self.invocation("glm-5.3")
        self.assertIn(ROLE_TOKEN_PLACEHOLDER, "".join(invocation.config_files.values()))
        files = invocation.render_config_files('sk-role-"quoted\\token')
        provider = json.loads(files["/home/agent/.zcode/v2/provider_config.json"])
        access = provider["config"]["providerConfigRules"]["providerRules"][0]["config"]["access"]
        self.assertEqual(access["apiKey"], 'sk-role-"quoted\\token')
        self.assertNotIn(ROLE_TOKEN_PLACEHOLDER, "".join(files.values()))

    def test_full_prompt_stdin_and_specific_session_resume_are_explicit(self):
        for model in MODELS:
            with self.subTest(model=model):
                invocation = self.invocation(model, resume_id="test-session-123")
                self.assertIn("test-session-123", invocation.argv)
                self.assertNotIn("--last", invocation.argv)
                self.assertNotIn("--continue", invocation.argv)
                if invocation.harness in ("codex", "claude-code", "dsh"):
                    self.assertEqual(invocation.stdin_path, "/work/task.md")
                else:
                    self.assertIn("/work/task.md", " ".join(invocation.argv))
        self.assertIn("--resume", self.invocation("grok-4.7", resume_id="existing").argv)
        self.assertNotIn("--session-id", self.invocation("grok-4.7", resume_id="existing").argv)

    def test_latest_session_flags_are_real_and_never_silently_reset_dsh(self):
        for model in MODELS:
            if model.startswith("deepseek"):
                with self.assertRaisesRegex(ValueError, "DSH has no native"):
                    self.invocation(model, resume_last=True)
                continue
            argv = self.invocation(model, resume_last=True).argv
            expected = "--last" if model.startswith("gpt") else "--resume" if model.startswith("gemini") else "--continue"
            self.assertIn(expected, argv)
            with self.assertRaisesRegex(ValueError, "mutually exclusive"):
                self.invocation(model, resume_last=True, resume_id="existing")
        self.assertIn("--chat-recording", self.invocation("qwen3-coder").argv)

    def test_dsh_session_id_is_read_only_from_native_session_events(self):
        fixture_root = PROJECT / "tmp" / "seekgnomes-v12-harness-audit"
        fixture_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=fixture_root) as temp:
            path = Path(temp) / "native.stdout.log"
            self.assertIsNone(dsh_session_id_from_events(path))
            path.write_text('not json\n' + json.dumps({"type": "session", "sessionId": "session-real"}) + '\n'
                + json.dumps({"type": "final", "sessionId": "forged-in-other-event"}) + '\n'
                + json.dumps({"type": "assistant", "text": '{"type":"session","sessionId":"forged-in-text"}'}) + '\n')
            self.assertEqual(dsh_session_id_from_events(path), "session-real")

    def test_native_fixes_prevent_gemini_model_rewrite_and_kimi_watch_loop(self):
        invocation = self.invocation("gemini-4-argon")
        settings = json.loads(invocation.config_files["/home/agent/.gemini/settings.json"])
        self.assertTrue(settings["experimental"]["dynamicModelConfiguration"])
        self.assertEqual(settings["modelConfigs"]["modelIdResolutions"]["gemini-4-argon"],
                         {"default": "gemini-4-argon", "contexts": []})
        self.assertEqual(self.invocation("kimi-k3.1-pro").env["KIMI_CODE_WATCH"], "0")

    def test_native_extra_session_entrypoints_have_effective_vendor_controls(self):
        claude = self.invocation("claude-fable-5-2")
        claude_disallowed = set(claude.argv[claude.argv.index("--disallowedTools") + 1].split(","))
        self.assertTrue({"Agent", "Task", "Workflow", "Skill"}.issubset(claude_disallowed))
        grok = self.invocation("grok-4.7")
        self.assertIn("--no-subagents", grok.argv)
        grok_disallowed = set(grok.argv[grok.argv.index("--disallowed-tools") + 1].split(","))
        self.assertEqual(grok_disallowed, {"workflow", "scheduler_create"})
        gemini = self.invocation("gemini-4-argon")
        settings = json.loads(gemini.config_files["/home/agent/.gemini/settings.json"])
        # The generic experimental enableAgents setting alone does not remove
        # invoke_agent in pinned 0.68; tools.exclude also installs a DENY rule.
        self.assertIn("invoke_agent", settings["tools"]["exclude"])
        zcode = self.invocation("glm-5.3")
        disallow_index = zcode.argv.index("--disallowed-tools")
        self.assertEqual(set(zcode.argv[disallow_index + 1].split(",")),
                         {"CreateSession", "ForkSession", "SendToSession", "WaitSessions"})
        mimo = self.invocation("mimo-v2.6-pro")
        config = json.loads(mimo.config_files["/home/agent/.config/mimocode/mimocode.json"])
        # actor is a different permission from task. It may remain advertised,
        # but explicit deny must reject its run/spawn path before child creation.
        self.assertEqual(config["permission"]["actor"], "deny")
        self.assertEqual(config["permission"]["*"], "allow")

    def test_qwen_does_not_impose_a_hidden_per_turn_tool_call_budget(self):
        invocation = self.invocation("qwen3-coder")
        settings = json.loads(invocation.config_files["/home/agent/.qwen/settings.json"])
        # Pinned Qwen 0.27.4 otherwise has a 1000-call adaptive hard backstop.
        # Its actual config getter maps an explicit nonpositive value to Infinity.
        self.assertEqual(settings["model"]["maxToolCallsPerTurn"], 0)
        self.assertEqual(settings["model"]["generationConfig"]["timeout"], 900000)

    def test_formalist_handoff_is_not_cut_off_after_one_hour(self):
        self.assertGreater(MCP_TOOL_TIMEOUT_MS, 3600 * 1000)
        for model in MODELS:
            invocation = self.invocation(model)
            settings = json.dumps(invocation.snapshot())
            self.assertIn(str(MCP_TOOL_TIMEOUT_MS) if invocation.harness not in ("codex", "grok-build")
                          else str(MCP_TOOL_TIMEOUT_MS // 1000), settings)

    def test_kimi_requires_declared_context_window(self):
        with self.assertRaisesRegex(ValueError, "model-context-tokens"):
            build_invocation(resolve_harness("kimi-k3.1-pro"), "/work", "/home/agent", "/work/task.md", "http://localhost/v1")

    def test_api_roots_match_the_native_sdk_path_rules(self):
        self.assertEqual(self.invocation("claude-fable-5-2").env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8080")
        self.assertEqual(self.invocation("gemini-4-argon").env["GOOGLE_GEMINI_BASE_URL"], "http://127.0.0.1:8080")
        self.assertEqual(self.invocation("deepseek-v4.1-flash").env["DEEPSEEK_BASE_URL"], "http://127.0.0.1:8080/v1")
        self.assertIn('base_url = "http://127.0.0.1:8080/v1"',
                      self.invocation("gpt-6.1-terra").config_files["/home/agent/.codex/config.toml"])

    def test_model_metacharacters_are_single_argv_or_json_toml_values(self):
        invocation = self.invocation('claude-fable-5-2;$(touch /tmp/should-not-exist)')
        self.assertIn('claude-fable-5-2;$(touch /tmp/should-not-exist)', invocation.argv)
        self.assertNotIn("sh", invocation.argv)
        for invalid in ("http://user:secret@localhost/v1", "http://localhost/v1?key=secret", "file:///tmp/endpoint"):
            with self.assertRaises(ValueError):
                build_invocation(resolve_harness("gpt-6.1-terra"), "/work", "/home/agent", "/work/task.md", invalid)

    def test_fake_cli_version_probe_detects_missing_and_wrong_versions(self):
        selection = resolve_harness("gpt-6.1-terra")
        fixture_root = PROJECT / "tmp" / "seekgnomes-v12-harness-audit"
        fixture_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=fixture_root) as temp:
            self.assertFalse(probe_harness(selection, search_path=temp)["ok"])
            binary = Path(temp, "codex")
            binary.write_text("#!/bin/sh\nprintf 'codex-cli 0.167.1\\n'\n")
            binary.chmod(0o700)
            self.assertTrue(probe_harness(selection, search_path=temp)["ok"])
            binary.write_text("#!/bin/sh\nprintf 'codex-cli 0.169.0\\n'\n")
            self.assertFalse(probe_harness(selection, search_path=temp)["ok"])

    def test_cancel_refuses_supervisor_process_group(self):
        with self.assertRaises(ValueError):
            cancel_owned_process_group(os.getpid())


def _codex_fixture_sse(model, index, *, tool=None):
    """Actual Responses event shapes, with synthetic local data and usage."""
    response_id = f"resp_local_{index}"
    if tool is not None:
        custom = tool.get("type") == "custom"
        item = {"id": f"fc_local_{index}", "type": "custom_tool_call" if custom else "function_call",
                "call_id": f"call_local_{index}", "name": tool["name"], "status": "completed"}
        item["input" if custom else "arguments"] = tool.get("input", '{"action":"finish"}')
        if tool.get("namespace"):
            item["namespace"] = tool["namespace"]
    else:
        item = {"id": f"msg_local_{index}", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "Offline native MCP fixture completed.", "annotations": []}]}
    response = {"id": response_id, "object": "response", "created_at": 0, "model": model,
                "status": "completed", "output": [item], "error": None, "incomplete_details": None,
                "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18,
                          "input_tokens_details": {"cached_tokens": 0},
                          "output_tokens_details": {"reasoning_tokens": 0}}}
    events = [
        {"type": "response.created", "response": {**response, "status": "in_progress", "output": [], "usage": None}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {**item, "status": "in_progress", **({"input" if custom else "arguments": ""} if tool else {"content": []})}},
    ]
    if tool:
        field = "input" if custom else "arguments"
        event_prefix = "response.custom_tool_call_input" if custom else "response.function_call_arguments"
        events += [
            {"type": event_prefix + ".delta", "item_id": item["id"], "output_index": 0,
             "delta": item[field]},
            {"type": event_prefix + ".done", "item_id": item["id"], "output_index": 0,
             field: item[field]},
        ]
    else:
        events += [
            {"type": "response.content_part.added", "item_id": item["id"], "output_index": 0,
             "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}},
            {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0,
             "content_index": 0, "delta": item["content"][0]["text"]},
            {"type": "response.output_text.done", "item_id": item["id"], "output_index": 0,
             "content_index": 0, "text": item["content"][0]["text"]},
            {"type": "response.content_part.done", "item_id": item["id"], "output_index": 0,
             "content_index": 0, "part": item["content"][0]},
        ]
    events += [{"type": "response.output_item.done", "output_index": 0, "item": item},
               {"type": "response.completed", "response": response}]
    return b"".join(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode()
                    for event in events)


@unittest.skipUnless(os.environ.get("SEEKGNOMES_REAL_CODEX"), "Set SEEKGNOMES_REAL_CODEX to the pinned native executable")
class NativeLiveFixtureTests(unittest.TestCase):
    """Opt-in true CLI + bwrap + MCP + broker test, with no external API calls."""

    def test_real_codex_calls_mcp_finish_through_private_role_bridge(self):
        from revisions.seekgnomes_v1_2.budget import Ledger
        from revisions.seekgnomes_v1_2.gateway import RoleGateway
        from revisions.seekgnomes_v1_2.sandbox import RoleSandbox
        from revisions.seekgnomes_v1_2.tests.test_budget_gateway import provider_fixture

        binary = Path(os.environ["SEEKGNOMES_REAL_CODEX"]).resolve(strict=True)
        fixture_root = PROJECT / "tmp" / "seekgnomes-v12-zcode-build"
        fixture_root.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix="real-codex-fixture-", dir=fixture_root))
        harness = folder / "harness"
        (harness / "bin").mkdir(parents=True)
        shutil.copy2(binary, harness / "bin" / "codex")
        code_host = binary.with_name("codex-code-mode-host")
        if code_host.is_file():
            shutil.copy2(code_host, harness / "bin" / "codex-code-mode-host")
        work = folder / "workspace"
        work.mkdir()
        (work / "task.md").write_text("This is an offline plumbing test. Call the seekgnomes benchmark_action MCP tool with action finish, then finish.\n")
        model = "gpt-6.1-terra"
        calls = []
        requests = []

        def dispatch(value):
            calls.append(value)
            if value.get("method") == "list_tools":
                return {"tools": [{"name": "benchmark_action", "description": "Finish the local plumbing fixture.",
                    "inputSchema": {"type": "object", "properties": {"action": {"type": "string", "enum": ["finish"]}},
                                    "required": ["action"], "additionalProperties": False}}]}
            if value.get("method") == "call_tool" and value.get("name") == "benchmark_action":
                return {"ok": value.get("arguments") == {"action": "finish"}, "fixture": "finish accepted"}
            return {"ok": False, "error": "unexpected fixture tool"}

        def respond(handler):
            request = json.loads(handler.body)
            requests.append(request)
            tool = None
            if len(requests) % 2 == 1:
                def find_tool(entries, namespace=None):
                    for entry in entries:
                        if entry.get("type") == "namespace":
                            found = find_tool(entry.get("tools", []), entry.get("name"))
                            if found:
                                return found
                        elif "benchmark_action" in entry.get("name", ""):
                            return {"name": entry["name"], "namespace": namespace}
                    return None
                entries = list(request.get("tools", []))
                for item in request.get("input", []):
                    if item.get("type") == "additional_tools":
                        entries.extend(item.get("tools", []))
                tool = find_tool(entries)
                if tool is None:
                    # Codex 0.167's native Code Mode exposes deferred MCP tools
                    # through its actual JS host, rather than direct functions.
                    tool = {"type": "custom", "name": "exec", "namespace": "functions",
                            "input": 'const entry=ALL_TOOLS.find(x=>x.name.includes("benchmark_action")); '
                                     'if(!entry) throw new Error("fixture MCP tool missing"); '
                                     'text(await tools[entry.name]({action:"finish"}));'}
            raw = _codex_fixture_sse(model, len(requests), tool=tool)
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Content-Length", str(len(raw)))
            handler.end_headers()
            handler.wfile.write(raw)

        # The shared provider fixture consumes body before calling a plan.
        # A small wrapper exposes that same captured request to our responder.
        def plan(handler):
            handler.body = json.dumps(seen[-1]["payload"]).encode()
            respond(handler)

        gateway = sandbox = None
        outcome = {"fixture_directory": str(folder), "external_api_calls": 0, "model": model}
        try:
            with provider_fixture([plan]) as (endpoint, seen):
                ledger = Ledger(folder / "ledger.json", "constructor")
                gateway = RoleGateway(folder / "gateway", role="constructor", model=model,
                    endpoint=endpoint, api_key="offline-host-fixture-capability", protocol="responses",
                    ledger=ledger, tool_dispatch=dispatch).start()
                invocation = build_invocation(resolve_harness(model), "/work", "/home/agent", "/work/task.md",
                                              "http://127.0.0.1:8731/v1")
                sandbox = RoleSandbox(folder / "run", work, harness_root=harness)
                process = sandbox.start(invocation, gateway.socket_path)
                try:
                    code = process.wait(timeout=90)
                except subprocess.TimeoutExpired:
                    sandbox.stop()
                    self.fail(f"Actual Codex CLI fixture timed out; inspect {folder}")
                outcome.update(exit_code=code, tool_calls=calls, api_requests=requests, ledger=ledger.snapshot())
                self.assertEqual(code, 0, f"Inspect {folder}/run/native.stderr.log")
                self.assertTrue(any(item.get("method") == "call_tool" and item.get("name") == "benchmark_action"
                                    and item.get("arguments") == {"action": "finish"} for item in calls), str(folder))
                self.assertEqual(len(requests), 2, str(folder))
                self.assertEqual(ledger.snapshot()["output_tokens_charged"], 14)
                self.assertTrue(all(item["model"] == model for item in requests))
                # A closing phase resumes the actual native session and appends
                # to the same private logs/home/ledger, rather than starting over.
                (work / "closing.md").write_text("This is the offline closing fixture. Call benchmark_action with action finish once more, then finish.\n")
                closing = build_invocation(resolve_harness(model), "/work", "/home/agent", "/work/closing.md",
                                           "http://127.0.0.1:8731/v1", resume_last=True)
                command = sandbox.command(closing, gateway.socket_path)
                with (folder / "run/native.stdout.log").open("ab") as out, (folder / "run/native.stderr.log").open("ab") as err:
                    resumed = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                               env={}, start_new_session=True)
                    sandbox.process = resumed
                    try:
                        closing_code = resumed.wait(timeout=90)
                    except subprocess.TimeoutExpired:
                        sandbox.stop()
                        self.fail(f"Actual Codex native resume fixture timed out; inspect {folder}")
                self.assertEqual(closing_code, 0, str(folder))
                self.assertEqual(len(requests), 4)
                self.assertEqual(ledger.snapshot()["output_tokens_charged"], 28)
                events = [json.loads(line) for line in (folder / "run/native.stdout.log").read_text().splitlines() if line.startswith("{")]
                ids = [event["thread_id"] for event in events if event.get("type") == "thread.started"]
                self.assertEqual(len(ids), 2)
                self.assertEqual(ids[0], ids[1], "--last must resume the identical role session")
                outcome.update(closing_exit_code=closing_code, resumed_same_thread=True, native_thread_ids=ids,
                               tool_calls=calls, api_requests=requests, ledger=ledger.snapshot())
                namespaces = [entry.get("name") for request in requests
                    for item in request.get("input", []) if item.get("type") == "additional_tools"
                    for entry in item.get("tools", []) if entry.get("type") == "namespace"]
                self.assertNotIn("collaboration", namespaces,
                                 "Native Codex delegation remains available despite the two-role policy")
        finally:
            if sandbox:
                sandbox.stop()
            if gateway:
                gateway.close()
            (folder / "offline-fixture-result.json").write_text(json.dumps(outcome, indent=2) + "\n")


if __name__ == "__main__":
    unittest.main()
