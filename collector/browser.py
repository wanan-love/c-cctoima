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
        warp_socks = os.environ.get("WARP_SOCKS")
        if warp_socks:
            proxy = {"server": warp_socks}
        launch_args = [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ]
        self._browser: Browser = self._pw.chromium.launch(headless=True, args=launch_args, proxy=proxy)
        self.contexts: list[BrowserContext] = []

    def new_context(self) -> BrowserContext:
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
            ctx = self._browser.new_context(
                user_agent=ua,
                viewport=viewport,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
            )
        # 反自动化检测补丁
        ctx.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            window.chrome = window.chrome || { runtime: {} };
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
