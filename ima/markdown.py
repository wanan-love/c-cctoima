"""资费数据 → IMA 知识库 Markdown 生成。

结构（无官方建文件夹接口 → 文件名编码层级，检索友好）：
  河北移动_套餐.md / 河北移动_加装包.md / …
  河北联通_套餐.md / …
内容：分类头（来源/采集时间/条数）+ 每个资费的详情段落。
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
    """返回 (file_name, markdown_content)。"""
    items = sorted(items, key=lambda x: (x.get("subcategory") or "", x.get("name") or ""))
    n = len(items)
    md = []
    md.append(f"# {operator_name} · {category}")
    md.append("")
    md.append(f"> 范围：河北省 · 个人用户 · {category}")
    md.append(f"> 来源：{items[0].get('source_url') if items else ''}")
    md.append(f"> 采集时间：{collected_at} · 共 {n} 条资费")
    md.append("")
    subcats: dict[str, list] = {}
    for it in items:
        subcats.setdefault(it.get("subcategory") or "", []).append(it)
    if any(subcats):
        for sub, group in subcats.items():
            if sub:
                md.append(f"## {sub}（{len(group)} 条）")
                md.append("")
            for it in group:
                md.append(tariff_section(it))
    if n == 0:
        md.append("（本分类当前无在售资费）")
    content = "\n".join(md)
    # 文件名安全化：斜杠等路径字符替换（IMA 文件名即 title，必须等于 file_name）
    safe_category = str(category).replace("/", "／").replace("\\", "＼").replace(":", "：")
    file_name = f"{operator_name}_{safe_category}.md"
    return file_name, content


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
