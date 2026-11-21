# -*- coding: utf-8 -*-
"""自动翻页 + 定距滚动（Windows / Python + Playwright + Tkinter）

用法：
    python app.py
依赖：
    pip install playwright
    python -m playwright install chromium   # 用 Edge / Chrome 时可跳过
"""

from __future__ import annotations

import base64
import html as html_lib
import json
import os
import queue
import re
import threading
import time
import tkinter as tk
from dataclasses import asdict, dataclass, fields
from tkinter import messagebox, ttk
from urllib.parse import urljoin

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
# URL 递增翻页：.../3738.html → .../3738_2.html → .../3738_3.html
# --------------------------------------------------------------------------- #
URL_RE = re.compile(r"^(?P<base>.*?)(?:_(?P<num>\d+))?\.(?P<ext>html?|shtml|xhtml)$", re.IGNORECASE)


def parse_paged_url(url: str):
    """解析出 (base, 起始页码, 扩展名, 页码位数)；不支持的形式返回 None。"""
    clean = (url or "").split("#")[0].strip()
    if "?" in clean:
        return None
    match = URL_RE.match(clean)
    if not match:
        return None
    num = match.group("num")
    return match.group("base"), int(num) if num else 1, match.group("ext"), len(num) if num else 0


def paged_url(url: str, index: int) -> str:
    parsed = parse_paged_url(url)
    if parsed is None:
        return ""
    base, _, ext, width = parsed
    if index <= 1:
        return f"{base}.{ext}"
    return f"{base}_{index:0{width}d}.{ext}" if width else f"{base}_{index}.{ext}"


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


def _viewport_center(page):
    """鼠标落点：有模拟视口就用它，没有（跟随窗口）就问页面要 innerWidth/Height。"""
    try:
        size = page.viewport_size
    except Exception:
        size = None
    if size:
        return size["width"] // 2, size["height"] // 2
    try:
        width, height = page.evaluate("() => [innerWidth, innerHeight]")
        return int(width) // 2, int(height) // 2
    except Exception:
        return 400, 400


def _activate(page, log=None):
    """把页面拉到前台，并把鼠标放进视口——滚轮事件才会落到页面里。"""
    try:
        page.bring_to_front()
    except Exception:
        pass
    try:
        cx, cy = _viewport_center(page)
        page.mouse.move(cx, cy)
    except Exception:
        pass


def _wait_ready(page, ctl: Controller) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    if ctl.stopped:
        return
    _activate(page)


def _to_top(page) -> None:
    try:
        page.mouse.move(*_viewport_center(page))
        page.keyboard.press("Home")
        # behavior:'instant' 才会无视站点 CSS 里的 scroll-behavior:smooth，
        # 否则“回到顶部”会自己演一段动画，跟后面的逐帧滚动打架。
        page.evaluate("() => window.scrollTo({top: 0, left: 0, behavior: 'instant'})")
    except Exception:
        pass


def _scroll_y(page) -> float:
    try:
        return float(page.evaluate("() => window.scrollY || document.documentElement.scrollTop || 0"))
    except Exception:
        return 0.0


# --------------------------------------------------------------------------- #
# 平滑滚动：页面内 requestAnimationFrame 驱动
#
# 为什么不用“每 50ms 发一次滚轮/scrollBy”：那样每步会走 speed×0.05 像素
# （500px/s 时是 25px 一步），浏览器按帧落地，中间 2~3 帧完全不动、
# 然后猛跳 25px——看起来就是图片在抖。实测 500px/s：逐帧位移只有 {0, 25}
# 两种值、67% 的帧位移为 0，速度方差 ~640；改成页面内 rAF 逐帧推进后
# 每帧走 8~9px、没有静止帧，方差降到 ~46。速度越快跳得越大，所以“抖动”
# 确实和速度相关，但根子在步进方式。
#
# 另外这里用 scrollTo({behavior:'instant'})：站点若给 html 写了
# scroll-behavior:smooth，普通 scrollTo 会被浏览器再插一段动画，和我们的
# 逐帧推进互相拉扯，同样会抖。
# --------------------------------------------------------------------------- #
AUTOSCROLL_JS = r"""
() => {
  const KEY = '__autoScroll';
  if (window[KEY] && window[KEY].version === 3) return true;
  const S = window[KEY] = {
    version: 3, running: false, paused: false, speed: 500,
    acc: 0, last: 0, raf: 0, limit: 0, startY: 0, frames: 0
  };
  const frame = (now) => {
    if (!S.running) { S.raf = 0; return; }
    S.frames += 1;
    const dt = Math.min(0.25, Math.max(0, (now - S.last) / 1000));
    S.last = now;
    if (S.paused) { S.raf = requestAnimationFrame(frame); return; }
    S.acc += S.speed * dt;
    const whole = Math.floor(S.acc);
    if (whole <= 0) { S.raf = requestAnimationFrame(frame); return; }
    S.acc -= whole;
    const here = Math.round(window.scrollY || document.documentElement.scrollTop || 0);
    let step = whole;
    if (S.limit > 0) step = Math.max(0, Math.min(step, S.limit - (here - S.startY)));
    if (step > 0) {
      window.scrollTo({top: here + step, left: window.scrollX || 0, behavior: 'instant'});
    }
    if (S.limit > 0 && here + step - S.startY >= S.limit) { S.running = false; S.raf = 0; return; }
    S.raf = requestAnimationFrame(frame);
  };
  S.start = (speed, limit) => {
    S.speed = Math.max(1, Number(speed) || 1);
    S.limit = Math.max(0, Number(limit) || 0);
    S.startY = Math.round(window.scrollY || document.documentElement.scrollTop || 0);
    S.acc = 0;
    S.paused = false;
    S.frames = 0;
    S.last = performance.now();
    if (!S.running) { S.running = true; S.raf = requestAnimationFrame(frame); }
  };
  S.stop = () => { S.running = false; };
  return true;
}
"""


