"use strict";

const byId = (id) => document.getElementById(id);
const controls = {
  form: byId("setup"),
  company: byId("company"),
  model: byId("model"),
  endpoint: byId("api-base-url"),
  key: byId("api-key"),
  protocol: byId("api-protocol"),
  modelSettings: byId("model-settings"),
  context: byId("model-context-tokens"),
  contextHelp: byId("context-help"),
  harness: byId("harness"),
  tokenField: byId("token-field"),
  timeField: byId("time-field"),
  tokenLimit: byId("token-limit"),
  timeLimit: byId("time-limit"),
  cpus: byId("cpus"),
  parallelTasks: byId("parallel-tasks"),
  parallelHelp: byId("parallel-help"),
  web: byId("web"),
  share: byId("share"),
  shareNote: byId("share-note"),
  search: byId("task-search"),
  dimension: byId("task-dimension"),
  taskList: byId("task-list"),
  select: byId("select-tasks"),
  clear: byId("clear-tasks"),
  selectedCount: byId("selected-count"),
  visibleCount: byId("visible-count"),
  start: byId("start"),
  startMessage: byId("start-message"),
  runList: byId("run-list"),
  runMessage: byId("run-message"),
  refresh: byId("refresh-runs"),
};
const token = document.querySelector('meta[name="seekgnomes-token"]').content;
const selection = new Set();
const minimumTasks = 5;
const activeStates = new Set(["preparing", "running", "stopping"]);
const statusLabels = {
  preparing: "Preparing",
  running: "Running",
  stopping: "Stopping",
  finished: "Finished",
  finished_with_technical_gaps: "Finished with technical gaps",
  stopped: "Stopped",
  failed: "Failed",
  interrupted: "Interrupted",
};
const sharingLabels = {
  pending: "Submission pending",
  sending: "Sending submission",
  sent: "Submission received for review",
  failed: "Submission failed",
  unavailable: "Submission unavailable",
};
const protocolLabels = {
  chat_completions: "OpenAI Chat Completions",
  responses: "OpenAI Responses",
  messages: "Anthropic Messages",
  gemini: "Gemini generateContent",
};
const harnessLabels = {
  codex: "Codex",
  "claude-code": "Claude Code",
  "kimi-code": "Kimi Code",
  "gemini-cli": "Gemini CLI",
  "grok-build": "Grok Build",
  "qwen-code": "Qwen Code",
  dsh: "DeepSeek Harness",
  "mimo-code": "MiMo Code",
  zcode: "ZCode",
};

let tasks = [];
let companies = [];
let shownTasks = [];
let setupReady = false;
let isStarting = false;
let refreshingRuns = false;
let previousRuns = "";

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function request(path, payload) {
  const response = await fetch(path, {
    method: payload === undefined ? "GET" : "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: payload === undefined ? {} : {
      "Content-Type": "application/json",
      "X-SeekGnomes-Token": token,
    },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `Local console request failed (HTTP ${response.status}).`);
  }
  return data;
}

function writeMessage(node, text, isError = false) {
  if (node.textContent !== text) node.textContent = text;
  node.classList.toggle("is-error", isError);
}

function setCompany() {
  const company = companies.find(({ id }) => id === controls.company.value);
  const options = company?.protocols || [];
  controls.harness.textContent = harnessLabels[company?.harness] || company?.harness || "—";
  controls.protocol.replaceChildren(...options.map((id) => new Option(protocolLabels[id] || id, id)));
  if (options.includes(company?.default_protocol)) controls.protocol.value = company.default_protocol;
  controls.protocol.disabled = options.length === 0;
  updateContextRequirement();
  updateSelection();
}

function updateContextRequirement() {
  const harness = companies.find(({ id }) => id === controls.company.value)?.harness;
  const requiresContext = harness === "kimi-code" || (harness === "dsh" && controls.protocol.value === "chat_completions");
  controls.context.required = requiresContext;
  controls.context.placeholder = requiresContext ? "Required" : "Default";
  if (requiresContext) controls.modelSettings.open = true;
  controls.contextHelp.textContent = requiresContext
    ? "This harness and API protocol require the model’s context window. Enter the value specified by your provider."
    : "Leave blank to use the harness default.";
}

function budgetMode() {
  return controls.form.querySelector('input[name="budget-mode"]:checked').value;
}

function setBudgetMode() {
  const isTime = budgetMode() === "time";
  controls.tokenField.hidden = isTime;
  controls.tokenLimit.disabled = isTime;
  controls.timeField.hidden = !isTime;
  controls.timeLimit.disabled = !isTime;
}

