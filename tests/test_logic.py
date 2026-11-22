# -*- coding: utf-8 -*-
"""不依赖真实浏览器/网络，用模拟的 playwright 对象验证 run_job 的主流程。

运行：.venv\\Scripts\\python.exe tests\\test_logic.py
"""

import os
import sys
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


# ----------------------------- 模拟 playwright ----------------------------- #
class FakeKeyboard:
    def press(self, key):
        pass


class FakeMouse:
    def __init__(self, page):
        self.page = page

    def move(self, x, y):
        pass

    def wheel(self, dx, dy):
        ctx = self.page.ctx
        ctx.scrolled[self.page.idx] = ctx.scrolled.get(self.page.idx, 0) + dy
        self.page._y = max(0, self.page._y + dy)


class FakeLocator:
    def __init__(self, page):
        self.page = page

    @property
    def first(self):
        return self

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def click(self, timeout=None):
        self.page.ctx.clicks += 1
        if self.page.ctx.new_tab_on_click:
            self.page.ctx.pages.append(FakePage(self.page.ctx, self.page.idx + 1))
        else:
            self.page.idx += 1
            self.page._y = 0


class FakePage:
    def __init__(self, ctx, idx=1):
        self.ctx = ctx
        self.idx = idx
        self._y = 0
        self.mouse = FakeMouse(self)
        self.keyboard = FakeKeyboard()
        self.closed = False

    @property
    def url(self):
        return f"https://fake.local/p{self.idx}"

    @property
    def viewport_size(self):
        return {"width": 1280, "height": 860}

    def goto(self, url, wait_until=None, timeout=None):
        self.ctx.gotos.append(url)
        self._y = 0
        import re
        m = re.search(r"_(\d+)\.html$", url)
        index = int(m.group(1)) if m else 1
        self.ctx.goto_index = index
        if self.ctx.not_found_from is not None and index >= self.ctx.not_found_from:
            return types.SimpleNamespace(status=404)
        return types.SimpleNamespace(status=200)

    def evaluate(self, script, arg=None):
        # 整组图片快路径：第 1 页的隐藏域里带着所有图片地址
        if "imgData" in script and self.ctx.whole_urls:
            return {
                "found": [{"name": "imgData", "raw": "\n".join(self.ctx.whole_urls)}],
                "visible": 4,
                "longest": "",
            }
        # 平滑滚动器（页面内 rAF）：假页面没法真跑 rAF，直接把“应走的位移”一次落地，
        # 这样断言距离、到底判定和真实环境一致。
        if "__autoScroll" in script:
            if "start(s, l)" in script and isinstance(arg, (list, tuple)):
                limit = int(arg[1] or 0)
                start = self._y
                goal = start + limit if limit > 0 else max(0, self.ctx.page_height - 860)
                moved = max(0, goal - start)
                self._y = start + moved
                self.ctx.scrolled[self.idx] = self.ctx.scrolled.get(self.idx, 0) + moved
            return True
        if "naturalWidth" in script:
            total = len(self.ctx.whole_urls) if self.ctx.whole_urls else 4
            return {"n": total, "loaded": total, "pending": 0, "failed": 0, "total": total}
        if "scrollHeight" in script and "innerHeight" in script:
            return self.ctx.page_height - 860
        if "innerHTML" in script and "sels" in script:
            if self.ctx.empty_from is not None and self.ctx.goto_index >= self.ctx.empty_from:
                return {"html": "", "sel": ""}
            return {"html": f"<img src='/img/{self.ctx.goto_index}.jpg'>", "sel": "#pageContainer"}
        if "document.body.innerHTML" in script:
            self.ctx.injected = arg
            return None
        if "document.images.length" in script:
            return len(self.ctx.whole_urls) if self.ctx.whole_urls else 4
        if "scrollHeight" in script:
            return self.ctx.page_height
        if "querySelector" in script:
            if self.ctx.empty_from is not None and self.ctx.goto_index >= self.ctx.empty_from:
                return True
            return False
        if "innerText" in script:
            return {"t": 200, "i": 5}
        if "scrollY" in script:              # 注意：scrollTop 里也含 "scrollTo"，必须先判 scrollY
            return self._y
        if "window.scrollTo" in script:
            self._y = 0
            return None
        if "scrollBy" in script:
            self.ctx.scrolled[self.idx] = self.ctx.scrolled.get(self.idx, 0) + (arg or 0)
            self._y += arg or 0
            return None
        return self._y

    def locator(self, selector):
        return FakeLocator(self)

    def set_content(self, html, wait_until=None):
        self.ctx.injected = html
        self._y = 0

    def bring_to_front(self):
        pass

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, new_tab_on_click=False, not_found_from=None, empty_from=None,
                 page_height=600, whole_urls=None):
        self.scrolled = {}
        self.clicks = 0
        self.gotos = []
        self.goto_index = 1
        self.injected = None
        self.page_height = page_height
        self.whole_urls = whole_urls
        self.new_tab_on_click = new_tab_on_click
        self.not_found_from = not_found_from
        self.empty_from = empty_from
        self.pages = [FakePage(self)]
        self.closed = False

    def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, ctx):
        self.ctx = ctx

    def launch_persistent_context(self, user_data_dir, **kwargs):
        return self.ctx

    def launch(self, **kwargs):
        return types.SimpleNamespace(new_context=lambda **kw: self.ctx, close=lambda: None)


