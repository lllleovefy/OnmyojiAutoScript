# 严格式神池 TDD 证据

日期：2026-08-02

## 范围

- `bp_shishen_pool` 保存规范化、去重的式神 ID 列表；空列表保持原有不限制行为。
- 攻略、个人历史和冷启动候选统一先经过池白名单，并排除我方已选式神。
- 候选扫描拒绝池外式神；没有可执行池内候选时返回未执行，保留旧自动上阵兜底。
- OASX 支持按 ID、名称、别名搜索，多选、数量提示和保存。

## RED

OAS 目标测试最初 5/5 失败，分别证明配置字段、JSON 参数类型、扫描白名单、三类候选过滤和兜底边界尚未实现：

```text
FAILED (errors=5)
AttributeError: DuelConfig has no attribute bp_shishen_pool
ImportError: _coerce_script_argument_value
TypeError: unexpected keyword argument allowed_ids
ValueError: DuelConfig has no field bp_shishen_pool
```

OASX 目标测试最初编译失败，缺失 `shishenPoolIds`、搜索、切换、数量、脏状态和保存接口。

## GREEN

```text
python -m unittest <5 strict-pool target tests>
Ran 5 tests ... OK

flutter test test/duel_models_test.dart --plain-name "loads searches toggles and saves the strict shishen pool"
+1: All tests passed!
```

## 回归

```text
python -m unittest discover -s tests -p 'test_duel*.py'
Ran 199 tests ... OK

python -m py_compile tasks/Duel/config.py tasks/Duel/selection.py tasks/Duel/script_task.py module/server/script_router.py
OK

flutter test test/duel_models_test.dart
+30: All tests passed!

flutter analyze lib/modules/duel test/duel_models_test.dart
No issues found!
```

本次没有创建 TDD 检查点提交：OAS 与 OASX 都已有用户未提交的共享开发修改，按要求保持现状且不混入提交操作。
