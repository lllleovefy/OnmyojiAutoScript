import ast
import unittest
from pathlib import Path

import cv2
import numpy as np


class DokanGatheringTemplateTest(unittest.TestCase):
    @staticmethod
    def gathering_rule() -> tuple[tuple[int, int, int, int], float, Path]:
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse((root / "tasks/Dokan/assets.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Name)
                and target.id == "I_RYOU_DOKAN_GATHERING"
                for target in node.targets
            ):
                continue
            values = {
                keyword.arg: ast.literal_eval(keyword.value)
                for keyword in node.value.keywords
            }
            return values["roi_back"], values["threshold"], root / values["file"]
        raise AssertionError("未找到道馆集结识别规则")

    def test_gathering_text_tolerates_small_position_drift(self) -> None:
        roi, threshold, template_path = self.gathering_rule()
        template = cv2.imdecode(
            np.fromfile(template_path, dtype=np.uint8), cv2.IMREAD_COLOR
        )
        height, width = template.shape[:2]
        screenshot = np.zeros((720, 1280, 3), dtype=np.uint8)
        screenshot[75 : 75 + height, 655 : 655 + width] = template
        x, y, roi_width, roi_height = roi
        source = screenshot[y : y + roi_height, x : x + roi_width]
        result = cv2.matchTemplate(source, template, cv2.TM_CCOEFF_NORMED)
        score = float(cv2.minMaxLoc(result)[1])

        self.assertGreater(score, threshold)


if __name__ == "__main__":
    unittest.main()
