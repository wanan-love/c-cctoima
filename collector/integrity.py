"""完整性校验：河北正确 / 个人范围 / 分类覆盖 / 总量一致 / 详情完整 / 无重复 / 无骤降。

输出 integrity-report.json。任一 FAILED 项 → 整体 FAIL →
禁止更新 latest.json、禁止更新 IMA（保留上次成功版本）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from collector.config import INTEGRITY, data_dir_for


def check_integrity(op: str, outcome: dict, normalized: list[dict], previous: list[dict] | None) -> dict:
    """outcome = collect.json 的元信息（evidence/categories/errors）。"""
    checks = []

    def add(name: str, status: str, detail: str = "", **extra):
        checks.append({"check": name, "status": status, "detail": detail, **extra})

    ev = outcome.get("evidence") or {}

    # 1. 河北选择正确
    prov_ok = bool(ev.get("province_ok"))
    add("province_hebei", "PASS" if prov_ok else "FAIL",
        f"province_ok={prov_ok}; evidence={json.dumps({k: v for k, v in ev.items() if 'prov' in k or 'area' in k or 'verified' in k}, ensure_ascii=False)[:300]}")

    # 2. 个人用户范围
    audience_ok = True
    if op == "cbn":
        audience_ok = "公众" in str((ev.get("personal_type") or {}).get("typeName") or "")
    elif op == "cmcc":
        audience_ok = bool(ev.get("personal", {}).get("url_is_personal_zone")) and bool(
            ev.get("audience_type1_ok", True)
        )
    add("audience_personal", "PASS" if audience_ok else "FAIL",
        f"audience evidence: {json.dumps({k: v for k, v in ev.items() if 'audience' in k or 'personal' in k or 'type1' in k}, ensure_ascii=False)[:300]}")

    # 3. 所有分类已发现并采集
    cat_results = ev.get("category_results") or ev.get("type_results") or []
    def _got(c):
        g = c.get("items")
        if g is None:
            g = c.get("collected") if c.get("collected") is not None else c.get("beans")
        return g or 0

    cats_with_data = [c for c in cat_results if _got(c) > 0 or (c.get("total") or c.get("expected")) == 0]
    cats_ok = len(cat_results) > 0 and len(cats_with_data) == len(cat_results)
    empty_cats = [
        f"{c.get('category') or c.get('label')}"
        for c in cat_results
        if (c.get("error") or c.get("note")) and (c.get("collected") or 0) == 0
    ]
    add("categories_covered", "PASS" if cats_ok else "FAIL",
        f"发现 {len(cat_results)} 个分类，其中有效 {len(cats_with_data)}；异常: {empty_cats[:5]}")

    # 4. 官方 total 与实际数量一致
    total_mismatches = []
    for c in cat_results:
        expected = c.get("total") if c.get("total") is not None else c.get("expected")
        # 口径：cmcc 用方案数 items（与官方 page.total 同口径）；其他用 collected/beans
        got = c.get("items")
        if got is None:
            got = c.get("collected") if c.get("collected") is not None else c.get("beans")
        if expected is not None and got is not None:
            if c.get("error") or c.get("note"):
                continue  # 已在别处记录
            if got < expected:
                total_mismatches.append(f"{c.get('category') or c.get('label')}: {got}/{expected}")
    totals_ok = not total_mismatches
    add("official_totals", "PASS" if totals_ok else "FAIL",
        "; ".join(total_mismatches[:5]) or f"全部分类数量与官方声明一致（{len(cat_results)} 分类）")

    # 5. 详情采集完整（关键字段非空率）
    n = len(normalized)
    with_name = sum(1 for x in normalized if x.get("name"))
    with_price = sum(1 for x in normalized if x.get("price"))
    with_details = sum(1 for x in normalized if x.get("details"))
    coverage = (with_details / n) if n else 0
    detail_ok = n > 0 and with_name == n and coverage >= INTEGRITY["detail_coverage_min"]
    add("detail_completeness", "PASS" if detail_ok else "FAIL",
        f"{n} 条；name 覆盖 {with_name}/{n}，details 覆盖 {with_details}/{n}（阈值 {INTEGRITY['detail_coverage_min']}）")

    # 6. 无明显重复：权威判据 = tariff_id（方案编号）唯一（normalize 已强制去重，
    #    此处防回归）。同分类同名但不同方案编号属官方数据正常形态
    #    （实测：'60元档分期包' 存在 24HE200377/24HE200489 等多个独立方案），
    #    仅作信息项不计失败。
    from collections import Counter
    id_counts = Counter(x.get("tariff_id") for x in normalized if x.get("tariff_id"))
    dup_ids = sum(1 for c in id_counts.values() if c > 1)
    cat_name_counts = Counter((x.get("category"), x.get("name")) for x in normalized if x.get("name"))
    dup_names = sum(1 for c in cat_name_counts.values() if c > 1)
    dup_ok = dup_ids <= INTEGRITY["duplicate_max"]
    add(
        "no_duplicates",
        "PASS" if dup_ok else "FAIL",
        f"重复 tariff_id {dup_ids} 个（权威判据）；同分类同名不同编号 {dup_names} 组"
        "（官方数据的同名独立方案，信息项）",
    )

    # 7. 无严重 API/页面错误
    api_errors = ev.get("api_errors") or []
    errors = outcome.get("errors") or []
    err_ok = len(errors) <= INTEGRITY["api_error_max"]
    add("no_severe_errors", "PASS" if err_ok else "FAIL",
        f"采集错误 {len(errors)} 条: {'; '.join(errors[:3])[:200]}" if errors else "无采集错误")

    # 8. 无异常数量下降（相对上次成功版本）
    drop_note = ""
    if previous is not None:
        prev_n = len(previous)
        if prev_n > 0:
            drop = prev_n - n
            ratio = drop / prev_n
            suspicious = drop > INTEGRITY["drop_abs_threshold"] and ratio > INTEGRITY["drop_ratio_threshold"]
            # 官方 total 与实际一致时豁免（证明是官方正常变化）
            total_verified = totals_ok and n > 0 and cats_ok and not total_mismatches
            drop_ok = (not suspicious) or total_verified
            drop_note = f"上次 {prev_n} 条 → 本次 {n} 条（下降 {drop} 条 / {ratio:.0%}）"
            if suspicious and total_verified:
                drop_note += "；数量下降但本次官方 total 校验一致，判定为官方正常下线"
            add("no_abnormal_drop", "PASS" if drop_ok else "FAIL", drop_note)
        else:
            add("no_abnormal_drop", "PASS", "上次成功版本为 0 条（首采）")
    else:
        add("no_abnormal_drop", "PASS", "首次采集，无历史基线")

    failed = [c for c in checks if c["status"] == "FAIL"]
    overall = "FAIL" if failed else ("PASS" if n > 0 else "FAIL")

    report = {
        "operator": op,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "overall": overall,
        "item_count": n,
        "previous_count": len(previous) if previous is not None else None,
        "checks": checks,
        "category_results": cat_results,
        "scope": ev.get("scope", ""),
        "evidence_summary": {
            k: ev.get(k) for k in (
                "prov_entry_text", "api_province_codes", "report_prefixes",
                "hebei_prov_found_in_indexData", "verified_city_code", "hebei_area",
                "personal_type", "type_options", "level_list",
            ) if k in ev
        },
        "failure_reasons": [f"{c['check']}: {c['detail']}" for c in failed],
    }
    return report


def save_report(op: str, report: dict) -> Path:
    d = data_dir_for(op)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "integrity-report.json"
    p.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return p
