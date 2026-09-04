"""Playwright 浏览器基础设施：WARP 代理、反自动化检测、真人化视口。"""
from __future__ import annotations

import os
import random
from typing import Optional

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from collector.config import USER_AGENTS


class BrowserSession:
    """管理一个 Playwright 实例 + 按需创建 context。

    - WARP_SOCKS 环境变量存在时（GitHub Actions docker 化 WARP），Chromium 走 socks5 代理
    - 视口尺寸微随机（降低指纹一致性）
    - 禁用自动化特征
    """

    def __init__(self, mobile: bool = False):
        self.mobile = mobile
        self._pw = sync_playwright().start()
        proxy = None
        egress = os.environ.get("C2I_EGRESS", "warp")
        warp_socks = os.environ.get("WARP_SOCKS")
        if warp_socks and egress != "direct":
            proxy = {"server": warp_socks}
        launch_args = [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ]
        self._browser: Browser = self._pw.chromium.launch(headless=True, args=launch_args, proxy=proxy)
        self.contexts: list[BrowserContext] = []

    def new_context(self) -> BrowserContext:
        bare = os.environ.get("C2I_BARE_CONTEXT") == "1"
        ua = USER_AGENTS["mobile" if self.mobile else "desktop"]
        if self.mobile:
            viewport = {
                "width": random.randint(388, 396),
                "height": random.randint(836, 852),
            }
            ctx = self._browser.new_context(
                user_agent=ua,
                viewport=viewport,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                is_mobile=True,
                has_touch=True,
                extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
            )
        else:
            viewport = {
                "width": random.randint(1346, 1386),
                "height": random.randint(870, 930),
            }
            if bare:
                # 复刻实测可通过 189.cn 瑞数挑战的最小配置（无 locale/timezone/额外头）
                ctx = self._browser.new_context(user_agent=ua, viewport=viewport)
            else:
                ctx = self._browser.new_context(
                    user_agent=ua,
                    viewport=viewport,
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                    extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
                )
        # 最小化反自动化补丁：仅隐藏 webdriver 标记。
        # ⚠️ 不得伪造 navigator.plugins / languages 等——伪造结构异常（如数字数组）
        # 反而是瑞数等 WAF 的强机器人特征（实测：无补丁可通过 189.cn 挑战）。
        ctx.add_init_script(
            """
            try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (e) {}
            """
        )
        self.contexts.append(ctx)
        return ctx

    def new_page(self, init_script: Optional[str] = None) -> Page:
        ctx = self.new_context()
        page = ctx.new_page()
        page.set_default_timeout(45000)
        if init_script:
            ctx.add_init_script(init_script)
        return page

    def close(self):
        for ctx in self.contexts:
            try:
                ctx.close()
            except Exception:
                pass
        try:
            self._browser.close()
        except Exception:
            pass
        try:
            self._pw.stop()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
