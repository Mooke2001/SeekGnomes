"""Loopback-only SeekGnomes Console and its persistent local run state."""
from __future__ import annotations

import argparse
from contextlib import suppress
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import threading
import time
from urllib.parse import unquote, urlsplit
import webbrowser

if __package__:
    from .runner import ContainerRunner, RecoveryRequired, RunnerError, RunStopped, command_env
else:
    from runner import ContainerRunner, RecoveryRequired, RunnerError, RunStopped, command_env

RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
RUN_ROUTE = re.compile(r"/api/runs/([0-9a-f]{32})/(results|stop|retry-share)\Z")
TASK_IDS = {f"{i:02d}" for i in range(1, 51)}
NORMAL = {"finished", "finished_with_technical_gaps"}
LIVE = {"preparing", "stopping"}
SCIENTIFIC = {"completed", "scientific_failure"}
MAX_INT = (1 << 53) - 1
MAX_BODY = 64 * 1024
MAX_SCORE_BYTES = 4 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def canonical_file(path):
    try:
        return path.resolve(strict=True) == path and path.is_file()
    except (OSError, RuntimeError):
        return False


def read_json(path, limit=MAX_SCORE_BYTES):
    if canonical_file(path):
        try:
            with path.open("rb") as stream:
                data = stream.read(limit + 1)
            if len(data) <= limit:
                return json.loads(data)
        except (OSError, UnicodeError, ValueError, RecursionError):
            pass
    return None


def write_json(path, value):
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def load_table(path, field):
    raw = read_json(path)
    if isinstance(raw, dict):
        raw = raw.get(field)
    if not isinstance(raw, list) or not raw or not all(isinstance(row, dict) for row in raw):
        raise ValueError(f"Invalid bundled {field} file.")
    return raw


def task_table(rows):
    keys = ("id", "name", "dimension", "group")
    tasks = [{key: row[key] for key in keys} for row in rows]
    if len(tasks) != 50 or {task["id"] for task in tasks} != TASK_IDS:
        raise ValueError("The bundled catalogue must contain the real 50 task IDs.")
    return tasks


def company_table(rows):
    keys = ("id", "label", "harness", "protocols")
    companies = [{key: row[key] for key in keys} for row in rows]
    for company, row in zip(companies, rows):
        if row.get("default_protocol") in company["protocols"]:
            company["default_protocol"] = row["default_protocol"]
    ids = [company["id"] for company in companies]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("Invalid bundled company registry.")
    return companies


def text_field(value, limit):
    return (isinstance(value, str) and bool(value) and len(value) <= limit
            and not any(ord(c) < 32 or 127 <= ord(c) < 160 or 0xd800 <= ord(c) <= 0xdfff for c in value))


