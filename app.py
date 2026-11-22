# -*- coding: utf-8 -*-
"""自动翻页 + 定距滚动（Windows / Python + Playwright + Tkinter）

用法：
    python app.py
依赖：
    pip install playwright
    python -m playwright install chromium   # 用 Edge / Chrome 时可跳过
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import tkinter as tk
from dataclasses import asdict, dataclass, fields
from tkinter import messagebox, ttk

import merge
from browser_core import (
    _click_next,
    _goto,
    _scroll_through,
    _scroll_y,
    _set_bar,
    _smooth_install,
    _smooth_set,
    _smooth_start,
    _smooth_stop,
    _to_top,
    _use_smooth,
    _viewport_center,
    _window_metrics,
    paged_url,
    parse_paged_url,
)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
PROFILE_DIR = os.path.join(APP_DIR, "browser-data")

SELECTOR_PRESETS = [
    "text=下一页",
    "text=下页",
    "text=Next",
    "a[rel=next]",
    "a:has-text('下一页')",
    "li.next a",
    ".next",
    ".pagination-next",
    "button:has-text('下一页')",
    "#pageNav a:has-text('下一页')",
]

PAGE_MODES = ["点击下一页", "URL 递增（_2 _3 …）", "合并成一页", "只滚动（滚到底）"]

# 滚动方式：脚本滚动走页面内 rAF，逐帧推进（实测无静止帧、速度方差最小）；
# 滚轮给“只认滚轮事件”的站点留着，代价是逐次派发、落地按帧，多少有阶梯感。
SCROLL_LABELS = {"js": "脚本滚动（平滑·推荐）", "wheel": "鼠标滚轮（兼容）"}


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    url: str = "https://example.com/"
    selector: str = "text=下一页"
    pages: int = 10
    distance: int = 3000
    speed: int = 500
    settle: float = 1.5
    mode: str = "js"             # js=脚本滚动（页面内 rAF，平滑）/ wheel=鼠标滚轮（兼容）
    page_mode: str = "click"     # click=点击下一页 / url=URL 递增（_2 _3）/ merge=合并成一页 / scroll=只滚动
    whole_first: bool = True     # 合并模式下，第 1 页若有整组图片地址就直接排一页，不再逐页翻
    browser: str = "msedge"      # msedge / chrome / chromium
    keep_login: bool = True
    reset_top: bool = True


def load_config() -> Config:
    cfg = Config()
    if not os.path.exists(CONFIG_PATH):
        return cfg
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return cfg
    valid = {f.name for f in fields(Config)}
    for key, value in data.items():
        if key in valid and value is not None:
            setattr(cfg, key, value)
    return cfg


def save_config(cfg: Config) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(asdict(cfg), fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 运行控制：暂停 / 继续 / 停止
# --------------------------------------------------------------------------- #
class Controller:
    def __init__(self) -> None:
        self._run = threading.Event()
        self._run.set()
        self._stop = threading.Event()
        self._rescroll = threading.Event()
        self._speed = 0                  # 0 = 未设置，用 cfg.speed；运行中 UI 会实时写入
        self._speed_lock = threading.Lock()

    def pause(self) -> None:
        self._run.clear()

    def resume(self) -> None:
        self._run.set()

    def stop(self) -> None:
        self._stop.set()
        self._run.set()          # 暂停状态下也要放行，线程才能退出

    def request_rescroll(self) -> None:
        """请求把当前页面再滚一遍（由 UI 线程调用，实际动作在 worker 里做）。"""
        self._rescroll.set()

    def take_rescroll(self) -> bool:
        if self._rescroll.is_set():
            self._rescroll.clear()
            return True
        return False

    @property
    def running(self) -> bool:
        return self._run.is_set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # ---- 实时速度：UI 线程写，worker 线程每个滚动周期读 ----
    def set_speed(self, value: int) -> None:
        try:
            value = int(value)
        except (TypeError, ValueError):
            return
        if value <= 0:
            return
        with self._speed_lock:
            self._speed = value

    def speed_or(self, default: int) -> int:
        """取当前速度；没被 UI 改过就用配置里的值。"""
        with self._speed_lock:
            value = self._speed
        return int(value) if value > 0 else max(1, int(default))

    def sleep(self, seconds: float) -> bool:
        """可被暂停 / 停止打断的等待。返回 False 表示应当停止。"""
        end = time.time() + max(0.0, seconds)
        while True:
            if self._stop.is_set():
                return False
            if self._run.is_set():
                remain = end - time.time()
                if remain <= 0:
                    return True
                time.sleep(min(0.03, remain))
            else:
                self._run.wait(0.1)


# --------------------------------------------------------------------------- #
# 浏览器动作
# --------------------------------------------------------------------------- #
def _launch(p, cfg: Config, log, headless: bool):
    """返回 (browser, context)；持久化配置时 browser 为 None。"""
    # chromium_sandbox=True 让 Playwright 不再带 --no-sandbox，
    # 否则 Edge/Chrome 顶部会弹出“你使用的是不受支持的命令行标志”的提示条。
    #
    # no_viewport=True：不再把视口写死成 1280×860。
    # 写死视口时，页面永远按 1280×860 排版：窗口比它宽，右侧空一大块（看着像没居中）；
    # 窗口比它矮（笔记本 125%/150% 缩放后很常见），底部直接被切掉——合并页的图就被“截断”了。
    # 改成跟随真实窗口后，可视区域 = 浏览器窗口大小，图片按窗口宽度重新居中。
    common = dict(headless=headless, chromium_sandbox=True)
    if cfg.browser in ("msedge", "chrome"):
        common["channel"] = cfg.browser

    if cfg.keep_login:
        os.makedirs(PROFILE_DIR, exist_ok=True)
        log(f"使用本地浏览器配置（保留登录状态）：{PROFILE_DIR}")
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, no_viewport=True, **common)
        return None, ctx

    # no_viewport 只能给 context（持久化时是 launch_persistent_context 的参数）
    browser = p.chromium.launch(**common)
    ctx = browser.new_context(no_viewport=True)
    return browser, ctx


def _maximize(ctx, page, log=None) -> None:
    """把浏览器窗口最大化，让可视区域拿到屏幕可用高度（合并页整屏看图用）。"""
    try:
        cdp = ctx.new_cdp_session(page)
        win = cdp.send("Browser.getWindowForTarget")["windowId"]
        cdp.send(
            "Browser.setWindowBounds",
            {"windowId": win, "bounds": {"windowState": "maximized"}},
        )
        try:
            cdp.detach()
        except Exception:
            pass
    except Exception:
        return
    if log:
        log("浏览器窗口已最大化（页面按真实窗口大小排版，不再固定 1280×860）")


def _run_scroll(page, cfg: Config, ctl: Controller, log) -> None:
    """只滚动当前页：打开网址 → 一路滚到底，不翻页、不合并、不改写页面。

    就是“帮我把这页滚一遍”：懒加载的图会随滚动加载出来，滚到底就停。
    适合长文章 / 长图 / 无限下拉，又不想让程序动页面结构的站点。
    """
    log(f"只滚动模式：打开 {cfg.url}")
    state: dict = {}
    if not _goto(page, cfg.url, cfg, ctl, log, state):
        return
    if ctl.stopped:
        return

    log("不翻页、不改动页面，直接滚到底（一直滚到没有新内容为止）")
    view = _window_metrics(page)
    height = _page_height(page)
    size_note = f"，可视区域 {view[0]}×{view[1]}" if view else ""
    log(f"页面高度 {height}px{size_note}")
    if height:
        secs = round(height / max(1, int(cfg.speed)), 1) or 0.1
        log(f"按 {cfg.speed}px/s 滚到底约需 {secs} 秒；运行中拖“拖动调速”可随时加减")
    log("开始滚动，可用“暂停/停止”随时中断")

    # 上限放宽到 60 分钟：无限下拉的页面高度会长，20 分钟容易半路停
    _scroll_through(page, cfg, ctl, log, max_minutes=60.0, follow_pending=True)
    if ctl.stopped:
        return
    final = _page_height(page)
    if final and final != height:
        log(f"已滚到底：页面高度从 {height}px 增长到 {final}px（懒加载内容已展开）")
    else:
        log(f"已滚到底：页面高度 {final or height}px")


def _rescroll_page(page, cfg: Config, ctl: Controller, log) -> None:
    """窗口保留期间“再滚一遍”：不重新抓取、不重新合并，只把当前这页从头滚到底。"""
    try:
        page.bring_to_front()
    except Exception:
        pass
    try:
        bar = str(page.evaluate(
            "() => { const el = document.getElementById('merged-bar');"
            " return el ? el.textContent : ''; }"
        ) or "")
    except Exception:
        bar = ""

    log("重新滚动：回到顶部，按当前“滚动速度”再滚一遍")
    _to_top(page)
    if not ctl.sleep(0.3):
        return
    _scroll_through(page, cfg, ctl, log, max_minutes=60.0, follow_pending=True)
    if bar:
        _set_bar(page, bar)          # 恢复合并页左下角的“已合并 N 页 · M 张图”
    if not ctl.stopped:
        log("已重新滚到底：想再看一遍就再点“重新滚动”，点“停止”关闭浏览器")


def _hold_open(page, cfg: Config, ctl: Controller, log, notify=None) -> None:
    """合并 / 只滚动跑完后把窗口留给用户，并在这里响应“重新滚动”请求。"""
    if notify:
        notify("hold")
    while not ctl.stopped:
        if ctl.take_rescroll():
            if notify:
                notify("rescroll")
            _rescroll_page(page, cfg, ctl, log)
            if notify and not ctl.stopped:
                notify("hold")
        if not ctl.sleep(0.25):
            return


def _page_height(page) -> int:
    try:
        return int(page.evaluate("() => document.documentElement.scrollHeight") or 0)
    except Exception:
        return 0


def _process_page(page, cfg: Config, ctl: Controller, log, n: int, total: int) -> None:
    log(f"—— 第 {n}/{total} 页，滚动 {cfg.distance}px（{cfg.speed}px/s）——")
    if cfg.reset_top:
        _to_top(page)
        ctl.sleep(0.15)

    start_y = _scroll_y(page)
    scroll_page(page, cfg, ctl)
    if ctl.stopped:
        return

    moved = _scroll_y(page) - start_y
    if cfg.distance >= 200 and moved <= 1:
        log("提示：页面几乎没有滚动。若站点内层容器在滚动，可把“滚动方式”改成 js")


def scroll_page(page, cfg: Config, ctl: Controller) -> None:
    """把当前页向下滚动 cfg.distance 像素，速度 cfg.speed 像素/秒（可运行中改）。"""
    distance = max(0, int(cfg.distance))
    if distance <= 0:
        return
    if _use_smooth(cfg) and _smooth_install(page):
        _scroll_distance_js(page, cfg, ctl, distance)
    else:
        _scroll_distance_wheel(page, cfg, ctl, distance)


def _scroll_distance_js(page, cfg: Config, ctl: Controller, distance: int) -> None:
    """滚动固定距离（翻页模式用）：rAF 推进，到距离自动停，不会多滚。"""
    start_y = _scroll_y(page)
    speed = ctl.speed_or(cfg.speed)
    if not _smooth_start(page, speed, distance):
        _scroll_distance_wheel(page, cfg, ctl, distance)
        return

    deadline = time.time() + max(15.0, distance / max(1, speed) * 4 + 5)
    paused = False
    moved = 0.0
    try:
        while not ctl.stopped:
            # 同 _scroll_through_js：不能用 ctl.sleep()，否则暂停期间没法下发暂停
            if not ctl.running:                  # 暂停：不消耗距离，等继续
                if not paused:
                    _smooth_set(page, paused=True)
                    paused = True
                time.sleep(0.05)
                continue
            if paused:
                _smooth_set(page, paused=False)
                paused = False

            live = ctl.speed_or(cfg.speed)
            if live != speed:                    # 运行中调速：剩余距离按新速度重算时限
                speed = live
                _smooth_set(page, speed=speed)
                deadline = time.time() + max(15.0, (distance - moved) / max(1, speed) * 4 + 5)

            moved = _scroll_y(page) - start_y
            if moved >= distance - 1:
                return
            if time.time() > deadline:
                return
            time.sleep(0.05)
    finally:
        _smooth_stop(page)


def _scroll_distance_wheel(page, cfg: Config, ctl: Controller, distance: int) -> None:
    """滚轮版：按经过时间累计应走的距离，定时派发一批滚轮。"""
    page.mouse.move(*_viewport_center(page))
    tick = 0.05
    done = 0
    acc = 0.0
    last = time.monotonic()

    while done < distance:
        if ctl.stopped:
            return
        if not ctl.running:
            ctl.sleep(tick)
            last = time.monotonic()
            acc = 0.0
            continue

        speed = ctl.speed_or(cfg.speed)
        now = time.monotonic()
        acc += speed * max(0.0, min(0.25, now - last))
        last = now
        delta = min(int(acc), distance - done)
        if delta <= 0:
            ctl.sleep(0.01)
            continue
        acc -= delta
        try:
            page.mouse.wheel(0, delta)
        except Exception:
            return                       # 页面正在跳转时可能失败，交给外层判断
        done += delta
        if not ctl.sleep(tick):
            return


# --------------------------------------------------------------------------- #
# 任务主流程
# --------------------------------------------------------------------------- #
def run_job(cfg: Config, log, ctl: Controller, headless: bool = False, notify=None) -> None:
    """跑完整个任务。

    notify: 可选回调 notify(kind)，kind = "hold"（滚完了、窗口保留中）/
    "rescroll"（正在重新滚动）。只有合并 / 只滚动两种模式会用到。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("未安装 Playwright。请运行“安装依赖.bat”，或执行：pip install playwright")
        return

    ctx = browser = None
    with sync_playwright() as p:
        try:
            browser, ctx = _launch(p, cfg, log, headless)
        except Exception as exc:
            detail = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
            log(f"启动浏览器失败：{detail}")
            if "already in use" in detail or "ProcessSingleton" in detail or "user data directory" in detail:
                log("提示：browser-data 配置目录被占用。请关掉已打开的调试浏览器窗口，或取消勾选“记住登录状态”")
            else:
                log("提示：确认已安装 Microsoft Edge（或把“浏览器”改成 chromium 并执行 playwright install chromium）")
            return

        keep_open = cfg.page_mode in ("merge", "scroll")   # 合并页 / 只滚动都要留给用户看，结束后不关窗口
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            if not headless and cfg.page_mode == "merge":
                _maximize(ctx, page, log)     # 合并页要整屏看，可视高度 = 屏幕可用高度
            total = max(1, int(cfg.pages))
            state: dict = {}

            if cfg.page_mode == "scroll":
                _run_scroll(page, cfg, ctl, log)
                if not ctl.stopped:
                    log("滚动完成：浏览器窗口会保持打开，可手动回看或点“重新滚动”再看一遍；点“停止”关闭浏览器")
                    _hold_open(page, cfg, ctl, log, notify)
                return

            if cfg.page_mode == "merge":
                merge.run_merge(page, ctx, cfg, ctl, log)
                if not ctl.stopped:
                    log("合并完成：停在页面顶部，未自动滚动；窗口会保持打开——可手动滚动查看，"
                        "或点“重新滚动”按当前速度看一遍，点“停止”关闭浏览器")
                    _hold_open(page, cfg, ctl, log, notify)
                return

            if cfg.page_mode == "url":
                parsed = parse_paged_url(cfg.url)
                if parsed is None:
                    log("URL 递增模式只支持 .../xxx.html 或 .../xxx_2.html 这类地址（不支持带 ? 参数的链接）")
                    return
                start = parsed[1]
                last = start + total - 1
                log(f"URL 递增模式：第 {start} → {last} 页")
                for n, index in enumerate(range(start, last + 1), start=1):
                    if ctl.stopped:
                        break
                    target = paged_url(cfg.url, index)
                    log(f"打开：{target}")
                    if not _goto(page, target, cfg, ctl, log, state):
                        break
                    _process_page(page, cfg, ctl, log, n, total)
            else:
                log(f"打开：{cfg.url}")
                if not _goto(page, cfg.url, cfg, ctl, log, state):
                    return
                for n in range(1, total + 1):
                    if ctl.stopped:
                        break
                    _process_page(page, cfg, ctl, log, n, total)
                    if ctl.stopped:
                        break
                    if n < total:
                        page = _click_next(ctx, page, cfg, ctl, log)
                        if page is None:
                            log("未能继续翻页，任务提前结束")
                            break
                        if not ctl.sleep(max(0.0, float(cfg.settle))):
                            break
        finally:
            try:
                if keep_open and not ctl.stopped:
                    pass                      # 留给用户查看合并结果
                elif browser is not None:
                    browser.close()
                elif ctx is not None:
                    ctx.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# 界面
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.cfg = load_config()
        self.q: queue.Queue = queue.Queue()
        self.ctl: Controller | None = None
        self.thread: threading.Thread | None = None
        self._speed_guard = False        # 调速条 / 输入框互相回写时防循环
        self._speed_after = None         # 调速日志防抖
        self._pending_speed = None

        root.title("自动翻页滚动")
        root.minsize(560, 460)

        self.vars = {
            "url": tk.StringVar(value=self.cfg.url),
            "selector": tk.StringVar(value=self.cfg.selector),
            "pages": tk.StringVar(value=str(self.cfg.pages)),
            "distance": tk.StringVar(value=str(self.cfg.distance)),
            "speed": tk.StringVar(value=str(self.cfg.speed)),
            "settle": tk.StringVar(value=str(self.cfg.settle)),
            "mode": tk.StringVar(value=SCROLL_LABELS.get(self.cfg.mode, SCROLL_LABELS["js"])),
            "page_mode": tk.StringVar(value=self._page_mode_label(self.cfg.page_mode)),
            "browser": tk.StringVar(value=self.cfg.browser),
            "keep_login": tk.BooleanVar(value=bool(self.cfg.keep_login)),
            "reset_top": tk.BooleanVar(value=bool(self.cfg.reset_top)),
            "whole_first": tk.BooleanVar(value=bool(self.cfg.whole_first)),
        }
        # 调速条的变量与输入框分开：Scale 只认数字，直接绑字符串变量会写出 "500.0"
        self.speed_scale_var = tk.DoubleVar(value=float(self.cfg.speed))
        self.speed_hint = tk.StringVar(value=f"{int(self.cfg.speed)} px/秒")

        self._build()

    # ---------------------------- 布局 ---------------------------- #
    def _build(self) -> None:
        pad = dict(padx=6, pady=4)
        root = self.root
        # 先建状态变量：_sync_page_mode() 会用到它，且早于底部状态栏创建
        self.status = tk.StringVar(value="就绪")

        top = ttk.Frame(root, padding=(10, 10, 10, 4))
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="网址").grid(row=0, column=0, sticky="e", **pad)
        ttk.Entry(top, textvariable=self.vars["url"]).grid(row=0, column=1, sticky="ew", **pad)

        ttk.Label(top, text="下一页选择器").grid(row=1, column=0, sticky="e", **pad)
        self.sel_box = ttk.Combobox(
            top, textvariable=self.vars["selector"], values=SELECTOR_PRESETS
        )
        self.sel_box.grid(row=1, column=1, sticky="ew", **pad)

        grid = ttk.Frame(root, padding=(10, 0, 10, 4))
        grid.pack(fill="x")
        for col in (1, 3):
            grid.columnconfigure(col, weight=1)

        ttk.Label(grid, text="浏览页数").grid(row=0, column=0, sticky="e", **pad)
        self.pages_box = ttk.Spinbox(grid, from_=1, to=9999, textvariable=self.vars["pages"], width=10)
        self.pages_box.grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(grid, text="每页滚动(px)").grid(row=0, column=2, sticky="e", **pad)
        self.distance_box = ttk.Spinbox(
            grid, from_=0, to=1000000, increment=100, textvariable=self.vars["distance"], width=10
        )
        self.distance_box.grid(row=0, column=3, sticky="w", **pad)

        ttk.Label(grid, text="滚动速度(px/秒)").grid(row=1, column=0, sticky="e", **pad)
        self.speed_box = ttk.Spinbox(
            grid, from_=20, to=50000, increment=100, textvariable=self.vars["speed"], width=10
        )
        self.speed_box.grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(grid, text="翻页后等待(秒)").grid(row=1, column=2, sticky="e", **pad)
        ttk.Spinbox(grid, from_=0, to=120, increment=0.5, textvariable=self.vars["settle"], width=10).grid(
            row=1, column=3, sticky="w", **pad
        )

        # 调速条：运行中拖动即时生效（滚轮 / 脚本滚动都改在下一个滚动周期）
        ttk.Label(grid, text="拖动调速").grid(row=4, column=0, sticky="e", **pad)
        self.speed_scale = ttk.Scale(
            grid, from_=20, to=max(5000, int(self.cfg.speed)), orient="horizontal",
            variable=self.speed_scale_var, command=self._on_speed_scale,
        )
        self.speed_scale.grid(row=4, column=1, columnspan=2, sticky="ew", **pad)
        ttk.Label(grid, textvariable=self.speed_hint, foreground="#4B5563").grid(
            row=4, column=3, sticky="w", **pad
        )
        self.vars["speed"].trace_add("write", self._on_speed_text)

        ttk.Label(grid, text="滚动方式").grid(row=2, column=0, sticky="e", **pad)
        ttk.Combobox(
            grid, textvariable=self.vars["mode"],
            values=[SCROLL_LABELS["js"], SCROLL_LABELS["wheel"]],
            state="readonly", width=20,
        ).grid(row=2, column=1, sticky="w", **pad)

        ttk.Label(grid, text="浏览器").grid(row=2, column=2, sticky="e", **pad)
        ttk.Combobox(
            grid, textvariable=self.vars["browser"], values=["msedge", "chrome", "chromium"],
            state="readonly", width=12,
        ).grid(row=2, column=3, sticky="w", **pad)

        ttk.Label(grid, text="翻页方式").grid(row=3, column=0, sticky="e", **pad)
        self.page_mode_box = ttk.Combobox(
            grid, textvariable=self.vars["page_mode"], values=PAGE_MODES, state="readonly", width=22
        )
        self.page_mode_box.grid(row=3, column=1, columnspan=2, sticky="w", **pad)
        self.page_mode_box.bind("<<ComboboxSelected>>", lambda _e: self._sync_page_mode())
        ttk.Label(grid, text="URL 型站点无需选择器", foreground="#888780").grid(
            row=3, column=3, sticky="w", **pad
        )

        opts = ttk.Frame(root, padding=(10, 0, 10, 4))
        opts.pack(fill="x")
        ttk.Checkbutton(opts, text="每页开始前回到顶部", variable=self.vars["reset_top"]).pack(side="left", padx=4)
        ttk.Checkbutton(opts, text="记住登录状态", variable=self.vars["keep_login"]).pack(side="left", padx=4)
        self.whole_box = ttk.Checkbutton(
            opts, text="整组地址直接排一页", variable=self.vars["whole_first"]
        )
        self.whole_box.pack(side="left", padx=4)
        self._sync_page_mode()

        bar = ttk.Frame(root, padding=(10, 2, 10, 2))
        bar.pack(fill="x")
        self.start_btn = ttk.Button(bar, text="开始", command=self.start)
        self.start_btn.pack(side="left", padx=4)
        self.pause_btn = ttk.Button(bar, text="暂停", command=self.toggle_pause, state="disabled")
        self.pause_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(bar, text="停止", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.rescroll_btn = ttk.Button(bar, text="重新滚动", command=self.rescroll, state="disabled")
        self.rescroll_btn.pack(side="left", padx=(12, 4))
        ttk.Label(bar, text="（合并后停在顶部不自动滚；也可点此按当前速度滚一遍）", foreground="#888780").pack(side="left")

        ttk.Label(root, textvariable=self.status, padding=(12, 0)).pack(fill="x")

        log_box = ttk.Frame(root, padding=(10, 4, 10, 10))
        log_box.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_box, height=10, wrap="word", state="disabled",
                                font=("Consolas", 9), relief="solid", borderwidth=1)
        scr = ttk.Scrollbar(log_box, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scr.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scr.pack(side="right", fill="y")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(120, self._drain)

    # ---------------------------- 调速 ---------------------------- #
    def _on_speed_text(self, *_args) -> None:
        """输入框改了（含 Spinbox 上下箭头）→ 同步调速条 + 立刻下发。"""
        try:
            value = max(1, int(float(self.vars["speed"].get())))
        except (TypeError, ValueError):
            return                        # 正在输入 "8"、"12" 这类中间态，先不管
        if self._speed_guard:
            return
        self._speed_guard = True
        try:
            if value > float(self.speed_scale.cget("to")):
                self.speed_scale.configure(to=value)   # 输入了超过 5000 的值，滑块量程跟着放宽
            self.speed_scale_var.set(min(value, float(self.speed_scale.cget("to"))))
        except Exception:
            pass
        finally:
            self._speed_guard = False
        self._apply_speed(value)

    def _on_speed_scale(self, raw) -> None:
        try:
            value = max(1, int(float(raw)))
        except (TypeError, ValueError):
            return
        if self._speed_guard:
            return
        self._speed_guard = True
        try:
            self.vars["speed"].set(str(value))
        finally:
            self._speed_guard = False
        self._apply_speed(value)

    def _running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def _apply_speed(self, value: int) -> None:
        """把速度写进 Controller：worker 每个滚动周期读一次，所以下一帧就变。"""
        self.speed_hint.set(f"{value} px/秒")
        if self.ctl is None or not self._running():
            return
        self.ctl.set_speed(value)
        self._pending_speed = value
        if self._speed_after is not None:
            try:
                self.root.after_cancel(self._speed_after)
            except Exception:
                pass
        self._speed_after = self.root.after(500, self._flush_speed_log)

    def _flush_speed_log(self) -> None:
        self._speed_after = None
        value = self._pending_speed
        if value and self.ctl is not None and self._running() and not self.ctl.stopped:
            self.log(f"滚动速度改为 {value}px/s（已生效）")

    # ---------------------------- 模式联动 ---------------------------- #
    @staticmethod
    def _page_mode_label(mode: str) -> str:
        return {"url": PAGE_MODES[1], "merge": PAGE_MODES[2],
                "scroll": PAGE_MODES[3]}.get(mode, PAGE_MODES[0])

    def _sync_page_mode(self) -> None:
        label = self.vars["page_mode"].get()
        by_url = label == PAGE_MODES[1]
        is_merge = label == PAGE_MODES[2]
        pure_scroll = label == PAGE_MODES[3]
        self.sel_box.configure(state="disabled" if (by_url or pure_scroll) else "normal")
        self.pages_box.configure(state="disabled" if pure_scroll else "normal")
        self.distance_box.configure(state="disabled" if (is_merge or pure_scroll) else "normal")
        self.whole_box.configure(state="normal" if is_merge else "disabled")
        if is_merge:
            self.status.set("合并模式：第 1 页若带整组图片地址就直接排一页，否则逐页抓取拼成一页；合并后不自动滚动")
        elif by_url:
            self.status.set("URL 递增模式：网址填第一页，如 .../3738.html")
        elif pure_scroll:
            self.status.set("只滚动模式：打开网址后一路滚到底，不翻页、不改动页面")
        else:
            self.status.set("就绪")

    # ---------------------------- 日志 ---------------------------- #
    def log(self, text: str) -> None:
        self.q.put(("log", text))
    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", time.strftime("[%H:%M:%S] ") + str(payload) + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "status":
                    self.status.set(payload)
                elif kind == "hold":
                    self.status.set("窗口保留中 · 可手动滚动，或点“重新滚动”按当前速度看一遍")
                    self.pause_btn.configure(state="disabled", text="暂停")
                    self.rescroll_btn.configure(state="normal")
                elif kind == "rescroll":
                    self.status.set("重新滚动中…")
                    self.pause_btn.configure(state="normal", text="暂停")
                    self.rescroll_btn.configure(state="disabled")
                elif kind == "done":
                    self._finish(payload)
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    # ---------------------------- 控制 ---------------------------- #
    def _read_config(self) -> Config | None:
        try:
            pages = int(float(self.vars["pages"].get()))
            distance = int(float(self.vars["distance"].get()))
            speed = int(float(self.vars["speed"].get()))
            settle = float(self.vars["settle"].get())
        except ValueError:
            messagebox.showerror("参数错误", "页数 / 距离 / 速度 / 等待 必须是数字")
            return None
        if pages < 1 or distance < 0 or speed < 1 or settle < 0:
            messagebox.showerror("参数错误", "页数≥1，距离≥0，速度≥1，等待≥0")
            return None
        url = self.vars["url"].get().strip()
        if not url:
            messagebox.showerror("参数错误", "请填写网址")
            return None
        if not url.startswith(("http://", "https://", "file://")):
            url = "https://" + url
            self.vars["url"].set(url)
        label = self.vars["page_mode"].get()
        page_mode = "click"
        if label == PAGE_MODES[3]:
            page_mode = "scroll"
        elif label == PAGE_MODES[2]:
            page_mode = "merge"
        elif label == PAGE_MODES[1]:
            page_mode = "url"
        if page_mode == "url" and parse_paged_url(url) is None:
            messagebox.showerror(
                "网址不支持 URL 递增",
                "URL 递增模式需要形如 .../3738.html 或 .../3738_2.html 的地址，且不能带 ? 参数。\n"
                "如果站点是点击翻页，请把“翻页方式”改回“点击下一页”。",
            )
            return None
        return Config(
            url=url,
            selector=self.vars["selector"].get().strip(),
            pages=pages,
            distance=distance,
            speed=speed,
            settle=settle,
            mode="wheel" if self.vars["mode"].get() == SCROLL_LABELS["wheel"] else "js",
            page_mode=page_mode,
            whole_first=bool(self.vars["whole_first"].get()),
            browser=self.vars["browser"].get(),
            keep_login=bool(self.vars["keep_login"].get()),
            reset_top=bool(self.vars["reset_top"].get()),
        )

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        cfg = self._read_config()
        if cfg is None:
            return
        save_config(cfg)
        self.ctl = Controller()
        self.ctl.set_speed(cfg.speed)      # 以界面当前值为准，运行中随时可改
        self.status.set("运行中…")
        self.start_btn.configure(state="disabled")
        self.pause_btn.configure(state="normal", text="暂停")
        self.stop_btn.configure(state="normal")
        self.rescroll_btn.configure(state="disabled")
        self.thread = threading.Thread(target=self._worker, args=(cfg,), daemon=True)
        self.thread.start()

    def _worker(self, cfg: Config) -> None:
        assert self.ctl is not None
        try:
            run_job(cfg, self.log, self.ctl, notify=self._notify)
            msg = "已停止" if self.ctl.stopped else "完成"
        except Exception as exc:                      # noqa: BLE001
            self.log(f"运行出错：{exc.__class__.__name__}: {exc}")
            msg = "出错（详见日志）"
        self.q.put(("done", msg))

    def _notify(self, kind: str) -> None:
        """worker 线程 → UI 线程的状态通知（只放队列，不碰控件）。"""
        self.q.put((kind, ""))

    def rescroll(self) -> None:
        """把当前页面再滚一遍：不重新抓取、不重新合并。"""
        if self.ctl is None or not (self.thread and self.thread.is_alive()):
            return
        self.ctl.request_rescroll()
        self.rescroll_btn.configure(state="disabled")
        self.log("已请求重新滚动")

    def toggle_pause(self) -> None:
        if self.ctl is None:
            return
        if self.ctl.running:
            self.ctl.pause()
            self.pause_btn.configure(text="继续")
            self.status.set("已暂停")
            self.log("已暂停")
        else:
            self.ctl.resume()
            self.pause_btn.configure(text="暂停")
            self.status.set("运行中…")
            self.log("继续")

    def stop(self) -> None:
        if self.ctl is not None:
            self.ctl.stop()
            self.status.set("正在停止…")
            self.log("收到停止指令")

    def _finish(self, msg: str) -> None:
        self.status.set(msg)
        self.start_btn.configure(state="normal")
        self.pause_btn.configure(state="disabled", text="暂停")
        self.stop_btn.configure(state="disabled")
        self.rescroll_btn.configure(state="disabled")

    def on_close(self) -> None:
        cfg = self._read_config()
        if cfg is not None:
            save_config(cfg)
        if self.ctl is not None:
            self.ctl.stop()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.5)
        self.root.destroy()


def _fatal(detail: str) -> None:
    """启动失败时把原因写进 error.log，并弹窗告知，避免 pythonw 下无声退出。"""
    try:
        with open(os.path.join(APP_DIR, "error.log"), "w", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S\n") + detail)
    except Exception:
        pass
    text = "程序启动失败：\n\n" + detail.strip().splitlines()[-1] + "\n\n详细信息已写入 error.log"
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, text, "自动翻页滚动", 0x10)
    except Exception:
        print(detail)


def main() -> None:
    try:
        root = tk.Tk()
        try:
            ttk.Style().theme_use("vista")
        except Exception:
            pass
        App(root)
        root.mainloop()
    except Exception:
        import traceback

        _fatal(traceback.format_exc())


if __name__ == "__main__":
    main()
