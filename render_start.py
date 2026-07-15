#!/usr/bin/env python3
"""Point d'entrée Render: interface, API et collecte dans un seul service."""

from __future__ import annotations

import http.server
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import aviator_audit


ROOT = Path(__file__).resolve().parent
FRONTEND_PORT = 3001
STARTED_AT = time.time()
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def database_path() -> Path:
    explicit = os.environ.get("AVIATOR_DB_PATH")
    if explicit:
        return Path(explicit)
    data_dir = Path(os.environ.get("AVIATOR_DATA_DIR", str(ROOT)))
    return data_dir / "aviator.sqlite3"


def collector_config() -> Path | None:
    """Construit une configuration éphémère à partir des secrets Render."""
    raw_config = os.environ.get("AVIATOR_CONFIG_JSON", "").strip()
    source_url = os.environ.get("AVIATOR_SOURCE_URL", "").strip()
    if not raw_config and not source_url:
        return None

    if raw_config:
        config = json.loads(raw_config)
    else:
        config: dict[str, Any] = {
            "source": os.environ.get("AVIATOR_SOURCE_NAME", source_url),
            "url": source_url,
            "poll_interval_seconds": float(os.environ.get("AVIATOR_POLL_SECONDS", "5")),
            "timeout_seconds": float(os.environ.get("AVIATOR_TIMEOUT_SECONDS", "20")),
            "items_path": os.environ.get("AVIATOR_ITEMS_PATH", "data.rounds"),
            "round_id_path": os.environ.get("AVIATOR_ROUND_ID_PATH", "id"),
            "timestamp_path": os.environ.get("AVIATOR_TIMESTAMP_PATH", "created_at"),
            "multiplier_path": os.environ.get("AVIATOR_MULTIPLIER_PATH", "multiplier"),
            "headers": json.loads(os.environ.get("AVIATOR_HEADERS_JSON", "{}")),
            "headers_from_env": {},
        }

    if not isinstance(config, dict) or not str(config.get("url", "")).startswith(("http://", "https://")):
        raise ValueError("AVIATOR_CONFIG_JSON doit contenir une URL HTTP(S) valide.")
    if not config.get("multiplier_path"):
        raise ValueError("multiplier_path est obligatoire dans la configuration de collecte.")

    path = Path(tempfile.gettempdir()) / "aviator-render-config.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)
    return path


def wait_for_frontend(process: subprocess.Popen[bytes], timeout: float = 90) -> None:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{FRONTEND_PORT}/"
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Le serveur web s'est arrêté (code {process.returncode}).")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status < 500:
                    return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("Le serveur web n'a pas démarré à temps.")


def send_json(handler: http.server.BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


def make_handler(
    db_path: Path,
    frontend: subprocess.Popen[bytes],
    collector: subprocess.Popen[bytes] | None,
    source_configured: bool,
) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802
            route = self.path.split("?", 1)[0].rstrip("/")
            if route == "/health":
                frontend_ok = frontend.poll() is None
                collector_code = collector.poll() if collector else None
                collector_ok = not source_configured or collector_code in (None, 0)
                send_json(
                    self,
                    {
                        "status": "ok" if frontend_ok and collector_ok else "error",
                        "frontend": "running" if frontend_ok else "stopped",
                        "collector": (
                            "not-configured"
                            if not source_configured
                            else "running"
                            if collector_code is None
                            else "completed"
                            if collector_code == 0
                            else "failed"
                        ),
                        "deployment_mode": os.environ.get(
                            "AVIATOR_DEPLOYMENT_MODE", "render"
                        ),
                        "database": str(db_path),
                        "time": aviator_audit.utc_now(),
                    },
                    200 if frontend_ok and collector_ok else 503,
                )
                return
            if route == "/api/dashboard":
                send_json(self, aviator_audit.dashboard_payload(db_path, STARTED_AT))
                return
            self.proxy_frontend()

        def proxy_frontend(self) -> None:
            target = f"http://127.0.0.1:{FRONTEND_PORT}{self.path}"
            headers = {
                "Accept": self.headers.get("Accept", "*/*"),
                "Accept-Encoding": self.headers.get("Accept-Encoding", "identity"),
                "User-Agent": self.headers.get("User-Agent", "aviator-render-proxy"),
            }
            request = urllib.request.Request(target, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    body = response.read()
                    self.send_response(response.status)
                    for name, value in response.headers.items():
                        if name.lower() not in HOP_BY_HOP_HEADERS and name.lower() != "content-length":
                            self.send_header(name, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(body)
            except urllib.error.HTTPError as error:
                body = error.read()
                self.send_response(error.code)
                self.send_header("Content-Type", error.headers.get("Content-Type", "text/plain"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
            except OSError as error:
                send_json(self, {"error": "Interface indisponible", "detail": str(error)}, 502)

        def log_message(self, format: str, *args: Any) -> None:
            print(f"{aviator_audit.utc_now()} render {format % args}", flush=True)

    return Handler


def stop_process(process: subprocess.Popen[bytes] | None) -> None:
    if not process or process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def main() -> None:
    db_path = database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    aviator_audit.connect(db_path).close()
    config_path = collector_config()

    frontend = subprocess.Popen(
        ["npm", "run", "start", "--", "--host", "127.0.0.1", "--port", str(FRONTEND_PORT)],
        cwd=ROOT / "dashboard",
    )
    collector: subprocess.Popen[bytes] | None = None
    server: http.server.ThreadingHTTPServer | None = None
    try:
        wait_for_frontend(frontend)
        if config_path:
            # Un PID d'un ancien conteneur ne doit pas bloquer la reprise.
            db_path.with_suffix(db_path.suffix + ".collector.pid").unlink(missing_ok=True)
            collector = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "aviator_audit.py"),
                    "--db",
                    str(db_path),
                    "collect",
                    "--config",
                    str(config_path),
                    "--duration-days",
                    os.environ.get("AVIATOR_DURATION_DAYS", "20"),
                ],
                cwd=ROOT,
            )
            print("Collecteur Render démarré pour une campagne de 20 jours.", flush=True)
        else:
            print("Source non configurée: interface en ligne, collecte en attente.", flush=True)

        port = int(os.environ.get("PORT", "10000"))
        handler = make_handler(db_path, frontend, collector, config_path is not None)
        server = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)

        def shutdown(_signal: int, _frame: Any) -> None:
            if server:
                threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        print(f"Aviator Audit écoute sur 0.0.0.0:{port}", flush=True)
        server.serve_forever()
    finally:
        if server:
            server.server_close()
        stop_process(collector)
        stop_process(frontend)


if __name__ == "__main__":
    main()
