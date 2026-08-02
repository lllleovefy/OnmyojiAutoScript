from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from module.duel_data.recommendation import DuelDataCandidateProvider
from module.duel_data.repository import DuelRepository


class DuelDataCandidateProviderTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = DuelRepository(Path(self.temp_dir.name) / "duel.sqlite3")
        self.account_id, _ = self.repository.upsert_account({"id": "main", "name": "main"})
        self.provider = DuelDataCandidateProvider(self.repository)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _seed_history(self, count: int):
        for index in range(count):
            candidate = 30 if index < 12 else 40
            if candidate == 30:
                result = "win" if index < 8 else "loss"
            else:
                result = "win" if index < 14 else "loss"
            self.repository.upsert_match(
                self.account_id,
                {
                    "source_record_id": f"history-{index}",
                    "started_at": f"2026-07-14T00:{index:02d}:00+00:00",
                    "self_ban": 255,
                    "opponent_ban": 256,
                    "picks": [
                        {"side": "self", "round": 1, "shishen_id": 10, "count": 1},
                        {"side": "self", "round": 2, "shishen_id": candidate, "count": 1},
                        {"side": "opponent", "round": 1, "shishen_id": 20, "count": 1},
                    ],
                    "result": result,
                    "valid": True,
                    "practice_mode": False,
                    "raw": {},
                },
            )

    def test_strategy_rule_schema_and_unavailable_filter(self):
        self.repository.upsert_strategy(
            {
                "id": "rule-1",
                "name": "counter",
                "content": {
                    "rules": [
                        {
                            "own_picks": [10],
                            "opponent_picks": [20],
                            "bans": [255, 256],
                            "recommended_shishen_ids": [30, 255],
                            "priority": 3,
                            "confidence": 0.96,
                            "reason": "fixture rule",
                        }
                    ]
                },
            }
        )
        candidates = self.provider.rule_candidates(
            own_picks=[10], opponent_picks=[20], bans=[256, 255]
        )
        self.assertEqual(["30"], [item["shikigami_id"] for item in candidates])
        self.assertEqual("rule", candidates[0]["source"])
        self.assertEqual(3, candidates[0]["priority"])
        self.assertEqual(0.96, candidates[0]["confidence"])

    def test_strategy_allows_opponent_mirror_pick(self):
        self.repository.upsert_strategy(
            {
                "id": "mirror-rule",
                "name": "mirror",
                "content": {
                    "rules": [
                        {
                            "opponent_picks": [20],
                            "recommended_shishen_ids": [20],
                        }
                    ]
                },
            }
        )
        candidates = self.provider.rule_candidates(opponent_picks=[20])
        self.assertEqual(["20"], [item["shikigami_id"] for item in candidates])

    def test_personal_prefix_and_ban_condition_requires_twenty_samples(self):
        self._seed_history(20)
        candidates = self.provider.personal_candidates(
            own_picks=[10], opponent_picks=[20], bans=[255, 256]
        )
        self.assertEqual(["30", "40"], [item["shikigami_id"] for item in candidates])
        self.assertAlmostEqual(8 / 12, candidates[0]["score"])
        self.assertEqual(12, candidates[0]["sample_size"])
        self.assertEqual(8, candidates[1]["sample_size"])
        self.assertEqual("personal", candidates[0]["source"])
        self.assertEqual(
            [],
            self.provider.personal_candidates(
                own_picks=[10], opponent_picks=[20], bans=[255]
            ),
        )

    def test_personal_confidence_uses_each_candidates_own_sample_count(self):
        for index in range(36):
            candidate = 999 if index == 35 else 30
            self.repository.upsert_match(
                self.account_id,
                {
                    "source_record_id": f"skewed-history-{index}",
                    "started_at": f"2026-07-14T01:{index:02d}:00+00:00",
                    "self_ban": 255,
                    "opponent_ban": 256,
                    "picks": [
                        {"side": "self", "round": 1, "shishen_id": 10, "count": 1},
                        {"side": "self", "round": 2, "shishen_id": candidate, "count": 1},
                        {"side": "opponent", "round": 1, "shishen_id": 20, "count": 1},
                    ],
                    "result": "win",
                    "valid": True,
                    "practice_mode": False,
                    "raw": {},
                },
            )

        candidates = {
            item["shikigami_id"]: item
            for item in self.provider.personal_candidates(
                own_picks=[10], opponent_picks=[20], bans=[255, 256]
            )
        }

        self.assertEqual(35, candidates["30"]["sample_size"])
        self.assertAlmostEqual(0.975, candidates["30"]["confidence"])
        self.assertEqual(1, candidates["999"]["sample_size"])
        self.assertEqual(0.90, candidates["999"]["confidence"])
        self.assertLess(candidates["999"]["confidence"], 0.98)

    def test_personal_history_below_threshold_uses_bayesian_global_fallback(self):
        self._seed_history(19)
        candidates = self.provider.personal_candidates(
            own_picks=[10], opponent_picks=[20], bans=[255, 256]
        )
        self.assertEqual(
            {"round_global"},
            {item["context_level"] for item in candidates},
        )
        candidate_30 = next(
            item for item in candidates if item["shikigami_id"] == "30"
        )
        self.assertAlmostEqual((8 + 1) / (12 + 2), candidate_30["score"])
        self.assertEqual(19, candidate_30["context_sample_size"])

    def test_first_pick_without_context_uses_bayesian_global_fallback(self):
        for index in range(20):
            candidate = 255 if index == 0 else 393
            self.repository.upsert_match(
                self.account_id,
                {
                    "source_record_id": f"first-pick-history-{index}",
                    "started_at": f"2026-07-14T02:{index:02d}:00+00:00",
                    "picks": [
                        {
                            "side": "self",
                            "round": 1,
                            "shishen_id": candidate,
                            "count": 1,
                        },
                        {
                            "side": "opponent",
                            "round": 1,
                            "shishen_id": 20,
                            "count": 1,
                        },
                    ],
                    "result": "win" if index < 16 else "loss",
                    "valid": True,
                    "practice_mode": False,
                    "raw": {},
                },
            )

        layers = self.provider.personal_candidate_layers()
        candidates = self.provider.personal_candidates()

        self.assertEqual([], layers["exact_history"])
        self.assertEqual(
            {"round_global"},
            {item["context_level"] for item in candidates},
        )
        self.assertEqual("393", candidates[0]["shikigami_id"])
        self.assertAlmostEqual((15 + 1) / (19 + 2), candidates[0]["score"])
        candidate_255 = next(
            item for item in candidates if item["shikigami_id"] == "255"
        )
        self.assertAlmostEqual((1 + 1) / (1 + 2), candidate_255["score"])

    def test_own_prefix_without_opponent_is_not_exact_history(self):
        self._seed_history(20)

        layers = self.provider.personal_candidate_layers(own_picks=[10])

        self.assertEqual([], layers["exact_history"])
        self.assertEqual(
            ["30", "40"],
            [item["shikigami_id"] for item in layers["own_prefix"]],
        )

    def test_unknown_opponent_slot_keeps_later_position_for_matching(self):
        for index in range(40):
            later_slot_matches = index < 20
            self.repository.upsert_match(
                self.account_id,
                {
                    "source_record_id": f"positional-history-{index}",
                    "started_at": f"2026-07-15T00:{index:02d}:00+00:00",
                    "picks": [
                        {
                            "side": "self",
                            "round": 1,
                            "shishen_id": 30 if later_slot_matches else 40,
                            "count": 1,
                        },
                        {
                            "side": "opponent",
                            "round": 1,
                            "shishen_id": 77 if later_slot_matches else 20,
                            "count": 1,
                        },
                        {
                            "side": "opponent",
                            "round": 2,
                            "shishen_id": 20 if later_slot_matches else 99,
                            "count": 1,
                        },
                    ],
                    "result": "win",
                    "valid": True,
                    "practice_mode": False,
                    "raw": {},
                },
            )

        layers = self.provider.personal_candidate_layers(
            opponent_picks=[None, 20]
        )
        self.assertEqual([], layers["exact_history"])
        self.assertEqual(
            ["30"],
            [
                item["shikigami_id"]
                for item in layers["opponent_known"]
            ],
        )
        self.assertEqual(
            20, layers["opponent_known"][0]["context_sample_size"]
        )

    def test_grouped_candidates_include_ranked_personal_fallback_layers(self):
        self._seed_history(20)
        grouped = self.provider.get_candidates(
            own_picks=[10],
            opponent_picks=[20],
            bans=[255, 256],
        )
        levels = {item["context_level"] for item in grouped["personal"]}
        self.assertIn("exact_history", levels)
        self.assertIn("opponent_known", levels)
        self.assertIn("own_prefix", levels)
        self.assertIn("round_global", levels)

    def test_external_snapshots_are_first_pick_only_and_never_auto_confident(self):
        self.repository.save_snapshot(
            "assist_first_pick",
            {
                "my_shishens": [
                    {"shishen_id": 101, "total": 100, "win_rate": 60},
                    {"shishen_id": 103, "total": 30, "win_rate": 0.55},
                ]
            },
            recommendation=True,
        )
        self.repository.save_snapshot(
            "ai_first_pick",
            {
                "our_recommendation": [
                    {
                        "raw_id": 102,
                        "expected_win_rate": 0.62,
                        "blended_score": 0.61,
                        "policy_prob": 1.0,
                        "reason": "fixture AI",
                    }
                ]
            },
            recommendation=True,
        )
        candidates = self.provider.external_candidates(bans=[101])
        self.assertEqual(["102", "103"], [item["shikigami_id"] for item in candidates])
        self.assertTrue(all(item["source"] == "external" for item in candidates))
        self.assertTrue(all(item["confidence"] <= 0.97 for item in candidates))
        self.assertEqual([], self.provider.external_candidates(own_picks=[10]))
        self.assertEqual([], self.provider.external_candidates(opponent_picks=[20]))

    def test_group_contract_has_three_candidate_sources(self):
        grouped = self.provider.get_candidates()
        self.assertEqual({"rules", "personal", "external"}, set(grouped))

    def test_no_shikigami_recommendation_after_five_committed_picks(self):
        layers = self.provider.personal_candidate_layers(
            own_picks=[1, 2, 3, 4, 5]
        )
        self.assertTrue(all(not candidates for candidates in layers.values()))


if __name__ == "__main__":
    unittest.main()
