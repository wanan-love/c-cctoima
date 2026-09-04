"""中国电信采集器：瑞数 WAF 页面内交互 + 官方接口批量。

实测（2026-09-05）+ 前端逆向验证的 API（www.189.cn，GET，瑞数 WAF 自动注入 URL 令牌）：
- /bss/tariffZone/newTarifZone12List.do?provCode=<code>          → 分类（lable1 + lable2List）
- /bss/tariffZone/newTarifZone3Title.do?provCode&lable1Id&lable2Id → 全量资费列表（无分页，含详情 HTML）
省份表（页面前端配置）：河北 provinceCode=609906 / cityCode=he

流程：进入专区 → 点城市页签选择河北（页面真实交互）→ 验证 →
页面内 XHR 批量调用（WAF 令牌由站点自身 JS 钩子自动注入）。
"""
from __future__ import annotations

import json
import time
import urllib.parse

from collector.capture import CaptureCollector
from collector.config import HEBEI_EVIDENCE
from collector.human import jitter
from collector.operators.base import BaseOperator, RawTariff, page_fetch_json

API_12LIST = "/bss/tariffZone/newTarifZone12List.do"
API_3TITLE = "/bss/tariffZone/newTarifZone3Title.do"

# 页面内 XHR（走站点自身 axios/XHR 通道，瑞数 WAF 的 XHR 钩子自动追加 URL 令牌）
XHR_JS = """
async ([url, timeoutMs]) => {
  return await new Promise(resolve => {
    try {
      const xhr = new XMLHttpRequest()
      xhr.open('GET', url, true)
      xhr.timeout = timeoutMs
      xhr.onload = () => {
        let parsed = null
        try { parsed = JSON.parse(xhr.responseText) } catch (e) {}
        resolve({ status: xhr.status, text: xhr.responseText.slice(0, 900000), json: parsed })
      }
      xhr.onerror = () => resolve({ status: 0, text: 'xhr error', json: null })
      xhr.ontimeout = () => resolve({ status: 0, text: 'xhr timeout', json: null })
      xhr.send()
    } catch (e) {
      resolve({ status: 0, text: String(e), json: null })
    }
  })
}
"""


