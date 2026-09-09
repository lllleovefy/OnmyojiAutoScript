# 道馆集结识别 ROI 修复：TDD 证据

## 来源与用户场景

本次没有外部计划文件。用户场景：道馆仍在集结倒计时时，脚本应容忍文字出现少量位置偏移，正确识别“开战”，避免误点“挑战”并在准备前退出。

## RED / GREEN

| 阶段 | 验证命令 | 结果 | 证据 |
|---|---|---|---|
| RED | `.\toolkit\python.exe -m unittest tests.test_dokan_gathering_template -v` | 失败 | 模拟素材向右 2 像素、向上 1 像素后，匹配分 `0.41296`，低于阈值 `0.85`。 |
| GREEN | `.\toolkit\python.exe -m unittest tests.test_dokan_gathering_template -v` | 通过 | 扩大搜索 ROI 后，同一位置偏移用例通过。 |
| 真实截图 | 使用生产 ROI 对三张错误截图执行 OpenCV 模板匹配 | 通过 | 三张截图匹配分均为 `0.90350`，高于阈值 `0.85`。 |
| 完整回归 | `unittest.defaultTestLoader.discover('tests')` | 通过 | 共运行 `207` 项测试，全部通过。 |

## 测试保证

| # | 保证 | 测试文件 | 类型 | 结果 |
|---|---|---|---|---|
| 1 | 集结文字发生小幅位置偏移时仍能被现有素材和阈值识别 | `tests/test_dokan_gathering_template.py` | 单元/资源配置回归 | PASS |

## 覆盖率与已知缺口

工具包未安装 `coverage` 模块，无法生成百分比报告。本次生产修改仅调整一个资源搜索 ROI；自动测试直接读取生产 ROI、阈值和素材并覆盖该配置行为，另以三张真实失败截图完成回归验证。
