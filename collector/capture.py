"""应用层捕获：JSON.parse 钩子（加密信封解密必经点）+ XHR/fetch 网络捕获。

技术来源：CMCC-HE v4 生产方案——移动资费接口走 isWX 加密通道，
网络层只能看到 {body:'<密文>'} 信封，明文在 axios 响应拦截器内
解密后 JSON.parse 才出现；因此 hook JSON.parse 是唯一明文捕获点。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable, Optional

# 注入到页面的捕获钩子（在任何页面 JS 之前执行，每次导航自动重放）
CAPTURE_HOOK = r"""
(() => {
  try {
    if (window.__apiCaptureInstalled) return
    window.__apiCaptureInstalled = true
    window.__apiCapture = []
    window.__currentXhrUrl = null

    const xhrProto = XMLHttpRequest.prototype
    const origOpen = xhrProto.open
    xhrProto.open = function (method, url) {
      try { this.__hookUrl = String(url) } catch (e) {}
      return origOpen.apply(this, arguments)
    }
    const desc = Object.getOwnPropertyDescriptor(xhrProto, 'onreadystatechange')
    if (desc && desc.configurable && desc.set && desc.get) {
      Object.defineProperty(xhrProto, 'onreadystatechange', {
        configurable: true, enumerable: true,
        get: function () { return desc.get.call(this) },
        set: function (fn) {
          if (typeof fn !== 'function') { desc.set.call(this, fn); return }
          const wrapped = function () {
            const prev = window.__currentXhrUrl
            window.__currentXhrUrl = this.__hookUrl || null
            try { return fn.apply(this, arguments) }
            finally { window.__currentXhrUrl = prev }
          }
          desc.set.call(this, wrapped)
        },
      })
    }

    const origParse = JSON.parse
    JSON.parse = function (text, reviver) {
      const result = origParse.call(JSON, text, reviver)
      try {
        if (result && typeof result === 'object' && !Array.isArray(result)) {
          const hasCode = ('returnCode' in result) || ('retCode' in result) || ('code' in result)
          const d = result.data
          const bizShape =
            (d && typeof d === 'object' && (d.page || Array.isArray(d.beans))) ||
            (d && (Array.isArray(d) || Array.isArray(d.tariffList))) ||
            Array.isArray(result.tariffList) || Array.isArray(result.dataObject)
          if (hasCode || bizShape) {
            window.__apiCapture.push({
              t: Date.now(), url: window.__currentXhrUrl || null,
              parsed: origParse.call(JSON, JSON.stringify(result))
            })
            if (window.__apiCapture.length > 1500) window.__apiCapture.splice(0, 400)
          }
        } else if (Array.isArray(result) && result.length && result[0] && typeof result[0] === 'object' && result[0].tariffTable) {
          window.__apiCapture.push({
            t: Date.now(), url: window.__currentXhrUrl || null,
            parsed: { data: result }
          })
        }
      } catch (e) {}
      return result
    }
  } catch (e) {}
})()
"""


class CaptureCollector:
    """聚合 page.on('response') 网络捕获与 JSON.parse 应用层捕获。"""

    def __init__(self, page, label: str = "page"):
        self.page = page
        self.label = label
        self.responses: list[dict] = []   # 网络层 XHR/fetch（明文站点用）
        self.captures: list[dict] = []    # 应用层 JSON.parse 明文（加密站点用）
        self._lock = threading.Lock()
        self._handler: Optional[Callable] = None
        self.api_errors: list[str] = []

    def attach(self):
        def on_response(resp):
            try:
                req = resp.request
                if req.resource_type not in ("xhr", "fetch"):
                    return
                entry = {
                    "t": time.time(),
                    "url": resp.url,
                    "method": req.method,
                    "status": resp.status,
                    "post_data": req.post_data,
                }
                body = None
                if resp.status < 400:
                    try:
                        body = resp.text()[:600000]
                    except Exception:
                        body = None
                else:
                    self.api_errors.append(f"{resp.status} {resp.url[:160]}")
                entry["body"] = body
                with self._lock:
                    self.responses.append(entry)
            except Exception:
                pass

        self._handler = on_response
        self.page.on("response", on_response)

    def pull(self) -> int:
        """拉取页面内捕获缓冲（应用层明文）。"""
        try:
            caps = self.page.evaluate(
                "() => { const a = window.__apiCapture || []; const out = a.slice(); a.length = 0; return out }"
            ) or []
            with self._lock:
                self.captures.extend(caps)
            return len(caps)
        except Exception:
            return len(self.captures)

    def json_bodies(self, url_contains: str = "") -> list[dict]:
        """网络层响应中解析出的 JSON 体（按 URL 过滤）。"""
        out = []
        with self._lock:
            for r in self.responses:
                if url_contains and url_contains not in r["url"]:
                    continue
                body = r.get("body")
                if not body:
                    continue
                try:
                    out.append({"url": r["url"], "method": r["method"], "post_data": r.get("post_data"), "json": json.loads(body)})
                except Exception:
                    continue
        return out

    def dump(self) -> dict:
        with self._lock:
            return {
                "responses": [
                    {k: (v[:300000] if k == "body" else v) for k, v in r.items()} for r in self.responses
                ],
                "captures": self.captures[-800:],
                "api_errors": self.api_errors[:50],
            }
