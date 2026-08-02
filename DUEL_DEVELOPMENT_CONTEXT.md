# OAS/OASX 斗技功能开发上下文

> 最后更新：2026-08-02（Asia/Shanghai）
> 用途：后续开发、真机联调和新会话接手。本文不包含任何登录凭据。

## 当前结论

斗技数据库、顺序 BP 账本、分层 Top-5 推荐、候选扫描与安全点击、实时 SSE、头像纠错接口和 OASX 页面已经实现。阳间斗技仅用于本开发环境的一次性冷启动数据，运行时不再依赖它。

当前不能宣布 AUTO 真机可用，原因如下：

1. 严格提取及人工目录归档只确认了 274 个式神中的 47 个 ID（81 张候选模板），仍缺少 227 个 ID；这会降低对方式神头像识别率，但不再是 AUTO 硬门禁。
2. 持久门禁 `bp_auto_verified=false`。必须先通过离线回放和 oas2 `recommend` 真机验证，再显式开启。

第六轮不再要求六张阴阳师大模型模板，也不在点击固定槽位后做身份图像匹配。运行时改为点击前、点击后各用一张新鲜截图确认仍处于可操作的第六轮，再点击“确定”并验证转场。

`oas2` 继续保持 `bp_mode=observe`、`bp_opponent_inspect_enabled=false`、`bp_sample_capture_enabled=true`、`switch_enabled=false`；不得操作 `oas1`。这里的旧配置 `switch_enabled=false` 只是不在任务准备阶段预切换阴阳师，不会禁用 BP 第六轮读取 `switch_onmyoji=源赖光`。

## 工作区

### OAS

- 目录：`C:\Users\liudh\.codex\worktrees\ac71\oas-mine`
- 分支：`dev/douji`
- 真机配置：`config/oas2.json`
- oas2 ADB：`127.0.0.1:16512`

### OASX

- 目录：`D:\liudh\OASX`
- 分支：`cobra`
- 斗技代码仍为未提交修改。
- 工作树中原有的 generated 文件和其他修改必须保留，不得清理或混入斗技提交。

## 本地初始化数据

数据库：`config/duel/duel.sqlite3`

该数据库、本地头像库和原始视频均被 Git 忽略，只属于当前开发环境。
当前数据库 schema `user_version=3`。

| 数据 | 当前数量 |
|---|---:|
| `duel_accounts` | 2 |
| `duel_matches` | 333 |
| `duel_picks` | 3769 |
| `duel_strategies` | 0 |
| `duel_import_runs` | 2 |
| `external_snapshots` | 2 |
| `recommendation_snapshots` | 2 |
| `duel_portrait_templates` | 81 |

其中 326 场战报来自一次性初始化，其后 OAS 可独立查询、统计、推荐和记录。运行时不读取或刷新阳间斗技会话。

## 式神素材提取

### 输入与输出

- 输入视频：`log/式神素材视频.mp4`
- 视频信息：1280×720、约 107.8 秒、3233 帧
- 现有补充样本：`log/duel/bp_samples/oas2/20260729T222420Z-live`
- 素材库：`config/duel/portrait_library/`
- 覆盖报告：`config/duel/portrait_library/coverage.json`
- 已命名索引：`config/duel/portrait_library/index.jsonl`
- 待确认索引：`config/duel/portrait_library/unresolved.jsonl`
- 离线工具：`dev_tools/duel_extract_portraits.py`
- 待确认名称归档工具：`dev_tools/duel_resolve_unresolved.py`

标准运行方式：

```powershell
D:\Game\oas-mine\toolkit\python.exe -m dev_tools.duel_extract_portraits `
  "log\式神素材视频.mp4"
```

仅分析：

```powershell
D:\Game\oas-mine\toolkit\python.exe -m dev_tools.duel_extract_portraits `
  "log\式神素材视频.mp4" --analyze-only
```

工具已实现：

