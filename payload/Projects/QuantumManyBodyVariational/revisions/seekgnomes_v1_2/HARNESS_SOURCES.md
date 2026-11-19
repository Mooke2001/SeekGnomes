# Official harness selection and launch evidence

Registry frozen on 2026-11-17. The model company selects the native harness.
Codex is the fallback for an unrecognized company or a company for which this
registry has no verified official harness. Every fallback records its reason;
it does not establish that the company has no harness. A router namespace such
as `infi/`, `ora/`, `bh2/`, or an OpenAI-compatible `openai/` namespace is not
evidence of model authorship. Model IDs pass through unchanged. An explicit
company supports opaque aliases; conflicting recognizable model families are
rejected. Explicit harness flags cannot bypass the company rule.

| Company | Official harness | Fixed version | Native wire protocol in this package | Harbor 0.22.0 integration |
| --- | --- | --- | --- | --- |
| OpenAI | Codex | 0.167.1 | Responses | builtin `codex` |
| Anthropic | Claude Code | 2.1.326 | Messages | builtin `claude-code` |
| Moonshot | Kimi Code | 2.6.2 | OpenAI Chat Completions custom provider by default | builtin `kimi-code` |
| Google | Gemini CLI | 0.68.0 | Gemini GenerateContent | builtin `gemini-cli` |
| xAI | Grok Build | 1.0.88 | Responses by default | builtin `grok-build` |
| Alibaba | Qwen Code | 0.27.4 | OpenAI Chat Completions | builtin `qwen-coder` |
| DeepSeek | DeepSeek Harness, DSH | 0.2.2-rc.1 | Messages, official DeepSeek adapter | custom SeekGnomes native driver |
| Z.ai | ZCode | CLI 0.17.1 at source commit below | OpenAI Chat Completions by default | custom SeekGnomes native driver |
| Xiaomi | MiMo Code | 0.1.17 | OpenAI Chat Completions custom provider | builtin `mimo` |

The pinned Harbor wheel's agent enum and factory were inspected, including its
installed-agent adapters. `kimi-cli` also exists in Harbor, but the maintained
Moonshot product selected here is Kimi Code. DSH and ZCode have official vendor
harnesses even though Harbor 0.22.0 does not supply their adapters. Their absence
from Harbor therefore does not select Codex.

## Primary sources

