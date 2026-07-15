import json
import os
import random
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import aviator_audit as audit
import render_start


class AuditTests(unittest.TestCase):
    def test_path_reader(self):
        payload = {"data": {"rounds": [{"value": 2.5}]}}
        self.assertEqual(audit.get_path(payload, "data.rounds.0.value"), 2.5)

    def test_deduplication_and_analysis(self):
        rng = random.Random(42)
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "rounds.sqlite3"
            connection = audit.connect(db)
            rows = []
            for index in range(2000):
                # Distribution crash théorique simplifiée: P(M >= x) ~= 0,97/x.
                multiplier = max(1.0, 0.97 / max(rng.random(), 1e-9))
                rows.append((str(index), None, multiplier, "{}"))
            self.assertEqual(audit.insert_rows(connection, "test", rows), 2000)
            self.assertEqual(audit.insert_rows(connection, "test", rows), 0)
            report = Path(directory) / "report.json"
            audit.analyze(db, report, 0.97)
            parsed = json.loads(report.read_text())
            self.assertEqual(parsed["rounds"], 2000)
            self.assertIn("2", parsed["chronological_holdout"])

    def test_twenty_day_campaign_archives_complete_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "campaign.sqlite3"
            connection = audit.connect(db)
            campaign_id, deadline = audit.open_campaign(connection, "test-source", 20)
            audit.archive_response(
                connection,
                campaign_id,
                {"data": {"rounds": [{"id": "r1", "multiplier": 2.4}]}},
                rows_seen=1,
                rows_added=1,
            )
            with patch.dict(os.environ, {"AVIATOR_DEPLOYMENT_MODE": "render-free"}):
                payload = audit.dashboard_payload(db, 0)
            self.assertEqual(payload["campaign"]["duration_days"], 20)
            self.assertEqual(payload["deployment_mode"], "render-free")
            self.assertEqual(payload["campaign"]["raw_snapshots"], 1)
            self.assertEqual(payload["campaign"]["successful_polls"], 1)
            self.assertGreater((deadline - audit.dt.datetime.now(audit.UTC)).days, 18)

    def test_render_configuration_comes_from_environment(self):
        environment = {
            "AVIATOR_SOURCE_URL": "https://example.test/history",
            "AVIATOR_SOURCE_NAME": "authorized-test-source",
            "AVIATOR_ITEMS_PATH": "payload.rounds",
            "AVIATOR_MULTIPLIER_PATH": "result.multiplier",
            "AVIATOR_HEADERS_JSON": '{"X-Test":"yes"}',
        }
        with patch.dict(os.environ, environment, clear=True):
            path = render_start.collector_config()
            self.assertIsNotNone(path)
            config = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(config["url"], environment["AVIATOR_SOURCE_URL"])
        self.assertEqual(config["source"], "authorized-test-source")
        self.assertEqual(config["items_path"], "payload.rounds")
        self.assertEqual(config["multiplier_path"], "result.multiplier")
        self.assertEqual(config["headers"]["X-Test"], "yes")

    def test_relay_ingestion_starts_once_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "relay.sqlite3"
            payload = {
                "round_id": "pb-12345678-abcd",
                "multiplier": 2.75,
                "observed_at_utc": audit.utc_now(),
                "collector_id": "local-test",
                "frame_host": "aviator-next.spribegaming.com",
                "history_size": 12,
            }
            first = audit.ingest_relay_round(db, payload)
            duplicate = audit.ingest_relay_round(db, payload)
            dashboard = audit.dashboard_payload(db, 0)
            self.assertTrue(first["accepted"])
            self.assertTrue(first["added"])
            self.assertTrue(duplicate["accepted"])
            self.assertFalse(duplicate["added"])
            self.assertEqual(dashboard["rounds"], 1)
            self.assertEqual(dashboard["campaign"]["duration_days"], 20)
            self.assertEqual(dashboard["campaign"]["raw_snapshots"], 2)

    def test_relay_rejects_invalid_or_finished_campaign(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "relay.sqlite3"
            with self.assertRaises(ValueError):
                audit.ingest_relay_round(db, {"round_id": "bad", "multiplier": 0.5})
            payload = {
                "round_id": "pb-finished-0001",
                "multiplier": 1.25,
                "observed_at_utc": audit.utc_now(),
            }
            first = audit.ingest_relay_round(db, payload)
            connection = audit.connect(db)
            connection.execute(
                "UPDATE campaigns SET status='completed', completed_at_utc=? WHERE id=?",
                (audit.utc_now(), first["campaign_id"]),
            )
            connection.commit()
            later = audit.ingest_relay_round(
                db,
                {**payload, "round_id": "pb-finished-0002"},
            )
            self.assertFalse(later["accepted"])
            self.assertEqual(later["campaign_id"], first["campaign_id"])

    def test_relay_http_endpoint_requires_secret(self):
        class Frontend:
            @staticmethod
            def poll():
                return None

        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "relay.sqlite3"
            audit.connect(db).close()
            handler = render_start.make_handler(db, Frontend(), None, False, True)
            server = render_start.http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}/api/ingest"
            body = json.dumps(
                {
                    "round_id": "pb-http-test-0001",
                    "multiplier": 3.2,
                    "observed_at_utc": audit.utc_now(),
                }
            ).encode()
            try:
                with patch.dict(os.environ, {"AVIATOR_INGEST_TOKEN": "test-secret"}):
                    with self.assertRaises(urllib.error.HTTPError) as unauthorized:
                        urllib.request.urlopen(
                            urllib.request.Request(
                                url,
                                data=body,
                                method="POST",
                                headers={"Content-Type": "application/json"},
                            )
                        )
                    self.assertEqual(unauthorized.exception.code, 401)
                    request = urllib.request.Request(
                        url,
                        data=body,
                        method="POST",
                        headers={
                            "Authorization": "Bearer test-secret",
                            "Content-Type": "application/json",
                        },
                    )
                    with urllib.request.urlopen(request) as response:
                        result = json.load(response)
                    self.assertTrue(result["accepted"])
                    self.assertTrue(result["added"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_relay_status_records_only_safe_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "relay.sqlite3"
            saved = audit.record_relay_status(
                db, "history-detected", "aviator-next.spribegaming.com"
            )
            self.assertEqual(saved["stage"], "history-detected")
            self.assertEqual(audit.relay_status(db)["frame_host"], saved["frame_host"])
            with self.assertRaises(ValueError):
                audit.record_relay_status(db, "history-detected", "host/path?token=secret")


if __name__ == "__main__":
    unittest.main()
