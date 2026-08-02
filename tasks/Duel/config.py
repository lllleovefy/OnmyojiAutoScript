# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from pydantic import BaseModel, Field, field_validator
from datetime import time
from tasks.Component.SwitchOnmyoji.config import Onmyoji

from tasks.Component.config_scheduler import Scheduler
from tasks.Component.config_base import ConfigBase, Time
from tasks.Component.GeneralBattle.config_general_battle import GreenMarkType
from enum import Enum
from tasks.Component.SwitchSoul.switch_soul_config import SwitchSoulConfig
from tasks.Duel.bp import DuelBPMode
from tasks.Duel.identity import DEFAULT_IDENTITY_REGIONS_JSON


class DuelConfig(ConfigBase):
    # BP assistant is opt-in. Off preserves the legacy auto-entry behavior.
    bp_mode: DuelBPMode = Field(default=DuelBPMode.OFF, description='duel_bp_mode_help')
    bp_stable_frames: int = Field(default=3, ge=3, le=10, description='duel_bp_stable_frames_help')
    bp_recommend_confidence: float = Field(default=0.90, ge=0, le=1, description='duel_bp_recommend_confidence_help')
    bp_auto_confidence: float = Field(default=0.98, ge=0.98, le=1, description='duel_bp_auto_confidence_help')
    bp_personal_min_samples: int = Field(default=20, ge=1, description='duel_bp_personal_min_samples_help')
    bp_identity_enabled: bool = Field(default=True, description='duel_bp_identity_enabled_help')
    bp_identity_confidence: float = Field(default=0.90, ge=0.5, le=1, description='duel_bp_identity_confidence_help')
    bp_identity_regions: str = Field(
        default=DEFAULT_IDENTITY_REGIONS_JSON,
        description='duel_bp_identity_regions_help',
    )
    bp_pick_targets: str = Field(
        default='{}',
        description='duel_bp_pick_targets_help',
    )
    bp_shishen_pool: list[int] = Field(
        default_factory=list,
        description='duel_bp_shishen_pool_help',
    )
    bp_portrait_library: str = Field(
        default='config/duel/portrait_library',
        description='duel_bp_portrait_library_help',
    )
    bp_candidate_swipe_limit: int = Field(
        default=12,
        ge=1,
        le=40,
        description='duel_bp_candidate_swipe_limit_help',
    )
    bp_candidate_scan_budget: float = Field(
        default=5.0,
        ge=1.0,
        le=8.0,
        description='duel_bp_candidate_scan_budget_help',
    )
    bp_opponent_inspect_enabled: bool = Field(
        default=False,
        description='duel_bp_opponent_inspect_enabled_help',
    )
    bp_log_raw_frames: bool = Field(
        default=False,
        description='duel_bp_log_raw_frames_help',
    )
    bp_sample_capture_enabled: bool = Field(
        default=False,
        description='duel_bp_sample_capture_enabled_help',
    )
    bp_sample_capture_interval: float = Field(
        default=1.0,
        ge=0.25,
        le=10.0,
        description='duel_bp_sample_capture_interval_help',
    )
    bp_auto_verified: bool = Field(
        default=False,
        description='duel_bp_auto_verified_help',
    )

    @field_validator('bp_shishen_pool')
    @classmethod
    def normalize_bp_shishen_pool(cls, values: list[int]) -> list[int]:
        normalized: list[int] = []
        seen: set[int] = set()
        for value in values:
            shishen_id = int(value)
            if shishen_id <= 0:
                raise ValueError('shishen IDs must be positive integers')
            if shishen_id not in seen:
                seen.add(shishen_id)
                normalized.append(shishen_id)
        return normalized
    # 是否切换阴阳师
    switch_enabled: bool = Field(default=True, description='是否切换阴阳师')
    # 切换阴阳师
    switch_onmyoji: Onmyoji = Field(default=Onmyoji.YORIMITSU, description='切换阴阳师')
    # 一键切换斗技御魂
    switch_all_soul: bool = Field(default=False, description='switch_all_soul_help')
    # 限制时间
    limit_time: Time = Field(default=Time(minute=30), description='limit_time_help')
    # 目标分数
    target_score: int = Field(default=2000, description='target_score_help')
    # 刷满荣誉就退出
    honor_full_exit: bool = Field(default=False, description='honor_full_exit_help')
    # 是否开启绿标
    green_enable: bool = Field(default=False, description='green_enable_help')
    # 选哪一个绿标
    green_mark: GreenMarkType = Field(default=GreenMarkType.GREEN_LEFT1, description='green_mark_help')


class DuelCelebConfig(ConfigBase):
    # 是否开启名仕战斗
    celeb_battle: bool = Field(default=False, description='是否开启名仕战斗')
    # 填写第五手式神名称，如果阵容式神被办，第五手就会换式神，退出斗技
    ban_name: str = Field(default='', description='填写第五手式神名称')
    initial_score: int = Field(default=3800, description='设置初始斗技分值默认为8颗星之后每赢一场加100输一场减100')


class Duel(ConfigBase):
    scheduler: Scheduler = Field(default_factory=Scheduler)
    duel_config: DuelConfig = Field(default_factory=DuelConfig)
    duel_celeb_config: DuelCelebConfig = Field(default_factory=DuelCelebConfig)
    switch_soul: SwitchSoulConfig = Field(default_factory=SwitchSoulConfig)
