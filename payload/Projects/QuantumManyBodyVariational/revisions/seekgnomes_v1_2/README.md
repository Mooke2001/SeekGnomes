# SeekGnomes

**English** | [简体中文](README.zh-CN.md)

SeekGnomes asks LLMs to construct valid low-energy states on infinite quantum lattices. It checks each proof against a fixed Lean target and verifies it independently with the Lean kernel. It uses Harbor 0.22.0 to organize tasks and trials and defaults to the `seekgnomes-harbor-state/v1` protocol. The benchmark uses the model provider's official harness where available and Codex where the provider has no official harness. Version 1.3.0 includes the `.state` workflow, trial checkpoint recovery, and a local browser console.

## Quick start

Download the **public build context** from GitHub Releases. The initial build needs Linux x86-64 or WSL on x86-64, Python 3.11 or later, Docker Engine or rootless Podman on Linux, and network access to the pinned dependency sources. The container requires at least 12 GiB of available memory. We recommend 16 GiB. It runs on CPU. See [BUILD.md](BUILD.md) for multipart download recovery and image build instructions.

After building the image, run these commands from the same directory.

```bash
./seekgnomes doctor
./seekgnomes self-test
bash ./console-src/Start.command
```

The launcher opens a page on your machine. Select a model, configure its API, choose 5 to 50 tasks, and set CPU allocation, parallelism, token or time budget, web access, and optional result sharing. Run `./seekgnomes` for the terminal interface. On Windows, use WSL with Linux containers. The validation summary records the platforms covered by testing.

The interactive interface asks for the exact model ID, API URL, and an API key entered without echo. Use `--company` if the model provider cannot be inferred from the ID. You can inspect the configuration before making model requests.

```bash
./seekgnomes harnesses
./seekgnomes run --company openai --model YOUR_EXACT_MODEL_ID \
  --api-base-url https://YOUR_PROVIDER.example/v1 \
  --api-protocol responses --tasks 2 --print-config
```

Remove `--print-config` to run the benchmark. The default `--tasks all` selects all 50 tasks. Use an explicit selection such as `--tasks 1,7,13-15` for a subset. Controlled access to public information is enabled by default. Runs using `--no-web` are recorded separately. Supply the API key at the hidden prompt or through `SEEKGNOMES_API_KEY`.

`--constructor-output-tokens` sets the Constructor's cumulative output budget for the main phase. The default is 128,000 tokens, and `SEEKGNOMES_CONSTRUCTOR_OUTPUT_TOKENS` can set it through the environment. If the main phase ends before a complete `f.state` has been handed off with `create_handoff`, the same session gets one closing phase with a fixed 50,000-output-token budget to complete the handoff. The trial records a scientific failure if the closing phase ends without a complete handoff.

`--cpus` sets both the container CPU quota and the CPU allocation in the run plan. The default is 2. `--constructor-active-seconds` sets the Constructor's active time limit for the main phase, defaulting to 14,400 seconds. An eligible closing phase receives a separate allocation of the same duration. The Constructor's clock pauses while the Formalist is active. Cumulative input tokens are unlimited by default. If you set `--constructor-input-tokens`, the main phase and any eligible closing phase each receive that amount. The cumulative number of API calls is unlimited. The environment variables are `SEEKGNOMES_CPUS`, `SEEKGNOMES_CONSTRUCTOR_ACTIVE_SECONDS`, and `SEEKGNOMES_CONSTRUCTOR_INPUT_TOKENS`.

## The two roles

The Constructor explores the physics, writes a complete `f.state`, and hands it off with `create_handoff`. The host freezes the source and generates its Lean definition and fixed proof target. The Formalist retrieves the generated files with `prepare`, writes the proof, and submits it with `score`. After the proof matches the target and passes independent Lean kernel verification, the host evaluates the energy density per qubit from that same `.state` and returns it to the Constructor. The Formalist can revise a proof that fails verification and ask the Constructor to fix a specific defect in the frozen definition. Both roles receive the same [`.state` language guide](STATE_USAGE.md) at `reference/state_language.md`.

The native harness manages each role's context. Framework requests for summarization or context compaction count toward the corresponding role's usage. The Constructor's budget carries across handoffs. The Formalist has unlimited cumulative usage under the protocol. Per-request timeouts and resource limits still apply to individual API calls and Lean processes.

CPU allocation and active time control resource use. The numerical tools reserve CPU time conservatively as requested CPUs times requested wall time. Native CLI usage is measured on the process tree. These measurements are recorded separately and use a different accounting scheme from earlier releases.

## Results and recovery

The host writes results to `results/` under the extracted package directory by default. Set `SEEKGNOMES_RESULTS_DIR` to choose another directory. Inside the container it maps to

```text
/home/ubuntu/Mooke/Eureka/Projects/QuantumManyBodyVariational/runs
```

Use a new path under the container directory when passing `--out`. Each run preserves its plan, Harbor results, both role traces, source definitions, candidate code, proofs, and verification receipts. The `report` command reads the stored evidence without making API requests.

```bash
./seekgnomes report \
  /home/ubuntu/Mooke/Eureka/Projects/QuantumManyBodyVariational/runs/YOUR_RUN
```

A state counts toward the score once its proof matches the fixed target and passes independent Lean kernel verification. The report recomputes the energy from the original `.state` and verification records. The reported energy is a floating-point estimate. The Lean proof covers state validity, translation invariance, and the applicable domain and limit conditions. Technical failures are recorded as missing results. Exactly solvable models and general variational models are scored separately. See [SCORING.md](SCORING.md) and [PROTOCOL.md](PROTOCOL.md).

The run configuration records budget, CPU, and web settings so that rankings can compare runs under the same conditions. Harbor automatically resumes from the same trial's checkpoint after an unexpected trial controller exit. When a terminal technical failure requires a fresh attempt, a task can run up to three attempts with the same configuration. Scientific failures retain the results obtained under the original budget.

If the outer supervisor or host itself stopped, use a build with checkpoint support to resume the original run.

```bash
./seekgnomes resume \
  /home/ubuntu/Mooke/Eureka/Projects/QuantumManyBodyVariational/runs/YOUR_RUN
```

Mount the same results directory and use `SEEKGNOMES_CPUS` to provide the CPU count required by the original plan. The run resumes with the stored model and budget. Provide the API key again at the hidden prompt or through the environment. Completed tasks and explicitly cancelled runs keep their terminal status.

## Isolation and reproducibility

The outer package mounts the results directory. It does not mount the host home directory or Docker socket. Native agents run in isolated role environments with role-specific tool connections and without direct access to model API credentials. The Constructor's scientific tools and the Formalist's proof tools are isolated from each other. Candidates and persistent memory stay within their model, task, and technical attempt.

`doctor` and `self-test` make no paid model requests. The self-test covers positive and negative proof checks, local `.state` verification and energy evaluation, and an offline Harbor trial. See `validation-summary.json` for the scope of platform and native CLI testing.

## Release files

- `seekgnomes-v1.3.0-github-source.tar.gz` contains the task set, Lean specifications, runner, local console, and build recipes.
- `seekgnomes-v1.3.0-public-build-context.tar.gz` also includes pinned Lean, Mathlib, and Python wheels. Follow [BUILD.md](BUILD.md) to obtain the vendor CLIs and build the image locally.

Release assets include SHA-256 checksums and multipart recovery scripts. The author has yet to specify a project license. Third-party components retain their own licenses. The archives exclude model weights, API keys, and historical benchmark data. Users run the benchmark on their own machine or infrastructure. Result sharing is off by default and requires a configured submission endpoint and user opt-in.
