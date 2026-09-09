from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from tasks.Duel.sample_capture import DuelBPSampleCollector


class DuelBPSampleCollectorTest(unittest.TestCase):
    def make_collector(self, root: Path, times: list[float]):
        iterator = iter(times)
        return DuelBPSampleCollector(
            config_name="../oas2",
            root=root,
            interval_seconds=1.0,
            monotonic=lambda: next(iterator),
        )

    def test_crops_slots_and_appends_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            image = np.zeros((720, 1280, 3), dtype=np.uint8)
            image[16:64, 100:163] = (11, 22, 33)
            collector = self.make_collector(Path(temp), [1.0])
            session = collector.start_session("round-1")

            result = collector.capture(
                image,
                phase="SELF_PICK",
                confidence=0.99,
                metadata={"own_picks": ["596"]},
            )

            self.assertTrue(result.captured)
            self.assertEqual(48, result.crop_count)
            event = json.loads(
                (session / "manifest.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual("SELF_PICK", event["phase"])
            self.assertEqual(["596"], event["metadata"]["own_picks"])
            own_slot = next(
                item
                for item in event["crops"]
                if item["kind"] == "top_identity"
                and item["role"] == "self"
                and item["slot"] == 1
            )
            self.assertEqual([100, 16, 63, 48], own_slot["roi"])
            crop = np.asarray(Image.open(session / own_slot["path"]))
            self.assertTrue(np.all(crop == (11, 22, 33)))
            self.assertNotIn("..", str(session))

    def test_throttle_exact_dedupe_and_phase_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            image = np.zeros((720, 1280, 3), dtype=np.uint8)
            collector = self.make_collector(
                Path(temp),
                [1.0, 1.5, 2.5, 3.5],
            )
            session = collector.start_session("round-1")

            first = collector.capture(
                image,
                phase="OPPONENT_PICK",
                confidence=1.0,
            )
            throttled = collector.capture(
                image.copy(),
                phase="OPPONENT_PICK",
                confidence=1.0,
            )
            duplicate = collector.capture(
                image.copy(),
                phase="OPPONENT_PICK",
                confidence=1.0,
            )
            ignored = collector.capture(
                image,
                phase="BATTLE",
                confidence=1.0,
            )

            self.assertTrue(first.captured)
            self.assertFalse(throttled.captured)
            self.assertFalse(duplicate.captured)
            self.assertFalse(ignored.captured)
            self.assertEqual(
                1,
                len(
                    (session / "manifest.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ),
            )

    def test_onmyoji_selection_omits_candidate_cards(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            collector = self.make_collector(Path(temp), [1.0])
            session = collector.start_session("round-1")
            collector.capture(
                np.zeros((720, 1280, 3), dtype=np.uint8),
                phase="SELF_PICK",
                confidence=1.0,
                onmyoji_selection=True,
            )

            event = json.loads(
                (session / "manifest.jsonl").read_text(encoding="utf-8")
            )
            self.assertFalse(
                any(
                    item["role"] == "candidate"
                    for item in event["crops"]
                )
            )

    def test_countdown_only_changes_do_not_create_duplicate_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            collector = self.make_collector(Path(temp), [1.0, 2.5])
            session = collector.start_session("round-1")
            first = np.zeros((720, 1280, 3), dtype=np.uint8)
            second = first.copy()
            # Countdown and centre-stage animation are outside every stable
            # identity/card crop used by the sample fingerprint.
            second[100:140, 610:670] = (200, 100, 50)

            self.assertTrue(
                collector.capture(
                    first,
                    phase="OPPONENT_PICK",
                    confidence=1.0,
                ).captured
            )
            self.assertFalse(
                collector.capture(
                    second,
                    phase="OPPONENT_PICK",
                    confidence=1.0,
                ).captured
            )
            self.assertEqual(
                1,
                len(
                    (session / "manifest.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ),
            )

    def test_rejects_small_frames(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            collector = self.make_collector(Path(temp), [1.0])
            with self.assertRaisesRegex(ValueError, "1280x720"):
                collector.capture(
                    np.zeros((360, 640, 3), dtype=np.uint8),
                    phase="SELF_PICK",
                    confidence=1.0,
                )


if __name__ == "__main__":
    unittest.main()