class FakePlaywright:
    def __init__(self, ctx):
        self.chromium = FakeChromium(ctx)


class FakeSyncPlaywright:
    def __init__(self, ctx):
        self.ctx = ctx

    def __enter__(self):
        return FakePlaywright(self.ctx)

    def __exit__(self, *exc):
        return False


def install_fake(ctx):
    pkg = types.ModuleType("playwright")
    sub = types.ModuleType("playwright.sync_api")
    sub.sync_playwright = lambda: FakeSyncPlaywright(ctx)
    sub.Error = RuntimeError
    pkg.sync_api = sub
    sys.modules["playwright"] = pkg
    sys.modules["playwright.sync_api"] = sub


# -------------------------------- 用例 -------------------------------- #
def case_basic():
    ctx = FakeContext()
    install_fake(ctx)
    logs = []
    ctl = app.Controller()
    cfg = app.Config(url="https://fake.local", pages=3, distance=300, speed=6000,
                     settle=0, keep_login=True, selector="text=下一页")
    app.run_job(cfg, logs.append, ctl, headless=True)

    assert ctx.clicks == 2, f"应点击 2 次下一页，实际 {ctx.clicks}"
    for idx in (1, 2, 3):
        assert ctx.scrolled.get(idx) == 300, f"第 {idx} 页滚动了 {ctx.scrolled.get(idx)}px，应为 300"
    assert any("第 3/3 页" in line for line in logs), logs
    print("case_basic OK", ctx.scrolled, "clicks=", ctx.clicks)


def case_new_tab():
    ctx = FakeContext(new_tab_on_click=True)
    install_fake(ctx)
    logs = []
    ctl = app.Controller()
    cfg = app.Config(url="https://fake.local", pages=2, distance=200, speed=8000,
                     settle=0, keep_login=True)
    app.run_job(cfg, logs.append, ctl, headless=True)
    for idx in (1, 2):
        assert ctx.scrolled.get(idx) == 200, f"第 {idx} 页滚动了 {ctx.scrolled.get(idx)}px"
    assert any("新标签页" in line for line in logs), logs
    print("case_new_tab OK", ctx.scrolled)


def case_js_mode():
    ctx = FakeContext()
    install_fake(ctx)
    logs = []
    ctl = app.Controller()
    cfg = app.Config(url="https://fake.local", pages=1, distance=500, speed=20000,
                     settle=0, mode="js", keep_login=False)
    app.run_job(cfg, logs.append, ctl, headless=True)
    assert ctx.scrolled.get(1) == 500, ctx.scrolled
    print("case_js_mode OK", ctx.scrolled)


