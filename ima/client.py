"""IMA OpenAPI 官方客户端（协议来自官方 ima-skills-1.1.9 实测）。

- Base URL: https://ima.qq.com，HTTP POST + JSON
- Headers: ima-openapi-clientid / ima-openapi-apikey / Content-Type: application/json
- 响应: {code, msg, data}，code=0 成功
- 游标分页：cursor="" 起始，is_end=true 停止
- 错误码：110010(下游网络)/110021(频控) 可重试；110011 不可重试
- 文件上传：create_media → COS PUT → add_knowledge（title 必须等于 file_name）
  官方 GATE 3 明确文件「不支持替换」且无删除接口 → 文件路线已废弃，仅保留方法供参考
- 笔记路线（现行）：import_doc 创建 → append_doc 原地追加（挂库后为活引用，实测生效）
  add_knowledge(media_type=11) 挂库幂等：重复挂同一 note_id 返回 220001「知识重复添加」

实测备忘（2026-09-05 探针）：
- 笔记标题取 content 首行 # 标题，超长被截断（约 30 字符）
- note_id 为 16 位数字；媒体 media_id 形如 note_{hash}_{note_id}{user_id}
- 单次 append 实测 4MB+ 成功；笔记总长 8MB+ 仍可正常读取
- search_note 按标题（search_type=0）/正文（search_type=1）检索
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from collector.config import IMA_BASE_URL

RETRYABLE_CODES = {"110010", "110021", 110010, 110021}


class ImaApiError(Exception):
    def __init__(self, code, msg, path):
        super().__init__(f"[{path}] code={code} msg={msg}")
        self.code = code
        self.msg = msg
        self.path = path


class ImaClient:
    def __init__(self, client_id: str | None = None, api_key: str | None = None, base_url: str = IMA_BASE_URL):
        self.client_id = client_id or os.environ.get("IMA_CLIENT_ID") or os.environ.get("IMA_OPENAPI_CLIENTID")
        self.api_key = api_key or os.environ.get("IMA_API_KEY") or os.environ.get("IMA_OPENAPI_APIKEY")
        self.base_url = base_url
        if not self.client_id or not self.api_key:
            raise RuntimeError("缺少 IMA 凭证（IMA_CLIENT_ID / IMA_API_KEY）")

    def post(self, path: str, body: dict, retries: int = 3) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last_err = None
        for attempt in range(1, retries + 1):
            try:
                req = urllib.request.Request(
                    f"{self.base_url}/{path}",
                    data=data,
                    headers={
                        "ima-openapi-clientid": self.client_id,
                        "ima-openapi-apikey": self.api_key,
                        "Content-Type": "application/json",
                        "User-Agent": "c-cctoima/1.0",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=60) as r:
                    text = r.read().decode("utf-8")
                resp = json.loads(text)
                code = resp.get("code")
                if code == 0 or code == "0":
                    return resp
                if code in RETRYABLE_CODES and attempt < retries:
                    time.sleep(2 * attempt)
                    continue
                raise ImaApiError(code, resp.get("msg", ""), path)
            except urllib.error.HTTPError as e:
                last_err = ImaApiError(e.code, f"HTTP {e.code}", path)
                if attempt < retries:
                    time.sleep(2 * attempt)
                    continue
                raise last_err
            except urllib.error.URLError as e:
                last_err = ImaApiError(-1, f"网络错误: {e.reason}", path)
                if attempt < retries:
                    time.sleep(3 * attempt)
                    continue
                raise last_err
        raise last_err or ImaApiError(-1, "未知错误", path)

    # ── 知识库（openapi/wiki/v1） ──
    def search_knowledge_base(self, query: str, limit: int = 20) -> list[dict]:
        resp = self.post("openapi/wiki/v1/search_knowledge_base", {"query": query, "cursor": "", "limit": limit})
        info_list = (resp.get("data") or {}).get("info_list") or []
        # 实测字段名 kb_id/kb_name（与文档 id/name 兼容）
        out = []
        for kb in info_list:
            out.append(
                {
                    "id": kb.get("kb_id") or kb.get("id"),
                    "name": kb.get("kb_name") or kb.get("name"),
                    "raw": kb,
                }
            )
        return out

    def find_knowledge_base(self, name: str) -> dict | None:
        for kb in self.search_knowledge_base(name):
            if kb["name"] == name:
                return kb
        # 空 query 兜底全量搜索
        for kb in self.search_knowledge_base("", limit=50):
            if kb["name"] == name:
                return kb
        return None

    def get_knowledge_list(self, kb_id: str, folder_id: str | None = None, limit: int = 50) -> list[dict]:
        body = {"knowledge_base_id": kb_id, "cursor": "", "limit": limit}
        if folder_id:
            body["folder_id"] = folder_id
        resp = self.post("openapi/wiki/v1/get_knowledge_list", body)
        return (resp.get("data") or {}).get("knowledge_list") or []

    def check_repeated_names(self, names: list[str], kb_id: str, media_type: int = 7, folder_id: str | None = None) -> dict:
        body = {
            "params": [{"name": n, "media_type": media_type} for n in names],
            "knowledge_base_id": kb_id,
        }
        if folder_id:
            body["folder_id"] = folder_id
        resp = self.post("openapi/wiki/v1/check_repeated_names", body)
        results = (resp.get("data") or {}).get("results") or []
        return {r.get("name"): bool(r.get("is_repeated")) for r in results}

    def create_media(self, file_name: str, file_size: int, content_type: str, kb_id: str, file_ext: str) -> dict:
        resp = self.post(
            "openapi/wiki/v1/create_media",
            {
                "file_name": file_name,
                "file_size": file_size,
                "content_type": content_type,
                "knowledge_base_id": kb_id,
                "file_ext": file_ext,
            },
        )
        return resp.get("data") or {}

    def add_knowledge_file(self, media_id: str, title: str, kb_id: str, file_name: str, file_size: int, cos_key: str) -> dict:
        resp = self.post(
            "openapi/wiki/v1/add_knowledge",
            {
                "media_type": 7,  # Markdown
                "media_id": media_id,
                "title": title,
                "knowledge_base_id": kb_id,
                "file_info": {"cos_key": cos_key, "file_size": file_size, "file_name": file_name},
            },
        )
        return resp.get("data") or {}

    def get_knowledge_base_info(self, kb_id: str) -> dict:
        resp = self.post("openapi/wiki/v1/get_knowledge_base", {"ids": [kb_id]})
        infos = (resp.get("data") or {}).get("infos") or {}
        return infos.get(kb_id) or {}

    # ── 笔记（openapi/note/v1）——现行同步路线 ──
    def import_doc(self, content: str) -> str:
        """创建 Markdown 笔记，返回 note_id。笔记标题取 content 首行 # 标题。"""
        resp = self.post("openapi/note/v1/import_doc", {"content_format": 1, "content": content})
        note_id = (resp.get("data") or {}).get("note_id")
        if not note_id:
            raise ImaApiError(-1, "import_doc 未返回 note_id", "import_doc")
        return str(note_id)

    def append_doc(self, note_id: str, content: str) -> None:
        """向已有笔记末尾原地追加 Markdown（挂库后知识库条目实时生效）。"""
        self.post("openapi/note/v1/append_doc", {"note_id": note_id, "content_format": 1, "content": content})

    def get_doc_content(self, note_id: str) -> str:
        """读取笔记纯文本正文（target_content_format=0）。"""
        resp = self.post("openapi/note/v1/get_doc_content", {"note_id": note_id, "target_content_format": 0})
        return (resp.get("data") or {}).get("content") or ""

    def search_notes(self, query: str, by_content: bool = False, limit: int = 20) -> list[dict]:
        """搜索本人笔记，返回 NoteBookInfo 列表（note_id/title/modify_time…）。"""
        resp = self.post(
            "openapi/note/v1/search_note",
            {
                "search_type": 1 if by_content else 0,
                "query_info": {"content": query} if by_content else {"title": query},
                "start": 0,
                "end": max(1, min(limit, 20)),
            },
        )
        infos = (resp.get("data") or {}).get("search_note_infos") or []
        return [it.get("note_book_info") or {} for it in infos]

    def add_knowledge_note(self, note_id: str, title: str, kb_id: str) -> dict:
        """把笔记挂进知识库（media_type=11，活引用）。

        官方幂等保护：同一 note_id 重复挂库返回 code=220001「知识重复添加」→ 视为已挂载成功。
        """
        try:
            resp = self.post(
                "openapi/wiki/v1/add_knowledge",
                {
                    "media_type": 11,
                    "note_info": {"content_id": note_id},
                    "title": title,
                    "knowledge_base_id": kb_id,
                },
            )
            return resp.get("data") or {}
        except ImaApiError as e:
            if str(e.code) == "220001":
                return {"already_linked": True}
            raise
