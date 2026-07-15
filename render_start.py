#!/usr/bin/env python3
"""Point d'entrée Render: interface, API et collecte dans un seul service."""

from __future__ import annotations

import http.server
import hmac
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import aviator_audit


ROOT = Path(__file__).resolve().parent
FRONTEND_PORT = 3001
STARTED_AT = time.time()
PREMIERBET_GAME_ID = "291195"
PREMIERBET_API = "https://gaming-api.premierbet.com/cd/v1"
SOURCE_PROBE_LOCK = threading.Lock()
SOURCE_PROBE: dict[str, Any] = {
    "operator": "PremierBet CD",
    "game_id": PREMIERBET_GAME_ID,
    "status": "checking",
    "collection_ready": False,
    "message": "Vérification de la source PremierBet en cours.",
    "checked_at": None,
}
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


def _public_request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Origin": "https://www.premierbet.com",
            "Referer": "https://www.premierbet.com/cd/casino/game/aviator-291195",
            "User-Agent": "Mozilla/5.0 Aviator-Audit-Source-Probe/1.0",
        },
    )


def refresh_premierbet_probe() -> None:
    """Vérifie les endpoints publics sans cookie ni contournement de connexion."""
    query = urllib.parse.urlencode(
        {
            "country": "CD",
            "group": "g5",
            "platform": "desktop",
            "locale": "fr",
            "query": "aviator",
        }
    )
    catalog_url = f"{PREMIERBET_API}/casino/games/search?{query}"
    launch_query = urllib.parse.urlencode(
        {
            "country": "CD",
            "group": "g5",
            "platform": "desktop",
            "locale": "fr",
            "lobbyUrl": "https://www.premierbet.com/cd/casino",
        }
    )
    launch_url = (
        f"{PREMIERBET_API}/casino/game/{PREMIERBET_GAME_ID}/launch-url?{launch_query}"
    )
    result: dict[str, Any] = {
        "operator": "PremierBet CD",
        "game_id": PREMIERBET_GAME_ID,
        "status": "unavailable",
        "collection_ready": False,
        "catalog_available": False,
        "game_available": False,
        "fun_mode_available": None,
        "launch_requires_authentication": None,
        "message": "La source PremierBet n'est pas joignable pour le moment.",
        "checked_at": aviator_audit.utc_now(),
    }
    try:
        with urllib.request.urlopen(_public_request(catalog_url), timeout=15) as response:
            payload = json.load(response)
        games = payload.get("data", {}).get("games", [])
        game = next(
            (item for item in games if str(item.get("id")) == PREMIERBET_GAME_ID),
            None,
        )
        result["catalog_available"] = True
        result["game_available"] = game is not None
        if game:
            result["display_name"] = game.get("displayName")
            result["provider"] = game.get("providerDisplayName")
            result["fun_mode_available"] = bool(game.get("isFunModeAvailable"))

        try:
            with urllib.request.urlopen(_public_request(launch_url), timeout=15) as response:
                response.read(1)
            result.update(
                {
                    "status": "launch-accessible",
                    "launch_requires_authentication": False,
                    "message": (
                        "L'URL de lancement est accessible, mais aucun flux de manches "
                        "n'est encore configuré dans le collecteur."
                    ),
                }
            )
        except urllib.error.HTTPError as error:
            if error.code == 401:
                result.update(
                    {
                        "status": "authentication-required",
                        "launch_requires_authentication": True,
                        "message": (
                            "Aviator est disponible, mais PremierBet refuse le lancement "
                            "sans une session de jeu authentifiée (HTTP 401)."
                        ),
                    }
                )
            else:
                result["message"] = f"Le lancement PremierBet répond HTTP {error.code}."
    except urllib.error.HTTPError as error:
        if error.code == 403:
            result.update(
                {
                    "status": "hosting-network-blocked",
                    "message": (
                        "PremierBet/Cloudflare refuse l'adresse réseau de cet "
                        "hébergement avant même le lancement du jeu (HTTP 403)."
                    ),
                }
            )
        else:
            result["message"] = f"Le catalogue PremierBet répond HTTP {error.code}."
    except (OSError, ValueError, TypeError, urllib.error.URLError) as error:
        result["message"] = f"Sonde PremierBet en échec: {str(error)[:300]}"
    with SOURCE_PROBE_LOCK:
        SOURCE_PROBE.clear()
        SOURCE_PROBE.update(result)


