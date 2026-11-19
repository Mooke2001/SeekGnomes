"""Native CLI launch contracts; process and network isolation belong to runtime.

This module neither invokes a model nor installs software. Configuration paths
are paths inside a role's sandbox. Only the role-scoped proxy capability may be
rendered into these private files; the upstream API key stays in the broker.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from .harnesses import Selection, harness_record, installation_manifest


ROLE_TOKEN_PLACEHOLDER = "__SEEKGNOMES_PRIVATE_ROLE_CAPABILITY__"
DEFAULT_MCP_COMMAND = (
    "/usr/bin/python3", "-I", "-B", "/opt/seekgnomes/role_bridge.py", "mcp"
)
# Protection against a lost native tool call, not a Formalist token/time budget.
MCP_TOOL_TIMEOUT_SECONDS = 2147483
MCP_TOOL_TIMEOUT_MS = MCP_TOOL_TIMEOUT_SECONDS * 1000
# Broker owns the 600 s upstream deadline. Native guards allow replay overhead.
REQUEST_TIMEOUT_MS = 900000


@dataclass(frozen=True)
class NativeInvocation:
    harness: str
    version: str
    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: str
    stdin_path: str | None
    config_files: Mapping[str, str]
    credential_aliases: Mapping[str, str]
    session_files: tuple[str, ...]
    resume_id: str | None = None
    resume_last: bool = False

    def environment(self, role_token: str, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """Build an allowlisted environment without inheriting caller settings."""
        _validate_role_token(role_token)
        result = dict(self.env)
        if extra:
            result.update(extra)
        # Aliases map names to the input capability variable, never a key value.
        for target, source in self.credential_aliases.items():
            result[source] = role_token
            result[target] = role_token
        return result

    def render_config_files(self, role_token: str) -> dict[str, str]:
        _validate_role_token(role_token)
        # The replacement is inside JSON/TOML string literals. Escape as JSON
        # string content, rather than allowing a capability to alter the config.
        escaped = json.dumps(role_token, ensure_ascii=True)[1:-1]
        return {path: body.replace(ROLE_TOKEN_PLACEHOLDER, escaped) for path, body in self.config_files.items()}

    def snapshot(self) -> dict[str, Any]:
        """Safe provenance. Call before injecting a role capability."""
        return {
            "harness": self.harness, "version": self.version,
            "argv": list(self.argv), "env": dict(self.env), "cwd": self.cwd,
            "stdin_path": self.stdin_path, "config_files": dict(self.config_files),
            "credential_aliases": dict(self.credential_aliases),
            "session_files": list(self.session_files), "resume_id": self.resume_id,
            "resume_last": self.resume_last,
        }


def _validate_role_token(value: str) -> None:
    if not value or any(ord(c) < 32 for c in value):
        raise ValueError("A nonempty role-scoped proxy capability is required")


def _path(value: str | Path, label: str) -> str:
    value = str(value)
    if not Path(value).is_absolute() or ".." in Path(value).parts or any(ord(c) < 32 for c in value):
        raise ValueError(f"{label} must be an absolute sandbox path without traversal")
    return value.rstrip("/") or "/"


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True)


def _json_file(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=True) + "\n"


def build_invocation(
    selection: Selection,
    workspace: str | Path,
    home: str | Path,
    prompt_file: str | Path,
    endpoint: str,
    api_key_env: str = "SEEKGNOMES_ROLE_TOKEN",
    resume_id: str | None = None,
    mcp_command: Sequence[str] = DEFAULT_MCP_COMMAND,
    model_context_size: int | None = None,
    resume_last: bool = False,
) -> NativeInvocation:
    """Create a launch plan for an already isolated role.

    Native CLIs retain their own session and context handling. The supervisor
    must mount only this role's workspace/home, clear inherited environment,
    supply a private loopback API endpoint, and enforce budgets externally.
    Config files are returned, never written here. Version probes and unit
    tests verify contracts; they are not successful model-run evidence.
    """
    if not selection.supported:
        raise ValueError(selection.unsupported_reason or "Unsupported native harness")
    work = _path(workspace, "workspace")
    root = _path(home, "home")
    prompt = _path(prompt_file, "prompt_file")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
        raise ValueError("api_key_env must be an environment variable name")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("endpoint must be an HTTP(S) API root without credentials, query or fragment")
    endpoint = endpoint.rstrip("/")
    # Anthropic/GenAI SDKs append their own version path. OpenAI and DSH's
    # adapter instead expect (or explicitly reuse) the final /v1 API segment.
    unversioned_endpoint = endpoint[:-3] if endpoint.endswith("/v1") else endpoint
    if not mcp_command or any(not isinstance(a, str) or not a or "\x00" in a for a in mcp_command):
        raise ValueError("mcp_command must be a nonempty argv sequence")
    if resume_id is not None and (not resume_id or any(ord(c) < 32 for c in resume_id)):
        raise ValueError("resume_id must be a nonempty native session ID")
    if type(resume_last) is not bool or (resume_last and resume_id is not None):
        raise ValueError("resume_last must be boolean and is mutually exclusive with resume_id")
    if selection.name == "dsh" and resume_last:
        raise ValueError("DSH has no native latest-session flag; read dsh_session_id_from_events(stdout_path) and pass resume_id")
    if model_context_size is not None and (isinstance(model_context_size, bool) or not isinstance(model_context_size, int) or model_context_size <= 0):
        raise ValueError("model_context_size must be a positive integer")
    mcp = {"command": mcp_command[0], "args": list(mcp_command[1:])}
    env = {
        "HOME": root,
        "XDG_CONFIG_HOME": f"{root}/.config",
        "XDG_CACHE_HOME": f"{root}/.cache",
        "XDG_DATA_HOME": f"{root}/.local/share",
        "XDG_STATE_HOME": f"{root}/.local/state",
        "PATH": "/opt/seekgnomes/harness/bin:/usr/local/bin:/usr/bin:/bin",
        "TMPDIR": f"{root}/tmp", "TERM": "dumb", "NO_COLOR": "1",
    }
    configs: dict[str, str] = {}
    aliases: dict[str, str] = {api_key_env: api_key_env}
    stdin: str | None = None
    name, model = selection.name, selection.model
    # Some native CLIs accept only inline prompts. A fixed file-reference seed
    # avoids OS argv limits without changing their context-management behavior.
    seed = f"Read the complete task in {_j(prompt)} and carry it out. Preserve all instructions and definitions in that file."

    if name == "codex":
        codex_home = f"{root}/.codex"
        env["CODEX_HOME"] = codex_home
        configs[f"{codex_home}/config.toml"] = (
            f"model = {_j(model)}\nmodel_provider = \"seekgnomes\"\n"
            "approval_policy = \"never\"\nsandbox_mode = \"danger-full-access\"\n"
            "web_search = \"disabled\"\n"
            "[agents]\nenabled = false\n"
            "[features]\nmulti_agent = false\nmulti_agent_v2 = false\nmulti_agent_mode = false\n"
            "[model_providers.seekgnomes]\nname = \"SeekGnomes role broker\"\n"
            f"base_url = {_j(endpoint)}\nenv_key = {_j(api_key_env)}\nwire_api = \"responses\"\n"
            "supports_websockets = false\nrequest_max_retries = 2\nstream_max_retries = 2\n"
            f"stream_idle_timeout_ms = {REQUEST_TIMEOUT_MS}\n"
            f"[projects.{_j(work)}]\ntrust_level = \"trusted\"\n"
            "[mcp_servers.seekgnomes]\n"
            f"command = {_j(mcp['command'])}\nargs = {_j(mcp['args'])}\n"
            f"startup_timeout_sec = 60\ntool_timeout_sec = {MCP_TOOL_TIMEOUT_SECONDS}\n"
        )
        common = ["--json", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox"]
        if resume_id or resume_last:
            argv = ["codex", "exec", "resume", *common, *(["--last"] if resume_last else [resume_id]), "-"]
        else:
            argv = ["codex", "exec", *common, "-C", work, "-"]
        stdin = prompt
        sessions = (f"{codex_home}/sessions", f"{codex_home}/state_5.sqlite")

    elif name == "claude-code":
        config_dir = f"{root}/.claude"
        env.update({"CLAUDE_CONFIG_DIR": config_dir, "ANTHROPIC_BASE_URL": unversioned_endpoint,
                    "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": model, "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
                    "ANTHROPIC_DEFAULT_OPUS_MODEL": model, "ANTHROPIC_DEFAULT_FABLE_MODEL": model,
                    "ANTHROPIC_DEFAULT_MODEL": model, "ANTHROPIC_SMALL_FAST_MODEL": model,
                    "CLAUDE_CODE_SUBAGENT_MODEL": model, "MCP_TOOL_TIMEOUT": str(MCP_TOOL_TIMEOUT_MS),
                    "CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT": "0", "API_TIMEOUT_MS": str(REQUEST_TIMEOUT_MS),
                    "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS": str(REQUEST_TIMEOUT_MS),
                    "CLAUDE_STREAM_IDLE_TIMEOUT_MS": str(REQUEST_TIMEOUT_MS),
                    "CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS": str(REQUEST_TIMEOUT_MS),
                    "DISABLE_AUTOUPDATER": "1"})
        aliases["ANTHROPIC_API_KEY"] = api_key_env
        mcp_path = f"{config_dir}/seekgnomes-mcp.json"
        configs[mcp_path] = _json_file({"mcpServers": {"seekgnomes": {**mcp, "timeout": MCP_TOOL_TIMEOUT_MS}}})
        configs[f"{config_dir}/settings.json"] = _json_file({
            "permissions": {"defaultMode": "bypassPermissions"},
            "enableAllProjectMcpServers": False,
        })
        argv = ["claude", "-p", "--verbose", "--output-format", "stream-json",
                "--dangerously-skip-permissions", "--model", model,
                "--strict-mcp-config", "--mcp-config", mcp_path,
                "--setting-sources", "user", "--disallowedTools", "Agent,Task,Workflow,Skill,WebSearch,WebFetch"]
        if resume_id:
            argv += ["--resume", resume_id]
        elif resume_last:
            argv += ["--continue"]
        stdin = prompt
        sessions = (f"{config_dir}/projects",)

    elif name == "kimi-code":
        if model_context_size is None:
            raise ValueError("Kimi Code requires --model-context-tokens for a custom endpoint model; do not guess its context window")
        kimi_home = f"{root}/.kimi-code"
        env["KIMI_CODE_HOME"] = kimi_home
        # This pinned CLI's shared watcher service and config reload honour
        # the native switch. Role definitions/configs are fixed during a run.
        env["KIMI_CODE_WATCH"] = "0"
        env["KIMI_CODE_LLM_HEADERS_TIMEOUT_MS"] = str(REQUEST_TIMEOUT_MS)
        provider_type = {"chat_completions": "openai", "responses": "openai_responses", "messages": "anthropic", "gemini": "google-genai"}[selection.api_protocol]
        kimi_endpoint = unversioned_endpoint if selection.api_protocol in ("messages", "gemini") else endpoint
        configs[f"{kimi_home}/config.toml"] = (
            "default_model = \"seekgnomes\"\ntelemetry = false\nauto_session_title = false\n"
            "merge_all_available_skills = false\n"
            "[providers.seekgnomes]\n"
            f"type = {_j(provider_type)}\nbase_url = {_j(kimi_endpoint)}\napi_key_env = {_j(api_key_env)}\n"
            "[models.seekgnomes]\nprovider = \"seekgnomes\"\n"
            f"model = {_j(model)}\nmax_context_size = {model_context_size}\ncapabilities = [\"tool_use\"]\n"
            "[tools]\ndisabled = [\"Agent\", \"AgentSwarm\", \"WebSearch\", \"WebFetch\"]\n"
        )
        configs[f"{kimi_home}/mcp.json"] = _json_file({"mcpServers": {"seekgnomes": {**mcp, "toolTimeoutMs": MCP_TOOL_TIMEOUT_MS}}})
        argv = ["kimi", "--prompt", seed, "--output-format", "stream-json"]
        if resume_id:
            argv += ["--session", resume_id]
        elif resume_last:
            argv += ["--continue"]
        sessions = (f"{kimi_home}/sessions",)

    elif name == "gemini-cli":
        gemini_home = f"{root}/.gemini"
        env.update({"GOOGLE_GEMINI_BASE_URL": unversioned_endpoint, "GEMINI_CLI_TRUST_WORKSPACE": "true",
                    "GEMINI_API_HEADERS_TIMEOUT_MS": str(REQUEST_TIMEOUT_MS)})
        aliases["GEMINI_API_KEY"] = api_key_env
        configs[f"{gemini_home}/settings.json"] = _json_file({
            "security": {"auth": {"selectedType": "gemini-api-key"}},
            "mcpServers": {"seekgnomes": {**mcp, "timeout": MCP_TOOL_TIMEOUT_MS}},
            "telemetry": {"enabled": False},
            "experimental": {"enableAgents": False, "dynamicModelConfiguration": True},
            "tools": {"exclude": ["google_web_search", "web_fetch", "invoke_agent"]},
            "model": {"requestTimeoutMs": REQUEST_TIMEOUT_MS},
            "modelConfigs": {"modelIdResolutions": {model: {"default": model, "contexts": []}}},
        })
        argv = ["gemini", "--yolo", "--model", model, "--prompt", seed, "--output-format", "stream-json"]
        if resume_id:
            argv += ["--resume", resume_id]
        elif resume_last:
            argv += ["--resume", "latest"]
        sessions = (f"{gemini_home}/tmp",)

    elif name == "grok-build":
        grok_home = f"{root}/.grok"
        configs[f"{grok_home}/config.toml"] = (
            f"disable_web_search = true\ninference_idle_timeout_secs = {REQUEST_TIMEOUT_MS // 1000}\n"
            "[cli]\nauto_update = false\n[features]\ntelemetry = false\n"
            "[harness]\ndisable_codebase_upload = true\n"
            "[subagents]\nenabled = false\n"
            f"[models]\ndefault = {_j(model)}\nsession_summary = {_j(model)}\n"
            f"image_description = {_j(model)}\nweb_search = {_j(model)}\n"
            f"[model.{_j(model)}]\nname = {_j(model)}\nmodel = {_j(model)}\n"
            f"base_url = {_j(endpoint)}\nenv_key = {_j(api_key_env)}\napi_backend = {_j(selection.api_protocol)}\n"
            "[mcp_servers.seekgnomes]\n"
            f"command = {_j(mcp['command'])}\nargs = {_j(mcp['args'])}\n"
            f"tool_timeout_sec = {MCP_TOOL_TIMEOUT_SECONDS}\n"
        )
        env["GROK_SUBAGENTS"] = "0"
        argv = ["grok", "--no-auto-update", "--no-subagents", "--disallowed-tools", "workflow,scheduler_create",
                "--single", seed, "--always-approve",
                "--output-format", "streaming-json", "--model", model]
        if resume_id:
            argv += ["--resume", resume_id]
        elif resume_last:
            argv += ["--continue"]
        sessions = (f"{grok_home}/sessions",)

    elif name == "qwen-code":
        qwen_home = f"{root}/.qwen"
        env.update({"OPENAI_BASE_URL": endpoint, "OPENAI_MODEL": model,
                    "QWEN_CODE_API_TIMEOUT_MS": str(REQUEST_TIMEOUT_MS),
                    "QWEN_STREAM_IDLE_TIMEOUT_MS": str(REQUEST_TIMEOUT_MS),
                    "QWEN_STREAM_MAX_LIFETIME_MS": str(REQUEST_TIMEOUT_MS)})
        aliases["OPENAI_API_KEY"] = api_key_env
        configs[f"{qwen_home}/settings.json"] = _json_file({
            "security": {"auth": {"selectedType": "openai"}},
            "mcpServers": {"seekgnomes": {**mcp, "timeout": MCP_TOOL_TIMEOUT_MS}}, "telemetry": {"enabled": False},
            "tools": {"disabled": ["agent", "send_message", "workflow", "web_search", "web_fetch"]},
            "model": {"maxToolCallsPerTurn": 0,
                      "generationConfig": {"timeout": REQUEST_TIMEOUT_MS, "streamIdleTimeoutMs": REQUEST_TIMEOUT_MS}},
        })
        argv = ["qwen", "--yolo", "--auth-type", "openai", "--chat-recording", "--advisor", "off",
                "--workflow-mode", "off", "--model", model, "--prompt", seed, "--output-format", "stream-json"]
        if resume_id:
            argv += ["--resume", resume_id]
        elif resume_last:
            argv += ["--continue"]
        sessions = (f"{qwen_home}/projects", f"{qwen_home}/tmp")

    elif name == "dsh":
        dsh_home = f"{root}/.dsh"
        env.update({"DSH_HOME": dsh_home, "DEEPSEEK_BASE_URL": endpoint,
                    "DSH_TELEMETRY_DISABLED": "1", "DSH_PERMISSION_MODE": "danger-full-access"})
        aliases["DEEPSEEK_API_KEY"] = api_key_env
        # JSON is valid YAML. DSH patches operate by stable plugin ID; adding a
        # second llm/default row would duplicate routes rather than reconfigure.
        patch = [
            {"id": "agent-default-model", "config": {"provider": "deepseek-official", "model": model}},
            {"id": "llm-deepseek", "config": {"baseURL": endpoint, "apiKeyEnv": api_key_env,
                                               "models": [{"id": model}], "streamIdleTimeoutMs": REQUEST_TIMEOUT_MS}},
            {"id": "tool-web", "disabled": True},
            {"id": "tool-subagent", "disabled": True},
            {"id": "tool-subagent-fork", "disabled": True},
            {"id": "tool-agent-team", "disabled": True},
            {"id": "tool-workflow", "disabled": True},
            {"id": "session-title-llm", "disabled": True},
            {"id": "skill", "disabled": True},
            {"insert": [{"id": "seekgnomes-mcp", "name": "@deepseek-ai/dsh-mcp-client", "config": {
                "serverName": "seekgnomes", "transport": "stdio", **mcp,
                "toolCallTimeoutMs": MCP_TOOL_TIMEOUT_MS, "failOnStartupError": True,
            }}]},
        ]
        configs[f"{dsh_home}/profiles/headless/cordis.patch.yml"] = _json_file(patch)
        argv = ["dsh", "--profile", "headless", "--json", "--permission-mode", "danger-full-access", "--approval-policy", "never", "-"]
        if resume_id:
            argv[3:3] = ["--session-id", resume_id]
        stdin = prompt
        sessions = (f"{dsh_home}/sessions", f"{dsh_home}/storages")

    elif name == "zcode":
        zhome = f"{root}/.zcode"
        env.update({"ZCODE_DATA_BASE_DIR": zhome,
                    "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE": f"{zhome}/v2/provider_config.json",
                    "ZCODE_STORAGE_DIR": f"{zhome}/storage", "ZCODE_SESSION_DB_PATH": f"{zhome}/sessions.db",
                    "ZCODE_LOG_DIR": f"{zhome}/logs"})
        api_type = {"chat_completions": "openai-chat-completions", "responses": "openai-responses", "messages": "anthropic-messages"}[selection.api_protocol]
        configs[f"{zhome}/v2/provider_config.json"] = _json_file({
            "schemaVersion": 1, "config": {
                "providerConfigRules": {"providerRules": [{"providerId": "seekgnomes", "providerName": "SeekGnomes", "config": {
                    "group": "standard-personal", "access": {"type": "api-key", "apiKey": ROLE_TOKEN_PLACEHOLDER},
                    "api": {"type": api_type, "baseUrl": endpoint}, "personalModelIds": [model],
                }}]},
                "modelConfigRules": {"providerModelRules": [], "manualProviderModelRules": []},
                "defaultModelSelection": {"providerId": "seekgnomes", "modelId": model},
            },
        })
        configs[f"{zhome}/cli/config.json"] = _json_file({
            "features": {"mcp": True, "subagent": False, "memory": False, "skill": False},
            "mcp": {"servers": {"seekgnomes": {"type": "stdio", **mcp, "enabled": True, "timeoutMs": MCP_TOOL_TIMEOUT_MS}}},
            "memory": {"use": False}, "plugins": {"enabled": False}, "skills": {"enabled": False},
            "hooks": {"enabled": False}, "modelStream": {"idleTimeoutMs": REQUEST_TIMEOUT_MS},
        })
        argv = ["zcode", "--prompt", seed, "--cwd", work, "--mode", "yolo", "--output-format", "stream-json",
                "--disallowed-tools", "CreateSession,ForkSession,SendToSession,WaitSessions"]
        if resume_id:
            argv += ["--resume", resume_id]
        elif resume_last:
            argv += ["--continue"]
        sessions = (f"{zhome}/sessions.db", f"{zhome}/storage")

    elif name == "mimo-code":
        config_path = f"{root}/.config/mimocode/mimocode.json"
        env.update({"MIMOCODE_CONFIG": config_path, "MIMOCODE_DISABLE_PROJECT_CONFIG": "1",
                    "MIMOCODE_DISABLE_CLAUDE_CODE_MCP": "1", "MIMOCODE_DISABLE_AUTOUPDATE": "1"})
        route = f"seekgnomes/{model}"
        configs[config_path] = _json_file({
            "model": route, "small_model": route, "enabled_providers": ["seekgnomes"],
            "provider": {"seekgnomes": {"npm": "@ai-sdk/openai-compatible", "name": "SeekGnomes role broker",
                "options": {"baseURL": endpoint, "apiKey": "{env:" + api_key_env + "}",
                            "timeout": REQUEST_TIMEOUT_MS, "headerTimeout": REQUEST_TIMEOUT_MS,
                            "chunkTimeout": REQUEST_TIMEOUT_MS}, "models": {model: {}}}},
            "mcp": {"seekgnomes": {"type": "local", "command": list(mcp_command), "enabled": True, "timeout": MCP_TOOL_TIMEOUT_MS}},
            "permission": {"*": "allow", "actor": "deny", "task": "deny", "workflow": "deny", "websearch": "deny", "webfetch": "deny"},
            "instructions": [],
        })
        argv = ["mimo", "run", "--format", "json", "--model", route, "--dir", work,
                "--dangerously-skip-permissions", seed]
        if resume_id:
            argv += ["--session", resume_id]
        elif resume_last:
            argv += ["--continue"]
        sessions = (f"{root}/.local/share/mimocode",)
    else:
        raise ValueError(f"No native driver for {name}")

    return NativeInvocation(name, selection.version, tuple(argv), env, work, stdin,
                            configs, aliases, sessions, resume_id, resume_last)


def dsh_session_id_from_events(stdout_path: str | Path) -> str | None:
    """Read DSH 0.2.2-rc.1's native opening JSON event, never infer a new ID.

    A private role's append-only stdout may contain multiple resumptions. Only
    top-level ``type=session, sessionId=<string>`` native events count; IDs in
    assistant text, final results or nested payloads do not. A missing/invalid
    event returns None so the supervisor can fail closed instead of resetting
    the conversation. DSH does not expose a headless ``--continue`` flag.
    """
    latest = None
    try:
        with Path(stdout_path).open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(event, dict) or event.get("type") != "session":
                    continue
                identifier = event.get("sessionId")
                if isinstance(identifier, str) and identifier and not any(ord(c) < 32 for c in identifier):
                    latest = identifier
    except FileNotFoundError:
        return None
    return latest


def probe_harness(selection: Selection, search_path: str | None = None) -> dict[str, Any]:
    """Read only ``--version``. An installed CLI is not an API connectivity test."""
    binary = harness_record(selection)["binary"]
    effective_path=search_path or "/opt/seekgnomes/harness/bin:/usr/local/bin:/usr/bin:/bin"
    executable = shutil.which(binary, path=effective_path)
    result: dict[str, Any] = {"ok": False, "executable": executable, "expected_version": selection.version,
                              "observed_version": None, "failures": []}
    if not executable:
        result["failures"].append(f"Missing native CLI: {binary}=={selection.version}")
        return result
    try:
        probe_root=Path(__file__).resolve().parents[2]/'runs'
        if probe_root.resolve()!=probe_root:raise ValueError('Native probe output must not use an aliased path')
        probe_root.mkdir(parents=True,exist_ok=True)
        # Some pinned CLIs initialize a cache even for --version. A private
        # writable home permits that without reading the user's global config.
        with tempfile.TemporaryDirectory(prefix='native-version-probe-',dir=probe_root) as folder:
            home=Path(folder)
            settings={"PATH":effective_path,"HOME":str(home),"NO_COLOR":"1","TERM":"dumb"}
            for variable,name in (('XDG_CONFIG_HOME','config'),('XDG_CACHE_HOME','cache'),
                    ('XDG_DATA_HOME','data'),('XDG_STATE_HOME','state'),('TMPDIR','tmp')):
                target=home/name;target.mkdir();settings[variable]=str(target)
            probe = subprocess.run([executable, "--version"], text=True, capture_output=True,
                timeout=15, check=False, env=settings)
        output = (probe.stdout + "\n" + probe.stderr).strip()
        result['probe_output']=output[:4096]
        match = re.search(r"(?<![\d.])\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", output)
        result["observed_version"] = match.group(0) if match else None
        if probe.returncode:
            result["failures"].append(f"Version probe exited {probe.returncode}")
        if result["observed_version"] != selection.version:
            result["failures"].append(f"Expected version {selection.version}, observed {result['observed_version']!r}")
        result["ok"] = not result["failures"]
    except (OSError, subprocess.TimeoutExpired) as exc:
        result["failures"].append(f"Version probe failed: {type(exc).__name__}")
    return result


def cancel_owned_process_group(pid: int, sig: int = signal.SIGTERM) -> None:
    """Signal a dedicated CLI session, refusing the supervisor's process group."""
    if pid <= 1 or pid == os.getpid() or os.getpgid(pid) != pid:
        raise ValueError("Only a dedicated child process-group leader can be cancelled")
    # Execution targets Linux containers. A group leader alone is insufficient:
    # it could be another user's unrelated service. Require our direct child.
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        parent = next(int(line.split()[1]) for line in status.splitlines() if line.startswith("PPid:"))
    except (OSError, StopIteration, ValueError) as exc:
        raise ValueError("Cannot establish ownership of the child process group") from exc
    if parent != os.getpid():
        raise ValueError("Refusing to cancel a process group not owned by this supervisor")
    os.killpg(pid, sig)


__all__ = ["NativeInvocation", "build_invocation", "probe_harness", "installation_manifest",
           "cancel_owned_process_group", "dsh_session_id_from_events", "ROLE_TOKEN_PLACEHOLDER", "DEFAULT_MCP_COMMAND"]