def case_stop():
    ctx = FakeContext()
    install_fake(ctx)
    logs = []
    ctl = app.Controller()
    cfg = app.Config(url="https://fake.local", pages=5, distance=2000, speed=800,
                     settle=2, keep_login=True)
    threading.Timer(0.3, ctl.stop).start()
    start = time.time()
    app.run_job(cfg, logs.append, ctl, headless=True)
    elapsed = time.time() - start
    assert elapsed < 3, f"停止耗时 {elapsed:.2f}s，响应过慢"
    assert ctx.clicks < 4, f"停止后仍继续翻页：{ctx.clicks}"
    print(f"case_stop OK 耗时 {elapsed:.2f}s clicks={ctx.clicks}")


def case_pause():
    ctx = FakeContext()
    install_fake(ctx)
    logs = []
    ctl = app.Controller()
    cfg = app.Config(url="https://fake.local", pages=1, distance=1000, speed=1000,
                     settle=0, keep_login=True)
    ctl.pause()                       # 一开始就暂停
    threading.Timer(1.2, ctl.resume).start()
    start = time.time()
    app.run_job(cfg, logs.append, ctl, headless=True)
    elapsed = time.time() - start
    assert ctx.scrolled.get(1) == 1000, ctx.scrolled
    assert elapsed > 1.0, f"暂停期间未真正等待，耗时 {elapsed:.2f}s"
    print(f"case_pause OK 耗时 {elapsed:.2f}s scrolled={ctx.scrolled}")


def case_url_helpers():
    assert app.paged_url("https://a.com/x/3738.html", 1) == "https://a.com/x/3738.html"
    assert app.paged_url("https://a.com/x/3738.html", 12) == "https://a.com/x/3738_12.html"
    assert app.paged_url("https://a.com/x/3738_02.html", 3) == "https://a.com/x/3738_03.html"
    assert app.parse_paged_url("https://a.com/x/3738_2.html")[1] == 2
    assert app.parse_paged_url("https://a.com/x/list?p=2") is None
    assert app.parse_paged_url("https://a.com/x/") is None
    print("case_url_helpers OK")


def case_url_mode():
    ctx = FakeContext()
    install_fake(ctx)
    logs = []
    ctl = app.Controller()
    cfg = app.Config(url="https://www.nnao.cc/Articles/Content/3738.html", pages=3,
                     distance=300, speed=6000, settle=0, page_mode="url", keep_login=True)
    app.run_job(cfg, logs.append, ctl, headless=True)

    assert ctx.gotos == [
        "https://www.nnao.cc/Articles/Content/3738.html",
        "https://www.nnao.cc/Articles/Content/3738_2.html",
        "https://www.nnao.cc/Articles/Content/3738_3.html",
    ], ctx.gotos
    assert ctx.clicks == 0, "URL 模式下不应点击任何按钮"
    assert ctx.scrolled.get(1) == 900, f"三页共应滚动 900px，实际 {ctx.scrolled}"
    print("case_url_mode OK", ctx.scrolled)


def case_url_mode_from_second():
    ctx = FakeContext()
    install_fake(ctx)
    logs = []
    ctl = app.Controller()
    cfg = app.Config(url="https://a.com/x/3738_2.html", pages=2, distance=100, speed=9000,
                     settle=0, page_mode="url", keep_login=True)
    app.run_job(cfg, logs.append, ctl, headless=True)
    assert [u.rsplit("/", 1)[-1] for u in ctx.gotos] == ["3738_2.html", "3738_3.html"], ctx.gotos
    print("case_url_mode_from_second OK", ctx.gotos)


def case_url_mode_404():
    ctx = FakeContext(not_found_from=3)
    install_fake(ctx)
    logs = []
    ctl = app.Controller()
    cfg = app.Config(url="https://a.com/x/3738.html", pages=5, distance=200, speed=9000,
                     settle=0, page_mode="url", keep_login=True)
    app.run_job(cfg, logs.append, ctl, headless=True)
    assert len(ctx.gotos) == 3, ctx.gotos
    assert any("404" in line for line in logs), logs
    print("case_url_mode_404 OK", ctx.gotos[-1])


