"""c-cctoima 命令行入口。

用法：
  python -m collector collect --operator cmcc [--dry-run]   # 单运营商采集（含校验/标准化/diff）
  python -m collector collect --operator all                # 全部（本地串行；CI 用 matrix 并发）
  python -m collector sync [--dry-run] [--operators cmcc,cucc]  # 汇总：promote + IMA 同步 + 摘要
  python -m collector summary                                # 仅生成运行摘要
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from collector.config import OPERATORS, data_dir_for, ensure_dirs
from collector.diff import diff_tariffs, save_diff
from collector.integrity import check_integrity, save_report
from collector.normalize import normalize_items
from collector.operators.base import BaseOperator
from collector.storage import load_latest, promote_to_latest, read_json, save_normalized

OPERATOR_CLASSES: dict[str, type] = {}


def _load_operator_classes():
    global OPERATOR_CLASSES
    if OPERATOR_CLASSES:
        return
    from collector.operators.cbn import CbnOperator
    from collector.operators.cmcc import CmccOperator
    from collector.operators.ctcc import CtccOperator
    from collector.operators.cucc import CuccOperator

    OPERATOR_CLASSES = {
        "cmcc": CmccOperator,
        "cucc": CuccOperator,
        "ctcc": CtccOperator,
        "cbn": CbnOperator,
    }


def cmd_collect(operators: list[str], dry_run: bool) -> int:
    _load_operator_classes()
    exit_code = 0
    for op in operators:
        if op not in OPERATOR_CLASSES:
            print(f"未知运营商: {op}")
            return 2
        print(f"\n===== 采集 {op} =====")
        ensure_dirs(op)
        # 失败自动重试（每次全新实例，避免上次错误/数据残留）
        last_outcome = None
        attempt_results = []
        for attempt in range(1, 3):
            collector: BaseOperator = OPERATOR_CLASSES[op](dry_run=dry_run)
            outcome = collector.collect()
            last_outcome = outcome
            attempt_results.append(
                {"attempt": attempt, "ok": outcome.ok, "items": len(outcome.items), "errors": outcome.errors[:5]}
            )
            if outcome.ok or attempt == 2:
                break
            print(f"[{op}] 第 {attempt} 次尝试失败，冷却后重试…")
            time.sleep(30)
        outcome = last_outcome
        raw_path = collector.save_raw()
        outcome.evidence.setdefault("attempts", attempt_results)

        # 标准化
        collect_data = read_json(raw_path) or {}
        raw_items = collect_data.get("items") or []
        normalized = normalize_items(op, raw_items)
        save_normalized(op, normalized, meta={"ok": outcome.ok, "duration_s": outcome.duration_s})
        print(f"[{op}] 标准化: {len(normalized)} 条")

        # 完整性校验（与上次成功版本比较）
        previous = load_latest(op)
        report = check_integrity(op, collect_data, normalized, previous)
        save_report(op, report)
        print(f"[{op}] 完整性: {report['overall']}（{report['item_count']} 条，失败项 {len(report['failure_reasons'])}）")
        for r in report["failure_reasons"]:
            print(f"    ✗ {r[:150]}")

        # diff（与上次成功版本）
        diff = diff_tariffs(normalized, previous)
        save_diff(op, diff)
        print(f"[{op}] diff: {diff['status']} +{diff['added']['count']} -{diff['removed']['count']} ~{diff['modified']['count']}")

        if not outcome.ok or report["overall"] != "PASS":
            exit_code = 1
    return exit_code


def cmd_sync(operators: list[str], dry_run: bool, promote: bool = True) -> int:
    """汇总任务：promote latest + IMA 增量同步 + 摘要。"""
    from ima.sync import ImaSyncManager

    summary = {"operators": {}, "dry_run": dry_run, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    any_pass = False
    any_fail = False
    for op in operators:
        d = data_dir_for(op)
        normalized_path = d / "normalized.json"
        report_path = d / "integrity-report.json"
        if not normalized_path.exists() or not report_path.exists():
            summary["operators"][op] = {"status": "MISSING", "reason": "采集产物缺失"}
            any_fail = True
            continue
        normalized_data = read_json(normalized_path) or {}
        normalized = normalized_data.get("items") or []
        report = read_json(report_path) or {}
        previous = load_latest(op)
        diff = diff_tariffs(normalized, previous)
        save_diff(op, diff)

        entry = {
            "status": "PASS" if report.get("overall") == "PASS" else "FAIL",
            "count": len(normalized),
            "previous_count": len(previous) if previous is not None else None,
            "added": diff["added"]["count"],
            "removed": diff["removed"]["count"],
            "modified": diff["modified"]["count"],
            "diff_status": diff["status"],
            "failure_reasons": report.get("failure_reasons") or [],
        }
        if entry["status"] == "PASS":
            any_pass = True
            if promote and not dry_run:
                promote_to_latest(op)
                entry["latest_promoted"] = True
        else:
            any_fail = True
            entry["latest_promoted"] = False
            entry["reason"] = "完整性校验未通过，保留上次成功版本"
        summary["operators"][op] = entry

    # IMA 同步（仅完整性 PASS 且有变化的运营商）
    sync_results = {}
    try:
        manager = ImaSyncManager(dry_run=dry_run)
        sync_results = manager.sync(operators, summary)
    except Exception as e:
        sync_results = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
        any_fail = True
    summary["ima"] = sync_results

    # 总体状态
    if any_pass and not any_fail:
        summary["overall"] = "SUCCESS"
    elif any_pass and any_fail:
        summary["overall"] = "PARTIAL_SUCCESS"
    else:
        summary["overall"] = "FAILED"

    out = Path("run-summary.json")
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0 if summary["overall"] in ("SUCCESS", "PARTIAL_SUCCESS") else 1


def cmd_summary(operators: list[str]) -> int:
    summary = {"operators": {}}
    for op in operators:
        d = data_dir_for(op)
        report = read_json(d / "integrity-report.json") or {}
        diff = read_json(d / "diff.json") or {}
        summary["operators"][op] = {
            "overall": report.get("overall"),
            "count": report.get("item_count"),
            "added": (diff.get("added") or {}).get("count"),
            "removed": (diff.get("removed") or {}).get("count"),
            "modified": (diff.get("modified") or {}).get("count"),
        }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


def main():
    parser = argparse.ArgumentParser(prog="collector")
    sub = parser.add_subparsers(dest="cmd")

    p_collect = sub.add_parser("collect")
    p_collect.add_argument("--operator", default="all", help="cmcc/cucc/ctcc/cbn/all 或逗号分隔")
    p_collect.add_argument("--dry-run", action="store_true")

    p_sync = sub.add_parser("sync")
    p_sync.add_argument("--dry-run", action="store_true")
    p_sync.add_argument("--operators", default="all")
    p_sync.add_argument("--no-promote", action="store_true")

    p_summary = sub.add_parser("summary")
    p_summary.add_argument("--operators", default="all")

    args = parser.parse_args()
    if args.cmd == "collect":
        ops = OPERATORS if args.operator == "all" else [x.strip() for x in args.operator.split(",") if x.strip()]
        sys.exit(cmd_collect(ops, args.dry_run))
    elif args.cmd == "sync":
        ops = OPERATORS if args.operators == "all" else [x.strip() for x in args.operators.split(",") if x.strip()]
        sys.exit(cmd_sync(ops, args.dry_run, promote=not args.no_promote))
    elif args.cmd == "summary":
        ops = OPERATORS if args.operators == "all" else [x.strip() for x in args.operators.split(",") if x.strip()]
        sys.exit(cmd_summary(ops))
    else:
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
