"""Pure phase classification for the current 1280x720 Duel BP UI.

The top portraits are not phase signals: auto-entry pre-fills all five of our
spatial slots before they are locked.  Turn ownership comes from the bottom
confirmation control, and READY only starts after the round-six Onmyoji
selection controls disappear.
"""

from __future__ import annotations

from dataclasses import dataclass

from tasks.Duel.bp import DuelBPState


@dataclass(frozen=True)
class DuelBPPhaseSignals:
    result: float = 0.0
    battle: float = 0.0
    ban: float = 0.0
    confirm_active: float = 0.0
    confirm_locked: float = 0.0
    opponent_selecting: float = 0.0
    opponent_locked: float = 0.0
    onmyoji_selection: float = 0.0
    lineup_reveal: float = 0.0
    weak_selection: float = 0.0
    legacy_action: float = 0.0

    @property
    def selection_controls_visible(self) -> bool:
        return max(
            self.confirm_active,
            self.confirm_locked,
            self.opponent_selecting,
            self.opponent_locked,
            self.onmyoji_selection,
            self.weak_selection,
            self.legacy_action,
        ) > 0


@dataclass(frozen=True)
class DuelBPPhaseClassification:
    state: DuelBPState | None
    confidence: float
    seen_onmyoji_selection: bool
    reason: str


def classify_bp_phase(
    signals: DuelBPPhaseSignals,
    *,
    previous_state: DuelBPState,
    seen_onmyoji_selection: bool = False,
) -> DuelBPPhaseClassification:
    """Classify one complete template-match batch, failing closed on doubt."""

    seen_onmyoji = seen_onmyoji_selection or signals.onmyoji_selection > 0

    # Terminal and real-battle evidence always wins over stale selection UI.
    if signals.result > 0:
        return DuelBPPhaseClassification(
            DuelBPState.RESULT, signals.result, seen_onmyoji, "result"
        )
    if signals.battle > 0:
        return DuelBPPhaseClassification(
            DuelBPState.BATTLE, signals.battle, seen_onmyoji, "battle"
        )
    if signals.ban > 0:
        return DuelBPPhaseClassification(
            DuelBPState.BAN, signals.ban, seen_onmyoji, "ban"
        )

    # A dedicated final-lineup background is strong evidence.  The Onmyoji
    # guard prevents an unrelated transition background from ending BP early.
    if signals.lineup_reveal > 0 and seen_onmyoji:
        return DuelBPPhaseClassification(
            DuelBPState.READY,
            signals.lineup_reveal,
            seen_onmyoji,
            "final_lineup_reveal",
        )

    # The active button means we can still act, even when the centre says
    # either "opponent selecting" or "opponent confirmed".
    if signals.confirm_active > 0:
        return DuelBPPhaseClassification(
            DuelBPState.SELF_PICK,
            # Only this control authorizes an AUTO action. Opponent and
            # round-six overlays must not inflate weak button evidence.
            signals.confirm_active,
            seen_onmyoji,
            "self_confirm_active",
        )

    # Once our control says "confirmed", acting again is unsafe.  Treat all
    # reveal overlays and opponent waits as the opponent/non-action phase.
    if signals.confirm_locked > 0:
        return DuelBPPhaseClassification(
            DuelBPState.OPPONENT_PICK,
            # Only this control proves our irreversible confirmation.
            signals.confirm_locked,
            seen_onmyoji,
            "self_confirm_locked",
        )

    # After round six, three identical observations of vanished controls are
    # debounced again by DuelBPStateMachine.  A single missed template cannot
    # therefore produce READY.
    if (
        seen_onmyoji
        and not signals.selection_controls_visible
        and previous_state
        in (DuelBPState.SELF_PICK, DuelBPState.OPPONENT_PICK, DuelBPState.READY)
    ):
        return DuelBPPhaseClassification(
            DuelBPState.READY,
            0.90,
            seen_onmyoji,
            "round_six_controls_disappeared",
        )

    # Opponent text reports only the other player's progress.  If our own
    # confirmation control was missed, it cannot prove that our turn ended or
    # that round six is READY.  Keep the raw signal for progress metadata but
    # do not assign it phase authority.
    opponent_status = max(
        signals.opponent_selecting,
        signals.opponent_locked,
    )
    if opponent_status > 0:
        return DuelBPPhaseClassification(
            None,
            0.0,
            seen_onmyoji,
            "opponent_status_without_own_control",
        )

    # These are deliberately non-action classifications.  They retain useful
    # live state without ever granting AUTO permission to click.
    weak_opponent = max(
        signals.onmyoji_selection,
        signals.weak_selection,
        signals.legacy_action,
    )
    if weak_opponent > 0:
        return DuelBPPhaseClassification(
            DuelBPState.OPPONENT_PICK,
            weak_opponent,
            seen_onmyoji,
            "selection_without_active_confirm",
        )

    return DuelBPPhaseClassification(
        None, 0.0, seen_onmyoji, "no_phase_evidence"
    )