- Windows 中文视频路径的 ASCII 临时副本。
- 10 FPS 扫描和连续三帧稳定门槛。
- 动态候选列表基准定位，并兼容计划中的固定 ROI。
- 多裁剪 OCR、规范名/别名严格解析、0.90 相似度和 0.08 差值约束。
- 同一身份至少两个独立裁剪一致才自动命名。
- SHA-256、感知哈希和视觉相似度幂等去重。
- 阵容网格只和已确认候选模板关联，不按位置或出现顺序命名。
- 失败样本记录时间、帧号、槽位、ROI、OCR 文本、最高候选和置信度。
- 重复执行不增加重复模板或数据库记录。

### 当前覆盖

| 项目 | 数量 |
|---|---:|
| 映射总 ID | 274 |
| 已覆盖 ID | 47 |
| 缺失 ID | 227 |
| 已命名模板 | 81 |
| 候选待确认 | 0 |
| 阵容待确认 | 0 |
| 待确认合计 | 0 |

所有 81 个已命名文件均符合：

```text
ID-式神名/ID-式神名-候选-序号.png
```

2026-08-01 对 225 组候选名称图执行了严格 OCR 与离线增强 OCR。没有任何自动结果达到两个独立变体一致；最终只接受用户明确放入对应 `ID-式神名` 目录的 10 组头像，并把错放到 `385-大夜摩天阎魔` 目录的旧阎魔模板按父目录纠正。共规范新增 11 张、移除 1 条断裂索引，覆盖由 46 提升为 47。其余 215 组候选和 270 张无名称阵容图已舍弃，`unresolved.jsonl`、`_unresolved/` 和数据库 unresolved 行均为空。处理报告为 `config/duel/portrait_library/resolve_report.json`，识别计划保留在 `log/duel/portrait-resolve-plan-20260801.json`。

已命名索引中的文件全部存在且可解码；没有为了凑齐 274 个 ID 而降低双票规则或误标素材。再次运行视频提取工具会从原始视频重新生成待确认样本。

提取前旧素材备份：

```text
log/duel/portrait_library_pre_strict_20260730
```

数据库同步前备份：

```text
log/duel/duel-before-portrait-sync-20260730.sqlite3
```

## BP 顺序与身份模型

状态机：

```text
BAN -> SELF_PICK <-> OPPONENT_PICK -> READY -> BATTLE -> RESULT
```

阴阳师选择不是独立的 `DuelBPState`。它是第五手完成后锁存的第六轮界面信号；只有阴阳师选择控件消失或最终阵容出现后才进入 `READY`。

用户确认的空间规则：

- 我方为左侧蓝色区域。
- 我方顶部空间槽位从左至右为 1、2、3、4、5。
- 槽位空间顺序和实际出手时间顺序不能混用。
- 第六轮是阴阳师选择，不是 BP 完成。

我方式神身份只来自“选择账本”：

1. 扫描候选卡并按 Top-5 优先级选择。
2. 点击候选。
3. 用左侧大名称 ROI `(170,150,40,165)` 连续三帧确认目标 ID。
4. 点击“确定”。
5. 稳定检测到确认后的转场才提交我方账本。

`recommend` 模式不点击，但会观察人工选择的大名称和确认转场。进入每轮时，首个稳定名称只作为默认高亮基线；必须先观察到连续三帧稳定的人工变更，且随后确认转场成立，才提交人工账本。若未证明选择变化就发生确认，则该手记录为 `unknown` 并停止后续人工顺序推断，避免把默认高亮误记为玩家选择。`observe` 不推断人工账本。

对方身份：

- 对方顶部槽位从左至右处理。
- 默认关闭“点击对方槽位查看名称”，因为真机尚未证明该点击无副作用。
- 优先使用多视图头像库；同一 ID 连续三帧且置信度不低于 0.98 才写入。
- 未达到门槛时记录 `unknown`、最高候选和置信度，并继续后续推荐。

## 推荐顺序

每轮返回前五名，固定按以下层级：

1. 用户攻略规则。
2. 完整条件个人历史。
3. 对方已知槽位统计。
4. 我方已选前缀统计。
5. 同手次全局贝叶斯平滑胜率。
6. 一次性冷启动榜单。

Ban 当前不识别，也不作为推荐条件。对方存在未知槽位时不会阻塞推荐。

