# 检索手册（search_playbook）

> 模块3（主 agent 构建条件）与模块4（搜索 subagent 执行）读本文件。

## 一、检索条件构建（模块3，主 agent）

### 输入
`CVProfile`（`data/cv/<hash>.json` 或抽取结果）+ 用户 query。

### query 拆解与约束分流
| query 信息 | 去向 |
|-----------|------|
| 目标职位改写 | → search_plan 的 role（覆盖 CV `preferred_roles`） |
| 行业/公司类型（出海、外企） | → 搜索词 + candidate_profile.preferences |
| 薪资下限、雇佣类型 | → **仅** candidate_profile.hard_filters（不进搜索词） |
| 负面要求（不要外包/996/实习） | → **仅** candidate_profile.deal_breakers |

### 融合优先级
```
roles     : query 改写 > CV.preferred_roles（都缺 → 追问用户）
locations : CV.preferred_locations + (remote if open_to_remote)  ← query 不改地点
其余约束  : query > CV
```
**地点完全缺失 → 停下追问用户**，不臆测。

### search_plan 生成（≤5 条，有序）
- `roles`：取 top-2，并对每个做 **LLM 适度同义扩展**（2-3 个变体，含目标语言写法）。
- `locations`：CV 地点 + remote，取 top-2。
- 组合：主role×主地点(P1) > 主role×次地点(P2) > 次role×主地点(P3)… ≤4 条 + 1 条站点定向 = **≤5**。
- `query_string`：加 `jobs / hiring / careers` 等词，提升招聘页命中、利于解析出 company+title。
- `language` = `CVProfile.search_language`（**CV 语言**）。

### 按 CV 语言分市场
| search_language | 站点策略 |
|-----------------|---------|
| `en` | 可加 `site:` 定向：greenhouse.io / lever.co / linkedin.com/jobs / ashbyhq.com |
| `zh` | 不用 site 限定（或本地站如 zhipin/lagou/liepin）；用中文职位词 |
| 其他 | 不限定站点，纯关键词 |

### candidate_profile 输出
```json
{ "hard_filters": {"salary_min": 30000, "work_mode": "remote", "job_type": "fulltime"},
  "deal_breakers": ["纯外包", "996", "实习"],
  "preferences": ["出海公司", "Go 技术栈"] }
```
（这是给打分阶段用的候选人侧约束。`candidate_profile_hash` 进 match_score 缓存键。）

## 二、检索执行（模块4，搜索 subagent + 自适应分批）

### 自适应分批（主 agent 编排）
```
第1批：单条消息并行 spawn plan 前 2 条 query 的搜索 subagent（每个恰好 1 次 WebSearch）
  → 汇总回传 → merge_jobs.py(聚合/缓存判定) → 统计净有效新职位
  ├─ ≥ stop_threshold(12) → 停
  └─ < 阈值 → 追加下一批（plan 剩余 query）
停止条件（任一）：净有效≥12 / WebSearch 累计≥6 / 连续 2 批 0 结果
批内并行 ≤ max_parallel_subagents(3)
```

### 搜索 subagent 职责
```
1. 执行 1 次 WebSearch（用 query_string）
2. 从结果摘要解析职位：title / company / location / url / snippet / date_posted / source
   · 聚合/列表页：subagent 判断——能解析单职位则取，否则取最相关首条；无法解析则跳过
   · 缺 company → 用域名兜底
3. 三维初筛（不过线丢弃）：
   · title：与 preferred_roles(含同义) 语义匹配
   · location：JD 城市 ∈ preferred_locations 或 remote（直接比对，不做地域层级）
   · seniority：∈ eligible/stretch；命中 blocked 丢
   · 【信息缺失从宽】snippet 没写 seniority/location → 放过，留精排
4. 噪音过滤：命中 deal_breakers、培训/招生/代写简历/职位聚合导航页/内容农场 → 丢
5. 回传通过初筛的结构化职位数组（JSON）+ 一行统计；原始网页结果留在 subagent 内
```

### 回传格式（每职位）
```json
{ "title": "", "company": "", "location": "", "url": "",
  "snippet": "", "date_posted": "", "source": "greenhouse|linkedin|lever|web", "salary": "" }
```
主 agent 汇总后喂给 `merge_jobs.py merge`。
