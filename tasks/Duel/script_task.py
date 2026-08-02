# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from time import monotonic, sleep

import json
import random
from datetime import time, datetime, timedelta, timezone

from module.logger import logger
from module.exception import TaskEnd
from module.base.timer import Timer

from tasks.Component.GeneralBattle.general_battle import GeneralBattle
from tasks.Component.SwitchOnmyoji.switch_onmyoji import SwitchOnmyoji
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_duel, page_onmyodo, random_click
from tasks.Duel.config import Duel
from tasks.Duel.assets import DuelAssets
from tasks.Duel.bp import (
    BPObservation,
    build_pick_payload,
    DuelBPAssistant,
    DuelDraftLedger,
    DuelBPMode,
    DuelBPState,
    DuelRecommendation,
    DuelRecommendationCandidate,
    RecommendationSource,
)
from tasks.Duel.phase import DuelBPPhaseSignals, classify_bp_phase
from tasks.Duel.identity import (
    DuelIdentityAdapter,
    DuelIdentityObservation,
    ShikigamiNameIndex,
    parse_identity_regions,
)
from tasks.Duel.live import DuelLivePublisherMixin
from tasks.Duel.selection import (
    OPPONENT_IDENTITY_ROIS,
    OPPONENT_REVEAL_NAME_ROI,
    OPPONENT_SLOT_CENTERS,
    SELECTED_NAME_ROI,
    VisibleCandidate,
    candidate_name_roi,
    candidate_page_fingerprint,
    choose_visible_candidate,
    crop_reference_roi,
    crop_xywh,
    detect_candidate_base_x,
    onmyoji_click_point,
)
from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from tasks.GameUi.page import page_main, page_shikigami_records

""" 斗技 """


class _BPActionWindowLost(RuntimeError):
    """Internal signal for a scan that lost its safe action window."""


