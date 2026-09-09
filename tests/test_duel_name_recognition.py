from __future__ import annotations

import unittest

import numpy as np

from tasks.Duel.name_recognition import (
    StrictNameRecognizer,
    asset_names,
    normalize_shishen_assets,
    normalize_name,
)


class _FakeOcr:
    def __init__(self, text: str, score: float = 0.96) -> None:
        self.text = text
        self.score = score

    def ocr_single_line(self, image):
        return self.text, self.score

    def detect_and_ocr(self, image, **kwargs):
        return []


class DuelStrictNameRecognitionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assets = [
            {"id": 596, "name": "神无月", "alias": "神元月"},
            {"id": 566, "name": "晨晖惠比寿", "alias": ""},
            {"id": 255, "name": "阎魔", "alias": "阁魔,阅魔"},
        ]
        self.recognizer = StrictNameRecognizer(self.assets)

    def test_alias_and_decorations_are_normalized(self) -> None:
        self.assertEqual("神无月", normalize_name("SP：神无月"))
        names = asset_names(self.assets)
        self.assertIn(("神元月", "596", "神无月"), names)

    def test_wrapped_shishen_assets_use_the_same_runtime_contract(self) -> None:
        wrapped = {"items": self.assets}

        normalized = normalize_shishen_assets(wrapped)
        recognizer = StrictNameRecognizer(wrapped)

        self.assertEqual(self.assets, normalized)
        resolved, method = recognizer.resolve_text(
            "神元月",
            ocr_score=0.99,
        )
        self.assertEqual("596", resolved.shikigami_id)
        self.assertEqual("exact", method)

    def test_resolve_text_is_strict_and_preserves_ocr_confidence(self) -> None:
        resolved, method = self.recognizer.resolve_text(
            "满级 阁魔 典藏",
            ocr_score=0.73,
        )
        self.assertEqual("255", resolved.shikigami_id)
        self.assertEqual(0.73, resolved.confidence)
        self.assertEqual("substring", method)

        missing, method = self.recognizer.resolve_text("完全不存在")
        self.assertIsNone(missing)
        self.assertEqual("unresolved", method)

    def test_image_recognition_requires_multi_variant_consensus(self) -> None:
        crop = np.zeros((105, 30, 3), dtype=np.uint8)
        model = _FakeOcr("神元月")
        model.shapes = []
        original = model.ocr_single_line

        def record_shape(image):
            model.shapes.append(image.shape)
            return original(image)

        model.ocr_single_line = record_shape
        result = self.recognizer.recognize(crop, model)

        self.assertTrue(result.accepted)
        self.assertEqual("596", result.resolved.shikigami_id)
        self.assertGreaterEqual(result.consensus, 2)
        self.assertTrue(
            any(64 in shape[:2] for shape in model.shapes),
            "16px glyph-core OCR variant should be evaluated",
        )

    def test_reversible_locator_can_stop_on_one_exact_name_vote(self) -> None:
        crop = np.zeros((105, 30, 3), dtype=np.uint8)

        class FirstExactThenUnknown(_FakeOcr):
            def __init__(self) -> None:
                super().__init__("")
                self.calls = 0

            def ocr_single_line(self, image):
                self.calls += 1
                if self.calls == 1:
                    return "神无月", 0.995
                return "不存在", 0.995

        model = FirstExactThenUnknown()

        result = self.recognizer.recognize(
            crop,
            model,
            min_consensus=1,
        )

        self.assertEqual("596", result.resolved.shikigami_id)
        self.assertEqual("exact", result.method)
        self.assertEqual(1, result.consensus)
        self.assertEqual(1, model.calls)

    def test_unknown_image_is_not_forced(self) -> None:
        crop = np.zeros((105, 30, 3), dtype=np.uint8)
        result = self.recognizer.recognize(crop, _FakeOcr("不存在"))

        self.assertFalse(result.accepted)
        self.assertIsNone(result.resolved)

    def test_decorated_base_substring_is_rejected_but_exact_base_is_allowed(
        self,
    ) -> None:
        recognizer = StrictNameRecognizer(
            [
                {"id": 200, "name": "桃花妖", "alias": ""},
                {"id": 598, "name": "灼华桃花妖", "alias": ""},
            ]
        )
        ambiguous, method = recognizer.resolve_text(
            "满华桃花妖卡",
            ocr_score=0.9,
        )
        truncated, truncated_method = recognizer.resolve_text(
            "桃花妖",
            ocr_score=0.99,
        )
        exact, exact_method = recognizer.resolve_text(
            "灼华桃花妖",
            ocr_score=0.9,
        )

        self.assertIsNone(ambiguous)
        self.assertEqual("unresolved", method)
        self.assertEqual("200", truncated.shikigami_id)
        self.assertEqual("exact", truncated_method)
        self.assertEqual("598", exact.shikigami_id)
        self.assertEqual("exact", exact_method)

    def test_two_trimmed_crops_can_form_consensus(self) -> None:
        crop = np.zeros((105, 30, 3), dtype=np.uint8)

        class TrimOnlyOcr(_FakeOcr):
            def ocr_single_line(self, image):
                if image.shape[1] < 105 * 4:
                    return "神元月", 0.96
                return "不存在", 0.96

        result = self.recognizer.recognize(crop, TrimOnlyOcr(""))

        self.assertTrue(result.accepted)
        self.assertEqual("596", result.resolved.shikigami_id)
        self.assertGreaterEqual(result.consensus, 2)


if __name__ == "__main__":
    unittest.main()