def _smooth_install(page) -> bool:
    """装上（或复用）页面内的 rAF 滚动器；装不上返回 False，调用方回退到滚轮。"""
    try:
        return bool(page.evaluate(AUTOSCROLL_JS))
    except Exception:
        return False


def _smooth_start(page, speed: int, limit: int = 0) -> bool:
    try:
        page.evaluate(
            "([s, l]) => window.__autoScroll.start(s, l)", [max(1, int(speed)), max(0, int(limit))]
        )
        return True
    except Exception:
        return False


def _smooth_set(page, speed: int | None = None, paused: bool | None = None) -> None:
    """改速度 / 暂停，页面里的滚动器下一帧就生效。"""
    parts = ["() => { const S = window.__autoScroll; if (!S) return;"]
    if speed is not None:
        parts.append(f" S.speed = {max(1, int(speed))};")
    if paused is not None:
        parts.append(" S.paused = %s;" % ("true" if paused else "false"))
    parts.append(" }")
    try:
        page.evaluate("".join(parts))
    except Exception:
        pass


def _smooth_stop(page) -> None:
    try:
        page.evaluate("() => { const S = window.__autoScroll; if (S) S.stop(); }")
    except Exception:
        pass


def _smooth_frames(page) -> int:
    """rAF 已经跑了多少帧：用来区分“页面被挂起”和“页面在动但滚不动”。"""
    try:
        return int(page.evaluate(
            "() => { const S = window.__autoScroll; return S ? S.frames : -1; }"
        ) or -1)
    except Exception:
        return -1


def _use_smooth(cfg: Config) -> bool:
    """脚本滚动走页面内 rAF（最平滑）；滚轮模式给“只认滚轮事件”的站点留着。"""
    return cfg.mode == "js"


def _wait_images(page, ctl: Controller, budget: float, min_width: int = 0,
                 label: str = "", steady: float = 2.0, log=None) -> None:
    """等图片下载告一段落（不是等到“全都成功”），最多 budget 秒。

    判断标准：没有还在下载的图就结束；下载数长时间不动（站点卡住）也结束。
    steady 是“多久没进展算卡住”，默认 2 秒——只适合本地或快站。
    出图慢的站点（排队时进度是成批到达的）必须放大，否则会在下载途中被误判。

    min_width=1 时 1×1 的图算作失败——很多站点先用 grey.gif 占位，
    这种图交给调用方重试或丢弃，不应该把整个等待拖满预算。
    label 不为空时把「已加载 n/N 张」写到合并页左下角，免得看着像卡住。
    """
    deadline = time.time() + max(0.0, budget)
    started = time.time()
    last = None
    stable = 0.0
    last_log = 0.0
    while time.time() < deadline:
        if ctl.stopped:
            return
        try:
            info = page.evaluate(
                "(mw) => { const im = Array.from(document.images);"
                " return {n: im.length,"
                " pending: im.filter(i => !i.complete).length,"
                " failed: im.filter(i => i.complete && i.naturalWidth <= mw).length}; }",
                min_width,
            )
        except Exception:
            return
        if not isinstance(info, dict) or not info.get("n"):
            return
        total = int(info.get("n") or 0)
        pending = int(info.get("pending") or 0)
        failed = int(info.get("failed") or 0)
        if label:
            _set_bar(page, f"{label}已加载 {max(0, total - pending - failed)}/{total} 张…")
        if not pending:
            return
        if log and time.time() - last_log >= 20:
            last_log = time.time()
            log(f"{label}还有 {pending} 张在下载（已等 {int(time.time() - started)} 秒，"
                f"最多 {int(budget)} 秒）")
        if info == last:
            stable += 0.4
            if stable >= max(0.4, steady):    # 连续 steady 秒没有任何进展，当作卡住
                return
        else:
            stable = 0.0
            last = info
        if not ctl.sleep(0.4):
            return


def _retry_failed_images(page, ctl: Controller, log, rounds: int = 2,
                         budget: float = 90.0) -> int:
    """把没取到的图重挂一遍，返回重试后仍然失败的张数。

    一次挂几十张时，站点偶尔会掐掉一批连接（表现是一批图 naturalWidth=0，
    并非真的没有权限）；重试一轮基本都能补回来。

    第 1 轮用**原地址**重挂：站点图带 `cache-control: max-age=31536000`，
    原地址能命中 HTTP 缓存，不必再让慢站重新出图。
    第 2 轮才加 `?r=` 破缓存——它会让每张都重新下载，非必要不用。
    """
    remaining = 0
    for attempt in range(1, max(1, rounds) + 1):
        if ctl.stopped:
            return remaining
        bust = "true" if attempt > 1 else "false"
        try:
            n = int(
                page.evaluate(
                    """(bust) => { let n = 0;
                         document.querySelectorAll('#merged-auto img').forEach((img, k) => {
                           if (img.complete && img.naturalWidth <= 1) {
                             const orig = img.dataset.src || img.src;
                             if (!orig) return;
                             const next = bust
                               ? orig.split('#')[0] + (orig.includes('?') ? '&' : '?') + 'r=' + Date.now() + '-' + k
                               : orig;
                             img.removeAttribute('src');   // 先清空，确保同一个地址也会重新发起请求
                             img.src = next;
                             n++;
                           }
                         });
                         return n; }""",
                    bust == "true",
                )
                or 0
            )
        except Exception:
            return remaining
        if not n:
            return 0
        how = "原地址（走缓存）" if attempt == 1 else "加时间戳（破缓存）"
        log(f"有 {n} 张图没取到（站点掐了连接），用{how}重试第 {attempt} 轮…")
        _wait_images(page, ctl, budget, min_width=1, label="重试 ", steady=12.0, log=log)
        stats = _image_stats(page)
        remaining = stats[2] if stats else 0
        if remaining == 0:
            log("重试后图片都补齐了")
            break
    return remaining


