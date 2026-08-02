# 名称驱动 BP 自动上阵：TDD 证据

日期：2026-08-01

## RED

- OAS 新增测试首先证明旧实现仍会：
  - 因 `portrait_coverage_report_mismatch` 和 `portrait_templates=0/1` 拒绝部分头像库；
  - 在候选小名称未知时仅凭 `source=portrait` 点击并提交 `596`；
  - 初始化我方候选头像匹配器；
  - 接受置信度 `0.97` 的候选名称。
- 单票精确名称定位测试首先以 `unexpected keyword argument 'min_consensus'` 失败。
- OASX 新增测试首先证明：`coverageComplete=false` 会错误禁用 AUTO，而空式神名称映射仍会错误启用 AUTO。
- 审查补充 RED：损坏 PNG 被错误视为阴阳师模板就绪；`{items:[...]}` 名称映射无法供运行时使用；名称接口异常会阻断 recommend 模式加载；基础式神的精确全名被长名称歧义规则永久拒绝；运行时绕过统一 OCR 服务。

## GREEN

- `StrictNameRecognizer.recognize(..., min_consensus=1)` 只允许可逆定位提前接受一个精确规范名/别名；默认仍要求两个独立裁剪结果一致。
- 我方候选扫描只读取竖排名称，不再初始化或调用头像匹配器。
- 可逆的候选定位允许单个规范名/别名精确命中，并保留原始 OCR 分数；非精确命中仍须达到 `bp_auto_confidence`。名称未知、ID 冲突或含“禁”均不点击。
- 点击后的左侧大名称继续使用默认严格识别、连续三帧和 `>=0.98` 门槛；行动窗口、确认按钮和转场复核保持不变。
- 头像覆盖率和 `coverage.json` 退出 AUTO 硬门槛；规范素材库路径、式神名称映射和 `bp_auto_verified` 仍为硬门槛。
- OASX 使用相同就绪规则；覆盖率仅作为对方头像识别质量诊断。
- 名称映射统一兼容顶层列表和 `{items|data|list:[...]}` 包装；运行时改用统一 OCR 服务，避免首轮同步加载本地模型。
- 名称接口暂时失败只会关闭 AUTO，不再阻断 off/observe/recommend 模式加载。

2026-08-02 后续调整：按用户要求，六张阴阳师 selected 大图及点击后的身份比对均从运行时移除。阴阳师改用固定槽位点击、点击前后动作窗口复核及确认转场验证；状态接口为兼容 OASX 固定返回 `onmyoji_ready=true` 和空缺失列表。

## 验证

- OAS 定向 GREEN：5/5 通过。
- OAS 全部斗技测试：175/175 通过。
- OAS 修改文件 `py_compile` 与 `git diff --check` 通过。
- OASX 斗技测试：29/29 通过。
- OASX `flutter analyze`：No issues found。
- OASX 除仓库既有 `widget_test.dart` 外的全部测试：51/51 通过。
- OASX 全量测试唯一失败为既有 Counter smoke test 未注册 `LocaleService`，与斗技改动无关。

OAS 自带 Python 未安装 `coverage`（`No module named coverage`），因此不虚报覆盖率百分比；上述回归集覆盖名称解析、候选定位、点击前复核、AUTO 持久门禁、损坏模板、包装数据契约和 OASX 异常降级。

未创建 TDD 检查点提交：OAS 和 OASX 均已有大量用户未提交且与斗技重叠的修改，本次保持工作区原状，不擅自暂存或提交。
