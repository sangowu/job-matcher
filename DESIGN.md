# job-matcher 设计文档

> 本文件固化全部设计决策（经 16 轮逐项确认封板）。是实现与维护的权威参考。
> 配套实现计划见 `~/.claude/plans/elegant-spinning-river.md`。

## 1. 定位

输入简历(CV) + 求职意向(query) → 抽取 CV 结构化字段 → 实时检索匹配职位 → 生成可交互 HTML 报告。

是 `D:\Python_Projects\JobRadar` 的**轻量版**：纯 Claude Code 原生能力（WebSearch + subagent），**零代码依赖** JobRadar，仅借鉴其 schema、算法、HTML 视觉。

| 维度 | JobRadar | 本 skill |
|------|----------|----------|
| 数据源 | JobSpy 抓取 Indeed/LinkedIn | **WebSearch** 实时搜索 |
| 形态 | 服务器 SPA + SSE | **静态单文件 HTML** |
| 缓存 | SQLite | **JSON** |
| 运行 | 独立服务 | Claude Code 内触发 |

## 2. 执行模型

- **主 agent = 编排者**：调脚本、融合 query、追问用户、spawn subagent。
- **subagent 承担重上下文工作**（CV 抽取、搜索+解析、打分）：大块原始文本（CV 全文、搜索结果、JD 全文）留在 subagent / 文件，**主上下文只搬运「路径 + 小 JSON」**。
- **脚本承担确定性工作**：解析、校验、去重/聚合/缓存、失效验证、渲染。

| 步骤 | 执行者 |
|------|--------|
| 解析/校验/合并/验证/渲染 | 脚本 |
| CV抽取 / 搜索 / 打分 | subagent（隔离+并行） |
| 融合query / 编排 / 追问 | 主 agent |

## 3. 数据流

```
CV+query →[脚本]extract_cv→ cv_text + cv_hash
        →[缓存检查]→ 命中跳过抽取
        →[subagent]抽取 CVProfile →[脚本]validate_profile
        →[主agent]融合query→ search_plan + candidate_profile
        →[并行subagent]WebSearch+解析+初筛 →[脚本]merge_jobs(去重/聚合/缓存)
        →[并行subagent]粗排(snippet)→精排Top-(N+5)抓JD→5维打分 + 失效验证
        →[脚本]render_html→ report_{ts}.html（自动打开）
```

## 4. 模块设计

### 模块1 输入解析（extract_cv.py）
- 支持 pdf(pdfplumber)/docx(python-docx，遍历 paragraphs+tables)/txt/md；**不 OCR、不 .doc**。
- 编码兜底链：`utf-8-sig → utf-8 → gbk → latin1`。
- **文本规范化后算 cv_hash**（压空白/统一\n/strip 再 sha256）；**保留换行/bullet 结构**。
- 上限：>80 页或 >200k 字符截断+warning；扫描件(<50字符)/DOCX文本框 warning。
- 全文落盘 `data/cv_text.txt`，输出 `{ok, source_type, char_count, text_path, warnings}`。
- **不提取超链接**（仅给 HR 看，JD 匹配不需要）。
- 输入识别：灵活识别 CV（路径或大段文本）+ query；非简历交模块2检测。

### 模块2 CV 结构化（subagent + validate_profile.py）
- LLM 抽取 `CVProfile`（读 `references/cv_schema.md`），脚本校验+补全。
- **CVProfile 字段**：summary / preferred_roles / skills / years_of_experience / seniority / eligible·stretch·blocked_levels / preferred_locations / open_to_remote / languages / industries / education_level / current_title / search_language / missing / schema_version。
- **seniority 六档**（对齐 JobRadar）：`intern / new_grad / junior / mid / senior / lead`。LLM 只出单值，脚本映射三档列表。
- 关键规则：**相关年限定级**（实习半计）、**经历证据 > 头衔**、**现居地当期望地+remote**、技能归一化、不臆造。
- **search_language = CV 语言**（不从地点推断）。
- cv_hash 缓存复用（命中跳过抽取）。

### 模块3 检索条件（主 agent）
- 产出 `search_plan`（≤5 条）+ `candidate_profile`{hard_filters, deal_breakers, preferences}。
- **约束分流**：title/行业→搜索词；薪资/雇佣类型/deal-breaker→candidate_profile（打分阶段筛，不进搜索词）。
- 融合优先级：query硬约束 > title_override > CV；地点固定 = CV.preferred_locations + remote。
- 缺 preferred_roles 或 完全无地点 → **停下追问用户**。
- LLM 适度同义扩展（2-3 变体）。负面约束只进 deal_breakers。
- **must_have/nice_to_have 是 JD 侧字段**（模块5 抽），不在此处。

### 模块4 职位检索（并行搜索 subagent + merge_jobs.py）
- 按 search_plan **自适应分批**：批内并行 ≤3 subagent，批间串行；每 subagent 恰好 1 次 WebSearch。
- 停止：净有效 ≥ stop_threshold(12) 或 WebSearch ≥6 或连续2批空。
- 搜索 subagent：WebSearch → 解析职位 → **三维初筛**（title 语义 / location 直接比对城市 / seniority ∈ eligible）→ 噪音过滤。**信息缺失从宽放过**。
- **列表页/聚合页**：subagent 判断（单职位解析，列表取首条）。
- merge_jobs：url_key 缓存命中 + dedup_key 聚合（见横切）。

