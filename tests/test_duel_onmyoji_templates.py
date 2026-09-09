from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from dev_tools.duel_extract_onmyoji_templates import (
    extract_onmyoji_templates,
)
from tasks.Duel.portrait_library import load_image, save_image


class DuelOnmyojiTemplateExtractionTest(unittest.TestCase):
    def test_six_roster_references_but_only_declared_selected_identity(self) -> None:
        image = np.zeros((720, 1280, 3), dtype=np.uint8)
        for slot in range(6):
            image[548:720, 193 + 153 * slot : 332 + 153 * slot] = (
                20 + slot * 30
            )
        image[125:540, 250:590] = 211

        with tempfile.TemporaryDirectory() as directory:
            result = extract_onmyoji_templates(
                image,
                Path(directory),
                selected_enum_name="YORIMITSU",
            )

            self.assertEqual(6, len(result.roster_paths))
            self.assertEqual("yorimitsu-selected.png", result.selected_path.name)
            self.assertEqual(
                (415, 340, 3),
                load_image(result.selected_path).shape,
            )
            for path in result.roster_paths:
                self.assertEqual((150, 74, 3), load_image(path).shape)
            self.assertFalse(
                (Path(directory) / "seimi-selected.png").exists()
            )

            repeated = extract_onmyoji_templates(
                image,
                Path(directory),
                selected_enum_name="YORIMITSU",
            )
            self.assertEqual(result, repeated)

    def test_selected_identity_must_be_explicit(self) -> None:
        image = np.zeros((720, 1280, 3), dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            result = extract_onmyoji_templates(image, Path(directory))

            self.assertIsNone(result.selected_path)
            self.assertEqual([], list(Path(directory).glob("*-selected.png")))

    def test_conflicting_selected_template_requires_explicit_overwrite(self) -> None:
        image = np.zeros((720, 1280, 3), dtype=np.uint8)
        image[125:540, 250:590] = 111
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = extract_onmyoji_templates(
                image,
                root,
                selected_enum_name="YORIMITSU",
            )
            save_image(
                result.selected_path,
                np.full((415, 340, 3), 222, dtype=np.uint8),
            )

            with self.assertRaises(FileExistsError):
                extract_onmyoji_templates(
                    image,
                    root,
                    selected_enum_name="YORIMITSU",
                )

            extract_onmyoji_templates(
                image,
                root,
                selected_enum_name="YORIMITSU",
                overwrite=True,
            )
            self.assertTrue(
                np.array_equal(
                    load_image(result.selected_path),
                    image[125:540, 250:590],
                )
            )


if __name__ == "__main__":
    unittest.main()
