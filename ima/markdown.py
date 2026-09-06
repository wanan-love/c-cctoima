"""资费数据 → IMA 知识库内容生成。

现行载体 = **笔记**（官方唯一支持原地编辑的文档形态）：
  挂库（add_knowledge media_type=11）后为活引用——append_doc 追加即时生效，
  变更不再产生新文件。官方文件路线不支持替换/删除，已废弃。

笔记结构（每运营商每分类一篇）：
  标题：河北移动_套餐（实时更新）        ← 知识库条目标题（add_knowledge.title）
  首行：# 河北移动 · 套餐（实时更新）    ← 笔记标题（IMA 取首行 # 标题）
  正文：阅读指引 + 标识行 + 全量基线 + 逐次【增量更新】段（追加在文末）
"""
from __future__ import annotations

import hashlib
import time
from typing import Iterable

FIELD_LABELS = [
    ("price", "资费标准"),
    ("traffic", "流量"),
    ("voice", "语音"),
    ("sms", "短信"),
    ("validity", "有效期限"),
    ("eligibility", "适用范围"),
]

DETAIL_LABELS = [
    "方案编号", "系列", "编号", "资费类型", "超出资费", "其他费用", "宽带", "IPTV",
    "权益", "退订方式", "违约责任", "在网要求", "销售渠道", "订购渠道", "上线日期",
    "下线日期", "生效日期", "失效日期", "合约期", "服务内容", "其他说明", "适用地区",
    "三级名称", "副卡/亲情网", "套外资费", "互斥规则", "到期规则", "产品代码",
]


def _md_escape(s: str) -> str:
    return str(s or "").replace("|", "\\|").replace("\n", " ")


def tariff_section(item: dict) -> str:
    lines = []
    name = item.get("name") or "（未命名）"
    tid = item.get("tariff_id") or ""
    title = f"### {name}"
    if tid:
        title += f"（{tid}）"
    lines.append(title)
    lines.append("")
    core = [f"**{label}**：{_md_escape(item.get(key))}" for key, label in FIELD_LABELS if item.get(key)]
    if core:
        lines.append("；".join(core))
        lines.append("")
    details = item.get("details") or {}
    rows = [(k, details[k]) for k in DETAIL_LABELS if details.get(k)]
    # 兜底：未映射键
    for k, v in details.items():
        if k not in DETAIL_LABELS and v:
            rows.append((k, v))
    if rows:
        lines.append("| 项目 | 内容 |")
        lines.append("| --- | --- |")
        for k, v in rows:
            lines.append(f"| {_md_escape(k)} | {_md_escape(v)} |")
        lines.append("")
    desc = item.get("description")
    if desc:
        lines.append("**说明**：" + str(desc).strip())
        lines.append("")
    return "\n".join(lines)


def category_markdown(op: str, operator_name: str, category: str, items: Iterable[dict], collected_at: str) -> tuple[str, str]:
    """（旧文件路线，保留兼容）返回 (file_name, markdown_content)。"""
    items = sorted(items, key=lambda x: (x.get("subcategory") or "", x.get("name") or ""))
    n = len(items)
    md = []
    md.append(f"# {operator_name} · {category}")
    md.append("")
    md.append(f"> 范围：河北省 · 个人用户 · {category}")
    md.append(f"> 来源：{items[0].get('source_url') if items else ''}")
    md.append(f"> 采集时间：{collected_at} · 共 {n} 条资费")
    md.append("")
    md.extend(_category_body(items))
    if n == 0:
        md.append("（本分类当前无在售资费）")
    content = "\n".join(md)
    # 文件名安全化：斜杠等路径字符替换（IMA 文件名即 title，必须等于 file_name）
    safe_category = str(category).replace("/", "／").replace("\\", "＼").replace(":", "：")
    file_name = f"{operator_name}_{safe_category}.md"
    return file_name, content


def _category_body(items: Iterable[dict]) -> list[str]:
    """分类正文：按子分类分组 + 每资费详情段落（笔记/文件共用）。

    注意：全部资费无子分类（key=""）时也必须输出详情段落——
    旧实现 `if any(subcats)` 对全空 key 判 False，导致正文为空
    （生产环境河北电信三个文件仅 169~182 字节的根因，已修复）。
    """
    items = sorted(items, key=lambda x: (x.get("subcategory") or "", x.get("name") or ""))
    md: list[str] = []
    subcats: dict[str, list] = {}
    for it in items:
        subcats.setdefault(it.get("subcategory") or "", []).append(it)
    for sub, group in subcats.items():
        if sub:
            md.append(f"## {sub}（{len(group)} 条）")
            md.append("")
        for it in group:
            md.append(tariff_section(it))
    return md