稳定变化会输出 INFO，逐帧原始候选仅在显式调试配置下输出 DEBUG。SSE 同步轮次、双方账本、识别来源与置信度、Top-5、兜底层级和点击校验状态。所有实时事件带当前 API 进程内单调递增的 revision/SSE `id`，缓存重放严格按 revision 排序；OASX 会丢弃重复或迟到事件。snapshot 会把状态和动作的同名置信度分别归一化为 `recognition_confidence` 与 `action_confidence`。进程重启后 revision 从头开始，没有持久事件日志，也不承诺按 `Last-Event-ID` 无损补齐；断线恢复的语义是重新取得最新 snapshot 和进程内缓存。

## AUTO 安全约束

AUTO 只在以下条件全部成立时执行：

- OAS 与 OASX 指向项目内规范素材库 `config/duel/portrait_library`，不能用可配置的外部目录替代。
- 当前 `shishen_assets` 名称映射可用；头像覆盖率只影响对方识别质量，不阻止 AUTO。
- `bp_auto_verified=true`，表示离线回放与 oas2 `recommend` 真机验证已经完成。
- 式神候选识别、确认 ROI、稳定帧数、置信度和扫描预算配置有效。
- 我方式神选择目标经过三帧稳定名称校验。

OASX 在门禁未满足时拒绝保存 AUTO；即使绕过前端把 OAS 配置写成 `auto`，后端创建助手时仍会降级为 `recommend`。`SELF_PICK` 的动作置信度只取 active-confirm 本身，不能被对方或阴阳师覆盖层抬高；三帧名称 OCR 取实际最低置信度，稳定帧数本身不会把 0.97 提升到 0.98。

候选列表扫描：

- 当前页找不到第一名时，按 Top-5 依次降级。
- 每页选择可见的最高排名、可点击且未被禁用的候选。
- 只做有限次数和 5 秒预算内的横向扫描。
- 页面视觉指纹避免在空识别页无限重复。
- 名称冲突、禁用或单个候选校验失败时尝试 Top-5 的下一项。
- 每次候选横向滑动、候选卡点击和阴阳师槽位点击前都刷新截图，并重新确认专用 active-confirm 控件；窗口丢失立即停止，不回退到固定坐标或旧准备逻辑。
- Top-5 全部不可用、没有稳定推荐或推荐置信度不足，且尚未产生 pending 时，允许按既定设计回退游戏原生“自动上阵”；一旦候选已点击、确认转场不确定或已有 pending，则冻结当前手，禁止第二次点击和原生回退。

五手完成后进入阴阳师轮。六个阴阳师使用现有固定槽位映射；大模型图片仅可作为离线参考，不再参与运行时门禁或身份验证。执行顺序为：

1. 新鲜截图确认五手账本已完成、阴阳师选择页存在且“确定”可点击。
2. 点击 `switch_onmyoji` 对应的固定槽位。
3. 短暂等待后重新截图，再次确认仍处于同一可操作窗口；窗口丢失则不点“确定”，最多重试三次。
4. 点击“确定”，再通过既有三帧转场检查确认已离开选择页。

这条路径验证的是固定槽位和操作时机，不再声称通过图像二次验证了阴阳师身份。

进入真实战斗后复用原 Duel 战斗逻辑，自动战斗只开启一次。

## OAS API

本地 API：

- `GET /duel-data/matches`
- `GET /duel-data/matches/{id}`
- `PATCH /duel-data/matches/{id}`
- `GET /duel-data/summary`
- `GET /duel-data/live`
- `GET /duel-data/live/snapshot`
- `GET /duel-data/shishen-assets`
- `GET /duel-data/portraits/status`
- `GET /duel-data/portraits/unresolved`
- `GET /duel-data/portraits/{id}/image`
- `POST /duel-data/portraits/{id}/label`

头像图片接口只允许返回素材库内的 PNG，拒绝路径穿越，并返回 `no-store` 和 `nosniff`。纠错接口必须提交本地映射中存在的 ID 和对应规范名；别名会归一化，ID/名称错配返回 422。人工纠错后会把待确认素材提升到标准目录，立即写入本地模板表并原子更新覆盖报告。文件序号、索引和数据库 upsert 均有并发保护，重复纠错不会覆盖文件或新增重复记录。