- Codex: [official repository](https://github.com/openai/codex),
  [advanced configuration](https://developers.openai.com/codex/config-advanced/),
  [noninteractive runs](https://developers.openai.com/codex/noninteractive/).
  `CODEX_HOME` isolates configuration and sessions; custom providers use
  `wire_api = "responses"` and `env_key`. `codex exec` and `exec resume` accept
  `-` for stdin. The existing locally inspected binary is `codex-cli 0.167.1`.
  The [fixed 0.167.1 configuration schema](https://github.com/openai/codex/blob/rust-v0.167.1/codex-rs/core/config.schema.json)
  defines `agents.enabled`, with enabled `features.multi_agent_v2` taking
  precedence. Consequently the driver sets `agents.enabled = false` and
  explicitly disables both multi-agent feature generations and the proactive
  mode. The legacy `features.multi_agent = false` alone did not remove the
  actual V2 `collaboration` namespace in the first live fixture. The current
  concurrent-child key is `max_concurrent_threads_per_session`, whose minimum
  is one; a child-count limit is not used as a substitute for disabling agents.
- Claude Code: [headless usage](https://code.claude.com/docs/en/headless),
  [settings and environment](https://code.claude.com/docs/en/settings),
  [MCP configuration](https://code.claude.com/docs/en/mcp).
  The launch uses print mode, stream-json, strict MCP config and private
  `CLAUDE_CONFIG_DIR`. All default model aliases, including the small model,
  are pinned to the selected model. Native delegation and native web tools are
  disabled; controlled web tools, if allowed, come from the role bridge.
  The actual 2.1.326 payload exposed independent `Workflow` and `Skill` tools:
  Workflow's description says it orchestrates many subagents, and Skill's
  description says some skills run in a subagent. Both are explicitly listed
  in `--disallowedTools`, alongside Agent and Task; blocking Agent alone was
  insufficient. Listing an existing agent is not treated as creating one.
- Kimi Code: [official repository](https://github.com/MoonshotAI/kimi-code),
  [CLI reference](https://moonshotai.github.io/kimi-code/en/reference/kimi-command),
  [configuration](https://moonshotai.github.io/kimi-code/en/configuration/config-files).
  The exact npm 2.6.2 bundle was inspected as well. `KIMI_CODE_HOME` isolates
  its `config.toml`, `mcp.json` and sessions. The custom provider explicitly
  names `api_key_env`. Custom model definitions require `max_context_size`;
  the package therefore requires the operator's model context parameter and
  does not guess a router model's context window. Per-server `toolTimeoutMs`
  is supported by this pinned bundle. Its `[tools].disabled` policy removes
  native `Agent`/`AgentSwarm` delegation, while keeping the supervised roles.
  `KIMI_CODE_WATCH=0` uses the native switch present in this pinned bundle's
  shared watcher service and configuration reload. Role configs are fixed
  during a run, so live filesystem watching is unnecessary; disabling it
  avoids an observed ENOSPC watcher loop on a crowded host.
- Gemini CLI: [official repository](https://github.com/google-gemini/gemini-cli),
  [headless mode](https://geminicli.com/docs/cli/headless/),
  [configuration](https://geminicli.com/docs/get-started/configuration/).
  Its native API is Gemini GenerateContent. `GEMINI_API_KEY` and
  `GOOGLE_GEMINI_BASE_URL` configure the endpoint. An OpenAI-compatible
  Chat Completions endpoint is not silently treated as a Gemini endpoint.
  This pinned CLI otherwise rewrites recognized model aliases to newer/custom
  tool variants. Its native dynamic resolver is enabled and an explicit
  `modelConfigs.modelIdResolutions[requested] = {default: requested, contexts: []}`
  overrides that mapping. The broker still rejects any changed model ID.
  The fixed 0.68 bundle still exposes `invoke_agent` when only
  `experimental.enableAgents=false` is set. `tools.exclude` therefore also
  names `invoke_agent`. The installed bundle's tool registry filters excluded
  tools and its policy compiler emits a DENY rule for each excluded name,
  including in YOLO mode.
- Grok Build: [official repository](https://github.com/xai-org/grok-build),
  [getting started](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/01-getting-started.md),
  [configuration](https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/05-configuration.md).
  The official npm package is `@xai-official/grok`. `--single`,
  `--output-format streaming-json`, `--resume`, per-model `api_backend`,
  and `[mcp_servers]` follow the vendor CLI and pinned Harbor adapter.
  Default, session-summary and auxiliary model choices use the selected route.
  `[subagents].enabled = false` and `GROK_SUBAGENTS=0` disable native delegation.
  Independent tool paths also need exclusion. In the actual 1.0.88 payload,
  `workflow` runs a Rhai script with many subagents, and `scheduler_create`
  starts a background subagent for each scheduled run. The real CLI help
  supports `--no-subagents` and `--disallowed-tools` (remove built-in tools).
  The driver uses both and explicitly removes `workflow,scheduler_create`,
  rather than assuming the subagent setting guards every internal path.
- Qwen Code: [official repository](https://github.com/QwenLM/qwen-code),
  [headless documentation](https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/).
  The pinned Harbor adapter uses `--auth-type openai`, `OPENAI_API_KEY`,
  `OPENAI_BASE_URL` and `OPENAI_MODEL`. Private `~/.qwen/settings.json`
  registers the role's sole MCP server.
  `--chat-recording` preserves resumable state; native Advisor and Workflow
  guidance are explicitly off for the fixed two-role protocol.
  The fixed 0.27.4 bundle has one otherwise conflicting budget: unset
  `model.maxToolCallsPerTurn` defaults to an adaptive 100-call threshold and an
  unconditional 1000-call hard backstop, also in headless runs. The driver
  explicitly sets it to `0`. The installed configuration schema documents
  nonpositive values as disabling this cap, and the actual getter returns
  `Number.POSITIVE_INFINITY` for values `<= 0`. Native cumulative
  `maxSessionTurns` and `maxToolCalls` retain their unlimited `-1` defaults.
  The removed cap would stop with `loop_detected`/`turn_tool_call_cap`, with
  `loopReason: backstop`; its counters reset for a new turn or native resume,
  which is not a substitute for honouring the uncapped Formalist allocation.
- DSH: [official product page](https://www.deepseek.com/harness/en/),
  [official repository](https://github.com/deepseek-ai/deepseek-harness),
  [headless contract](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.md),
  [DeepSeek API adapter](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/llm/llm-deepseek/README.md),
  [default model configuration](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/agent-default-model/README.md),
  [MCP client](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md).
  The exact npm 0.2.2-rc.1 headless, DeepSeek transport, API-key and MCP
  packages were inspected. `dsh --profile headless --json -` reads the
  complete task from stdin. `--session-id` resumes an existing persisted
  session, refusing unknown IDs. The current `deepseek-official` adapter uses
  Messages, `x-api-key`, and `DEEPSEEK_BASE_URL`; it explicitly does not accept
  a configurable protocol field. Chat Completions would require DSH's own
  alternate pi-ai route, which this driver does not silently substitute.
  Profile patches replace existing rows by stable ID and insert one MCP client.
- ZCode: [official source at fixed commit](https://github.com/zai-org/ZCode/tree/fec975905a2abd4c07e35c40d51fa097a2258ec9/apps/zcode-cli),
  [CLI reference](https://github.com/zai-org/ZCode/blob/fec975905a2abd4c07e35c40d51fa097a2258ec9/apps/zcode-cli/docs/usage/02-cli-reference.md),
  [provider and runtime configuration](https://github.com/zai-org/ZCode/blob/fec975905a2abd4c07e35c40d51fa097a2258ec9/apps/zcode-cli/docs/usage/03-config.md),
  [MCP configuration](https://github.com/zai-org/ZCode/blob/fec975905a2abd4c07e35c40d51fa097a2258ec9/apps/zcode-cli/docs/usage/07-mcp.md),
  [desktop release v3.18.2](https://github.com/zai-org/ZCode/releases/tag/v3.18.2).
  This source commit declares CLI 0.17.1. The desktop release's tag is not
  the CLI's version. No Linux standalone CLI asset is listed in that release;
  SeekGnomes therefore builds the official source at this exact commit. The
  packaged standalone Linux runtime was built with pinned pnpm 10.33.2 and
  Node 24.14.0, with the upstream dependency lock. A production deployment
  passed actual `--version` (0.17.1) and `--help` probes. The runtime archive,
  Node archive, locks, build logs, source provenance and executable SHA256 are
  bundled under `harness-assets/zcode-source-build/`. No GUI is required for
  those probes. This is not a successful paid GLM model run.
  The personal provider file's `apiKey` is a literal, not a promised `${ENV}`
  interpolation. The runtime renders only the role-scoped proxy capability
  into the private file. `mcp.servers` requires `features.mcp = true`.
  CLI session-control tools are separate from `features.subagent`. The driver
  passes the supported `--disallowed-tools` option for `CreateSession`,
  `ForkSession`, `SendToSession` and `WaitSessions`. In the fixed source,
  [built-in registration](https://github.com/zai-org/ZCode/blob/fec975905a2abd4c07e35c40d51fa097a2258ec9/apps/zcode-cli/packages/core/src/tool/handlers/index.ts)
  filters this disallowlist before registering handlers. This is a registry
  exclusion, rather than relying on YOLO permission checks to reject a call.
- MiMo Code: [official announcement](https://mimo.mi.com/docs/en-US/news/latest/mimocode),
  [integration guide](https://mimo.mi.com/docs/en-US/tokenplan/integration/mimo-code),
  [official repository](https://github.com/XiaomiMiMo/MiMo-Code).
  The official npm package is `@mimo-ai/cli`. `mimo run --format json`,
  `--session`, and a custom `@ai-sdk/openai-compatible` provider retain the
  exact model route under a private provider ID. `MIMOCODE_CONFIG`, isolated
  XDG directories and disabled project/Claude MCP imports prevent inheritance
  of a user's global configuration.
  In the [fixed v0.1.17 ActorTool source](https://github.com/XiaomiMiMo/MiMo-Code/blob/3704f8d8854cb5c4d4a73ff82c0a1d1008b8a564/packages/cli/src/tool/actor.ts),
  model-initiated run/spawn checks permission `actor`, not `task`, before
  looking up and starting a child. The driver explicitly denies `actor`.
  The [same release's permission service](https://github.com/XiaomiMiMo/MiMo-Code/blob/3704f8d8854cb5c4d4a73ff82c0a1d1008b8a564/packages/cli/src/permission/index.ts)
  rejects explicit deny before its runtime skip-all/approval shortcuts.
  Real v0.1.17 fixture regression subsequently showed the configured actor
  denial removes the actor from the actual available tool set. Replaying
  schema-valid run and spawn operations returns `unavailable tool 'actor'`
  before any child model request. This proves the earlier unavailable-tool
  guard, not execution of the deeper `ctx.ask` permission branch.

## Assets, configuration and validation limits

`harness_registry.json` records fixed top-level npm versions, tarball URLs and
registry SHA512 integrity, plus the ZCode source commit and built runtime SHA256. The registry was fetched
directly from the official npm registry, not copied from search snippets.
`installation_manifest()` returns these records without running installers.
Install into package-owned prefixes, generate and retain dependency locks, and
record the final executable hashes. A top-level version alone does not freeze
all transitive npm dependencies or establish a successful model run.

`native.build_invocation()` returns argv, a cleared/allowlisted environment,
private configuration templates, exact-session resume flags and session paths.
It performs no installation or paid model call. Root runtime must materialize
the templates inside the role's private sandbox, inject a role capability, and
keep upstream API credentials in the broker. The native CLI can use its own
file and shell tools only inside that sandbox. The broker separately enforces
the selected model and all reported-token budgets, including native compaction
and auxiliary requests. Off-model requests must be rejected, not hidden as
unmetered helper work.

API roots follow each native SDK's URL rules. Claude Code's Anthropic SDK
appends `/v1/messages`, and the installed Gemini GenAI SDK appends its API
version to the base URL, so these drivers remove an exact final `/v1` from the
private endpoint. OpenAI-compatible SDKs retain it. DSH explicitly reuses an
existing final `/v1` before appending `/messages`, so its driver retains it too.
The gateway does not accept duplicate-version paths to conceal a driver error.

Native MCP calls have a 2,147,483-second protection timeout, approximately
24.9 days. This is separate from the Formalist's uncapped cumulative budget.
The timeout avoids a one-hour limit interrupting a Constructor handoff while
the Formalist is still running; timeout or cancellation must remain a recorded
technical event, not an unproved scientific failure.

Native API header/stream guards are set to 900 seconds where the CLI exposes
the setting. The broker retains the protocol's 600-second upstream request
deadline and buffers a response for model/usage validation before replay.
The additional native headroom prevents a shorter default client timeout from
interrupting that validation. Source-backed knobs include Codex's provider
`stream_idle_timeout_ms`, DSH's `streamIdleTimeoutMs`, Kimi's
`KIMI_CODE_LLM_HEADERS_TIMEOUT_MS`, Gemini's `GEMINI_API_HEADERS_TIMEOUT_MS`,
Grok's `inference_idle_timeout_secs`, Qwen's request/stream timeout variables,
Claude's API/first-byte/stream-idle variables, ZCode's `modelStream`, and MiMo's
provider `timeout`, `headerTimeout` and `chunkTimeout`. Claude's separate MCP
idle watchdog is disabled so a legitimate long Formalist handoff is governed
by the explicitly recorded MCP call protection timeout.

The opt-in true Codex fixture runs the actual pinned native executable and its
Code Mode host inside the role's bubblewrap namespace. A localhost synthetic
Responses provider drives `functions.exec`, which invokes the actual MCP
`benchmark_action` tool through the role bridge and host gateway. Subsequent
native response generation completes under the same ledger. The test asserts
that the actual model-request tool declarations contain no `collaboration`
namespace. This verifies CLI/MCP/broker plumbing without an external model call.
The self-contained ZCode production runtime also passed real version/help
probes. These checks do not establish successful scientific inference across
all nine model families. Provider API compatibility, native handoffs and
remaining vendor CLI configurations require their separate runtime evidence.

## Native continuation for the closing phase

The pinned real CLI help outputs were read with empty private homes. Native
session continuation uses these options, always preserving the role's home,
workspace, ledger and append-only logs:

| Harness | Most recent session | Explicit existing session |
| --- | --- | --- |
| Codex | `exec resume --last` | `exec resume ID` |
| Claude Code | `--continue` | `--resume ID` |
| Kimi Code | `--continue` | `--session ID` |
| Gemini CLI | `--resume latest` | `--resume ID` |
| Grok Build | `--continue` | `--resume ID` |
| Qwen Code | `--continue` | `--resume ID` |
| DSH headless | No native last-session option | `--session-id ID` |
| ZCode | `--continue` | `--resume ID` |
| MiMo Code | `--continue` | `--session ID` |

`resume_last=True` and `resume_id` are mutually exclusive. Grok's
`--session-id` creates a new conversation and must not be used for resuming.
For DSH, `dsh_session_id_from_events(stdout_path)` reads the actual top-level
opening `{"type":"session","sessionId":"..."}` native event from the private
stdout log. Missing IDs fail closed; the supervisor must not reset the
conversation to conceal a recovery failure.

## Final scope check of autonomous-session entry points

The final check inspected the actual registered tool names and descriptions
from all nine pinned native CLIs. Claude's independent Workflow/Skill paths
and Grok's workflow/scheduler paths required the additional exclusions above.
Fresh local provider fixtures confirmed that Claude's actual payload omits
Agent, Task, Workflow and Skill, and Grok's omits workflow and scheduler_create
while native deferred MCP discovery and the completion-tool call still work.
The previously recorded Gemini/ZCode schema exclusions and MiMo's explicit
unavailable-tool rejection remain applicable.

Tools that list existing sessions, read existing outputs, load inline skill
instructions or enqueue another turn into the current session are not counted
as autonomous new LLM sessions merely because their description mentions
agents. In particular the fixed Kimi Skill implementation rejects non-inline
model invocations and reminds the current loop, Qwen describes its skill tool
as operating in the main conversation, and Grok monitor executes a script and
notifies the current session. This is a check of the two-role convention's
direct registered entry points, not a general security audit. No paid model
call or new scientific score was produced by these fixtures.
