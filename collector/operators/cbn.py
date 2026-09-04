"""中国广电采集器：移动端资费公示 → 区域选择河北 → 公众(个人)类型树批量。

实测（2026-09-05）验证的 API（m.10099.com.cn/contact-web/api/，明文 JSON POST）：
- busi/qryAreaList            → 区域列表（河北 areaCode=HB00）
- goods/queryTariffCondition  → 类型树（type1: 公众GZ/政企ZQ → type2 → type3）
- goods/queryTariffNames      → 资费名列表（分类下产品名）
- goods/queryTariffAllByCond  → 全字段资费数据（type1/type2/type3 + applicableArea）

流程：进入公示页 → 点击省份入口选择河北（页面真实交互）→ 验证 applicableArea=HB00 →
仅取"公众"（=个人）类型树 → 页面内 fetch 批量采集全部分类组合。
"""
from __future__ import annotations

import json
import time

from collector.capture import CaptureCollector
from collector.config import HEBEI_EVIDENCE
from collector.human import jitter
from collector.operators.base import BaseOperator, RawTariff, page_fetch_json

API_BASE = "https://m.10099.com.cn/contact-web/api"
CHANNEL_AREA = "cd_20220516_503441"
CHANNEL_GOODS = "cd_20220914_514144"


class CbnOperator(BaseOperator):
    op = "cbn"
    mobile = True

    def __init__(self, **kw):
        super().__init__(**kw)
        self.collector: CaptureCollector | None = None
        self.area_code = ""
        self.type_tree: list = []
        self.gp_type: dict | None = None   # "公众" type1 节点（= 个人）

    def collect_pages(self):
        self.collector = CaptureCollector(self.page, self.op)
        self.collector.attach()

        if not self.navigate():
            return
        jitter(self.page, (6, 9))

        # 1) 区域列表（页面自己的 qryAreaList 请求）→ 河北区域码
        area_resp = self._wait_json("qryAreaList")
        if not area_resp:
            self.outcome.errors.append("qryAreaList 未捕获")
            return
        areas = ((area_resp.get("json") or {}).get("data") or {}).get("regionalList") or []
        for a in areas:
            if "河北" in str(a.get("areaName") or ""):
                self.area_code = str(a.get("areaCode") or "")
                self.outcome.evidence["hebei_area"] = a
                break
        if not self.area_code:
            self.outcome.errors.append("区域列表中未找到河北")
            return

        # 2) 类型树（queryTariffCondition，页面自己请求）
        cond_resp = self._wait_json("queryTariffCondition")
        if not cond_resp:
            self.outcome.errors.append("queryTariffCondition 未捕获")
            return
        self.type_tree = (cond_resp.get("json") or {}).get("data") or []
        # "公众" = 个人受众节点（运行时按名称发现，不硬编码）
        self.gp_type = next(
            (t for t in self.type_tree if "公众" in str(t.get("typeName") or "")), None
        )
        if not self.gp_type:
            self.outcome.errors.append("类型树中未找到公众(个人)分类")
            return
        self.outcome.evidence["personal_type"] = {
            "typeName": self.gp_type.get("typeName"),
            "typeCode": self.gp_type.get("typeCode"),
        }

        # 3) 页面真实交互：点击省份入口 → 选择河北
        if not self._select_hebei_ui():
            self.outcome.errors.append("province: 河北区域选择交互失败")
            return
        # 4) 验证选择后页面请求的 applicableArea
        self._verify_area_param()
        if self.outcome.evidence.get("verified_area") not in (self.area_code,):
            self.log(f"[cbn] 警告: 未捕获到 applicableArea={self.area_code} 的页面请求，继续但记录证据")

        # 5) 批量采集：公众(GZ) × type2 × type3 × 河北(HB00)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        t1_code = str(self.gp_type.get("typeCode") or "")
        cat_results = []
        for t2 in self.gp_type.get("childTariffTypes") or []:
            t2_code = str(t2.get("typeCode") or "")
            t2_name = str(t2.get("typeName") or "")
            for t3 in t2.get("childTariffTypes") or []:
                t3_code = str(t3.get("typeCode") or "")
                t3_name = str(t3.get("typeName") or "")
                expected, got, err = self._collect_combo(t1_code, t2_code, t3_code, t2_name, t3_name, ts)
                cat_results.append(
                    {"category": t2_name, "subcategory": t3_name, "expected": expected, "collected": got, "error": err}
                )
                self.log(f"[cbn] {t2_name}>{t3_name}: 期望 {expected} / 采集 {got}" + (f" ERR {err}" if err else ""))
                jitter(self.page, (0.8, 2.0))
        self.outcome.evidence["category_results"] = cat_results
        self._collect_evidence_from_items()

    # ── 内部 ──
    def _wait_json(self, url_part: str, tries: int = 8):
        for _ in range(tries):
            for r in self.collector.json_bodies(url_part):
                if r.get("json"):
                    return r
            self.page.wait_for_timeout(2000)
        return None

    def _api_post(self, path: str, body: dict) -> dict:
        body = {"channelId": CHANNEL_GOODS, "timestamp": int(time.time() * 1000), **body}
        resp = page_fetch_json(
            self.page,
            f"{API_BASE}/{path}",
            "POST",
            json.dumps(body, ensure_ascii=False),
            "application/json",
        )
        self.record_api(f"{API_BASE}/{path}")
        return resp

    def _select_hebei_ui(self) -> bool:
        """点击省份入口（北京资费/当前省份）→ 区域选择面板 → 河北。"""
        clicked = self.page.evaluate(
            """() => {
              const cands = [...document.querySelectorAll('body *')].filter(e => {
                const t = (e.innerText || '').trim()
                return t.includes('资费') && t.length < 12 && e.children.length <= 2
              })
              const entry = cands.find(e => /全网|北京|河北|当前/.test(e.innerText))
              if (entry) { entry.click(); return 'clicked:' + (entry.innerText || '').trim() }
              return 'nf'
            }"""
        )
        self.log(f"[cbn] 点击区域入口: {clicked}")
        jitter(self.page, (1.5, 3.0))
        ok = self.page.evaluate(
            """(areaName) => {
              const cands = [...document.querySelectorAll('body *')].filter(e => {
                const t = (e.innerText || '').trim()
                return t === areaName && e.children.length === 0
              })
              const vis = cands.find(e => e.getBoundingClientRect().width > 0) || cands[0]
              if (vis) { vis.click(); return 'clicked' }
              return 'nf'
            }""",
            "河北省",
        )
        if ok != "clicked":
            ok = self.page.evaluate(
                """(areaName) => {
                  const cands = [...document.querySelectorAll('body *')].filter(e => {
                    const t = (e.innerText || '').trim()
                    return t === areaName && e.children.length === 0
                  })
                  const vis = cands.find(e => e.getBoundingClientRect().width > 0) || cands[0]
                  if (vis) { vis.click(); return 'clicked' }
                  return 'nf'
                }""",
                "河北",
            )
        self.log(f"[cbn] 选择河北: {ok}")
        if ok != "clicked":
            return False
        self.human_pause((5, 8))
        text = self.page.evaluate("() => document.body.innerText.slice(0, 300)")
        self.outcome.evidence["after_hebei_page_head"] = text[:200]
        return True

    def _verify_area_param(self):
        for r in self.collector.json_bodies("queryTariff"):
            pd = r.get("post_data") or ""
            if self.area_code and self.area_code in pd:
                try:
                    body = json.loads(pd)
                    if body.get("applicableArea") == self.area_code:
                        self.outcome.evidence["verified_area"] = self.area_code
                        self.outcome.evidence["verified_area_request"] = pd[:200]
                        return
                except Exception:
                    continue

    def _collect_combo(self, t1, t2, t3, t2_name, t3_name, ts):
        # queryTariffNames：该组合的官方资费名列表（完整性基准）
        names_resp = self._api_post(
            "goods/queryTariffNames",
            {"type1": t1, "type2": t2, "type3": t3, "applicableArea": self.area_code},
        )
        names = []
        if names_resp and names_resp.get("json"):
            names = (names_resp["json"].get("data") or [])
        expected = len(names)
        # queryTariffAllByCond：全字段数据
        resp = self._api_post(
            "goods/queryTariffAllByCond",
            {
                "type1": t1,
                "type2": t2,
                "type3": t3,
                "productName": "",
                "stateFlag": "1",
                "minPrice": "",
                "maxPrice": "",
                "applicableArea": self.area_code,
            },
        )
        if not resp or not resp.get("json"):
            return expected, 0, f"HTTP {resp.get('status') if resp else 'none'}"
        status = str((resp["json"].get("status") or ""))
        if status != "000000":
            return expected, 0, f"status={status}"
        items = resp["json"].get("data") or []
        for it in items:
            self.outcome.items.append(
                RawTariff(
                    operator=self.op,
                    category=t2_name,
                    subcategory=t3_name,
                    raw=it,
                    source_api=f"{API_BASE}/goods/queryTariffAllByCond",
                    collected_at=ts,
                )
            )
        return expected, len(items), ""

    def _collect_evidence_from_items(self):
        prefixes = set()
        areas = set()
        for it in self.outcome.items:
            rn = str(it.raw.get("filingNumber") or "")
            if len(rn) >= 4:
                prefixes.add(rn[:2])
            if it.raw.get("applicableArea"):
                areas.add(str(it.raw["applicableArea"]))
            if it.raw.get("areaNames"):
                areas.add(str(it.raw["areaNames"]))
        self.outcome.evidence["filing_prefixes"] = sorted(prefixes)
        self.outcome.evidence["item_areas"] = sorted(areas)[:10]
        self.outcome.evidence["province_ok"] = self.area_code == "HB00"
        self.outcome.evidence["scope"] = f"公众({self.gp_type.get('typeCode') if self.gp_type else ''}) × 河北({self.area_code})"