def source_probe_snapshot(
    source_configured: bool,
    relay_configured: bool = False,
    db_path: Path | None = None,
) -> dict[str, Any]:
    if source_configured:
        return {
            "operator": os.environ.get("AVIATOR_SOURCE_NAME", "Source configurée"),
            "status": "collector-configured",
            "collection_ready": True,
            "message": "Le collecteur dispose d'une source configurée.",
            "checked_at": aviator_audit.utc_now(),
        }
    if relay_configured and db_path is not None:
        relay = aviator_audit.relay_status(db_path)
        connection = aviator_audit.connect(db_path)
        campaign = connection.execute(
            """
            SELECT id, status, started_at_utc, ends_at_utc, last_success_at_utc
            FROM campaigns WHERE source=? ORDER BY id DESC LIMIT 1
            """,
            (aviator_audit.PREMIERBET_RELAY_SOURCE,),
        ).fetchone()
        connection.close()
        if campaign:
            return {
                "operator": "PremierBet CD",
                "game_id": PREMIERBET_GAME_ID,
                "status": "relay-running" if campaign[1] == "running" else "relay-completed",
                "collection_ready": campaign[1] == "running",
                "message": (
                    "Le relais local envoie les manches réelles vers Render."
                    if campaign[1] == "running"
                    else "La campagne de collecte par relais est terminée."
                ),
                "campaign_id": campaign[0],
                "started_at": campaign[2],
                "ends_at": campaign[3],
                "checked_at": campaign[4] or aviator_audit.utc_now(),
                "relay": relay,
            }
        stage_messages = {
            "extension-started": "Extension chargée; attente de la page PremierBet.",
            "premierbet-page": "Page PremierBet détectée; attente de l'iframe SPRIBE.",
            "provider-frame": "Iframe SPRIBE détectée; attente de l'historique des manches.",
            "history-detected": "Historique Aviator détecté; attente de la prochaine manche.",
        }
        return {
            "operator": "PremierBet CD",
            "game_id": PREMIERBET_GAME_ID,
            "status": "relay-ready",
            "collection_ready": True,
            "message": stage_messages.get(
                relay["stage"] if relay else "",
                "Relais sécurisé prêt; attente du navigateur local.",
            ),
            "checked_at": relay["updated_at_utc"] if relay else aviator_audit.utc_now(),
            "relay": relay,
        }
    with SOURCE_PROBE_LOCK:
        return dict(SOURCE_PROBE)


def source_probe_loop() -> None:
    while True:
        refresh_premierbet_probe()
        time.sleep(300)


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


def send_json(
    handler: http.server.BaseHTTPRequestHandler,
    payload: Any,
    status: int = 200,
    cors: bool = False,
) -> None:
    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    if cors:
        handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


def make_handler(
    db_path: Path,
    frontend: subprocess.Popen[bytes],
    collector: subprocess.Popen[bytes] | None,
    source_configured: bool,
    relay_configured: bool,
) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_OPTIONS(self) -> None:  # noqa: N802
            route = self.path.split("?", 1)[0].rstrip("/")
            if route not in {"/api/ingest", "/api/relay-heartbeat"}:
                self.send_error(404)
                return
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Max-Age", "86400")
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            route = self.path.split("?", 1)[0].rstrip("/")
            if route not in {"/api/ingest", "/api/relay-heartbeat"}:
                send_json(self, {"error": "Route inconnue"}, 404, cors=True)
                return
            expected = os.environ.get("AVIATOR_INGEST_TOKEN", "").strip()
            if not expected:
                send_json(self, {"error": "Relais non configuré"}, 503, cors=True)
                return
            authorization = self.headers.get("Authorization", "")
            supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
            if not supplied or not hmac.compare_digest(supplied, expected):
                send_json(self, {"error": "Non autorisé"}, 401, cors=True)
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= content_length <= 8192:
                    raise ValueError("Taille de requête invalide")
                payload = json.loads(self.rfile.read(content_length))
                if route == "/api/relay-heartbeat":
                    result = aviator_audit.record_relay_status(
                        db_path,
                        str(payload.get("stage", "")),
                        str(payload.get("frame_host", "")),
                    )
                    send_json(self, result, 200, cors=True)
                    return
                result = aviator_audit.ingest_relay_round(
                    db_path,
                    payload,
                    duration_days=float(os.environ.get("AVIATOR_DURATION_DAYS", "20")),
                )
                send_json(self, result, 200 if result["accepted"] else 410, cors=True)
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                send_json(self, {"error": str(error)[:300]}, 400, cors=True)

        def do_GET(self) -> None:  # noqa: N802
            route = self.path.split("?", 1)[0].rstrip("/")
            if route == "/health":
                frontend_ok = frontend.poll() is None
                collector_code = collector.poll() if collector else None
                collector_ok = relay_configured or not source_configured or collector_code in (None, 0)
                source = source_probe_snapshot(
                    source_configured, relay_configured, db_path
                )
                send_json(
                    self,
                    {
                        "status": "ok" if frontend_ok and collector_ok else "error",
                        "frontend": "running" if frontend_ok else "stopped",
                        "collector": (
                            source["status"]
                            if relay_configured
                            else "blocked-source"
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
                        "source": source,
                        "time": aviator_audit.utc_now(),
                    },
                    200 if frontend_ok and collector_ok else 503,
                )
                return
            if route == "/api/dashboard":
                payload = aviator_audit.dashboard_payload(db_path, STARTED_AT)
                payload["source"] = source_probe_snapshot(
                    source_configured, relay_configured, db_path
                )
                send_json(self, payload)
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
    relay_configured = bool(os.environ.get("AVIATOR_INGEST_TOKEN", "").strip())
    if config_path is None and not relay_configured:
        threading.Thread(target=source_probe_loop, daemon=True).start()

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
        elif relay_configured:
            print("Relais local sécurisé prêt pour la campagne de 20 jours.", flush=True)
        else:
            print("Source non configurée: interface en ligne, collecte en attente.", flush=True)

        port = int(os.environ.get("PORT", "10000"))
        handler = make_handler(
            db_path,
            frontend,
            collector,
            config_path is not None,
            relay_configured,
        )
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
