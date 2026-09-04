"""历史变化检测：与上一次"成功版本"比较（tariff_id + content_hash）。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from collector.config import data_dir_for


def diff_tariffs(current: list[dict], previous: list[dict] | None) -> dict:
    cur_map = {x["tariff_id"]: x for x in current if x.get("tariff_id")}
    prev_map = {x["tariff_id"]: x for x in (previous or []) if x.get("tariff_id")}

    added_ids = sorted(set(cur_map) - set(prev_map))
    removed_ids = sorted(set(prev_map) - set(cur_map))
    modified_ids = sorted(
        tid for tid in set(cur_map) & set(prev_map)
        if cur_map[tid].get("content_hash") != prev_map[tid].get("content_hash")
    )
    unchanged_ids = sorted(set(cur_map) & set(prev_map) - set(modified_ids))

    def brief(tid, source):
        x = source[tid]
        return {"tariff_id": tid, "name": x.get("name"), "category": x.get("category")}

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "current_count": len(cur_map),
        "previous_count": len(prev_map),
        "added": {
            "count": len(added_ids),
            "items": [brief(t, cur_map) for t in added_ids],
        },
        "removed": {
            "count": len(removed_ids),
            "items": [brief(t, prev_map) for t in removed_ids],
        },
        "modified": {
            "count": len(modified_ids),
            "items": [
                {
                    "tariff_id": t,
                    "name": cur_map[t].get("name"),
                    "category": cur_map[t].get("category"),
                    "fields": _changed_fields(cur_map[t], prev_map[t]),
                }
                for t in modified_ids
            ],
        },
        "unchanged": {"count": len(unchanged_ids)},
        "has_change": bool(added_ids or removed_ids or modified_ids),
        "status": "CHANGED" if (added_ids or removed_ids or modified_ids) else "NO_CHANGE",
    }


def _changed_fields(cur: dict, prev: dict) -> list[str]:
    keys = ["name", "price", "traffic", "voice", "sms", "validity", "eligibility", "description", "category", "subcategory"]
    return [k for k in keys if cur.get(k) != prev.get(k)]


def save_diff(op: str, diff: dict) -> Path:
    d = data_dir_for(op)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "diff.json"
    # 控制文件体积：added/modified 明细最多各保留 200 条，removed 全量
    slim = dict(diff)
    if diff["added"]["count"] > 200:
        slim["added"] = {"count": diff["added"]["count"], "items": diff["added"]["items"][:200], "truncated": True}
    if diff["modified"]["count"] > 200:
        slim["modified"] = {"count": diff["modified"]["count"], "items": diff["modified"]["items"][:200], "truncated": True}
    p.write_text(json.dumps(slim, ensure_ascii=False, indent=1), encoding="utf-8")
    return p
