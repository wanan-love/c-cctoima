"""中国联通采集器：级联选择河北 → indexData 发现分类树 → 接口批量采集。

实测（2026-09-05）验证的 API 链（m.client.10010.com/servicequerybusiness/queryTariffNew/）：
- indexData            → provinceList（含河北 provCode）+ levelList（动态分类树）
- threeLevelName       → tariffAttributes=2(本省) + firstLevel + secondLevel + provinceId/cityId → 资费 id 列表
- operateData/{ids}    → 10 个 id 下划线拼接 + page/size/provinceId/cityId → 完整详情（reportNo、detailsList）

流程：进入专区 → 级联选择 河北→石家庄（页面真实交互）→ 验证页面与请求参数
→ 以页面同款参数批量调用官方接口。
"""
from __future__ import annotations

import json
import time
import urllib.parse

from collector.capture import CaptureCollector
from collector.config import HEBEI_EVIDENCE
from collector.human import jitter
from collector.operators.base import BaseOperator, RawTariff, page_fetch_json

API_BASE = "https://m.client.10010.com/servicequerybusiness/queryTariffNew"
COMMON_FORM = "duanlianjieabc=&channelCode=&serviceType=&saleChannel=&externalSources=&contactCode=&version=WT"


class CuccOperator(BaseOperator):
    op = "cucc"
    mobile = False

    def __init__(self, **kw):
        super().__init__(**kw)
        self.collector: CaptureCollector | None = None
        self.prov_code = ""
        self.city_code = ""
        self.prov_name = ""
        self.city_name = ""
        self.behavior_id = ""
        self.level_list: list = []

    def collect_pages(self):
        self.collector = CaptureCollector(self.page, self.op)
        self.collector.attach()

        if not self.navigate():
            return
        jitter(self.page, (5, 8))

        # 1) indexData：省份列表 + 分类树（全部来自页面自己的请求）
        idx = self._wait_json("indexData", contains='"provinceList"')
        if not idx:
            self.outcome.errors.append("indexData 未捕获（页面未正常加载）")
            return
        data = (idx.get("json") or {}).get("data") or {}
        self._discover_province(data)
        self.level_list = data.get("levelList") or []
        if not self.level_list:
            self.outcome.errors.append("levelList 为空（分类发现失败）")
            return
        self.outcome.evidence["level_list"] = self.level_list

        # 2) 页面真实交互：级联选择 河北 → 石家庄
        if not self._select_hebei_ui():
            self.outcome.errors.append("province: 级联选择河北失败")
            return
        # 3) 验证：选择后页面发起的请求带河北参数
        self._verify_hebei_params()
        if self.prov_code != HEBEI_EVIDENCE["cucc"]["prov_code"]:
            self.outcome.errors.append(
                f"province: 交互后 provinceId={self.prov_code!r} ≠ 河北 018"
            )
            return
        # behaviorId 从页面自己的请求中提取（追踪参数，随页面会话）
        self.behavior_id = self._extract_behavior_id()

        # 4) 按页面发现的分类树批量采集（firstLevel × secondLevel）
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        cat_results = []
        for fl in self.level_list:
            fl_name = str(fl.get("firstLevelName") or fl.get("firstLevel") or "")
            fl_code = str(fl.get("firstLevel") or "")
            for sl in fl.get("secondLevels") or []:
                sl_name = str(sl.get("secondLevelName") or sl.get("secondLevel") or "")
                sl_code = str(sl.get("secondLevel") or "")
                if not sl_code:
                    continue
                n_expected, n_got, err = self._collect_category(fl_code, fl_name, sl_code, sl_name, ts)
                cat_results.append(
                    {
                        "category": f"{fl_name}>{sl_name}",
                        "expected": n_expected,
                        "collected": n_got,
                        "error": err,
                    }
                )
                self.log(f"[cucc] {fl_name}>{sl_name}: 期望 {n_expected} / 采集 {n_got}" + (f" ERR={err}" if err else ""))
        self.outcome.evidence["category_results"] = cat_results
        self._collect_evidence_from_items()

    # ── 内部方法 ──
    def _wait_json(self, url_part: str, contains: str = "", tries: int = 8):
        needle = contains.strip('"') if contains else ""
        for _ in range(tries):
            for r in self.collector.json_bodies(url_part):
                j = r.get("json")
                if not j:
                    continue
                if not needle or needle in json.dumps(j, ensure_ascii=False):
                    return r
            self.page.wait_for_timeout(2000)
        return None

    def _discover_province(self, data: dict):
        for p in data.get("provinceList") or []:
            if "河北" in str(p.get("provName") or ""):
                self.prov_code = str(p.get("provCode") or "")
                self.outcome.evidence["hebei_prov_found_in_indexData"] = {
                    "provName": p.get("provName"),
                    "provCode": p.get("provCode"),
                }
                break

    def _select_hebei_ui(self) -> bool:
        """页面级联交互：河北 → 石家庄（省会）。带重试的稳健实现。"""
        self.log("[cucc] 级联选择 河北 → 石家庄 …")
        for attempt in range(1, 4):
            try:
                self.page.evaluate(
                    "() => document.querySelector('.bodybox .el-cascader .el-input__inner')?.click()"
                )
            except Exception as e:
                self.outcome.errors.append(f"cascader open({attempt}): {str(e)[:150]}")
                return False
            jitter(self.page, (1.5, 2.5))
            hebei = self.page.locator(".el-cascader__dropdown:visible .el-cascader-node:has-text('河北')")
            if hebei.count() == 0:
                # 下拉未展开，重试
                jitter(self.page, (1.5, 3.0))
                continue
            hebei.first.click()
            jitter(self.page, (1.8, 3.2))
            city = self.page.locator(".el-cascader__dropdown:visible .el-cascader-node:has-text('石家庄')")
            if city.count() > 0:
                city.first.click()
                self.human_pause((6, 9))
                text = self.page.evaluate("() => document.body.innerText.slice(0, 2000)")
                self.outcome.evidence["after_hebei_page_shows_hebei"] = "（河北）" in text or "河北" in text[:600]
                return True
            # 城市子菜单未出现 → 重新展开重试
            self.log(f"[cucc] 石家庄节点未出现（尝试 {attempt}/3），重新展开级联")
            jitter(self.page, (2.0, 4.0))
            # 点击空白处收起再重开
            try:
                self.page.mouse.click(700, 400)
            except Exception:
                pass
            jitter(self.page, (1.0, 2.0))
        self.outcome.errors.append("cascader: 河北→石家庄 选择失败（3 次尝试）")
        return False

    def _verify_hebei_params(self):
        for r in self.collector.json_bodies("threeLevelName"):
            pd = r.get("post_data") or ""
            if "provinceId=018" in pd or "provinceId=018" in pd:
                self.outcome.evidence["threeLevelName_postdata"] = pd[:300]
                break
        for r in self.collector.responses:
            pd = r.get("post_data") or ""
            if "threeLevelName" in r.get("url", "") and "provinceId=018" in pd:
                params = dict(x.split("=", 1) for x in pd.split("&") if "=" in x)
                self.prov_code = params.get("provinceId", self.prov_code)
                self.city_code = params.get("cityId", "")
                self.outcome.evidence["verified_city_code"] = self.city_code
                break

    def _extract_behavior_id(self) -> str:
        for r in self.collector.responses:
            pd = r.get("post_data") or ""
            if "behaviorId=" in pd:
                for kv in pd.split("&"):
                    if kv.startswith("behaviorId="):
                        return kv.split("=", 1)[1]
        return ""

    def _form(self, extra: dict) -> str:
        parts = COMMON_FORM.split("&")
        if self.behavior_id:
            parts.append(f"behaviorId={self.behavior_id}")
        for k, v in extra.items():
            parts.append(f"{k}={urllib.parse.quote(str(v), safe='')}")
        return "&".join(parts)

    def _collect_category(self, fl_code, fl_name, sl_code, sl_name, ts) -> tuple[int, int, str]:
        # 1) threeLevelName：本省资费(tariffAttributes=2) + 分类 + 河北
        form = self._form(
            {
                "tariffAttributes": HEBEI_EVIDENCE["cucc"]["tariff_attributes"],
                "firstLevel": fl_code,
                "secondLevel": sl_code,
                "provinceId": self.prov_code,
                "cityId": self.city_code,
            }
        )
        resp = page_fetch_json(
            self.page,
            f"{API_BASE}/threeLevelName",
            "POST",
            form,
            "application/x-www-form-urlencoded",
        )
        self.record_api(f"{API_BASE}/threeLevelName [{fl_name}>{sl_name}]")
        if not resp or resp.get("status") != 200 or not resp.get("json"):
            return 0, 0, f"threeLevelName HTTP {resp.get('status') if resp else 'none'}"
        data = (resp["json"].get("data") or {})
        data_list = data.get("dataList") or []
        ids = [str(x.get("id") or "") for x in data_list if x.get("id")]
        expected = len(ids)

        # 2) operateData：按页面同款分页（10 个 id 拼接 URL）
        got = 0
        for page_no, i in enumerate(range(0, len(ids), 10), start=1):
            chunk = ids[i : i + 10]
            if not chunk:
                break
            form2 = self._form(
                {
                    "page": page_no,
                    "size": 10,
                    "provinceId": self.prov_code,
                    "cityId": self.city_code,
                }
            )
            resp2 = page_fetch_json(
                self.page,
                f"{API_BASE}/operateData/{'_'.join(chunk)}",
                "POST",
                form2,
                "application/x-www-form-urlencoded",
            )
            self.record_api(f"{API_BASE}/operateData/{chunk[0][:8]}… [{fl_name}>{sl_name}] p{page_no}")
            if not resp2 or resp2.get("status") != 200 or not resp2.get("json"):
                self.outcome.errors.append(
                    f"operateData {fl_name}>{sl_name} p{page_no}: HTTP {resp2.get('status') if resp2 else 'none'}"
                )
                continue
            d2 = resp2["json"].get("data") or {}
            for item in d2.get("dataList") or []:
                details = item.get("detailsList") or []
                detail = details[0] if details else {}
                self.outcome.items.append(
                    RawTariff(
                        operator=self.op,
                        category=fl_name,
                        subcategory=sl_name,
                        raw={
                            "threeLevelName": next(
                                (x.get("name") for x in data_list if str(x.get("id")) == str(item.get("_srcId", ""))),
                                None,
                            ),
                            "list_item": item,
                            "detail": detail,
                        },
                        source_api=f"{API_BASE}/operateData",
                        collected_at=ts,
                    )
                )
                got += 1
            jitter(self.page, (0.6, 1.8))  # 接口批量节奏
        return expected, got, ""

    def _collect_evidence_from_items(self):
        prefixes = set()
        names_hebei = 0
        for it in self.outcome.items:
            item = it.raw.get("list_item") or {}
            rn = str(item.get("reportNo") or "")
            if len(rn) >= 4:
                prefixes.add(rn[:2])
            name = str(item.get("name") or "")
            if "河北" in name:
                names_hebei += 1
        self.outcome.evidence["report_prefixes"] = sorted(prefixes)
        self.outcome.evidence["names_with_hebei"] = names_hebei
        self.outcome.evidence["province_ok"] = self.prov_code == "018"
        self.outcome.evidence["scope"] = "本省资费(tariffAttributes=2) × 河北(provinceId=018)"
