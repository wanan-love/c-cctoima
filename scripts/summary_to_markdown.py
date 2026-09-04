#!/usr/bin/env python3
"""run-summary.json → GitHub Actions Job Summary Markdown。"""
import json
import pathlib

p = pathlib.Path("run-summary.json")
if not p.exists():
    print("（无 run-summary.json）")
    raise SystemExit(0)

s = json.loads(p.read_text(encoding="utf-8"))
OPS_NAMES = {"cmcc": "移动", "cucc": "联通", "ctcc": "电信", "cbn": "广电"}

print("# 河北资费采集运行摘要\n")
mode = "🧪 DRY_RUN" if s.get("dry_run") else "🔴 实际运行"
print(f"- 模式：{mode}")
print(f"- 总体状态：**{s.get('overall', '?')}**\n")

print("| 运营商 | 状态 | 采集数 | 上次 | 新增 | 修改 | 删除 | IMA | 失败原因 |")
print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
for op, e in (s.get("operators") or {}).items():
    name = OPS_NAMES.get(op, op)
    status = e.get("status", "?")
    reasons = "; ".join(e.get("failure_reasons") or []) or e.get("reason", "") or "-"
    reasons = reasons.replace("|", "/")[:100]
    print(
        f"| {name} | {status} | {e.get('count', '-')} | {e.get('previous_count', '-')} "
        f"| +{e.get('added', '-')} | ~{e.get('modified', '-')} | -{e.get('removed', '-')} "
        f"| {e.get('diff_status', '-')} | {reasons} |"
    )

ima = s.get("ima") or {}
print("\n## IMA 同步\n")
if "error" in ima:
    print(f"❌ {ima['error']}")
else:
    for op, r in (ima.get("operators") or {}).items():
        name = OPS_NAMES.get(op, op)
        sync = r.get("sync", "?")
        files = r.get("uploaded") or []
        detail = ", ".join(f.get("file", "?") for f in files[:5]) or r.get("reason", "") or "-"
        print(f"- **{name}**：{sync}（{detail}）")
print()
