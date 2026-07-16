# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from time import sleep

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
    auto_identity_is_complete,
    parse_identity_regions,
)
from tasks.Duel.live import DuelLivePublisherMixin
from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from tasks.GameUi.page import page_main, page_shikigami_records

""" 斗技 """


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
        self._bp_own_picks: tuple[str, ...] = ()
        self._bp_opponent_picks: tuple[str, ...] = ()
        self._bp_bans: tuple[str, ...] = ()
        self._bp_candidate_provider = None
        self._bp_candidate_cache_key = None
        self._bp_candidate_cache = None
        self._bp_identity_adapter = None
        self._bp_identity_tracker = None
        self._bp_identity_disabled = False
        self._bp_identity_missing_in_selection = False
        self._bp_last_identity = DuelIdentityObservation()
        self._bp_seen_onmyoji_selection = False
        self._bp_confirm_active_seen = False
        self._bp_confirmed_self_rounds = 0
        self._bp_confirmed_opponent_rounds = 0
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
        self._bp_own_picks = ()
        self._bp_opponent_picks = ()
        self._bp_bans = ()
        self._bp_candidate_cache_key = None
        self._bp_candidate_cache = None
        self._bp_identity_missing_in_selection = False
        self._bp_last_identity = DuelIdentityObservation()
        self._bp_seen_onmyoji_selection = False
        self._bp_confirm_active_seen = False
        self._bp_confirmed_self_rounds = 0
        self._bp_confirmed_opponent_rounds = 0
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
        recommend_confidence = config.bp_recommend_confidence
        auto_confidence = config.bp_auto_confidence
        if auto_confidence < recommend_confidence:
            logger.warning(
                'Duel BP auto confidence is below recommendation confidence; '
                'using recommendation confidence for auto mode'
            )
            auto_confidence = recommend_confidence
        return DuelBPAssistant(
            mode=config.bp_mode,
            stable_frames=config.bp_stable_frames,
            recommend_confidence=recommend_confidence,
            auto_confidence=auto_confidence,
            personal_min_samples=config.bp_personal_min_samples,
        )

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

        signals = DuelBPPhaseSignals(**signal_scores)
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
            identity = self.recognize_bp_identities(recognized_state)
            self._bp_last_identity = identity
            own_picks = (
                identity.own_picks
                if len(identity.own_picks) >= len(self._bp_own_picks)
                else self._bp_own_picks
            )
            opponent_picks = (
                identity.opponent_picks
                if len(identity.opponent_picks) >= len(self._bp_opponent_picks)
                else self._bp_opponent_picks
            )
            bans = (
                identity.bans
                if len(identity.bans) >= len(self._bp_bans)
                else self._bp_bans
            )
            identity_complete = auto_identity_is_complete(
                state=recognized_state.value,
                previous_state=previous_state.value,
                observation=identity,
                previous_own_picks=self._bp_own_picks,
                previous_opponent_picks=self._bp_opponent_picks,
                previous_bans=self._bp_bans,
                is_celeb=self.is_celeb,
            )
            auto_selection_state = recognized_state in (
                DuelBPState.BAN,
                DuelBPState.SELF_PICK,
                DuelBPState.OPPONENT_PICK,
                DuelBPState.READY,
            )
            if (
                self.bp_assistant.mode == DuelBPMode.AUTO
                and auto_selection_state
                and not identity_complete
            ):
                recognized_confidence = 0.0
                self._bp_identity_missing_in_selection = True
            elif identity.confidence is not None:
                recognized_confidence = min(
                    recognized_confidence,
                    identity.confidence,
                )
            elif (
                self.bp_assistant.mode == DuelBPMode.AUTO
                and auto_selection_state
            ):
                allow_unidentified_first_pick = (
                    recognized_state == DuelBPState.SELF_PICK
                    and previous_state == DuelBPState.BAN
                    and not self.is_celeb
                    and not self._bp_identity_missing_in_selection
                    and not self._bp_own_picks
                    and not self._bp_opponent_picks
                    and not self._bp_bans
                )
                if not allow_unidentified_first_pick:
                    recognized_confidence = 0.0
                if recognized_state in (DuelBPState.OPPONENT_PICK, DuelBPState.READY) or (
                    recognized_state == DuelBPState.BAN and self.is_celeb
                ):
                    self._bp_identity_missing_in_selection = True
            return BPObservation(
                recognized_state,
                confidence=recognized_confidence,
                own_picks=own_picks,
                opponent_picks=opponent_picks,
                bans=bans,
            )
        return BPObservation(
            self.bp_assistant.state_machine.state,
            confidence=0.0,
            own_picks=self._bp_own_picks,
            opponent_picks=self._bp_opponent_picks,
            bans=self._bp_bans,
        )

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

                assets = DuelRepository().latest_snapshot('shishen_assets')
                if not isinstance(assets, list) or not assets:
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
        cache_key = observation.fingerprint
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
                    converted.append(
                        DuelRecommendationCandidate(
                            shikigami_id=str(item['shikigami_id']),
                            source=RecommendationSource(str(item['source'])),
                            score=float(item.get('score', 0.0)),
                            confidence=float(item.get('confidence', 0.0)),
                            priority=int(item.get('priority', 0)),
                            sample_size=int(item.get('sample_size', 0)),
                            reason=str(item.get('reason') or ''),
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    logger.warning(f'Ignoring invalid Duel BP candidate: {exc!r}')
            groups[group_name] = tuple(converted)
        self._bp_candidate_cache_key = cache_key
        self._bp_candidate_cache = groups
        return groups

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

    def execute_bp_recommendation(
        self, recommendation: DuelRecommendation
    ) -> bool:
        """Click an explicitly configured target and verify the next frames.

        Coordinates are deliberately user supplied. A database ID is not a
        stable screen coordinate, so guessing here would make AUTO unsafe.
        """
        try:
            targets = json.loads(self.conf.duel_config.bp_pick_targets or '{}')
            target = targets.get(str(recommendation.shikigami_id))
            if isinstance(target, dict):
                x, y = target.get('x'), target.get('y')
            elif isinstance(target, (list, tuple)) and len(target) == 2:
                x, y = target
            else:
                return False
            x, y = int(x), int(y)
            height, width = self.device.image.shape[:2]
            if not (0 <= x < width and 0 <= y < height):
                logger.warning('Duel BP pick target is outside the screenshot')
                return False
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(f'Invalid Duel BP pick target configuration: {exc!r}')
            return False

        active_slot_index = 4 - self._bp_confirmed_self_rounds
        visible_slots = self._bp_last_identity.own_visible_slots
        if (
            not 0 <= active_slot_index < 5
            or len(visible_slots) != 5
            or any(value is None for value in visible_slots)
            or self._bp_last_identity.confidence is None
            or self._bp_last_identity.confidence
            < self.conf.duel_config.bp_auto_confidence
        ):
            logger.warning(
                'Duel BP AUTO requires all five spatial slots before clicking'
            )
            return False

        self.device.click(x=x, y=y, control_name='Duel_BP_Auto_Pick')
        verification_key = None
        stable_verification_frames = 0
        for _ in range(3):
            sleep(0.35)
            self.screenshot()
            observation = self.recognize_bp_observation()
            if observation.confidence < self.conf.duel_config.bp_auto_confidence:
                verification_key = None
                stable_verification_frames = 0
                continue
            visible_slots = self._bp_last_identity.own_visible_slots
            if (
                len(visible_slots) != 5
                or visible_slots[active_slot_index]
                != recommendation.shikigami_id
            ):
                verification_key = None
                stable_verification_frames = 0
                continue
            current_key = ('candidate', active_slot_index, visible_slots)
            if current_key == verification_key:
                stable_verification_frames += 1
            else:
                verification_key = current_key
                stable_verification_frames = 1
            if stable_verification_frames >= 3:
                self.click(self.C_D_BP_CONFIRM, interval=0.2)
                break
        else:
            logger.warning(
                'Duel BP target click was not verified; suppressing further '
                'selection clicks for this frame'
            )
            return True

        # Confirm is irreversible. Verify the non-action phase for diagnostics,
        # but always suppress any legacy fallback after issuing it.
        stable_confirmation_frames = 0
        confirmation_key = None
        for _ in range(3):
            sleep(0.35)
            self.screenshot()
            observation = self.recognize_bp_observation()
            if observation.state not in (
                DuelBPState.OPPONENT_PICK,
                DuelBPState.READY,
                DuelBPState.BATTLE,
            ):
                stable_confirmation_frames = 0
                confirmation_key = None
                continue
            current_key = (observation.state, observation.own_picks)
            if current_key == confirmation_key:
                stable_confirmation_frames += 1
            else:
                confirmation_key = current_key
                stable_confirmation_frames = 1
        if stable_confirmation_frames < 3:
            logger.warning('Duel BP confirm transition was not stably verified')
        return True

    def handle_bp_assistant(self) -> bool:
        """Process one BP frame and return whether to suppress auto-entry."""
        assistant = self.bp_assistant
        if assistant.mode == DuelBPMode.OFF:
            return False

        observation = self.recognize_bp_observation()
        # Do not commit identity context before the state machine accepts the
        # complete fingerprint for three frames. A single longer false
        # detection must not become the baseline for subsequent frames.
        self._bp_last_observation = observation
        if self._bp_seen_onmyoji_selection:
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
        )
        self.bp_last_decision = decision
        if decision.state_update.accepted:
            self._bp_own_picks = observation.own_picks
            self._bp_opponent_picks = observation.opponent_picks
            self._bp_bans = observation.bans
        state_key = (
            decision.state_update.state,
            observation.own_picks,
            observation.opponent_picks,
            observation.bans,
        )
        if decision.state_update.accepted and state_key != self._bp_last_published_state:
            self.publish_bp_live_event(
                'state',
                {
                    'previous_state': decision.state_update.previous_state.value,
                    'state': decision.state_update.state.value,
                    'phase': decision.state_update.state.value,
                    'mode': assistant.mode.value,
                    'confidence': observation.confidence,
                    'stable_frames': decision.state_update.stable_frames,
                    'own_picks': list(observation.own_picks),
                    'opponent_picks': list(observation.opponent_picks),
                    'bans': list(observation.bans),
                    'self_ban': list(observation.bans[:1]),
                    'opponent_ban': list(observation.bans[1:2]),
                    'picks': self._bp_pick_payload(observation),
                    'recommendations': [],
                    'explanation': '',
                },
            )
            self._bp_last_published_state = state_key
        if decision.recommendation is not None:
            recommendation = decision.recommendation
            effective_confidence = min(
                recommendation.confidence,
                observation.confidence,
            )
            recommendation_key = (
                recommendation.shikigami_id,
                recommendation.source,
                recommendation.score,
                effective_confidence,
            )
            if recommendation_key != self._bp_last_published_recommendation:
                wire_shishen_id = self._duel_shikigami_id(
                    recommendation.shikigami_id
                )
                if wire_shishen_id is None:
                    logger.warning(
                        'Duel BP recommendation has a non-numeric shikigami ID'
                    )
                    return True
                logger.info(
                    'Duel BP recommendation: '
                    f'{recommendation.shikigami_id} '
                    f'[{recommendation.source.value}, '
                    f'confidence={effective_confidence:.3f}]'
                )
                self.publish_bp_live_event(
                    'recommendation',
                    {
                        'state': decision.state_update.state.value,
                        'phase': decision.state_update.state.value,
                        'mode': assistant.mode.value,
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
                        'recommendations': [
                            {
                                'shikigami_id': wire_shishen_id,
                                'shishen_id': wire_shishen_id,
                                'score': recommendation.score,
                                'confidence': effective_confidence,
                                'sample_size': recommendation.sample_size,
                                'reason': recommendation.reason,
                                'source': recommendation.source.value,
                                'evidence_sources': [
                                    source.value
                                    for source in recommendation.evidence_sources
                                ],
                            }
                        ],
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
        if self._bp_seen_onmyoji_selection:
            # Round six uses an independent Onmyoji ID space. Until that
            # identity path is verified, AUTO remains fail-closed and never
            # falls through to shikigami/legacy clicks.
            return True
        if observation.state != DuelBPState.SELF_PICK:
            return True
        if decision.should_auto_pick and decision.recommendation is not None:
            if self.execute_bp_recommendation(decision.recommendation):
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
                'own_picks': list(self._bp_own_picks),
                'opponent_picks': list(self._bp_opponent_picks),
                'bans': list(self._bp_bans),
                'self_ban': list(self._bp_bans[:1]),
                'opponent_ban': list(self._bp_bans[1:2]),
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