def normalize_request(raw, companies):
    fields = {"company", "model", "api_base_url", "api_key", "api_protocol", "model_context_tokens",
              "task_ids", "budget_mode", "budget_limit", "cpus", "web", "share"}
    if not isinstance(raw, dict) or set(raw) - {"parallel_tasks"} != fields:
        raise ApiError("Complete the model, task, and run settings.")
    request = dict(raw)
    key = request.pop("api_key")
    if not text_field(key, 4096) or any(not 33 <= ord(c) <= 126 for c in key):
        raise ApiError("Supply a nonempty API key with visible ASCII characters and no spaces.")
    company = companies.get(request["company"]) if isinstance(request["company"], str) else None
    if company is None or request["api_protocol"] not in company["protocols"]:
        raise ApiError("Select a supported company and API protocol.")
    model = request["model"]
    if not text_field(model, 256) or model != model.strip():
        raise ApiError("Supply the exact model ID as a single line without outer spaces.")
    endpoint = request["api_base_url"]
    if not text_field(endpoint, 4096) or any(c.isspace() for c in endpoint) or "\\" in endpoint:
        raise ApiError("Supply a valid API base URL.")
    try:
        parsed = urlsplit(endpoint)
        _ = parsed.port
    except ValueError:
        raise ApiError("Supply a valid API base URL.") from None
    is_local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (not parsed.hostname or parsed.username is not None or parsed.password is not None
            or "?" in endpoint or "#" in endpoint or not (parsed.scheme == "https" or is_local and parsed.scheme == "http")):
        raise ApiError("Use HTTPS or loopback HTTP, with no credentials, query, or fragment in the API base URL.")
    if key in unquote(endpoint) or key in endpoint or key in model:
        raise ApiError("Keep the API key out of the model ID and API base URL.")
    request["api_base_url"] = endpoint.rstrip("/")
    tasks = request["task_ids"]
    if (not isinstance(tasks, list) or not 5 <= len(tasks) <= 50
            or any(not isinstance(task, str) or task not in TASK_IDS for task in tasks) or len(set(tasks)) != len(tasks)):
        raise ApiError("Select at least five distinct tasks from the catalogue.")
    request["task_ids"] = sorted(tasks)
    if request["budget_mode"] not in ("tokens", "time"):
        raise ApiError("Select a token or time budget.")
    labels = {"budget_limit": "Budget limit", "cpus": "CPU count", "model_context_tokens": "Model context window"}
    for field, label in labels.items():
        value = request[field]
        if field == "model_context_tokens" and value is None:
            if company["harness"] == "kimi-code":
                raise ApiError("Kimi Code requires a model context window in tokens.")
            if company["harness"] == "dsh" and request["api_protocol"] == "chat_completions":
                raise ApiError("DeepSeek DSH with Chat Completions requires a model context window in tokens.")
            continue
        if type(value) is not int or not 1 <= value <= MAX_INT:
            raise ApiError(label + " must be a positive integer within JavaScript's safe range.")
    parallel_tasks = request.setdefault("parallel_tasks", 1)
    max_parallel = min(request["cpus"], len(tasks))
    if type(parallel_tasks) is not int or not 1 <= parallel_tasks <= max_parallel:
        raise ApiError(f"Parallel tasks must be an integer from 1 to {max_parallel} within the CPU and selected task limits.")
    if type(request["web"]) is not bool or type(request["share"]) is not bool:
        raise ApiError("Web access and sharing settings must be true or false.")
    return request, key


