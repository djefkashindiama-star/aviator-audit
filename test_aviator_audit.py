import json
import os
import random
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
