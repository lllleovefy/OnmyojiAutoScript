from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import tempfile
import unittest
from pathlib import Path

from module.duel_data.models import (
    DuelMatchPatch,
    DuelPick,
    DuelPortraitTemplateInput,
)
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

    def test_portrait_templates_are_idempotent_and_labels_are_not_erased(self):
        template, created = self.repository.upsert_portrait_template(
            DuelPortraitTemplateInput(
                path="log/duel/portraits/unresolved.png",
                view="top",
                hash="0123456789abcdef",
                source="video",
                confidence=0.2,
            )
        )
        self.assertTrue(created)
        self.assertEqual("unresolved", template.status)

        labeled = self.repository.label_portrait_template(
            template.id,
            shishen_id=596,
            name="神无月",
            path="596-神无月/596-神无月-上阵-001.png",
        )
        self.assertEqual("recognized", labeled.status)
        self.assertEqual(596, labeled.shishen_id)

        duplicate, duplicate_created = self.repository.upsert_portrait_template(
            {
                "path": "log/duel/portraits/reimported.png",
                "view": "top",
                "hash": "0123456789abcdef",
                "source": "capture",
                "confidence": 0.5,
            }
        )
        self.assertFalse(duplicate_created)
        self.assertEqual(template.id, duplicate.id)
        self.assertEqual(596, duplicate.shishen_id)
        self.assertEqual("神无月", duplicate.name)
        self.assertEqual(
            "596-神无月/596-神无月-上阵-001.png",
            duplicate.path,
        )
        self.assertEqual(1, self.repository.portrait_status().recognized)
        self.assertEqual(0, self.repository.list_unresolved_portrait_templates().total)

    def test_portrait_upsert_is_atomic_across_repository_instances(self):
        database_path = self.repository.database_path
        payload = {
            "path": "log/duel/portraits/concurrent.png",
            "view": "candidate",
            "hash": "concurrent-hash",
            "source": "capture",
            "confidence": 0.8,
        }

        def upsert(_):
            return DuelRepository(database_path).upsert_portrait_template(
                payload
            )

        with ThreadPoolExecutor(max_workers=16) as executor:
            results = list(executor.map(upsert, range(32)))

        self.assertEqual(1, sum(created for _, created in results))
        self.assertEqual(1, len({template.id for template, _ in results}))
        connection = sqlite3.connect(database_path)
        try:
            count = connection.execute(
                """SELECT COUNT(*) FROM duel_portrait_templates
                   WHERE view=? AND hash=?""",
                ("candidate", "concurrent-hash"),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(1, count)

    def test_portrait_status_and_unresolved_view_filter(self):
        self.repository.upsert_portrait_template(
            {
                "path": "a.png",
                "view": "top",
                "hash": "aaaaaaaa",
                "source": "video",
                "confidence": 0,
            }
        )
        self.repository.upsert_portrait_template(
            {
                "path": "b.png",
                "shishen_id": 101,
                "name": "fixture",
                "view": "candidate",
                "hash": "bbbbbbbb",
                "source": "video",
                "confidence": 0.99,
            }
        )
        status = self.repository.portrait_status()
        self.assertEqual((2, 1, 1), (status.total, status.recognized, status.unresolved))
        self.assertEqual({"candidate": 1, "top": 1}, status.by_view)
        self.assertEqual(0, status.asset_id_count)
        self.assertEqual(1, status.covered_id_count)
        self.assertFalse(status.coverage_complete)
        self.assertTrue(status.onmyoji_ready)
        self.assertEqual([], status.missing_onmyoji_templates)
        unresolved = self.repository.list_unresolved_portrait_templates(view="top")
        self.assertEqual(1, unresolved.total)
        self.assertEqual("top", unresolved.items[0].view)

    def test_portrait_status_ignores_legacy_onmyoji_templates(self):
        onmyoji_root = (
            self.repository.database_path.parent
            / "portrait_library"
            / "_onmyoji"
        )
        onmyoji_root.mkdir(parents=True)
        filenames = (
            "seimi-selected.png",
            "kagura-selected.png",
            "hiromasa-selected.png",
            "yao_bikuni-selected.png",
            "yorimitsu-selected.png",
            "michinaga-selected.png",
        )
        for filename in filenames:
            (onmyoji_root / filename).write_bytes(b"not a png")

        status = self.repository.portrait_status()

        self.assertTrue(status.onmyoji_ready)
        self.assertEqual([], status.missing_onmyoji_templates)

    def test_portrait_coverage_counts_only_current_asset_ids(self):
        self.repository.save_snapshot(
            "shishen_assets",
            [
                {"id": 101, "name": "mapped"},
                {"id": 102, "name": "missing"},
            ],
        )
        for identity in (101, 999):
            self.repository.upsert_portrait_template(
                {
                    "path": f"{identity}.png",
                    "shishen_id": identity,
                    "name": str(identity),
                    "view": "candidate",
                    "hash": f"hash-{identity}",
                    "source": "test",
                    "confidence": 1,
                }
            )

        status = self.repository.portrait_status()

        self.assertEqual(2, status.asset_id_count)
        self.assertEqual(1, status.covered_id_count)
        self.assertFalse(status.coverage_complete)


if __name__ == "__main__":
    unittest.main()
