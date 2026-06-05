# job-matcher

[English](README.en.md) | **中文**

> 一个 **agent skill（Claude Code 与 Codex 通用）**：输入**简历(CV) + 求职意向**，自动抽取简历字段、用 **web 搜索实时检索**匹配职位，生成一份**可交互的 HTML 报告**。

是 [JobRadar](https://github.com/sangowu/JobRadar) 的**轻量版**——纯 agent 原生能力（web 搜索 + 子代理 + Python 脚本），**零外部服务依赖**，借鉴 JobRadar 的 schema、算法与界面风格。

---

## ✨ 功能

- 📄 **简历解析**：支持 PDF / DOCX / TXT / MD，或直接粘贴文本（不做 OCR）。
- 🧠 **结构化抽取**：抽取目标职位、技能、资历(seniority)、地点、语言等，自动按相关年限定级。
- 🔎 **实时职位检索**：基于 WebSearch 自适应分批搜索，按 CV 语言切换市场（中/英）。
- 🎯 **5 维匹配打分**：title / seniority / skills / location / must-have，输出五档投递建议（强烈投递→跳过）。
- 🗂️ **增量缓存**：CV、JD、匹配分三层缓存；多来源同职位自动聚合；换 query 自动失效重算。
- 📊 **可交互报告**：两栏布局（左职位列表 30% + 右详情 70%）+ 评分徽章 + 深色模式 + 排序/筛选/搜索 + 中英 i18n，自包含单文件 HTML。

## 🏗️ 架构

- **主 agent = 编排者**：调脚本、融合 query、追问用户、spawn subagent。
- **subagent 承担重上下文工作**（CV 抽取 / 搜索 / 打分）：大块原始文本留在 subagent，主上下文只搬「路径 + 小 JSON」，保持整洁。
- **Python 脚本承担确定性工作**：解析、校验、去重聚合缓存、失效验证、渲染。

```
CV + query
   │ [脚本] extract_cv          → 纯文本 + cv_hash
   │ [缓存检查]                 → 命中则跳过抽取
   │ [subagent] 抽取 CVProfile  → [脚本] validate_profile
   │ [主agent] 融合 query       → search_plan + candidate_profile
   │ [并行 subagent] WebSearch+解析+初筛 → [脚本] merge_jobs(去重/聚合/缓存)
   │ [并行 subagent] 粗排→精排抓JD+5维打分 + 失效验证(容错阶梯)
   │ [脚本] render_html         → report_*.html（自动打开）
   ▼
可交互 HTML 报告
```

**容错阶梯**（失效验证 & JD 抓取共用）：`WebFetch → requests 静态抓 → playwright headless（复用系统默认浏览器，不另下载）→ 标注未验证不阻塞`。

## 📁 结构

```
job-matcher/
├── SKILL.md              # 触发描述 + 编排入口
├── WORKFLOW.md           # agent-中立完整流程
├── config.json           # 配置旋钮
├── references/           # subagent 按需读取的指令
│   ├── cv_schema.md          # CV 抽取规则
│   ├── scoring_rubric.md     # 5 维打分 + 五档阈值
│   └── search_playbook.md    # fan-out / 分市场 / 自适应分批
├── scripts/              # 确定性 Python 脚本
│   ├── extract_cv.py         # 解析 CV → 文本 + hash
│   ├── validate_profile.py   # 校验 + seniority→levels 映射
│   ├── merge_jobs.py         # 去重 + 聚合 + 缓存判定（merge/update）
│   ├── verify_jobs.py        # 失效职位状态码检测
│   ├── fetch_rendered.py     # headless 渲染兜底（复用系统浏览器）
│   ├── render_html.py        # 渲染 HTML 报告
│   └── _jobutil.py           # 共享：归一化/去重键/URL 规范化
├── assets/template.html  # 静态报告模板（Tailwind + 纯 JS）
└── data/                 # 运行时数据（.gitignore，含 PII）
```

## 🚀 使用

把本仓库放在 `~/.claude/skills/job-matcher/`（个人 skill 目录），Claude Code 会自动识别。然后在对话里：

> 这是我的简历 `D:\cv.pdf`，帮我找远程后端职位

或直接粘贴简历文本 + 求职意向。skill 会走完整流程并在浏览器打开报告。

## ⚙️ 配置

`config.json` 集中所有旋钮：

| 键 | 默认 | 说明 |
|----|------|------|
| `top_n` | 15 | 最终展示职位数 |
| `precise_buffer` | 5 | 精排多抓缓冲 |
| `max_parallel_subagents` | 3 | 批内并行上限 |
| `max_websearch_calls` | 6 | WebSearch 总次数上限 |
| `stop_threshold` | 12 | 净有效职位达标停止 |
| `jd_ttl_days` | 30 | JD 缓存有效期 |
| `seniority_mode` | balanced | strict / balanced / stretch |
| `enable_headless_fallback` | true | headless 兜底开关 |
| `headless_budget` | 3 | 每次运行 headless 上限 |

## 🔧 依赖

- Python 3.10+
- 必需：`pdfplumber` `python-docx` `requests`
- 可选：`playwright`（headless 兜底，复用系统已装的 Chromium 系浏览器，无需 `playwright install`）

```bash
pip install pdfplumber python-docx requests
pip install playwright   # 可选
```

---

*Built with [Claude Code](https://claude.com/claude-code).*
