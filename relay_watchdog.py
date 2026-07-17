#!/usr/bin/env python3
"""Keep the authenticated Aviator relay alive without storing credentials."""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


UTC = dt.timezone.utc
ROOT = Path(__file__).resolve().parent
RUNTIME = Path.home() / ".aviator-audit-runtime"
PROFILE = Path.home() / ".aviator-audit-browser"
TARGET_URL = "https://www.premierbet.com/cd/casino/game/aviator-291195"
DASHBOARD_URL = "https://aviator-audit.onrender.com/api/dashboard"
DEVTOOLS_URL = "http://127.0.0.1:9223"
STATE_FILE = RUNTIME / "watchdog-status.json"
LOG_FILE = RUNTIME / "watchdog.log"
BROWSER_LOG = RUNTIME / "browser.log"

CHECK_SECONDS = int(os.getenv("AVIATOR_WATCHDOG_CHECK_SECONDS", "30"))
STALE_SECONDS = int(os.getenv("AVIATOR_WATCHDOG_STALE_SECONDS", "300"))
RECOVERY_COOLDOWN = int(os.getenv("AVIATOR_WATCHDOG_RECOVERY_COOLDOWN", "300"))
AUTH_RETRY_SECONDS = int(os.getenv("AVIATOR_WATCHDOG_AUTH_RETRY_SECONDS", "1800"))

STOP = False


def utc_now() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso_utc(value: dt.datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="seconds")


def parse_timestamp(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def seconds_since(value: str | None, now: dt.datetime | None = None) -> float | None:
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    return max(0.0, ((now or utc_now()) - parsed).total_seconds())


def log(message: str) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"{iso_utc()} {message}\n")


def write_state(payload: dict[str, Any]) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"updated_at": iso_utc(), **payload}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(STATE_FILE)