def _looks_empty(page) -> bool:
    """正文既没有文字也没有图片，基本可以判定是空页 / 已到末尾。"""
    try:
        info = page.evaluate(
            "() => ({t: document.body ? document.body.innerText.trim().length : 0,"
            " i: document.images.length})"
        )
    except Exception:
        return False
    if not isinstance(info, dict):
        return False
    return info.get("t", 0) < 30 and info.get("i", 0) == 0


CONTENT_SELECTORS = [
    "#pageContainer", "#content", ".article-content", ".post-content",
    "article", "#article", ".entry-content", "#cont",
]


def _content_empty(page) -> bool:
    """常见正文容器存在但里面没字也没图 —— 说明这一页是空壳。"""
    try:
        return bool(
            page.evaluate(
                """(sels) => {
                     for (const s of sels) {
                       const el = document.querySelector(s);
                       if (!el) continue;
                       const text = (el.innerText || '').trim().length;
                       const imgs = el.querySelectorAll('img').length;
                       return text < 20 && imgs === 0;
                     }
                     return false;
                   }""",
                CONTENT_SELECTORS,
            )
        )
    except Exception:
        return False


def _goto(page, url: str, cfg: Config, ctl: Controller, log, state: dict,
          wait_imgs: bool = True) -> bool:
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as exc:
        detail = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        log(f"打开失败：{detail}")
        return False
    if resp is not None and resp.status >= 400:
        log(f"HTTP {resp.status}，没有更多页了，停止")
        return False
    _wait_ready(page, ctl)
    if wait_imgs:
        _wait_images(page, ctl, max(6.0, float(cfg.settle) * 3))

    if _looks_empty(page):
        log("这一页没有任何内容，停止")
        return False
    if state.get("loaded", 0) >= 1 and _content_empty(page):
        log("这一页正文容器是空的（通常表示已超出末页），停止")
        return False
    state["loaded"] = state.get("loaded", 0) + 1
    return True


IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
PLACEHOLDER_IMG_RE = re.compile(
    r"""src\s*=\s*["']?[^"'\s>]*?(?:grey|gray|placeholder|loading|blank|spacer|pixel)[^"'\s>]*\.(?:gif|png)""",
    re.IGNORECASE,
)
# 图片上写死的尺寸/样式必须去掉：站点给了固定 height（或 overflow:hidden 的包裹层）时，
# 合并页里图片只会露出一截，还会因为 width 被锁死而偏到一边。
IMG_SIZE_ATTR_RE = re.compile(
    r"""\s(?:width|height|style)\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""",
    re.IGNORECASE,
)


def _strip_junk(html: str) -> tuple[str, int]:
    """去掉脚本、占位图，以及图片上写死的宽高/内联样式。返回 (干净 HTML, 丢掉的占位图数量)。"""
    html = SCRIPT_RE.sub("", html)
    dropped = 0

    def _sub(match: re.Match) -> str:
        nonlocal dropped
        tag = match.group(0)
        if PLACEHOLDER_IMG_RE.search(tag):
            dropped += 1
            return ""
        return IMG_SIZE_ATTR_RE.sub("", tag)

    return IMG_TAG_RE.sub(_sub, html), dropped


def _grab_content(page):
    """取出正文容器的 innerHTML，返回 (html, 选择器, 丢掉的占位图数量)。"""
    try:
        result = page.evaluate(
            """(sels) => {
                 for (const s of sels) {
                   const el = document.querySelector(s);
                   if (!el) continue;
                   const text = (el.innerText || '').trim().length;
                   const imgs = el.querySelectorAll('img').length;
                   if (text > 10 || imgs > 0) return {html: el.innerHTML, sel: s};
                 }
                 return {html: '', sel: ''};
               }""",
            CONTENT_SELECTORS,
        )
    except Exception:
        return "", "", 0
    if not isinstance(result, dict):
        return "", "", 0
    raw = result.get("html") or ""
    clean, dropped = _strip_junk(raw)
    return clean, result.get("sel") or "", dropped


def _wait_real_images(page, ctl: Controller, budget: float, log=None, page_no: int = 0) -> None:
    """等正文里的图片换成真实地址（针对先用 1×1 占位图的站点）。

    只盯正文容器，避免被页头 1px 统计图拖住。
    """
    start = time.time()
    deadline = start + max(0.0, budget)
    last = None
    while True:
        if ctl.stopped:
            return
        try:
            last = page.evaluate(
                """(sels) => {
                     let root = null;
                     for (const s of sels) { const el = document.querySelector(s); if (el) { root = el; break; } }
                     if (!root) root = document.body;
                     if (!root) return {n: 0, bad: 0};
                     const im = Array.from(root.querySelectorAll('img'));
                     return {n: im.length,
                             bad: im.filter(i => !i.complete || i.naturalWidth <= 1).length};
                   }""",
                CONTENT_SELECTORS,
            )
        except Exception:
            return
        if not isinstance(last, dict):
            return
        if not last.get("n") or not last.get("bad"):
            break
        if time.time() >= deadline:
            break
        if not ctl.sleep(0.25):
            return

    if not log or not isinstance(last, dict) or not last.get("n"):
        return
    elapsed = time.time() - start
    if last.get("bad"):
        log(f"第 {page_no} 页还有 {last['bad']}/{last['n']} 张图没换成真实地址（等了 {elapsed:.0f}s）")
    elif elapsed >= 2:
        log(f"第 {page_no} 页 {last['n']} 张图已就位（等 {elapsed:.0f}s）")


