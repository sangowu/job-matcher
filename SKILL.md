---
name: job-matcher
description: 根据用户简历(CV)和求职意向，抽取CV结构化字段、实时检索匹配职位、生成可交互HTML报告。当用户提供简历文件(pdf/docx/txt/md)或粘贴简历文本，并希望找工作、匹配职位、获取职位推荐、做求职匹配时使用。
---

# Job Matcher

把简历(CV) + 求职意向 变成一份匹配职位的可交互 HTML 报告。

> **完整流程是 agent-中立的，写在 [`WORKFLOW.md`](WORKFLOW.md)（单一事实源）。**
> 本文件只是 **Claude Code 适配入口**——把流程里的「能力」映射到 Claude 工具，并补充 Claude 特定执行要点。先读 WORKFLOW.md 再按下面映射执行。

## 能力映射（Claude Code）

| WORKFLOW 里的能力 | Claude Code 对应 |
|------|------|
| web 搜索 | `WebSearch` 工具 |
| 子代理（并行 + 上下文隔离） | `Task` / `Agent` 工具——**在单条消息里发多个调用以并行 spawn**，用 general-purpose 类型 |
| 网页抓取 | `WebFetch` 工具（失败回退 `scripts/fetch_rendered.py`） |
| 运行脚本 / 读写文件 | `Bash` / 文件工具 |

## Claude 特定执行要点

按 `WORKFLOW.md` 的 0–7 步执行，其中：

- **第 2 步 CV 抽取、第 4 步搜索、第 5 步打分**走 subagent；**单条消息内并行** spawn 多个，受 `config.json` 的 `max_parallel_subagents` 约束。
- 委派 subagent 时只让它回传「**摘要 + 文件路径**」，CV 全文 / 搜索原始结果 / JD 全文 **留在 subagent**，保持主上下文整洁。
- 第 4 步每个搜索 subagent **恰好 1 次 WebSearch**，计入 `max_websearch_calls`。

## 其余

脚本契约、容错阶梯、护栏、配置、降级策略 —— 全部见 [`WORKFLOW.md`](WORKFLOW.md)。
完整设计决策见 [`DESIGN.md`](DESIGN.md)。指令文档在 `references/`。
