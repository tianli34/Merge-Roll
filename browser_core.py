# -*- coding: utf-8 -*-
"""各模式共用的浏览器能力：翻页、等图、平滑滚动、页面信息。

合并功能在 merge.py；这里只放“点击/URL 翻页、滚动、只滚动”也会用到的代码，
不反向依赖 app.py，避免 app ↔ merge 循环导入。
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app import Config, Controller


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
# 浏览器动作
# --------------------------------------------------------------------------- #
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


def _is_merged_page(page) -> bool:
    """合并页是我们自己 set_content 的文档，不依赖站点脚本，脚本滚动一定可用。"""
    try:
        return bool(page.evaluate("() => !!document.getElementById('merged-auto')"))
    except Exception:
        return False


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


def _scroll_through(page, cfg: Config, ctl: Controller, log, max_minutes: float = 20.0,
                    follow_pending: bool = False,
                    pending_budget: float = 60.0, no_progress: float = 60.0) -> None:
    """一直滚到页面底部（合并模式下用），高度随图片加载增长会实时重算。

    follow_pending=True 时，滚到底但还有图片在下载就不算结束：等一会儿再继续，
    免得页面底部后半截图片还没下完就停了。pending_budget 是这份等待的总预算，
    出图慢的站点要按张数放大（见 _wait_budget）；no_progress 是“多久没新图就算它断了”。
    """
    # 合并页强制用脚本滚动：滚轮按次落地、一阶一阶跳（实测 75% 的帧是静止的），
    # 在长图上看起来就像图片在抖；而合并页是本地文档，rAF 逐帧推进一定可用。
    merged = _is_merged_page(page)
    if (merged or _use_smooth(cfg)) and _smooth_install(page):
        if merged and not _use_smooth(cfg):
            log("合并页改用“脚本滚动”逐帧推进（滚轮模式会一阶一阶跳，看着像图片在抖）")
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