MERGED_CSS = (
    "html,body{margin:0;padding:0;width:100%;background:#111;overflow-x:hidden}"
    "body{color:#e6e6e6;font:15px/1.7 'Segoe UI','Microsoft YaHei',sans-serif}"
    # 容器跟着窗口宽度走，靠 margin:auto 居中；不再固定 1400px 后靠边留白
    "#merged-auto{width:100%;max-width:1600px;margin:0 auto;padding:0 12px 28px;box-sizing:border-box}"
    # 图片：块级 + 左右 auto 居中；宽高都 auto，只受 max-width 限制，绝不写死高度。
    # 全部 !important，是因为站点常在图片或包裹层上写内联的 width/height/max-height，
    # 那会把图片压扁、裁掉一截，或者挤到一边（内联样式优先级高于普通样式表规则）。
    "#merged-auto img{display:block!important;width:auto!important;height:auto!important;"
    "max-width:100%!important;max-height:none!important;min-height:0!important;"
    "margin:0 auto 10px!important;background:#1b1b1b;object-fit:contain!important;"
    "position:static!important;float:none!important;transform:none!important;border:0}"
    # 站点残留的固定高度 / overflow:hidden 包裹层会把图片裁掉一截，这里统一放开
    "#merged-auto div,#merged-auto p,#merged-auto span,#merged-auto a{"
    "height:auto!important;max-height:none!important;overflow:visible!important;"
    "position:static!important;float:none!important}"
    "#merged-auto *{max-width:100%}"
    "#merged-bar{position:fixed;left:8px;bottom:8px;z-index:2147483647;"
    "font:12px/1.6 Consolas,monospace;color:#ddd;background:rgba(0,0,0,.72);"
    "padding:2px 8px;border-radius:4px;pointer-events:none}"
)


def _merged_document(body: str, note: str) -> str:
    """合并页是一份全新文档：不带站点的 CSS / JS，避免它们把内容重新清空或隐藏。"""
    return (
        "<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>合并页</title><style>" + MERGED_CSS + "</style></head><body>"
        f"<div id=\"merged-auto\">{body}</div>"
        f"<div id=\"merged-bar\">{html_lib.escape(note)}</div></body></html>"
    )


def _set_bar(page, text: str) -> None:
    try:
        page.evaluate(
            "(t) => { const el = document.getElementById('merged-bar'); if (el) el.textContent = t; }",
            text,
        )
    except Exception:
        pass


def _window_metrics(page):
    """返回 (可视宽度, 可视高度, 文档宽度)；拿不到返回 None。"""
    try:
        data = page.evaluate(
            "() => [innerWidth, innerHeight, document.documentElement.scrollWidth]"
        )
    except Exception:
        return None
    if not isinstance(data, (list, tuple)) or len(data) != 3:
        return None
    try:
        return tuple(int(v) for v in data)
    except Exception:
        return None


def _prune_broken_images(page) -> int:
    """删掉合并页里确实加载失败的图，否则黑底上只剩几个 1px 小点，看着像“一条虚线”。

    只删「已经结束加载但宽高≤1」的图：还在下载中的图片不能算坏图，
    否则会在加载完成前被误删。
    """
    try:
        return int(
            page.evaluate(
                """() => { let n = 0;
                     document.querySelectorAll('#merged-auto img').forEach(i => {
                       if (i.complete && i.naturalWidth <= 1) { i.remove(); n++; }
                     });
                     return n; }"""
            )
            or 0
        )
    except Exception:
        return 0


# --------------------------------------------------------------------------- #
# 整组图片：一页就带全所有图片地址的站点（图集站常见），不用逐页翻
#
# 翻页、取地址、把图片排在同一页是三件事。很多站点第 1 页的 HTML / 接口里
# 已经带着整组图片地址（base64 隐藏域、JSON 数组、脚本变量），分页只是前端
# 拿这组地址切片显示。这种情况一次 GET 就够：解码出地址 → 按顺序拼成一串
# <img> → 同一个浏览器会话里渲染成一页，既不用翻页，也不用先下载图片文件。
# --------------------------------------------------------------------------- #
JUNK_IMAGE_STEMS = {
    "grey", "gray", "blank", "spacer", "pixel", "1x1", "loading", "placeholder", "transparent",
}
B64_TOKEN_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
IMG_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|bmp)$", re.IGNORECASE)
JSON_IMG_RE = re.compile(
    r"""["']((?:https?:)?/{1,2}[^"'\s\\]{1,300}?\.(?:jpe?g|png|webp|gif|bmp)"""
    r"""|[^"'\s\\/:]{1,200}\.(?:jpe?g|png|webp|gif|bmp))["']""",
    re.IGNORECASE,
)


def _b64_to_path(token: str) -> str:
    """站点把图片路径 base64 藏起来时，还原成路径；不像图片地址就返回空串。"""
    token = (token or "").strip()
    if len(token) < 8 or not B64_TOKEN_RE.match(token):
        return ""
    try:
        text = base64.b64decode(token).decode("utf-8").strip()
    except Exception:
        return ""
    if not text or len(text) > 600 or not IMG_EXT_RE.search(text):
        return ""
    if any(ch in text for ch in ' \t\r\n"\'<>'):
        return ""
    return text


def _parse_img_list(raw: str) -> list[str]:
    """从一段字符串里取出图片地址：支持 base64 竖线列表、JSON 数组、逐行列表。"""
    raw = (raw or "").strip()
    if len(raw) < 20:
        return []

    if "|" in raw:
        chunks = [c for c in raw.split("|") if c.strip()]
        paths = [_b64_to_path(c) for c in chunks]
        good = [p for p in paths if p]
        if len(good) >= 2 and len(good) == len(paths):
            return good

    urls = [u.strip() for u in JSON_IMG_RE.findall(raw) if len(u.strip()) > 4]
    if len(urls) >= 2:
        return urls

    hits = []
    for piece in re.split(r"[\s,;\[\]\(\)]+", raw):
        piece = piece.strip().strip("\"'").strip()
        if len(piece) > 4 and IMG_EXT_RE.search(piece):
            hits.append(piece)
    return hits if len(hits) >= 2 else []


