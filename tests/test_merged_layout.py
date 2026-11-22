# -*- coding: utf-8 -*-
"""合并页排版回归测试：图片必须居中、不被任何包裹层裁掉、页面不能横向溢出。

用真实 Playwright（headless）渲染，不需要网络：图片用内联 SVG，自带原始宽高。
运行：.venv\\Scripts\\python.exe tests\\test_merged_layout.py
"""

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import merge  # noqa: E402

SVG_SRC = ("<svg xmlns='http://www.w3.org/2000/svg' width='{w}' height='{h}'>"
           "<rect width='100%' height='100%' fill='green'/></svg>")


def svg(w: int, h: int) -> str:
    raw = SVG_SRC.format(w=w, h=h).encode()
    return "data:image/svg+xml;base64," + base64.b64encode(raw).decode()

# 三种典型情况：
# 1) 普通窄图（nnao 这类站点常见 600px 宽）
# 2) 带写死高度 + overflow:hidden 的包裹层（旧版本会把图裁掉一截）
# 3) 比窗口还宽的图（必须等比缩到窗口内，而不是溢出/偏到一边）
BODY = (
    f"<div><img src=\"{svg(600, 1400)}\"></div>"
    f"<div style=\"height:120px;overflow:hidden\"><img src=\"{svg(600, 1600)}\"></div>"
    f"<div><img src=\"{svg(2000, 900)}\" style=\"width:2000px;height:900px\"></div>"
)

MEASURE = """() => {
  const vw = innerWidth;
  const imgs = Array.from(document.querySelectorAll('#merged-auto img')).map(i => {
    const r = i.getBoundingClientRect();
    const host = i.parentElement.getBoundingClientRect();
    return {nw: i.naturalWidth, nh: i.naturalHeight, w: r.width, h: r.height,
            left: r.left, right: vw - r.right, hostH: host.height};
  });
  return {vw, sw: document.documentElement.scrollWidth, imgs,
          overflowY: document.documentElement.scrollHeight};
}"""


def main():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 900})
        page = ctx.new_page()
        page.set_content(merge._merged_document(BODY, "布局测试"), wait_until="domcontentloaded")
        page.wait_for_function(
            "() => Array.from(document.images).every(i => i.complete && i.naturalWidth > 0)",
            timeout=10000,
        )
        m = page.evaluate(MEASURE)

    assert m["sw"] <= m["vw"] + 1, f"页面横向溢出：文档宽 {m['sw']} > 窗口 {m['vw']}"
    assert len(m["imgs"]) == 3, m["imgs"]

    for idx, im in enumerate(m["imgs"], 1):
        # 图片按原始比例显示（宽度不超过窗口时保持原始尺寸）
        expect_w = min(im["nw"], m["vw"] - 24)          # 容器左右各 12px padding
        assert abs(im["w"] - expect_w) <= 1, f"第 {idx} 张图宽 {im['w']}，应为 {expect_w}"
        # 高度必须按比例完整显示，不能被包裹层裁掉
        assert abs(im["h"] - expect_w * im["nh"] / im["nw"]) <= 2, \
            f"第 {idx} 张图高 {im['h']}，应为等比高度 {expect_w * im['nh'] / im['nw']:.0f}"
        # 左右留白相等 = 居中
        assert abs(im["left"] - im["right"]) <= 1, \
            f"第 {idx} 张图不居中：左 {im['left']:.1f} 右 {im['right']:.1f}"

    print("合并页排版 OK：窗口", m["vw"], "px，文档宽", m["sw"], "px")
    for idx, im in enumerate(m["imgs"], 1):
        print(f"  图{idx}: {im['nw']}x{im['nh']} → 显示 {im['w']:.0f}x{im['h']:.0f}"
              f"  左 {im['left']:.0f} 右 {im['right']:.0f}")


if __name__ == "__main__":
    main()