class ScriptTask(DuelLivePublisherMixin, GameUi, GeneralBattle, SwitchSoul, DuelAssets, SwitchOnmyoji):
    # TODO: 斗技适配页面模块

    battle_win_count = 0
    battle_lose_count = 0
    current_score = 0
    pre_battle_win_cnt = battle_win_count
    pre_battle_lose_cnt = battle_lose_count
    is_celeb: bool = False  # 是否是名仕
    conf: Duel = None

    def run(self):
        current_time = datetime.now().time()
        if not (time(12, 00) <= current_time < time(23, 00)):
            self.set_next_run(task='Duel', success=True, finish=False)
            raise TaskEnd('Duel')
        self.conf = self.config.duel
        limit_time = self.conf.duel_config.limit_time
        self.limit_time: timedelta = timedelta(hours=limit_time.hour, minutes=limit_time.minute,
                                               seconds=limit_time.second)
        self.prepare_duel()
        while True:
            self.screenshot()
            self.check_and_get_reward()
            if not self.duel_main():
                self.goto_page(page_duel)
                continue
            if not self.can_start_duel():
                break
            self.start_duel()
        logger.info('Duel battle end')
        self.goto_page(page_main)
        self.set_next_run(task='Duel', success=True, finish=True)
        raise TaskEnd('Duel')

    def prepare_duel(self):
        """斗技准备工作(切换御魂or阴阳师...), 最后回到斗技主界面"""
        self.goto_page(page_main)
        self.switch_soul()
        if self.conf.duel_config.switch_enabled:
            self.goto_page(page_onmyodo)
            self.switch_onmyoji(self.conf.duel_config.switch_onmyoji)
        self.goto_page(page_duel)
        self.switch_all_soul()
        self.current_score = self.conf.duel_celeb_config.initial_score
        self.bp_assistant = self.create_bp_assistant()
        self.bp_last_decision = None
        self._bp_last_observation = None
        self._bp_result_published = False
        self._bp_last_published_state = None
        self._bp_last_published_recommendation = None
        self._bp_last_published_action = None
        self._bp_own_picks: tuple[str, ...] = ()
        self._bp_opponent_picks: tuple[str | None, ...] = ()
        self._bp_bans: tuple[str, ...] = ()
        self._bp_ledger = DuelDraftLedger()
        self._bp_opponent_slot_meta = [
            {
                'slot': slot,
                'shikigami_id': None,
                'confidence': 0.0,
                'source': '',
                'status': 'pending',
            }
            for slot in range(1, 6)
        ]
        self._bp_selected_onmyoji = None
        self._bp_onmyoji_action_issued = False
        self._bp_onmyoji_attempts = 0
        self._bp_name_index = None
        self._bp_names_by_id = {}
        self._bp_name_ocr_model = None
        self._bp_strict_name_recognizer = None
        self._bp_portrait_matcher = None
        self._bp_portrait_matcher_disabled = False
        self._bp_selection_in_progress = False
        self._bp_pending_source = None
        self._bp_manual_name_candidate = None
        self._bp_manual_name_frames = 0
        self._bp_manual_pending_verified = False
        self._bp_manual_baseline_round = None
        self._bp_manual_baseline_id = None
        self._bp_manual_selection_changed = False
        self._bp_manual_tracking_blocked = False
        self._bp_candidate_provider = None
        self._bp_candidate_cache_key = None
        self._bp_candidate_cache = None
        self._bp_identity_adapter = None
        self._bp_identity_tracker = None
        self._bp_identity_disabled = False
        self._bp_identity_missing_in_selection = False
        self._bp_last_identity = DuelIdentityObservation()
        self._bp_seen_onmyoji_selection = False
        self._bp_onmyoji_signal_frames = 0
        self._bp_last_phase_signals = None
        self._bp_reveal_pair_round = None
        self._bp_reveal_pair_ids = None
        self._bp_reveal_pair_names = None
        self._bp_reveal_pair_frames = 0
        self._bp_reveal_pair_confidence = 0.0
        self._bp_reveal_episode_consumed = False
        self._bp_confirm_active_seen = False
        self._bp_confirmed_self_rounds = 0
        self._bp_confirmed_opponent_rounds = 0
        self._bp_sample_collector = None
        if self.conf.duel_config.bp_sample_capture_enabled:
            try:
                from tasks.Duel.sample_capture import DuelBPSampleCollector

                self._bp_sample_collector = DuelBPSampleCollector(
                    config_name=str(
                        getattr(self.config, 'config_name', 'default')
                    ),
                    interval_seconds=(
                        self.conf.duel_config.bp_sample_capture_interval
                    ),
                )
            except Exception as exc:
                logger.warning(
                    f'Unable to initialize Duel BP sample capture: {exc!r}'
                )
        self._duel_aborted_before_battle = False
        self._duel_abort_reason = None

    def can_start_duel(self) -> bool:
        """是否可以运行斗技"""
        # 任务执行时间超过限制时间，退出
        if datetime.now() - self.start_time >= self.limit_time:
            logger.info('Duel task is over time')
            return False
        # 当前分数跟目标分数比较, 判断分数是否已经满足条件
        if self.get_and_update_cur_score() >= self.conf.duel_config.target_score:
            logger.info('Duel task is over score')
            return False
        # 若不开启名仕战斗, 则到达名士直接退出
        if not self.conf.duel_celeb_config.celeb_battle and self.is_celeb:
            logger.info('You are already a celeb（名仕）')
            return False
        # 练习
        if self.appear(self.I_BATTLE_WITH_TRAIN) or self.appear(self.I_BATTLE_WITH_TRAIN2):
            return False
        # 荣誉满了，退出
        if self.conf.duel_config.honor_full_exit and self.check_honor():
            logger.info('Duel task is over honor')
            return False
        return True

    def start_duel(self):
        """进行一次斗技"""
        logger.hr('Duel battle', 2)
        self._duel_aborted_before_battle = False
        self._duel_abort_reason = None
        self.current_count += 1
        battle_started_at = datetime.now(timezone.utc)
        self.enter_battle()
        self.battle_prepare()
        battle_ret = self.wait_battle()
        if self._duel_aborted_before_battle:
            battle_ret = None
        if battle_ret is True:
            self.pre_battle_win_cnt = self.battle_win_count
            self.battle_win_count += 1
        elif battle_ret is False:
            self.pre_battle_lose_cnt = self.battle_lose_count
            self.battle_lose_count += 1
        task_run_time_seconds = timedelta(seconds=int((datetime.now() - self.start_time).total_seconds()))
        logger.info(f'battle result: {battle_ret}')
        logger.info(f'battle count:{self.current_count} | win:{self.battle_win_count} failure:{self.battle_lose_count}')
        logger.info(f'battle time: {task_run_time_seconds} / {self.limit_time}')
        self.record_duel_match(
            battle_started_at=battle_started_at,
            battle_result=battle_ret,
        )
        self.goto_page(page_duel)

    def enter_battle(self):
        """点击开始战斗(一直到出现战斗准备界面)"""
        logger.hr('duel battle matching')
        while not self.is_in_battle_prepare():
            self.screenshot()
            # 战斗按钮
            self.ui_click_until_disappear(self.I_D_BATTLE, interval=1.2)
            self.ui_click_until_disappear(self.I_D_BATTLE2, interval=1.2)
            # 战斗带保护的按钮
            self.ui_click_until_disappear(self.I_D_BATTLE_PROTECT, interval=1.2)

    def battle_prepare(self):
        """选式神准备斗技阶段"""
        logger.hr('duel battle preparing')
        self.bp_assistant.reset()
        self.bp_last_decision = None
        self._bp_last_observation = None
        self._bp_result_published = False
        self._bp_last_published_state = None
        self._bp_last_published_recommendation = None
        self._bp_last_published_action = None
        self._bp_own_picks = ()
        self._bp_opponent_picks = ()
        self._bp_bans = ()
        self._bp_ledger.reset()
        self._bp_opponent_slot_meta = [
            {
                'slot': slot,
                'shikigami_id': None,
                'confidence': 0.0,
                'source': '',
                'status': 'pending',
            }
            for slot in range(1, 6)
        ]
        self._bp_selected_onmyoji = None
        self._bp_onmyoji_action_issued = False
        self._bp_onmyoji_attempts = 0
        self._bp_selection_in_progress = False
        self._bp_pending_source = None
        self._bp_manual_name_candidate = None
        self._bp_manual_name_frames = 0
        self._bp_manual_pending_verified = False
        self._bp_manual_baseline_round = None
        self._bp_manual_baseline_id = None
        self._bp_manual_selection_changed = False
        self._bp_manual_tracking_blocked = False
        self._bp_candidate_cache_key = None
        self._bp_candidate_cache = None
        self._bp_identity_missing_in_selection = False
        self._bp_last_identity = DuelIdentityObservation()
        self._bp_seen_onmyoji_selection = False
        self._bp_onmyoji_signal_frames = 0
        self._bp_last_phase_signals = None
        self._bp_reveal_episode_consumed = False
        self._reset_bp_reveal_pair_window()
        self._bp_confirm_active_seen = False
        self._bp_confirmed_self_rounds = 0
        self._bp_confirmed_opponent_rounds = 0
        if self._bp_sample_collector is not None:
            try:
                session_dir = self._bp_sample_collector.start_session()
                logger.info(f'Duel BP sample capture: {session_dir}')
            except Exception as exc:
                self._bp_sample_collector = None
                logger.warning(
                    f'Unable to start Duel BP sample capture: {exc!r}'
                )
        if self._bp_identity_tracker is not None:
            try:
                self._bp_identity_tracker.clear_tracks()
            except Exception:
                pass
        not_in_prepare_cnt, max_retry = 0, 3
        while True:
            if not_in_prepare_cnt >= max_retry:  # max_retry次识别不到任何阶段元素(准备,战斗,结算), 退出
                break
            self.screenshot()
            # Observe before either prepare-loop exit path so the live state
            # lifecycle reaches BATTLE/RESULT instead of stopping at READY.
            suppress_legacy = self.handle_bp_assistant()
            if self.is_battle_end() or self.is_in_real_battle():  # 战斗已经结束或已经开始战斗
                break
            if not self.is_in_battle_prepare():  # 一般不会出现这种情况(不在准备,战斗,结束界面), 但是处理一下
                not_in_prepare_cnt += 1
                sleep(random.uniform(1.2, 2.4))
                continue
            not_in_prepare_cnt = 0
            # Assistant modes own the selection loop. OBSERVE and RECOMMEND
            # are strictly passive; AUTO may only use the explicit fallback
            # returned by handle_bp_assistant. Legacy OCR/click/exit behavior
            # is confined to OFF.
            if self.bp_assistant.mode != DuelBPMode.OFF:
                if self.appear(self.I_BAN):
                    self.is_celeb = True
                if suppress_legacy:
                    continue
                if self.bp_assistant.mode == DuelBPMode.AUTO and (
                    self.appear_then_click(self.I_D_AUTO_ENTRY, interval=1.2)
                    or self.appear_then_click(self.I_D_PREPARE, interval=1.2)
                ):
                    self.reset_device('PREPARE_BEFORE_BATTLE')
                continue
            # 再次检查是否是名仕(若斗技主界面识别名仕失效的话)
            if self.appear_then_click(self.I_BAN, interval=1.2):
                self.is_celeb = True
                continue
            # 名仕不开启自动上阵, 根据最后一个式神的名字是否改变来检查自己式神是否被ban
            if (
                not self.appear(self.I_D_CHECK_BAN, interval=0.8)
                and self.is_celeb
            ):
                ocr_name = self.O_D_BAN_NAME.ocr(self.device.image)
                shikigami_banned = ocr_name != '' and not any(
                    char in ocr_name for char in self.conf.duel_celeb_config.ban_name)
                logger.info(f'Check self shikigami is banned:{shikigami_banned}')
                if shikigami_banned:
                    self.duel_exit_battle(
                        abort_before_battle=True,
                        reason='configured_pick_banned',
                    )
                    continue
                self.click(self.C_DUEL_CLICK_5, interval=random.uniform(0.7, 1.4))
                sleep(random.uniform(1.5, 3))  # 降低点击频率和ocr识别频率
                continue
            # The assistant either handles this frame or deliberately falls
            # through to the legacy auto-entry behavior.
            if suppress_legacy:
                continue
            # 点击自动上阵或准备
            if self.appear_then_click(self.I_D_AUTO_ENTRY, interval=1.2) or \
                    self.appear_then_click(self.I_D_PREPARE, interval=1.2):
                self.reset_device('PREPARE_BEFORE_BATTLE')

    def create_bp_assistant(self) -> DuelBPAssistant:
        config = self.conf.duel_config
        mode = config.bp_mode
        if mode == DuelBPMode.AUTO:
            failures = self._bp_auto_prerequisite_failures()
            if failures:
                logger.warning(
                    'Duel BP AUTO is fail-closed; using recommend mode: '
                    + ', '.join(failures)
                )
                mode = DuelBPMode.RECOMMEND
        recommend_confidence = config.bp_recommend_confidence
        auto_confidence = config.bp_auto_confidence
        if auto_confidence < recommend_confidence:
            logger.warning(
                'Duel BP auto confidence is below recommendation confidence; '
                'using recommendation confidence for auto mode'
            )
            auto_confidence = recommend_confidence
        return DuelBPAssistant(
            mode=mode,
            stable_frames=config.bp_stable_frames,
            recommend_confidence=recommend_confidence,
            auto_confidence=auto_confidence,
            personal_min_samples=config.bp_personal_min_samples,
        )

    def _bp_auto_prerequisite_failures(self) -> tuple[str, ...]:
        """Return persistent/material gates that must pass before AUTO."""

        from pathlib import Path

        from module.duel_data.repository import DuelRepository
        from tasks.Duel.name_recognition import normalize_shishen_assets

        config = self.conf.duel_config
        failures = []
        if not config.bp_auto_verified:
            failures.append('offline_and_recommend_validation_missing')

        project_root = Path(__file__).resolve().parents[2]
        canonical_library_root = (
            project_root / 'config' / 'duel' / 'portrait_library'
        ).resolve()
        configured_library_root = Path(config.bp_portrait_library).expanduser()
        if not configured_library_root.is_absolute():
            configured_library_root = project_root / configured_library_root
        configured_library_root = configured_library_root.resolve()
        if configured_library_root != canonical_library_root:
            failures.append('portrait_library_not_canonical')
        expected_assets: dict[str, str] = {}
        try:
            assets = DuelRepository(
                project_root / 'config' / 'duel' / 'duel.sqlite3'
            ).latest_snapshot('shishen_assets')
            assets = normalize_shishen_assets(assets)
            expected_assets = {
                str(item['id']): str(item.get('name') or '').strip()
                for item in assets
                if item.get('id') is not None
                and str(item.get('name') or '').strip()
            }
            if not expected_assets:
                failures.append('shishen_assets_unavailable')
        except (OSError, TypeError, ValueError):
            failures.append('shishen_assets_unavailable')

        return tuple(failures)

    def recognize_bp_observation(self) -> BPObservation:
        """Recognize current Duel BP phase and only locked spatial slots."""
        signal_groups = (
            ('result', (self.I_D_VICTORY, self.I_D_FAIL, self.I_WIN, self.I_FALSE)),
            ('battle', (self.I_BATTLE_INFO,)),
            ('ban', (self.I_BAN, self.I_D_CHECK_BAN)),
            ('confirm_active', (self.I_D_BP_CONFIRM_ACTIVE,)),
            ('confirm_locked', (self.I_D_BP_CONFIRMED,)),
            ('opponent_selecting', (self.I_D_BP_OPPONENT_SELECTING,)),
            ('opponent_locked', (self.I_D_BP_OPPONENT_CONFIRMED,)),
            ('onmyoji_selection', (self.I_D_BP_ONMYOJI_SELECT,)),
            ('lineup_reveal', (self.I_D_BP_READY,)),
            # These legacy assets are presence/action hints only. They must
            # never decide READY or current turn over the dedicated controls.
            ('weak_selection', (self.I_D_WORD_BATTLE,)),
            ('legacy_action', (self.I_D_PREPARE,)),
        )
        rules = [rule for _, group in signal_groups for rule in group]
        try:
            from module.image.rpc import get_image_client

            results = get_image_client().match_many(
                rules_data=[rule.to_service_payload() for rule in rules],
                image=self.device.image,
                frame_id=self.device.image_frame_id,
            )
            self.device.update_image_batch_cache(
                rules,
                results,
                frame_id=self.device.image_frame_id,
            )
        except Exception as exc:
            # Recognition must fail closed. In particular, AUTO must never
            # turn a transport error into an artificial 1.0 confidence.
            logger.warning(f'Unable to recognize Duel BP frame: {exc!r}')
            self._bp_last_phase_signals = None
            return BPObservation(
                self.bp_assistant.state_machine.state,
                confidence=0.0,
                own_picks=self._bp_own_picks,
                opponent_picks=self._bp_opponent_picks,
                bans=self._bp_bans,
            )

        signal_scores = {}
        result_index = 0
        for signal_name, group in signal_groups:
            signal_score = 0.0
            for rule in group:
                result = results[result_index] if result_index < len(results) else {}
                result_index += 1
                if rule._apply_match_result(result):
                    signal_score = max(
                        signal_score, float(result.get('score') or 0.0)
                    )
            signal_scores[signal_name] = min(signal_score, 1.0)

        signal_scores['onmyoji_selection'] = (
            self._stable_bp_onmyoji_signal(
                signal_scores.get('onmyoji_selection', 0.0)
            )
        )
        signals = DuelBPPhaseSignals(**signal_scores)
        self._bp_last_phase_signals = signals
        self._update_bp_confirmation_progress(signals)
        previous_state = self.bp_assistant.state_machine.state
        classification = classify_bp_phase(
            signals,
            previous_state=previous_state,
            seen_onmyoji_selection=self._bp_seen_onmyoji_selection,
        )
        self._bp_seen_onmyoji_selection = (
            classification.seen_onmyoji_selection
        )
        recognized_state = classification.state
        recognized_confidence = classification.confidence
        if recognized_state is not None:
            # Our side is authoritative only after OAS itself clicked,
            # verified and confirmed a candidate. Opponent positions are kept
            # nullable in the draft ledger. Neither side depends on the old
            # generic top-row detector, whose missing boxes used to reduce a
            # perfectly valid phase confidence to zero.
            ledger = getattr(self, '_bp_ledger', None)
            own_picks = (
                ledger.own_picks if ledger is not None else self._bp_own_picks
            )
            opponent_picks = (
                ledger.opponent_context
                if ledger is not None
                else self._bp_opponent_picks
            )

            # Keep the old detector only as optional sample metadata. It may
            # never alter phase, recommendation or AUTO action confidence.
            if getattr(self, '_bp_sample_collector', None) is not None:
                self._bp_last_identity = self.recognize_bp_identities(
                    recognized_state
                )
            return BPObservation(
                recognized_state,
                confidence=recognized_confidence,
                own_picks=own_picks,
                opponent_picks=opponent_picks,
                # Ban recognition is intentionally excluded from the new
                # recommendation context.
                bans=(),
            )
        return BPObservation(
            self.bp_assistant.state_machine.state,
            confidence=0.0,
            own_picks=self._bp_own_picks,
            opponent_picks=self._bp_opponent_picks,
            bans=self._bp_bans,
        )

    def _stable_bp_onmyoji_signal(self, score: float) -> float:
        """Admit the round-six roster only in context and after three frames."""

        ledger = getattr(self, '_bp_ledger', None)
        expected = bool(
            ledger is not None
            and (
                ledger.completed
                or (
                    len(ledger.own_picks) == 4
                    and ledger.pending_own_pick is not None
                )
            )
        )
        normalized = max(0.0, min(float(score or 0.0), 1.0))
        if not expected or normalized <= 0:
            self._bp_onmyoji_signal_frames = 0
            return 0.0
        self._bp_onmyoji_signal_frames += 1
        if self._bp_onmyoji_signal_frames < 3:
            return 0.0
        return normalized

    def _update_bp_confirmation_progress(
        self, signals: DuelBPPhaseSignals
    ) -> None:
        """Track locked rounds without treating pre-filled portraits as picks."""

        active = signals.confirm_active > 0
        if active:
            # Reaching the next active round proves both sides completed all
            # earlier rounds, even if a short reveal hid opponent status text.
            self._bp_confirmed_opponent_rounds = max(
                self._bp_confirmed_opponent_rounds,
                self._bp_confirmed_self_rounds,
            )
            self._bp_confirm_active_seen = True

        if signals.opponent_locked > 0:
            current_round = self._bp_confirmed_self_rounds + (1 if active else 0)
            self._bp_confirmed_opponent_rounds = max(
                self._bp_confirmed_opponent_rounds,
                min(current_round, 6),
            )

        if signals.confirm_locked > 0 and self._bp_confirm_active_seen:
            self._bp_confirmed_self_rounds = min(
                self._bp_confirmed_self_rounds + 1, 6
            )
            self._bp_confirm_active_seen = False

    def recognize_bp_identities(self, state: DuelBPState) -> DuelIdentityObservation:
        """Recognize selected shikigami with OAS's own tracker and ID map."""
        config = self.conf.duel_config
        if (
            not config.bp_identity_enabled
            or self._bp_identity_disabled
            or state in (DuelBPState.BATTLE, DuelBPState.RESULT)
        ):
            return DuelIdentityObservation()
        try:
            if self._bp_identity_adapter is None:
                from module.duel_data.repository import DuelRepository
                from oashya.labels import id2name
                from oashya.tracker import Tracker
                from tasks.Duel.name_recognition import (
                    normalize_shishen_assets,
                )

                assets = normalize_shishen_assets(
                    DuelRepository().latest_snapshot('shishen_assets')
                )
                if not assets:
                    raise RuntimeError(
                        'Duel identity recognition requires a shishen mapping snapshot'
                    )
                regions = parse_identity_regions(config.bp_identity_regions)
                self._bp_identity_adapter = DuelIdentityAdapter(
                    ShikigamiNameIndex(assets),
                    name_resolver=id2name,
                    regions=regions,
                    minimum_confidence=config.bp_identity_confidence,
                )
                self._bp_identity_tracker = Tracker(
                    args={
                        'conf_threshold': config.bp_identity_confidence,
                        'iou_threshold': 0.45,
                        'precision': 'fp32',
                        'inference_engine': 'onnxruntime',
                        'debug': False,
                    }
                )
            detections = self._bp_identity_tracker(
                image=self.device.image,
                response=[0, 0, False, 10],
            )
            return self._bp_identity_adapter.recognize(
                detections,
                state=state.value,
                own_locked_slots=min(self._bp_confirmed_self_rounds, 5),
                opponent_locked_slots=min(
                    self._bp_confirmed_opponent_rounds, 5
                ),
            )
        except Exception as exc:
            # Disable for this Duel task after the first failure. Phase
            # recognition and the legacy auto-entry fallback remain usable.
            self._bp_identity_disabled = True
            logger.warning(f'Duel identity recognition is unavailable: {exc!r}')
            return DuelIdentityObservation()

    def _bp_candidate_groups(self, observation: BPObservation) -> dict:
        pool_ids = self._bp_shishen_pool_ids()
        cache_key = (observation.fingerprint, tuple(sorted(pool_ids)))
        if cache_key == self._bp_candidate_cache_key and self._bp_candidate_cache is not None:
            return self._bp_candidate_cache
        try:
            if self._bp_candidate_provider is None:
                from module.duel_data import DuelDataCandidateProvider
                from module.duel_data.repository import DuelRepository

                self._bp_candidate_provider = DuelDataCandidateProvider(
                    DuelRepository(),
                    personal_min_samples=self.conf.duel_config.bp_personal_min_samples,
                )
            raw_groups = self._bp_candidate_provider.get_candidates(
                own_picks=observation.own_picks,
                opponent_picks=observation.opponent_picks,
                bans=observation.bans,
            )
        except Exception as exc:
            logger.warning(f'Unable to load Duel BP candidates: {exc!r}')
            raw_groups = {}

        groups = {'rules': (), 'personal': (), 'external': ()}
        for group_name in groups:
            converted = []
            for item in raw_groups.get(group_name, ()):
                try:
                    shikigami_id = str(item['shikigami_id'])
                    if pool_ids and shikigami_id not in pool_ids:
                        continue
                    if shikigami_id in observation.unavailable_shikigami:
                        continue
                    converted.append(
                        DuelRecommendationCandidate(
                            shikigami_id=shikigami_id,
                            source=RecommendationSource(str(item['source'])),
                            score=float(item.get('score', 0.0)),
                            confidence=float(item.get('confidence', 0.0)),
                            priority=int(item.get('priority', 0)),
                            sample_size=int(item.get('sample_size', 0)),
                            reason=str(item.get('reason') or ''),
                            context_level=item.get('context_level'),
                            context_sample_size=int(
                                item.get('context_sample_size', 0)
                            ),
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning(f'Ignoring invalid Duel BP candidate: {exc!r}')
            groups[group_name] = tuple(converted)
        self._bp_candidate_cache_key = cache_key
        self._bp_candidate_cache = groups
        return groups

    def _bp_shishen_pool_ids(self) -> frozenset[str]:
        values = getattr(self.conf.duel_config, 'bp_shishen_pool', ())
        return frozenset(str(int(value)) for value in values)

    def _filter_bp_recommendations_by_pool(
        self,
        recommendations: tuple[DuelRecommendation, ...],
    ) -> tuple[DuelRecommendation, ...]:
        pool_ids = self._bp_shishen_pool_ids()
        unavailable = self._bp_ledger.unavailable_ids
        return tuple(
            item
            for item in recommendations
            if (
                (not pool_ids or str(item.shikigami_id) in pool_ids)
                and str(item.shikigami_id) not in unavailable
            )
        )

    def bp_rule_candidates(
        self, observation: BPObservation
    ) -> tuple[DuelRecommendationCandidate, ...]:
        """Return matching candidates from the user's strategy rules."""
        return self._bp_candidate_groups(observation)['rules']

    def bp_personal_candidates(
        self, observation: BPObservation
    ) -> tuple[DuelRecommendationCandidate, ...]:
        """Return candidates calculated from the user's own match history."""
        return self._bp_candidate_groups(observation)['personal']

    def bp_external_candidates(
        self, observation: BPObservation
    ) -> tuple[DuelRecommendationCandidate, ...]:
        """Return one-time external cold-start candidates, if available."""
        return self._bp_candidate_groups(observation)['external']

    def _sync_bp_ledger(self) -> None:
        ledger = self._bp_ledger
        self._bp_own_picks = ledger.own_picks
        self._bp_opponent_picks = ledger.opponent_context
        # Any accepted context change must invalidate the provider cache.
        self._bp_candidate_cache_key = None
        self._bp_candidate_cache = None

    def _bp_round(self) -> int:
        ledger = getattr(self, '_bp_ledger', None)
        return ledger.round_number if ledger is not None else 1

    def _ensure_bp_name_index(self) -> None:
        if self._bp_name_index is not None:
            return
        from module.duel_data.repository import DuelRepository
        from tasks.Duel.name_recognition import normalize_shishen_assets

        assets = normalize_shishen_assets(
            DuelRepository().latest_snapshot('shishen_assets')
        )
        if not assets:
            raise RuntimeError(
                'Duel BP requires the local shishen_assets snapshot'
            )
        self._bp_name_index = ShikigamiNameIndex(assets)
        self._bp_names_by_id = {
            str(item['id']): str(item.get('name') or item['id'])
            for item in assets
            if isinstance(item, dict) and item.get('id') is not None
        }

    def _bp_name(self, shikigami_id: object | None) -> str:
        if shikigami_id in (None, '', 0, '0'):
            return 'unknown'
        try:
            self._ensure_bp_name_index()
        except Exception:
            return str(shikigami_id)
        return self._bp_names_by_id.get(
            str(shikigami_id), str(shikigami_id)
        )

    def _bp_opponent_slots_payload(self) -> list[dict]:
        meta_by_slot = {
            int(item['slot']): item
            for item in getattr(self, '_bp_opponent_slot_meta', ())
        }
        ledger = getattr(self, '_bp_ledger', None)
        values = (
            ledger.opponent_slots
            if ledger is not None
            else tuple(self._bp_opponent_picks) + (None,) * 5
        )
        payload = []
        for slot in range(1, 6):
            value = values[slot - 1] if slot <= len(values) else None
            metadata = meta_by_slot.get(slot, {})
            wire_id = self._duel_shikigami_id(value)
            payload.append(
                {
                    'slot': slot,
                    'shishen_id': wire_id,
                    'shikigami_id': wire_id,
                    'confidence': float(
                        metadata.get('confidence') or 0.0
                    ),
                    'source': str(metadata.get('source') or ''),
                    'status': (
                        'recognized'
                        if wire_id is not None
                        else str(metadata.get('status') or 'pending')
                    ),
                }
            )
        return payload

    def _publish_bp_action(
        self,
        action: str,
        *,
        status: str,
        shikigami_id: object | None = None,
        confidence: float | None = None,
        message: str = '',
        round_number: int | None = None,
        **extra,
    ) -> None:
        if round_number is None:
            round_number = self._bp_round()
        key = (
            action,
            status,
            round_number,
            str(shikigami_id) if shikigami_id is not None else None,
            confidence,
            message,
            tuple(sorted((str(key), str(value)) for key, value in extra.items())),
        )
        if key == getattr(self, '_bp_last_published_action', None):
            return
        payload = {
            'action': action,
            'status': status,
            'state': self.bp_assistant.state_machine.state.value,
            'phase': self.bp_assistant.state_machine.state.value,
            'mode': self.bp_assistant.mode.value,
            'round': round_number,
            'shishen_id': self._duel_shikigami_id(shikigami_id),
            'confidence': confidence,
            'message': message,
            **extra,
        }
        self.publish_bp_live_event('action', payload)
        self._bp_last_published_action = key

    def _ensure_bp_name_recognizer(self) -> None:
        self._ensure_bp_name_index()
        if self._bp_name_ocr_model is None:
            from module.ocr.models import get_ocr_model

            self._bp_name_ocr_model = get_ocr_model("ch")
        if self._bp_strict_name_recognizer is None:
            from tasks.Duel.name_recognition import StrictNameRecognizer
            from module.duel_data.repository import DuelRepository

            assets = DuelRepository().latest_snapshot('shishen_assets')
            self._bp_strict_name_recognizer = StrictNameRecognizer(assets)

    def _recognize_bp_name_roi(
        self,
        roi: tuple[int, int, int, int],
        *,
        min_consensus: int = 2,
    ) -> dict:
        """Resolve one vertical name crop; uncertainty stays unknown.

        Candidate location is reversible, so an exact canonical/alias match
        may use one OCR crop. Irreversible confirmation keeps the default
        two-crop consensus and still requires three stable frames.
        """

        self._ensure_bp_name_recognizer()
        crop = crop_reference_roi(self.device.image, roi)
        candidate = self._bp_strict_name_recognizer.recognize(
            crop,
            self._bp_name_ocr_model,
            min_consensus=min_consensus,
        )
        resolved = candidate.resolved
        accepted = bool(
            candidate.accepted
            or (
                min_consensus == 1
                and resolved is not None
                and candidate.method == 'exact'
                and candidate.consensus >= 1
            )
        )
        result = {
            'shikigami_id': (
                resolved.shikigami_id if accepted else None
            ),
            'name': resolved.name if accepted else None,
            'confidence': (
                float(resolved.confidence) if accepted else 0.0
            ),
            'text': str(candidate.text or ''),
            'consensus': int(candidate.consensus),
            'method': str(candidate.method),
            'variant': str(candidate.variant),
        }
        if self.conf.duel_config.bp_log_raw_frames:
            logger.debug(
                'Duel BP raw name: '
                f'roi={roi} text={result["text"]!r} '
                f'id={result["shikigami_id"] or "unknown"} '
                f'confidence={result["confidence"]:.3f} '
                f'consensus={result["consensus"]}'
            )
        return result

    def _recognize_bp_name_stable(
        self,
        roi: tuple[int, int, int, int],
        *,
        expected_id: object | None = None,
        frames: int = 3,
        require_actionable: bool = False,
        min_confidence: float | None = None,
    ) -> dict | None:
        """Require the same strict name result on consecutive screenshots."""

        expected = str(expected_id) if expected_id is not None else None
        stable_id = None
        stable_results = []
        for frame_index in range(frames):
            if frame_index:
                sleep(0.25)
                self.screenshot()
            if require_actionable and not self._bp_actionable_self_pick():
                stable_id = None
                stable_results = []
                continue
            result = self._recognize_bp_name_roi(roi)
            current = result['shikigami_id']
            if current is None or (
                expected is not None and str(current) != expected
            ):
                stable_id = None
                stable_results = []
                continue
            if str(current) != stable_id:
                stable_id = str(current)
                stable_results = [result]
            else:
                stable_results.append(result)
        if len(stable_results) < frames:
            return None
        stable_confidence = min(
            item['confidence'] for item in stable_results
        )
        if (
            min_confidence is not None
            and stable_confidence < float(min_confidence)
        ):
            return None
        best = max(
            stable_results,
            key=lambda item: (
                item['confidence'],
                item['consensus'],
            ),
        )
        return {
            **best,
            # Stability is an independent requirement; it must not promote a
            # lower OCR score through the irreversible 0.98 AUTO gate.
            'confidence': stable_confidence,
            'stable_frames': frames,
        }

    def _reset_bp_reveal_pair_window(self) -> None:
        self._bp_reveal_pair_round = None
        self._bp_reveal_pair_ids = None
        self._bp_reveal_pair_names = None
        self._bp_reveal_pair_frames = 0
        self._bp_reveal_pair_confidence = 0.0

    def _finalize_bp_reveal_pair_if_ready(self) -> bool:
        """Atomically commit one stable, locked opponent reveal."""

        frames = int(getattr(self, '_bp_reveal_pair_frames', 0) or 0)
        round_number = getattr(self, '_bp_reveal_pair_round', None)
        pair = getattr(self, '_bp_reveal_pair_ids', None)
        names = getattr(self, '_bp_reveal_pair_names', None)
        if frames < 3 or round_number is None or pair is None:
            return False

        ledger = self._bp_ledger
        slot = int(round_number)
        if not 1 <= slot <= 5:
            self._bp_reveal_episode_consumed = True
            self._reset_bp_reveal_pair_window()
            return False
        if ledger.opponent_rounds_seen >= slot:
            self._bp_reveal_episode_consumed = True
            self._reset_bp_reveal_pair_window()
            return False
        if slot != ledger.opponent_rounds_seen + 1:
            return False
        own_id, opponent_id = (str(pair[0]), str(pair[1]))
        discovered_own = False
        if len(ledger.own_picks) < slot:
            if ledger.pending_own_pick is not None:
                # The pair can stabilize before the debounced phase
                # transition commits our pending pick. Keep it cached for
                # this same frame's post-transition finalization.
                return False
            if self.bp_assistant.mode not in (
                DuelBPMode.OBSERVE,
                DuelBPMode.RECOMMEND,
            ):
                # AUTO owns an authoritative click ledger and may never
                # invent our side from OCR after the fact.
                return False
            ledger.begin_own_pick(own_id)
            ledger.commit_pending_own_pick(expected_id=own_id)
            discovered_own = True
            if self.bp_assistant.mode == DuelBPMode.RECOMMEND:
                # A late reveal resolves the fail-closed "default highlight"
                # ambiguity and permits the following round to recommend
                # again from the now-complete paired ledger.
                self._bp_manual_tracking_blocked = False
                self._bp_manual_baseline_round = None
                self._bp_manual_baseline_id = None
                self._bp_manual_selection_changed = False
                self._bp_manual_name_candidate = None
                self._bp_manual_name_frames = 0
                self._bp_manual_pending_verified = False
        elif str(ledger.own_picks[slot - 1]) != own_id:
            self._reset_bp_reveal_pair_window()
            return False

        confidence = float(
            getattr(self, '_bp_reveal_pair_confidence', 0.0) or 0.0
        )
        source = (
            'reveal_name'
            if confidence >= self.conf.duel_config.bp_auto_confidence
            else 'reveal_name_relaxed'
        )
        ledger.record_opponent_pick(slot, opponent_id)
        if discovered_own:
            own_name = (
                str(names[0])
                if names is not None and names[0]
                else self._bp_name(own_id)
            )
            logger.info(
                f'BP[round={slot}][own] slot={slot} id={own_id} '
                f'name={own_name} confidence={confidence:.3f} '
                f'source={source} confirmed=true'
            )
        opponent_name = (
            str(names[1])
            if names is not None and len(names) > 1 and names[1]
            else self._bp_name(opponent_id)
        )
        self._bp_opponent_slot_meta[slot - 1] = {
            'slot': slot,
            'shikigami_id': opponent_id,
            'confidence': confidence,
            'source': source,
            'status': 'recognized',
        }
        self._sync_bp_ledger()
        logger.info(
            f'BP[round={slot}][opponent] slot={slot} '
            f'id={opponent_id} name={opponent_name} '
            f'confidence={confidence:.3f} source={source}'
        )
        # A locked reveal commonly remains visible for many main-loop frames.
        # Consume the whole episode, not just its three-frame vote window, so
        # the same pair cannot be interpreted as the following round.  Only
        # a positively observed active confirmation control rearms tracking.
        self._bp_reveal_episode_consumed = True
        self._reset_bp_reveal_pair_window()
        return True

    def _observe_bp_reveal_pair_frame(self) -> bool:
        """Track both locked name ribbons once per main-loop screenshot.

        Unlike the blocking three-screenshot verifier used immediately before
        an irreversible click, this tracker reads both sides from the same
        frame. It therefore cannot combine a previous reveal with the next
        round while the drum/transition animation is running.
        """

        signals = getattr(self, '_bp_last_phase_signals', None)
        if signals is None:
            self._reset_bp_reveal_pair_window()
            return False

        # This is the only strong boundary between two reveal episodes.  A
        # missing/weak template frame must not rearm a consumed locked screen.
        if signals.confirm_active > 0 and signals.confirm_locked <= 0:
            self._bp_reveal_episode_consumed = False
            self._reset_bp_reveal_pair_window()
            return False

        if (
            getattr(self, '_bp_reveal_episode_consumed', False)
            or signals.confirm_locked <= 0
            or signals.confirm_active > 0
            or signals.opponent_selecting > 0
            or signals.opponent_locked > 0
            or self._bp_seen_onmyoji_selection
        ):
            self._reset_bp_reveal_pair_window()
            return False

        ledger = self._bp_ledger
        slot = ledger.opponent_rounds_seen + 1
        if not 1 <= slot <= 5:
            self._reset_bp_reveal_pair_window()
            return False

        pending = ledger.pending_own_pick
        if slot <= len(ledger.own_picks):
            expected_own_id = str(ledger.own_picks[slot - 1])
        elif slot == len(ledger.own_picks) + 1:
            if pending is not None:
                expected_own_id = str(pending)
            elif self.bp_assistant.mode in (
                DuelBPMode.OBSERVE,
                DuelBPMode.RECOMMEND,
            ):
                expected_own_id = None
            else:
                self._reset_bp_reveal_pair_window()
                return False
        else:
            self._reset_bp_reveal_pair_window()
            return False

        try:
            own = self._recognize_bp_name_roi(
                SELECTED_NAME_ROI,
                min_consensus=1,
            )
            opponent = self._recognize_bp_name_roi(
                OPPONENT_REVEAL_NAME_ROI,
                min_consensus=1,
            )
        except Exception as exc:
            self._reset_bp_reveal_pair_window()
            logger.warning(f'Duel BP reveal-name recognition failed: {exc!r}')
            return False

        own_id = own.get('shikigami_id')
        opponent_id = opponent.get('shikigami_id')
        own_confidence = float(own.get('confidence') or 0.0)
        opponent_confidence = float(opponent.get('confidence') or 0.0)
        if (
            own_id is None
            or opponent_id is None
            or not self._bp_reveal_name_vote_accepted(own)
            or not self._bp_reveal_name_vote_accepted(opponent)
        ):
            self._reset_bp_reveal_pair_window()
            return False

        own_id = str(own_id)
        opponent_id = str(opponent_id)
        if expected_own_id is not None and own_id != expected_own_id:
            if self.conf.duel_config.bp_log_raw_frames:
                logger.debug(
                    'Duel BP reveal-name mismatch: '
                    f'round={slot} expected_own={expected_own_id} '
                    f'observed_own={own_id} opponent={opponent_id}'
                )
            self._reset_bp_reveal_pair_window()
            return False

        pair = (own_id, opponent_id)
        pair_confidence = min(own_confidence, opponent_confidence)
        if (
            self._bp_reveal_pair_round == slot
            and self._bp_reveal_pair_ids == pair
        ):
            self._bp_reveal_pair_frames += 1
            self._bp_reveal_pair_confidence = min(
                self._bp_reveal_pair_confidence,
                pair_confidence,
            )
        else:
            self._bp_reveal_pair_round = slot
            self._bp_reveal_pair_ids = pair
            self._bp_reveal_pair_names = (
                str(own.get('name') or self._bp_name(own_id)),
                str(opponent.get('name') or self._bp_name(opponent_id)),
            )
            self._bp_reveal_pair_frames = 1
            self._bp_reveal_pair_confidence = pair_confidence

        if self.conf.duel_config.bp_log_raw_frames:
            logger.debug(
                'Duel BP raw reveal names: '
                f'round={slot} own={own_id} opponent={opponent_id} '
                f'confidence={pair_confidence:.3f} '
                f'stable={self._bp_reveal_pair_frames}/3'
            )
        return self._finalize_bp_reveal_pair_if_ready()

    def _bp_reveal_name_vote_accepted(self, result: dict) -> bool:
        """Accept low OCR scores only when spatial consensus is strong.

        The OCR service's probability describes the decorated glyph pixels,
        not the resolved identity. Real locked ribbons can score around 0.6
        even when two independent crops both contain the same registered
        name. Fuzzy identities keep the configured recommendation threshold;
        only exact/substring identities with two spatial votes get this
        bounded relaxation, and the caller still requires three temporal
        pair votes plus either an authoritative AUTO own-ID match or the
        non-clicking OBSERVE/RECOMMEND paired-reveal contract.
        """

        if result.get('shikigami_id') is None:
            return False
        confidence = float(result.get('confidence') or 0.0)
        configured = float(
            self.conf.duel_config.bp_recommend_confidence
        )
        if confidence >= configured:
            return True
        relaxed_floor = max(0.55, configured - 0.35)
        return bool(
            str(result.get('method') or '') in ('exact', 'substring')
            and int(result.get('consensus') or 0) >= 2
            and confidence >= relaxed_floor
        )

    def _bp_actionable_self_pick(self) -> bool:
        """Verify that the current frame is still our actionable BP turn."""

        try:
            observation = self.recognize_bp_observation()
            return bool(
                observation.state == DuelBPState.SELF_PICK
                and observation.confidence
                >= self.conf.duel_config.bp_auto_confidence
                and self.appear(self.I_D_BP_CONFIRM_ACTIVE)
            )
        except Exception as exc:
            logger.warning(
                f'Duel BP action-window verification failed: {exc!r}'
            )
            return False

    def _recognize_visible_bp_candidates(
        self,
    ) -> tuple[VisibleCandidate, ...]:
        base_x = detect_candidate_base_x(self.device.image)
        candidates = []
        for slot in range(1, 9):
            result = self._recognize_bp_name_roi(
                candidate_name_roi(slot, base_x=base_x),
                min_consensus=1,
            )
            shikigami_id = result['shikigami_id']
            confidence = float(result['confidence'])
            if (
                shikigami_id is None
                or (
                    result.get('method') != 'exact'
                    and confidence
                    < self.conf.duel_config.bp_auto_confidence
                )
            ):
                continue
            raw_text = result['text']
            candidates.append(
                VisibleCandidate(
                    slot=slot,
                    shikigami_id=str(shikigami_id),
                    name=str(result['name'] or shikigami_id),
                    confidence=confidence,
                    disabled='禁' in raw_text,
                    source='name',
                    base_x=base_x,
                )
            )
        return tuple(candidates)

    def _save_unresolved_opponent_portrait(
        self,
        slot: int,
        *,
        highest_candidate: str | None = None,
        confidence: float = 0.0,
    ) -> None:
        """Persist an unknown live portrait for correction without blocking BP."""

        try:
            import hashlib
            from pathlib import Path

            import cv2

            crop = crop_xywh(
                self.device.image, OPPONENT_IDENTITY_ROIS[slot - 1]
            )
            ok, encoded = cv2.imencode('.png', crop)
            if not ok:
                return
            content = encoded.tobytes()
            digest = hashlib.sha256(content).hexdigest()
            library = Path(self.conf.duel_config.bp_portrait_library)
            unresolved = library / '_unresolved'
            unresolved.mkdir(parents=True, exist_ok=True)
            image_path = unresolved / f'{digest}.png'
            if not image_path.exists():
                image_path.write_bytes(content)
            sidecar = unresolved / f'{digest}.json'
            if not sidecar.exists():
                sidecar.write_text(
                    json.dumps(
                        {
                            'slot': slot,
                            'view': '上阵',
                            'highest_candidate': highest_candidate,
                            'confidence': confidence,
                            'roi': list(OPPONENT_IDENTITY_ROIS[slot - 1]),
                        },
                        ensure_ascii=False,
                        separators=(',', ':'),
                    ),
                    encoding='utf-8',
                )
            from module.duel_data.repository import DuelRepository

            DuelRepository().upsert_portrait_template(
                {
                    'path': image_path.relative_to(library).as_posix(),
                    'view': '上阵',
                    'hash': digest,
                    'source': 'live_unknown',
                    'confidence': confidence,
                }
            )
        except Exception as exc:
            logger.warning(
                f'Unable to save unresolved Duel portrait: {exc!r}'
            )

    def _ensure_bp_portrait_matcher(self) -> None:
        if (
            self._bp_portrait_matcher is not None
            or self._bp_portrait_matcher_disabled
        ):
            return
        try:
            from tasks.Duel.portrait_library import PortraitMatcher

            self._bp_portrait_matcher = PortraitMatcher(
                self.conf.duel_config.bp_portrait_library
            )
        except Exception as exc:
            self._bp_portrait_matcher_disabled = True
            logger.warning(
                f'Duel portrait matcher is unavailable: {exc!r}'
            )

    @staticmethod
    def _bp_match_value(match, key: str, default=None):
        if isinstance(match, dict):
            return match.get(key, default)
        return getattr(match, key, default)

    def _recognize_opponent_portrait_stable(
        self, slot: int, *, frames: int = 3
    ) -> tuple[dict | None, str | None, float]:
        """Return a high-confidence three-frame portrait match and diagnostics."""

        self._ensure_bp_portrait_matcher()
        matcher = self._bp_portrait_matcher
        if matcher is None:
            return None, None, 0.0
        stable_id = None
        stable_matches = []
        highest_candidate = None
        highest_confidence = 0.0
        for frame_index in range(frames):
            if frame_index:
                sleep(0.25)
                self.screenshot()
            crop = crop_xywh(
                self.device.image, OPPONENT_IDENTITY_ROIS[slot - 1]
            )
            match = matcher.match(
                crop,
                views=('上阵', '阵容', '候选'),
            )
            current_id = self._bp_match_value(
                match, 'shikigami_id'
            )
            confidence = float(
                self._bp_match_value(match, 'confidence', 0.0) or 0.0
            )
            candidate = self._bp_match_value(
                match, 'highest_candidate'
            )
            if candidate is not None and confidence >= highest_confidence:
                highest_candidate = str(candidate)
                highest_confidence = confidence
            if self.conf.duel_config.bp_log_raw_frames:
                logger.debug(
                    'Duel BP raw portrait: '
                    f'slot={slot} id={current_id or "unknown"} '
                    f'confidence={confidence:.3f} '
                    f'highest={candidate or "unknown"}'
                )
            if current_id is None or confidence < 0.98:
                stable_id = None
                stable_matches = []
                continue
            current_id = str(current_id)
            if current_id != stable_id:
                stable_id = current_id
                stable_matches = [match]
            else:
                stable_matches.append(match)
        if len(stable_matches) < frames:
            return None, highest_candidate, highest_confidence
        best = max(
            stable_matches,
            key=lambda item: float(
                self._bp_match_value(item, 'confidence', 0.0) or 0.0
            ),
        )
        return (
            {
                'shikigami_id': str(
                    self._bp_match_value(best, 'shikigami_id')
                ),
                'name': str(
                    self._bp_match_value(best, 'name')
                    or self._bp_name(stable_id)
                ),
                'confidence': min(
                    float(
                        self._bp_match_value(
                            item, 'confidence', 0.0
                        )
                        or 0.0
                    )
                    for item in stable_matches
                ),
                'source': str(
                    self._bp_match_value(best, 'source', 'portrait')
                    or 'portrait'
                ),
            },
            highest_candidate,
            highest_confidence,
        )

    def _observe_next_bp_opponent(self, *, allow_click: bool = True) -> bool:
        """Resolve exactly one newly revealed opponent slot, or record unknown."""

        ledger = self._bp_ledger
        if ledger.opponent_rounds_seen >= len(ledger.own_picks):
            return False
        slot = ledger.opponent_rounds_seen + 1
        result = None
        highest_candidate = None
        highest_confidence = 0.0

        allow_inspect = (
            allow_click
            and self.conf.duel_config.bp_opponent_inspect_enabled
            and self.bp_assistant.mode
            in (DuelBPMode.RECOMMEND, DuelBPMode.AUTO)
            and ledger.pending_own_pick is None
        )
        if allow_inspect:
            try:
                x, y = OPPONENT_SLOT_CENTERS[slot - 1]
                self.device.click(
                    x=x,
                    y=y,
                    control_name=f'Duel_BP_Inspect_Opponent_{slot}',
                )
                sleep(0.25)
                self.screenshot()
                result = self._recognize_bp_name_stable(
                    SELECTED_NAME_ROI,
                    min_confidence=(
                        self.conf.duel_config.bp_auto_confidence
                    ),
                )
                if result is not None:
                    result = {**result, 'source': 'inspect'}
            except Exception as exc:
                logger.warning(
                    f'Duel BP opponent inspect failed for slot {slot}: {exc!r}'
                )

        if result is None:
            try:
                (
                    result,
                    highest_candidate,
                    highest_confidence,
                ) = self._recognize_opponent_portrait_stable(slot)
            except Exception as exc:
                logger.warning(
                    f'Duel BP portrait recognition failed for slot {slot}: '
                    f'{exc!r}'
                )

        shikigami_id = (
            str(result['shikigami_id']) if result is not None else None
        )
        ledger.record_opponent_pick(slot, shikigami_id)
        metadata = {
            'slot': slot,
            'shikigami_id': shikigami_id,
            'confidence': (
                float(result['confidence']) if result is not None else 0.0
            ),
            'source': (
                str(result.get('source') or 'portrait')
                if result is not None
                else 'unknown'
            ),
            'status': (
                'recognized' if result is not None else 'unresolved'
            ),
        }
        self._bp_opponent_slot_meta[slot - 1] = metadata
        self._sync_bp_ledger()

        if result is None:
            logger.info(
                f'BP[round={ledger.round_number}][opponent] '
                f'slot={slot} id=unknown '
                f'highest={highest_candidate or "unknown"} '
                f'confidence={highest_confidence:.3f} source=unknown'
            )
            self._save_unresolved_opponent_portrait(
                slot,
                highest_candidate=highest_candidate,
                confidence=highest_confidence,
            )
        else:
            logger.info(
                f'BP[round={ledger.round_number}][opponent] '
                f'slot={slot} id={shikigami_id} '
                f'name={result["name"]} '
                f'confidence={float(result["confidence"]):.3f} '
                f'source={metadata["source"]}'
            )
        return True

    def _scan_ranked_bp_candidate(
        self,
        recommendations: tuple[DuelRecommendation, ...],
    ) -> tuple[VisibleCandidate, int] | None:
        """Find the highest-ranked usable card with bounded horizontal scans."""

        pool_ids = self._bp_shishen_pool_ids()
        unavailable = self._bp_ledger.unavailable_ids
        ranked_ids = tuple(
            str(item.shikigami_id)
            for item in recommendations
            if (
                (not pool_ids or str(item.shikigami_id) in pool_ids)
                and str(item.shikigami_id) not in unavailable
            )
        )
        if not ranked_ids:
            return None
        max_pages = self.conf.duel_config.bp_candidate_swipe_limit + 1
        scan_budget = float(
            getattr(
                self.conf.duel_config,
                'bp_candidate_scan_budget',
                5.0,
            )
        )
        deadline = monotonic() + max(1.0, min(scan_budget, 8.0))
        seen_pages = set()
        for page_index in range(max_pages):
            candidates = self._recognize_visible_bp_candidates()
            fingerprint = candidate_page_fingerprint(
                candidates,
                self.device.image,
            )
            if fingerprint in seen_pages:
                break
            seen_pages.add(fingerprint)
            selected = choose_visible_candidate(
                ranked_ids,
                candidates,
                unavailable_ids=unavailable,
                allowed_ids=pool_ids if pool_ids else None,
            )
            if selected is not None:
                rank = ranked_ids.index(str(selected.shikigami_id))
                # Preserve enough of the turn for selected-name verification
                # and the irreversible confirm. Waiting across every page for
                # rank 1 can exceed the 16-second in-game timer even when a
                # valid rank 2--5 card is already visible.
                return selected, rank + 1
            if (
                page_index + 1 >= max_pages
                or monotonic() >= deadline
            ):
                break
            # A bounded scan still mutates the UI. Refresh and authorize every
            # individual swipe so a turn ending mid-scan cannot drag a later
            # Onmyoji/battle page at the same fixed coordinates.
            self.screenshot()
            if not self._bp_actionable_self_pick():
                raise _BPActionWindowLost(
                    'active BP confirm control disappeared during scan'
                )
            self.device.swipe(
                (930, 610),
                (330, 610),
                duration=0.35,
                control_name='Duel_BP_Candidate_Next',
            )
            sleep(0.4)
            self.screenshot()
        return None

    def _verify_bp_confirm_transition(self) -> bool:
        stable_key = None
        stable_frames = 0
        for _ in range(6):
            sleep(0.30)
            self.screenshot()
            observation = self.recognize_bp_observation()
            fifth_pick_reached_onmyoji = bool(
                len(self._bp_ledger.own_picks) == 4
                and self._bp_ledger.pending_own_pick is not None
                and self._bp_seen_onmyoji_selection
                and self.appear(self.I_D_BP_ONMYOJI_SELECT)
                and observation.state == DuelBPState.SELF_PICK
            )
            transition_key = (
                'ONMYOJI_SELECTION'
                if fifth_pick_reached_onmyoji
                else observation.state
            )
            if observation.confidence < (
                self.conf.duel_config.bp_auto_confidence
            ) or (
                not fifth_pick_reached_onmyoji
                and observation.state
                not in (
                    DuelBPState.OPPONENT_PICK,
                    DuelBPState.READY,
                    DuelBPState.BATTLE,
                )
            ):
                stable_key = None
                stable_frames = 0
                continue
            if transition_key == stable_key:
                stable_frames += 1
            else:
                stable_key = transition_key
                stable_frames = 1
            if stable_frames >= 3:
                return True
        return False

    def execute_bp_recommendations(
        self,
        recommendations: tuple[DuelRecommendation, ...],
    ) -> bool:
        """Scan, click, verify and commit one of the ranked Top-5 choices."""

        if self._bp_selection_in_progress:
            return True
        self._bp_selection_in_progress = True
        try:
            remaining = self._filter_bp_recommendations_by_pool(
                recommendations
            )[:5]
            issued_click = False
            ranked_candidate_seen = False
            while remaining:
                if not self._bp_actionable_self_pick():
                    self._publish_bp_action(
                        'scan_candidate',
                        status='action_window_lost',
                        message='Active BP confirm control is not available',
                    )
                    return True
                try:
                    found = self._scan_ranked_bp_candidate(remaining)
                except _BPActionWindowLost:
                    self._publish_bp_action(
                        'scan_candidate',
                        status='action_window_lost',
                        message=(
                            'Active BP confirm control disappeared '
                            'during candidate scan'
                        ),
                    )
                    return True
                if found is None:
                    self._publish_bp_action(
                        'scan_candidate',
                        status='not_found',
                        message='No ranked candidate was found in bounded scan',
                    )
                    return issued_click or ranked_candidate_seen
                candidate, rank = found
                ranked_candidate_seen = True
                target_id = str(candidate.shikigami_id)
                # Candidate scans may take several screenshots or swipes.
                # Re-check the dedicated active-confirm control immediately
                # before the first card click; phase classification alone is
                # not authorization for an irreversible UI action.
                if not self._bp_actionable_self_pick():
                    self._publish_bp_action(
                        'click_candidate',
                        status='action_window_lost',
                        shikigami_id=target_id,
                        confidence=candidate.confidence,
                        candidate_rank=rank,
                        selected_verified=False,
                        confirmed=False,
                    )
                    return True
                card_name = self._recognize_bp_name_roi(
                    candidate_name_roi(
                        candidate.slot,
                        base_x=candidate.base_x,
                    ),
                    min_consensus=1,
                )
                card_is_disabled = '禁' in card_name['text']
                card_identity = card_name['shikigami_id']
                if (
                    str(card_identity) != target_id
                    or (
                        card_name.get('method') != 'exact'
                        and float(card_name['confidence'])
                        < self.conf.duel_config.bp_auto_confidence
                    )
                    or card_is_disabled
                ):
                    self._bp_ledger.mark_unavailable(target_id)
                    logger.info(
                        f'BP[round={self._bp_ledger.round_number}]'
                        f'[action] skipped={target_id} '
                        f'reason={"disabled" if card_is_disabled else "name_unverified"}'
                    )
                    self._publish_bp_action(
                        'scan_candidate',
                        status=(
                            'disabled'
                            if card_is_disabled
                            else 'name_unverified'
                        ),
                        shikigami_id=target_id,
                        confidence=card_name['confidence'],
                        candidate_rank=rank,
                    )
                    remaining = tuple(
                        item
                        for item in remaining
                        if str(item.shikigami_id) != target_id
                    )
                    continue
                try:
                    self._bp_ledger.begin_own_pick(target_id)
                    self._bp_pending_source = 'auto'
                except (RuntimeError, ValueError) as exc:
                    logger.warning(
                        f'Duel BP candidate {target_id} is unavailable: {exc}'
                    )
                    remaining = tuple(
                        item
                        for item in remaining
                        if str(item.shikigami_id) != target_id
                    )
                    continue

                # OCR and disabled-card checks above take time. Refresh the
                # frame and authorize once more at the exact action boundary
                # so an expiring turn cannot receive even the card click.
                self.screenshot()
                if not self._bp_actionable_self_pick():
                    self._bp_ledger.rollback_pending_own_pick()
                    self._bp_pending_source = None
                    self._publish_bp_action(
                        'click_candidate',
                        status='action_window_lost',
                        shikigami_id=target_id,
                        confidence=candidate.confidence,
                        candidate_rank=rank,
                        selected_verified=False,
                        confirmed=False,
                    )
                    return True

                x, y = candidate.click_point
                self.device.click(
                    x=x,
                    y=y,
                    control_name=f'Duel_BP_Auto_Pick_{target_id}',
                )
                issued_click = True
                logger.info(
                    f'BP[round={self._bp_ledger.round_number}][auto] '
                    f'pending={target_id} selected_verified=false '
                    'confirmed=false source=auto'
                )
                self._publish_bp_action(
                    'click_candidate',
                    status='pending',
                    shikigami_id=target_id,
                    confidence=candidate.confidence,
                    candidate_rank=rank,
                    selected_verified=False,
                    confirmed=False,
                )
                sleep(0.25)
                self.screenshot()
                verified = self._recognize_bp_name_stable(
                    SELECTED_NAME_ROI,
                    expected_id=target_id,
                    require_actionable=True,
                    min_confidence=(
                        self.conf.duel_config.bp_auto_confidence
                    ),
                )
                if verified is None:
                    action_window_open = self._bp_actionable_self_pick()
                    self._bp_ledger.rollback_pending_own_pick(
                        mark_unavailable=action_window_open
                    )
                    self._bp_pending_source = None
                    logger.info(
                        f'BP[round={self._bp_ledger.round_number}]'
                        f'[action] clicked={target_id} '
                        'selected_verified=false confirmed=false'
                    )
                    self._publish_bp_action(
                        'click_candidate',
                        status='verification_failed',
                        shikigami_id=target_id,
                        confidence=candidate.confidence,
                        candidate_rank=rank,
                        selected_verified=False,
                        confirmed=False,
                    )
                    if not action_window_open:
                        # The timer or phase changed while OCR was running.
                        # Do not click a second card or the fixed confirm point.
                        return True
                    remaining = tuple(
                        item
                        for item in remaining
                        if str(item.shikigami_id) != target_id
                    )
                    continue

                self.screenshot()
                if not self._bp_actionable_self_pick():
                    self._bp_ledger.rollback_pending_own_pick()
                    self._bp_pending_source = None
                    logger.info(
                        f'BP[round={self._bp_ledger.round_number}]'
                        f'[action] clicked={target_id} '
                        'selected_verified=true confirmed=false '
                        'reason=action_window_lost'
                    )
                    self._publish_bp_action(
                        'confirm_candidate',
                        status='action_window_lost',
                        shikigami_id=target_id,
                        confidence=verified['confidence'],
                        candidate_rank=rank,
                        selected_verified=True,
                        confirmed=False,
                    )
                    return True

                self.click(self.C_D_BP_CONFIRM, interval=0.2)
                confirmed = self._verify_bp_confirm_transition()
                if not confirmed:
                    # Confirmation is irreversible. Retain the pending ledger
                    # entry for diagnostics, but never issue a second pick.
                    logger.info(
                        f'BP[round={self._bp_ledger.round_number}]'
                        f'[action] clicked={target_id} '
                        'selected_verified=true confirmed=false'
                    )
                    self._publish_bp_action(
                        'confirm_candidate',
                        status='transition_unverified',
                        shikigami_id=target_id,
                        confidence=verified['confidence'],
                        candidate_rank=rank,
                        selected_verified=True,
                        confirmed=False,
                    )
                    return True

                committed = self._bp_ledger.commit_pending_own_pick(
                    expected_id=target_id
                )
                self._bp_pending_source = None
                self._sync_bp_ledger()
                logger.info(
                    f'BP[round={len(self._bp_ledger.own_picks)}]'
                    f'[action] clicked={committed} '
                    'selected_verified=true confirmed=true'
                )
                self._publish_bp_action(
                    'confirm_candidate',
                    status='success',
                    shikigami_id=committed,
                    confidence=verified['confidence'],
                    round_number=len(self._bp_ledger.own_picks),
                    candidate_rank=rank,
                    selected_verified=True,
                    confirmed=True,
                )
                return True
            return issued_click or ranked_candidate_seen
        finally:
            self._bp_selection_in_progress = False

    def execute_bp_recommendation(
        self, recommendation: DuelRecommendation
    ) -> bool:
        """Backward-compatible single-choice wrapper."""

        return self.execute_bp_recommendations((recommendation,))

    def execute_bp_onmyoji_selection(self) -> bool:
        """Select the configured fixed Onmyoji exactly once in AUTO mode.

        The six roster slots are stable configuration targets.  Runtime safety
        therefore comes from checking the round-six action window immediately
        before and after the slot click, followed by the normal confirmation
        transition check.  Large selected-character identity templates are not
        part of this path.
        """

        if self._bp_onmyoji_action_issued:
            return True
        if self._bp_onmyoji_attempts >= 3:
            return True
        onmyoji = self.conf.duel_config.switch_onmyoji
        display_name = str(getattr(onmyoji, 'value', onmyoji))
        try:
            x, y = onmyoji_click_point(onmyoji)
        except ValueError as exc:
            self._bp_onmyoji_action_issued = True
            logger.warning(f'Duel BP Onmyoji selection is invalid: {exc}')
            self._publish_bp_action(
                'select_onmyoji',
                status='invalid_configuration',
                message=str(exc),
                round_number=6,
            )
            return True

        # Authorize the roster click on one fresh frame and require the
        # completed five-pick ledger, the round-six roster, and the dedicated
        # active confirm control.
        self.screenshot()
        if not (
            self._bp_ledger.completed
            and self._bp_seen_onmyoji_selection
            and self.appear(self.I_D_BP_ONMYOJI_SELECT)
            and self._bp_actionable_self_pick()
        ):
            self._publish_bp_action(
                'select_onmyoji',
                status='action_window_lost',
                message='Round-six Onmyoji action window is not available',
                round_number=6,
                selection_method='fixed_slot',
                selected_verified=False,
                confirmed=False,
            )
            return True

        self._bp_onmyoji_attempts += 1
        self.device.click(
            x=x,
            y=y,
            control_name=f'Duel_BP_Onmyoji_{display_name}',
        )
        # A fixed-slot click does not need a second identity recognition pass.
        # It does need a fresh screen check before the irreversible confirm so
        # a delayed or reordered UI cannot turn a stale button into permission
        # to click elsewhere.
        sleep(0.25)
        self.screenshot()
        if not (
            self._bp_ledger.completed
            and self._bp_seen_onmyoji_selection
            and self.appear(self.I_D_BP_ONMYOJI_SELECT)
            and self._bp_actionable_self_pick()
        ):
            logger.info(
                'BP[round=6][action] '
                f'onmyoji={display_name} selection_method=fixed_slot '
                'confirmed=false reason=action_window_lost'
            )
            self._publish_bp_action(
                'select_onmyoji',
                status='action_window_lost',
                message=display_name,
                round_number=6,
                selection_method='fixed_slot',
                selected_verified=False,
                confirmed=False,
            )
            return True

        self._bp_onmyoji_action_issued = True
        self.click(self.C_D_BP_CONFIRM, interval=0.2)
        confirmed = self._verify_bp_confirm_transition()
        if confirmed:
            self._bp_selected_onmyoji = display_name
        logger.info(
            'BP[round=6][action] '
            f'onmyoji={display_name} selection_method=fixed_slot '
            f'confirmed={str(confirmed).lower()}'
        )
        self._publish_bp_action(
            'select_onmyoji',
            status='success' if confirmed else 'transition_unverified',
            message=display_name,
            round_number=6,
            selection_method='fixed_slot',
            confirmed=confirmed,
        )
        return True

    def _reset_bp_manual_name_window(
        self,
        *,
        invalidate_pending: bool = False,
    ) -> None:
        self._bp_manual_name_candidate = None
        self._bp_manual_name_frames = 0
        if (
            invalidate_pending
            and self._bp_pending_source == 'manual'
            and self._bp_ledger.pending_own_pick is not None
        ):
            self._bp_manual_pending_verified = False

    def _track_recommend_manual_selection(
        self,
        observation: BPObservation,
        state_update,
    ) -> None:
        """Stage a manually selected shikigami without issuing any click."""

        if self._bp_manual_tracking_blocked:
            return
        if observation.state != DuelBPState.SELF_PICK:
            # Preserve the last complete three-frame proof while the phase
            # state machine debounces the user's confirmation transition.
            self._reset_bp_manual_name_window()
            return

        actionable = False
        if (
            state_update.accepted
            and state_update.is_stable
            and observation.confidence >= self.bp_assistant.recommend_confidence
        ):
            try:
                actionable = bool(self.appear(self.I_D_BP_CONFIRM_ACTIVE))
            except Exception:
                actionable = False
        if not actionable:
            self._reset_bp_manual_name_window(invalidate_pending=True)
            return

        try:
            result = self._recognize_bp_name_roi(SELECTED_NAME_ROI)
        except Exception as exc:
            logger.warning(
                f'Duel BP manual selection recognition failed: {exc!r}'
            )
            self._reset_bp_manual_name_window(invalidate_pending=True)
            return
        current = result.get('shikigami_id')
        if current in (None, '', 0, '0'):
            self._reset_bp_manual_name_window(invalidate_pending=True)
            return
        current = str(current)
        round_number = self._bp_ledger.round_number
        if self._bp_manual_baseline_round != round_number:
            self._bp_manual_baseline_round = round_number
            self._bp_manual_baseline_id = None
            self._bp_manual_selection_changed = False

        if current == self._bp_manual_name_candidate:
            self._bp_manual_name_frames += 1
        else:
            self._bp_manual_name_candidate = current
            self._bp_manual_name_frames = 1

        pending = self._bp_ledger.pending_own_pick
        if self._bp_pending_source == 'manual' and pending != current:
            # One different recognized frame is enough to make the previous
            # pending identity unsafe to commit. A replacement still requires
            # its own complete three-frame window.
            self._bp_manual_pending_verified = False
        if self._bp_manual_name_frames < 3:
            return

        if self._bp_manual_baseline_id is None:
            # The BP screen opens with a default/highlighted shikigami. Merely
            # seeing it is not proof that the user chose it.
            self._bp_manual_baseline_id = current
            logger.debug(
                f'BP[round={round_number}][manual] baseline={current}'
            )
            return
        if current != self._bp_manual_baseline_id:
            self._bp_manual_selection_changed = True
        elif not self._bp_manual_selection_changed:
            return

        if pending is not None and self._bp_pending_source != 'manual':
            # Never reinterpret an AUTO click as a manual selection after a
            # runtime mode change.
            return
        if pending != current:
            if pending is not None:
                self._bp_ledger.rollback_pending_own_pick()
            try:
                self._bp_ledger.begin_own_pick(current)
            except (RuntimeError, ValueError) as exc:
                logger.warning(
                    f'Duel BP manual selection {current} was not staged: {exc}'
                )
                self._bp_pending_source = None
                self._bp_manual_pending_verified = False
                return
            self._bp_pending_source = 'manual'
            logger.info(
                f'BP[round={self._bp_ledger.round_number}][manual] '
                f'pending={current} selected_verified=true '
                'confirmed=false source=manual'
            )
            self._publish_bp_action(
                'manual_pick',
                status='pending',
                shikigami_id=current,
                confidence=float(result.get('confidence') or 0.0),
                selected_verified=True,
                confirmed=False,
                message='manual selection observed for three stable frames',
            )
        self._bp_manual_pending_verified = True

    def handle_bp_assistant(self) -> bool:
        """Process one BP frame and return whether to suppress auto-entry."""
        assistant = self.bp_assistant
        if (
            assistant.mode == DuelBPMode.OFF
            and self._bp_sample_collector is None
        ):
            return False

        observation = self.recognize_bp_observation()
        if self._bp_sample_collector is not None:
            try:
                result = self._bp_sample_collector.capture(
                    self.device.image.copy(),
                    phase=observation.state.value,
                    confidence=observation.confidence,
                    onmyoji_selection=self._bp_seen_onmyoji_selection,
                    metadata={
                        'own_picks': list(observation.own_picks),
                        'opponent_picks': list(observation.opponent_picks),
                        'bans': list(observation.bans),
                        'own_visible_slots': list(
                            self._bp_last_identity.own_visible_slots
                        ),
                        'opponent_visible_slots': list(
                            self._bp_last_identity.opponent_visible_slots
                        ),
                    },
                )
                if result.captured:
                    logger.info(
                        'Duel BP sample captured: '
                        f'{result.frame_sha256[:12]} '
                        f'({result.crop_count} crops)'
                    )
            except Exception as exc:
                # Collection is diagnostic and must never stop or influence
                # the live selection loop.
                self._bp_sample_collector = None
                logger.warning(
                    f'Duel BP sample capture disabled after error: {exc!r}'
                )
        if assistant.mode == DuelBPMode.OFF:
            return False

        self._bp_last_observation = observation
        ledger = self._bp_ledger
        can_recommend = (
            not self._bp_seen_onmyoji_selection
            and not self._bp_manual_tracking_blocked
            and ledger.ready_for_recommendation
        )
        if not can_recommend:
            rule_candidates = personal_candidates = external_candidates = ()
        else:
            rule_candidates = self.bp_rule_candidates(observation)
            personal_candidates = self.bp_personal_candidates(observation)
            external_candidates = self.bp_external_candidates(observation)
        decision = assistant.process(
            observation,
            rule_candidates=rule_candidates,
            personal_candidates=personal_candidates,
            external_candidates=external_candidates,
            action_confidence=observation.confidence,
            unavailable_ids=ledger.unavailable_ids,
        )
        self.bp_last_decision = decision
        reveal_context_updated = self._observe_bp_reveal_pair_frame()

        if assistant.mode == DuelBPMode.RECOMMEND:
            self._track_recommend_manual_selection(
                observation,
                decision.state_update,
            )
        else:
            self._reset_bp_manual_name_window(
                invalidate_pending=False,
            )

        fifth_pick_reached_onmyoji = bool(
            len(ledger.own_picks) == 4
            and ledger.pending_own_pick is not None
            and self._bp_seen_onmyoji_selection
            and observation.state == DuelBPState.SELF_PICK
            and self.appear(self.I_D_BP_ONMYOJI_SELECT)
        )
        pending_source = getattr(self, '_bp_pending_source', None)
        source_matches_mode = (
            pending_source == 'manual'
            and assistant.mode == DuelBPMode.RECOMMEND
        ) or (
            pending_source == 'auto'
            and assistant.mode == DuelBPMode.AUTO
        )
        phase_signals = getattr(self, '_bp_last_phase_signals', None)
        own_confirm_locked = bool(
            phase_signals is not None
            and phase_signals.confirm_locked > 0
        )
        stable_confirmed_phase = (
            observation.state == DuelBPState.OPPONENT_PICK
            and own_confirm_locked
        )
        if (
            decision.state_update.accepted
            and ledger.pending_own_pick is not None
            and source_matches_mode
            and (stable_confirmed_phase or fifth_pick_reached_onmyoji)
        ):
            if (
                pending_source == 'manual'
                and not self._bp_manual_pending_verified
            ):
                discarded = ledger.rollback_pending_own_pick()
                self._bp_pending_source = None
                self._sync_bp_ledger()
                logger.info(
                    f'BP[round={ledger.round_number}][manual] '
                    f'pending={discarded} confirmed=false '
                    'source=manual_transition reason=unstable_name'
                )
                self._publish_bp_action(
                    'manual_pick',
                    status='transition_unverified',
                    shikigami_id=discarded,
                    confidence=observation.confidence,
                    selected_verified=False,
                    confirmed=False,
                    message='manual name was not stable through confirmation',
                )
            else:
                committed = ledger.commit_pending_own_pick()
                self._bp_pending_source = None
                self._bp_manual_pending_verified = False
                self._sync_bp_ledger()
                if pending_source == 'manual':
                    logger.info(
                        f'BP[round={len(ledger.own_picks)}][manual] '
                        f'selected={committed} selected_verified=true '
                        'confirmed=true source=manual_transition'
                    )
                    action = 'manual_pick'
                    message = 'manual selection confirmed by stable transition'
                else:
                    logger.info(
                        f'BP[round={len(ledger.own_picks)}][auto] '
                        f'clicked={committed} selected_verified=true '
                        'confirmed=true source=auto_delayed_transition'
                    )
                    action = 'confirm_candidate'
                    message = 'auto selection confirmed by later stable transition'
                self._publish_bp_action(
                    action,
                    status='success',
                    shikigami_id=committed,
                    confidence=observation.confidence,
                    round_number=len(ledger.own_picks),
                    selected_verified=True,
                    confirmed=True,
                    message=message,
                )
                observation = BPObservation(
                    state=observation.state,
                    confidence=observation.confidence,
                    own_picks=ledger.own_picks,
                    opponent_picks=ledger.opponent_context,
                    bans=(),
                )
                self._bp_last_observation = observation
        elif (
            assistant.mode == DuelBPMode.RECOMMEND
            and decision.state_update.accepted
            and stable_confirmed_phase
            and ledger.pending_own_pick is None
            and self._bp_manual_baseline_round == ledger.round_number
            and self._bp_manual_baseline_id is not None
        ):
            # A confirmation occurred without a proven change from the
            # initial highlight. We cannot know the chosen identity and must
            # stop advancing the sequential ledger instead of assigning the
            # default card to the next round.
            self._bp_manual_tracking_blocked = True
            logger.info(
                f'BP[round={ledger.round_number}][manual] '
                'id=unknown selected_verified=false confirmed=true '
                'reason=no_selection_change_proof'
            )
            self._publish_bp_action(
                'manual_pick',
                status='identity_unknown',
                round_number=ledger.round_number,
                selected_verified=False,
                confirmed=True,
                message=(
                    'confirmation observed without a proven change from '
                    'the initial highlight'
                ),
            )

        # The pair may have reached three same-frame votes just before the
        # debounced transition above committed our pending own pick. Finalize
        # it now without another screenshot or OCR pass.
        if self._finalize_bp_reveal_pair_if_ready():
            reveal_context_updated = True
        if reveal_context_updated:
            observation = BPObservation(
                state=observation.state,
                confidence=observation.confidence,
                own_picks=ledger.own_picks,
                opponent_picks=ledger.opponent_context,
                bans=(),
            )
            self._bp_last_observation = observation

        # When our next turn is stably visible, resolve exactly the opponent
        # slot revealed since our previous confirmed pick. Unknown remains a
        # real positional slot and must not block the next recommendation.
        opponent_context_updated = False
        if (
            decision.state_update.accepted
            and observation.state == DuelBPState.SELF_PICK
            and ledger.opponent_rounds_seen < len(ledger.own_picks)
        ):
            opponent_context_updated = self._observe_next_bp_opponent(
                allow_click=(
                    assistant.mode == DuelBPMode.AUTO
                    and not self._bp_seen_onmyoji_selection
                )
            )
            if opponent_context_updated:
                observation = BPObservation(
                    state=observation.state,
                    confidence=observation.confidence,
                    own_picks=ledger.own_picks,
                    opponent_picks=ledger.opponent_context,
                    bans=(),
                )
                self._bp_last_observation = observation

        state_key = (
            decision.state_update.state,
            ledger.own_picks,
            ledger.opponent_context,
            ledger.pending_own_pick,
            tuple(
                (
                    item['slot'],
                    item['shikigami_id'],
                    item['status'],
                    item['confidence'],
                    item['source'],
                )
                for item in self._bp_opponent_slot_meta
            ),
            self._bp_selected_onmyoji,
        )
        if decision.state_update.accepted and state_key != self._bp_last_published_state:
            if observation.state not in (
                DuelBPState.BATTLE,
                DuelBPState.RESULT,
            ):
                self.reset_device('DUEL_BP_PROGRESS')
            own_wire = [
                value
                for value in (
                    self._duel_shikigami_id(item)
                    for item in ledger.own_picks
                )
                if value is not None
            ]
            opponent_wire = [
                value
                for value in (
                    self._duel_shikigami_id(item)
                    for item in ledger.opponent_context
                )
                if value is not None
            ]
            state_round = (
                6
                if self._bp_seen_onmyoji_selection
                else ledger.round_number
            )
            self.publish_bp_live_event(
                'state',
                {
                    'previous_state': decision.state_update.previous_state.value,
                    'state': decision.state_update.state.value,
                    'phase': decision.state_update.state.value,
                    'mode': assistant.mode.value,
                    'confidence': observation.confidence,
                    'stable_frames': decision.state_update.stable_frames,
                    'round': state_round,
                    'own_picks': own_wire,
                    'opponent_picks': opponent_wire,
                    'opponent_slots': self._bp_opponent_slots_payload(),
                    'pending_own_pick': self._duel_shikigami_id(
                        ledger.pending_own_pick
                    ),
                    'selected_onmyoji': self._bp_selected_onmyoji,
                    'bans': [],
                    'self_ban': [],
                    'opponent_ban': [],
                    'picks': self._bp_pick_payload(observation),
                    'recommendations': [],
                    'explanation': '',
                },
            )
            self._bp_last_published_state = state_key

        if (
            decision.recommendations
            and not opponent_context_updated
            and not reveal_context_updated
        ):
            recommendation = decision.recommendations[0]
            effective_confidence = min(
                recommendation.confidence,
                observation.confidence,
            )
            recommendation_key = (
                ledger.round_number,
                tuple(
                    (
                        item.shikigami_id,
                        item.source,
                        item.score,
                        item.confidence,
                        item.context_level,
                        item.rank,
                    )
                    for item in decision.recommendations
                ),
            )
            if recommendation_key != self._bp_last_published_recommendation:
                wire_items = []
                for item in decision.recommendations:
                    wire_id = self._duel_shikigami_id(
                        item.shikigami_id
                    )
                    if wire_id is None:
                        continue
                    item_confidence = min(
                        item.confidence, observation.confidence
                    )
                    wire_items.append(
                        {
                            'rank': item.rank,
                            'shikigami_id': wire_id,
                            'shishen_id': wire_id,
                            'score': item.score,
                            'confidence': item_confidence,
                            'sample_size': item.sample_size,
                            'context_level': item.context_level.value,
                            'context_sample_size': (
                                item.context_sample_size
                            ),
                            'reason': item.reason,
                            'source': item.source.value,
                            'evidence_sources': [
                                source.value
                                for source in item.evidence_sources
                            ],
                        }
                    )
                if not wire_items:
                    logger.warning(
                        'Duel BP recommendations contain no numeric '
                        'shikigami IDs'
                    )
                    return True
                wire_shishen_id = wire_items[0]['shishen_id']
                logger.info(
                    f'BP[round={ledger.round_number}][recommend] '
                    f'id={recommendation.shikigami_id} '
                    f'name={self._bp_name(recommendation.shikigami_id)} '
                    f'source={recommendation.source.value} '
                    f'context={recommendation.context_level.value} '
                    f'score={recommendation.score:.4f} '
                    'top5='
                    + ','.join(
                        f'{item.shikigami_id}:{item.score:.4f}'
                        for item in decision.recommendations
                    )
                )
                self.publish_bp_live_event(
                    'recommendation',
                    {
                        'state': decision.state_update.state.value,
                        'phase': decision.state_update.state.value,
                        'mode': assistant.mode.value,
                        'target_round': ledger.round_number,
                        'context_level': (
                            recommendation.context_level.value
                        ),
                        'shikigami_id': wire_shishen_id,
                        'shishen_id': wire_shishen_id,
                        'source': recommendation.source.value,
                        'score': recommendation.score,
                        'confidence': effective_confidence,
                        'recognition_confidence': observation.confidence,
                        'recommendation_confidence': recommendation.confidence,
                        'sample_size': recommendation.sample_size,
                        'reason': recommendation.reason,
                        'explanation': recommendation.reason,
                        'recommendations': wire_items,
                        'evidence_sources': [
                            source.value
                            for source in recommendation.evidence_sources
                        ],
                    },
                )
                self._bp_last_published_recommendation = recommendation_key

        if assistant.mode == DuelBPMode.OBSERVE:
            return True
        if assistant.mode == DuelBPMode.RECOMMEND:
            return True
        if ledger.pending_own_pick is not None:
            # A confirmation click may have succeeded even when its immediate
            # transition could not be verified. Until a later stable phase
            # commits it, never fall through to the legacy auto-entry button
            # and never issue a second irreversible choice.
            return True
        if self._bp_seen_onmyoji_selection:
            if (
                ledger.completed
                and observation.state == DuelBPState.SELF_PICK
                and decision.state_update.accepted
                and decision.state_update.is_stable
                and observation.confidence
                >= self.conf.duel_config.bp_auto_confidence
            ):
                return self.execute_bp_onmyoji_selection()
            return True
        if observation.state != DuelBPState.SELF_PICK:
            return True
        if opponent_context_updated or reveal_context_updated:
            # Recommendation will be recalculated against the new nullable
            # opponent context on the next frame.
            return True
        if decision.should_auto_pick and decision.recommendations:
            if self.execute_bp_recommendations(decision.recommendations):
                return True
            logger.warning(
                'Duel BP recommendation could not be verified; '
                'falling back to legacy auto-entry'
            )
            return False
        if decision.fallback_to_auto_entry:
            logger.info(
                f'Duel BP fallback to legacy auto-entry: {decision.reason}'
            )
            return False
        # Auto mode waits until the stable-frame window is complete.
        return True

    @staticmethod
    def _bp_pick_payload(observation: BPObservation) -> list[dict]:
        return build_pick_payload(
            observation.own_picks,
            observation.opponent_picks,
        )

    def wait_battle(self) -> bool | None:
        """等待战斗结束, 返回战斗结果, 最后会退出到斗技主界面"""
        logger.hr('duel battle waiting')
        battle_operated = False
        battle_timeout_timer = Timer(270).start()
        ret_timer = Timer(5)
        battle_timeout_cnt, max_timeout_cnt = 0, 3
        ret = None
        while True:
            self.screenshot()
            self.handle_bp_assistant()
            self.check_and_get_reward()
            if self.appear(self.I_CHECK_DUEL) and self.appear(self.I_D_HELP):  # 斗技主界面
                break
            if self.appear(self.I_D_WIN_SHARE,interval= 1.2): #拔得头筹
                self.click(random_click(ltrb=(True, True, False, True)), interval=1.2)
                continue
            if self.appear_then_click(self.I_UI_BACK_RED, interval=1.2):  # 关闭段位上升页面
                ret_timer.reset()
                continue
            if ret_timer.started() and ret_timer.reached():  # 兜底逻辑, 已经结算了但是还没有到斗技主界面
                self.goto_page(page_duel)
                break
            if self.is_battle_win():
                ret = True
                self.publish_bp_result_event(ret)
                ret_timer.start()
                self.click(random_click(ltrb=(True, True, False, True)), interval=1.2)
                continue
            if self.is_battle_lose():
                ret = None if self._duel_aborted_before_battle else False
                self.publish_bp_result_event(ret)
                ret_timer.start()
                self.click(random_click(ltrb=(True, True, False, True)), interval=1.2)
                continue
            if not ret_timer.started() and battle_timeout_cnt >= max_timeout_cnt:
                logger.warning('Duel battle timeout[>15 minutes], exit')
                self.duel_exit_battle()
                continue
            if (
                ret is None
                and not battle_operated
                and not ret_timer.started()
                and not self._duel_aborted_before_battle
            ):  # 进行战斗前的操作
                self.ui_click(self.O_BATTLE_HAND, self.O_BATTLE_AUTO, interval=0.8)
                self.green_mark(self.conf.duel_config.green_enable, self.conf.duel_config.green_mark)
                battle_operated = True
                self.reset_device('BATTLE_STATUS_S')
                continue
            if not ret_timer.started() and battle_timeout_timer.reached_and_reset():
                battle_timeout_cnt += 1
                self.reset_device('BATTLE_STATUS_S')
                logger.warning("battle' time is too long, increase wait time")
        return ret

    def publish_bp_result_event(self, result: bool | None) -> None:
        if self.bp_assistant.mode == DuelBPMode.OFF or self._bp_result_published:
            return
        observation = self._bp_last_observation
        confidence = (
            observation.confidence
            if observation is not None and observation.state == DuelBPState.RESULT
            else 0.0
        )
        self.publish_bp_live_event(
            'state',
            {
                'state': DuelBPState.RESULT.value,
                'phase': DuelBPState.RESULT.value,
                'mode': self.bp_assistant.mode.value,
                'confidence': confidence,
                'result': (
                    'win'
                    if result is True
                    else 'loss'
                    if result is False
                    else 'unknown'
                ),
                'valid': result is not None,
                'aborted_before_battle': self._duel_aborted_before_battle,
                'abort_reason': self._duel_abort_reason,
                'round': 6,
                'own_picks': [
                    value
                    for value in (
                        self._duel_shikigami_id(item)
                        for item in self._bp_own_picks
                    )
                    if value is not None
                ],
                'opponent_picks': [
                    value
                    for value in (
                        self._duel_shikigami_id(item)
                        for item in self._bp_opponent_picks
                    )
                    if value is not None
                ],
                'opponent_slots': self._bp_opponent_slots_payload(),
                'pending_own_pick': self._duel_shikigami_id(
                    self._bp_ledger.pending_own_pick
                ),
                'selected_onmyoji': self._bp_selected_onmyoji,
                'bans': [],
                'self_ban': [],
                'opponent_ban': [],
                'picks': self._bp_pick_payload(
                    BPObservation(
                        DuelBPState.RESULT,
                        confidence,
                        own_picks=self._bp_own_picks,
                        opponent_picks=self._bp_opponent_picks,
                        bans=self._bp_bans,
                    )
                ),
                'recommendations': [],
                'explanation': '',
            },
        )
        self._bp_result_published = True

    @staticmethod
    def _duel_shikigami_id(value) -> int | None:
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    def record_duel_match(self, *, battle_started_at: datetime, battle_result: bool | None) -> None:
        """Persist one locally played duel without affecting battle cleanup."""
        try:
            from module.duel_data.repository import DuelRepository

            effective_result = (
                None if self._duel_aborted_before_battle else battle_result
            )
            repository = DuelRepository()
            config_name = str(getattr(self.config, 'config_name', 'default'))
            account_id, _ = repository.upsert_account(
                {
                    'id': f'config:{config_name}',
                    'name': config_name,
                    'latest_at': battle_started_at.isoformat(),
                    'latest_score': self.current_score,
                },
                source='oas',
            )
            picks = []
            for item in build_pick_payload(
                self._bp_own_picks[:6], self._bp_opponent_picks[:6]
            ):
                shikigami_id = self._duel_shikigami_id(item['shishen_id'])
                if shikigami_id is not None:
                    picks.append(
                        {
                            **item,
                            'shishen_id': shikigami_id,
                        }
                    )

            self_ban = self._duel_shikigami_id(self._bp_bans[0]) if self._bp_bans else None
            opponent_ban = (
                self._duel_shikigami_id(self._bp_bans[1])
                if len(self._bp_bans) > 1
                else None
            )
            finished_at = datetime.now(timezone.utc)
            match_id, _ = repository.upsert_match(
                account_id,
                {
                    'started_at': battle_started_at.isoformat(),
                    'score': self.current_score,
                    'star': None,
                    'self_ban': self_ban,
                    'opponent_ban': opponent_ban,
                    'picks': picks,
                    'result': (
                        'win'
                        if effective_result is True
                        else 'loss'
                        if effective_result is False
                        else 'unknown'
                    ),
                    'duration': max(0.0, (finished_at - battle_started_at).total_seconds()),
                    'valid': effective_result is not None,
                    'practice_mode': False,
                    'source_record_id': f'{config_name}:{battle_started_at.isoformat()}',
                    'raw': {
                        'recorded_by': 'oas_duel',
                        'own_picks': list(self._bp_own_picks),
                        'opponent_picks': list(self._bp_opponent_picks),
                        'bans': list(self._bp_bans),
                        'aborted_before_battle': self._duel_aborted_before_battle,
                        'abort_reason': self._duel_abort_reason,
                    },
                },
                source='oas',
            )
            match = repository.get_match(match_id)
            self.publish_bp_live_event(
                'match',
                match.model_dump(mode='json') if match is not None else {'id': match_id},
            )
        except Exception as exc:
            # A local database issue must not break the existing Duel task.
            logger.warning(f'Unable to persist Duel match: {exc!r}')

    def duel_exit_battle(
        self,
        *,
        abort_before_battle: bool = False,
        reason: str | None = None,
    ):
        if abort_before_battle:
            self._duel_aborted_before_battle = True
            self._duel_abort_reason = reason or 'selection_aborted'
        while 1:
            self.screenshot()
            if self.appear(self.I_D_FAIL) or self.appear(self.I_FALSE):
                return
            if self.appear_then_click(self.I_EXIT_ENSURE):
                continue
            # 选式神界面退出或战斗内退出
            if self.appear_then_click(self.I_DUEL_EXIT, interval=1) or self.appear_then_click(self.I_EXIT, interval=1):
                continue

    def check_honor(self) -> bool:
        """检查荣誉是否满了"""
        if not self.appear(self.I_DUEL_HONOR):
            return False
        roi_x = self.I_DUEL_HONOR.roi_front[0] + self.I_DUEL_HONOR.roi_front[2]
        roi_y = self.I_DUEL_HONOR.roi_front[1]
        roi_w = 110
        roi_h = self.I_DUEL_HONOR.roi_front[3]
        self.O_D_HONOR.roi = [roi_x, roi_y, roi_w, roi_h]
        current, remain, total = self.O_D_HONOR.ocr(self.device.image)
        return current == total and remain == 0

    def get_and_update_cur_score(self, skip_screenshot: bool = True) -> int:
        """
        获取并更新当前斗技分数, 要求处于斗技主界面, 同时更新名仕状态
        :param skip_screenshot: 是否跳过截图
        :return: 当前斗技分数
        """
        self.maybe_screenshot(skip_screenshot)
        score = self.current_score
        self.is_celeb = False
        if self.appear(self.I_D_CELEB_STAR) or self.appear(self.I_D_CELEB_HONOR):
            self.is_celeb = True
            if self.battle_win_count - self.pre_battle_win_cnt == 1:
                self.pre_battle_win_cnt = self.battle_win_count
                score += 100
            elif self.battle_lose_count - self.pre_battle_lose_cnt == 1:
                self.pre_battle_lose_cnt = self.battle_lose_count
                score -= 100
        else:
            score, remain, total = self.O_D_SCORE.ocr(self.device.image)
            if score > 10000:
                # 识别错误分数超过一万, 去掉最高位
                logger.warning('Recognition error, score is too high')
                score = int(str(score)[1:])
        logger.info(f'battle score: {score}')
        self.current_score = score
        return self.current_score

    def switch_soul(self):
        """从式神录界面切换御魂"""
        if self.conf.switch_soul.enable:
            self.goto_page(page_shikigami_records)
            self.run_switch_soul(self.conf.switch_soul.switch_group_team)
        if self.conf.switch_soul.enable_switch_by_name:
            self.goto_page(page_shikigami_records)
            self.run_switch_soul_by_name(self.conf.switch_soul.group_name, self.conf.switch_soul.team_name)

    def duel_main(self, screenshot=False) -> bool:
        """判断是否在斗技主界面"""
        if screenshot:
            self.screenshot()
        return self.appear(self.I_D_HELP) or self.appear(self.I_CHECK_DUEL) or \
            self.appear(self.I_D_CELEB_STAR) or self.appear(self.I_D_CELEB_HONOR)

    def switch_all_soul(self):
        """在斗技式神备选界面一键切换所有御魂"""
        if not self.conf.duel_config.switch_all_soul:
            return
        click_count = 0  # 计数
        while 1:
            self.screenshot()
            if click_count >= 3:
                break
            if self.appear_then_click(self.I_D_TEAM, interval=1):
                continue
            if self.appear_then_click(self.I_UI_CONFIRM, interval=0.6):
                continue
            if self.appear_then_click(self.I_D_TEAM_SWTICH, interval=1):
                click_count += 1
                continue
        logger.info('Souls Switch is complete')
        self.ui_click(self.I_UI_BACK_YELLOW, self.I_D_TEAM)

    def check_and_get_reward(self):
        """检查并收获奖励"""
        if self.appear(self.I_REWARD) or self.appear(self.I_UI_REWARD):
            if self.click(random_click(ltrb=(True, True, False, True)), interval=0.6):
                logger.info('get reward')

    def is_in_battle_prepare(self, skip_screenshot=True) -> bool:
        """是否在战斗准备界面"""
        self.maybe_screenshot(skip_screenshot)
        last_observation = getattr(self, '_bp_last_observation', None)
        if (
            getattr(self, '_bp_seen_onmyoji_selection', False)
            and last_observation is not None
            and last_observation.state == DuelBPState.READY
        ):
            return True
        return self.appear(self.I_D_PREPARE) or \
            self.appear(self.I_D_AUTO_ENTRY) or \
            self.appear(self.I_BAN) or \
            self.appear(self.I_D_BP_CONFIRM_ACTIVE) or \
            self.appear(self.I_D_BP_CONFIRMED) or \
            self.appear(self.I_D_BP_ONMYOJI_SELECT) or \
            self.appear(self.I_D_BP_READY) or \
            self.appear(self.I_D_WORD_BATTLE) or \
            self.appear(self.I_D_CHECK_BAN)

    def is_battle_win(self) -> bool:
        return self.appear(self.I_WIN) or self.appear(self.I_D_VICTORY)

    def is_battle_lose(self) -> bool:
        return self.appear(self.I_FALSE) or self.appear(self.I_D_FAIL)

    def is_battle_end(self) -> bool:
        return self.is_battle_win() or self.is_battle_lose() or \
            self.appear(self.I_REWARD) or self.appear(self.I_UI_REWARD)

    def reset_device(self, status: str):
        self.device.click_record_clear()
        self.device.stuck_record_clear()
        self.device.stuck_record_add(status)


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    c = Config('oas3')
    d = Device(c)
    t = ScriptTask(c, d)

    t.run()