def fetch_json(url: str, *, method: str = "GET", timeout: float = 12) -> Any:
    request = urllib.request.Request(
        url,
        method=method,
        headers={"User-Agent": "aviator-audit-watchdog/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def browser_binary() -> Path:
    candidates = (
        RUNTIME
        / "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise FileNotFoundError("Aucun navigateur Chrome compatible n'est installe")


def browser_command() -> list[str]:
    extension = ROOT / "browser-relay"
    if not (extension / "config.js").is_file():
        raise FileNotFoundError("browser-relay/config.js est absent")
    return [
        str(browser_binary()),
        f"--user-data-dir={PROFILE}",
        f"--load-extension={extension}",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=9223",
        "--no-first-run",
        "--no-default-browser-check",
        TARGET_URL,
    ]


def dedicated_browser_pid() -> int | None:
    try:
        output = subprocess.check_output(
            ["/bin/ps", "-ax", "-o", "pid=,command="], text=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    profile_flag = f"--user-data-dir={PROFILE}"
    for line in output.splitlines():
        if profile_flag not in line or "--type=" in line:
            continue
        pid, _, _command = line.strip().partition(" ")
        if pid.isdigit():
            return int(pid)
    return None


def launch_browser() -> int:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    PROFILE.mkdir(parents=True, exist_ok=True)
    browser_output = BROWSER_LOG.open("a", encoding="utf-8")
    process = subprocess.Popen(
        browser_command(),
        stdout=browser_output,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    browser_output.close()
    log(f"browser-launched pid={process.pid}")
    return process.pid


def devtools_targets() -> list[dict[str, Any]]:
    payload = fetch_json(f"{DEVTOOLS_URL}/json/list", timeout=4)
    return payload if isinstance(payload, list) else []


def is_game_target(target: dict[str, Any]) -> bool:
    return target.get("type") == "page" and "premierbet.com/cd/casino/game/aviator-291195" in str(
        target.get("url", "")
    )


def close_target(target: dict[str, Any]) -> None:
    target_id = urllib.parse.quote(str(target.get("id", "")), safe="")
    if target_id:
        fetch_json(f"{DEVTOOLS_URL}/json/close/{target_id}", timeout=4)


def tidy_game_targets() -> int:
    """Keep one game tab so restored duplicates cannot exhaust the renderer."""
    targets = devtools_targets()
    games = [target for target in targets if is_game_target(target)]
    closed = 0
    for target in games[1:]:
        try:
            close_target(target)
            closed += 1
        except (OSError, urllib.error.URLError, ValueError):
            pass
    for target in targets:
        if target.get("type") == "page" and target.get("url") == "chrome://newtab/":
            try:
                close_target(target)
                closed += 1
            except (OSError, urllib.error.URLError, ValueError):
                pass
    if closed:
        log(f"duplicate-tabs-closed count={closed}")
    return closed


def open_fresh_game() -> None:
    for target in devtools_targets():
        if not is_game_target(target):
            continue
        try:
            close_target(target)
        except (OSError, urllib.error.URLError, ValueError):
            pass
    encoded_url = urllib.parse.quote(TARGET_URL, safe="")
    fetch_json(f"{DEVTOOLS_URL}/json/new?{encoded_url}", method="PUT", timeout=6)
    log("game-page-reopened")


def stop_browser(pid: int, timeout: float = 12) -> None:
    try:
        process_group = os.getpgid(pid)
    except ProcessLookupError:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            log(f"browser-stopped pid={pid}")
            return
        time.sleep(0.5)
    try:
        os.killpg(process_group, signal.SIGKILL)
        log(f"browser-force-stopped pid={pid}")
    except ProcessLookupError:
        pass


def latest_round_age(payload: dict[str, Any], now: dt.datetime | None = None) -> float | None:
    return seconds_since(payload.get("last_round_at"), now)


def authentication_suspected(
    payload: dict[str, Any], now: dt.datetime | None = None
) -> bool:
    relay = payload.get("source", {}).get("relay") or {}
    stage = relay.get("stage")
    heartbeat_age = seconds_since(relay.get("updated_at_utc"), now)
    return bool(
        stage in {"premierbet-page", "provider-missing"}
        and heartbeat_age is not None
        and heartbeat_age <= 180
    )


def handle_signal(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def run() -> None:
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    log("watchdog-started")
    caffeinate = subprocess.Popen(
        ["/usr/bin/caffeinate", "-dimsu"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Give an existing or newly launched page time to initialize before recovery.
    last_recovery = time.monotonic()
    recovery_count = 0
    try:
        while not STOP:
            status: dict[str, Any] = {
                "watchdog": "running",
                "browser": "unknown",
                "render": "unknown",
                "recovery_count": recovery_count,
            }
            pid = dedicated_browser_pid()
            if pid is None:
                try:
                    pid = launch_browser()
                    status["browser"] = "launched"
                except Exception as error:  # keep launchd alive and retry safely
                    status["browser"] = "launch-error"
                    status["error"] = type(error).__name__
                    log(f"browser-launch-error type={type(error).__name__}")
            else:
                status["browser"] = "running"
            status["browser_pid"] = pid
            if pid is not None:
                try:
                    status["duplicate_tabs_closed"] = tidy_game_targets()
                except Exception:
                    # Chrome may still be starting; the next cycle retries.
                    status["duplicate_tabs_closed"] = None

            try:
                dashboard = fetch_json(DASHBOARD_URL)
                age = latest_round_age(dashboard)
                status["rounds"] = dashboard.get("rounds")
                status["last_round_at"] = dashboard.get("last_round_at")
                status["last_round_age_seconds"] = round(age, 1) if age is not None else None
                status["render"] = "fresh" if age is not None and age <= STALE_SECONDS else "stale"
                auth_suspected = status["render"] == "stale" and authentication_suspected(
                    dashboard
                )
                if auth_suspected:
                    status["render"] = "authentication-required"
            except Exception as error:
                age = None
                auth_suspected = False
                status["render"] = "unreachable"
                status["render_error"] = type(error).__name__

            recovery_delay = AUTH_RETRY_SECONDS if auth_suspected else RECOVERY_COOLDOWN
            due = time.monotonic() - last_recovery >= recovery_delay
            if pid is not None and auth_suspected and due:
                recovery_count += 1
                status["recovery_count"] = recovery_count
                try:
                    open_fresh_game()
                    status["last_recovery"] = "authentication-page-reopened"
                    log(f"recovery-authentication-page count={recovery_count}")
                except Exception as error:
                    status["last_recovery"] = "failed"
                    status["recovery_error"] = type(error).__name__
                    log(f"recovery-authentication-error type={type(error).__name__}")
                last_recovery = time.monotonic()
            elif pid is not None and status["render"] == "stale" and due:
                recovery_count += 1
                status["recovery_count"] = recovery_count
                try:
                    if recovery_count % 2 == 0:
                        stop_browser(pid)
                        if dedicated_browser_pid() is None:
                            launch_browser()
                        status["last_recovery"] = "browser-restarted"
                        log(f"recovery-browser-restart count={recovery_count}")
                    else:
                        open_fresh_game()
                        status["last_recovery"] = "game-page-reopened"
                        log(f"recovery-page-reopen count={recovery_count}")
                except Exception as error:
                    status["last_recovery"] = "failed"
                    status["recovery_error"] = type(error).__name__
                    log(f"recovery-error type={type(error).__name__}")
                last_recovery = time.monotonic()
            elif status["render"] == "fresh":
                recovery_count = 0
                status["recovery_count"] = 0

            write_state(status)
            deadline = time.monotonic() + CHECK_SECONDS
            while not STOP and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))
    finally:
        caffeinate.terminate()
        try:
            caffeinate.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        log("watchdog-stopped")


if __name__ == "__main__":
    run()