### 模块5 匹配排序（并行打分 subagent）
- **两阶段**：粗排（snippet 5维，所有候选）→ 排序 → 精排 **Top-(N+5)=20** 抓 JD 全文 → 综合排序展示 N=15。
- **5 维打分**：title / seniority / skills / location / **must_have 满足度**（CV 满足 JD must_have 的比例）。
- recommendation 五档：strong_apply / apply / stretch_apply / low_priority / skip。
- JD 抽取 must_have_requirements / good_to_have / required_skills。
- candidate hard_filters 不满足 或 命中 deal_breakers → skip。
- JD 抓取失败 → 复用容错阶梯；彻底失败 → snippet 粗分 + 标注「基于摘要评分」。
- 多来源按权威度排序（greenhouse/lever > 官网 > linkedin > 聚合）取**首个抓取成功**。
- 主 agent 统一写表（防并发写）。

### 模块6 HTML 渲染（render_html.py + template.html）
- Jinja2 注入 → `data/reports/report_{ts}.html` → 自动打开（跨平台）。
- **借鉴 JobRadar 视觉**：Tailwind CDN、深色+localStorage、scoreBadge、recommendation 五档色标。
- 布局：**单页卡片 + 点击展开**。卡片含评分/色标/技能chips/来源chips/优势·待加强/「⚠未验证」「基于摘要评分」标注/投递链接。
- 交互（纯 JS）：score/date 排序、date(全部/今天/本周)+score(≥3/5/7/9) 筛选、搜索框。
- **全表展示** + 🆕新增徽章 + 顶部「新增X·复用缓存Y」；历史区默认 Top-20 + 展开。
- **报告语言 = CV 语言**（中/英 i18n，其他 fallback 英文）。

## 5. 横切设计

### 缓存与聚合（三层）
| 缓存 | key | 跨CV复用 | 失效 |
|------|-----|:--:|------|
| CV Profile | `cv_hash`（规范化文本 sha256） | — | 文件变 |
| JD Profile | `dedup_key`（company\|title 归一） | ✅ | TTL 30天 |
| Match Score | `dedup_key + cv_hash + candidate_profile_hash` | ❌ | 随JD |

- **url_key**（规范化 URL：去追踪参数+锚点，保留 job-id 参数，主流平台提 `平台:id`）= **缓存命中**键。
- **dedup_key** = **多来源聚合**键（raw_sources 按 source 去重合并）。
- merge_jobs 输出 `{to_analyze, to_score_only, cached}`；`to_score_only` 含「同CV换query」（candidate_profile_hash 不同）。
- 过期(>30天)移入 `archive.json`，主表保精简。

### 容错阶梯（失效验证 & JD 抓取共用）
```
WebFetch 抓正文 → 失败退避重试1次
  → requests+UA 取静态HTML 扫 _CLOSED_PATTERN
  → fetch_rendered.py（playwright，detect_browser 复用系统浏览器，
                       受 headless_budget=3 约束，缺浏览器跳过）
  → 全失败：标注「⚠未验证」/「基于摘要评分」，不阻塞
```
护栏：不绕验证码 / 不登录 / 尊重 robots / 失败降级不阻塞主流程。

### 隐私
`data/` 整体 .gitignore（含 CV 文本、画像、职位表、报告等 PII）。

## 6. 配置（config.json）
| 键 | 默认 | 说明 |
|----|------|------|
| top_n | 15 | 最终展示数 |
| precise_buffer | 5 | 精排多抓缓冲（抓 Top-20 选 15） |
| max_parallel_subagents | 3 | 批内并行上限 |
| max_websearch_calls | 6 | WebSearch 总次数硬上限 |
| stop_threshold | 12 | 净有效职位达标停止 |
| consecutive_empty_stop | 2 | 连续空批停止 |
| jd_ttl_days | 30 | JD 缓存有效期 |
| seniority_mode | balanced | strict/balanced/stretch |
| enable_headless_fallback | true | headless 兜底开关 |
| headless_budget | 3 | 每次运行 headless 上限 |
| report_keep_history | true | 保留历史报告 |

## 7. 借鉴 JobRadar 的具体资产
- `schemas.py`：normalize_company / normalize_title / make_dedup_key / _CLOSED_PATTERN（11类关闭关键词）。
- `seniority.py`：六档 + default_eligible/stretch/blocked_levels 映射 + normalize_seniority 关键词匹配。
- `cache.py`：_merge_job 的 raw_sources 合并逻辑。
- `templates/index.html`：scoreColor/scoreBadge/recommendationBadge/sortedJobs/jobMatchesFilters/深色/i18n。

## 8. 依赖
- 必需：`pdfplumber` `python-docx` `jinja2` `requests`（环境已装，Python 3.13.5）。
- 可选：`playwright`（headless 兜底，复用系统浏览器；已装）。