function updateParallelTasks() {
  const cores = controls.cpus.valueAsNumber;
  const validCores = Number.isSafeInteger(cores) && controls.cpus.validity.valid;
  const max = validCores ? Math.max(1, Math.min(cores, selection.size)) : 1;
  controls.parallelTasks.max = String(max);
  let help = "Limited by the total CPU cores and the number of selected tasks.";
  if (validCores && selection.size >= minimumTasks) {
    help = `Maximum ${max} parallel tasks.`;
    if (controls.parallelTasks.validity.valid) {
      const coresPerTask = Math.floor(cores / controls.parallelTasks.valueAsNumber);
      const unit = coresPerTask === 1 ? "core" : "cores";
      help = `${coresPerTask} CPU ${unit} per active task. ${help}`;
    }
  }
  controls.parallelHelp.textContent = help;
}

function updateSelection() {
  controls.selectedCount.textContent = `${selection.size} selected`;
  updateParallelTasks();
  controls.select.disabled = !shownTasks.some(({ id }) => !selection.has(id));
  controls.clear.disabled = selection.size === 0;
  controls.start.disabled = !setupReady || isStarting || selection.size < minimumTasks
    || !controls.company.value || !controls.protocol.value;
}

function taskRow(task) {
  const row = element("li");
  const label = element("label", "task-row");
  const check = element("input");
  check.type = "checkbox";
  check.value = task.id;
  check.checked = selection.has(task.id);
  check.addEventListener("change", () => {
    if (check.checked) selection.add(task.id);
    else selection.delete(task.id);
    updateSelection();
  });
  label.append(
    check,
    element("span", "task-id", task.id),
    element("span", "task-name", task.name),
    element("span", "task-dimension", `${task.dimension}D`),
  );
  row.append(label);
  return row;
}

function filterTasks() {
  const words = controls.search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const dimension = controls.dimension.value;
  shownTasks = tasks.filter((task) => {
    const text = `${task.id} ${task.name} ${task.group || ""}`.toLowerCase();
    return (!dimension || String(task.dimension) === dimension) && words.every((word) => text.includes(word));
  });
  controls.taskList.replaceChildren(...shownTasks.map(taskRow));
  if (!shownTasks.length) controls.taskList.append(element("li", "task-empty", "No tasks match these filters."));
  controls.taskList.scrollTop = 0;
  controls.visibleCount.textContent = `${shownTasks.length} of ${tasks.length} tasks`;
  controls.select.textContent = shownTasks.length === tasks.length ? "Select all" : "Select visible";
  updateSelection();
}

function syncTaskChecks() {
  for (const check of controls.taskList.querySelectorAll('input[type="checkbox"]')) {
    check.checked = selection.has(check.value);
  }
  updateSelection();
}

async function configure() {
  try {
    const config = await request("/api/config");
    tasks = config.tasks;
    companies = config.companies;
    controls.company.replaceChildren(
      new Option("Choose a model company", ""),
      ...companies.map(({ id, label }) => new Option(label, id)),
    );
    controls.company.disabled = false;
    controls.search.disabled = false;
    controls.dimension.disabled = false;
    controls.share.disabled = !config.sharing_available;
    controls.share.checked = false;
    controls.shareNote.textContent = config.sharing_available
      ? "Sends the result summary, selected candidate and Lean files, and provenance to the configured review service."
      : "This installation has no results submission service configured.";
    setupReady = true;
    setCompany();
    filterTasks();
    writeMessage(controls.startMessage, "");
  } catch (error) {
    controls.taskList.replaceChildren(element("li", "task-empty", "The task library could not be loaded."));
    writeMessage(controls.startMessage, `${error.message} Reload the console to try again.`, true);
  }
}

