"""IMA 增量同步：仅完整性 PASS 且有变化时更新；NO_CHANGE 不重复上传。

官方接口能力约束（实测 ima-skills-1.1.9）：
- 无"替换"能力 → 更新 = 上传新版本文件（同名冲突时加时间戳后缀，保留历史版本）
- 无删除接口 → 下架资费通过重新生成整分类文件体现（旧版本文件保留在知识库）
- 无建文件夹接口 → 层级通过文件名编码：`河北移动_套餐.md`
状态文件 data/ima-state.json 记录每个分类文件的 content_hash 与 media_id。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from collector.config import DATA_DIR, IMA_KB_NAME, OPERATOR_META
from collector.storage import read_json
from ima.client import ImaClient
from ima.cos import cos_upload
from ima.markdown import category_markdown, content_hash, overview_markdown

STATE_PATH = DATA_DIR / "ima-state.json"
MD_CONTENT_TYPE = "text/markdown"


class ImaSyncManager:
    def __init__(self, dry_run: bool = False, log=print):
        self.dry_run = dry_run
        self.log = log
        self.client: ImaClient | None = None
        self.kb_id: str | None = None
        self.state: dict = self._load_state()

    def _load_state(self) -> dict:
        if STATE_PATH.exists():
            data = read_json(STATE_PATH)
            if isinstance(data, dict):
                return data
        return {"operators": {}}

    def _save_state(self):
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")

    def _ensure_client(self):
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
        report = {"mode": "DRY_RUN" if self.dry_run else "LIVE", "operators": {}, "kb": IMA_KB_NAME}
        candidates = []
        for op in operators:
            entry = run_summary.get("operators", {}).get(op) or {}
            if entry.get("status") != "PASS":
                report["operators"][op] = {"sync": "SKIPPED", "reason": "完整性校验未通过，不更新 IMA"}
                continue
            if not entry.get("latest_promoted", False) and not self.dry_run:
                report["operators"][op] = {"sync": "SKIPPED", "reason": "latest 未提升"}
                continue
            diff_status = entry.get("diff_status")
            if diff_status == "NO_CHANGE":
                # 分类级 hash 兜底校验（state 缺失时仍需上传）
                if self._category_hashes_unchanged(op):
                    report["operators"][op] = {"sync": "NO_CHANGE", "reason": "数据无变化，不重复上传"}
                    continue
            candidates.append(op)

        if not candidates and not self.dry_run:
            report["summary"] = "无待同步运营商"
            return report

        if self.dry_run:
            for op in candidates:
                plan = self._plan_operator(op)
                report["operators"][op] = {"sync": "DRY_RUN_PLAN", "files": plan}
            report["summary"] = f"DRY_RUN：{len(candidates)} 家运营商待更新（共 {sum(len(r['files']) for r in [report['operators'][op] for op in candidates])} 个文件）"
            return report

        if not self._ensure_kb():
            report["error"] = "知识库定位失败"
            return report

        total_files = 0
        for op in candidates:
            try:
                res = self._sync_operator(op)
                report["operators"][op] = res
                total_files += len(res.get("uploaded", []))
            except Exception as e:
                report["operators"][op] = {"sync": "FAILED", "error": f"{type(e).__name__}: {str(e)[:200]}"}
        self._save_state()
        report["summary"] = f"同步完成：{total_files} 个文件更新"
        return report

    # ── 单运营商 ──
    def _category_hashes_unchanged(self, op: str) -> bool:
        state = (self.state.get("operators") or {}).get(op) or {}
        files = state.get("files") or {}
        if not files:
            return False
        plan = self._plan_operator(op)
        if len(plan) != len(files):
            return False
        for f in plan:
            prev = files.get(f["file_name"])
            if not prev or prev.get("hash") != f["hash"]:
                return False
        return True

    def _plan_operator(self, op: str) -> list[dict]:
        """基于 latest.json（或 DRY_RUN 时的 normalized.json）生成本次应上传的分类文件计划。

        hash 使用业务稳定内容（tariff_id + content_hash 排序序列），不含采集时间戳——
        同样的数据无论何时生成，hash 一致，从而严格实现 NO_CHANGE 不重复上传。
        """
        latest = read_json(DATA_DIR / op / "latest.json")
        if not latest or not latest.get("items"):
            latest = read_json(DATA_DIR / op / "normalized.json") or {}
        items = latest.get("items") or []
        collected_at = latest.get("promoted_at") or latest.get("generated_at") or ""
        meta = OPERATOR_META[op]
        groups: dict[str, list] = {}
        for it in items:
            groups.setdefault(it.get("category") or "未分类", []).append(it)
        plan = []
        for cat in sorted(groups):
            file_name, content = category_markdown(op, meta["ima_folder"], cat, groups[cat], collected_at)
            stable = "\n".join(
                sorted(f"{it.get('tariff_id')}:{it.get('content_hash')}" for it in groups[cat])
            )
            plan.append(
                {
                    "file_name": file_name,
                    "content": content,
                    "hash": content_hash(stable),
                    "size": len(content.encode("utf-8")),
                    "count": len(groups[cat]),
                }
            )
        return plan

    def _sync_operator(self, op: str) -> dict:
        plan = self._plan_operator(op)
        state_op = (self.state.setdefault("operators", {}).setdefault(op, {"files": {}}))
        files_state = state_op.setdefault("files", {})
        uploaded = []
        skipped = []
        for f in plan:
            prev = files_state.get(f["file_name"])
            if prev and prev.get("hash") == f["hash"]:
                skipped.append(f["file_name"])
                continue
            media_id, final_name = self._upload_file(f["file_name"], f["content"])
            files_state[f["file_name"]] = {
                "hash": f["hash"],
                "media_id": media_id,
                "uploaded_as": final_name,
                "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "count": f["count"],
            }
            uploaded.append({"file": final_name, "media_id": media_id, "count": f["count"]})
            time.sleep(1.0)  # 温和节奏
        state_op["last_sync"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        return {
            "sync": "UPDATED" if uploaded else "NO_CHANGE",
            "uploaded": uploaded,
            "skipped_unchanged": skipped,
        }

    def _upload_file(self, file_name: str, content: str) -> tuple[str, str]:
        """上传一个 Markdown 文件；同名已存在 → 时间戳版本名。返回 (media_id, final_name)。"""
        content_bytes = content.encode("utf-8")
        final_name = file_name
        # 重名检查（官方 GATE 3：重复 → 保留两者，时间戳后缀）
        repeated = self.client.check_repeated_names([file_name], self.kb_id)
        if repeated.get(file_name):
            stem, _, ext = file_name.rpartition(".")
            final_name = f"{stem}_{time.strftime('%Y%m%d%H%M%S')}.{ext}"
        # create_media（官方流程）
        media = self.client.create_media(
            file_name=final_name,
            file_size=len(content_bytes),
            content_type=MD_CONTENT_TYPE,
            kb_id=self.kb_id,
            file_ext="md",
        )
        media_id = media.get("media_id")
        cred = media.get("cos_credential") or {}
        if not media_id or not cred.get("cos_key"):
            raise RuntimeError(f"create_media 返回异常: {json.dumps(media, ensure_ascii=False)[:200]}")
        # COS 上传（官方凭证，临时密钥仅发往 *.myqcloud.com）
        cos_upload(content_bytes, cred, MD_CONTENT_TYPE)
        # add_knowledge（title 必须等于 file_name）
        self.client.add_knowledge_file(
            media_id=media_id,
            title=final_name,
            kb_id=self.kb_id,
            file_name=final_name,
            file_size=len(content_bytes),
            cos_key=cred["cos_key"],
        )
        self.log(f"[ima] 已上传: {final_name} (media_id={str(media_id)[:16]}…)")
        return media_id, final_name
