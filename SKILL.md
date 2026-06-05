---
name: job-matcher
description: 根据用户简历(CV)和求职意向，抽取CV结构化字段、实时检索匹配职位、生成可交互HTML报告。当用户提供简历文件(pdf/docx/txt/md)或粘贴简历文本，并希望找工作、匹配职位、获取职位推荐、做求职匹配时使用。
---

# Job Matcher

把用户的简历(CV) + 求职意向(query) 变成一份匹配职位的可交互 HTML 报告。
数据源是实时 **WebSearch**；产出 `data/reports/report_*.html`。

## 核心原则

- **你是编排者**：调脚本、融合 query、追问用户、spawn subagent。
- **重上下文工作交给 subagent**：CV 抽取、搜索+解析、打分。大块原始文本（CV 全文、搜索结果、JD 全文）**留在 subagent / 文件**，你的上下文只搬运「文件路径 + 小 JSON」。
- 路径均相对本 skill 目录；脚本用 Python 3 运行，自己定位 `data/`。
- 脚本输出是 JSON（纯 ASCII），解析后使用。

## 工作流

### 0. 准备
- 读 `config.json` 拿参数（top_n / 并发 / 阈值 / TTL 等）。
- **灵活识别输入**：从用户消息找出 CV（文件路径，或粘贴的大段简历文本）和 query（求职意向）。
  - 只有 query 没有 CV → 追问 CV。
  - 有 CV 没有 query → 可继续（纯 CV 模式），但目标职位/地点缺失时按下面规则追问。

### 1. 解析 CV（脚本）
- CV 是**文件**：`python scripts/extract_cv.py <path>`。
- CV 是**粘贴文本**：先存成 `data/cv_text.txt`（UTF-8），再 `python scripts/extract_cv.py data/cv_text.txt`。
- 读返回 JSON：`ok:false` → 把 error 告诉用户并请其换格式；有 `warnings` → 先告知质量风险再继续。记下 `cv_hash`、`text_path`、`cache_hit`。

### 2. CV 结构化
- `cache_hit: true` → 直接读 `cached_profile_path` 载入 CVProfile，**跳过抽取**。
- 否则 **spawn 一个 general-purpose subagent** 抽取：
  - 让它读 `references/cv_schema.md` 和 `text_path`，按规则产出 CVProfile JSON；
  - 自行 `python scripts/validate_profile.py`（stdin 喂抽取的 JSON）校验补全；
  - 把结果写 `data/cv/<cv_hash>.json`，**只回传简短摘要**（preferred_roles / seniority / missing），不回贴全文或完整 JSON。
  - 若它判定输入不是简历（返回 error）→ 提示用户。
- 需要完整字段时按需读 `data/cv/<cv_hash>.json`。

### 3. 构建检索条件（你来做，读 `references/search_playbook.md`）
- 融合 CVProfile + query → `search_plan`(≤5 条) + `candidate_profile`。
- 约束分流、融合优先级、同义扩展、按 CV 语言分市场——见 search_playbook。
- **缺目标职位 或 地点完全缺失 → 停下追问用户**。
- 算 `candidate_profile_hash`（candidate_profile 的稳定 hash，进 match_score 缓存键）。

### 4. 检索职位（并行搜索 subagent + 脚本，自适应分批）
- 按 search_playbook 的自适应分批：每批**单条消息并行 spawn** 搜索 subagent（≤ `max_parallel_subagents`，每个恰好 1 次 WebSearch），让它们按 search_playbook「搜索 subagent 职责」执行，回传结构化职位数组。
- 汇总本批回传 → `python scripts/merge_jobs.py merge --cv-hash <h> --cp-hash <h>`（stdin 喂候选数组）→ 得 `{to_analyze, to_score_only, cached, stats}`。
- 按 stats 的净有效数判断是否追加下一批（阈值/上限/连续空批见 playbook）。
- 一行进度反馈：`第N批 搜X条→候选Y→新Z/缓存W`。

### 5. 匹配排序（并行打分 subagent + 脚本，读 `references/scoring_rubric.md`）
- **粗排**：对 `to_analyze` + `to_score_only` 用 snippet 5 维快速估分，并行打分 subagent，排序。
- **精排**：取 Top-(top_n + precise_buffer) → 并行 subagent 抓 JD 全文 → 抽 jd_profile + 精确 5 维打分；`to_score_only` 复用已有 jd_profile 只打分。
- **失效验证**（精排的 Top-N）：`python scripts/verify_jobs.py`（stdin 喂 url 数组）查死链；对 `possibly_closed` 的走容错阶梯确认；失效则剔除、从次位递补。
- 写回：`python scripts/merge_jobs.py update --cv-hash <h> --cp-hash <h>`（stdin 喂 `[{dedup_key, jd_profile, match_score, verified, scored_from}]`）。
- `cached` 的直接复用，不重打分。

### 6. 生成报告（脚本）
- 写 `data/run_meta.json`：`{profile_summary, new_count, cached_count, lang}`（lang = CVProfile.search_language）。
- `python scripts/render_html.py --cv-hash <h> --cp-hash <h> --meta-file data/run_meta.json` → 生成并自动打开报告。
- 把 `report_path` 告诉用户。

### 7. 收尾
- 简述结果（新增 X / 复用 Y / 报告路径）、指出风险（未验证/基于摘要评分的职位）。

## 容错阶梯（失效验证 & JD 抓取共用）
```
WebFetch 抓正文 → 失败退避重试1次
  → python scripts/...（requests 静态抓，可在 subagent 内用 requests + 常规 UA）扫关闭关键词
  → python scripts/fetch_rendered.py <url>（headless，受 headless_budget 约束，缺浏览器自动跳过）
  → 全失败：标注「未验证」/「基于摘要评分」，不阻塞主流程
```

## 护栏
- 抓取**不绕验证码、不模拟登录、不抓需付费/登录内容、尊重 robots/ToS**；headless 默认开但受 budget 限制。
- 失败一律**降级不阻塞**；搜 0 结果/全失效时如实告知并建议放宽条件。
- 大块文本（CV 全文、搜索原始结果、JD 全文）**留在 subagent / 文件**，主上下文只放路径与小 JSON。
- 不臆造职位或字段；CV 含 PII，数据落 `data/`（已 .gitignore）。

## 配置与脚本一览
- `config.json`：top_n, precise_buffer, max_parallel_subagents, max_websearch_calls, stop_threshold, consecutive_empty_stop, jd_ttl_days, seniority_mode, enable_headless_fallback, headless_budget。
- 脚本：`extract_cv.py` `validate_profile.py` `merge_jobs.py`(merge/update) `verify_jobs.py` `fetch_rendered.py` `render_html.py`。
- 指令：`references/cv_schema.md` `references/scoring_rubric.md` `references/search_playbook.md`。
- 设计细节见 `DESIGN.md`。
