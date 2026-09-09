from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from dev_tools.duel_extract_portraits import (
    GRID_EMITTED_PHASH_DISTANCE,
    StableFrameGate,
    VideoPortraitExtractor,
    candidate_crop_specs,
    deduplicate_grid_boxes,
    detect_candidate_base_x,
    is_ascii_path,
    unicode_safe_video_path,
)
from tasks.Duel.portrait_library import save_image


class _UnusedOcr:
    def ocr_single_line(self, image):
        return "", 0.0

    def detect_and_ocr(self, image, **kwargs):
        return []


class DuelPortraitExtractorTest(unittest.TestCase):
    def test_fixed_candidate_rois(self) -> None:
        specs = candidate_crop_specs()
        self.assertEqual(8, len(specs))
        self.assertEqual((183, 535, 90, 144), specs[0].card)
        self.assertEqual((204, 538, 67, 120), specs[0].portrait)
        self.assertEqual((182, 550, 30, 105), specs[0].name)
        self.assertEqual((890, 535, 90, 144), specs[-1].card)

    def test_shifted_candidate_base_is_detected_from_periodic_edges(self) -> None:
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        base_x = 147
        for index in range(8):
            x = base_x + 101 * index
            frame[535:679, x : x + 2] = 255
            frame[535:679, x + 89 : x + 91] = 255

        detected = detect_candidate_base_x(frame)
        specs = candidate_crop_specs(base_x=detected)

        self.assertEqual(base_x, detected)
        self.assertEqual((147, 535, 90, 144), specs[0].card)
        self.assertEqual((168, 538, 67, 120), specs[0].portrait)

    def test_grid_box_nms_limits_nested_contours(self) -> None:
        boxes = []
        for row in range(10):
            for column in range(4):
                x, y = column * 130, row * 70
                boxes.extend(
                    [
                        (x, y, 70, 90),
                        (x + 2, y + 2, 67, 87),
                        (x + 3, y + 3, 65, 85),
                    ]
                )

        selected = deduplicate_grid_boxes(boxes, max_boxes=16)

        self.assertEqual(16, len(selected))
        self.assertEqual(len(selected), len(set(selected)))

    def test_unicode_video_path_uses_temporary_ascii_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "式神素材视频.mp4"
            source.write_bytes(b"video")
            with unicode_safe_video_path(source) as safe:
                self.assertTrue(safe.exists())
                self.assertTrue(is_ascii_path(safe))
                self.assertEqual(b"video", safe.read_bytes())
                copied = safe
            self.assertFalse(copied.exists())

    def test_stable_gate_requires_three_frames_and_deduplicates(self) -> None:
        gate = StableFrameGate(required_frames=3)
        first = np.zeros((720, 1280, 3), dtype=np.uint8)
        changed = first.copy()
        changed[525:695, 170:690] = 255

        self.assertFalse(gate.update(first))
        self.assertFalse(gate.update(first.copy()))
        self.assertTrue(gate.update(first.copy()))
        self.assertFalse(gate.update(first.copy()))
        self.assertFalse(gate.update(changed))
        self.assertFalse(gate.update(changed.copy()))
        self.assertTrue(gate.update(changed.copy()))

    def test_grid_gate_skips_animation_but_keeps_scrolled_roster(self) -> None:
        roi = (140, 90, 1000, 560)
        gate = StableFrameGate(
            required_frames=1,
            roi=roi,
            emitted_phash_distance=GRID_EMITTED_PHASH_DISTANCE,
        )
        random = np.random.default_rng(7)
        base = np.zeros((720, 1280, 3), dtype=np.uint8)
        base[90:650, 140:1140] = random.integers(
            0,
            256,
            size=(560, 1000, 3),
            dtype=np.uint8,
        )
        animation = base.copy()
        animation[300:308, 500:508] ^= 7
        scrolled = base.copy()
        scrolled[90:650, 140:1140] = np.roll(
            base[90:650, 140:1140],
            101,
            axis=1,
        )

        self.assertTrue(gate.update(base))
        self.assertFalse(gate.update(animation))
        self.assertTrue(gate.update(scrolled))

    def test_unresolved_visual_dedupe_is_per_view_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            output = base / "library"
            database = base / "duel.sqlite3"
            extractor = VideoPortraitExtractor(
                assets=[{"id": 596, "name": "神无月", "alias": ""}],
                ocr_model=_UnusedOcr(),
                library_path=output,
                database_path=database,
            )
            y, x = np.mgrid[:90, :70]
            portrait = np.stack(
                (x * 3 % 256, y * 5 % 256, (x + y) * 7 % 256),
                axis=2,
            ).astype(np.uint8)
            animated = portrait.copy()
            animated[10:14, 20:24] = np.clip(
                animated[10:14, 20:24].astype(np.int16) + 3,
                0,
                255,
            ).astype(np.uint8)
            stats = {
                "sampled_frames": 0,
                "stable_frames": 0,
                "labelled_candidates": 0,
                "unresolved_candidates": 0,
                "templates_created": 0,
                "templates_deduplicated": 0,
                "grid_templates_created": 0,
            }
            extractor._begin_unresolved_run(analyze_only=False)
            extractor._record_unresolved(
                portrait,
                name_crop=None,
                timestamp=84.0,
                frame_index=2520,
                slot=1,
                candidate=None,
                view="阵容",
                highest_candidate={"confidence": 0.82},
                analyze_only=False,
            )
            extractor._record_unresolved(
                animated,
                name_crop=None,
                timestamp=84.2,
                frame_index=2526,
                slot=1,
                candidate=None,
                view="阵容",
                highest_candidate={"confidence": 0.81},
                analyze_only=False,
            )
            extractor._record_unresolved(
                portrait,
                name_crop=None,
                timestamp=84.3,
                frame_index=2529,
                slot=1,
                candidate=None,
                view="候选",
                highest_candidate={"confidence": 0.80},
                analyze_only=False,
            )
            extractor._finish(
                source="test",
                analyze_only=False,
                stats=stats,
            )

            manifest = output / "unresolved.jsonl"
            rows = [
                json.loads(line)
                for line in manifest.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(2, len(rows))
            self.assertEqual({"候选", "阵容"}, {row["view"] for row in rows})
            self.assertTrue(all(row.get("portrait_phash") for row in rows))

            # Loading the manifest and seeing the animated crop again must
            # keep the same two representatives.
            extractor._begin_unresolved_run(analyze_only=False)
            extractor._record_unresolved(
                animated,
                name_crop=None,
                timestamp=90.0,
                frame_index=2700,
                slot=2,
                candidate=None,
                view="阵容",
                highest_candidate={"confidence": 0.81},
                analyze_only=False,
            )
            extractor._finish(
                source="test-repeat",
                analyze_only=False,
                stats=stats,
            )
            self.assertEqual(
                2,
                len(manifest.read_text(encoding="utf-8").splitlines()),
            )

            import sqlite3

            connection = sqlite3.connect(database)
            try:
                count = connection.execute(
                    """SELECT COUNT(*) FROM duel_portrait_templates
                       WHERE shishen_id IS NULL"""
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(2, count)

    def test_import_sample_session_uses_only_auto_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            session = base / "session"
            (session / "crops").mkdir(parents=True)
            portrait_path = session / "crops" / "portrait.png"
            image = np.zeros((120, 67, 3), dtype=np.uint8)
            image[:, :33] = (20, 100, 220)
            save_image(portrait_path, image)
            labels = [
                {
                    "frame_sha256": "frame-a",
                    "slot": 1,
                    "status": "auto",
                    "resolved_id": "596",
                    "resolved_name": "神无月",
                    "resolve_confidence": 0.99,
                    "portrait_path": "crops/portrait.png",
                },
                {
                    "frame_sha256": "frame-a",
                    "slot": 2,
                    "status": "unresolved",
                },
            ]
            (session / "labels.jsonl").write_text(
                "".join(
                    json.dumps(row, ensure_ascii=False) + "\n"
                    for row in labels
                ),
                encoding="utf-8",
            )
            (session / "manifest.jsonl").write_text(
                json.dumps(
                    {"frame_sha256": "frame-a", "crops": []}
                )
                + "\n",
                encoding="utf-8",
            )
            output = base / "library"
            existing_unresolved = {
                "timestamp": 1.0,
                "frame_index": 30,
                "slot": 1,
                "view": "候选",
                "portrait_path": "_unresolved/existing-portrait.png",
                "portrait_sha256": "existing-hash",
                "name_sha256": "existing-name",
                "ocr_score": 0.71,
            }
            output.mkdir(parents=True)
            (output / "unresolved.jsonl").write_text(
                json.dumps(existing_unresolved) + "\n",
                encoding="utf-8",
            )
            extractor = VideoPortraitExtractor(
                assets=[
                    {"id": 596, "name": "神无月", "alias": ""},
                    {"id": 200, "name": "桃花妖", "alias": ""},
                ],
                ocr_model=_UnusedOcr(),
                library_path=output,
                database_path=base / "duel.sqlite3",
            )

            report = extractor.import_sample_session(session)
            extractor.import_sample_session(session)

            expected = (
                output
                / "596-神无月"
                / "596-神无月-候选-001.png"
            )
            self.assertTrue(expected.exists())
            self.assertEqual(1, report["stats"]["labelled_candidates"])
            self.assertEqual(1, report["stats"]["unresolved_candidates"])
            self.assertEqual(1, report["coverage"]["covered_count"])
            unresolved = [
                json.loads(line)
                for line in (output / "unresolved.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual([existing_unresolved], unresolved)
            import sqlite3

            connection = sqlite3.connect(base / "duel.sqlite3")
            try:
                unresolved_rows = connection.execute(
                    """SELECT path, shishen_id, view, hash, source, confidence
                       FROM duel_portrait_templates
                       WHERE shishen_id IS NULL"""
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(1, len(unresolved_rows))
            self.assertEqual(
                (
                    "_unresolved/existing-portrait.png",
                    None,
                    "候选",
                    "existing-hash",
                    "video_unresolved",
                    0.71,
                ),
                unresolved_rows[0],
            )


if __name__ == "__main__":
    unittest.main()
