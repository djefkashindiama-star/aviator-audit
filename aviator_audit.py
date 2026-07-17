#!/usr/bin/env python3
"""Collecte et audit statistique de résultats de jeux crash.

Ce programme ne prédit pas une manche future. Il conserve des observations
publiques autorisées et teste si l'historique contient un signal reproductible.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import http.server
import json
import math
import os
import re
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Any, Iterable


UTC = dt.timezone.utc
PREMIERBET_RELAY_SOURCE = "premierbet-cd-aviator-291195"
RELAY_ROUND_ID = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def utc_now() -> str:
    return dt.datetime.now(UTC).isoformat(timespec="milliseconds")


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS rounds (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            round_id TEXT NOT NULL,
            observed_at_utc TEXT NOT NULL,
            occurred_at_utc TEXT,
            multiplier REAL NOT NULL CHECK(multiplier >= 1.0),
            raw_json TEXT,
            UNIQUE(source, round_id)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_rounds_time ON rounds(occurred_at_utc, observed_at_utc)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'stopped')),
            duration_days REAL NOT NULL,
            started_at_utc TEXT NOT NULL,
            ends_at_utc TEXT NOT NULL,
            completed_at_utc TEXT,
            polls INTEGER NOT NULL DEFAULT 0,
            successful_polls INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            last_poll_at_utc TEXT,
            last_success_at_utc TEXT,
            last_error TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_responses (
            id INTEGER PRIMARY KEY,
            campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
            observed_at_utc TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            byte_size INTEGER NOT NULL,
            payload_zlib BLOB NOT NULL,
            rows_seen INTEGER NOT NULL,
            rows_added INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_raw_campaign_time ON raw_responses(campaign_id, observed_at_utc)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS relay_status (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            stage TEXT NOT NULL,
            frame_host TEXT,
            updated_at_utc TEXT NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def record_relay_status(db_path: Path, stage: str, frame_host: str = "") -> dict[str, str]:
    allowed = {
        "extension-started",
        "premierbet-page",
        "provider-missing",
        "provider-frame",
        "history-detected",
    }
    if stage not in allowed:
        raise ValueError("stage de relais invalide")
    host = str(frame_host)[:200]
    if host and not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise ValueError("frame_host invalide")
    updated = utc_now()
    connection = connect(db_path)
    connection.execute(
        """
        INSERT INTO relay_status(id, stage, frame_host, updated_at_utc)
        VALUES (1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            stage=excluded.stage,
            frame_host=excluded.frame_host,
            updated_at_utc=excluded.updated_at_utc
        """,
        (stage, host or None, updated),
    )
    connection.commit()
    connection.close()
    return {"stage": stage, "frame_host": host, "updated_at_utc": updated}


def relay_status(db_path: Path) -> dict[str, Any] | None:
    connection = connect(db_path)
    row = connection.execute(
        "SELECT stage, frame_host, updated_at_utc FROM relay_status WHERE id=1"
    ).fetchone()
    connection.close()
    if not row:
        return None
    return {"stage": row[0], "frame_host": row[1], "updated_at_utc": row[2]}


def get_path(value: Any, path: str) -> Any:
    """Lit une notation simple: data.rounds.0.multiplier."""
    if not path:
        return value
    current = value
    for part in path.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(f"Impossible de lire {part!r} dans {type(current).__name__}")
    return current


def normalized_round(
    item: dict[str, Any], config: dict[str, Any], source: str
) -> tuple[str, str | None, float, str]:
    multiplier = float(get_path(item, config["multiplier_path"]))
    if not math.isfinite(multiplier) or multiplier < 1:
        raise ValueError(f"Multiplicateur invalide: {multiplier}")
    occurred = (
        str(get_path(item, config["timestamp_path"]))
        if config.get("timestamp_path")
        else None
    )
    raw = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if config.get("round_id_path"):
        round_id = str(get_path(item, config["round_id_path"]))
    else:
        round_id = hashlib.sha256(
            f"{source}|{occurred}|{multiplier}|{raw}".encode()
        ).hexdigest()
    return round_id, occurred, multiplier, raw


def insert_rows(
    connection: sqlite3.Connection,
    source: str,
    rows: Iterable[tuple[str, str | None, float, str]],
) -> int:
    before = connection.total_changes
    observed = utc_now()
    connection.executemany(
        """
        INSERT OR IGNORE INTO rounds
        (source, round_id, observed_at_utc, occurred_at_utc, multiplier, raw_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ((source, rid, observed, occurred, value, raw) for rid, occurred, value, raw in rows),
    )
    connection.commit()
    return connection.total_changes - before


def fetch_json(config: dict[str, Any]) -> Any:
    headers = {
        "Accept": "application/json",
        "User-Agent": "aviator-statistical-audit/1.0",
        **config.get("headers", {}),
    }
    for name, env_name in config.get("headers_from_env", {}).items():
        value = os.environ.get(env_name)
        if not value:
            raise RuntimeError(f"Variable d'environnement manquante: {env_name}")
        headers[name] = value
    request = urllib.request.Request(config["url"], headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=float(config.get("timeout_seconds", 20))) as response:
        if "json" not in (response.headers.get("Content-Type") or "").lower():
            raise RuntimeError(f"Réponse non JSON: {response.headers.get('Content-Type')}")
        return json.load(response)


def parse_utc(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def open_campaign(
    connection: sqlite3.Connection, source: str, duration_days: float
) -> tuple[int, dt.datetime]:
    now = dt.datetime.now(UTC)
    existing = connection.execute(
        """
        SELECT id, ends_at_utc FROM campaigns
        WHERE source = ? AND status = 'running'
        ORDER BY id DESC LIMIT 1
        """,
        (source,),
    ).fetchone()
    if existing:
        deadline = parse_utc(existing[1])
        if deadline > now:
            return int(existing[0]), deadline
        connection.execute(
            "UPDATE campaigns SET status='completed', completed_at_utc=? WHERE id=?",
            (utc_now(), existing[0]),
        )
    deadline = now + dt.timedelta(days=duration_days)
    cursor = connection.execute(
        """
        INSERT INTO campaigns(source, status, duration_days, started_at_utc, ends_at_utc)
        VALUES (?, 'running', ?, ?, ?)
        """,
        (source, duration_days, now.isoformat(), deadline.isoformat()),
    )
    connection.commit()
    return int(cursor.lastrowid), deadline


def ingest_relay_round(
    db_path: Path,
    payload: dict[str, Any],
    duration_days: float = 20,
    source: str = PREMIERBET_RELAY_SOURCE,
) -> dict[str, Any]:
    """Valide et archive une manche envoyée par l'extension locale.

    La campagne démarre à la première manche réelle et ne redémarre pas
    automatiquement après son échéance.
    """
    if duration_days <= 0:
        raise ValueError("duration_days doit être positif")
    if not isinstance(payload, dict):
        raise TypeError("Le corps JSON doit être un objet")

    round_id = str(payload.get("round_id", "")).strip()
    if not RELAY_ROUND_ID.fullmatch(round_id):
        raise ValueError("round_id invalide")
    multiplier = float(payload.get("multiplier"))
    if not math.isfinite(multiplier) or not 1 <= multiplier <= 1_000_000:
        raise ValueError("multiplicateur invalide")

    occurred = payload.get("observed_at_utc")
    if occurred:
        occurred_at = parse_utc(str(occurred))
        if abs((dt.datetime.now(UTC) - occurred_at).total_seconds()) > 86_400:
            raise ValueError("horodatage hors fenêtre autorisée")
        occurred = occurred_at.isoformat(timespec="milliseconds")
    else:
        occurred = utc_now()

    sanitized = {
        "round_id": round_id,
        "multiplier": multiplier,
        "observed_at_utc": occurred,
        "collector_id": str(payload.get("collector_id", ""))[:80],
        "frame_host": str(payload.get("frame_host", ""))[:200],
        "history_size": int(payload.get("history_size", 0)),
    }
    connection = connect(db_path)
    latest = connection.execute(
        """
        SELECT id, status, ends_at_utc FROM campaigns
        WHERE source=? ORDER BY id DESC LIMIT 1
        """,
        (source,),
    ).fetchone()
    now = dt.datetime.now(UTC)
    if latest is None:
        campaign_id, deadline = open_campaign(connection, source, duration_days)
    else:
        campaign_id, status, raw_deadline = int(latest[0]), str(latest[1]), latest[2]
        deadline = parse_utc(raw_deadline)
        if status != "running":
            return {
                "accepted": False,
                "campaign_id": campaign_id,
                "campaign_status": status,
                "ends_at_utc": deadline.isoformat(),
            }
        if deadline <= now:
            connection.execute(
                "UPDATE campaigns SET status='completed', completed_at_utc=? WHERE id=?",
                (utc_now(), campaign_id),
            )
            connection.commit()
            return {
                "accepted": False,
                "campaign_id": campaign_id,
                "campaign_status": "completed",
                "ends_at_utc": deadline.isoformat(),
            }

    raw = json.dumps(sanitized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    added = insert_rows(
        connection,
        source,
        [(round_id, str(occurred), multiplier, raw)],
    )
    archive_response(connection, campaign_id, sanitized, rows_seen=1, rows_added=added)
    total = connection.execute(
        "SELECT COUNT(*) FROM rounds WHERE source=?", (source,)
    ).fetchone()[0]
    return {
        "accepted": True,
        "added": bool(added),
        "rounds": int(total),
        "campaign_id": campaign_id,
        "campaign_status": "running",
        "ends_at_utc": deadline.isoformat(),
    }


def archive_response(
    connection: sqlite3.Connection,
    campaign_id: int,
    payload: Any,
    rows_seen: int,
    rows_added: int,
) -> None:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    observed = utc_now()
    connection.execute(
        """
        INSERT INTO raw_responses
        (campaign_id, observed_at_utc, sha256, byte_size, payload_zlib, rows_seen, rows_added)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            campaign_id,
            observed,
            hashlib.sha256(raw).hexdigest(),
            len(raw),
            zlib.compress(raw, level=6),
            rows_seen,
            rows_added,
        ),
    )
    connection.execute(
        """
        UPDATE campaigns SET polls=polls+1, successful_polls=successful_polls+1,
        last_poll_at_utc=?, last_success_at_utc=?, last_error=NULL WHERE id=?
        """,
        (observed, observed, campaign_id),
    )
    connection.commit()


def record_failure(connection: sqlite3.Connection, campaign_id: int, error: Exception) -> None:
    observed = utc_now()
    connection.execute(
        """
        UPDATE campaigns SET polls=polls+1, failure_count=failure_count+1,
        last_poll_at_utc=?, last_error=? WHERE id=?
        """,
        (observed, str(error)[:1000], campaign_id),
    )
    connection.commit()


def _collect(
    config_path: Path, db_path: Path, once: bool, duration_days: float
) -> None:
    if duration_days <= 0:
        raise ValueError("duration_days doit être positif")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    interval = max(2.0, float(config.get("poll_interval_seconds", 5)))
    source = config.get("source", config["url"])
    connection = connect(db_path)
    campaign_id, deadline = open_campaign(connection, source, duration_days)
    failures = 0
    while True:
        if dt.datetime.now(UTC) >= deadline:
            connection.execute(
                "UPDATE campaigns SET status='completed', completed_at_utc=? WHERE id=?",
                (utc_now(), campaign_id),
            )
            connection.commit()
            print(f"{utc_now()} campagne={campaign_id} terminée après {duration_days:g} jours", flush=True)
            return
        try:
            payload = fetch_json(config)
            items = get_path(payload, config.get("items_path", ""))
            if not isinstance(items, list):
                raise TypeError("items_path doit désigner une liste JSON")
            rows = [normalized_round(item, config, source) for item in items]
            added = insert_rows(connection, source, rows)
            archive_response(connection, campaign_id, payload, len(rows), added)
            failures = 0
            remaining = max(0, (deadline - dt.datetime.now(UTC)).total_seconds())
            print(
                f"{utc_now()} campagne={campaign_id} reçues={len(rows)} nouvelles={added} "
                f"reste={remaining / 86400:.2f}j",
                flush=True,
            )
        except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
            failures += 1
            record_failure(connection, campaign_id, exc)
            delay = min(300.0, interval * (2 ** min(failures, 6)))
            print(f"{utc_now()} erreur={exc} prochain_essai={delay:.0f}s", file=sys.stderr, flush=True)
            if once:
                raise
            time.sleep(delay)
            continue
        if once:
            return
        time.sleep(interval)


def collect(
    config_path: Path, db_path: Path, once: bool, duration_days: float
) -> None:
    pid_path = db_path.with_suffix(db_path.suffix + ".collector.pid")
    if pid_path.exists():
        try:
            existing_pid = int(pid_path.read_text().strip())
            os.kill(existing_pid, 0)
        except (ValueError, OSError):
            pid_path.unlink(missing_ok=True)
        else:
            raise RuntimeError(f"Une collecte est déjà active (PID {existing_pid}).")
    pid_path.write_text(str(os.getpid()), encoding="ascii")
    try:
        _collect(config_path, db_path, once, duration_days)
    finally:
        pid_path.unlink(missing_ok=True)


def import_csv(csv_path: Path, db_path: Path, source: str) -> None:
    connection = connect(db_path)
    rows = []
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        for line_number, item in enumerate(csv.DictReader(handle), start=2):
            try:
                value = float(item["multiplier"])
                occurred = item.get("timestamp") or None
                rid = item.get("round_id") or hashlib.sha256(
                    f"{source}|{occurred}|{value}|{line_number}".encode()
                ).hexdigest()
                rows.append((rid, occurred, value, json.dumps(item, ensure_ascii=False)))
            except (KeyError, ValueError) as exc:
                raise ValueError(f"Ligne CSV {line_number}: {exc}") from exc
    print(f"Nouvelles lignes: {insert_rows(connection, source, rows)}")


def percentile(values: list[float], probability: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    position = probability * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def correlation(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 3:
        return math.nan
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    numerator = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    denominator = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return numerator / denominator if denominator else 0.0


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return math.nan, math.nan
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return center - margin, center + margin


def evaluate_threshold(values: list[float], threshold: float, lags: int = 5) -> dict[str, Any]:
    labels = [value >= threshold for value in values]
    split = max(lags + 1, int(len(labels) * 0.8))
    train, test = labels[:split], labels[split:]
    baseline_label = sum(train) >= len(train) / 2 if train else False
    baseline_accuracy = (
        sum(label == baseline_label for label in test) / len(test) if test else math.nan
    )

    # Modèle volontairement simple: taux historique pour chacun des 2^lags états.
    state_counts: dict[tuple[bool, ...], list[int]] = {}
    for index in range(lags, split):
        state = tuple(labels[index - lags : index])
        bucket = state_counts.setdefault(state, [0, 0])
        bucket[1] += 1
        bucket[0] += int(labels[index])
    global_rate = sum(train) / len(train) if train else 0.0
    predictions = []
    for index in range(split, len(labels)):
        state = tuple(labels[index - lags : index])
        successes, total = state_counts.get(state, [0, 0])
        probability = (successes + global_rate * 10) / (total + 10)
        predictions.append(probability >= 0.5)
    model_accuracy = (
        sum(prediction == actual for prediction, actual in zip(predictions, test)) / len(test)
        if test
        else math.nan
    )
    return {
        "test_rounds": len(test),
        "baseline_accuracy": baseline_accuracy,
        "lag_state_accuracy": model_accuracy,
        "improvement": model_accuracy - baseline_accuracy if test else math.nan,
    }


def analyze(db_path: Path, output: Path, assumed_rtp: float) -> None:
    connection = connect(db_path)
    records = connection.execute(
        "SELECT multiplier FROM rounds ORDER BY COALESCE(occurred_at_utc, observed_at_utc), id"
    ).fetchall()
    values = [float(record[0]) for record in records]
    if len(values) < 100:
        raise RuntimeError("Au moins 100 manches sont nécessaires pour une première analyse.")

    thresholds = [1.01, 1.2, 1.5, 2, 3, 5, 10, 20, 50, 100]
    rates = []
    for threshold in thresholds:
        successes = sum(value >= threshold for value in values)
        low, high = wilson(successes, len(values))
        rates.append(
            {
                "threshold": threshold,
                "observed": successes / len(values),
                "ci95": [low, high],
                "reference_rtp_over_x": min(1.0, assumed_rtp / threshold),
            }
        )

    log_values = [math.log(value) for value in values]
    autocorrelations = {
        str(lag): correlation(log_values[:-lag], log_values[lag:])
        for lag in range(1, min(51, len(values) // 4))
    }
    evaluations = {str(t): evaluate_threshold(values, t) for t in [1.5, 2, 3, 5, 10]}
    report = {
        "generated_at_utc": utc_now(),
        "rounds": len(values),
        "summary": {
            "minimum": min(values),
            "median": statistics.median(values),
            "mean": statistics.fmean(values),
            "p90": percentile(values, 0.90),
            "p95": percentile(values, 0.95),
            "p99": percentile(values, 0.99),
            "maximum": max(values),
        },
        "survival_rates": rates,
        "log_multiplier_autocorrelation": autocorrelations,
        "chronological_holdout": evaluations,
        "interpretation": (
            "Un modèle n'est intéressant que si son amélioration hors-échantillon est positive, "
            "stable sur plusieurs fenêtres temporelles et supérieure à l'incertitude statistique."
        ),
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Rapport écrit dans {output}")


def dashboard_payload(db_path: Path, started_at: float) -> dict[str, Any]:
    connection = connect(db_path)
    records = connection.execute(
        """
        SELECT round_id, COALESCE(occurred_at_utc, observed_at_utc), multiplier, source
        FROM rounds
        ORDER BY COALESCE(occurred_at_utc, observed_at_utc) ASC, id ASC
        """
    ).fetchall()
    values = [float(row[2]) for row in records]
    thresholds = [1.5, 2, 3, 5, 10]
    survival = [
        {
            "threshold": threshold,
            "rate": sum(value >= threshold for value in values) / len(values) if values else 0,
            "reference": min(1.0, 0.97 / threshold),
        }
        for threshold in thresholds
    ]
    ranges = [(1, 1.2), (1.2, 1.5), (1.5, 2), (2, 3), (3, 5), (5, 10), (10, 100), (100, math.inf)]
    histogram = [
        {
            "label": f"{low:g}–{high:g}×" if math.isfinite(high) else "100×+",
            "count": sum(low <= value < high for value in values),
        }
        for low, high in ranges
    ]
    log_values = [math.log(value) for value in values]
    correlations = [
        {
            "lag": lag,
            "value": correlation(log_values[:-lag], log_values[lag:]),
        }
        for lag in range(1, min(13, max(1, len(values) // 4)))
    ]
    evaluations = (
        [{"threshold": threshold, **evaluate_threshold(values, threshold)} for threshold in thresholds]
        if len(values) >= 100
        else []
    )
    latest = [
        {"round_id": row[0], "timestamp": row[1], "multiplier": row[2], "source": row[3]}
        for row in reversed(records[-30:])
    ]
    hourly: dict[str, int] = {}
    for row in records:
        label = str(row[1])[:13] + ":00"
        hourly[label] = hourly.get(label, 0) + 1
    campaign_row = connection.execute(
        """
        SELECT id, source, status, duration_days, started_at_utc, ends_at_utc,
               completed_at_utc, polls, successful_polls, failure_count,
               last_poll_at_utc, last_success_at_utc, last_error
        FROM campaigns ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    campaign = None
    if campaign_row:
        raw_stats = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(byte_size), 0) FROM raw_responses WHERE campaign_id=?",
            (campaign_row[0],),
        ).fetchone()
        response_times = [
            parse_utc(row[0])
            for row in connection.execute(
                "SELECT observed_at_utc FROM raw_responses WHERE campaign_id=? ORDER BY observed_at_utc",
                (campaign_row[0],),
            ).fetchall()
        ]
        max_gap_seconds = max(
            ((right - left).total_seconds() for left, right in zip(response_times, response_times[1:])),
            default=0,
        )
        started = parse_utc(campaign_row[4])
        ends = parse_utc(campaign_row[5])
        now = dt.datetime.now(UTC)
        total_seconds = max(1.0, (ends - started).total_seconds())
        elapsed_seconds = min(total_seconds, max(0.0, (now - started).total_seconds()))
        campaign = {
            "id": campaign_row[0],
            "source": campaign_row[1],
            "status": campaign_row[2],
            "duration_days": campaign_row[3],
            "started_at": campaign_row[4],
            "ends_at": campaign_row[5],
            "completed_at": campaign_row[6],
            "polls": campaign_row[7],
            "successful_polls": campaign_row[8],
            "failure_count": campaign_row[9],
            "last_poll_at": campaign_row[10],
            "last_success_at": campaign_row[11],
            "last_error": campaign_row[12],
            "progress": elapsed_seconds / total_seconds,
            "remaining_seconds": max(0, (ends - now).total_seconds()),
            "raw_snapshots": raw_stats[0],
            "raw_bytes": raw_stats[1],
            "success_rate": campaign_row[8] / campaign_row[7] if campaign_row[7] else 0,
            "max_gap_seconds": max_gap_seconds,
        }
    return {
        "generated_at": utc_now(),
        "uptime_seconds": int(time.time() - started_at),
        "database": str(db_path.resolve()),
        "deployment_mode": os.environ.get("AVIATOR_DEPLOYMENT_MODE", "local"),
        "rounds": len(values),
        "first_round_at": records[0][1] if records else None,
        "last_round_at": records[-1][1] if records else None,
        "summary": {
            "median": statistics.median(values) if values else None,
            "mean": statistics.fmean(values) if values else None,
            "maximum": max(values) if values else None,
            "above_2x": sum(value >= 2 for value in values) / len(values) if values else 0,
        },
        "survival": survival,
        "histogram": histogram,
        "autocorrelation": correlations,
        "evaluations": evaluations,
        "hourly": [{"hour": key, "count": count} for key, count in sorted(hourly.items())[-24:]],
        "latest": latest,
        "campaign": campaign,
    }


def serve_dashboard(db_path: Path, host: str, port: int) -> None:
    started_at = time.time()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.rstrip("/") not in {"", "/api/dashboard", "/health"}:
                self.send_error(404)
                return
            if self.path.rstrip("/") == "/health":
                payload = {"status": "ok", "time": utc_now()}
            elif self.path.rstrip("/") == "":
                payload = {
                    "message": "API Aviator Audit active",
                    "dashboard": "http://127.0.0.1:3000",
                    "data": f"http://{host}:{port}/api/dashboard",
                }
            else:
                payload = dashboard_payload(db_path, started_at)
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            print(f"{utc_now()} dashboard {format % args}")

    server = http.server.ThreadingHTTPServer((host, port), Handler)
    print(f"API du dashboard: http://{host}:{port}/api/dashboard", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--db", type=Path, default=Path("aviator.sqlite3"))
    commands = root.add_subparsers(dest="command", required=True)

    collect_parser = commands.add_parser("collect", help="Interroger une source JSON autorisée")
    collect_parser.add_argument("--config", type=Path, required=True)
    collect_parser.add_argument("--once", action="store_true")
    collect_parser.add_argument("--duration-days", type=float, default=20)

    csv_parser = commands.add_parser("import-csv", help="Importer round_id,timestamp,multiplier")
    csv_parser.add_argument("csv", type=Path)
    csv_parser.add_argument("--source", default="csv-import")

    analysis_parser = commands.add_parser("analyze", help="Créer un rapport statistique JSON")
    analysis_parser.add_argument("--output", type=Path, default=Path("report.json"))
    analysis_parser.add_argument("--assumed-rtp", type=float, default=0.97)
    serve_parser = commands.add_parser("serve", help="Servir les données du tableau de bord local")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    return root


def main() -> None:
    args = parser().parse_args()
    if args.command == "collect":
        collect(args.config, args.db, args.once, args.duration_days)
    elif args.command == "import-csv":
        import_csv(args.csv, args.db, args.source)
    elif args.command == "analyze":
        analyze(args.db, args.output, args.assumed_rtp)
    elif args.command == "serve":
        serve_dashboard(args.db, args.host, args.port)


if __name__ == "__main__":
    main()