class CtccOperator(BaseOperator):
    op = "ctcc"
    mobile = False

    def __init__(self, **kw):
        super().__init__(**kw)
        self.collector: CaptureCollector | None = None
        self.prov_code = ""
        self.categories: list = []

    def collect_pages(self):
        self.collector = CaptureCollector(self.page, self.op)
        self.collector.attach()

        if not self._navigate_with_waf():
            return
        jitter(self.page, (6, 10))

        # 1) 页面省份页签交互：北京资费 → 河北
        if not self._select_hebei_ui():
            self.outcome.errors.append("province: 河北选择交互失败")
            return
        self._verify_hebei_params()
        if self.prov_code != HEBEI_EVIDENCE["ctcc"]["prov_code"]:
            self.outcome.errors.append(f"province: 交互后 provCode={self.prov_code!r} ≠ 609906")
            return

        # 2) 分类发现（12List，含 lable2 子分类，全部页面发现）
        resp12 = self._page_xhr(f"{API_12LIST}?provCode={self.prov_code}")
        self.record_api(f"{API_12LIST}?provCode={self.prov_code}")
        if not resp12 or not resp12.get("json") or resp12["json"].get("code") != "0":
            self.outcome.errors.append(f"newTarifZone12List 失败: HTTP {resp12.get('status') if resp12 else 'none'}")
            return
        self.categories = (resp12["json"].get("dataObject") or [])
        if not self.categories:
            self.outcome.errors.append("分类列表为空")
            return
        self.outcome.evidence["categories_from_page"] = [
            {"lable1Name": c.get("lable1Name"), "lable1Id": c.get("lable1Id"),
             "lable2": [str(x.get("lable2Name") or "") for x in (c.get("lable2List") or [])]}
            for c in self.categories
        ]

        # 3) 逐分类全量采集（3Title 一次返回该分类全部资费，无分页）
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        cat_results = []
        for cat in self.categories:
            l1_id = str(cat.get("lable1Id") or "")
            l1_name = str(cat.get("lable1Name") or "")
            if not l1_id:
                continue
            params = {"provCode": self.prov_code, "lable1Id": l1_id}
            url = API_3TITLE + "?" + urllib.parse.urlencode(params)
            resp = self._page_xhr(url)
            self.record_api(f"{API_3TITLE}?provCode={self.prov_code}&lable1Id={l1_id[:24]}")
            if not resp or not resp.get("json") or resp["json"].get("code") != "0":
                self.outcome.errors.append(f"3Title[{l1_name}] HTTP {resp.get('status') if resp else 'none'}")
                cat_results.append({"category": l1_name, "expected": None, "collected": 0, "error": "api"})
                continue
            items = resp["json"].get("dataObject") or []
            for it in items:
                self.outcome.items.append(
                    RawTariff(
                        operator=self.op,
                        category=l1_name,
                        subcategory="",
                        raw=it,
                        source_api=f"https://www.189.cn{API_3TITLE}",
                        collected_at=ts,
                    )
                )
            cat_results.append({"category": l1_name, "expected": len(items), "collected": len(items), "error": ""})
            self.log(f"[ctcc] {l1_name}: {len(items)} 条")
            jitter(self.page, (1.0, 2.5))
        self.outcome.evidence["category_results"] = cat_results
        self._collect_evidence_from_items()

    # ── WAF 导航（瑞数 JS 挑战需要真实浏览器执行，首次 412 属预期） ──
    def _navigate_with_waf(self, tries: int = 4) -> bool:
        console_msgs: list[str] = []
        self.page.on("console", lambda m: console_msgs.append(f"[{m.type}] {m.text[:150]}"))
        self.page.on("pageerror", lambda e: console_msgs.append(f"[pageerror] {str(e)[:200]}"))
        # 先访问 189 首页完成瑞数 JS 挑战（获取 cookie），再进入资费专区
        warmups = ["https://www.189.cn/", "https://www.189.cn/tariffZone/"]
        for i, url in enumerate(warmups if tries > 2 else warmups[1:], start=1):
            try:
                resp = self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                status = resp.status if resp else None
                self.log(f"[ctcc] 导航({i}) {url} → HTTP {status}")
            except Exception as e:
                self.log(f"[ctcc] 导航({i}) 失败: {str(e)[:100]}")
                self.page.wait_for_timeout(10000)
                continue
            # 瑞数挑战：页面自动执行 JS 计算 cookie 并 reload；等待最终内容就绪
            for check in range(20):
                self.page.wait_for_timeout(3000)
                try:
                    title = self.page.title()
                    url_now = self.page.url
                    text = self.page.evaluate("() => document.body ? document.body.innerText.slice(0, 160) : ''")
                except Exception:
                    continue
                if "资费" in (title or "") or ("资费" in text and len(text) > 50):
                    if url.endswith("tariffZone/"):
                        self.log(f"[ctcc] WAF 通过，页面就绪（title={title}）")
                        return True
                    break  # 首页就绪 → 进入下一步
                if check % 5 == 4:
                    self.log(f"[ctcc] 等待挑战就绪…（{check + 1}/20 title={title!r} text={text[:40]!r}）")
            self.page.wait_for_timeout(5000)
        self.outcome.errors.append("WAF: 页面始终未就绪（可能被风控，详见 evidence）")
        try:
            self.outcome.evidence["waf_final_state"] = {
                "url": self.page.url,
                "title": self.page.title(),
                "text_head": self.page.evaluate("() => document.body ? document.body.innerText.slice(0, 200) : ''"),
                "html_head": self.page.evaluate("() => document.documentElement.outerHTML.slice(0, 1500)"),
                "cookies": [c["name"] for c in self.page.context.cookies()][:10],
                "console": console_msgs[-30:],
                "ua": self.page.evaluate("() => navigator.userAgent"),
            }
        except Exception:
            pass
        return False

    def _select_hebei_ui(self) -> bool:
        """点击城市页签（北京资费）→ 省份列表 → 河北。"""
        # 城市页签（.city-tab / 含省份名的 top-tab）
        clicked = self.page.evaluate(
            """() => {
              const cands = [...document.querySelectorAll('.city-tab, .top-tab, [class*="city"]')]
              const tab = cands.find(e => (e.innerText || '').includes('资费') && (e.innerText || '').trim().length < 20)
              if (tab) { tab.click(); return 'clicked:' + String(tab.className).slice(0, 40) }
              return 'nf'
            }"""
        )
        self.log(f"[ctcc] 点击省份页签: {clicked}")
        jitter(self.page, (1.5, 3.0))
        # 省份列表出现（hover 或点击后）；河北项
        ok = self.page.evaluate(
            """() => {
              const cands = [...document.querySelectorAll('body *')].filter(e => {
                const t = (e.innerText || '').trim()
                return (t === '河北' || t === '河北省') && e.children.length === 0
              })
              const vis = cands.find(e => e.getBoundingClientRect().width > 0) || cands[0]
              if (vis) { vis.click(); return 'clicked-hebei' }
              return 'nf'
            }"""
        )
        self.log(f"[ctcc] 选择河北: {ok}")
        if ok != "clicked-hebei":
            # 尝试 hover 页签再选
            try:
                self.page.locator(".city-tab").first.hover(timeout=5000)
            except Exception:
                pass
            jitter(self.page, (1.0, 2.0))
            ok = self.page.evaluate(
                """() => {
                  const cands = [...document.querySelectorAll('body *')].filter(e => {
                    const t = (e.innerText || '').trim()
                    return (t === '河北' || t === '河北省') && e.children.length === 0
                  })
                  const vis = cands.find(e => e.getBoundingClientRect().width > 0) || cands[0]
                  if (vis) { vis.click(); return 'clicked-hebei' }
                  return 'nf'
                }"""
            )
            self.log(f"[ctcc] hover 后选择河北: {ok}")
        if ok != "clicked-hebei":
            return False
        self.human_pause((6, 10))
        text = self.page.evaluate("() => document.body.innerText.slice(0, 300)")
        self.outcome.evidence["after_hebei_page_head"] = text[:200]
        return "河北" in text

    def _verify_hebei_params(self):
        for r in self.collector.json_bodies("newTarifZone"):
            url = r.get("url") or ""
            if "provCode=609906" in url:
                self.prov_code = "609906"
                self.outcome.evidence["page_request_with_hebei_provcode"] = url[:200]
                return
        # 页面 localStorage zfParams（前端存储省份选择）
        try:
            zf = self.page.evaluate("() => localStorage.getItem('zfParams')")
            if zf:
                self.outcome.evidence["zfParams_localstorage"] = zf[:200]
                if "609906" in zf:
                    self.prov_code = "609906"
        except Exception:
            pass

    def _page_xhr(self, path_or_url: str, timeout_ms: int = 30000) -> dict:
        url = path_or_url if path_or_url.startswith("http") else f"https://www.189.cn{path_or_url}"
        return self.page.evaluate(XHR_JS, [url, timeout_ms])

    def _collect_evidence_from_items(self):
        prefixes = set()
        lable_names = set()
        for it in self.outcome.items:
            rn = str(it.raw.get("report_no") or "")
            if len(rn) >= 4:
                prefixes.add(rn[:2])
            lable_names.add(str(it.raw.get("lable1Name") or ""))
        self.outcome.evidence["report_prefixes"] = sorted(prefixes)
        self.outcome.evidence["lable_names"] = sorted(lable_names)
        self.outcome.evidence["province_ok"] = self.prov_code == "609906"
        self.outcome.evidence["scope"] = f"河北资费页签(provCode={self.prov_code}) × 网厅个人资费专区"
