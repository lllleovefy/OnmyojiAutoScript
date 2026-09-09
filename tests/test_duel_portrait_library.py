from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np

from tasks.Duel.portrait_library import PortraitLibrary, PortraitMatcher


def portrait(seed: int = 0) -> np.ndarray:
    y, x = np.mgrid[:120, :67]
    return np.stack(
        (
            (x * 3 + seed) % 256,
            (y * 2 + seed * 7) % 256,
            ((x + y) * 5 + seed * 11) % 256,
        ),
        axis=2,
    ).astype(np.uint8)


class DuelPortraitLibraryTest(unittest.TestCase):
    def test_id_name_view_sequence_and_dual_dedupe(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "library"
            database = Path(temp) / "duel.sqlite3"
            library = PortraitLibrary(root, database_path=database)
            image = portrait()

            first = library.add_template(
                image,
                shikigami_id=596,
                name="神无月",
                view="candidate",
                source="test",
                confidence=0.99,
            )
            exact = library.add_template(
                image.copy(),
                shikigami_id=596,
                name="神无月",
                view="候选",
                source="test",
                confidence=0.99,
            )
            almost = image.copy()
            almost[0, 0, 0] += 1
            perceptual = library.add_template(
                almost,
                shikigami_id=596,
                name="神无月",
                view="候选",
                source="test",
                confidence=0.99,
            )
            lineup = library.add_template(
                image,
                shikigami_id=596,
                name="神无月",
                view="lineup",
                source="test",
                confidence=0.99,
            )

            self.assertTrue(first.created)
            self.assertEqual(
                "596-神无月/596-神无月-候选-001.png",
                first.record.relative_path,
            )
            self.assertFalse(exact.created)
            self.assertEqual("sha256", exact.dedupe_reason)
            self.assertFalse(perceptual.created)
            self.assertEqual("phash", perceptual.dedupe_reason)
            self.assertTrue(lineup.created)
            self.assertTrue((root / first.record.relative_path).exists())

            connection = sqlite3.connect(database)
            try:
                rows = connection.execute(
                    """SELECT path, shishen_id, name, view, hash
                       FROM duel_portrait_templates ORDER BY view"""
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(2, len(rows))
            self.assertEqual(596, rows[0][1])
            self.assertEqual("神无月", rows[0][2])

    def test_matcher_returns_identity_and_highest_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "library"
            library = PortraitLibrary(root)
            expected = portrait(3)
            library.add_template(
                expected,
                shikigami_id=596,
                name="神无月",
                view="候选",
                source="test",
                confidence=1.0,
            )
            library.add_template(
                portrait(99),
                shikigami_id=566,
                name="晨晖惠比寿",
                view="阵容",
                source="test",
                confidence=1.0,
            )

            matcher = PortraitMatcher(root)
            batch = matcher.match_many(
                (expected, portrait(99)),
                views=("候选", "阵容"),
            )
            matched = batch[0]
            absent_view = matcher.match(expected, views=("上阵",))

            self.assertEqual(2, len(batch))
            self.assertEqual("566", batch[1].shikigami_id)
            self.assertEqual("596", matched.shikigami_id)
            self.assertEqual("神无月", matched.name)
            self.assertEqual(1.0, matched.confidence)
            self.assertEqual(
                "596",
                matched.highest_candidate["shikigami_id"],
            )
            self.assertIsNone(absent_view.shikigami_id)
            self.assertIsNone(absent_view.highest_candidate)
            cached_features = len(matcher._features)
            matcher.match_many((expected, expected), views=("候选",))
            self.assertEqual(cached_features, len(matcher._features))

            newly_added = portrait(177)
            library.add_template(
                newly_added,
                shikigami_id=255,
                name="阎魔",
                view="上阵",
                source="manual",
                confidence=1.0,
            )
            hot_reloaded = matcher.match(newly_added, views=("上阵",))
            self.assertEqual("255", hot_reloaded.shikigami_id)
            self.assertEqual("阎魔", hot_reloaded.name)

    def test_analyze_only_and_coverage_report_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "library"
            library = PortraitLibrary(root)
            result = library.add_template(
                portrait(),
                shikigami_id=596,
                name="神无月",
                view="候选",
                source="test",
                confidence=1.0,
                analyze_only=True,
            )
            report = library.coverage_report(
                [
                    {"id": 596, "name": "神无月"},
                    {"id": 200, "name": "桃花妖"},
                ]
            )

            self.assertTrue(result.created)
            self.assertFalse(root.exists())
            self.assertEqual(["200", "596"], [row["id"] for row in report["items"]])
            self.assertEqual(1, report["covered_count"])
            self.assertEqual(1, report["missing_count"])

    def test_concurrent_instances_allocate_distinct_sequences(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "library"
            database = Path(temp) / "duel.sqlite3"
            images = (portrait(31), portrait(79))

            def add(index: int):
                return PortraitLibrary(
                    root,
                    database_path=database,
                ).add_template(
                    images[index],
                    shikigami_id=596,
                    name="神无月",
                    view="上阵",
                    source="manual",
                    confidence=1.0,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(add, range(2)))

            self.assertTrue(all(item.created for item in results))
            paths = {item.record.relative_path for item in results}
            self.assertEqual(
                {
                    "596-神无月/596-神无月-上阵-001.png",
                    "596-神无月/596-神无月-上阵-002.png",
                },
                paths,
            )
            self.assertTrue(all((root / path).is_file() for path in paths))
            records = [
                json.loads(line)
                for line in (root / "index.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertEqual(2, len(records))
            connection = sqlite3.connect(database)
            try:
                count = connection.execute(
                    "SELECT COUNT(*) FROM duel_portrait_templates"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(2, count)


if __name__ == "__main__":
    unittest.main()
