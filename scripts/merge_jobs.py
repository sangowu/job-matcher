#!/usr/bin/env python3
"""横切核心：去重 + 多来源聚合 + 缓存判定 + 增量表管理。

两个模式：

  merge  —— 输入本批候选职位，做：本批内聚合 → 与 jobs_table 比对
            （url_key 强命中 / dedup_key 弱命中聚合）→ TTL 判定 → 写表骨架
            → 输出 {to_analyze, to_score_only, cached, stats}
  update —— 输入打分结果，写回 jd_profile / match_score / verified

缓存键：
  jd_profile  按 dedup_key（跨 CV 复用，TTL jd_ttl_days）
  match_score 按 dedup_key + cv_hash + candidate_profile_hash

用法:
  python merge_jobs.py merge  --cv-hash H --cp-hash H   < candidates.json
  python merge_jobs.py update --cv-hash H --cp-hash H   < results.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from _jobutil import (
    all_url_keys,
    canonicalize_url,
    is_closed_posting,
    load_config,
    make_dedup_key,
)

SKILL_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_ROOT / "data"
TABLE_PATH = DATA_DIR / "jobs_table.json"
ARCHIVE_PATH = DATA_DIR / "archive.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"jobs": []}


def _save(path: Path, data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = _now().isoformat()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_expired(fetched_at: str | None, ttl_days: int) -> bool:
    if not fetched_at:
        return True
    try:
        ts = datetime.fromisoformat(fetched_at)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except Exception:
        return True
    return (_now() - ts) > timedelta(days=ttl_days)


def _brief(job: dict, *, with_jd: bool = False, with_score: bool = False, mk: str = "") -> dict:
    """给下游 subagent 的精简视图。"""
    b = {
        "dedup_key": job["dedup_key"],
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "url": job.get("url", ""),
        "snippet": job.get("snippet", ""),
        "raw_sources": job.get("raw_sources", []),
        "possibly_closed": job.get("possibly_closed", False),
        "status": job.get("status", "existing"),
    }
    if with_jd and job.get("jd_profile"):
        b["jd_profile"] = job["jd_profile"]
    if with_score and mk:
        b["match_score"] = (job.get("match_scores") or {}).get(mk)
    return b


def _aggregate_batch(candidates: list) -> dict:
    """本批内按 dedup_key 聚合，合并 raw_sources（按 source 去重）。"""
    batch: dict[str, dict] = {}
    for c in candidates:
        if not isinstance(c, dict):
            continue
        dk = make_dedup_key(c.get("company", ""), c.get("title", ""))
        if dk.strip("|") == "":  # 公司和 title 都空 → 无效
            continue
        src = {
            "source": c.get("source", "web"),
            "url": c.get("url", ""),
            "date_posted": c.get("date_posted", ""),
        }
        if dk in batch:
            agg = batch[dk]
            srcs = {rs["source"] for rs in agg["raw_sources"]}
            if src["source"] not in srcs:
                agg["raw_sources"].append(src)
            for f in ("location", "snippet", "salary", "date_posted", "url"):
                if not agg.get(f) and c.get(f):
                    agg[f] = c[f]
        else:
            batch[dk] = {
                "dedup_key": dk,
                "title": c.get("title", ""),
                "company": c.get("company", ""),
                "location": c.get("location", ""),
                "url": c.get("url", ""),
                "snippet": c.get("snippet", ""),
                "salary": c.get("salary", ""),
                "date_posted": c.get("date_posted", ""),
                "raw_sources": [src],
            }
    return batch


def _merge_into(hit: dict, cand: dict) -> None:
    """把本批候选的来源聚合进已存在职位。"""
    srcs = {rs["source"] for rs in hit.get("raw_sources", [])}
    for rs in cand.get("raw_sources", []):
        if rs["source"] not in srcs:
            hit.setdefault("raw_sources", []).append(rs)
            srcs.add(rs["source"])
    # 合并 url_keys
    existing = set(hit.get("url_keys", []))
    for uk in all_url_keys(cand):
        if uk not in existing:
            hit.setdefault("url_keys", []).append(uk)
            existing.add(uk)
    # 补字段
    for f in ("location", "snippet", "salary"):
        if not hit.get(f) and cand.get(f):
            hit[f] = cand[f]


def _archive_stale(table: dict, ttl_days: int) -> int:
    """把本次未搜到、且 last_seen 超 TTL 的旧职位移入 archive，主表保精简。"""
    cutoff = (_now() - timedelta(days=ttl_days)).date()
    keep, stale = [], []
    for j in table["jobs"]:
        try:
            last = date.fromisoformat(j.get("last_seen", ""))
        except Exception:
            last = None
        if j.get("status") == "existing" and last and last < cutoff:
            stale.append(j)
        else:
            keep.append(j)
    if stale:
        arch = _load(ARCHIVE_PATH)
        arch["jobs"].extend(stale)
        _save(ARCHIVE_PATH, arch)
        table["jobs"] = keep
    return len(stale)


def cmd_merge(cv_hash: str, cp_hash: str) -> None:
    cfg = load_config()
    ttl_days = int(cfg.get("jd_ttl_days", 30))
    mk = f"{cv_hash}:{cp_hash}"

    candidates = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "[]")
    if not isinstance(candidates, list):
        print(json.dumps({"ok": False, "error": "输入必须是职位候选数组"}))
        sys.exit(1)

    table = _load(TABLE_PATH)
    by_urlkey: dict[str, dict] = {}
    by_dedup: dict[str, dict] = {}
    for job in table["jobs"]:
        job["status"] = "existing"  # 重置本次状态
        for uk in job.get("url_keys", []):
            by_urlkey[uk] = job
        by_dedup[job["dedup_key"]] = job

    batch = _aggregate_batch(candidates)
    today = date.today().isoformat()
    to_analyze, to_score_only, cached = [], [], []

    for dk, cand in batch.items():
        cand_keys = all_url_keys(cand)
        hit = None
        for uk in cand_keys:                      # url_key 强命中
            if uk in by_urlkey:
                hit = by_urlkey[uk]
                break
        if hit is None and dk in by_dedup:          # dedup_key 弱命中
            hit = by_dedup[dk]

        if hit is not None:
            _merge_into(hit, cand)
            hit["last_seen"] = today
            hit["seen_count"] = hit.get("seen_count", 0) + 1
            hit["status"] = "existing"
            jd = hit.get("jd_profile")
            expired = _is_expired(hit.get("fetched_at"), ttl_days)
            if jd and not expired:
                if mk in (hit.get("match_scores") or {}):
                    cached.append(_brief(hit, with_score=True, mk=mk))
                else:
                    to_score_only.append(_brief(hit, with_jd=True))
            else:
                if expired:
                    hit["jd_profile"] = None
                to_analyze.append(_brief(hit))
        else:
            newjob = {
                "dedup_key": dk,
                "title": cand["title"], "company": cand["company"],
                "location": cand.get("location", ""), "url": cand.get("url", ""),
                "snippet": cand.get("snippet", ""), "salary": cand.get("salary", ""),
                "date_posted": cand.get("date_posted", ""),
                "raw_sources": cand["raw_sources"], "url_keys": cand_keys,
                "first_seen": today, "last_seen": today, "seen_count": 1,
                "fetched_at": None, "jd_profile": None, "match_scores": {},
                "status": "new",
                "possibly_closed": is_closed_posting(cand.get("snippet", "")),
                "verified": None, "scored_from": None,
            }
            table["jobs"].append(newjob)
            by_dedup[dk] = newjob
            for uk in cand_keys:
                by_urlkey[uk] = newjob
            to_analyze.append(_brief(newjob))

    archived = _archive_stale(table, ttl_days)
    _save(TABLE_PATH, table)

    stats = {
        "candidates_in": len(candidates), "deduped": len(batch),
        "new": sum(1 for j in table["jobs"] if j["status"] == "new"),
        "to_analyze": len(to_analyze), "to_score_only": len(to_score_only),
        "cached": len(cached), "archived": archived,
        "table_size": len(table["jobs"]),
    }
    print(json.dumps({"ok": True, "to_analyze": to_analyze,
                      "to_score_only": to_score_only, "cached": cached, "stats": stats}))


def cmd_update(cv_hash: str, cp_hash: str) -> None:
    mk = f"{cv_hash}:{cp_hash}"
    results = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "[]")
    if not isinstance(results, list):
        print(json.dumps({"ok": False, "error": "输入必须是打分结果数组"}))
        sys.exit(1)

    table = _load(TABLE_PATH)
    by_dedup = {j["dedup_key"]: j for j in table["jobs"]}
    updated = 0
    for r in results:
        if not isinstance(r, dict):
            continue
        job = by_dedup.get(r.get("dedup_key", ""))
        if not job:
            continue
        if r.get("jd_profile") is not None:
            job["jd_profile"] = r["jd_profile"]
            job["fetched_at"] = _now().isoformat()
        if r.get("match_score") is not None:
            job.setdefault("match_scores", {})[mk] = r["match_score"]
        for f in ("verified", "scored_from"):
            if r.get(f) is not None:
                job[f] = r[f]
        updated += 1

    _save(TABLE_PATH, table)
    print(json.dumps({"ok": True, "updated": updated}))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["merge", "update"])
    ap.add_argument("--cv-hash", required=True)
    ap.add_argument("--cp-hash", required=True)
    args = ap.parse_args()
    if args.mode == "merge":
        cmd_merge(args.cv_hash, args.cp_hash)
    else:
        cmd_update(args.cv_hash, args.cp_hash)


if __name__ == "__main__":
    main()