class RunManager:
    def __init__(self, results_dir, catalogue, companies, backend, submission_endpoint=None):
        self.tasks = task_table(catalogue)
        self.companies = company_table(companies)
        self.root = Path(results_dir).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock_fd = os.open(self.root / ".console.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock_fd)
            raise RunnerError("A Console is already using this results directory.") from None
        self.company_by_id = {row["id"]: row for row in self.companies}
        self.backend = backend
        self.endpoint = submission_endpoint
        self.endpoint_sha = hashlib.sha256(self.endpoint.encode()).hexdigest() if self.endpoint else None
        self.lock = threading.RLock()
        self.states = {}
        self.active = {}
        self.recovering = set()
        self.workers = set()
        for folder in sorted(self.root.iterdir()):
            if not RUN_ID.fullmatch(folder.name):
                continue
            state = read_json(folder / "state.json")
            if not self._valid_state(state, folder.name):
                continue
            if state["status"] in LIVE:
                state.update(status="stopping", phase="recovery", message="Checking the interrupted run in the container engine.")
            elif state.get("share_status") in ("pending", "sending"):
                self._recover_share(state)
            self.states[state["id"]] = state
        for run_id, state in self.states.items():
            if state["status"] == "stopping":
                self._start_recovery(run_id)

    @staticmethod
    def _valid_state(state, run_id):
        return (isinstance(state, dict) and state.get("id") == run_id and isinstance(state.get("model"), str)
                and isinstance(state.get("task_ids"), list) and 5 <= len(state["task_ids"]) <= 50
                and all(isinstance(t, str) and t in TASK_IDS for t in state["task_ids"])
                and len(set(state["task_ids"])) == len(state["task_ids"])
                and type(state.get("parallel_tasks", 1)) is int
                and 1 <= state.get("parallel_tasks", 1) <= len(state["task_ids"])
                and type(state.get("created_ns")) is int
                and state.get("status") in (*LIVE, *NORMAL, "failed", "interrupted", "stopped"))

    def config(self):
        return {"tasks": self.tasks, "companies": self.companies, "sharing_available": bool(self.endpoint)}

    def campaign_dir(self, run_id):
        return self.root / run_id / "results" / "one-task"

    def _queue(self, state):
        queue = read_json(self.campaign_dir(state["id"]) / "queue.json")
        if (isinstance(queue, dict) and queue.get("schema") == "seekgnomes-queue/v1.2"
                and queue.get("model") == state["model"] and queue.get("task_ids") == state["task_ids"]
                and isinstance(queue.get("status"), str)
                and isinstance(queue.get("tasks"), dict) and set(queue["tasks"]) == set(state["task_ids"])
                and all(isinstance(entry, dict) and isinstance(entry.get("status"), str) for entry in queue["tasks"].values())):
            return queue
        return {}

    def _scores(self, state):
        path = self.campaign_dir(state["id"]) / "results" / "scores.json"
        if not canonical_file(path):
            return None
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_SCORE_BYTES + 1)
            if len(data) > MAX_SCORE_BYTES:
                return None
            scores = json.loads(data)
            if (isinstance(scores, dict) and scores.get("schema") == "seekgnomes-scores/v1.1"
                    and isinstance(scores.get("rows"), list)
                    and all(isinstance(row, dict) for row in scores["rows"])
                    and [row.get("task_id") for row in scores["rows"]] == state["task_ids"]):
                return data
        except (OSError, ValueError, UnicodeError, RecursionError):
            pass
        return None

    def _save(self, state):
        write_json(self.root / state["id"] / "state.json", state)

    def _update(self, run_id, **fields):
        with self.lock:
            state = self.states[run_id]
            state.update(fields)
            self._save(state)

    def _background(self, function, *args):
        def work():
            try:
                function(*args)
            finally:
                with self.lock:
                    self.workers.discard(threading.current_thread())

        worker = threading.Thread(target=work, daemon=True)
        self.workers.add(worker)
        worker.start()

    def _state(self, run_id):
        try:
            return self.states[run_id]
        except KeyError:
            raise ApiError("Run not found.", 404) from None

    def list(self):
        public = []
        with self.lock:
            for state in sorted(self.states.values(), key=lambda value: value["created_ns"], reverse=True):
                queue = self._queue(state)
                entries = queue.get("tasks", {})
                results_available = state["status"] not in LIVE and self._scores(state) is not None
                item = {key: state[key] for key in ("id", "status", "model", "phase", "message", "share_status", "receipt") if key in state}
                item.update(selected=len(state["task_ids"]),
                            completed=sum(entry["status"] in SCIENTIFIC for entry in entries.values()),
                            parallel_tasks=state.get("parallel_tasks", 1), running_task_ids=[],
                            results_available=results_available)
                if state["status"] == "preparing" and entries:
                    item.update(status="running", phase="benchmark", message="Running the selected benchmark tasks.")
                    running = [task for task in state["task_ids"] if entries[task]["status"] == "running"]
                    item["running_task_ids"] = running
                    if running:
                        label = "task" if len(running) == 1 else "tasks"
                        item["message"] = f"Running {label} {', '.join(running)}."
                public.append(item)
        return {"runs": public}

    def results(self, run_id):
        with self.lock:
            state = self._state(run_id)
            data = self._scores(state) if state["status"] not in LIVE else None
        if data is None:
            raise ApiError("No scored result file is available for this run.", 404)
        return data

    def start(self, raw):
        config, api_key = normalize_request(raw, self.company_by_id)
        with self.lock:
            if any(state["status"] in LIVE for state in self.states.values()):
                raise ApiError("One benchmark run is already active.", 409)
            run_id = secrets.token_hex(16)
            run_dir = self.root / run_id
            run_dir.mkdir(mode=0o700)
            output_dir = run_dir / "results"
            output_dir.mkdir(mode=0o700)
            share_status = "not_requested"
            if config["share"]:
                share_status = "pending" if self.endpoint else "unavailable"
            state = {"id": run_id, "status": "preparing", "model": config["model"],
                     "task_ids": config["task_ids"], "parallel_tasks": config["parallel_tasks"],
                     "created_ns": time.time_ns(), "phase": "engine",
                     "message": "Checking the local container engine.", "share_status": share_status}
            if share_status == "pending":
                state["endpoint_sha256"] = self.endpoint_sha
            self._save(state)
            self.states[run_id] = state
            stop = threading.Event()
            self.active[run_id] = stop
            self._background(self._execute, run_id, output_dir, config, api_key, stop)
        return {"id": run_id, "status": "preparing", "parallel_tasks": config["parallel_tasks"]}

    def _execute(self, run_id, output_dir, config, secret, stop):
        def update(phase, message, **extra):
            with self.lock:
                if not stop.is_set():
                    extra.update(phase=phase, message=message)
                if extra:
                    self._update(run_id, **extra)

        try:
            code = self.backend.execute(run_id, output_dir, config, secret, stop, update)
            problem = "The benchmark container ended before producing a completed run. Check the engine, resources, and model configuration."
        except RecoveryRequired as error:
            with self.lock:
                self.active.pop(run_id, None)
                self._update(run_id, status="stopping", phase="recovery", message=str(error))
            return
        except RunStopped:
            code = None
            problem = "The run was stopped."
        except RunnerError as error:
            code = None
            problem = str(error).replace(secret, "[REDACTED]")
        except Exception:
            code = None
            problem = "The local benchmark runner failed."
        with self.lock:
            self.active.pop(run_id, None)
            state = self.states[run_id]
            queue_status = self._queue(state).get("status")
            has_scores = self._scores(state) is not None
            if queue_status in NORMAL and has_scores:
                status = queue_status
                message = "Results are ready." if status == "finished" else "Results are ready, with technical gaps."
            elif stop.is_set() or queue_status == "cancelled":
                status, message = "stopped", "The run was stopped."
            else:
                status = "failed"
                if queue_status == "account_budget_exceeded":
                    problem = "The provider stopped the run because the API account budget was exceeded."
                elif queue_status in NORMAL or code == 0:
                    problem = "The benchmark did not produce a valid scored result file."
                message = problem
            share_ready = status in NORMAL and not stop.is_set() and state["share_status"] == "pending"
            fields = {"status": status, "phase": "complete", "message": message}
            if state["share_status"] == "pending" and not share_ready:
                fields["share_status"] = "unavailable"
            self._update(run_id, **fields)
        if share_ready:
            self._prepare_share(run_id, config, secret)

    def _recover_share(self, state):
        path = self.root / state["id"] / "submission.json"
        state["share_status"] = "failed" if canonical_file(path) else "unavailable"
        self._save(state)

    def _start_recovery(self, run_id):
        with self.lock:
            if run_id not in self.recovering:
                self.recovering.add(run_id)
                self._background(self._recover, run_id)

    def _recover(self, run_id):
        with self.lock:
            state = self.states[run_id]
            engine = state.get("engine")
        try:
            self.backend.recover(run_id, engine)
        except RunnerError as error:
            problem = str(error)
        except Exception:
            problem = "The Console could not confirm the interrupted run ended. Restore the engine and retry Stop."
        else:
            problem = None
        with self.lock:
            self.recovering.discard(run_id)
            if problem:
                self._update(run_id, phase="recovery", message=problem)
                return
            queue_status = self._queue(state).get("status")
            if queue_status in NORMAL and self._scores(state) is not None:
                message = "Results are ready." if queue_status == "finished" else "Results are ready, with technical gaps."
                self._update(run_id, status=queue_status, phase="complete", message=message)
            else:
                self._update(run_id, status="interrupted", phase="complete", message="The previous Console session ended. No work was restarted.")
            if state.get("share_status") in ("pending", "sending"):
                self._recover_share(state)

    def stop(self, run_id):
        with self.lock:
            state = self._state(run_id)
            if state["status"] not in LIVE:
                raise ApiError("This run has already ended.", 409)
            stop = self.active.get(run_id)
            if stop is None:
                self._update(run_id, status="stopping", phase="recovery", message="Checking the interrupted run in the container engine.")
                self._start_recovery(run_id)
            else:
                self._update(run_id, status="stopping", phase="stopping", message="Stopping this benchmark run.")
                stop.set()
        return {"ok": True}

    def _prepare_share(self, run_id, config, secret):
        try:
            build_submission, _ = sharing_functions()
            envelope = build_submission(self.campaign_dir(run_id), config, secret=secret)
            write_json(self.root / run_id / "submission.json", envelope)
        except Exception:
            self._update(run_id, share_status="unavailable")
            return
        self._update(run_id, share_status="sending")
        self._send_share(run_id, envelope)

    def _send_share(self, run_id, envelope):
        try:
            _, send_submission = sharing_functions()
            received = send_submission(self.endpoint, envelope)
            if (received.get("status") != "pending" or received.get("submission_id") != envelope.get("sha256")
                    or type(received.get("duplicate")) is not bool):
                raise ValueError("Unexpected receipt.")
            receipt = {key: received[key] for key in ("status", "submission_id", "duplicate")}
        except Exception:
            self._update(run_id, share_status="failed")
            return
        self._update(run_id, share_status="sent", receipt=receipt)

    def retry_share(self, run_id):
        with self.lock:
            state = self._state(run_id)
            if (state["status"] not in NORMAL or state.get("share_status") != "failed" or not self.endpoint
                    or state.get("endpoint_sha256") != self.endpoint_sha):
                raise ApiError("Retry is available for an opted-in submission with a send failure and the same intake endpoint.", 409)
            if __package__:
                from .sharing import MAX_SUBMISSION_BYTES
            else:
                from sharing import MAX_SUBMISSION_BYTES
            envelope = read_json(self.root / run_id / "submission.json", 2 * MAX_SUBMISSION_BYTES)
            if not isinstance(envelope, dict) or self._scores(state) is None:
                raise ApiError("The local submission or scored results are unavailable.", 409)
            self._update(run_id, share_status="sending")
            self._background(self._send_share, run_id, envelope)
        return {"ok": True}

    def close(self):
        with self.lock:
            for stop in self.active.values():
                stop.set()
            workers = list(self.workers)
        deadline = time.monotonic() + 38
        for worker in workers:
            worker.join(max(0, deadline - time.monotonic()))
        os.close(self.lock_fd)


