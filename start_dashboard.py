#!/usr/bin/env python3
"""Lance l'API SQLite et l'interface React locale en une commande."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
NODE_BIN = Path("/Users/rawtani/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin")
NPM_BIN = Path("/Applications/ChatGPT.app/Contents/Resources/cua_node/bin")


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=0.25):
            return True
    except OSError:
        return False


def wait_for(url: str, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"Le service n'a pas démarré: {url}")


def collector_active(pid_path: Path) -> bool:
    if not pid_path.exists():
        return False
    try:
        os.kill(int(pid_path.read_text().strip()), 0)
        return True
    except (OSError, ValueError):
        pid_path.unlink(missing_ok=True)
        return False


def main() -> None:
    processes: list[subprocess.Popen[bytes]] = []
    critical_processes: list[subprocess.Popen[bytes]] = []
    collector_process: subprocess.Popen[bytes] | None = None
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join(
        [str(NODE_BIN), str(NPM_BIN), environment.get("PATH", "")]
    )
    try:
        if not port_open(8765):
            api_process = subprocess.Popen(
                    [sys.executable, "aviator_audit.py", "--db", "aviator.sqlite3", "serve"],
                    cwd=ROOT,
                )
            processes.append(api_process)
            critical_processes.append(api_process)
        wait_for("http://127.0.0.1:8765/health")

        config = ROOT / "config.json"
        if config.exists() and not collector_active(ROOT / "aviator.sqlite3.collector.pid"):
            collector_process = subprocess.Popen(
                    [
                        sys.executable,
                        "aviator_audit.py",
                        "--db",
                        "aviator.sqlite3",
                        "collect",
                        "--config",
                        "config.json",
                        "--duration-days",
                        "20",
                    ],
                    cwd=ROOT,
                )
            processes.append(collector_process)
        else:
            print("Source non configurée: dashboard actif, collecte en attente.", flush=True)

        if not port_open(3000):
            npm = shutil.which("npm", path=environment["PATH"])
            if not npm:
                raise RuntimeError("npm est introuvable; installez Node.js 22 ou plus récent.")
            web_process = subprocess.Popen(
                [npm, "run", "dev"], cwd=ROOT / "dashboard", env=environment
            )
            processes.append(web_process)
            critical_processes.append(web_process)
        wait_for("http://localhost:3000")
        print("Tableau de bord actif: http://localhost:3000", flush=True)
        webbrowser.open("http://localhost:3000")
        while True:
            if any(process.poll() is not None for process in critical_processes):
                raise RuntimeError("Un service local s'est arrêté de façon inattendue.")
            if collector_process and collector_process.poll() is not None:
                if collector_process.returncode:
                    raise RuntimeError("La collecte s'est arrêtée avec une erreur.")
                print("Campagne de collecte terminée; le tableau de bord reste actif.", flush=True)
                collector_process = None
            time.sleep(1)
    except KeyboardInterrupt:
        print("Arrêt du tableau de bord…")
    finally:
        for process in processes:
            process.send_signal(signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    main()
