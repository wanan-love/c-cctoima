"""中国移动采集器：加密信封 + 懒加载滚动 + 应用层明文捕获。

技术要点（源自 CMCC-HE 生产方案并经 2026-09-05 实测验证）：
- 资费接口走 isWX 加密通道（网络层只见 {"body":"<密文>"} 信封），
  明文在 axios 响应拦截器解密后 JSON.parse 才出现 → hook JSON.parse 捕获；
- getTariffListInfo 明文 data={beans, page:{total,pageNumber,pageSize}}；
  bean=资费系列，bean.nonModuleList=该系列全部方案（reportNo 级），
  page.total 统计的是 bean 数 → 完整性基准用 bean 数；
- 页面交互：.prov-entry 验证河北省 → 河北资费 range-tab → 资费类型
  下拉动态枚举（.select-item）→ 滚动懒加载（pageSize=5/页）。
"""
from __future__ import annotations

import json
import threading
import time

from collector.capture import CAPTURE_HOOK, CaptureCollector
from collector.config import HEBEI_EVIDENCE
from collector.human import jitter, mouse_drift, scroll_like_human
from collector.operators.base import BaseOperator, CollectOutcome, RawTariff


def _classify_capture(cap: dict) -> dict:
    """把一条 JSON.parse 明文捕获分类为 list / standard / other。"""
    parsed = cap.get("parsed") or {}
    url = str(cap.get("url") or "")
    for body in (parsed.get("rspBody"), parsed.get("data"), parsed):
        if body is None:
            continue
        if isinstance(body, dict):
            d = body.get("data") if isinstance(body.get("data"), dict) else body
            if isinstance(d, dict) and (d.get("page") or isinstance(d.get("beans"), list)):
                return {
                    "kind": "list",
                    "endpoint": "getTariffListInfo" if "getTariffListInfo" in url else "list(other)",
                    "total": int((d.get("page") or {}).get("total") or 0),
                    "pageNumber": int((d.get("page") or {}).get("pageNumber") or 0),
                    "beans": d.get("beans") or [],
                }
        # 标准资费表格组
        groups = None
        if isinstance(body, list):
            groups = body
        elif isinstance(body, dict) and isinstance(body.get("tariffList"), list):
            groups = body["tariffList"]
        if groups and isinstance(groups, list) and groups and isinstance(groups[0], dict) and groups[0].get("tariffTable"):
            tables = []
            for g in groups:
                tt = g.get("tariffTable") or {}
                if isinstance(tt.get("tHead"), list) and isinstance(tt.get("tBody"), list):
                    tables.append(
                        {
                            "title": g.get("tableTitle") or g.get("tariffName") or g.get("title"),
                            "tHead": tt["tHead"],
                            "tBody": tt["tBody"],
                        }
                    )
            if tables:
                return {"kind": "standard", "endpoint": "getStandardlist", "tables": tables}
    return {"kind": "other", "endpoint": url or "unknown"}


