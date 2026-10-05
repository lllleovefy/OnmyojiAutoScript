"""Exercise both recognition methods using the project's existing RPC client."""

import argparse
import json
from pathlib import Path
from time import perf_counter

import cv2
import numpy as np
from PIL import Image

from module.ocr.rpc import ModelProxy


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address", help="Trusted OCR host:port")
    parser.add_argument("--image", type=Path)
    parser.add_argument("--expected", default="12345")
    parser.add_argument("--roi", type=int, nargs=4, metavar=("X1", "Y1", "X2", "Y2"))
    args = parser.parse_args()

    if args.image:
        with Image.open(args.image) as source:
            image = np.array(source.convert("RGB"))
    else:
        if args.expected != "12345":
            parser.error("--expected requires --image unless it is 12345")
        image = np.full((64, 220, 3), 255, dtype=np.uint8)
        cv2.putText(image, "12345", (10, 48), cv2.FONT_HERSHEY_SIMPLEX,
                    1.5, (0, 0, 0), 2, cv2.LINE_AA)

    if args.roi:
        x1, y1, x2, y2 = args.roi
        height, width = image.shape[:2]
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            parser.error("--roi must be a nonempty rectangle inside the image")
        image = image[y1:y2, x1:x2].copy()

    # Match BaseCor.detect_and_ocr(): square padding to the next multiple of 32.
    height, width = image.shape[:2]
    length = max(height, width) // 32 * 32 + 32
    detection_image = cv2.copyMakeBorder(
        image, 0, length - height, 0, length - width,
        cv2.BORDER_CONSTANT, value=(0, 0, 0),
    )

    client = ModelProxy(args.address)
    report = {"address": args.address, "expected": args.expected}
    try:
        start = perf_counter()
        text, score = client.ocr_single_line(image)
        report["single"] = {
            "text": text, "score": score,
            "seconds": round(perf_counter() - start, 3),
        }
        start = perf_counter()
        boxes = client.detect_and_ocr(detection_image)
        detected = "".join(item.ocr_text for item in boxes)
        report["detect"] = {
            "text": detected, "boxes": len(boxes),
            "seconds": round(perf_counter() - start, 3),
        }
        report["server"] = client.get_server_info()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        for method, actual in (("single", text), ("detect", detected)):
            if actual.replace(" ", "") != args.expected.replace(" ", ""):
                raise RuntimeError(f"{method}: expected {args.expected!r}, got {actual!r}")
    finally:
        client.client.close()


if __name__ == "__main__":
    main()
