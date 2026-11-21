# -*- coding: utf-8 -*-
"""整组图片（一页取全）相关纯逻辑测试，不需要网络/浏览器。

运行：.venv\\Scripts\\python.exe tests\\test_whole_list.py
"""

import base64
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402

BASE = "https://www.nnao.cc/Articles/Content/3738.html"
PATHS = [
    "/LoadImage.ashx/aaa111.jpg",
    "/LoadImage.ashx/bbb222.jpg",
    "/LoadImage.ashx/ccc333.jpg",
    "/LoadImage.ashx/ddd444.jpg",
    "/LoadImage.ashx/eee555.jpg",
]


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def test_b64_pipe_list():
    raw = "|".join(b64(p) for p in PATHS)
    assert app._parse_img_list(raw) == PATHS, app._parse_img_list(raw)
    print("ok  base64 竖线列表")


def test_json_array():
    raw = 'var imgs = ["/a/1.jpg", "/a/2.png", "https://cdn.x.com/3.webp"];'
    got = app._parse_img_list(raw)
    assert got == ["/a/1.jpg", "/a/2.png", "https://cdn.x.com/3.webp"], got
    print("ok  脚本里的图片数组")


def test_line_list():
    raw = "\n".join(PATHS)
    assert app._parse_img_list(raw) == PATHS
    print("ok  逐行列表")


def test_not_a_list():
    assert app._parse_img_list("short") == []
    assert app._parse_img_list("<html>这是一篇普通文章</html>") == []
    # base64 解出来不是图片（例如 ViewState）不能当成图片地址
    assert app._parse_img_list("|".join([b64("not an image"), b64("still not")])) == []
    print("ok  非图片数据不会被误判")


def test_normalize():
    urls = ["/x/1.jpg", "/x/1.jpg", "2.jpg", "https://other/3.jpg", "/grey.gif", "/img/blank.png"]
    got = app._normalize_urls(urls, BASE)
    assert got == [
        "https://www.nnao.cc/x/1.jpg",
        "https://www.nnao.cc/Articles/Content/2.jpg",
        "https://other/3.jpg",
    ], got
    print("ok  补绝对地址 / 去重 / 丢占位图")


def test_document_order():
    doc = app._images_document(["https://a/1.jpg", "https://a/2.jpg?a=1&b=2"], "note")
    assert doc.index("1.jpg") < doc.index("2.jpg"), "顺序必须保持"
    assert "a=1&amp;b=2" in doc, "地址里的 & 要转义"
    assert doc.count("<img ") == 2
    assert 'data-src="https://a/1.jpg"' in doc, "要留原始地址给重试（走缓存）"
    assert "#merged-auto" in doc and "merged-bar" in doc
    print("ok  同一页按顺序排 <img>，参数被正确转义，原始地址留底")


def test_wait_budget():
    assert app._wait_budget(81) == 243, app._wait_budget(81)
    assert app._wait_budget(4) == 60, "张数少时走 60 秒下限"
    assert app._wait_budget(1000) == 420, "再长也要封顶"
    print("ok  等图预算按张数放大（81 张 → 243s），有上下限")


class WaitPage:
    """永远差 1 张下不来：验证 _wait_images 不会超出预算死等。"""

    def __init__(self):
        self.calls = 0

    def evaluate(self, script, arg=None):
        self.calls += 1
        return {"n": 3, "pending": 1, "failed": 0}


class WaitCtl:
    stopped = False

    def sleep(self, seconds):
        time.sleep(min(0.05, seconds))
        return True


def test_wait_images_budget():
    page = WaitPage()
    t0 = time.time()
    app._wait_images(page, WaitCtl(), 1.2, min_width=1, label="测试 ", steady=30.0,
                     log=lambda s: None)
    spent = time.time() - t0
    assert spent < 4, f"超出预算：{spent:.1f}s"
    assert page.calls >= 2, "要按 0.4 秒轮询，而不是一次就放弃"
    print(f"ok  _wait_images 按预算轮询（{page.calls} 次 / {spent:.1f}s），不会死等")


class FakePage:
    def __init__(self, payload):
        self.payload = payload
        self.url = BASE

    def evaluate(self, script, arg=None):
        return self.payload


def test_collect_whole():
    raw = "|".join(b64(p) for p in PATHS)
    page = FakePage({"found": [{"name": "imgData", "raw": raw}], "visible": 4, "longest": ""})
    urls, src = app._collect_whole_image_urls(page)
    assert len(urls) == 5 and src == "#imgData", (urls, src)
    assert urls[0] == "https://www.nnao.cc/LoadImage.ashx/aaa111.jpg"
    print("ok  第 1 页取出整组地址（5 张 > 本页 4 张）")


def test_collect_rejects_same_page():
    """整组数量不比本页多，说明只是本页那几张，应该照旧逐页合并。"""
    raw = "|".join(b64(p) for p in PATHS[:4])
    page = FakePage({"found": [{"name": "imgData", "raw": raw}], "visible": 4, "longest": ""})
    urls, src = app._collect_whole_image_urls(page)
    assert urls == [] and src == "", (urls, src)
    print("ok  不把“本页 4 张”误判成整组")


def test_collect_script_fallback():
    longest = 'var list = ["/i/1.jpg","/i/2.jpg","/i/3.jpg","/i/4.jpg",' \
              '"/i/5.jpg","/i/6.jpg","/i/7.jpg","/i/8.jpg"];'
    page = FakePage({"found": [], "visible": 2, "longest": longest})
    urls, src = app._collect_whole_image_urls(page)
    assert len(urls) == 8 and "脚本" in src, (urls, src)
    print("ok  脚本数组兜底（>=8 张且远多于本页）")


def test_collect_handles_garbage():
    class Broken:
        url = BASE

        def evaluate(self, script, arg=None):
            raise RuntimeError("boom")

    assert app._collect_whole_image_urls(Broken()) == ([], "")
    assert app._collect_whole_image_urls(FakePage(None)) == ([], "")
    print("ok  拿不到数据时安全返回空")


if __name__ == "__main__":
    for fn in [
        test_b64_pipe_list, test_json_array, test_line_list, test_not_a_list,
        test_normalize, test_document_order, test_wait_budget, test_wait_images_budget,
        test_collect_whole, test_collect_rejects_same_page, test_collect_script_fallback,
        test_collect_handles_garbage,
    ]:
        fn()
    print("\n全部通过")
