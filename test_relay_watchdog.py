import datetime as dt
import unittest
from unittest.mock import patch

import relay_watchdog as watchdog


class RelayWatchdogTests(unittest.TestCase):
    def test_seconds_since_accepts_zulu_and_offsets(self):
        now = dt.datetime(2026, 7, 16, 14, 0, tzinfo=watchdog.UTC)
        self.assertEqual(watchdog.seconds_since("2026-07-16T13:55:00Z", now), 300)
        self.assertEqual(
            watchdog.seconds_since("2026-07-16T14:55:00+01:00", now), 300
        )

    def test_latest_round_age_handles_missing_timestamp(self):
        self.assertIsNone(watchdog.latest_round_age({"rounds": 0}))

    def test_game_target_matches_only_the_expected_page(self):
        self.assertTrue(
            watchdog.is_game_target(
                {
                    "type": "page",
                    "url": "https://www.premierbet.com/cd/casino/game/aviator-291195",
                }
            )
        )
        self.assertFalse(
            watchdog.is_game_target(
                {"type": "iframe", "url": "https://aviator-next.spribegaming.com/"}
            )
        )

    def test_tidy_game_targets_closes_only_duplicates_and_new_tab(self):
        targets = [
            {"id": "game-1", "type": "page", "url": watchdog.TARGET_URL},
            {"id": "game-2", "type": "page", "url": watchdog.TARGET_URL},
            {"id": "settings", "type": "page", "url": "chrome://settings/"},
            {"id": "new-tab", "type": "page", "url": "chrome://newtab/"},
        ]
        with patch.object(watchdog, "devtools_targets", return_value=targets), patch.object(
            watchdog, "close_target"
        ) as close:
            self.assertEqual(watchdog.tidy_game_targets(), 2)
        self.assertEqual([call.args[0]["id"] for call in close.call_args_list], ["game-2", "new-tab"])


if __name__ == "__main__":
    unittest.main()
