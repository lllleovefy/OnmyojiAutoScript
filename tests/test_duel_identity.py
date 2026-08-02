from __future__ import annotations

import unittest

from tasks.Duel.identity import (
    DuelIdentityAdapter,
    DuelIdentityObservation,
    ShikigamiNameIndex,
    auto_identity_is_complete,
    parse_identity_regions,
)


class ShikigamiNameIndexTest(unittest.TestCase):
    def setUp(self):
        self.index = ShikigamiNameIndex(
            [
                {"id": 393, "name": "禅心云外镜", "alias": "禅心外镜,和心云外镜"},
                {"id": 575, "name": "封阳君", "alias": ""},
            ]
        )

    def test_exact_alias_and_rarity_prefix_resolve_to_raw_id(self):
        exact = self.index.resolve("SP 禅心云外镜", detector_confidence=0.99)
        alias = self.index.resolve("禅心外镜", detector_confidence=0.95)
        self.assertEqual("393", exact.shikigami_id)
        self.assertEqual("393", alias.shikigami_id)
        self.assertEqual(0.95, alias.confidence)

    def test_fuzzy_resolution_is_bounded_by_detector_confidence(self):
        resolved = self.index.resolve("封阳君君", detector_confidence=0.93)
        self.assertEqual("575", resolved.shikigami_id)
        self.assertLessEqual(resolved.confidence, 0.93)

    def test_unknown_name_is_rejected(self):
        self.assertIsNone(self.index.resolve("完全未知", detector_confidence=1.0))


class DuelIdentityAdapterTest(unittest.TestCase):
    def setUp(self):
        index = ShikigamiNameIndex(
            [
                {"id": 101, "name": "甲", "alias": ""},
                {"id": 102, "name": "乙", "alias": ""},
                {"id": 103, "name": "丙", "alias": ""},
            ]
        )
        names = {1: "甲", 2: "乙", 3: "丙"}
        self.adapter = DuelIdentityAdapter(
            index,
            name_resolver=names.__getitem__,
            regions={
                "self": (0, 0, 600, 150),
                "opponent": (680, 0, 600, 150),
                "self_ban": (0, 160, 600, 100),
                "opponent_ban": (680, 160, 600, 100),
            },
            minimum_confidence=0.90,
        )

    @staticmethod
    def detection(track_id, class_id, confidence, cx, cy):
        return (track_id, class_id, confidence, cx, cy, 50, 50, 0.0)

    def test_orders_both_sides_and_preserves_repeated_shikigami(self):
        result = self.adapter.recognize(
            [
                self.detection(1, 1, 0.99, 520, 50),
                self.detection(2, 1, 0.98, 410, 50),
                self.detection(3, 2, 0.97, 100, 50),
                self.detection(4, 1, 0.99, 700, 50),
                self.detection(5, 3, 0.96, 900, 50),
            ],
            state="SELF_PICK",
        )
        self.assertEqual(("101", "101", "102"), result.own_picks)
        self.assertEqual(("101", "103"), result.opponent_picks)
        self.assertEqual(("102", "101", "101"), result.own_slots)
        self.assertEqual(("101", "103"), result.opponent_slots)
        self.assertEqual(
            ("102", None, None, "101", "101"),
            result.own_visible_slots,
        )
        self.assertEqual(0.96, result.confidence)

    def test_locked_slot_mask_separates_spatial_order_from_pick_order(self):
        result = self.adapter.recognize(
            [
                self.detection(1, 2, 0.99, 100, 50),
                self.detection(2, 1, 0.98, 410, 50),
                self.detection(3, 3, 0.97, 520, 50),
                self.detection(4, 1, 0.99, 700, 50),
                self.detection(5, 3, 0.96, 900, 50),
            ],
            state="SELF_PICK",
            own_locked_slots=2,
            opponent_locked_slots=1,
        )
        self.assertEqual(("101", "103"), result.own_slots)
        self.assertEqual(("103", "101"), result.own_picks)
        self.assertEqual(("101",), result.opponent_slots)
        self.assertEqual(("101",), result.opponent_picks)

    def test_bans_are_split_in_self_opponent_order(self):
        result = self.adapter.recognize(
            [
                self.detection(1, 2, 0.99, 500, 200),
                self.detection(2, 3, 0.98, 700, 200),
            ],
            state="BAN",
        )
        self.assertEqual(("102", "103"), result.bans)

    def test_low_confidence_and_unknown_classes_are_ignored(self):
        result = self.adapter.recognize(
            [
                self.detection(1, 1, 0.89, 500, 50),
                self.detection(2, 999, 1.0, 400, 50),
            ],
            state="SELF_PICK",
        )
        self.assertEqual((), result.own_picks)
        self.assertIsNone(result.confidence)

    def test_region_parser_rejects_invalid_roi(self):
        with self.assertRaises(ValueError):
            parse_identity_regions('{"self":[0,0,-1,100]}')

    def test_default_regions_cover_exact_rows_and_both_bans(self):
        regions = parse_identity_regions(None)
        self.assertEqual((96, 10, 360, 78), regions["self"])
        self.assertEqual((827, 10, 360, 78), regions["opponent"])
        self.assertIn("self_ban", regions)
        self.assertIn("opponent_ban", regions)

    def test_auto_rejects_partial_detection_with_high_box_confidence(self):
        observation = DuelIdentityObservation(
            own_picks=("101",),
            confidence=0.99,
        )
        self.assertFalse(
            auto_identity_is_complete(
                state="SELF_PICK",
                previous_state="SELF_PICK",
                observation=observation,
                previous_own_picks=("101", "102", "103", "104", "105"),
            )
        )

    def test_auto_requires_exact_pick_increment_on_phase_transition(self):
        complete = DuelIdentityObservation(
            own_picks=("101",),
            opponent_picks=("102",),
            confidence=0.99,
        )
        self.assertTrue(
            auto_identity_is_complete(
                state="SELF_PICK",
                previous_state="OPPONENT_PICK",
                observation=complete,
                previous_own_picks=("101",),
            )
        )
        self.assertFalse(
            auto_identity_is_complete(
                state="SELF_PICK",
                previous_state="OPPONENT_PICK",
                observation=DuelIdentityObservation(
                    own_picks=("101",), confidence=0.99
                ),
                previous_own_picks=("101",),
            )
        )
        self.assertFalse(
            auto_identity_is_complete(
                state="OPPONENT_PICK",
                previous_state="SELF_PICK",
                observation=DuelIdentityObservation(
                    own_picks=("999", "102"), confidence=0.99
                ),
                previous_own_picks=("101",),
            )
        )
        self.assertTrue(
            auto_identity_is_complete(
                state="OPPONENT_PICK",
                previous_state="SELF_PICK",
                observation=DuelIdentityObservation(
                    own_picks=("101", "101"), confidence=0.99
                ),
                previous_own_picks=("101",),
            )
        )

    def test_celeb_auto_requires_both_bans_after_ban_phase(self):
        self.assertFalse(
            auto_identity_is_complete(
                state="SELF_PICK",
                previous_state="BAN",
                observation=DuelIdentityObservation(confidence=0.99),
                is_celeb=True,
            )
        )
        self.assertTrue(
            auto_identity_is_complete(
                state="SELF_PICK",
                previous_state="BAN",
                observation=DuelIdentityObservation(
                    bans=("101", "102"), confidence=0.99
                ),
                is_celeb=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