class CmccOperator(BaseOperator):
    op = "cmcc"
    mobile = False

    def __init__(self, **kw):
        super().__init__(**kw)
        self.collector: CaptureCollector | None = None
        self.current_type: str | None = None
        self.list_by_type: dict[str, dict] = {}   # label -> {total, beans:{seqno:bean}, maxPage}
        self.standard_tables: list | None = None
        self._pull_thread: threading.Thread | None = None
        self._stop_pull = threading.Event()

    # ── 捕获拉取 ──
    def _pull(self):
        try:
            caps = self.page.evaluate(
                "() => { const a = window.__apiCapture || []; const out = a.slice(); a.length = 0; return out }"
            ) or []
            self._ingest(caps)
        except Exception:
            pass

    def _start_puller(self):
        # Playwright sync API 不允许跨线程调用 → 改为同步拉取点
        # （交互间隙 + 滚动每轮 via on_round 回调）
        pass

    def _stop_puller(self):
        self._pull()

    def _ingest(self, caps: list):
        for cap in caps:
            info = _classify_capture(cap)
            if info["kind"] == "list":
                label = self.current_type or "（初始加载）"
                entry = self.list_by_type.setdefault(label, {"total": 0, "beans": {}, "maxPage": 0})
                if info["total"] > entry["total"]:
                    entry["total"] = info["total"]
                if info["pageNumber"] > entry["maxPage"]:
                    entry["maxPage"] = info["pageNumber"]
                for b in info.get("beans") or []:
                    key = str(b.get("tariffSeqno") or b.get("tariffName") or json.dumps(b, ensure_ascii=False)[:80])
                    entry["beans"][key] = b
            elif info["kind"] == "standard":
                self.standard_tables = info["tables"]

    # ── 页面工具 ──
    def wait_idle(self):
        for _ in range(15):
            loading = self.page.evaluate(
                "() => document.body.innerText.includes('努力加载中') ? '1' : '0'"
            )
            if loading == "0":
                break
            jitter(self.page, (1.5, 3.0))

    def count_cards(self) -> int:
        return self.page.evaluate("() => document.querySelectorAll('.tariff-item-container').length")

    def verify_hebei(self) -> bool:
        text = self.page.evaluate(
            "() => { const e = document.querySelector('.prov-entry'); return e ? (e.innerText || '').trim() : '' }"
        )
        self.outcome.evidence["prov_entry_text"] = text
        return HEBEI_EVIDENCE["cmcc"]["prov_entry_text"] in (text or "")

    def select_hebei_via_ui(self):
        """按页面自身交互流程选择河北省（不信任 URL 参数）。"""
        if self.verify_hebei():
            self.log("[cmcc] 页面省份入口已是河北省")
            return True
        self.log("[cmcc] 通过省份选择器切换河北…")
        self.page.evaluate("() => document.querySelector('.prov-entry')?.click()")
        jitter(self.page, (1.5, 3.0))
        clicked = self.page.evaluate(
            """() => {
              const items = [...document.querySelectorAll('*')].filter(
                e => e.children.length === 0 && (e.innerText || '').trim() === '河北省'
              )
              items.forEach(e => e.click())
              return items.length
            }"""
        )
        self.human_pause((6, 10))
        self.wait_idle()
        self._pull()
        ok = self.verify_hebei()
        self.outcome.evidence["hebei_selected_via_ui"] = bool(clicked)
        return ok

    def verify_personal(self) -> bool:
        checks = {}
        checks["url_is_personal_zone"] = "tariffZonePers" in self.page.url
        checks["page_shows_personal_tab"] = self.page.evaluate(
            "() => document.body.innerText.includes('个人资费')"
        )
        self.outcome.evidence["personal"] = checks
        return all(checks.values())

    def click_hebei_range_tab(self):
        tabs = self.page.evaluate(
            """() => {
              const tabs = [...document.querySelectorAll('.range-tab')]
              const hebei = tabs.find(e => (e.innerText || '').includes('河北'))
              if (hebei) hebei.click()
              return tabs.map(e => (e.innerText || '').trim())
            }"""
        )
        self.outcome.evidence["range_tabs"] = tabs
        self.human_pause((8, 12))
        self.wait_idle()
        self._pull()

    # ── 类型下拉（动态枚举，绝不硬编码） ──
    TYPE_SELECT_JS = """([mode, label]) => {
      const root = (() => {
        const labels = [...document.querySelectorAll('.select-label')]
        const lb = labels.find(el => (el.innerText || '').trim().replace(/[:：]/, '') === '资费类型')
        let r = null
        if (lb) {
          const box = lb.parentElement?.querySelector('.select-box')
          r = box ? (box.closest('.select-container') || lb.parentElement) : lb.parentElement
        }
        if (!r) {
          const sb = document.querySelector('.select-box')
          r = sb ? (sb.closest('.select-container') || sb.parentElement) : null
        }
        return r
      })()
      if (!root) return null
      if (mode === 'options') {
        return [...root.querySelectorAll('.select-item')]
          .filter(e => e.offsetParent !== null)
          .map(e => (e.innerText || '').trim())
          .filter(Boolean)
      }
      if (mode === 'selected') {
        const box = root.querySelector('.select-box')
        return box ? (box.innerText || '').trim() : null
      }
      if (mode === 'open') {
        const visible = [...root.querySelectorAll('.select-item')].filter(e => e.offsetParent !== null).length
        if (visible > 0) return 'already-open'
        const box = root.querySelector('.select-box') || root.querySelector('.select-label')
        if (box) { box.click(); return 'opened' }
        return 'nf'
      }
      if (mode === 'click') {
        const items = [...root.querySelectorAll('.select-item')].filter(e => e.offsetParent !== null)
        const hit = items.find(e => (e.innerText || '').trim() === label)
        if (hit) { hit.click(); return 'ok' }
        return 'nf'
      }
      if (mode === 'close') {
        const box = root.querySelector('.select-box')
        if (box) { box.click(); return 'ok' }
        return 'nf'
      }
      return null
    }"""

    def open_type_dropdown(self) -> str:
        res = self.page.evaluate(self.TYPE_SELECT_JS, ["open"])
        jitter(self.page, (1.2, 2.5))
        return res or "nf"

    def list_type_options(self) -> list[str]:
        return self.page.evaluate(self.TYPE_SELECT_JS, ["options"]) or []

    def close_type_dropdown(self):
        opened = self.page.evaluate(self.TYPE_SELECT_JS, ["options"]) or []
        if opened:
            self.page.evaluate(self.TYPE_SELECT_JS, ["close"])
            jitter(self.page, (0.8, 1.6))

    def select_type(self, label: str) -> str:
        for attempt in (1, 2):
            self.open_type_dropdown()
            res = self.page.evaluate(self.TYPE_SELECT_JS, ["click", label])
            if res == "ok":
                self.current_type = label  # 点击即切换归属
                self.human_pause((8, 14))
                self._pull()
                shown = self.page.evaluate(self.TYPE_SELECT_JS, ["selected"])
                if not shown or label in (shown or ""):
                    return "ok"
                self.log(f"[cmcc] 选中验证失败（显示「{shown}」≠「{label}」），重试")
            else:
                return "nf"
        return "verify-failed"

    # ── 主流程 ──
    def collect_pages(self):
        ctx = self.page.context
        ctx.add_init_script(CAPTURE_HOOK)
        self.collector = CaptureCollector(self.page, self.op)
        self.collector.attach()
        self._start_puller()

        if not self.navigate():
            return
        self.human_pause((8, 14))
        self.wait_idle()
        self._pull()

        # 1) 河北：必须通过页面省份入口确认/选择
        if not self.select_hebei_via_ui():
            self.outcome.errors.append("province: 无法确认河北省选中（prov-entry 文本不匹配）")
            return
        # 2) 个人：tariffZonePers 专区 + 页签证据
        if not self.verify_personal():
            self.outcome.errors.append("audience: 未能确认个人资费专区")
            return
        # 3) 河北资费（分省）页签：先固定类型归属（页签切换会用当前类型重拉列表）
        shown_type = self.page.evaluate(self.TYPE_SELECT_JS, ["selected"])
        self.current_type = shown_type or "套餐"
        self.click_hebei_range_tab()
        # 归属校正（页签切换后选中类型可能显示为其他值）
        shown2 = self.page.evaluate(self.TYPE_SELECT_JS, ["selected"])
        if shown2 and shown2 != shown_type:
            self.log(f"[cmcc] 页签切换后选中类型为「{shown2}」，归属已跟随校正")
            self.current_type = shown2

        # 4) 枚举资费类型下拉（页面实际选项）
        options: list[str] = []
        for _ in range(3):
            self.open_type_dropdown()
            options = self.list_type_options()
            if options:
                break
            self.close_type_dropdown()
            self.human_pause((4, 8))
        self.close_type_dropdown()
        if not options:
            self.outcome.errors.append("categories: 资费类型下拉枚举失败（页面结构可能变化）")
            return
        self.log(f"[cmcc] 类型下拉选项（{len(options)} 个）: {' / '.join(options)}")
        self.outcome.evidence["type_options"] = options

        # 5) 逐类型采集（SMOKE 模式只采前 2 类）
        results = []
        done = set()
        import os as _os
        if _os.environ.get("C2I_SMOKE"):
            options = options[:2]
        for label in options:
            if label in done:
                continue
            done.add(label)
            already = (self.page.evaluate(self.TYPE_SELECT_JS, ["selected"]) or "")
            sel = "already" if label in already else self.select_type(label)
            self.wait_idle()
            if sel == "nf":
                self.log(f"[cmcc] {label}: 选项不可用，跳过")
                results.append({"label": label, "beans": 0, "total": None, "note": "选项不可用"})
                continue
            if sel == "verify-failed":
                results.append({"label": label, "beans": 0, "total": None, "note": "选中验证失败"})
                continue

            entry = self.list_by_type.get(label) or {"total": 0, "beans": {}, "maxPage": 0}

            def oracle():
                e = self.list_by_type.get(label)
                return e["total"] if e and e["total"] else None

            def count_beans_dom():
                # DOM 卡片数作为滚动进度信号（卡片=bean）
                return self.count_cards()

            import os as _os
            max_rounds = 3 if _os.environ.get("C2I_SMOKE") else 350
            cards = scroll_like_human(
                self.page, count_beans_dom, oracle, max_rounds=max_rounds, log=self.log,
                on_round=lambda: self._pull(),
            )
            self._pull()
            entry = self.list_by_type.get(label) or entry
            # 0 beans 且接口未声明 0 → 冷却重试一次
            if len(entry["beans"]) == 0 and entry["total"] != 0:
                self.log(f"[cmcc] {label}: 0 beans —— 冷却后重试一次")
                self.human_pause((45, 75))
                self.select_type(label)
                self.wait_idle()
                import os as _os
                mr = 3 if _os.environ.get("C2I_SMOKE") else 350
                scroll_like_human(self.page, count_beans_dom, oracle, max_rounds=mr, log=self.log,
                    on_round=lambda: self._pull())
                self._pull()
                entry = self.list_by_type.get(label) or entry

            # 标准资费兜底（列表 0 时用 getStandardlist 表格）
            used_fallback = False
            if label == "标准资费" and len(entry["beans"]) == 0 and self.standard_tables:
                self._save_standard_items(label)
                used_fallback = True

            if not used_fallback:
                self._save_beans(label, entry)
            results.append(
                {
                    "label": label,
                    "beans": len(entry["beans"]),
                    "total": entry["total"],
                    "note": "表格兜底" if used_fallback else "",
                }
            )
            self.log(f"[cmcc] {label}: beans={len(entry['beans'])} / total={entry['total']}")
            self.outcome.pages_visited = entry.get("maxPage", 0) + self.outcome.pages_visited
            self.human_pause((2, 4))

        self._stop_puller()
        self.outcome.evidence["type_results"] = results
        # 河北/个人证据：来自采集到的记录字段
        self._collect_evidence_from_items()

    def _save_beans(self, label: str, entry: dict):
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        for key, bean in entry["beans"].items():
            for item_key in ("nonModuleList", "moduleList"):
                for it in bean.get(item_key) or []:
                    if not isinstance(it, dict):
                        continue
                    self.outcome.items.append(
                        RawTariff(
                            operator=self.op,
                            category=label,
                            subcategory=str(bean.get("tariffName") or ""),
                            raw={
                                "bean_seqno": bean.get("tariffSeqno"),
                                "bean_name": bean.get("tariffName"),
                                "list_kind": item_key,
                                **it,
                            },
                            source_api="https://h.app.coc.10086.cn/website/nrapigate/nrtariff/new/Tariff/getTariffListInfo",
                            collected_at=ts,
                        )
                    )

    def _save_standard_items(self, label: str):
        """getStandardlist 表格兜底：tHead/tBody → 记录。"""
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        for tbl in self.standard_tables or []:
            heads = [h for h in (tbl.get("tHead") or []) if h]
            keys = [list(h.keys())[0] if isinstance(h, dict) and h else None for h in heads]
            titles = [
                (str(h[keys[i]]) if keys[i] is not None else f"列{i+1}") for i, h in enumerate(heads)
            ]
            for row in tbl.get("tBody") or []:
                if not row:
                    continue
                fields = {}
                for i, t in enumerate(titles):
                    v = row.get(keys[i]) if keys[i] else row.get(f"field{i+1}")
                    v = str(v).strip() if v is not None else ""
                    if v:
                        fields[t] = v
                if not fields:
                    continue
                name = (
                    fields.get("资费名称")
                    or fields.get("业务名称")
                    or fields.get("项目")
                    or fields.get("业务")
                    or fields.get("服务名称")
                    or next(iter(fields.values()), "未命名标准资费")
                )
                self.outcome.items.append(
                    RawTariff(
                        operator=self.op,
                        category=label,
                        subcategory=str(tbl.get("title") or ""),
                        raw={"fields": fields, "standard_table": True},
                        source_api="https://h.app.coc.10086.cn/website/nrapigate/nrtariff/new/Tariff/getStandardlist",
                        collected_at=ts,
                    )
                )

    def _collect_evidence_from_items(self):
        ev = HEBEI_EVIDENCE["cmcc"]
        provinces = set()
        type1s = set()
        report_prefixes = set()
        for it in self.outcome.items:
            r = it.raw
            # province 字段可能是多省适用列表（"100,210,...,311,..."，含 311 即覆盖河北）
            prov = str(r.get("province") or "")
            if prov:
                provinces.update(x for x in prov.split(",") if x)
            if r.get("type1") is not None:
                type1s.add(str(r["type1"]))
            rn = str(r.get("reportNo") or "")
            if len(rn) >= 6:
                # reportNo 格式：YY+上报方+流水（如 26HE201171=2026河北，25JT214609=2025集团）
                report_prefixes.add(rn[2:4])
        hebei_code = ev["api_province"]
        self.outcome.evidence["api_province_codes"] = sorted(provinces)[:40]
        self.outcome.evidence["api_type1_values"] = sorted(type1s)
        self.outcome.evidence["report_prefixes"] = sorted(report_prefixes)
        # 省份校验：带 province 字段的记录必须覆盖河北（311）；
        # 无该字段的记录（标准资费表格兜底等）不判失败，但需 ≥80% 记录带字段
        items_total = len(self.outcome.items)
        with_field = sum(1 for it in self.outcome.items if str(it.raw.get("province") or "").strip())

        def covers_hebei(rec):
            prov = str(rec.get("province") or "")
            return hebei_code in [x for x in prov.split(",") if x]

        self.outcome.evidence["province_field_coverage"] = round(with_field / items_total, 3) if items_total else 0
        self.outcome.evidence["province_ok"] = bool(provinces) and all(
            covers_hebei(it.raw) for it in self.outcome.items if str(it.raw.get("province") or "").strip()
        ) and (items_total == 0 or with_field / items_total >= 0.8)
        # 受众：带 type1 字段的记录必须全部为 1（个人）；无字段不判失败
        self.outcome.evidence["audience_type1_ok"] = type1s == {"1"} or not type1s
