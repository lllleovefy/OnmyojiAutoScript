"""Extract safe Duel Onmyoji templates from a 1280x720 selection frame.

Bottom roster cards are reference-only: all six are visible at once and do
not prove which identity is selected.  A ``*-selected.png`` file is produced
only when the caller explicitly labels the large left-side model shown in the
source frame.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from tasks.Duel.portrait_library import load_image, save_image
from tasks.Duel.selection import (
    ONMYOJI_ROSTER_IDENTITY_ROIS,
    ONMYOJI_SELECTED_IDENTITY_ROI,
    ONMYOJI_SLOT_BY_ENUM_NAME,
    crop_xywh,
)


@dataclass(frozen=True)
class OnmyojiExtractionResult:
    roster_paths: tuple[Path, ...]
    selected_path: Path | None


def extract_onmyoji_templates(
    image: np.ndarray,
    output_root: Path,
    *,
    selected_enum_name: str | None = None,
    overwrite: bool = False,
) -> OnmyojiExtractionResult:
    """Write six roster references and at most one explicit selected model."""

    if not isinstance(image, np.ndarray) or image.shape[:2] != (720, 1280):
        raise ValueError("Onmyoji template source must be exactly 1280x720")
    root = Path(output_root)
    reference_root = root / "_reference"
    roster_paths: list[Path] = []
    ordered = sorted(
        ONMYOJI_SLOT_BY_ENUM_NAME.items(),
        key=lambda item: item[1],
    )
    for enum_name, slot in ordered:
        path = reference_root / f"{enum_name.lower()}-roster.png"
        _write_template(
            path,
            crop_xywh(image, ONMYOJI_ROSTER_IDENTITY_ROIS[slot - 1]),
            overwrite=overwrite,
        )
        roster_paths.append(path)

    selected_path = None
    if selected_enum_name is not None:
        normalized = str(selected_enum_name).strip().upper()
        if normalized not in ONMYOJI_SLOT_BY_ENUM_NAME:
            raise ValueError(
                f"Unsupported selected Onmyoji: {selected_enum_name!r}"
            )
        selected_path = root / f"{normalized.lower()}-selected.png"
        _write_template(
            selected_path,
            crop_xywh(image, ONMYOJI_SELECTED_IDENTITY_ROI),
            overwrite=overwrite,
        )
    return OnmyojiExtractionResult(tuple(roster_paths), selected_path)


def _write_template(
    path: Path,
    image: np.ndarray,
    *,
    overwrite: bool,
) -> None:
    if path.exists():
        try:
            existing = load_image(path)
        except (OSError, TypeError, ValueError):
            existing = None
        if existing is not None and np.array_equal(existing, image):
            return
        if not overwrite:
            raise FileExistsError(
                f"Refusing to replace a different Onmyoji template: {path}"
            )
    save_image(path, image)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--selected",
        choices=tuple(ONMYOJI_SLOT_BY_ENUM_NAME),
        help="explicit identity rendered as the large left-side model",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace a different existing roster/selected template",
    )
    args = parser.parse_args()
    result = extract_onmyoji_templates(
        load_image(args.source),
        args.output,
        selected_enum_name=args.selected,
        overwrite=args.force,
    )
    print(
        json.dumps(
            {
                "roster": [path.as_posix() for path in result.roster_paths],
                "selected": (
                    result.selected_path.as_posix()
                    if result.selected_path is not None
                    else None
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
