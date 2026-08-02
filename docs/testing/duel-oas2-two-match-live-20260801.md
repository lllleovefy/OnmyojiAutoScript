# oas2 两把斗技真机测试（2026-08-01）

## 测试目标

- 只操作 oas2：`127.0.0.1:16512`，分辨率 `1280x720`。
- 连续完成两把名士斗技，检查实时 BP 推荐、推荐上阵、进入战斗后的自动战斗与结算。
- 不改写持久配置；本次运行仅在进程内使用 `recommend` 模式。
- 不绕过 AUTO 安全门禁。

## 结果

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 连续完成两把 | 通过 | `battle count:2`，测试进程正常退出 |
| 实时首手推荐 | 通过 | 两把均输出 `BP[round=1][recommend] id=255 name=阎魔` |
| Top-5 推荐 | 通过 | 两把均返回 `255,577,341,393,597` |
| 按推荐自动点击候选 | 未通过/未执行 | `recommend` 是被动模式，没有 `BP[*][action]` |
| 我方选择校验 | 未通过 | 游戏超时使用默认高亮，日志为 `selected_verified=false`、`reason=no_selection_change_proof` |
| 第 2～5 手顺序推荐 | 未验证 | 第 1 手无法安全写入我方账本，后续推断按设计停止 |
| 对方式神识别 | 未验证 | 本次没有稳定的 `BP[*][opponent]` 事件；当前候选头像覆盖为 `47/274` |
| 自动战斗 | 通过 | 两把进入实战后均从手动切到自动 |
| 战斗结算 | 通过 | 两把均正常识别为失败；最终 `win:0 failure:2` |

首手推荐内容：

```text
id=255 name=阎魔 source=personal context=exact_history score=1.0000
top5=255:1.0000,577:1.0000,341:0.8571,393:0.8000,597:0.6957
```

## 真机中发现并修复的问题

第一次试跑进入实战后，BP 发布逻辑把 `BATTLE_STATUS_S` 卡死保护覆盖成
`DUEL_BP_PROGRESS`，导致战斗超过 60 秒时抛出 `GameStuckError`。

### RED

新增回归测试：

```text
DuelTaskSafetyTest.test_battle_state_does_not_replace_battle_stuck_record
```

修复前，稳定的 `BATTLE` 状态会错误调用：

```text
Expected: []
Actual:   ['DUEL_BP_PROGRESS']
```

测试同时覆盖 `RESULT`，保证结算状态也不会覆盖战斗卡死保护。

### GREEN

状态仍正常发布到实时接口，但 `BATTLE` 和 `RESULT` 不再调用
`reset_device('DUEL_BP_PROGRESS')`。修复后两把实战分别持续约 106 秒和
140 秒，均没有再发生 60 秒卡死，且都正常到达结算。

## AUTO 未启用的原因

当前持久配置仍为 `bp_auto_verified=false`。后端还会校验：

- 规范本地素材库路径和式神名称映射是否可用。
- 离线回放和 recommend 真机验证是否完成。

`47/274` 头像覆盖率现在只影响对方头像识别质量，不再阻止 AUTO。2026-08-02 起，六个阴阳师 selected 大图及点击后身份匹配也不再是运行时要求；第六轮改为固定槽位点击、点击前后动作窗口复核和确认转场验证。

因此把配置写成 `auto` 也会安全降级到 `recommend`。本次没有伪造验证状态，
也没有把游戏的超时默认上阵当作 OAS 自动上阵成功。

## 日志与样本

- 主日志：`log/2026-08-01_run.txt`
- 第一次失败试跑：`log/duel/live-test-oas2-20260801-213055/attempt1-crash.log`
- 第一把有效样本：`log/duel/bp_samples/oas2/20260801T135130.202890Z-7d90ed8e`
- 第二把有效样本：`log/duel/bp_samples/oas2/20260801T135552.666643Z-71ce3588`

工作树在测试前已包含大量未提交的斗技开发改动，因此本次没有自动创建
RED/GREEN checkpoint commit，避免把用户的既有修改混入测试提交。
