"""采集器基类：发现→采集→证据收集 的统一框架。"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Optional

from collector.browser import BrowserSession
from collector.config import ENTRY_URLS, PACING, ensure_dirs
from collector.human import jitter


@dataclass
class RawTariff:
    """原始资费记录（保留运营商原始字段 + 采集上下文）。"""

    operator: str
    category: str                     # 一级分类（页面实际名称）
    subcategory: str = ""             # 二级分类（页面实际名称，可为空）
    raw: dict = field(default_factory=dict)   # 原始记录（接口返回 or DOM 解析）
    source_api: str = ""              # 数据来源接口 URL
    collected_at: str = ""


@dataclass
class CollectOutcome:
    operator: str
    ok: bool = False
    items: list = field(default_factory=list)          # RawTariff 列表
    categories: list = field(default_factory=list)     # 发现的分类树 [{name, subcategories:[...], expected_total}]
    evidence: dict = field(default_factory=dict)       # 河北/个人证据
    api_log: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    pages_visited: int = 0
    duration_s: float = 0.0


def page_fetch_json(
    page,
    url: str,
    method: str = "GET",
    post_data: Optional[str] = None,
    content_type: Optional[str] = None,
    timeout_ms: int = 30000,
):
    """页面内 fetch（同源策略/站点自身 CORS 范围内），返回 (status, json_or_text)。

    走页面上下文的意义：
    - 站点 WAF 对 XHR/fetch 的 URL 令牌注入（如电信瑞数）自动生效
    - Referer/Cookie/UA 与真实页面一致
    - 批量采集仍使用官方接口，但参数来自页面实际交互验证
    """
    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    js = """
    async ([url, method, postData, headers, timeoutMs]) => {
      try {
        const ctrl = new AbortController()
        const timer = setTimeout(() => ctrl.abort(), timeoutMs)
        const resp = await fetch(url, {
          method, body: postData || undefined,
          headers: headers || undefined,
          credentials: 'include',
          signal: ctrl.signal
        })
        clearTimeout(timer)
        const text = await resp.text()
        let parsed = null
        try { parsed = JSON.parse(text) } catch (e) {}
        return { status: resp.status, text: text.slice(0, 500000), json: parsed }
      } catch (e) {
        return { status: 0, text: String(e), json: null }
      }
    }
    """
    result = page.evaluate(js, [url, method, post_data, headers, timeout_ms])
    return result


class BaseOperator:
    """运营商采集器基类。子类实现 collect_pages()。"""

    op: str = ""
    mobile: bool = False

    def __init__(self, dry_run: bool = False, log=print):
        self.dry_run = dry_run
        self.log = log
        self.outcome = CollectOutcome(operator=self.op)
        self.session: Optional[BrowserSession] = None
        self.page = None

    # ── 生命周期 ──
    def start(self):
        self.session = BrowserSession(mobile=self.mobile)
        ctx = self.session.new_context()
        self.page = ctx.new_page()
        self.page.set_default_timeout(45000)
        return self.page

    def finish(self):
        if self.session:
            self.session.close()
            self.session = None

    def navigate(self, retries: int = 2):
        url = ENTRY_URLS[self.op]
        for i in range(1, retries + 1):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=PACING["nav_timeout_ms"])
                jitter(self.page, PACING["mid_jitter"])
                return True
            except Exception as e:
                self.log(f"[{self.op}] 导航失败({i}/{retries}): {str(e)[:120]}")
                self.outcome.errors.append(f"navigate: {str(e)[:200]}")
                if i == retries:
                    return False
                self.page.wait_for_timeout(8000)
        return False

    def record_api(self, url: str, note: str = ""):
        self.outcome.api_log.append({"t": time.time(), "url": url, "note": note})

    def human_pause(self, rng: tuple[float, float]):
        jitter(self.page, rng)

    def collect(self) -> CollectOutcome:
        t0 = time.time()
        try:
            self.start()
            self.collect_pages()
        except Exception as e:
            self.outcome.errors.append(f"fatal: {type(e).__name__}: {str(e)[:400]}")
            self.log(f"[{self.op}] FATAL: {str(e)[:200]}")
        finally:
            self.outcome.duration_s = round(time.time() - t0, 1)
            self.finish()
        self.outcome.ok = len(self.outcome.errors) == 0 and len(self.outcome.items) > 0
        return self.outcome

    def collect_pages(self):
        raise NotImplementedError

    # ── 输出落盘 ──
    def save_raw(self):
        d = ensure_dirs(self.op)
        raw_dir = d / "raw"
        payload = {
            "operator": self.op,
            "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "ok": self.outcome.ok,
            "categories": self.outcome.categories,
            "evidence": self.outcome.evidence,
            "errors": self.outcome.errors,
            "pages_visited": self.outcome.pages_visited,
            "duration_s": self.outcome.duration_s,
            "items": [
                {
                    "category": it.category,
                    "subcategory": it.subcategory,
                    "raw": it.raw,
                    "source_api": it.source_api,
                    "collected_at": it.collected_at,
                }
                for it in self.outcome.items
            ],
        }
        (raw_dir / "collect.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        return raw_dir / "collect.json"