# ════════════════ 笔记路线（现行） ════════════════

NOTE_SUFFIX = "（实时更新）"


def _safe_category(category: str) -> str:
    return str(category).replace("/", "／").replace("\\", "＼").replace(":", "：")


def note_title(operator_folder: str, category: str) -> str:
    """state/报告中的笔记标识名：河北移动_套餐（实时更新）。

    实测说明：知识库条目实际显示标题取自笔记首行 # 标题（note_heading，
    如「河北移动 · 套餐（实时更新）」），add_knowledge 的 title 参数对笔记不生效。
    """
    return f"{operator_folder}_{_safe_category(category)}{NOTE_SUFFIX}"


def note_heading(operator_folder: str, category: str) -> str:
    """笔记首行标题（IMA 取 content 首行 # 标题）：河北移动 · 套餐（实时更新）。"""
    return f"{operator_folder} · {_safe_category(category)}{NOTE_SUFFIX}"


def note_marker(op: str, category: str) -> str:
    """笔记唯一标识行（恢复/幂等判定 + 内容搜索锚点）。"""
    return f"c-cctoima:{op}:{_safe_category(category)}"


def note_baseline_markdown(
    op: str, operator_folder: str, category: str, items: Iterable[dict], collected_at: str
) -> str:
    """笔记全量基线内容（首次迁移创建时写入）。"""
    items = list(items)
    n = len(items)
    md = [f"# {note_heading(operator_folder, category)}", ""]
    md.append(f"> 范围：河北省 · 个人用户 · {category}")
    md.append(f"> 来源：{items[0].get('source_url') if items else ''}")
    md.append(f"> 笔记标识：{note_marker(op, category)}")
    md.append("")
    md.append("> ⚠️ **阅读指引**：本笔记按时间顺序增量更新，**最新数据始终在文末**。")
    md.append("> 前文与「【增量更新】」段冲突时，以更靠后的更新段为准；当前在售 = 基线 − 历次下架 + 历次新增，已下架资费以各更新段下架清单为准。")
    md.append("")
    md.append(f"## 全量基线（{collected_at}，共 {n} 条）")
    md.append("")
    md.extend(_category_body(items))
    if n == 0:
        md.append("（本分类当前无在售资费）")
    return "\n".join(md)


_FIELD_CN = {
    "name": "名称", "price": "资费", "traffic": "流量", "voice": "语音", "sms": "短信",
    "validity": "有效期", "eligibility": "适用范围", "description": "说明",
    "category": "分类", "subcategory": "子分类",
}


def _compact_line(item: dict) -> str:
    parts = [f"**{_md_escape(item.get('name') or '（未命名）')}**"]
    for key, label in (("price", "资费"), ("traffic", "流量"), ("voice", "语音"), ("validity", "有效期")):
        v = item.get(key)
        if v:
            parts.append(f"{label} {_md_escape(v)}")
    tid = item.get("tariff_id")
    if tid:
        parts.append(f"编号 {tid}")
    return " — ".join(parts)


def compact_list_markdown(items: Iterable[dict]) -> list[str]:
    """当前有效资费紧凑清单（每次更新段都携带最新全量，检索权威快照）。"""
    items = sorted(items, key=lambda x: (x.get("subcategory") or "", x.get("name") or ""))
    return [f"- {_compact_line(it)}" for it in items]


