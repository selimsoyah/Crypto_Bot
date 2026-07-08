"""
bot_runtime.py
==============
Shared runtime detection and control for the headless trading engine.

The dashboard and ``bot_loop`` coordinate through:
  * ``.bot_instance.lock`` — single-engine guard (PID of live process)
  * ``.bot_runtime_snapshot.json`` — latest in-process state written each scan
  * ``status_log`` SQLite rows — durable heartbeat + trade history
  * optional ``crypto-bot`` systemd unit — always-on server deployment
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

import config

INSTANCE_LOCK_PATH = str(config.BASE_DIR / ".bot_instance.lock")
RUNTIME_SNAPSHOT_PATH = config.RUNTIME_SNAPSHOT_FILE
BOT_SYSTEMD_UNIT = config.BOT_SYSTEMD_UNIT


def read_lock_pid() -> int:
    try:
        with open(INSTANCE_LOCK_PATH, encoding="utf-8") as fh:
            return int(str(fh.read()).strip() or "0")
    except (OSError, ValueError):
        return 0


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_runtime_snapshot() -> dict[str, Any]:
    try:
        with open(RUNTIME_SNAPSHOT_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _snapshot_age_seconds(snapshot: dict[str, Any]) -> Optional[float]:
    raw = snapshot.get("updated_at")
    if not raw:
        return None
    ts = pd.to_datetime(raw, errors="coerce", utc=True)
    if pd.isna(ts):
        return None
    return max(0.0, (pd.Timestamp.now(tz="UTC") - ts).total_seconds())


def _log_age_seconds(log: pd.DataFrame) -> Optional[float]:
    if log is None or log.empty:
        return None
    ts = pd.to_datetime(log.iloc[-1].get("Timestamp"), errors="coerce", utc=True)
    if pd.isna(ts):
        return None
    return max(0.0, (pd.Timestamp.now(tz="UTC") - ts).total_seconds())


def systemd_unit_available() -> bool:
    try:
        proc = subprocess.run(
            ["systemctl", "status", BOT_SYSTEMD_UNIT],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return proc.returncode in (0, 3)
    except (OSError, subprocess.TimeoutExpired):
        return False


def _run_systemctl(action: str) -> tuple[bool, str]:
    commands = (
        ["sudo", "-n", "systemctl", action, BOT_SYSTEMD_UNIT],
        ["systemctl", action, BOT_SYSTEMD_UNIT],
    )
    last_msg = "systemctl unavailable"
    for cmd in commands:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
            if proc.returncode == 0:
                return True, (proc.stdout or proc.stderr or f"{action} ok").strip()
            last_msg = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            last_msg = str(exc)
    return False, last_msg


def systemd_service_state() -> str:
    commands = (
        ["sudo", "-n", "systemctl", "is-active", BOT_SYSTEMD_UNIT],
        ["systemctl", "is-active", BOT_SYSTEMD_UNIT],
    )
    for cmd in commands:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
            state = (proc.stdout or "").strip().lower()
            if state:
                return state
        except (OSError, subprocess.TimeoutExpired):
            continue
    return "unknown"


def systemd_service_active() -> bool:
    return systemd_service_state() == "active"


def systemd_service_failure_message() -> str:
    commands = (
        ["sudo", "-n", "systemctl", "status", BOT_SYSTEMD_UNIT, "-n", "15", "--no-pager"],
        ["systemctl", "status", BOT_SYSTEMD_UNIT, "-n", "15", "--no-pager"],
    )
    for cmd in commands:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8, check=False)
            text = (proc.stdout or proc.stderr or "").strip()
            if text:
                return text
        except (OSError, subprocess.TimeoutExpired):
            continue
    return "Could not read systemd status for crypto-bot."


def start_engine_service() -> tuple[bool, str]:
    if not systemd_unit_available():
        return False, f"Systemd unit '{BOT_SYSTEMD_UNIT}' is not installed on this host."
    ok, msg = _run_systemctl("start")
    if not ok:
        return False, msg
    time.sleep(0.5)
    state = systemd_service_state()
    if state == "failed":
        return False, systemd_service_failure_message()
    if state in ("inactive", "dead", "unknown"):
        return False, systemd_service_failure_message()
    return True, f"Service state: {state}"


def stop_engine_service() -> tuple[bool, str]:
    if not systemd_unit_available():
        return False, f"Systemd unit '{BOT_SYSTEMD_UNIT}' is not installed on this host."
    return _run_systemctl("stop")


def engine_runtime_status(log: Optional[pd.DataFrame] = None) -> dict[str, Any]:
    """Return unified engine heartbeat used by the dashboard."""
    snapshot = read_runtime_snapshot()
    lock_pid = read_lock_pid()
    snapshot_pid = int(snapshot.get("pid", 0) or 0)
    pid = lock_pid or snapshot_pid
    lock_alive = is_pid_alive(lock_pid) if lock_pid > 0 else False
    snapshot_alive = is_pid_alive(snapshot_pid) if snapshot_pid > 0 else False
    process_alive = lock_alive or snapshot_alive

    snapshot_age = _snapshot_age_seconds(snapshot)
    log_age = _log_age_seconds(log) if log is not None else None
    stale_threshold = max(30.0, config.LOOP_SLEEP_SECONDS * 4)

    heartbeat_age = snapshot_age
    if heartbeat_age is None:
        heartbeat_age = log_age
    elif log_age is not None:
        heartbeat_age = min(snapshot_age, log_age)

    snapshot_fresh = snapshot_age is not None and snapshot_age <= stale_threshold
    log_fresh = log_age is not None and log_age <= stale_threshold

    # Fresh SQLite heartbeats are ground truth — the activity feed proves the engine is scanning.
    running = bool(log_fresh)
    if not running:
        running = bool(
            lock_alive
            and bool(snapshot.get("running"))
            and snapshot_fresh
        )
    service_state = systemd_service_state() if systemd_unit_available() else "offline"
    booting = bool(
        not running
        and (
            service_state in ("activating", "reloading")
            or (service_state == "active" and not lock_alive)
            or (lock_alive and not snapshot_fresh and not log_fresh)
        )
    )
    if running:
        stale = False
    elif booting:
        stale = False
    elif process_alive and heartbeat_age is not None:
        stale = heartbeat_age > stale_threshold
    else:
        stale = heartbeat_age is None or heartbeat_age > stale_threshold

    mode = "headless"
    if systemd_unit_available():
        mode = "systemd"
    elif not process_alive:
        mode = "offline"

    degraded = bool(snapshot.get("connection_degraded", False))
    connection_error = str(snapshot.get("connection_error", "") or "")

    return {
        "running": running,
        "booting": booting,
        "stale": stale,
        "degraded": degraded,
        "process_alive": process_alive,
        "pid": pid if process_alive else 0,
        "mode": mode,
        "service_state": service_state,
        "heartbeat_age": heartbeat_age,
        "snapshot": snapshot,
        "connection_error": connection_error,
        "last_error": str(snapshot.get("last_error", "") or ""),
        "systemd_available": systemd_unit_available(),
    }


def risk_flags_from_runtime(runtime: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    runtime = runtime or engine_runtime_status()
    snapshot = runtime.get("snapshot", {}) or {}
    risk = snapshot.get("risk", {}) if isinstance(snapshot.get("risk"), dict) else {}
    return {
        "kill_switch_active": os.path.exists(config.KILL_SWITCH_FILE),
        "manual_resume_required": bool(risk.get("manual_resume_required"))
        or os.path.exists(config.RISK_MANUAL_RESUME_FILE),
        "halted": bool(risk.get("halted")),
        "halt_reason": str(risk.get("halt_reason", "") or ""),
    }


def write_runtime_snapshot(payload: dict[str, Any]) -> None:
    payload = dict(payload)
    payload.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
    tmp_path = f"{RUNTIME_SNAPSHOT_PATH}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp_path, RUNTIME_SNAPSHOT_PATH)
    except OSError:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def clear_runtime_snapshot() -> None:
    try:
        if os.path.exists(RUNTIME_SNAPSHOT_PATH):
            os.remove(RUNTIME_SNAPSHOT_PATH)
    except OSError:
        pass


def wait_for_engine_start(timeout_seconds: float = 20.0, log: Optional[pd.DataFrame] = None) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        status = engine_runtime_status(log)
        if status.get("running"):
            return True
        if systemd_service_state() == "failed":
            return False
        time.sleep(0.5)
    return bool(engine_runtime_status(log).get("running"))


def wait_for_engine_stop(timeout_seconds: float = 20.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        pid = read_lock_pid()
        if pid <= 0 or not is_pid_alive(pid):
            return True
        time.sleep(0.5)
    return not is_pid_alive(read_lock_pid())