def case_url_mode_empty():
    ctx = FakeContext(empty_from=3)
    install_fake(ctx)
    logs = []
    ctl = app.Controller()
    cfg = app.Config(url="https://a.com/x/3738.html", pages=6, distance=200, speed=9000,
                     settle=0, page_mode="url", keep_login=True)
    app.run_job(cfg, logs.append, ctl, headless=True)
    assert len(ctx.gotos) == 3, ctx.gotos
    assert any("正文容器是空的" in line for line in logs), logs
    print("case_url_mode_empty OK", ctx.gotos[-1])


def case_merge():
    ctx = FakeContext()
    install_fake(ctx)
    logs = []
    ctl = app.Controller()
    cfg = app.Config(url="https://www.nnao.cc/Articles/Content/3738.html", pages=3,
                     distance=3000, speed=5000, settle=0, page_mode="merge",
                     whole_first=False, keep_login=True)
    threading.Timer(0.6, ctl.stop).start()
    app.run_job(cfg, logs.append, ctl, headless=True)

    assert [u.rsplit("/", 1)[-1] for u in ctx.gotos] == [
        "3738.html", "3738_2.html", "3738_3.html"
    ], ctx.gotos
    assert ctx.injected and "merged-auto" in ctx.injected, "合并后的 HTML 没有注入页面"
    assert ctx.injected.count("<img") == 3, ctx.injected
    assert any("已合并 3 页" in line for line in logs), logs
    assert not ctx.scrolled, f"合并后不应自动滚动：{ctx.scrolled}"
    assert any("未自动滚动" in line for line in logs), logs
    print("case_merge OK", [u.rsplit("/", 1)[-1] for u in ctx.gotos])


def case_merge_whole_list():
    """快路径：第 1 页就带整组地址 → 只打开一次、不翻页、不自动滚动。"""
    urls = [f"https://fake.local/img/{i}.jpg" for i in range(1, 6)]
    ctx = FakeContext(whole_urls=urls)
    install_fake(ctx)
    logs = []
    ctl = app.Controller()
    cfg = app.Config(url="https://www.nnao.cc/Articles/Content/3738.html", pages=21,
                     distance=3000, speed=5000, settle=0, page_mode="merge", keep_login=True)
    threading.Timer(0.4, ctl.stop).start()
    app.run_job(cfg, logs.append, ctl, headless=True)

    assert ctx.gotos == ["https://www.nnao.cc/Articles/Content/3738.html"], ctx.gotos
    assert ctx.clicks == 0, "整组快路径不应点击下一页"
    assert ctx.injected and ctx.injected.count("<img") == 5, "应按原顺序排成 5 张图"
    assert [u for u in urls if u in ctx.injected] == urls, "图片顺序应保持原顺序"
    assert not ctx.scrolled, f"合并后不应自动滚动：{ctx.scrolled}"
    assert any("整组图片地址" in line for line in logs), logs
    assert any("未自动滚动" in line for line in logs), logs
    print("case_merge_whole_list OK", ctx.gotos)


def case_merge_stops_at_empty():
    ctx = FakeContext(empty_from=4)
    install_fake(ctx)
    logs = []
    ctl = app.Controller()
    cfg = app.Config(url="https://a.com/x/3738.html", pages=6, distance=3000, speed=5000,
                     settle=0, page_mode="merge", whole_first=False, keep_login=True)
    threading.Timer(0.6, ctl.stop).start()
    app.run_job(cfg, logs.append, ctl, headless=True)
    assert len(ctx.gotos) == 4, ctx.gotos
    assert any("正文容器是空的" in line for line in logs), logs
    assert ctx.injected.count("<img") == 3, "只应合并前 3 页"
    assert not ctx.scrolled, f"合并后不应自动滚动：{ctx.scrolled}"
    print("case_merge_stops_at_empty OK")


