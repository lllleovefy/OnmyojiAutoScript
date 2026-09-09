from __future__ import annotations

import unittest

from dev_tools.duel_bp_label_samples import asset_names, resolve_ocr_text
from tasks.Duel.identity import ShikigamiNameIndex


class DuelBPSampleLabelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assets = [
            {"id": 596, "name": "神无月", "alias": "神元月"},
            {"id": 566, "name": "晨晖惠比寿", "alias": ""},
            {"id": 255, "name": "阎魔", "alias": "阁魔,阅魔"},
        ]
        self.index = ShikigamiNameIndex(self.assets)
        self.names = asset_names(self.assets)

    def resolve(self, text: str, score: float = 0.9):
        return resolve_ocr_text(
            text,
            ocr_score=score,
            index=self.index,
            names=self.names,
        )

    def test_leading_full_level_glyph_does_not_break_resolution(self) -> None:
        resolved, method = self.resolve("网晨晖惠比寿")
        self.assertEqual("566", resolved.shikigami_id)
        self.assertEqual("substring", method)

    def test_ocr_alias_with_skin_suffix_resolves(self) -> None:
        resolved, method = self.resolve("两阁魔京紫", score=0.62)
        self.assertEqual("255", resolved.shikigami_id)
        self.assertEqual(0.62, resolved.confidence)
        self.assertEqual("substring", method)

    def test_unknown_is_not_forced_to_a_label(self) -> None:
        resolved, method = self.resolve("不相狐禅儿")
        self.assertIsNone(resolved)
        self.assertEqual("unresolved", method)

    def test_loose_fuzzy_match_is_rejected(self) -> None:
        resolved, method = self.resolve("晨晖比寿")
        self.assertIsNone(resolved)
        self.assertEqual("unresolved", method)


if __name__ == "__main__":
    unittest.main()