def update_append_markdown(
    op: str,
    operator_folder: str,
    category: str,
    diff: dict,
    items: list[dict],
    collected_at: str,
    now: str | None = None,
    include_snapshot: bool = False,
) -> str:
    """生成一次增量更新段（append_doc 原地追加到笔记文末）——**只含变化项**。

    diff: data/<op>/diff.json（added/removed/modified 明细，运营商级，此处按分类过滤）
    items: 本分类当前（latest）资费列表，用于新增/修改的完整详情与头部计数
    include_snapshot: True 时段末额外携带「当前有效清单」全量紧凑快照
        （默认关闭：避免每次更新都追加全量内容导致笔记膨胀；
         设 IMA_UPDATE_SNAPSHOT=1 可恢复，供检索准确度回退使用）
    """
    by_id = {x.get("tariff_id"): x for x in items}
    added = [x for x in (diff.get("added") or {}).get("items") or [] if x.get("category") == category]
    removed = [x for x in (diff.get("removed") or {}).get("items") or [] if x.get("category") == category]
    modified = [x for x in (diff.get("modified") or {}).get("items") or [] if x.get("category") == category]
    truncated = bool((diff.get("added") or {}).get("truncated") or (diff.get("modified") or {}).get("truncated"))
    ts = now or time.strftime("%Y-%m-%d %H:%M")

    md = ["", "---", ""]
    md.append(f"## 【增量更新 {ts}】新增 {len(added)} · 修改 {len(modified)} · 下架 {len(removed)} · 当前有效 {len(items)} 条")
    md.append("")
    md.append("> ⚠️ 本段为最新数据，与此前段落冲突时一律以本段为准。")
    md.append("> 当前在售 = 全量基线 − 历次下架 + 历次新增（以更靠后的更新段为准）。")
    md.append("")

    if added:
        md.append(f"### ✅ 本次新增（{len(added)}）")
        md.append("")
        for x in added:
            it = by_id.get(x.get("tariff_id"))
            if it:
                md.append(tariff_section(it))
        md.append("")
    if modified:
        md.append(f"### 🔄 本次修改（{len(modified)}，以下为最新值）")
        md.append("")
        for x in modified:
            it = by_id.get(x.get("tariff_id"))
            if it:
                fields = "、".join(_FIELD_CN.get(k, k) for k in (x.get("fields") or []))
                if fields:
                    md.append(f"（变更字段：{fields}）")
                md.append(tariff_section(it))
        md.append("")
    if removed:
        md.append(f"### ❌ 本次下架（{len(removed)}，已停售，不再受理）")
        md.append("")
        for x in removed:
            md.append(f"- {_md_escape(x.get('name') or '（未命名）')}（编号 {x.get('tariff_id') or '—'}）")
        md.append("")
    if truncated:
        md.append("> 注：本次变更明细超长仅列出前若干条，完整明细见仓库 data/ 目录 diff.json。")
        md.append("")

    if include_snapshot:
        md.append(f"### 📋 当前有效清单（{len(items)} 条 · 数据版本 {collected_at}）")
        md.append("")
        md.extend(compact_list_markdown(items))
        md.append("")
    return "\n".join(md)


def category_retired_markdown(category: str, removed_items: list[dict], now: str | None = None) -> str:
    """整分类下架时的收尾追加段。"""
    ts = now or time.strftime("%Y-%m-%d %H:%M")
    md = ["", "---", ""]
    md.append(f"## 【增量更新 {ts}】本分类已全部下架（共 {len(removed_items)} 条）")
    md.append("")
    md.append(f"> ⚠️ 截至 {ts}，该分类下全部资费均已停售，以下清单仅供参考，不可订购。")
    for x in removed_items:
        md.append(f"- {_md_escape(x.get('name') or '（未命名）')}（编号 {x.get('tariff_id') or '—'}）")
    md.append("")
    return "\n".join(md)


def overview_markdown(op_results: list[dict]) -> tuple[str, str]:
    """总览文档：河北运营商资费_总览.md"""
    md = ["# 河北运营商资费 · 总览", ""]
    md.append("> 范围：河北省 · 个人用户 · 公示资费（四家运营商）")
    md.append(f"> 更新时间：{time.strftime('%Y-%m-%d %H:%M')}")
    md.append("")
    md.append("| 运营商 | 分类 | 资费数 | 更新时间 |")
    md.append("| --- | --- | --- | --- |")
    for r in op_results:
        for cat in r.get("categories", []):
            md.append(f"| {r['operator_name']} | {cat['category']} | {cat['count']} | {r.get('promoted_at', '')} |")
    md.append("")
    md.append("## 文档结构")
    md.append("")
    md.append("每个文件对应一家运营商的一个资费分类（文件名格式：`运营商_分类.md`），")
    md.append("内容为该分类下全部资费的标准字段与完整详情，供知识库检索问答使用。")
    content = "\n".join(md)
    return "河北运营商资费_总览.md", content


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
