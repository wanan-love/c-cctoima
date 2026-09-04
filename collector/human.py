"""真人节奏模拟：随机抖动、浏览式滚动、鼠标漂移（防风控核心）。"""
from __future__ import annotations

import random

from playwright.sync_api import Page


def jitter(page: Page, rng: tuple[float, float]):
    """带随机抖动的等待（秒）。"""
    lo, hi = rng
    page.wait_for_timeout(int(random.uniform(lo, hi) * 1000))


def mouse_drift(page: Page, viewport: dict, probability: float = 0.4):
    """偶发鼠标轨迹漂移，补充真实指针事件。"""
    if random.random() > probability:
        return
    try:
        page.mouse.move(
            random.randint(80, max(90, viewport["width"] - 80)),
            random.randint(60, max(70, viewport["height"] - 80)),
            steps=random.randint(3, 10),
        )
    except Exception:
        pass


def scroll_like_human(
    page: Page,
    count_dom: callable,
    oracle_total: callable = lambda: None,
    max_rounds: int = 400,
    interval: tuple[float, float] = (1.6, 3.0),
    stall_rounds: int = 12,
    log: callable = print,
    on_round: callable = None,
) -> int:
    """浏览式滚动直到列表加载完成（懒加载触发器：直跳底部）。

    完成判定（三重，优先级从高到低）：
    1) total 神谕：接口声明 total 已知且 DOM 数 ≥ total —— 2×5s 短确认后收工
    2) 停滞多轮 + 已到真底 + 3 轮长复查无新增
    3) 0 卡片空转提前退出
    """
    last = 0
    stall = 0
    empty_rounds = 0
    for round_ in range(max_rounds):
        if on_round:
            try:
                on_round()
            except Exception:
                pass
        prev = count_dom()
        if prev == 0:
            empty_rounds += 1
        else:
            empty_rounds = 0
        if empty_rounds >= 15:
            break
        # 10% 概率小步浏览（真人多样性）
        if random.random() < 0.1:
            for _ in range(1 + random.randint(0, 2)):
                page.evaluate(
                    "() => { const h = window.innerHeight * (0.8 + Math.random() * 0.7); window.scrollBy({ top: h, behavior: 'instant' }) }"
                )
                jitter(page, (0.8, 1.8))
        # 直跳绝对底部（模拟 End 键）——懒加载触发器
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        jitter(page, (4.0, 8.0) if random.random() < 0.08 else interval)
        now = count_dom()
        stall = stall + 1 if prev == now else 0
        last = now
        # total 神谕
        oracle = oracle_total()
        if oracle is not None and now != 0 and now >= oracle:
            confirmed = True
            for _ in range(2):
                jitter(page, (4, 7))
                again = count_dom()
                if again > now:
                    confirmed = False
                    stall = 0
                    last = again
                    break
            if confirmed:
                break
        # 停滞判定
        if stall >= stall_rounds and now != 0:
            at_bottom = page.evaluate(
                "() => window.innerHeight + window.scrollY >= document.body.scrollHeight - 100"
            )
            if at_bottom:
                confirmed = True
                for _ in range(3):
                    jitter(page, (8, 15))
                    again = count_dom()
                    if again > now:
                        confirmed = False
                        stall = 0
                        last = again
                        break
                if confirmed:
                    break
            else:
                stall = stall // 2
    # 平滑回顶部
    try:
        page.evaluate("() => window.scrollTo({ top: 0, behavior: 'smooth' })")
        jitter(page, (1.2, 2.5))
    except Exception:
        pass
    return last
