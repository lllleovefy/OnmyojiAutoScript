from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from module.duel_data.models import DuelMatchPatch, DuelPick
from module.duel_data.repository import DuelRepository


class DuelRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = DuelRepository(Path(self.temp_dir.name) / "duel.sqlite3")
        self.account_id, created = self.repository.upsert_account(
            {"id": "account-1", "name": "main", "latest_score": 2800}
        )
        self.assertTrue(created)

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def match_payload(record_id: str = "record-1"):
        return {
            "source_record_id": record_id,
            "started_at": "2026-07-14T10:00:00+00:00",
            "score": 2800,
            "star": 50,
            "self_ban": 255,
            "opponent_ban": 256,
            "picks": [
                {"side": "self", "round": 1, "shishen_id": 101, "count": 1},
                {"side": "opponent", "round": 1, "shishen_id": 201, "count": 1},
            ],
            "result": "win",
            "duration": 92.5,
            "valid": True,
            "practice_mode": False,
            "raw": {
                "id": record_id,
                "authorized-token": "secret-cookie-value",
                "nested": {"Authorization": "Bearer secret-access-token"},
            },
        }

    def test_match_upsert_is_idempotent_and_credentials_are_not_stored(self):
        match_id, created = self.repository.upsert_match(self.account_id, self.match_payload())
        self.assertTrue(created)
        duplicate_id, duplicate_created = self.repository.upsert_match(self.account_id, self.match_payload())
        self.assertEqual(match_id, duplicate_id)
        self.assertFalse(duplicate_created)

        result = self.repository.list_matches(page=1, page_size=20)
        self.assertEqual(1, result.total)
        self.assertEqual("win", result.items[0].result)
        self.assertEqual("oas", result.items[0].source)
        self.assertEqual(2, len(result.items[0].picks))
        database_text = self.repository.database_path.read_bytes().decode("utf-8", errors="ignore")
        self.assertNotIn("secret-cookie-value", database_text)
        self.assertNotIn("secret-access-token", database_text)
        self.assertNotIn("authorized-token", database_text)
        self.assertNotIn("Authorization", database_text)

    def test_local_defaults_do_not_rewrite_legacy_source_rows(self):
        legacy_account_id, created = self.repository.upsert_account(
            {"id": "account-1", "name": "imported"},
            source="yysrank",
        )
        self.assertTrue(created)
        self.assertNotEqual(self.account_id, legacy_account_id)
        connection = sqlite3.connect(self.repository.database_path)
        try:
            sources = {
                row[0]
                for row in connection.execute(
                    "SELECT source FROM duel_accounts WHERE source_account_id='account-1'"
                )
            }
            import_run_table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='duel_import_runs'"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual({"oas", "yysrank"}, sources)
        self.assertEqual(("duel_import_runs",), import_run_table)

    def test_patch_and_summary(self):
        match_id, _ = self.repository.upsert_match(self.account_id, self.match_payload())
        updated = self.repository.patch_match(
            match_id,
            DuelMatchPatch(
                result="loss",
                valid=False,
                picks=[DuelPick(side="self", round=1, shishen_id=303, count=1)],
            ),
        )
        self.assertIsNotNone(updated)
        self.assertEqual("loss", updated.result)
        self.assertFalse(updated.valid)
        self.assertEqual(303, updated.picks[0].shishen_id)

        summary = self.repository.summary()
        self.assertEqual(1, summary.total)
        self.assertEqual(0, summary.valid)
        self.assertEqual(0, summary.losses)
        self.assertEqual(0.0, summary.win_rate)

    def test_patch_time_is_validated_and_normalized_to_utc(self):
        patch = DuelMatchPatch(started_at="2026-07-14T12:30:00+08:00")
        self.assertEqual("2026-07-14T04:30:00+00:00", patch.started_at)
        with self.assertRaises(ValueError):
            DuelMatchPatch(started_at="not-a-time")

    def test_top_pick_counts_slots_not_repeat_ordinals(self):
        payload = self.match_payload("record-repeated")
        payload["picks"] = [
            {"side": "self", "round": 1, "shishen_id": 101, "count": 1},
            {"side": "self", "round": 2, "shishen_id": 101, "count": 2},
            {
                "side": "opponent",
                "round": 1,
                "shishen_id": 201,
                "count": 1,
            },
        ]
        self.repository.upsert_match(self.account_id, payload)
        top_pick = next(
            item
            for item in self.repository.summary().top_picks
            if item.shishen_id == 101
        )
        self.assertEqual(2, top_pick.count)
        self.assertEqual(2, top_pick.wins)
        self.assertEqual(1.0, top_pick.win_rate)

    def test_version_two_migration_normalizes_existing_timestamps(self):
        match_id, _ = self.repository.upsert_match(
            self.account_id, self.match_payload()
        )
        connection = sqlite3.connect(self.repository.database_path)
        connection.execute(
            "UPDATE duel_matches SET started_at=? WHERE id=?",
            ("2026-07-14T12:30:00+08:00", match_id),
        )
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        connection.close()

        migrated = DuelRepository(self.repository.database_path)
        self.assertEqual(
            "2026-07-14T04:30:00+00:00",
            migrated.get_match(match_id).started_at,
        )

    def test_snapshot_and_strategy_are_idempotent(self):
        strategy_id, created = self.repository.upsert_strategy(
            {"id": "strategy-1", "name": "first pick", "content": {"pick": 101}}
        )
        same_id, same_created = self.repository.upsert_strategy(
            {"id": "strategy-1", "name": "updated", "content": {"pick": 102}}
        )
        self.assertEqual(strategy_id, same_id)
        self.assertTrue(created)
        self.assertFalse(same_created)
        anonymous_id, anonymous_created = self.repository.upsert_strategy(
            {"name": "anonymous", "content": {"pick": 103}}
        )
        repeated_anonymous_id, repeated_anonymous_created = self.repository.upsert_strategy(
            {"name": "anonymous", "content": {"pick": 103}}
        )
        self.assertTrue(anonymous_created)
        self.assertFalse(repeated_anonymous_created)
        self.assertEqual(anonymous_id, repeated_anonymous_id)
        self.assertEqual("oas", self.repository.list_strategies()[0].source)
        self.assertTrue(self.repository.save_snapshot("shishen_assets", [{"id": 101}]))
        self.assertFalse(self.repository.save_snapshot("shishen_assets", [{"id": 101}]))


if __name__ == "__main__":
    unittest.main()
