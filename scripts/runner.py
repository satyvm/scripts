"""Script Runner — Background execution + state tracking for the dashboard.

Each registered script gets a ScriptRunner that:
  - Runs the script's main() in a background thread
  - Captures stdout/stderr into a ring buffer
  - Writes structured status to state/dashboard_status.json
  - Writes append-only event logs to state/run_log.jsonl
  - Supports manual re-run triggers from the dashboard
"""

import importlib
import json
import os
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


# ==========================================
# STATE FILE PATHS
# ==========================================
STATE_DIR = Path(os.environ.get("STATE_DIR", str(Path(__file__).resolve().parent.parent / "state")))
STATUS_FILE = STATE_DIR / "dashboard_status.json"
LOG_FILE = STATE_DIR / "run_log.jsonl"

MAX_LOG_ENTRIES = int(os.environ.get("DASHBOARD_MAX_LOGS", "500"))
MAX_OUTPUT_LINES = 200  # Ring buffer size for captured stdout per script

# Module-level lock for status file writes (prevents race between runners)
_status_file_lock = threading.Lock()
_log_file_lock = threading.Lock()


class OutputCapture:
    """Thread-safe ring buffer that captures stdout/stderr while still printing."""

    def __init__(self, original_stream, max_lines: int = MAX_OUTPUT_LINES):
        self._original = original_stream
        self._buffer: deque[str] = deque(maxlen=max_lines)
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        self._original.write(text)
        if text.strip():
            with self._lock:
                for line in text.splitlines():
                    if line.strip():
                        self._buffer.append(line)
        return len(text)

    def flush(self):
        self._original.flush()

    def get_lines(self) -> list[str]:
        with self._lock:
            return list(self._buffer)

    def clear(self):
        with self._lock:
            self._buffer.clear()