function actionButton(label, path) {
  const button = element("button", "button", label);
  button.type = "button";
  button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      await request(path, {});
      await refreshRuns();
    } catch (error) {
      writeMessage(controls.runMessage, error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

function runRow(run) {
  const id = String(run.id);
  const path = `/api/runs/${encodeURIComponent(id)}`;
  const row = element("li", "run-card");
  row.dataset.runId = id;
  const body = element("div", "run-body");
  const heading = element("div", "run-heading");
  heading.append(
    element("h3", "run-model", run.model || "Benchmark run"),
    element("span", "run-status", statusLabels[run.status] || run.status),
  );
  const detail = element("p", "run-meta", `${run.completed} / ${run.selected} tasks completed · `);
  detail.append(element("span", "run-id", id));
  if (run.parallel_tasks > 1) detail.append(` · Up to ${run.parallel_tasks} tasks in parallel`);
  body.append(heading, detail);
  const stage = run.message || run.phase;
  if (stage) body.append(element("p", "run-meta", stage));
  if (activeStates.has(run.status) && run.selected > 0) {
    const progress = element("progress");
    progress.max = run.selected;
    progress.value = run.completed;
    progress.setAttribute("aria-label", `Tasks completed for ${run.model || id}`);
    body.append(progress);
  }
  if (sharingLabels[run.share_status]) {
    const receipt = run.receipt?.submission_id;
    const sharing = sharingLabels[run.share_status];
    body.append(element("p", "run-meta", receipt ? `${sharing} · Receipt ${receipt}` : sharing));
  }
  const actions = element("div", "run-actions");
  if (run.results_available) {
    const link = element("a", "button", "Download results");
    link.href = `${path}/results`;
    link.download = `${id}.json`;
    actions.append(link);
  }
  if (run.status === "preparing" || run.status === "running") {
    actions.append(actionButton("Stop run", `${path}/stop`));
  }
  if (run.status === "stopping" && run.phase === "recovery") {
    actions.append(actionButton("Retry stop", `${path}/stop`));
  }
  if (run.share_status === "failed") {
    actions.append(actionButton("Retry submission", `${path}/retry-share`));
  }
  row.append(body, actions);
  return row;
}

async function refreshRuns() {
  if (refreshingRuns) return;
  refreshingRuns = true;
  try {
    const { runs } = await request("/api/runs");
    const signature = JSON.stringify(runs);
    if (signature !== previousRuns) {
      // Keep a focused action usable while the background poll refreshes its row.
      const focus = document.activeElement;
      const currentRow = controls.runList.contains(focus) ? focus.closest("li") : null;
      controls.runList.replaceChildren(...runs.map(runRow));
      if (currentRow) {
        const row = [...controls.runList.children].find(({ dataset }) => dataset.runId === currentRow.dataset.runId);
        const nextAction = row && [...row.querySelectorAll("button, a")].find((child) => child.textContent === focus.textContent);
        (nextAction || controls.refresh).focus({ preventScroll: true });
      }
      previousRuns = signature;
    }
    writeMessage(controls.runMessage, runs.length ? "" : "No benchmark runs yet.");
  } catch (error) {
    writeMessage(controls.runMessage, `Could not refresh runs. ${error.message}`, true);
  } finally {
    refreshingRuns = false;
  }
}

async function startRun(event) {
  event.preventDefault();
  if (controls.start.disabled) return;
  const mode = budgetMode();
  const budget = mode === "time" ? controls.timeLimit.valueAsNumber * 60 : controls.tokenLimit.valueAsNumber;
  const apiKey = controls.key.value.trim();
  const payload = {
    company: controls.company.value,
    model: controls.model.value.trim(),
    api_base_url: controls.endpoint.value.trim(),
    api_key: apiKey,
    api_protocol: controls.protocol.value,
    model_context_tokens: controls.context.valueAsNumber || null,
    task_ids: tasks.filter(({ id }) => selection.has(id)).map(({ id }) => id),
    budget_mode: mode,
    budget_limit: budget,
    cpus: controls.cpus.valueAsNumber,
    parallel_tasks: controls.parallelTasks.valueAsNumber,
    web: controls.web.checked,
    share: !controls.share.disabled && controls.share.checked,
  };
  isStarting = true;
  updateSelection();
  writeMessage(controls.startMessage, "Starting the local benchmark…");
  try {
    const run = await request("/api/runs", payload);
    controls.key.value = "";
    controls.share.checked = false;
    writeMessage(controls.startMessage, `Run ${run.id} started. Follow its progress in Run history.`);
    await refreshRuns();
  } catch (error) {
    const message = apiKey ? error.message.split(apiKey).join("[redacted]") : error.message;
    writeMessage(controls.startMessage, message, true);
  } finally {
    isStarting = false;
    updateSelection();
  }
}

controls.company.addEventListener("change", setCompany);
controls.protocol.addEventListener("change", updateContextRequirement);
controls.cpus.addEventListener("input", updateParallelTasks);
controls.parallelTasks.addEventListener("input", updateParallelTasks);
controls.context.addEventListener("invalid", () => {
  controls.modelSettings.open = true;
});
for (const radio of controls.form.querySelectorAll('input[name="budget-mode"]')) {
  radio.addEventListener("change", setBudgetMode);
}
controls.search.addEventListener("input", filterTasks);
controls.dimension.addEventListener("change", filterTasks);
controls.select.addEventListener("click", () => {
  for (const { id } of shownTasks) selection.add(id);
  syncTaskChecks();
});
controls.clear.addEventListener("click", () => {
  selection.clear();
  syncTaskChecks();
});
controls.form.addEventListener("submit", startRun);
controls.refresh.addEventListener("click", refreshRuns);
setBudgetMode();
configure();
refreshRuns();
window.setInterval(() => {
  if (!document.hidden) refreshRuns();
}, 3000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshRuns();
});
