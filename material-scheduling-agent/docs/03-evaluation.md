# 03 验证与限制

## 证据边界

当前环境没有模型凭据。真实模型适配器已实现，但真实模型调用与效果尚未实测。下列离线结果只用于证明工具边界、执行器、持久化和错误处理，不证明真实模型能正确重规划。因此，真实模型 ReAct 的完整验收仍待补充。

所有示例资料为合成数据。Trace 均由当次执行产生，记录动作、参数、返回结果和简短公开决策依据；不提交模型内部思维过程或密钥。工具观察保留了完整的小型合成结果，便于评审核对。

## 复现

在仓库根目录运行：

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
python scripts/run_examples.py
```

示例脚本为各场景使用临时隔离数据库，重复提交的两次调用共享数据库。运行后临时数据库删除；草稿回执与 Trace 保留在仓库中。脚本记录的绝对 Python 与临时数据库路径仅是当次执行环境记录，可复制的跨环境入口为上面的脚本命令。

## 实际 CLI 结果

[manifest.json](../examples/runs/manifest.json) 记录 **11 条 CLI 执行记录、10 组场景**，全部得到预期状态与退出码。以下时间均为合成日期 `2026-11-11`、UTC+08:00。

| 场景与输入 | 实际结果 | 输出与 Trace |
| --- | --- | --- |
| [正常排期](../examples/inputs/normal.json) | `completed`；审核通过，保存 19:00 草稿 | [输出](../examples/runs/normal.output.json) · [Trace](../examples/runs/normal.trace.json) |
| [缺失资料](../examples/inputs/missing.json) | `needs_information`；缺 `rights`，未保存 | [输出](../examples/runs/missing.output.json) · [Trace](../examples/runs/missing.trace.json) |
| [规则风险](../examples/inputs/risk.json) | `blocked`；命中 R01“保证收益”，未保存 | [输出](../examples/runs/risk.output.json) · [Trace](../examples/runs/risk.trace.json) |
| [时段冲突](../examples/inputs/conflict.json) | `pending_confirmation`；保留原 18:00 要求，补查并保存建议 19:00 的待确认草稿 | [输出](../examples/runs/conflict.output.json) · [Trace](../examples/runs/conflict.trace.json) |
| [重复提交](../examples/inputs/duplicate.json) | 首次 `saved`、再次 `existing`，返回同一草稿 ID | [首次输出](../examples/runs/duplicate_first.output.json) · [再次输出](../examples/runs/duplicate.output.json) · [再次 Trace](../examples/runs/duplicate.trace.json) |
| [保存失败](../examples/inputs/save_failure.json) | `failed`；注入一次 `save_draft` 失败，保留审核/查询结果，无成功 ID | [输出](../examples/runs/save_failure.output.json) · [Trace](../examples/runs/save_failure.trace.json) |
| [工具错误](../examples/inputs/tool_failure.json) | 读取首次失败，Mock 观察错误后重试一次，最终 `completed` | [输出](../examples/runs/tool_failure.output.json) · [Trace](../examples/runs/tool_failure.trace.json) |
| [调用上限](../examples/inputs/step_limit.json) | 上限 1 次；仅完成读取，返回 `incomplete`，未保存 | [输出](../examples/runs/step_limit.output.json) · [Trace](../examples/runs/step_limit.trace.json) |
| [更换占用时段](../examples/inputs/changed_schedule.json) | 把占用改为 19:00 后，直接保存原 18:00，返回 `completed` | [输出](../examples/runs/changed_schedule.output.json) · [Trace](../examples/runs/changed_schedule.trace.json) |
| [明确确认替代时段](../examples/inputs/confirmed_alternative.json) | 首次请求原 18:00、已确认 19:00；草稿 `confirmation_status=confirmed` | [输出](../examples/runs/confirmed_alternative.output.json) · [Trace](../examples/runs/confirmed_alternative.trace.json) |

正常路径为“读取 → 审核 → 查询 → 保存 → 结束”；冲突路径多一次替代时段查询。更换占用时段后，查询结果与后续动作随之变化。所有输出均为 `published=false`。这些差异是 Mock 观察驱动的流程证据，不能替代真实模型决策记录。

## 可执行测试

最终运行 **35 项测试全部通过**，退出码 `0`，测试执行耗时 `0.162s`；完整实际输出见 [test-results.txt](../examples/test-results.txt)。其中 [test_agent.py](../tests/test_agent.py) 22 项、[test_tools.py](../tests/test_tools.py) 8 项、[test_models.py](../tests/test_models.py) 5 项。

测试覆盖正常、缺失、风险、冲突、重复提交、保存失败、并发幂等、同键变参、模型越过审核/伪造完成、保存前规则与占用复检、真实文件系统保存错误、等价时区及非法参数等边界。模型适配器测试使用受控 HTTP 响应验证请求和解析契约，不是实际大模型调用。

## 真实模型补证

安全配置 `OPENAI_API_KEY` 与 `OPENAI_MODEL` 后运行：

```bash
python scripts/run_live_examples.py
```

已实际验证 live 模式在缺少凭据时明确失败且不回退 Mock，输出见 [live_unavailable.output.json](../examples/runs/live_unavailable.output.json)。这是配置失败记录，没有发生真实模型调用。

补证脚本将正常与冲突场景的实际输出、Trace 和汇总写入 `examples/live/`，不会使用 Mock 代替失败的真实调用。需检查模型是否根据工具结果选择不同动作、是否正确保留硬性要求，以及保存回执是否支持最终状态。汇总状态通过也不代替人工检查。

## 已知限制

1. 尚无两条真实模型执行路径；完整模型决策验收未完成。
2. 合成关键词审核规则不是生产内容审核服务，不能代表复杂语义风险识别能力。
3. 本地 JSON 与单机 SQLite 不模拟线上权限、跨系统事务或生产发布。
4. 待确认草稿暂占时段，尚未实现后续确认、修改、取消或过期释放。旧 ID 加确认会幂等冲突，新 ID 仍受旧草稿占用影响。
5. 模型上下文随工具观察增长；当前依靠调用上限控制规模，没有长任务摘要或失败步骤续跑功能。