class ScriptRunner:
    """Manages the lifecycle of a single registered script."""

    def __init__(self, name: str, module_path: str, description: str, poll_interval: int = 7200):
        self.name = name
        self.module_path = module_path
        self.description = description
        self.poll_interval = poll_interval

        # Runtime state
        self.status: str = "idle"  # "idle" | "running" | "error" | "stopped"
        self.last_run_start: str | None = None
        self.last_run_end: str | None = None
        self.last_error: str | None = None
        self.next_run: str | None = None
        self.total_runs: int = 0
        self.total_errors: int = 0

        # Output capture
        self.output = OutputCapture(sys.stdout)

        # Thread control
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._rerun_event = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start the script's background loop."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"runner-{self.name}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the script to stop after the current cycle."""
        self._stop_event.set()
        self._rerun_event.set()  # Wake up any sleep
        self.status = "stopped"
        self._save_status()

    def trigger_rerun(self) -> bool:
        """Trigger an immediate re-run. Returns False if already running."""
        with self._lock:
            if self.status == "running":
                return False
            self._rerun_event.set()
            return True

    def get_status(self) -> dict:
        """Return a snapshot of the current state."""
        return {
            "name": self.name,
            "description": self.description,
            "module": self.module_path,
            "status": self.status,
            "last_run_start": self.last_run_start,
            "last_run_end": self.last_run_end,
            "last_error": self.last_error,
            "next_run": self.next_run,
            "total_runs": self.total_runs,
            "total_errors": self.total_errors,
            "poll_interval": self.poll_interval,
        }

    def get_output(self) -> list[str]:
        """Return captured stdout lines."""
        return self.output.get_lines()

    # ── Private ──

    def _run_loop(self) -> None:
        """Main background loop: run → sleep → repeat."""
        while not self._stop_event.is_set():
            self._execute_once()

            if self._stop_event.is_set():
                break

            # Calculate and set next run time
            next_ts = datetime.now(timezone.utc).timestamp() + self.poll_interval
            self.next_run = datetime.fromtimestamp(next_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._save_status()

            # Sleep, but wake up for rerun or stop
            self._rerun_event.clear()
            self._rerun_event.wait(timeout=self.poll_interval)

            if self._stop_event.is_set():
                break

        self.status = "stopped"
        self._save_status()

    def _execute_once(self) -> None:
        """Execute a single run of the script."""
        self.status = "running"
        self.last_run_start = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.next_run = None
        self._save_status()
        self._append_log("run_start")

        # Redirect stdout/stderr for this script's thread
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout = self.output
        sys.stderr = self.output

        try:
            module = importlib.import_module(self.module_path)
            # Reload in case the module has top-level state
            importlib.reload(module)

            # Call the script's main function
            # We need to handle the fact that good_first_issue_tracker.main()
            # runs an infinite loop. Instead, we call check_for_issues directly.
            if hasattr(module, "check_for_issues") and hasattr(module, "build_all_repos"):
                all_repos, all_orgs = module.build_all_repos()
                module.check_for_issues(all_repos, all_orgs)
            elif hasattr(module, "main"):
                module.main()
            else:
                raise AttributeError(f"Module {self.module_path} has no main() or check_for_issues()")

            self.status = "idle"
            self.last_error = None
            self.total_runs += 1
            self.last_run_end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._append_log("run_end")

        except Exception as e:
            self.status = "error"
            self.last_error = f"{type(e).__name__}: {str(e)}"
            self.total_runs += 1
            self.total_errors += 1
            self.last_run_end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._append_log("run_error", error=self.last_error)

            # Print traceback to captured output
            traceback.print_exc(file=self.output)

        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            self._save_status()

    def _save_status(self) -> None:
        """Persist current status to dashboard_status.json (thread-safe, merges with other runners)."""
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)

            with _status_file_lock:
                # Read existing statuses from other runners
                existing: dict = {}
                if STATUS_FILE.exists():
                    try:
                        with open(STATUS_FILE, "r") as f:
                            existing = json.load(f)
                    except (json.JSONDecodeError, IOError):
                        existing = {}

                existing[self.name] = self.get_status()

                with open(STATUS_FILE, "w") as f:
                    json.dump(existing, f, indent=2)
        except Exception:
            pass  # Don't crash the runner for a status write failure

    def _append_log(self, event: str, **extra) -> None:
        """Append a structured log entry to run_log.jsonl."""
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)

            entry = {
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "script": self.name,
                "event": event,
                **extra,
            }

            with _log_file_lock:
                with open(LOG_FILE, "a") as f:
                    f.write(json.dumps(entry) + "\n")

                # Trim log file if too large
                self._trim_log()
        except Exception:
            pass

    @staticmethod
    def _trim_log() -> None:
        """Keep only the last MAX_LOG_ENTRIES lines in the log file."""
        try:
            if not LOG_FILE.exists():
                return
            lines = LOG_FILE.read_text().splitlines()
            if len(lines) > MAX_LOG_ENTRIES:
                LOG_FILE.write_text("\n".join(lines[-MAX_LOG_ENTRIES:]) + "\n")
        except Exception:
            pass


# ==========================================
# RUNNER REGISTRY
# ==========================================
_runners: dict[str, ScriptRunner] = {}
_registry_lock = threading.Lock()


def register_runner(name: str, module_path: str, description: str, poll_interval: int = 7200) -> ScriptRunner:
    """Create and register a ScriptRunner."""
    with _registry_lock:
        runner = ScriptRunner(name, module_path, description, poll_interval)
        _runners[name] = runner
        return runner


def get_runner(name: str) -> ScriptRunner | None:
    """Get a runner by script name."""
    return _runners.get(name)


def get_all_runners() -> dict[str, ScriptRunner]:
    """Get all registered runners."""
    return dict(_runners)


def get_all_statuses() -> dict[str, dict]:
    """Get status snapshots for all runners."""
    return {name: runner.get_status() for name, runner in _runners.items()}


def get_logs(limit: int = 50, script: str | None = None) -> list[dict]:
    """Read recent log entries from run_log.jsonl."""
    try:
        if not LOG_FILE.exists():
            return []
        lines = LOG_FILE.read_text().splitlines()
        entries = []
        for line in reversed(lines):
            try:
                entry = json.loads(line)
                if script and entry.get("script") != script:
                    continue
                entries.append(entry)
                if len(entries) >= limit:
                    break
            except json.JSONDecodeError:
                continue
        return entries
    except Exception:
        return []