def sharing_functions():
    if __package__:
        from .sharing import build_submission, send_submission
    else:
        from sharing import build_submission, send_submission
    return build_submission, send_submission


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "SeekGnomesConsole"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = 10

    def log_message(self, *_args):
        pass

    def _response(self, data, content_type, status=200, *, download=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, value, status=200):
        self._response(json.dumps(value, ensure_ascii=True, allow_nan=False).encode(), "application/json; charset=utf-8", status)

    def _verify(self, *, mutate=False):
        if self.headers.get_all("Host") != [self.server.host]:
            raise ApiError("Use the Console's local loopback address.", 403)
        if mutate:
            token = self.headers.get_all("X-SeekGnomes-Token", [])
            if (self.headers.get_all("Origin") != [self.server.origin] or len(token) != 1 or not token[0].isascii()
                    or not secrets.compare_digest(token[0], self.server.csrf)):
                raise ApiError("This request must come from the local Console page. Reload and try again.", 403)

    def _body(self):
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1
                or not re.fullmatch(r"[0-9]{1,6}", lengths[0])):
            raise ApiError("Expected a bounded JSON request body.")
        if self.headers.get_content_type() != "application/json":
            raise ApiError("Expected JSON request content.", 415)
        length = int(lengths[0])
        if length > MAX_BODY:
            raise ApiError("Request body is too large.", 413)
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            raise ApiError("Invalid JSON request body.") from None

    def _asset(self, path):
        assets = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
        filename = assets.get(path)
        asset = self.server.ui_dir / filename if filename else None
        if asset is None or not canonical_file(asset):
            raise ApiError("Page or asset not found.", 404)
        data = asset.read_bytes()
        if filename == "index.html":
            data = data.replace(b"__SG_CSRF__", self.server.csrf.encode())
        self._response(data, (mimetypes.guess_type(filename)[0] or "application/octet-stream") + "; charset=utf-8")

    def _get(self):
        if self.path == "/api/config":
            self._json(self.server.manager.config())
        elif self.path == "/api/runs":
            self._json(self.server.manager.list())
        elif (match := RUN_ROUTE.fullmatch(self.path)) and match[2] == "results":
            self._response(self.server.manager.results(match[1]), "application/json; charset=utf-8", download="scores.json")
        else:
            self._asset(self.path)

    def _post(self):
        if self.path == "/api/runs":
            self._json(self.server.manager.start(self._body()), 201)
        elif (match := RUN_ROUTE.fullmatch(self.path)) and match[2] in ("stop", "retry-share"):
            action = self.server.manager.stop if match[2] == "stop" else self.server.manager.retry_share
            self._json(action(match[1]), 202)
        else:
            raise ApiError("Route not found.", 404)

    def _handle(self, *, mutate=False):
        try:
            self._verify(mutate=mutate)
            if mutate:
                self._post()
            else:
                self._get()
        except ApiError as error:
            with suppress(OSError):
                self._json({"error": str(error)}, error.status)
        except (OSError, ValueError, TypeError, RuntimeError):
            with suppress(OSError):
                self._json({"error": "The local Console could not complete this request."}, 500)

    def do_GET(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    def do_POST(self):
        self._handle(mutate=True)


class ConsoleServer(ThreadingHTTPServer):
    def __init__(self, manager, ui_dir, port=0):
        super().__init__(("127.0.0.1", port), ConsoleHandler)
        self.manager = manager
        self.ui_dir = Path(ui_dir).resolve()
        self.host = f"127.0.0.1:{self.server_port}"
        self.origin = f"http://{self.host}"
        self.csrf = secrets.token_urlsafe(32)

    def handle_error(self, request, client_address):
        # Structured request errors are reported by ConsoleHandler; keep raw request tracebacks private.
        pass


def open_browser(url):
    if os.environ.get("WSL_DISTRO_NAME"):
        try:
            subprocess.Popen(["explorer.exe", url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, env=command_env())
            return
        except OSError:
            pass
    webbrowser.open(url)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Start the local SeekGnomes Console")
    parser.add_argument("--package-dir", type=Path, required=True)
    parser.add_argument("--console-dir", type=Path)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    package = args.package_dir.resolve()
    console = args.console_dir.resolve() if args.console_dir is not None else package / "console"
    manager = None
    try:
        if console == package / "console-src":
            # A checked-out release can launch its already-built local image.
            from build_console import package_data
            files = package_data(package, None)
            config = json.loads(files["console/console-config.json"])
            catalogue = json.loads(files["console/catalogue.json"])
            companies = json.loads(files["console/companies.json"])
        else:
            config = read_json(console / "console-config.json")
            catalogue = load_table(console / "catalogue.json", "tasks")
            companies = load_table(console / "companies.json", "companies")
        if not isinstance(config, dict) or (config.get("submission_endpoint") is not None
                                             and not isinstance(config["submission_endpoint"], str)):
            raise ValueError("Invalid bundled Console configuration.")
        manager = RunManager(args.results_dir, catalogue, companies,
                             ContainerRunner(package, console / "runtime"), config.get("submission_endpoint"))
        with ConsoleServer(manager, console / "ui", args.port) as server:
            if threading.current_thread() is threading.main_thread():
                def interrupt(_signum, _frame):
                    raise KeyboardInterrupt

                signal.signal(signal.SIGTERM, interrupt)
            print(f"SeekGnomes Console {server.origin}/", flush=True)
            if not args.no_browser:
                threading.Thread(target=open_browser, args=(server.origin + "/",), daemon=True).start()
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (OSError, OverflowError, ValueError, RunnerError, KeyError) as error:
        parser.exit(2, f"SeekGnomes Console could not start. {error}\n")
    finally:
        if manager is not None:
            manager.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
