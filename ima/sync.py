"""IMA 增量同步（笔记模式）：直接编辑已有文档，不再上传新文件。

官方接口能力约束（ima-skills-1.1.9 实测 + 2026-09-05 真实环境探针）：
- 知识库「文件」（create_media→COS→add_knowledge）不支持替换/编辑/删除
  → 文件路线已废弃；旧文件条目保留在知识库中（API 无法删除，见 README 清理指引）
- 「笔记」是官方唯一支持原地编辑的文档：append_doc 在原笔记末尾追加，
  挂库（media_type=11）后为**活引用**——追加内容知识库检索实时可见
- 重复挂同一笔记返回 220001「知识重复添加」→ 幂等
- 笔记容量实测：单次追加 4MB+ 成功，总长 8MB+ 正常

同步策略：
  每运营商每分类一篇笔记（标题：河北移动_套餐（实时更新））
  - 首次（迁移/笔记缺失）  → import_doc 全量基线 + add_knowledge 挂库
  - 数据变化（hash 改变）  → append_doc 原地追加【增量更新】段
    （新增/修改详情 + 下架清单 + 当前有效清单快照）
  - NO_CHANGE 且 hash 一致 → 跳过，不产生任何写操作
  - 完整性 FAIL            → 整个运营商跳过（闸门不变，删除同样以 PASS 为前提）

状态文件 data/ima-state.json：operators.<op>.categories.<分类> 记录
note_id / hash / count / updates；旧文件路线的 files 字段仅作历史参照。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from collector.config import DATA_DIR, IMA_KB_NAME, OPERATOR_META
from collector.storage import read_json
from ima.client import ImaApiError, ImaClient
from ima.markdown import (
    category_retired_markdown,
    note_baseline_markdown,
    note_heading,
    note_marker,
    note_title,
    update_append_markdown,
    content_hash,
)

STATE_PATH = DATA_DIR / "ima-state.json"
API_INTERVAL = 1.2          # 温和节奏（秒）
APPEND_CHUNK = 400_000      # 超限分片阈值（实测单次 4MB+ 安全，400KB 保守切分）
SIZE_ERROR_CODES = {"210009", "100009", 210009, 100009}


class ImaSyncManager:
    def __init__(self, dry_run: bool = False, log=print):
        self.dry_run = dry_run
        self.log = log
        self.client: ImaClient | None = None
        self.kb_id: str | None = None
        self.state: dict = self._load_state()

    # ── 状态持久化 ──
    def _load_state(self) -> dict:
        if STATE_PATH.exists():
            data = read_json(STATE_PATH)
            if isinstance(data, dict):
                data.setdefault("mode", "notes")
                return data
        return {"mode": "notes", "operators": {}}

    def _save_state(self):
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")

    def _ensure_client(self) -> bool:
        if self.client:
            return True
        client_id = os.environ.get("IMA_CLIENT_ID") or os.environ.get("IMA_OPENAPI_CLIENTID")
        api_key = os.environ.get("IMA_API_KEY") or os.environ.get("IMA_OPENAPI_APIKEY")
        if not client_id or not api_key:
            self.log("[ima] 缺少凭证（IMA_CLIENT_ID/IMA_API_KEY），跳过 IMA 同步")
            return False
        self.client = ImaClient(client_id, api_key)
        return True

    def _ensure_kb(self) -> bool:
        if self.kb_id:
            return True
        if not self._ensure_client():
            return False
        kb = self.client.find_knowledge_base(IMA_KB_NAME)
        if not kb:
            self.log(f"[ima] 未找到知识库「{IMA_KB_NAME}」")
            return False
        self.kb_id = kb["id"]
        self.log(f"[ima] 目标知识库: {IMA_KB_NAME} ({self.kb_id[:16]}…)")
        return True

    # ── 主入口 ──
    def sync(self, operators: list[str], run_summary: dict) -> dict:
        report = {"mode": "DRY_RUN" if self.dry_run else "LIVE", "carrier": "notes", "operators": {}, "kb": IMA_KB_NAME}
        candidates = []
        for op in operators:
            entry = run_summary.get("operators", {}).get(op) or {}
            if entry.get("status") != "PASS":
                report["operators"][op] = {"sync": "SKIPPED", "reason": "完整性校验未通过，不更新 IMA"}
                continue
            if not entry.get("latest_promoted", False) and not self.dry_run:
                report["operators"][op] = {"sync": "SKIPPED", "reason": "latest 未提升"}
                continue
            if entry.get("diff_status") == "NO_CHANGE":
                # 分类级 hash 兜底：笔记缺失（迁移未完成/部分失败）时仍需建笔记
                if self._category_hashes_unchanged(op):
                    report["operators"][op] = {"sync": "NO_CHANGE", "reason": "数据无变化，不重复写入"}
                    continue
            candidates.append(op)

        if not candidates and not self.dry_run:
            report["summary"] = "无待同步运营商"
            return report

        if self.dry_run:
            for op in candidates:
                plan = self._plan_operator(op)
                files = [
                    {"note_title": p["title"], "action": p["action"], "count": p["count"], "bytes": p["size"]}
                    for p in plan
                ]
                report["operators"][op] = {"sync": "DRY_RUN_PLAN", "notes": files}
            total = sum(len(r["notes"]) for r in [report["operators"][op] for op in candidates])
            report["summary"] = f"DRY_RUN：{len(candidates)} 家运营商待处理（共 {total} 篇笔记）"
            return report

        if not self._ensure_kb():
            report["error"] = "知识库定位失败"
            return report

        total_notes = 0
        for op in candidates:
            try:
                res = self._sync_operator(op)
                report["operators"][op] = res
                total_notes += len(res.get("created", [])) + len(res.get("appended", []))
            except Exception as e:
                report["operators"][op] = {"sync": "FAILED", "error": f"{type(e).__name__}: {str(e)[:200]}"}
        self._save_state()
        report["summary"] = f"同步完成：{total_notes} 篇笔记有写入（原地编辑，无新文件）"
        return report

    # ── 计划与判定 ──
    def _category_hashes_unchanged(self, op: str) -> bool:
        state_op = (self.state.get("operators") or {}).get(op) or {}
        cats_state = state_op.get("categories") or {}
        if not cats_state:
            return False  # 笔记尚未建立（迁移未完成）
        plan = self._plan_operator(op)
        plan_cats = {p["category"] for p in plan}
        if plan_cats != set(cats_state):
            return False
        for p in plan:
            st = cats_state.get(p["category"]) or {}
            if st.get("hash") != p["hash"]:
                return False
        return True

    def _load_items(self, op: str) -> tuple[list[dict], str]:
        latest = read_json(DATA_DIR / op / "latest.json")
        if not latest or not latest.get("items"):
            latest = read_json(DATA_DIR / op / "normalized.json") or {}
        items = latest.get("items") or []
        collected_at = latest.get("promoted_at") or latest.get("generated_at") or ""
        return items, collected_at

    def _plan_operator(self, op: str) -> list[dict]:
        """基于 latest.json（DRY_RUN 时 normalized.json）生成各分类笔记计划。

        hash 使用业务稳定内容（tariff_id + content_hash 排序序列），不含采集时间戳——
        同样的数据无论何时生成 hash 一致，严格实现 NO_CHANGE 不重复写入。
        """
        items, collected_at = self._load_items(op)
        meta = OPERATOR_META[op]
        state_op = ((self.state.get("operators") or {}).get(op) or {})
        cats_state = state_op.get("categories") or {}

        groups: dict[str, list] = {}
        for it in items:
            groups.setdefault(it.get("category") or "未分类", []).append(it)

        plan = []
        for cat in sorted(groups):
            cat_items = groups[cat]
            stable = "\n".join(sorted(f"{it.get('tariff_id')}:{it.get('content_hash')}" for it in cat_items))
            h = content_hash(stable)
            existing = cats_state.get(cat)
            content = None
            if not existing:
                content = note_baseline_markdown(op, meta["ima_folder"], cat, cat_items, collected_at)
                action, size = "CREATE_NOTE", len(content.encode("utf-8"))
            elif existing.get("hash") != h:
                action, size = "APPEND_UPDATE", 0  # 实际大小生成时确定
            else:
                action, size = "SKIP", 0
            plan.append(
                {
                    "category": cat,
                    "title": note_title(meta["ima_folder"], cat),
                    "hash": h,
                    "count": len(cat_items),
                    "action": action,
                    "content": content,
                    "size": size,
                }
            )
        return plan

    # ── 单运营商同步 ──
    def _sync_operator(self, op: str) -> dict:
        plan = self._plan_operator(op)
        state_op = (self.state.setdefault("operators", {}).setdefault(op, {"categories": {}}))
        cats_state = state_op.setdefault("categories", {})
        created, appended, skipped = [], [], []

        for entry in plan:
            cat = entry["category"]
            st = cats_state.get(cat)
            if st and st.get("hash") == entry["hash"]:
                skipped.append(entry["title"])
                continue
            if not st:
                note_id = self._find_existing_note(op, cat)
                if note_id is None:
                    content = entry["content"] or note_baseline_markdown(
                        op, OPERATOR_META[op]["ima_folder"], cat, self._cat_items(op, cat), self._collected_at(op)
                    )
                    note_id = self._create_note(content)
                    self._save_state()
                self.client.add_knowledge_note(note_id, entry["title"], self.kb_id)
                cats_state[cat] = {
                    "note_id": note_id,
                    "title": entry["title"],
                    "hash": entry["hash"],
                    "count": entry["count"],
                    "updates": 0,
                    "last_sync": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
                created.append({"note": entry["title"], "count": entry["count"], "note_id": note_id})
                self.log(f"[ima] 已建笔记并挂库: {entry['title']}（{entry['count']} 条）")
            else:
                content = self._build_update_markdown(op, cat, entry["count"])
                self._append_note(st["note_id"], content)
                st.update(
                    hash=entry["hash"],
                    count=entry["count"],
                    updates=(st.get("updates") or 0) + 1,
                    last_sync=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                )
                appended.append({"note": entry["title"], "count": entry["count"]})
                self.log(f"[ima] 已原地追加更新段: {entry['title']}（当前 {entry['count']} 条）")
            self._save_state()
            time.sleep(API_INTERVAL)

        # 分类整体消失（全部下架）→ 追加收尾段（完整性 PASS 前提下才走到这里）
        plan_cats = {p["category"] for p in plan}
        diff = read_json(DATA_DIR / op / "diff.json") or {}
        removed_all = (diff.get("removed") or {}).get("items") or []
        for cat, st in list(cats_state.items()):
            if cat in plan_cats or cat.startswith("__"):
                continue
            removed = [x for x in removed_all if x.get("category") == cat]
            if removed:
                md = category_retired_markdown(cat, removed)
                try:
                    self._append_note(st["note_id"], md)
                    st["retired"] = True
                    st["last_sync"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                    appended.append({"note": st.get("title") or cat, "retired": True, "count": 0})
                    self.log(f"[ima] 分类整体下架收尾: {cat}（{len(removed)} 条）")
                except Exception as e:
                    self.log(f"[ima] 分类收尾追加失败（下轮重试）: {cat}: {e}")
                self._save_state()

        state_op["last_sync"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        return {
            "sync": "UPDATED" if (created or appended) else "NO_CHANGE",
            "created": created,
            "appended": appended,
            "skipped_unchanged": skipped,
        }

    # ── 数据访问辅助 ──
    def _cat_items(self, op: str, cat: str) -> list[dict]:
        items, _ = self._load_items(op)
        return [it for it in items if (it.get("category") or "未分类") == cat]

    def _collected_at(self, op: str) -> str:
        _, collected_at = self._load_items(op)
        return collected_at

    def _build_update_markdown(self, op: str, cat: str, count: int) -> str:
        items = self._cat_items(op, cat)
        diff = read_json(DATA_DIR / op / "diff.json") or {}
        collected_at = self._collected_at(op)
        return update_append_markdown(op, OPERATOR_META[op]["ima_folder"], cat, diff, items, collected_at)

    # ── 笔记写入（含分片与恢复） ──
    def _create_note(self, content: str) -> str:
        """创建笔记；内容超限自动分片（官方指引：拆分多次 append_doc 写入）。"""
        try:
            note_id = self.client.import_doc(content)
            time.sleep(API_INTERVAL)
            return note_id
        except ImaApiError as e:
            if str(e.code) not in SIZE_ERROR_CODES:
                raise
            self.log(f"[ima] 单次写入超限（{str(e.msg)[:60]}），自动分片创建")
            cut = content.find("\n## ", 10_000)
            if cut < 0:
                raise
            head, rest = content[:cut], content[cut:]
            note_id = self.client.import_doc(head)
            self._append_note(note_id, rest)
            return note_id

    def _append_note(self, note_id: str, content: str) -> None:
        """原地追加；超限自动分片。"""
        try:
            self.client.append_doc(note_id, content)
            time.sleep(API_INTERVAL)
        except ImaApiError as e:
            if str(e.code) not in SIZE_ERROR_CODES:
                raise
            self.log(f"[ima] 追加超限（{str(e.msg)[:60]}），自动分片追加")
            for i in range(0, len(content), APPEND_CHUNK):
                self.client.append_doc(note_id, content[i : i + APPEND_CHUNK])
                time.sleep(API_INTERVAL)

    def _find_existing_note(self, op: str, cat: str) -> str | None:
        """迁移/部分失败恢复：按标识行找回已创建但未入 state 的笔记，避免重复建。"""
        marker = note_marker(op, cat)
        heading = note_heading(OPERATOR_META[op]["ima_folder"], cat)
        try:
            # 1) 正文搜索（标识行唯一）
            for nb in self.client.search_notes(marker, by_content=True):
                nid = nb.get("note_id")
                if nid and marker in self.client.get_doc_content(nid):
                    self.log(f"[ima] 复用既有笔记（正文标识匹配）: {heading}")
                    return str(nid)
            # 2) 标题搜索兜底
            for nb in self.client.search_notes(heading[:12]):
                nid = nb.get("note_id")
                if nid and marker in self.client.get_doc_content(nid):
                    self.log(f"[ima] 复用既有笔记（标题匹配）: {heading}")
                    return str(nid)
        except Exception as e:
            self.log(f"[ima] 笔记找回失败（忽略，将新建）: {str(e)[:80]}")
        return None
