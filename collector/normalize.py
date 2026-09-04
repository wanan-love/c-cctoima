"""标准化：四家运营商原始记录 → 统一资费 schema。

统一字段（任务要求）：
operator, province, audience, category, subcategory, tariff_id, name, price,
traffic, voice, sms, validity, eligibility, description, details,
source_url, source_api, collected_at, content_hash
"""
from __future__ import annotations

import hashlib
import html as html_mod
import json
import re
from typing import Any

from collector.config import AUDIENCE, ENTRY_URLS, OPERATOR_META, PROVINCE

# content_hash 参与字段（稳定业务字段；不含 collected_at / source / 抓取时间戳）
HASH_FIELDS = [
    "operator", "province", "audience", "category", "subcategory",
    "tariff_id", "name", "price", "traffic", "voice", "sms",
    "validity", "eligibility", "description", "details",
]


def _clean(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip()
    return re.sub(r"\s+", " ", s) if s else ""


def _fen_to_yuan(v: Any) -> str:
    """分 → 元（广电 productPrice 单位为分）。"""
    try:
        fen = float(v)
        yuan = fen / 100
        if yuan == int(yuan):
            return f"{int(yuan)}元"
        return f"{round(yuan, 2)}元"
    except (TypeError, ValueError):
        return _clean(v)


def _parse_html_fields(html_text: str) -> dict:
    """从电信 jbxx/ffnr HTML 中提取 标签：值 对 与 表格结构。"""
    if not html_text:
        return {}
    fields = {}
    # 1) 表格结构（th 表头 / td 值 成对映射）
    for tr_html in re.findall(r"<tr[^>]*>(.*?)</tr>", html_text, flags=re.S):
        headers = [re.sub(r"<[^>]+>", "", h).strip() for h in re.findall(r"<th[^>]*>(.*?)</th>", tr_html, flags=re.S)]
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<td[^>]*>(.*?)</td>", tr_html, flags=re.S)]
        if headers and cells and len(headers) == len(cells):
            for h, c in zip(headers, cells):
                h, c = html_mod.unescape(h).strip(), html_mod.unescape(c).strip()
                if h and c and h not in fields:
                    fields[h] = c
    # 2) 标签：值 文本对
    text = re.sub(r"<br\s*/?>", "\n", html_text)
    text = re.sub(r"</(p|div|tr|li|span)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    for m in re.finditer(
        r"([\u4e00-\u9fa5A-Za-z0-9（）()/-]{2,14})[：:]\s*([^：:\n]{1,400}?)(?=\s*[\u4e00-\u9fa5A-Za-z0-9（）()/-]{2,14}[：:]|\s*$)",
        text,
    ):
        key = m.group(1).strip()
        val = re.sub(r"\s+", " ", m.group(2)).strip()
        if key and val and key not in fields:
            fields[key] = val
    return fields


def _content_hash(item: dict) -> str:
    payload = {k: item.get(k, "") for k in HASH_FIELDS}
    s = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _make_item(op, category, subcategory, tariff_id, name, price, traffic, voice,
               sms, validity, eligibility, description, details, source_api, collected_at):
    item = {
        "operator": op,
        "operator_name": OPERATOR_META[op]["name"],
        "province": PROVINCE,
        "audience": AUDIENCE,
        "category": _clean(category),
        "subcategory": _clean(subcategory),
        "tariff_id": _clean(tariff_id),
        "name": _clean(name),
        "price": _clean(price),
        "traffic": _clean(traffic),
        "voice": _clean(voice),
        "sms": _clean(sms),
        "validity": _clean(validity),
        "eligibility": _clean(eligibility),
        "description": _clean(description),
        "details": details or {},
        "source_url": ENTRY_URLS[op],
        "source_api": source_api,
        "collected_at": collected_at,
    }
    item["content_hash"] = _content_hash(item)
    return item


# ══════════════ 各运营商标准化 ══════════════

def normalize_cmcc(raw_item: dict) -> dict | None:
    """raw_item: {bean_seqno, bean_name, list_kind, ...nonModule 字段}"""
    r = raw_item
    tariff_id = r.get("reportNo") or r.get("seqno") or r.get("id")
    name = r.get("name") or r.get("tariffName")
    if not name and not tariff_id:
        return None
    price = ""
    if r.get("fees") is not None:
        price = f"{_clean(r.get('fees'))}{_clean(r.get('feesUnit') or '')}"
    traffic = ""
    if r.get("data") is not None:
        traffic = f"{_clean(r.get('data'))}{_clean(r.get('dataUnit') or '')}"
    if r.get("orientTraffic"):
        traffic = (traffic + "+" if traffic else "") + f"定向{_clean(r['orientTraffic'])}{_clean(r.get('orientTrafficUnit') or '')}"
    voice = _clean(r.get("call"))
    sms = _clean(r.get("sms"))
    details = {
        "方案编号": _clean(r.get("reportNo")),
        "系列": _clean(r.get("bean_name")),
        "适用地区": _clean(r.get("applicableArea")),
        "销售渠道": _clean(r.get("channel")),
        "上线日期": _clean(r.get("onlineDay")),
        "下线日期": _clean(r.get("offineDay")),
        "超出资费": _clean(r.get("extraFees")),
        "其他费用": _clean(r.get("otherFees")),
        "宽带": _clean(r.get("brandwidth")),
        "IPTV": _clean(r.get("iptv")),
        "权益": _clean(r.get("rights")),
        "退订方式": _clean(r.get("unsubscribe")),
        "违约责任": _clean(r.get("responsibility")),
        "在网要求": _clean(r.get("duration") or r.get("others")),
    }
    details = {k: v for k, v in details.items() if v}
    return _make_item(
        "cmcc",
        category=raw_item.get("_category"),
        subcategory=raw_item.get("_subcategory"),
        tariff_id=f"CMCC-{tariff_id}" if tariff_id else "",
        name=name,
        price=price,
        traffic=traffic,
        voice=voice,
        sms=sms,
        validity=r.get("validPeriod"),
        eligibility=r.get("applicablePeople"),
        description=r.get("otherContent"),
        details=details,
        source_api=raw_item.get("_source_api", ""),
        collected_at=raw_item.get("_collected_at", ""),
    )


def normalize_cucc(raw_item: dict) -> dict | None:
    """raw_item: {threeLevelName, list_item: {...}, detail: {...}}"""
    li = raw_item.get("list_item") or {}
    de = raw_item.get("detail") or {}
    tariff_id = li.get("reportNo") or de.get("reportNo")
    name = li.get("name") or de.get("name")
    if not name and not tariff_id:
        return None
    price = ""
    if de.get("feesStandard") is not None:
        price = f"{_clean(de.get('feesStandard'))}{_clean(de.get('feeUnit') or '')}"
    traffic = ""
    if de.get("commonData") is not None:
        traffic = f"{_clean(de.get('commonData'))}{_clean(de.get('dataUnit') or '')}"
    if de.get("orientTraffic"):
        traffic = (traffic + "+" if traffic else "") + f"定向{_clean(de['orientTraffic'])}{_clean(de.get('orientTrafficUnit') or '')}"
    details = {
        "方案编号": _clean(li.get("reportNo")),
        "三级名称": _clean(raw_item.get("threeLevelName")),
        "资费类型": _clean(de.get("codeType")),
        "其他费用": _clean(de.get("otherFees")),
        "超出资费": _clean(de.get("extraFees")),
        "IPTV": _clean(de.get("iptv")),
        "带宽": _clean(de.get("broadBand")),
        "权益": _clean(de.get("equityCoupon")),
        "服务内容": _clean(de.get("serviceContent")),
        "订购渠道": _clean(de.get("saleChnl")),
        "合约期": _clean(de.get("onlinePeriod")),
        "生效日期": _clean(de.get("startDate")),
        "失效日期": _clean(de.get("endDate")),
        "退订方式": _clean(de.get("unsubscribe")),
        "违约责任": _clean(de.get("contractDuty")),
        "其他说明": _clean(de.get("otherDesc")),
    }
    details = {k: v for k, v in details.items() if v}
    return _make_item(
        "cucc",
        category=raw_item.get("_category"),
        subcategory=raw_item.get("_subcategory"),
        tariff_id=f"CUCC-{tariff_id}" if tariff_id else "",
        name=name,
        price=price,
        traffic=traffic,
        voice=_clean(de.get("minute")),
        sms=_clean(de.get("sms")),
        validity=de.get("validPeriod"),
        eligibility=de.get("useScope"),
        description=de.get("serviceContent") or de.get("otherDesc"),
        details=details,
        source_api=raw_item.get("_source_api", ""),
        collected_at=raw_item.get("_collected_at", ""),
    )


def normalize_ctcc(raw_item: dict) -> dict | None:
    """raw_item: 3Title.do 的 dataObject 元素（含 jbxx/ffnr HTML）。"""
    r = raw_item
    tariff_id = r.get("report_no") or r.get("id")
    name = r.get("name")
    if not name and not tariff_id:
        return None
    jb = _parse_html_fields(r.get("jbxx") or "")
    ff = _parse_html_fields(r.get("ffnr") or "")
    price = jb.get("资费标准", "")
    traffic = ff.get("通用流量", "")
    if ff.get("定向流量"):
        traffic = (traffic + "+" if traffic else "") + f"定向{ff['定向流量']}"
    if ff.get("国内流量"):
        traffic = traffic or ff["国内流量"]
    voice = ff.get("通话", "") or ff.get("语音", "") or ff.get("国内通话", "")
    sms = ff.get("短信", "") or ff.get("短/彩信", "")
    details = {}
    for k, v in jb.items():
        details[k] = v
    for k, v in ff.items():
        if k not in details:
            details[f"服务内容-{k}"] = v
    if r.get("extra_fees"):
        details["超出资费"] = _clean(r["extra_fees"])
    if r.get("other_fees"):
        details["其他费用"] = _clean(r["other_fees"])
    if r.get("others"):
        details["其他说明"] = _clean(r["others"])
    return _make_item(
        "ctcc",
        category=raw_item.get("_category"),
        subcategory=raw_item.get("_subcategory", ""),
        tariff_id=f"CTCC-{tariff_id}" if tariff_id else "",
        name=name,
        price=price,
        traffic=traffic,
        voice=voice,
        sms=sms,
        validity=jb.get("有效期限", ""),
        eligibility=jb.get("适用范围", ""),
        description=r.get("other_content"),
        details=details,
        source_api=raw_item.get("_source_api", ""),
        collected_at=raw_item.get("_collected_at", ""),
    )


def normalize_cbn(raw_item: dict) -> dict | None:
    """raw_item: queryTariffAllByCond 数据元素。"""
    r = raw_item
    tariff_id = r.get("filingNumber") or r.get("productCode") or r.get("id")
    name = r.get("productName")
    if not name and not tariff_id:
        return None
    price = _fen_to_yuan(r.get("productPrice"))
    if r.get("productPriceUnit"):
        price = f"{price}/{_clean(r['productPriceUnit'])}" if price else ""
    traffic = ""
    if r.get("domesticTraffic") is not None:
        traffic = f"{_clean(r.get('domesticTraffic'))}{_clean(r.get('domesticTrafficUnit') or 'GB')}"
    if r.get("orientTraffic"):
        traffic = (traffic + "+" if traffic else "") + f"定向{_clean(r['orientTraffic'])}{_clean(r.get('orientTrafficUnit') or 'GB')}"
    details = {
        "编号": _clean(r.get("filingNumber")),
        "产品代码": _clean(r.get("productCode")),
        "状态": "在售" if str(r.get("stateFlag") or "") == "1" else str(r.get("stateFlag") or ""),
        "适用地区": _clean(r.get("areaNames")),
        "副卡/亲情网": _clean(r.get("familyNetwork")),
        "套外资费": _clean(r.get("tariffAttr")),
        "在网要求": _clean(r.get("onlineRequirements")),
        "销售渠道": _clean(r.get("saleChannel")),
        "上线日期": _clean(r.get("onlineDay")),
        "下线日期": _clean(r.get("offlineDay")),
        "退订方式": _clean(r.get("unsubscribeMethod")),
        "违约责任": _clean(r.get("responsibility")),
        "互斥规则": _clean(r.get("mutexRule")),
        "到期规则": _clean(r.get("expirationRule")),
        "权利": _clean(r.get("rights")),
        "带宽": _clean(r.get("bandwidth")),
        "服务内容": _clean(r.get("otherContent")),
    }
    details = {k: v for k, v in details.items() if v}
    return _make_item(
        "cbn",
        category=raw_item.get("_category"),
        subcategory=raw_item.get("_subcategory"),
        tariff_id=f"CBN-{tariff_id}" if tariff_id else "",
        name=name,
        price=price,
        traffic=traffic,
        voice=_clean(r.get("domesticCall")),
        sms=_clean(r.get("sms")),
        validity=r.get("validPeriod"),
        eligibility=r.get("applicablePeople"),
        description=r.get("otherContent"),
        details=details,
        source_api=raw_item.get("_source_api", ""),
        collected_at=raw_item.get("_collected_at", ""),
    )


NORMALIZERS = {
    "cmcc": normalize_cmcc,
    "cucc": normalize_cucc,
    "ctcc": normalize_ctcc,
    "cbn": normalize_cbn,
}


def normalize_items(op: str, raw_items: list[dict]) -> list[dict]:
    """raw_items: collect.json 的 items 数组（含 category/subcategory/raw/source_api/collected_at）。"""
    normalizer = NORMALIZERS[op]
    out = []
    seen_ids: set[str] = set()
    for entry in raw_items:
        raw = dict(entry.get("raw") or {})
        raw["_category"] = entry.get("category", "")
        raw["_subcategory"] = entry.get("subcategory", "")
        raw["_source_api"] = entry.get("source_api", "")
        raw["_collected_at"] = entry.get("collected_at", "")
        try:
            item = normalizer(raw)
        except Exception:
            continue
        if not item:
            continue
        tid = item["tariff_id"]
        if tid and tid in seen_ids:
            continue
        if tid:
            seen_ids.add(tid)
        out.append(item)
    return out
