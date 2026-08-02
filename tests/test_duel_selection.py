from __future__ import annotations

import unittest

import numpy as np

from tasks.Component.SwitchOnmyoji.config import Onmyoji
from tasks.Duel.selection import (
    ONMYOJI_ROSTER_CARD_ROIS,
    ONMYOJI_ROSTER_IDENTITY_ROIS,
    OPPONENT_REVEAL_NAME_ROI,
    OPPONENT_IDENTITY_ROIS,
    OPPONENT_SLOT_CENTERS,
    SELECTED_NAME_ROI,
    VisibleCandidate,
    candidate_card_roi,
    candidate_click_point,
    candidate_name_roi,
    candidate_page_fingerprint,
    candidate_portrait_roi,
    choose_visible_candidate,
    crop_xywh,
    detect_candidate_base_x,
    onmyoji_click_point,
    scale_reference_roi,
)


class DuelSelectionGeometryTest(unittest.TestCase):
    def test_current_candidate_rois_and_click_points(self) -> None:
        self.assertEqual((183, 535, 90, 144), candidate_card_roi(1))
        self.assertEqual((204, 538, 67, 120), candidate_portrait_roi(1))
        self.assertEqual((182, 550, 30, 105), candidate_name_roi(1))
        self.assertEqual((228, 610), candidate_click_point(1))
        self.assertEqual((890, 535, 90, 144), candidate_card_roi(8))
        self.assertEqual((935, 610), candidate_click_point(8))
        self.assertEqual(
            (160, 535, 90, 144),
            candidate_card_roi(1, base_x=160),
        )
        self.assertEqual(
            (205, 610),
            candidate_click_point(1, base_x=160),
        )

    def test_detects_shifted_candidate_card_period(self) -> None:
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        for index in range(8):
            left = 160 + 101 * index
            frame[535:679, left : left + 2] = 255
            frame[535:679, left + 89 : left + 91] = 255
        self.assertEqual(160, detect_candidate_base_x(frame))
        self.assertEqual(
            183,
            detect_candidate_base_x(np.zeros_like(frame)),
        )

    def test_name_portrait_boundary_rejects_right_shifted_edge_phase(self) -> None:
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        for index in range(8):
            left = 160 + 101 * index
            for edge in (left, left + 21, left + 89):
                frame[535:679, edge : edge + 2] = 255
            # A strong portrait/next-ribbon phase resembles another card pair
            # but has no expected name-to-portrait boundary at +21.
            decoy = left + 14
            frame[535:679, decoy : decoy + 2] = 255
            frame[535:679, decoy + 89 : decoy + 91] = 255
        self.assertEqual(160, detect_candidate_base_x(frame))

    def test_invalid_slot_and_crop_fail_closed(self) -> None:
        for value in (0, 9, "x"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                candidate_card_roi(value)
        image = np.zeros((720, 1280, 3), dtype=np.uint8)
        self.assertEqual((105, 30, 3), crop_xywh(image, (182, 550, 30, 105)).shape)
        with self.assertRaises(ValueError):
            crop_xywh(image, (1270, 700, 30, 30))

    def test_opponent_slots_are_left_to_right(self) -> None:
        self.assertEqual(
            ((863, 48), (935, 48), (1007, 48), (1079, 48), (1151, 48)),
            OPPONENT_SLOT_CENTERS,
        )
        self.assertEqual((832, 16, 63, 48), OPPONENT_IDENTITY_ROIS[0])
        self.assertEqual((1120, 16, 63, 48), OPPONENT_IDENTITY_ROIS[4])

    def test_reveal_name_rois_scale_from_the_1280_reference_canvas(self) -> None:
        self.assertEqual((170, 150, 40, 165), SELECTED_NAME_ROI)
        self.assertEqual((680, 150, 40, 165), OPPONENT_REVEAL_NAME_ROI)
        self.assertEqual(
            (212, 187, 51, 207),
            scale_reference_roi(SELECTED_NAME_ROI, (900, 1600)),
        )
        self.assertEqual(
            (850, 187, 50, 207),
            scale_reference_roi(OPPONENT_REVEAL_NAME_ROI, (900, 1600)),
        )
        self.assertEqual(
            (213, 188, 51, 208),
            scale_reference_roi(SELECTED_NAME_ROI, (904, 1607)),
        )
        self.assertEqual(
            (853, 188, 51, 208),
            scale_reference_roi(OPPONENT_REVEAL_NAME_ROI, (904, 1607)),
        )

    def test_onmyoji_roster_rois_match_the_six_live_cards(self) -> None:
        self.assertEqual((193, 548, 139, 172), ONMYOJI_ROSTER_CARD_ROIS[0])
        self.assertEqual((958, 548, 139, 172), ONMYOJI_ROSTER_CARD_ROIS[5])
        self.assertEqual(
            (251, 554, 74, 150),
            ONMYOJI_ROSTER_IDENTITY_ROIS[0],
        )
        self.assertEqual(
            (1016, 554, 74, 150),
            ONMYOJI_ROSTER_IDENTITY_ROIS[5],
        )
        for click, card in zip(
            (
                (250, 613),
                (404, 613),
                (558, 613),
                (712, 613),
                (865, 613),
                (1018, 613),
            ),
            ONMYOJI_ROSTER_CARD_ROIS,
        ):
            x, y, width, height = card
            self.assertTrue(x <= click[0] < x + width)
            self.assertTrue(y <= click[1] < y + height)

    def test_onmyoji_enum_uses_current_roster_order(self) -> None:
        self.assertEqual((250, 613), onmyoji_click_point(Onmyoji.SEIMI))
        self.assertEqual((404, 613), onmyoji_click_point(Onmyoji.KAGURA))
        self.assertEqual((865, 613), onmyoji_click_point(Onmyoji.YORIMITSU))
        self.assertEqual((1018, 613), onmyoji_click_point(Onmyoji.MICHINAGA))


class DuelVisibleCandidateTest(unittest.TestCase):
    def test_highest_ranked_verified_enabled_candidate_wins(self) -> None:
        candidates = (
            VisibleCandidate(1, "10", "A", 0.99, disabled=True),
            VisibleCandidate(2, "20", "B", 0.97),
            VisibleCandidate(3, "20", "B", 0.99),
            VisibleCandidate(4, "30", "C", 1.0),
        )
        selected = choose_visible_candidate(("10", "20", "30"), candidates)
        self.assertIsNotNone(selected)
        self.assertEqual(3, selected.slot)

    def test_unavailable_and_missing_candidates_return_none(self) -> None:
        candidates = (VisibleCandidate(1, "20", "B", 0.99),)
        self.assertIsNone(
            choose_visible_candidate(
                ("10", "20"),
                candidates,
                unavailable_ids=("20",),
            )
        )

    def test_allowed_pool_rejects_a_higher_ranked_outside_candidate(self) -> None:
        candidates = (
            VisibleCandidate(1, "999", "outside", 1.0),
            VisibleCandidate(2, "596", "inside", 0.99),
        )

        selected = choose_visible_candidate(
            ("999", "596"),
            candidates,
            allowed_ids=("596",),
        )

        self.assertIsNotNone(selected)
        self.assertEqual("596", selected.shikigami_id)

    def test_page_fingerprint_is_order_independent(self) -> None:
        first = (
            VisibleCandidate(2, "20", "B", 0.99),
            VisibleCandidate(1, "10", "A", 0.99, disabled=True),
        )
        second = tuple(reversed(first))
        self.assertEqual(
            candidate_page_fingerprint(first),
            candidate_page_fingerprint(second),
        )

    def test_visual_fingerprint_distinguishes_empty_recognition_pages(self) -> None:
        first_page = np.zeros((720, 1280, 3), dtype=np.uint8)
        second_page = first_page.copy()
        second_page[535:679, 125:613] = 96

        first = candidate_page_fingerprint((), first_page)
        second = candidate_page_fingerprint((), second_page)

        self.assertEqual((), first[0])
        self.assertEqual((), second[0])
        self.assertNotEqual(first[1], second[1])

    def test_visual_fingerprint_is_stable_for_a_stationary_page(self) -> None:
        frame = np.zeros((720, 1280, 3), dtype=np.uint8)
        frame[535:679, 125:1101] = 96
        noisy = frame.astype(np.int16)
        noise = np.random.default_rng(7).integers(
            -2,
            3,
            size=(144, 976, 3),
        )
        noisy[535:679, 125:1101] += noise
        noisy = np.clip(noisy, 0, 255).astype(np.uint8)

        self.assertEqual(
            candidate_page_fingerprint((), frame),
            candidate_page_fingerprint((), noisy),
        )

    def test_scroll_transition_is_not_mistaken_for_either_static_page(self) -> None:
        old_page = np.zeros((720, 1280, 3), dtype=np.uint8)
        old_page[535:679, 125:450] = 64
        new_page = np.zeros_like(old_page)
        new_page[535:679, 776:1101] = 192
        transition = np.zeros_like(old_page)
        transition[535:679, 300:625] = 64
        transition[535:679, 625:950] = 192

        old_key = candidate_page_fingerprint((), old_page)
        new_key = candidate_page_fingerprint((), new_page)
        transition_key = candidate_page_fingerprint((), transition)

        self.assertNotEqual(old_key, new_key)
        self.assertNotEqual(old_key, transition_key)
        self.assertNotEqual(new_key, transition_key)

    def test_shifted_candidate_click_uses_detected_base(self) -> None:
        candidate = VisibleCandidate(
            2,
            "20",
            "B",
            0.99,
            source="portrait",
            base_x=160,
        )
        self.assertEqual((306, 610), candidate.click_point)


if __name__ == "__main__":
    unittest.main()
