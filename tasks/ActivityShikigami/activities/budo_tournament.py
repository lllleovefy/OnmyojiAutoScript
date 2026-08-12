import random

from module.base.protect import random_sleep
from module.base.timer import Timer
from module.logger import logger
from tasks.ActivityShikigami.base_act import BaseAct, TicketsNotEnough
from tasks.ActivityShikigami.config import GeneralBattleConfig
from tasks.Component.GeneralBattle.general_battle import ExitMatcher
import tasks.ActivityShikigami.page as pages


class BudoTournamentAct(BaseAct):
    """武道大会活动适配器。"""

    supported_climb_types = frozenset({'pass', 'ap'})

    def _exit_matcher(self) -> ExitMatcher | None:
        return pages.any_of(self.I_BUDO_PASS_PAGE, self.I_BUDO_AP_PAGE)

    def before_run(self):
        super().before_run()
        page_main = self.navigator.resolve_page(pages.page_main)
        page_act = self.navigator.resolve_page(pages.page_act)
        page_pass = self.navigator.resolve_page(pages.page_act_pass)
        page_ap = self.navigator.resolve_page(pages.page_act_ap)

        page_act.recognizer = pages.any_of(self.I_BUDO_HOME)
        page_pass.recognizer = pages.any_of(self.I_BUDO_PASS_PAGE)
        page_ap.recognizer = pages.any_of(self.I_BUDO_AP_PAGE)

        # 替换上一期活动路径，仅保留武道大会支持的两种玩法。
        page_main.connect(page_act, self.I_BUDO_MAIN_ENTRY, key='page_main->page_act')
        for destination in (page_pass, page_ap,
                            self.navigator.resolve_page(pages.page_act_ap100),
                            self.navigator.resolve_page(pages.page_act_boss)):
            page_act.remove_transition(destination=destination)
        page_act.connect(page_pass, self.I_BUDO_TO_PASS, key='page_act->page_act_pass')
        page_act.connect(page_ap, self.I_BUDO_TO_AP, key='page_act->page_act_ap')
        page_pass.connect(page_act, self.I_UI_BACK_YELLOW, key='page_act_pass->page_act')
        page_ap.connect(page_act, self.I_UI_BACK_YELLOW, key='page_act_ap->page_act')

    def _run_pass(self):
        self._open_pass_challenge()
        # 修行合训只有搜寻后的挑战弹层才显示式神录入口。
        self.switch_soul(self.I_BUDO_TO_RECORDS)
        if self.conf.general_climb.random_sleep:
            random_sleep(probability=0.2)
        if self.enter_battle(self.I_BUDO_PASS_FIRE):
            self.count_map[self.climb_type] += 1
            self.run_general_battle(
                self.conf.pass_battle_conf,
                battle_key='act_pass',
                exit_matcher=self.I_BUDO_PASS_PAGE,
            )

    def _run_ap(self):
        self.switch_soul(self.I_BUDO_TO_RECORDS)
        if self.conf.general_climb.random_sleep:
            random_sleep(probability=0.2)
        if self.enter_battle(self.I_BUDO_AP_FIRE):
            self.count_map[self.climb_type] += 1
            self.run_general_battle(
                self.conf.ap_battle_conf,
                battle_key='act_ap',
                exit_matcher=self.I_BUDO_AP_PAGE,
            )

    def _open_pass_challenge(self):
        """执行“搜寻”，等待“开启挑战”弹层出现。"""
        click_times, max_times = 0, random.randint(3, 5)
        wait_timer = Timer(20).start()
        while True:
            self.screenshot()
            if self.appear(self.I_BUDO_PASS_FIRE):
                return
            if click_times >= max_times or wait_timer.reached():
                logger.warning('Pass search cannot open challenge, finish current mode')
                raise TicketsNotEnough
            if self.appear(self.I_UI_BACK_RED, interval=1):
                logger.warning('Pass search shows a close button, maybe not enough tickets')
                raise TicketsNotEnough
            if self.appear_then_click(self.I_UI_CONFIRM_SAMLL, interval=1) or \
                    self.appear_then_click(self.I_UI_CONFIRM, interval=1):
                continue
            if self.appear_then_click(self.I_BUDO_SEARCH, interval=1.5):
                click_times += 1
                logger.info(f'Try search opponent, remain times[{max_times - click_times}]')

    def lock_team(self, battle_conf: GeneralBattleConfig):
        """日常训练切换阵容锁定；修行合训没有开关时安全跳过。"""
        if self.climb_type != 'ap':
            logger.info('Pass page has no team lock switch, skip it')
            return

        self.screenshot()
        is_locked = self.appear(self.I_BUDO_AP_LOCK)
        is_unlocked = self.appear(self.I_BUDO_AP_UNLOCK)
        if not is_locked and not is_unlocked:
            logger.warning('Cannot recognize daily training team lock, keep current state')
            return
        if battle_conf.lock_team_enable:
            logger.info('Lock ap team')
            if is_unlocked:
                self.ui_click(self.I_BUDO_AP_UNLOCK, stop=self.I_BUDO_AP_LOCK, interval=1.5)
            return
        logger.info('Unlock ap team')
        if is_locked:
            self.ui_click(self.I_BUDO_AP_LOCK, stop=self.I_BUDO_AP_UNLOCK, interval=1.5)
