from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from fastapi import HTTPException

from module.duel_data.models import DuelPortraitLabel, DuelRecommendation
from module.duel_data.repository import DuelRepository
from module.server.duel_data_router import (
    duel_data_app,
    duel_portrait_image,
    duel_portrait_label,
    duel_portrait_status,
    duel_portrait_unresolved,
    duel_shishen_assets,
)


class DuelDataRouterContractTest(unittest.TestCase):
    def test_public_recommendation_matches_sse_wire_shape(self):
        recommendation = DuelRecommendation.model_validate(
            {
                "state": "SELF_PICK",
                "phase": "SELF_PICK",
                "mode": "recommend",
                "target_round": 2,
                "context_level": "known-opponent-slots",
                "shishen_id": 101,
                "shikigami_id": 101,
                "source": "rule",
                "score": 0.95,
                "confidence": 0.99,
                "recognition_confidence": 0.99,
                "recommendation_confidence": 0.99,
                "sample_size": 20,
                "explanation": "fixture",
                "reason": "fixture",
                "recommendations": [
                    {
                        "rank": 1,
                        "shishen_id": 101,
                        "shikigami_id": 101,
                        "source": "rule",
                        "score": 0.95,
                        "confidence": 0.99,
                        "sample_size": 20,
                        "context_level": "exact_history",
                        "context_sample_size": 42,
                        "reason": "fixture",
                        "evidence_sources": ["rule"],
                    }
                ],
                "evidence_sources": ["rule"],
            }
        )
        self.assertEqual(101, recommendation.recommendations[0].shishen_id)
        self.assertEqual(2, recommendation.target_round)
        self.assertEqual(1, recommendation.recommendations[0].rank)
        self.assertEqual(
            "exact_history",
            recommendation.recommendations[0].context_level,
        )
        self.assertEqual(
            42,
            recommendation.recommendations[0].context_sample_size,
        )

    def test_expected_routes_and_methods_are_registered(self):
        routes = {
            (route.path, method)
            for route in duel_data_app.routes
            for method in (route.methods or set())
        }
        expected = {
            ("/duel-data/matches", "GET"),
            ("/duel-data/matches/{match_id}", "GET"),
            ("/duel-data/matches/{match_id}", "PATCH"),
            ("/duel-data/summary", "GET"),
            ("/duel-data/shishen-assets", "GET"),
            ("/duel-data/portraits/status", "GET"),
            ("/duel-data/portraits/unresolved", "GET"),
            ("/duel-data/portraits/{template_id}/image", "GET"),
            ("/duel-data/portraits/{template_id}/label", "POST"),
            ("/duel-data/live/snapshot", "GET"),
            ("/duel-data/live", "GET"),
        }
        self.assertTrue(expected.issubset(routes), expected - routes)
        self.assertFalse(
            any(path.startswith("/duel-data/import/") for path, _ in routes),
            routes,
        )

    def test_duel_routes_use_the_same_unrestricted_access_as_other_apis(self):
        self.assertEqual([], duel_data_app.dependencies)
        for route in duel_data_app.routes:
            with self.subTest(route=route.path):
                self.assertEqual([], route.dependencies)

    def test_portrait_status_unresolved_and_label_api_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = DuelRepository(Path(temp_dir) / "duel.sqlite3")
            repository.save_snapshot(
                "shishen_assets",
                [{"id": 596, "name": "神无月", "alias": ["月神"]}],
            )
            sample, _ = repository.upsert_portrait_template(
                {
                    "path": "log/portraits/unresolved.png",
                    "view": "top",
                    "hash": "0123456789abcdef",
                    "source": "video",
                    "confidence": 0.2,
                }
            )
            with patch(
                "module.server.duel_data_router.get_duel_repository",
                return_value=repository,
            ):
                status = asyncio.run(duel_portrait_status())
                unresolved = asyncio.run(
                    duel_portrait_unresolved(page=1, page_size=20, view="top")
                )
                labeled = asyncio.run(
                    duel_portrait_label(
                        sample.id,
                        DuelPortraitLabel(shishen_id=596, name="神无月"),
                    )
                )
                self.assertEqual(1, status.unresolved)
                self.assertEqual(1, unresolved.total)
                self.assertEqual(596, labeled.shishen_id)
                with self.assertRaises(HTTPException) as caught:
                    asyncio.run(
                        duel_portrait_label(
                            9999,
                            DuelPortraitLabel(shishen_id=596, name="神无月"),
                        )
                    )
                self.assertEqual(404, caught.exception.status_code)

    def test_manual_label_promotes_local_crop_into_runtime_library(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "config" / "duel" / "portrait_library"
            source = library / "_unresolved" / "sample.png"
            from tasks.Duel.portrait_library import PortraitMatcher, save_image

            image = np.zeros((48, 63, 3), dtype=np.uint8)
            image[8:40, 12:50] = (40, 120, 230)
            save_image(source, image)
            repository = DuelRepository(root / "duel.sqlite3")
            repository.save_snapshot(
                "shishen_assets",
                [{"id": 596, "name": "神无月", "alias": "月神"}],
            )
            sample, _ = repository.upsert_portrait_template(
                {
                    "path": "_unresolved/sample.png",
                    "view": "上阵",
                    "hash": "fedcba9876543210",
                    "source": "live_unknown",
                    "confidence": 0.2,
                }
            )
            with (
                patch(
                    "module.server.duel_data_router.get_duel_repository",
                    return_value=repository,
                ),
                patch(
                    "module.server.duel_data_router.PROJECT_ROOT",
                    root,
                ),
            ):
                labeled = asyncio.run(
                    duel_portrait_label(
                        sample.id,
                        DuelPortraitLabel(shishen_id=596, name="神无月"),
                    )
                )
            self.assertTrue(labeled.path.startswith("596-神无月/"))
            self.assertTrue((library / labeled.path).is_file())
            coverage = (
                library / "coverage.json"
            ).read_text(encoding="utf-8")
            self.assertIn('"covered_count": 1', coverage)
            match = PortraitMatcher(library).match(image, views=("上阵",))
            self.assertEqual("596", match.shikigami_id)

    def test_shishen_assets_normalize_aliases_and_guard_manual_labels(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = DuelRepository(Path(temp_dir) / "duel.sqlite3")
            repository.save_snapshot(
                "shishen_assets",
                [
                    {
                        "id": "596",
                        "name": "神无月",
                        "alias": "月神，神无月/月之神",
                    },
                    {
                        "id": 101,
                        "name": "不知火",
                        "aliases": ["阿离", "离"],
                    },
                    {"id": "invalid", "name": "坏数据"},
                ],
            )
            sample, _ = repository.upsert_portrait_template(
                {
                    "path": "_unresolved/mapping-guard.png",
                    "view": "上阵",
                    "hash": "abcdef0123456789",
                    "source": "live_unknown",
                    "confidence": 0.2,
                }
            )
            with patch(
                "module.server.duel_data_router.get_duel_repository",
                return_value=repository,
            ):
                assets = asyncio.run(duel_shishen_assets())
                self.assertEqual([101, 596], [asset.id for asset in assets])
                self.assertEqual(
                    ["月神", "月之神"],
                    assets[1].alias,
                )
                for label in (
                    DuelPortraitLabel(shishen_id=999, name="不存在"),
                    DuelPortraitLabel(shishen_id=596, name="月神"),
                ):
                    with self.subTest(label=label):
                        with self.assertRaises(HTTPException) as caught:
                            asyncio.run(duel_portrait_label(sample.id, label))
                        self.assertEqual(422, caught.exception.status_code)
                self.assertEqual(
                    "unresolved",
                    repository.get_portrait_template(sample.id).status,
                )

    def test_portrait_image_is_confined_to_runtime_library(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            library = root / "config" / "duel" / "portrait_library"
            source = library / "_unresolved" / "sample.png"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            outside = root / "config" / "duel" / "outside.png"
            outside.write_bytes(b"\x89PNG\r\n\x1a\noutside")
            repository = DuelRepository(root / "duel.sqlite3")
            valid, _ = repository.upsert_portrait_template(
                {
                    "path": "_unresolved/sample.png",
                    "view": "上阵",
                    "hash": "1111111111111111",
                    "source": "live_unknown",
                    "confidence": 0.2,
                }
            )
            traversal, _ = repository.upsert_portrait_template(
                {
                    "path": "../outside.png",
                    "view": "上阵",
                    "hash": "2222222222222222",
                    "source": "live_unknown",
                    "confidence": 0.2,
                }
            )
            missing, _ = repository.upsert_portrait_template(
                {
                    "path": "_unresolved/missing.png",
                    "view": "上阵",
                    "hash": "3333333333333333",
                    "source": "live_unknown",
                    "confidence": 0.2,
                }
            )
            with (
                patch(
                    "module.server.duel_data_router.get_duel_repository",
                    return_value=repository,
                ),
                patch("module.server.duel_data_router.PROJECT_ROOT", root),
            ):
                response = asyncio.run(duel_portrait_image(valid.id))
                self.assertEqual(source.resolve(), Path(response.path))
                self.assertEqual("image/png", response.media_type)
                self.assertEqual(
                    "private, no-store",
                    response.headers["cache-control"],
                )
                for template_id, status_code in (
                    (traversal.id, 403),
                    (missing.id, 404),
                    (9999, 404),
                ):
                    with self.subTest(template_id=template_id):
                        with self.assertRaises(HTTPException) as caught:
                            asyncio.run(duel_portrait_image(template_id))
                        self.assertEqual(
                            status_code,
                            caught.exception.status_code,
                        )


if __name__ == "__main__":
    unittest.main()