def _normalize_urls(urls, base: str) -> list[str]:
    """补成绝对地址、按出现顺序去重、丢掉 1×1 占位图。"""
    out: list[str] = []
    seen = set()
    for item in urls or []:
        text = (item or "").strip().replace("\\/", "/")
        if not text:
            continue
        name = text.split("?")[0].split("#")[0].rsplit("/", 1)[-1].lower()
        stem = name.rsplit(".", 1)[0] if "." in name else name
        if stem in JUNK_IMAGE_STEMS:
            continue
        full = urljoin(base, text) if base else text
        if not full.startswith(("http://", "https://", "file://", "data:")):
            continue
        if full in seen:
            continue
        seen.add(full)
        out.append(full)
    return out


def _collect_whole_image_urls(page):
    """在第 1 页里找“整组图片地址”。返回 (urls, 来源说明)；没有就返回 ([], "")。

    只认明显比本页展示得更多的列表（图集站是「整组地址 + 前端切片」），
    这样万一某个站点只是在隐藏域里放了本页那几张图，会照旧走逐页合并。
    """
    try:
        found = page.evaluate(
            """() => {
                 const nodes = document.querySelectorAll(
                   'input[type=hidden], textarea, [id*=imgData], [id*=imgList], [id*=imglist],'
                   + '[id*=picList], [id*=piclist], [class*=imgData], [class*=imgList],'
                   + '[data-imgs], [data-images], [data-srcs], [data-list]'
                 );
                 const out = [];
                 for (const el of nodes) {
                   const raw = el.value || el.getAttribute('data-imgs')
                             || el.getAttribute('data-images') || el.getAttribute('data-srcs')
                             || el.getAttribute('data-list') || el.textContent || '';
                   if (typeof raw === 'string' && raw.length > 40) {
                     out.push({name: el.id || el.getAttribute('name') || el.tagName.toLowerCase(), raw});
                   }
                 }
                 const imgs = Array.from(document.querySelectorAll(
                   '#pageContainer img, #content img, article img, .article-content img'
                 ));
                 const visible = imgs.length;
                 const longest = Array.from(document.scripts)
                   .map(s => s.textContent || '')
                   .sort((a, b) => b.length - a.length)[0] || '';
                 return {found: out, visible, longest: longest.slice(0, 300000)};
               }"""
        )
    except Exception:
        return [], ""
    if not isinstance(found, dict):
        return [], ""

    base = page.url or ""
    try:
        visible = int(found.get("visible") or 0)
    except Exception:
        visible = 0

    best: list[str] = []
    best_src = ""
    for item in found.get("found") or []:
        if not isinstance(item, dict):
            continue
        urls = _normalize_urls(_parse_img_list(item.get("raw") or ""), base)
        if len(urls) > len(best):
            best, best_src = urls, f"#{item.get('name') or '隐藏域'}"

    # 兜底：脚本里一整组图片地址（JSON 数组 / 变量），阈值放大，避免误判
    script_urls = _normalize_urls(_parse_img_list(found.get("longest") or ""), base)
    if len(script_urls) >= 8 and len(script_urls) > len(best) * 2:
        best, best_src = script_urls, "页面脚本里的图片数组"

    if len(best) < 4 or len(best) <= visible:
        return [], ""
    return best, best_src


def _images_document(urls, note: str) -> str:
    """按顺序把地址排成同一页：只有 <img>，没有站点 CSS/JS，也不用落地文件。

    data-src 留一份原始地址：重试时要按原地址重挂（能吃 HTTP 缓存），
    不能拿已经带过 `?r=` 的 src 再拼一层。
    """
    imgs = "".join(
        f'<img src="{html_lib.escape(u, quote=True)}" '
        f'data-src="{html_lib.escape(u, quote=True)}" alt="">'
        for u in urls
    )
    return _merged_document(imgs, note)


def _image_stats(page):
    """返回 (已加载, 下载中, 失败, 总数)；拿不到返回 None。"""
    try:
        data = page.evaluate(
            """() => { const im = Array.from(document.images);
                 return {loaded: im.filter(i => i.complete && i.naturalWidth > 1).length,
                         pending: im.filter(i => !i.complete).length,
                         failed: im.filter(i => i.complete && i.naturalWidth <= 1).length,
                         total: im.length}; }"""
        )
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return (int(data.get("loaded") or 0), int(data.get("pending") or 0),
                int(data.get("failed") or 0), int(data.get("total") or 0))
    except Exception:
        return None


def _wait_budget(total: int, per_image: float = 3.0,
                 floor: float = 60.0, cap: float = 420.0) -> float:
    """按张数估算等图预算：站点出图慢时（实测 3~5 秒/张）固定预算必然不够。

    per_image 取 3 秒是实测中位值：81 张 → 243 秒，够把整组下完；
    张数少时不低于 floor（60 秒），再长也不超过 cap（7 分钟）。
    """
    return max(float(floor), min(float(cap), max(0, int(total)) * per_image))


