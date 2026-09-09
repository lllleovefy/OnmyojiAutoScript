# 斗技 BP 参数中文说明 TDD 记录

## 来源与用户场景

本次用户场景由界面截图和需求直接整理：作为中文界面用户，我希望斗技新增的 BP 参数、帮助说明和模式选项显示中文，以便理解配置用途及安全限制。

## 执行记录

| 阶段 | 验证命令 | 结果 | 保证 |
|---|---|---|---|
| RED | `python -m unittest tests.test_duel_i18n -v` | 失败：37 个中文翻译断言缺失 | 测试能复现界面显示原始键名和英文模式值的问题 |
| GREEN | `python -m unittest tests.test_duel_i18n -v` | 通过：2 个测试 | 18 个 BP 参数的名称和帮助说明，以及 4 个模式选项均有预期中文文案 |
| JSON 校验 | `python -m json.tool assets/i18n/zh-CN.json` | 通过 | 中文翻译文件仍是合法 JSON |
| 格式校验 | `git diff --check` | 通过 | 修改没有空白或补丁格式问题 |
| 兼容性抽查 | `python -m unittest tests.test_duel_i18n tests.test_duel_bp -v` | 30 项通过，2 项因环境缺少 `inflection`、`rich` 未能加载 | 已执行的 BP 核心逻辑测试未出现回归；两项加载错误与翻译资源无关 |

## 测试规格

| # | 保证内容 | 测试 | 类型 | 结果 |
|---|---|---|---|---|
| 1 | 所有新增斗技 BP 参数均显示准确的中文名称和帮助说明 | `tests.test_duel_i18n.DuelI18nTest.test_bp_settings_have_chinese_labels_and_help_text` | 单元/资源回归 | PASS |
| 2 | BP 模式选项显示为“关闭、观察、推荐、自动” | `tests.test_duel_i18n.DuelI18nTest.test_bp_mode_options_have_chinese_labels` | 单元/资源回归 | PASS |

## 覆盖率与已知缺口

测试逐键覆盖本次新增的全部 40 项目标文案（36 项参数名称/说明和 4 项模式选项），任务范围内覆盖率为 100%。该改动只涉及静态翻译资源，未运行浏览器端到端截图测试；服务重新加载翻译资源后即可在配置页显示。扩展兼容性测试中的两项既有用例受本地缺少 `inflection` 和 `rich` 依赖阻塞，未把环境加载错误记为通过。

## 合并证据

- RED 检查点：`3acb96aa test: add reproducer for duel BP translations`
- GREEN 检查点：`cf1aa540 fix: localize duel BP settings in Chinese`
