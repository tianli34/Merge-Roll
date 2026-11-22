# -*- coding: utf-8 -*-
"""合并成一页：整组图片解析、合并页渲染、逐页抓取（合并后不自动滚动）。

只依赖 browser_core 的通用能力，不反过来依赖 app.py；
app.py 通过 run_merge() 调用。
"""

from __future__ import annotations

import base64
import html as html_lib
import re
import time
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from browser_core import (
    CONTENT_SELECTORS,
    _click_next,
    _goto,
    _image_stats,
    _set_bar,
    _wait_images,
    _window_metrics,
    paged_url,
    parse_paged_url,
)

if TYPE_CHECKING:
    from app import Config, Controller


# --------------------------------------------------------------------------- #
# 合并页：本地文档，不带站点 CSS / JS
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# 旧路径：逐页抓正文
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# 图片等待 / 重试 / 清理
# --------------------------------------------------------------------------- #
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


def _wait_budget(total: int, per_image: float = 3.0,
                 floor: float = 60.0, cap: float = 420.0) -> float:
    """按张数估算等图预算：站点出图慢时（实测 3~5 秒/张）固定预算必然不够。

    per_image 取 3 秒是实测中位值：81 张 → 243 秒，够把整组下完；
    张数少时不低于 floor（60 秒），再长也不超过 cap（7 分钟）。
    """
    return max(float(floor), min(float(cap), max(0, int(total)) * per_image))


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
# 快路径：整组地址直接排一页
# --------------------------------------------------------------------------- #
def _render_image_list(page, urls, source: str, ctl: Controller, log) -> bool:
    """把整组地址渲染成一页（合并完成即止，不自动滚动）。渲染成功返回 True。"""
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

    started = time.time()
    budget = _wait_budget(total)
    # 先等一小段打个底（够排出版面、算出大概高度）；合并后不自动滚动，
    # 所以剩下的图只能在这里等，滚动不再是“边滚边下”的缓冲。
    warm = min(budget, 90.0)
    log(f"正在等图片加载（同一浏览器会话内直接引用地址，不下载到本地）："
        f"先等最多 {int(warm)} 秒打个底；合并后不自动滚动，剩下的图会在窗口保留期间继续加载")
    _wait_images(page, ctl, warm, min_width=1, label=f"整组 {total} 张 · ", steady=15.0,
                 log=log)
    if ctl.stopped:
        return True
    # 先补一轮“明确失败”的（原地址走缓存，代价低）；大批仍在下载的不折腾，
    # 让它们继续跑——重挂反而会把已经在传的连接推倒重来。
    if (_image_stats(page) or (0, 0, 0, 0))[2]:
        _retry_failed_images(page, ctl, log, rounds=1, budget=min(45.0, budget))

    stats = _image_stats(page) or (0, 0, 0, total)
    if stats[1] and not ctl.stopped:
        remaining = max(0.0, budget - (time.time() - started))
        if remaining > 0:
            log(f"还有 {stats[1]} 张在下载（站点出图慢），继续等它们下完（不会自动滚动）")
            _wait_images(page, ctl, remaining, min_width=1,
                         label=f"整组 {total} 张 · ", steady=15.0, log=log)
            if (_image_stats(page) or (0, 0, 0, 0))[2] and not ctl.stopped:
                _retry_failed_images(page, ctl, log, rounds=2, budget=min(150.0, remaining))

    try:
        count = int(page.evaluate("() => document.images.length") or 0)
        height = int(page.evaluate("() => document.documentElement.scrollHeight") or 0)
    except Exception:
        count, height = 0, 0
    view = _window_metrics(page)

    size_note = f"，可视区域 {view[0]}×{view[1]}" if view else ""
    log(f"已排成同一页：{count}/{total} 张图片，页面高度 {height}px{size_note}")
    if not count:
        log("一张图都没加载出来：先别关窗口，按 F12 看 Network 里图片的状态码（常见是 403 / 需要登录）")
    _set_bar(page, f"整组 {total} 张 · 已排 {count} 张 · 高度 {height}px")

    final = _image_stats(page)
    if final:
        notes = []
        if final[2]:
            notes.append(f"{final[2]} 张坏图")
        if final[1]:
            notes.append(f"{final[1]} 张还没下完")
        tail = f"（{'，'.join(notes)}）" if notes else "（全部到位）"
        log(f"合并页已就绪：{final[0]}/{final[3]} 张图片可见{tail}；未自动滚动")
        _set_bar(page, f"整组 {total} 张 · 已加载 {final[0]}/{final[3]} · 高度 {height}px")
        if final[2]:
            removed = _prune_broken_images(page)
            if removed:
                log(f"{removed} 张确实没取到，已从页面移除；再跑一次可以让它们走缓存重下")
    else:
        log("合并页已就绪；未自动滚动")
    return True


# --------------------------------------------------------------------------- #
# 入口：合并 / 旧路径
# --------------------------------------------------------------------------- #
def run_merge(page, ctx, cfg: Config, ctl: Controller, log) -> None:
    """把多页正文抓下来拼成同一个页面，停在顶部给用户看（不自动滚动）。"""
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
            if urls and _render_image_list(page, urls, source, ctl, log):
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
        log(f"合并页已就绪：{len(parts)} 页、{count} 张图、高度 {height}px；未自动滚动，"
            f"可用滚轮手动看，或点“重新滚动”按当前速度看一遍")