def _wait_pending_at_bottom(page, ctl: Controller, log,
                            budget: float, no_progress: float = 60.0) -> None:
    """滚到底但还有图在下载：在这里等它们下完，而不是到点静默走人。

    两个刹车：总预算 budget 秒；以及连续 no_progress 秒一张都没下来
    （站点真断了，再等也没意义）。等待中如实写日志，免得用户以为卡死。
    """
    if budget <= 0:
        return
    t0 = time.time()
    last = None
    flat = 0.0
    tick = 0.6
    while time.time() - t0 < budget:
        if ctl.stopped:
            return
        stats = _image_stats(page)
        if not stats or not stats[1]:
            return
        pending = stats[1]
        if last is not None and pending < last:
            flat = 0.0
        else:
            flat += tick
        last = pending
        waited = int(time.time() - t0)
        if flat >= no_progress:
            log(f"连续 {int(flat)} 秒没有新图下来（还差 {pending} 张），先按已加载的继续")
            return
        _set_bar(page, f"滚到底 · 还差 {pending}/{stats[3]} 张图片，已等 {waited}s…")
        if not ctl.sleep(tick):
            return
    stats = _image_stats(page)
    if stats and stats[1]:
        log(f"已等满 {int(budget)} 秒，还有 {stats[1]} 张没下完"
            f"（站点出图慢或被限流），先按已加载的继续")


def _render_image_list(page, urls, source: str, cfg: Config, ctl: Controller, log) -> bool:
    """把整组地址渲染成一页并连续滚到底（合并模式的快路径）。渲染成功返回 True。"""
    total = len(urls)
    log(f"第 1 页就带着整组图片地址：{total} 张（来源 {source}），不必逐页翻，直接排成一页")
    try:
        page.set_content(
            _images_document(urls, f"整组 {total} 张 · 正在加载…"),
            wait_until="domcontentloaded",
        )
    except Exception as exc:
        detail = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        log(f"渲染整组图片页失败：{detail}（回退到逐页合并）")
        return False

    budget = _wait_budget(total)
    log(f"正在等图片加载（同一浏览器会话内直接引用地址，不下载到本地），"
        f"按每张约 3 秒估最多等 {int(budget)} 秒…")
    _wait_images(page, ctl, budget, min_width=1, label=f"整组 {total} 张 · ", steady=15.0,
                 log=log)
    if ctl.stopped:
        return True
    _retry_failed_images(page, ctl, log)

    stats = _image_stats(page) or (0, 0, 0, total)
    if stats[1]:
        log(f"还有 {stats[1]} 张在下载（站点出图慢），先开始滚动，滚到底时会等它们下完")
    broken = _prune_broken_images(page)
    try:
        count = int(page.evaluate("() => document.images.length") or 0)
        height = int(page.evaluate("() => document.documentElement.scrollHeight") or 0)
    except Exception:
        count, height = 0, 0
    view = _window_metrics(page)

    size_note = f"，可视区域 {view[0]}×{view[1]}" if view else ""
    log(f"已排成同一页：{count}/{total} 张图片，页面高度 {height}px{size_note}")
    if broken:
        log(f"有 {broken} 张图确实加载失败（站点拒绝直连，或会话失效），已从页面移除")
    if not count:
        log("一张图都没加载出来：先别关窗口，按 F12 看 Network 里图片的状态码（常见是 403 / 需要登录）")
    _set_bar(page, f"整组 {total} 张 · 已排 {count} 张 · 高度 {height}px")

    if height:
        log(f"按 {cfg.speed}px/s 滚到底约需 {height // max(1, int(cfg.speed))} 秒；运行中拖“拖动调速”可随时加减")
    log("开始连续滚动到底，可用“暂停/停止”随时中断")
    _scroll_through(page, cfg, ctl, log, follow_pending=True,
                    pending_budget=budget, no_progress=60.0)
    final = _image_stats(page)
    if final:
        notes = []
        if final[2]:
            notes.append(f"{final[2]} 张坏图")
        if final[1]:
            notes.append(f"{final[1]} 张还没下完")
        tail = f"（{'，'.join(notes)}）" if notes else "（全部到位）"
        log(f"已滚到底：{final[0]}/{final[3]} 张图片可见{tail}")
        _set_bar(page, f"整组 {total} 张 · 已加载 {final[0]}/{final[3]} · 高度 {height}px")
        if final[2]:
            removed = _prune_broken_images(page)
            if removed:
                log(f"{removed} 张确实没取到，已从页面移除；再跑一次可让它们走缓存重下")
    return True


def _scroll_through(page, cfg: Config, ctl: Controller, log, max_minutes: float = 20.0,
                    follow_pending: bool = False,
                    pending_budget: float = 60.0, no_progress: float = 60.0) -> None:
    """一直滚到页面底部（合并模式下用），高度随图片加载增长会实时重算。

    follow_pending=True 时，滚到底但还有图片在下载就不算结束：等一会儿再继续，
    免得页面底部后半截图片还没下完就停了。pending_budget 是这份等待的总预算，
    出图慢的站点要按张数放大（见 _wait_budget）；no_progress 是“多久没新图就算它断了”。
    """
    if _use_smooth(cfg) and _smooth_install(page):
        _scroll_through_js(page, cfg, ctl, log, max_minutes, follow_pending,
                           pending_budget, no_progress)
    else:
        _scroll_through_wheel(page, cfg, ctl, log, max_minutes, follow_pending,
                              pending_budget, no_progress)


def _bottom_gap(page):
    """离页面底部还有多少像素；取不到返回 None。"""
    try:
        bottom = page.evaluate(
            "() => document.documentElement.scrollHeight - window.innerHeight"
        )
    except Exception:
        return None
    return bottom if isinstance(bottom, (int, float)) else None


