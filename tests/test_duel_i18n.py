import json
import unittest
from pathlib import Path


class DuelI18nTest(unittest.TestCase):
    def test_bp_settings_have_chinese_labels_and_help_text(self) -> None:
        translations = json.loads(
            Path("assets/i18n/zh-CN.json").read_text(encoding="utf-8")
        )
        expected = {
            "bp_mode": "斗技 BP 助手模式",
            "duel_bp_mode_help": "控制斗技 BP 助手的参与程度：关闭不启用；观察仅识别和采集；推荐只给出候选，不执行点击；自动会在通过安全校验后自动选择。",
            "bp_stable_frames": "状态稳定帧数",
            "duel_bp_stable_frames_help": "同一识别结果连续达到此帧数后，才确认 BP 状态变化（3～10）。",
            "bp_recommend_confidence": "推荐置信度阈值",
            "duel_bp_recommend_confidence_help": "识别置信度达到此值后，才生成并采用 BP 推荐（0～1）。",
            "bp_auto_confidence": "自动操作置信度阈值",
            "duel_bp_auto_confidence_help": "自动模式执行选择操作所需的最低置信度；为保证安全，取值范围为 0.98～1。",
            "bp_personal_min_samples": "个人数据最少样本数",
            "duel_bp_personal_min_samples_help": "个人历史数据达到此样本数后，才用于调整推荐顺序。",
            "bp_identity_enabled": "启用式神身份识别",
            "duel_bp_identity_enabled_help": "识别双方已选式神和禁选式神，为分轮推荐提供阵容信息。",
            "bp_identity_confidence": "身份识别置信度阈值",
            "duel_bp_identity_confidence_help": "式神身份识别结果被接受所需的最低置信度（0.5～1）。",
            "bp_identity_regions": "身份识别区域",
            "duel_bp_identity_regions_help": "双方已选与禁选区域的 JSON 配置；每个区域格式为 [左上角X, 左上角Y, 宽度, 高度]。",
            "bp_pick_targets": "选择目标映射",
            "duel_bp_pick_targets_help": "式神选择目标的 JSON 映射预留项；当前流程未使用，通常保持 {}。",
            "bp_shishen_pool": "可选式神池",
            "duel_bp_shishen_pool_help": "允许推荐和自动选择的式神 ID 列表；留空表示不限制。",
            "bp_portrait_library": "式神头像库路径",
            "duel_bp_portrait_library_help": "用于识别候选和对方式神的头像素材库目录；自动模式必须使用项目内默认目录。",
            "bp_candidate_swipe_limit": "候选列表最大滑动次数",
            "duel_bp_candidate_swipe_limit_help": "查找推荐式神时，横向滑动候选列表的最大次数（1～40）。",
            "bp_candidate_scan_budget": "候选扫描时间上限",
            "duel_bp_candidate_scan_budget_help": "每轮查找候选式神允许消耗的最长时间，单位为秒（1～8）。",
            "bp_opponent_inspect_enabled": "允许查看对方式神",
            "duel_bp_opponent_inspect_enabled_help": "推荐或自动模式下，允许点击对方槽位查看式神名称；尚未真机验证时请保持关闭。",
            "bp_log_raw_frames": "记录逐帧调试日志",
            "duel_bp_log_raw_frames_help": "输出每帧的原始识别候选和置信度，仅用于调试；开启后日志量会明显增加。",
            "bp_sample_capture_enabled": "采集 BP 识别样本",
            "duel_bp_sample_capture_enabled_help": "运行斗技时定期保存 BP 截图和识别信息，用于离线分析与校准。",
            "bp_sample_capture_interval": "样本采集间隔",
            "duel_bp_sample_capture_interval_help": "两次 BP 样本采集之间的最短间隔，单位为秒（0.25～10）。",
            "bp_auto_verified": "自动模式验证已完成",
            "duel_bp_auto_verified_help": "自动模式安全门禁。仅在离线回放和推荐模式真机验证通过后开启；否则自动模式会降级为推荐模式。",
        }

        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertEqual(value, translations.get(key))

    def test_bp_mode_options_have_chinese_labels(self) -> None:
        translations = json.loads(
            Path("assets/i18n/zh-CN.json").read_text(encoding="utf-8")
        )

        self.assertEqual("关闭", translations.get("off"))
        self.assertEqual("观察", translations.get("observe"))
        self.assertEqual("推荐", translations.get("recommend"))
        self.assertEqual("自动", translations.get("auto"))


if __name__ == "__main__":
    unittest.main()
