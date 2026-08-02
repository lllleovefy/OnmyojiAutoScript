# 斗技首手空上下文推荐修复 TDD 证据

## 来源与用户旅程

本任务由 2026-08-01 的两把 oas2 真机测试结果派生，没有单独的计划文件。

作为斗技玩家，我希望双方尚未上阵任何式神时，首手推荐使用经过贝叶斯平滑的
全局同手次统计，而不是把空上下文误认为精确历史，以免 1 场 1 胜的小样本候选
压过稳定的大样本候选。

## 根因

`personal_candidate_layers()` 使用：

```python
all(value is not None for value in opponent)
```

判断对方上下文是否完整。首手的 `opponent` 是空元组，而 Python 的
`all(())` 为真，因此全部首手历史被错误标成高优先级、未平滑的
`exact_history`。当前数据库中，这让 1 场 1 胜的 `255-阎魔` 以 1.000 排在
45 场样本候选之前。

## RED

先新增两个回归用例：

- `test_first_pick_without_context_uses_bayesian_global_fallback`
- `test_own_prefix_without_opponent_is_not_exact_history`

执行：

```text
D:\Game\oas-mine\toolkit\python.exe -m unittest \
  tests.test_duel_data_recommendation.DuelDataCandidateProviderTest.test_first_pick_without_context_uses_bayesian_global_fallback \
  tests.test_duel_data_recommendation.DuelDataCandidateProviderTest.test_own_prefix_without_opponent_is_not_exact_history
```

修复前结果：

```text
Ran 2 tests in 0.450s
FAILED (failures=2)
exact_history: 1/1 wins in 20 matching battles
```

失败原因正是目标业务缺陷，而不是测试配置或语法错误。

## GREEN

最小生产修改是要求对方上下文非空且所有槽位均已知：

```python
if opponent and all(value is not None for value in opponent):
```

相同两个测试修复后结果：

```text
Ran 2 tests in 0.454s
OK
```

完整验证：

```text
tests.test_duel_data_recommendation: 12/12 passed
tests/test_duel*.py: 167/167 passed
py_compile: passed
git diff --check (two affected files): passed
```

当前本地 300 场推荐数据复算：

```text
exact_history_count=0
selected_layer=round_global
393 score=0.7872 samples=45
341 score=0.7778 samples=7
597 score=0.6800 samples=23
255 score=0.6667 samples=1
577 score=0.6667 samples=1
```

## 保证清单

| # | 保证 | 测试类型 | 结果 |
| --- | --- | --- | --- |
| 1 | 首手无双方前缀时 exact 层为空 | 单元 | PASS |
| 2 | 首手使用贝叶斯 round_global，小样本 1/1 不再排名第一 | 单元 | PASS |
| 3 | 只有我方前缀、没有对方上下文时使用 own_prefix，不冒充 exact | 单元 | PASS |
| 4 | 部分未知的对方槽位仍保留位置并使用 opponent_known | 既有单元回归 | PASS |
| 5 | 完整且非空的对方上下文仍可进入 exact 层 | 既有单元回归 | PASS |

## 覆盖率与提交说明

当前 Toolkit Python 未安装 `coverage`（`No module named coverage`），因此没有生成
百分比覆盖率报告；本次使用推荐模块全套测试和 167 项斗技回归作为验证。

工作树在本任务开始前已经包含同一生产文件和测试文件的大量未提交斗技开发改动。
为避免把用户既有修改混入 RED/GREEN checkpoint，本次没有创建 checkpoint commit；
实际 RED/GREEN 命令和输出已记录在本文。
