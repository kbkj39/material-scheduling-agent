# 素材审核与发布排期 Agent

小影校招 AI Agent 开发考核，第 **1 题**。帮助内容团队审核素材、识别排期冲突并保存可确认的草稿；不接入真实发布。

GitHub：[kbkj39/material-scheduling-agent](https://github.com/kbkj39/material-scheduling-agent)。

> 当前证据边界：已提供真实模型工具调用接口和离线 Mock 入口。仓库内 Mock 记录只能验证流程与工程边界，不能证明真实模型的 ReAct 决策效果。当前未配置模型凭据，真实模型路径尚未实测，完整的模型决策验收仍待补充。

## 评审入口

- [题号、GitHub 与 commit 获取方式](docs/submission.md)
- [需求与成功标准](docs/01-requirements.md)
- [设计与动态决策](docs/02-solution.md)
- [实测、证据与限制](docs/03-evaluation.md)
- [AI 使用与个人贡献](docs/04-ai-usage.md)
- [复盘与后续工作](docs/05-retrospective.md)
- [示例输入](examples/inputs/) 与 [实际运行记录](examples/runs/)

## 环境与运行

需要 Python 3.12，不依赖第三方运行时包。以下命令均在仓库根目录执行。

```bash
PYTHONPATH=src python -m material_agent \
  --mode mock \
  --input examples/inputs/normal.json \
  --catalog data/catalog.json \
  --db .state/drafts.sqlite \
  --output .state/result.json
```

Mock 不需要环境变量。输出包含每项素材的审核状态、风险原因、建议时段、待确认项和真实草稿保存回执，并带有工具调用 Trace。`.state/` 是本地运行状态目录，不提交真实业务数据。

已实测 35 项测试通过；11 条离线 CLI 记录符合预期。运行全部离线测试与生成验证材料：

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
python scripts/run_examples.py
```

脚本会用隔离数据库执行 11 条 CLI 记录，生成 `examples/runs/` 下的实际输出与 Trace；重复提交两次共享同一数据库。命令及退出码记录在 [manifest.json](examples/runs/manifest.json)。详细预期、实际结果和限制见 [评估文档](docs/03-evaluation.md)。

真实模型模式使用 OpenAI-compatible Chat Completions 工具调用接口。请在本地安全配置环境变量，不把密钥写入输入文件、Trace 或 Git：

```bash
export OPENAI_MODEL='你的工具调用模型名'
# 在安全环境中设置 OPENAI_API_KEY。
# 可选 OPENAI_BASE_URL，默认 https://api.openai.com/v1。
PYTHONPATH=src python -m material_agent \
  --mode live \
  --input examples/inputs/conflict.json \
  --catalog data/catalog.json \
  --db .state/live.sqlite \
  --output .state/live-conflict.json
```

真实模型必须支持 function calling；接口兼容不代表已验证该模型的行为。配置好凭据后，可运行 `python scripts/run_live_examples.py`，用独立数据库执行正常和冲突两条路径，结果写入 `examples/live/`。需人工核对实际动作、工具观察和确认状态后再更新评估结论。真实模式缺少配置会明确失败，不会静默回退为 Mock。

CLI 退出码：`0` 表示完成或已保存待确认草稿，`1` 表示工具/模型失败，`2` 表示缺资料、风险阻断或未完成，`64` 表示无效输入。`pending_confirmation` 返回 `0` 仅代表草稿已保存，仍需用户确认。`--max-steps` 默认 `20`，计入最后的结束动作；可用 `--fault save_draft=1` 注入一次保存失败，或 `--trace-output 路径` 单独保存 Trace。

## 关键行为

- 模型通过 `read_material`、`check_rules`、`query_slots`、`save_draft` 选择动作，观察结果后再决定下一步或结束。
- 硬性要求发生冲突时，保留 `required_at`；替代时段仅为建议。没有首次请求中的明确确认，草稿保持 `pending_confirmation`。
- 保存工具重新审核和复检冲突。草稿以 `(request_id, asset_id)` 防重复；同一键改变业务参数会报幂等冲突。
- 保存失败明确返回失败，不生成成功草稿 ID；模型的结束声明不能覆盖工具事实。
- 设置调用上限；到达上限时明确返回未完成。所有日期均使用带时区偏移的 ISO 8601 时间。

`confirmed_alternative_at` 只适用于首次请求已获得确认的场景。当前 CLI 不支持对已保存草稿追加确认、取消或修改：旧请求 ID 加确认会产生幂等冲突，新 ID 也会受原草稿暂占影响。

这份提交以完成核心链路为范围，不包含界面、线上部署或生产发布能力。