所有 `/duel-data` 路由只接受 loopback 客户端，并拒绝非本机 Origin；它们不是局域网或公网接口。

`GET /duel-data/portraits/status` 同时返回 `library_path`、`asset_id_count`、`covered_id_count` 和 `coverage_complete`。当前为 `81 total / 81 recognized / 0 unresolved / 274 assets / 47 covered`，这些覆盖字段只用于诊断对方头像识别质量。为兼容旧版 OASX，接口暂时继续返回 `onmyoji_ready=true` 和空的 `missing_onmyoji_templates`，其含义是固定槽位流程可用，并非已具备六张模板；当前 OASX 已不再用该字段决定 AUTO 是否就绪。

一次性阳间斗技初始化已经在本开发环境完成，运行时不暴露 `/duel-data/import/*` 路由；后续接手无需继续实现或寻找 status/preview/commit 导入端点。

## OASX

斗技页面包含：

- 战报汇总、列表和纠错。
- 实时 BP 状态、双方账本、识别置信度和 Top-5 推荐解释。
- SSE 断线恢复及 Web 轮询降级；Web 会立即并每 5 秒读取权威完整快照，新轮快照会清空旧推荐和旧动作。
- Native/Web 均丢弃重复 revision 和旧轮次 recommendation/action；切换活动脚本时以 generation 隔离旧 transport 和迟到响应。
- 待确认头像列表、图片预览和人工纠错。
- 按 ID、规范名和别名搜索的受控自动完成框。
- `off / observe / recommend / auto` 模式控制。
- 规范素材库路径、式神名称映射或持久验证门禁未就绪时禁用 AUTO；头像覆盖率仅作质量诊断。

主要文件：

```text
D:\liudh\OASX\lib\api\api_client_duel.dart
D:\liudh\OASX\lib\modules\duel\models\duel_models.dart
D:\liudh\OASX\lib\modules\duel\controllers\duel_controller.dart
D:\liudh\OASX\lib\modules\duel\duel_view.dart
D:\liudh\OASX\test\duel_models_test.dart
```

## 已完成验证

- OAS 斗技单元测试：`194/194` 全部通过。
- OAS 新增/修改 Python 文件：`py_compile` 通过。
- `config/template.json` 和 `config/oas2.json`：JSON 解析通过。
- OAS：`git diff --check` 通过。
- OASX 模型/Controller 定向测试：`29/29` 全部通过。
- OASX 全项目：`flutter analyze` 无问题。
- OASX：`git diff --check` 通过。
- OASX 全套测试中 48 项通过，旧的 `widget_test.dart` 计数器冒烟测试因未注册 `LocaleService` 失败；该测试不进入斗技页面，失败与本次斗技代码无关，未修改该旧测试或全局依赖注入。
- 数据库所有文本字段扫描：未发现登录凭据或认证请求头内容。
- `config/duel` 与 `log/duel` 文本文件扫描：未发现登录凭据或认证请求头内容。

## 下一次真机测试顺序

只使用 oas2：

1. 保持 `observe`，验证五轮状态、对方未知槽位、SSE 和日志。
2. 切换 `recommend`，人工按推荐选择，验证每轮人工账本提交和下一轮推荐。
3. 离线回放验证五轮名称选择、点击校验、未知对方继续推荐及第六轮固定槽位的点击前后窗口复核。
4. 验证候选列表每轮是否回到固定起点；当前扫描只向前滑动，若列表保留上一轮位置需要补双向恢复。
5. 上述项目通过后才把 `bp_auto_verified` 临时改为 `true` 并启用 `auto`，先验证单手，再验证五手和阴阳师。
6. 进入战斗后确认自动战斗只触发一次。

仍需重点观察：

- 对方头像库覆盖不足时，大部分槽位会是 `unknown`，但推荐应继续。
- 普通确认转场若在约 1.8 秒窗口内完全丢帧，会保留 pending 并停止继续点击，这是预期的安全失败。
- 对方槽位点击查看名称继续保持关闭，直到真机证明不会改变我方选择状态。
- 当前仅验证 1280×720 名士界面；其他分辨率和 UI 版本尚未适配。
