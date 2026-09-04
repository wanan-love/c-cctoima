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
        """选择河北：点击省份页签两次（第一次切到省份视图，第二次打开地区弹层级联），
        然后在 van-cascader 中选择河北省。

        交互逻辑（前端 439 chunk 逆向验证）：
        exChange('prov') 在已是 prov 视图时再次点击 → areaSHow=true 打开
        van-popup(position=bottom)+van-cascader("请选择所在地区")；
        选中省份 → handleCascaderChange → mgmtProv=areaCode、exChange('prov','new')
        → queryTariffCondition(applicableArea=<河北>)。
        """
        # 定位省份页签（文本为「<当前省份>资费」，如 北京资费）
        tab_found = self.page.evaluate(
            """() => {
              const divs = [...document.querySelectorAll('.filterBox .top div')]
              const tab = divs.find(e => /资费\\s*$/.test((e.innerText || '').trim()) && (e.innerText || '').includes('资费'))
              return tab ? (tab.innerText || '').trim() : null
            }"""
        )
        self.log(f"[cbn] 省份页签: {tab_found!r}")
        if not tab_found:
            self.outcome.errors.append("province: 未找到省份页签")
            return False
        tab = self.page.locator(".filterBox .top div", has_text="资费").last
        # 第一次点击：切换到省份视图
        try:
            tab.click(force=True)
        except Exception as e:
            self.outcome.errors.append(f"province: 页签点击失败 {str(e)[:120]}")
            return False
        self.human_pause((2.5, 4.5))
        # 第二次点击：打开地区选择弹层
        try:
            tab.click(force=True)
        except Exception as e:
            self.outcome.errors.append(f"province: 二次点击失败 {str(e)[:120]}")
            return False
        self.human_pause((1.5, 3.0))
        # van-popup + van-cascader 中找 河北省
        clicked = self.page.evaluate(
            """() => {
              // Vant 弹层内的级联选项（单级：省份列表）
              const cands = [...document.querySelectorAll('body *')].filter(e => {
                const t = (e.innerText || '').trim()
                return (t === '河北省' || t === '河北') && e.children.length === 0
              })
              const vis = cands.filter(e => {
                const r = e.getBoundingClientRect()
                return r.width > 0 && r.height > 0
              })
              const target = vis[0] || cands[0]
              if (target) { target.click(); return 'clicked:' + String(target.className).slice(0, 40) }
              return 'nf'
            }"""
        )
        self.log(f"[cbn] 级联选择河北: {clicked}")
        if not str(clicked).startswith("clicked"):
            self.outcome.errors.append("province: 地区弹层未出现或未找到河北")
            return False
        self.human_pause((4, 7))
        # 验证：页签文本变为 河北资费
        tab_text = self.page.evaluate(
            """() => {
              const divs = [...document.querySelectorAll('.filterBox .top div')]
              const tab = divs.find(e => (e.innerText || '').includes('资费'))
              return tab ? (tab.innerText || '').trim() : ''
            }"""
        )
        self.outcome.evidence["province_tab_after_select"] = tab_text
        text = self.page.evaluate("() => document.body.innerText.slice(0, 300)")
        self.outcome.evidence["after_hebei_page_head"] = text[:200]
        return "河北" in (tab_text or "") or "河北" in text[:300]

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
        """采集一个 (type2, type3) 组合。

        - queryTariffAllByCond(stateFlag="")：全状态（在售+停售），页面筛选面板
          自身提供"全部"状态选项；每条记录带 stateFlag 字段（"1"=在售）
        - queryTariffNames：资费名列表（不过滤状态，含重名）→ 唯一名数作为
          完整性软校验基准（唯一名数 < 采集数即异常）
        - status=704 表示该组合无数据（非错误）
        """
        # 全字段数据（全状态）
        resp = self._api_post(
            "goods/queryTariffAllByCond",
            {
                "type1": t1,
                "type2": t2,
                "type3": t3,
                "productName": "",
                "stateFlag": "",
                "minPrice": "",
                "maxPrice": "",
                "applicableArea": self.area_code,
            },
        )
        if not resp or not resp.get("json"):
            return 0, 0, f"HTTP {resp.get('status') if resp else 'none'}"
        status = str((resp["json"].get("status") or ""))
        if status == "704":
            return 0, 0, ""  # 该组合无数据（官方空态）
        if status != "000000":
            return 0, 0, f"status={status}"
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
        # 完整性基准：queryTariffNames（全状态）与 AllByCond(stateFlag="") 数量
        # 严格一致（实测验证：含同名重复的分布亦一致）→ expected = len(names)
        names_resp = self._api_post(
            "goods/queryTariffNames",
            {"type1": t1, "type2": t2, "type3": t3, "applicableArea": self.area_code},
        )
        expected = 0
        if names_resp and names_resp.get("json"):
            expected = len(names_resp.get("json").get("data") or [])
        if expected and len(items) != expected:
            return expected, len(items), f"采集 {len(items)} ≠ 官方名数 {expected}"
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
