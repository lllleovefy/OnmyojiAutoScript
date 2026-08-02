from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from dev_tools.duel_resolve_unresolved import UnresolvedPortraitResolver
from module.duel_data.repository import DuelRepository
from tasks.Duel.portrait_library import (
    PortraitLibrary,
    image_sha256,
    save_image,
)


class _FakeRecognizer:
    def recognize(self, image, model):
        marker = int(image[0, 0, 0])
        if marker == 101:
            return SimpleNamespace(
                accepted=True,
                resolved=SimpleNamespace(
                    shikigami_id="101",
                    name="测试甲",
                    confidence=0.99,
                ),
                method="exact",
                text="测试甲",
                ocr_score=0.99,
                consensus=2,
            )
        return SimpleNamespace(
            accepted=False,
            resolved=None,
            method="unresolved",
            text="",
            ocr_score=0.1,
            consensus=0,
        )


class _DetectorRecognizer:
    def recognize(self, image, model):
        return SimpleNamespace(
            accepted=False,
            resolved=None,
            method="unresolved",
            text="",
            ocr_score=0.1,
            consensus=0,
        )

    def resolve_text(self, text, *, ocr_score):
        if text == "测试甲":
            return (
                SimpleNamespace(
                    shikigami_id="101",
                    name="测试甲",
                    confidence=ocr_score,
                ),
                "exact",
            )
        return None, "unresolved"


class _DetectorModel:
    def ocr_single_line(self, image):
        return "", 0.1

    def detect_and_ocr(self, image, **kwargs):
        return [
            SimpleNamespace(
                score=0.99,
                ocr_text="测试甲",
                box=np.asarray(
                    [[0, 0], [10, 0], [10, 10], [0, 10]],
                    dtype=np.float32,
                ),
            )
        ]


def _image(marker: int, *, width: int = 67, height: int = 120):
    return np.full((height, width, 3), marker, dtype=np.uint8)


class UnresolvedPortraitResolverTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "portrait_library"
        self.root.mkdir()
        self.database_path = Path(self.temporary.name) / "duel.sqlite3"
        self.repository = DuelRepository(self.database_path)
        self.assets = [
            {"id": 101, "name": "测试甲", "alias": ""},
            {"id": 102, "name": "测试乙", "alias": ""},
        ]
        self.repository.save_snapshot("shishen_assets", self.assets)

    def tearDown(self):
        self.temporary.cleanup()

    def _write_manifest_pair(
        self,
        stem: str,
        *,
        marker: int,
        with_name: bool = True,
        view: str = "候选",
    ) -> dict:
        unresolved = self.root / "_unresolved"
        unresolved.mkdir(exist_ok=True)
        portrait = unresolved / f"{stem}-portrait.png"
        save_image(portrait, _image(marker))
        name_path = None
        if with_name:
            name = unresolved / f"{stem}-name.png"
            save_image(name, _image(marker, width=30, height=105))
            name_path = name.relative_to(self.root).as_posix()
        row = {
            "portrait_path": portrait.relative_to(self.root).as_posix(),
            "name_path": name_path,
            "portrait_sha256": image_sha256(_image(marker)),
            "view": view,
            "source": "video_unresolved",
            "confidence": 0.1,
        }
        return row

    def _write_manifest(self, rows: list[dict]) -> None:
        (self.root / "unresolved.jsonl").write_text(
            "".join(
                json.dumps(row, ensure_ascii=False) + "\n" for row in rows
            ),
            encoding="utf-8",
        )
        for row in rows:
            self.repository.upsert_portrait_template(
                {
                    "path": row["portrait_path"],
                    "view": row["view"],
                    "hash": row["portrait_sha256"],
                    "source": row["source"],
                    "confidence": row["confidence"],
                }
            )

    def _resolver(self) -> UnresolvedPortraitResolver:
        return UnresolvedPortraitResolver(
            library_path=self.root,
            database_path=self.database_path,
            assets=self.assets,
            recognizer=_FakeRecognizer(),
            ocr_model=object(),
        )

    def test_analyze_recognizes_strict_name_without_mutating_files(self):
        accepted = self._write_manifest_pair("accepted", marker=101)
        rejected = self._write_manifest_pair("rejected", marker=9)
        no_name = self._write_manifest_pair(
            "lineup", marker=8, with_name=False, view="阵容"
        )
        self._write_manifest([accepted, rejected, no_name])

        plan = self._resolver().analyze()

        self.assertEqual(3, plan.total)
        self.assertEqual(1, plan.recognized)
        self.assertEqual(2, plan.discarded)
        self.assertEqual("101", plan.decisions[0].shikigami_id)
        self.assertTrue((self.root / accepted["portrait_path"]).is_file())
        self.assertEqual(3, self.repository.portrait_status().unresolved)

    def test_parent_directory_is_an_explicit_manual_label(self):
        target = self.root / "102-测试乙"
        target.mkdir()
        save_image(target / "manual-portrait.png", _image(7))
        save_image(
            target / "manual-name.png",
            _image(7, width=30, height=105),
        )
        (self.root / "unresolved.jsonl").write_text("", encoding="utf-8")

        plan = self._resolver().analyze()

        self.assertEqual(1, plan.total)
        self.assertEqual(1, plan.recognized)
        self.assertEqual("102", plan.decisions[0].shikigami_id)
        self.assertEqual("manual_directory", plan.decisions[0].method)

    def test_enhanced_detector_can_supply_two_independent_votes(self):
        candidate = self._write_manifest_pair("detector", marker=7)
        self._write_manifest([candidate])
        resolver = UnresolvedPortraitResolver(
            library_path=self.root,
            database_path=self.database_path,
            assets=self.assets,
            recognizer=_DetectorRecognizer(),
            ocr_model=_DetectorModel(),
        )

        plan = resolver.analyze()

        self.assertEqual(1, plan.recognized)
        self.assertEqual("101", plan.decisions[0].shikigami_id)
        self.assertGreaterEqual(plan.decisions[0].consensus, 2)

    def test_commit_imports_recognized_and_discards_every_other_unresolved(self):
        accepted = self._write_manifest_pair("accepted", marker=101)
        rejected = self._write_manifest_pair("rejected", marker=9)
        self._write_manifest([accepted, rejected])
        resolver = self._resolver()
        plan = resolver.analyze()

        result = resolver.commit(plan, discard_unrecognized=True)

        self.assertEqual(1, result["recognized"])
        self.assertEqual(1, result["discarded"])
        self.assertEqual([], list((self.root / "_unresolved").rglob("*")))
        self.assertEqual(
            "", (self.root / "unresolved.jsonl").read_text(encoding="utf-8")
        )
        self.assertEqual(0, self.repository.portrait_status().unresolved)
        self.assertEqual(1, self.repository.portrait_status().recognized)
        standard = list((self.root / "101-测试甲").glob("*-候选-*.png"))
        self.assertEqual(1, len(standard))
        self.assertTrue((self.root / "resolve_report.json").is_file())

        repeated = resolver.analyze()
        self.assertEqual(0, repeated.total)

    def test_commit_requires_explicit_discard_authorization(self):
        rejected = self._write_manifest_pair("rejected", marker=9)
        self._write_manifest([rejected])
        resolver = self._resolver()

        with self.assertRaises(ValueError):
            resolver.commit(
                resolver.analyze(),
                discard_unrecognized=False,
            )

        self.assertTrue((self.root / rejected["portrait_path"]).is_file())
        self.assertEqual(1, self.repository.portrait_status().unresolved)

    def test_misplaced_standard_file_uses_parent_as_manual_correction(self):
        library = PortraitLibrary(
            self.root,
            database_path=self.database_path,
        )
        original = library.add_template(
            _image(42),
            shikigami_id="101",
            name="测试甲",
            view="候选",
            source="test",
            confidence=0.9,
        ).record
        corrected_parent = self.root / "102-测试乙"
        corrected_parent.mkdir()
        source = self.root / original.relative_path
        misplaced = corrected_parent / source.name
        source.replace(misplaced)
        (self.root / "unresolved.jsonl").write_text("", encoding="utf-8")
        resolver = self._resolver()

        plan = resolver.analyze()
        self.assertEqual(1, plan.total)
        self.assertEqual("102", plan.decisions[0].shikigami_id)

        resolver.commit(plan, discard_unrecognized=True)

        records = PortraitLibrary(self.root).records
        self.assertEqual({"102"}, {row.shikigami_id for row in records})
        self.assertFalse(misplaced.exists())
        with sqlite3.connect(self.database_path) as connection:
            identities = {
                str(row[0])
                for row in connection.execute(
                    "SELECT shishen_id FROM duel_portrait_templates"
                )
            }
        self.assertEqual({"102"}, identities)


if __name__ == "__main__":
    unittest.main()
