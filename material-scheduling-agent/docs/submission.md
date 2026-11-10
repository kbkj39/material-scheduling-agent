# 提交信息

- 题号：**1 — 素材审核与发布排期 Agent**。
- GitHub URL：[kbkj39/material-scheduling-agent](https://github.com/kbkj39/material-scheduling-agent)。
- 提交分支：`main`。仓库当前为公开，可直接提供给评审访问。
- 最终 commit SHA：在最终提交的本地检出目录运行 `git rev-parse HEAD`，以命令输出及最终交付消息中的完整 SHA 为准。文件不自引用自身所在 commit 的 SHA。

评审可用以下命令获取代码与实际提交 SHA：

```bash
git clone https://github.com/kbkj39/material-scheduling-agent.git
cd material-scheduling-agent
git rev-parse HEAD
PYTHONPATH=src python -m unittest discover -s tests -v
```

若后续将仓库改为私有，必须向指定评审账号授予可读权限并核验访问。

## 提交前尚须补充

1. 配置真实模型，运行并核验正常、冲突两条实际模型路径；当前仓库里的 Mock Trace 不能代替该项。
2. 由候选人补充实际个人修改、独立验证、学习收获与本人投入时间。现有 [AI 使用说明](04-ai-usage.md) 如实保留了当前可核实范围。
提交考核时，将题号、上述仓库 URL 和最终完整 commit SHA 一并填写。

工程验证：35 项测试通过，11 条离线 CLI 记录符合预期。全部材料的证据边界见 [评估文档](03-evaluation.md)。