def _scroll_through_js(page, cfg: Config, ctl: Controller, log, max_minutes: float,
                       follow_pending: bool, pending_budget: float = 60.0,
                       no_progress: float = 60.0) -> None:
    """平滑模式：页面内 rAF 逐帧推进，Python 只做监控（100ms 一次）。

    速度 / 暂停都是即时下发的，所以运行中拖动调速条下一帧就变。
    """
    speed = ctl.speed_or(cfg.speed)
    if not _smooth_start(page, speed, 0):
        _scroll_through_wheel(page, cfg, ctl, log, max_minutes, follow_pending,
                              pending_budget, no_progress)
        return

    deadline = time.time() + max_minutes * 60
    last_y = None
    last_frames = -1
    idle = 0.0
    paused = False
    tick = 0.1

    try:
        while time.time() < deadline:
            if ctl.stopped:
                return
            # 注意：这里不能用 ctl.sleep()——它会在暂停期间一直阻塞，
            # 而位移是页面里的 rAF 在跑，阻塞期间就没法把“暂停”下发给页面。
            running = ctl.running
            if not running:
                if not paused:
                    _smooth_set(page, paused=True)
                    paused = True
                time.sleep(0.05)
                continue
            if paused:
                _smooth_set(page, paused=False)
                paused = False

            live = ctl.speed_or(cfg.speed)
            if live != speed:                     # 运行中调速：下一帧生效
                speed = live
                _smooth_set(page, speed=speed)

            bottom = _bottom_gap(page)
            if bottom is None:
                return
            y = _scroll_y(page)
            if y >= bottom - 4:
                if not follow_pending:
                    return
                # 滚到底后还有图在下载：在这里老实等（预算按张数算），不再等 60 秒就走人
                _wait_pending_at_bottom(page, ctl, log, pending_budget, no_progress)
                return

            frames = _smooth_frames(page)
            if last_y is not None and abs(y - last_y) < 1:
                idle += tick
                if idle >= 2.0 and frames >= 0 and frames == last_frames:
                    # rAF 只在页面可见时跑：窗口被最小化/完全遮住时浏览器会停掉它，
                    # 这时页面根本不会动，换滚轮（CDP 输入不受可见性限制）继续。
                    log("页面 2 秒没动且动画被浏览器挂起（窗口在后台或被遮住），改用鼠标滚轮继续")
                    _smooth_stop(page)
                    remain = max(0.5, (deadline - time.time()) / 60.0)
                    _scroll_through_wheel(page, cfg, ctl, log, remain, follow_pending,
                                          pending_budget, no_progress)
                    return
                if idle >= 20.0:
                    log("滚了 20 秒页面纹丝不动：多半是内容放在内部容器里滚（window 不滚），"
                        "或页面拦了滚动事件；换成“鼠标滚轮”再试，仍不行就只能手动拖滚动条")
                    return
            else:
                idle = 0.0
            last_y = y
            last_frames = frames

            time.sleep(tick)
    finally:
        _smooth_stop(page)
    log("到达滚动时限，停止")


def _scroll_through_wheel(page, cfg: Config, ctl: Controller, log, max_minutes: float = 20.0,
                          follow_pending: bool = False, pending_budget: float = 60.0,
                          no_progress: float = 60.0) -> None:
    """滚轮模式：给“只认滚轮事件”的站点留着。

    位移按经过的时间累计（不再用固定的 step），所以实际速度更接近设定值；
    但滚轮是逐次派发的，浏览器按帧落地，仍然会有阶梯感——要平滑用脚本滚动。
    """
    tick = 0.05
    page.mouse.move(*_viewport_center(page))
    deadline = time.time() + max_minutes * 60
    last_y = None
    idle = 0.0                     # 滚了却不动的时间：站点可能把内容放在内部容器里
    acc = 0.0
    last = time.monotonic()

    while time.time() < deadline:
        if ctl.stopped:
            return
        if not ctl.running:
            ctl.sleep(tick)
            last = time.monotonic()
            acc = 0.0
            continue
        bottom = _bottom_gap(page)
        if bottom is None:
            return
        y = _scroll_y(page)
        if y >= bottom - 4:
            if not follow_pending:
                return
            _wait_pending_at_bottom(page, ctl, log, pending_budget, no_progress)
            return

        if last_y is not None and abs(y - last_y) < 1:
            idle += tick
            if idle >= 20.0:
                log("滚了 20 秒页面纹丝不动：多半是内容放在内部容器里滚（window 不滚），"
                    "或页面拦了滚轮事件；仍不行就只能手动拖滚动条")
                return
        else:
            idle = 0.0
        last_y = y

        speed = ctl.speed_or(cfg.speed)
        now = time.monotonic()
        acc += speed * max(0.0, min(0.25, now - last))
        last = now
        dy = int(acc)
        acc -= dy
        if dy > 0:
            try:
                page.mouse.wheel(0, dy)
            except Exception:
                return
        if not ctl.sleep(tick):
            return
    log("到达滚动时限，停止")