def case_scroll_only():
    """只滚动模式：只打开一页、不点击、滚到底后保持窗口，等“停止”再收工。"""
    ctx = FakeContext(page_height=3000)
    install_fake(ctx)
    logs = []
    ctl = app.Controller()
    cfg = app.Config(url="https://a.com/long.html", pages=9, distance=300,
                     speed=30000, settle=0, page_mode="scroll", keep_login=True)
    threading.Timer(0.5, ctl.stop).start()
    start = time.time()
    app.run_job(cfg, logs.append, ctl, headless=True)
    elapsed = time.time() - start

    assert ctx.gotos == ["https://a.com/long.html"], ctx.gotos
    assert ctx.clicks == 0, "只滚动模式不应点击下一页"
    assert ctx.scrolled.get(1, 0) >= 3000 - 860, ctx.scrolled
    assert any("只滚动模式" in line for line in logs), logs
    assert any("已滚到底" in line for line in logs), logs
    assert elapsed < 3, f"停止响应过慢：{elapsed:.2f}s"
    print(f"case_scroll_only OK 滚动 {ctx.scrolled} 耗时 {elapsed:.2f}s")


def case_rescroll():
    """窗口保留期间点“重新滚动”：同一页回顶部再滚一遍，不重新打开网址、不重新合并。"""
    ctx = FakeContext(page_height=3000)
    install_fake(ctx)
    logs, notes = [], []
    ctl = app.Controller()
    cfg = app.Config(url="https://a.com/long.html", speed=30000, settle=0,
                     page_mode="scroll", keep_login=True)

    def kick():
        time.sleep(0.35)
        ctl.request_rescroll()
        end = time.time() + 5                     # 等它真的滚完再停，避免靠猜时间
        while time.time() < end and not any("已重新滚到底" in l for l in logs):
            time.sleep(0.05)
        ctl.stop()

    threading.Thread(target=kick, daemon=True).start()
    app.run_job(cfg, logs.append, ctl, headless=True, notify=notes.append)

    assert ctx.gotos == ["https://a.com/long.html"], ctx.gotos
    assert ctx.scrolled.get(1, 0) >= 2 * (3000 - 860), ctx.scrolled
    assert any("重新滚动" in line for line in logs), logs
    assert any("已重新滚到底" in line for line in logs), logs
    assert "hold" in notes and "rescroll" in notes, notes
    print(f"case_rescroll OK 滚动 {ctx.scrolled} notes={notes}")


def case_live_speed():
    """运行中改速度：Controller 里的值立刻生效，非法值被忽略。"""
    ctl = app.Controller()
    assert ctl.speed_or(500) == 500, "没设置速度时应回落到配置值"
    ctl.set_speed(1200)
    assert ctl.speed_or(500) == 1200, ctl.speed_or(500)
    ctl.set_speed(0)                       # 非法值不改
    assert ctl.speed_or(500) == 1200, ctl.speed_or(500)
    ctl.set_speed("abc")
    assert ctl.speed_or(500) == 1200, ctl.speed_or(500)

    # 滚到一半提速：总距离不变，但明显更快收工
    ctx = FakeContext()
    install_fake(ctx)
    ctl = app.Controller()
    cfg = app.Config(url="https://fake.local", pages=1, distance=3000, speed=300,
                     settle=0, mode="wheel", keep_login=True)
    threading.Timer(0.3, lambda: ctl.set_speed(30000)).start()
    start = time.time()
    app.run_job(cfg, lambda *_: None, ctl, headless=True)
    elapsed = time.time() - start
    assert ctx.scrolled.get(1) == 3000, ctx.scrolled
    assert elapsed < 2.5, f"提速没生效？耗时 {elapsed:.2f}s"
    print(f"case_live_speed OK 耗时 {elapsed:.2f}s scrolled={ctx.scrolled}")


def case_mode_labels():
    assert app.PAGE_MODES[3] == "只滚动（滚到底）", app.PAGE_MODES
    assert app.App._page_mode_label("scroll") == app.PAGE_MODES[3]
    assert app.App._page_mode_label("merge") == app.PAGE_MODES[2]
    assert app.App._page_mode_label("click") == app.PAGE_MODES[0]
    print("case_mode_labels OK")


if __name__ == "__main__":
    case_basic()
    case_new_tab()
    case_js_mode()
    case_stop()
    case_pause()
    case_url_helpers()
    case_url_mode()
    case_url_mode_from_second()
    case_url_mode_404()
    case_url_mode_empty()
    case_merge()
    case_merge_whole_list()
    case_merge_stops_at_empty()
    case_scroll_only()
    case_rescroll()
    case_mode_labels()
    case_live_speed()
    print("\n全部逻辑用例通过")
