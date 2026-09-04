"""存储管理：data/<op>/ 布局 + latest 保护性更新。

布局（任务要求）：
data/<op>/
  raw/collect.json           本次原始采集（含原始接口数据与证据）
  normalized.json            本次标准化结果
  latest.json                上次【成功】版本（完整性 FAIL 时绝不覆盖）
  integrity-report.json      完整性校验报告
  diff.json                  与上次成功版本的差异
  history/<ts>.json.gz       历史成功版本归档（gzip）
"""
from __future__ import annotations

import gzip
import json
import shutil
import time
from pathlib import Path

from collector.config import data_dir_for


def load_latest(op: str) -> list[dict] | None:
    p = data_dir_for(op) / "latest.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("items") if isinstance(data, dict) else data
    except Exception:
        return None


def save_normalized(op: str, items: list[dict], meta: dict | None = None):
    d = data_dir_for(op)
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "operator": op,
        "count": len(items),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "items": items,
        **(meta or {}),
    }
    (d / "normalized.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def promote_to_latest(op: str) -> Path:
    """完整性 PASS 时：normalized.json → latest.json（并归档历史）。"""
    d = data_dir_for(op)
    normalized = d / "normalized.json"
    if not normalized.exists():
        raise FileNotFoundError(normalized)
    # 归档旧 latest
    latest = d / "latest.json"
    if latest.exists():
        hist_dir = d / "history"
        hist_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%dT%H%M%S")
        with open(latest, "rb") as fin, gzip.open(hist_dir / f"{ts}.json.gz", "wb") as fout:
            shutil.copyfileobj(fin, fout)
        # 保留最近 30 份归档
        archives = sorted(hist_dir.glob("*.json.gz"))
        for old in archives[:-30]:
            old.unlink(missing_ok=True)
    payload = json.loads(normalized.read_text(encoding="utf-8"))
    payload["promoted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    latest.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return latest


def read_json(p: Path):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return None