def _run_merge(page, ctx, cfg: Config, ctl: Controller, log) -> None:
    """把多页正文抓下来拼成同一个页面，再连续滚到底。"""
    total = max(1, int(cfg.pages))
    parsed = parse_paged_url(cfg.url)
    start = parsed[1] if parsed else 1
    state = {}                     # 第 1 页不判空，之后靠“正文容器是否为空”判断结束
    parts = []
    used_sel = ""
    dropped_total = 0
    # 站点把 grey.gif 占位换成真实地址要好几秒，等待时间随“翻页后等待”放大
    wait_budget = min(30.0, max(8.0, float(cfg.settle) * 6))

    for n in range(1, total + 1):
        if ctl.stopped:
            break
        if parsed:
            target = paged_url(cfg.url, start + n - 1)
            log(f"抓取第 {n}/{total} 页：{target}")
            if not _goto(page, target, cfg, ctl, log, state, wait_imgs=False):
                break
        elif n == 1:
            log(f"抓取第 1/{total} 页：{cfg.url}")
            if not _goto(page, cfg.url, cfg, ctl, log, state, wait_imgs=False):
                break
        else:
            log(f"抓取第 {n}/{total} 页：点击下一页")
            page = _click_next(ctx, page, cfg, ctl, log)
            if page is None:
                break
            if not ctl.sleep(max(0.0, float(cfg.settle))):
                break

        # 第 1 页若已带整组图片地址，一次 GET 就够：不用等占位图，也不用翻页
        if n == 1 and cfg.whole_first:
            ctl.sleep(max(0.0, min(3.0, float(cfg.settle))))   # 给站点脚本一点时间铺数据
            urls, source = _collect_whole_image_urls(page)
            if urls and _render_image_list(page, urls, source, cfg, ctl, log):
                return
            if urls:
                log("整组图片页没渲染成功，回退到逐页合并")

        # 关键：先等占位图换成真实图片地址，否则抓到的全是 1×1 的 grey.gif
        _wait_real_images(page, ctl, wait_budget, log, n)

        html, sel, dropped = _grab_content(page)
        if len(html.strip()) < 20:
            log(f"第 {n} 页没有抓到正文，结束抓取")
            break
        dropped_total += dropped
        parts.append(html)
        used_sel = sel or used_sel

    if not parts:
        log("没有抓到任何内容，未生成合并页")
        return
    if dropped_total:
        log(f"注意：{dropped_total} 张图仍是 1×1 占位图（站点没来得及换成真实地址），已丢弃")

    body = "".join(parts)
    try:
        page.set_content(
            _merged_document(body, f"已合并 {len(parts)} 页 · 正在加载图片…"),
            wait_until="domcontentloaded",
        )
    except Exception as exc:
        detail = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        log(f"渲染合并页失败：{detail}")
        return

    try:
        pre = int(page.evaluate("() => document.images.length") or 0)
    except Exception:
        pre = 0
    budget = _wait_budget(pre)
    _wait_images(page, ctl, budget, min_width=1, steady=15.0, log=log)
    _retry_failed_images(page, ctl, log, rounds=1, budget=budget)
    broken = _prune_broken_images(page)
    try:
        count = int(page.evaluate("() => document.images.length") or 0)
        height = int(page.evaluate("() => document.documentElement.scrollHeight") or 0)
    except Exception:
        count, height = 0, 0
    view = _window_metrics(page)

    size_note = f"，可视区域 {view[0]}×{view[1]}" if view else ""
    log(f"已合并 {len(parts)} 页 → 同一页，共 {count} 张图片，页面高度 {height}px{size_note}"
        f"（正文容器 {used_sel or '自动'}）")
    if view and view[2] > view[0] + 1:
        log(f"合并页比窗口宽 {view[2] - view[0]}px，说明原始内容里有超宽元素；图片本身已限制在窗口内")
    if broken:
        log(f"有 {broken} 张图没加载出来，已从合并页移除（可先点停止，把“翻页后等待”调大再跑）")
    if not count:
        log("合并页里一张图都没有：多半是被站点拦了或还没加载出来，先别关窗口，按 F12 看 Network 里的状态码")
    _set_bar(page, f"已合并 {len(parts)} 页 · {count} 张图 · 高度 {height}px")

    if height:
        log(f"按 {cfg.speed}px/s 滚到底约需 {height // max(1, int(cfg.speed))} 秒；运行中拖“拖动调速”可随时加减")
    log("开始连续滚动到底，可用“暂停/停止”随时中断")
    _scroll_through(page, cfg, ctl, log, follow_pending=True,
                    pending_budget=budget, no_progress=60.0)


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


def _click_next(ctx, page, cfg: Config, ctl: Controller, log):
    """点击下一页，返回新的 page；失败返回 None。"""
    selector = (cfg.selector or "").strip()
    if not selector:
        log("未填写“下一页”选择器，无法翻页")
        return None

    before_pages = list(ctx.pages)
    url_before = page.url
    try:
        loc = page.locator(selector).first
        try:
            loc.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        loc.click(timeout=8000)
    except Exception as exc:
        first_line = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        log(f"点击“下一页”失败：{first_line}")
        return None

    # 等待地址变化 / 新标签页，最多 1.2 秒
    deadline = time.time() + 1.2
    while time.time() < deadline and not ctl.stopped:
        added = [pg for pg in ctx.pages if pg not in before_pages]
        if added:
            new_page = added[-1]
            log("检测到新标签页，已切换到新页面")
            try:
                page.close()
            except Exception:
                pass
            _wait_ready(new_page, ctl)
            _wait_images(new_page, ctl, max(6.0, float(cfg.settle) * 3))
            return new_page
        if page.url != url_before:
            break
        time.sleep(0.05)

    _wait_ready(page, ctl)
    _wait_images(page, ctl, max(6.0, float(cfg.settle) * 3))
    return page


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
                _run_merge(page, ctx, cfg, ctl, log)
                if not ctl.stopped:
                    log("合并完成：浏览器窗口会保持打开，可手动回看或点“重新滚动”再看一遍；点“停止”关闭浏览器")
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
        ttk.Label(bar, text="（合并 / 只滚动跑完后可再滚一遍）", foreground="#888780").pack(side="left")

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
        merge = label == PAGE_MODES[2]
        pure_scroll = label == PAGE_MODES[3]
        self.sel_box.configure(state="disabled" if (by_url or pure_scroll) else "normal")
        self.pages_box.configure(state="disabled" if pure_scroll else "normal")
        self.distance_box.configure(state="disabled" if (merge or pure_scroll) else "normal")
        self.whole_box.configure(state="normal" if merge else "disabled")
        if merge:
            self.status.set("合并模式：第 1 页若带整组图片地址就直接排一页，否则逐页抓取后拼成一页")
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
                    self.status.set("已滚到底 · 窗口保留中，可点“重新滚动”再看一遍")
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
