# -*- coding: utf-8 -*-
"""只验证界面能否正常构建（不启动浏览器）。"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tkinter as tk
import app

# on_close() 会把界面上的值写回 config.json，测试结束要还原，别改掉用户的设置
_cfg_path = os.path.join(app.APP_DIR, "config.json")
_backup = open(_cfg_path, encoding="utf-8").read() if os.path.exists(_cfg_path) else None

root = tk.Tk()
root.withdraw()
ui = app.App(root)
root.update_idletasks()
root.update()
cfg = ui._read_config()
print("config:", cfg)
assert cfg is not None and cfg.pages >= 1

# 只滚动模式：页数 / 距离 / 选择器都不用填，模式值映射成 scroll
ui.vars["page_mode"].set(app.PAGE_MODES[3])
ui._sync_page_mode()
root.update()
cfg_scroll = ui._read_config()
assert cfg_scroll is not None and cfg_scroll.page_mode == "scroll", cfg_scroll
assert str(ui.pages_box.cget("state")) == "disabled"
assert str(ui.distance_box.cget("state")) == "disabled"
assert str(ui.sel_box.cget("state")) == "disabled"
print("scroll mode UI OK:", cfg_scroll.page_mode)

# “重新滚动”按钮：默认灰，进入 hold 才可点，开始重新滚动又置灰
assert str(ui.rescroll_btn.cget("state")) == "disabled"
ui.q.put(("hold", ""))
ui._drain()
root.update()
assert str(ui.rescroll_btn.cget("state")) == "normal"
assert "重新滚动" in ui.status.get()
ui.q.put(("rescroll", ""))
ui._drain()
root.update()
assert str(ui.rescroll_btn.cget("state")) == "disabled"
print("rescroll button OK:", ui.status.get())

# 调速条：滑块 ↔ 输入框双向同步，并立刻写进 Controller（运行中也能改）
ui.speed_scale_var.set(1200)
ui._on_speed_scale("1200")
root.update()
assert ui.vars["speed"].get() == "1200", ui.vars["speed"].get()
ui.vars["speed"].set("800")
root.update()
assert abs(ui.speed_scale_var.get() - 800) < 0.5, ui.speed_scale_var.get()
assert ui.speed_hint.get() == "800 px/秒", ui.speed_hint.get()


class _FakeThread:
    def is_alive(self):
        return True


ui.ctl = app.Controller()
ui.thread = _FakeThread()
ui._on_speed_scale("1500")
assert ui.ctl.speed_or(1) == 1500, ui.ctl.speed_or(1)
if ui._speed_after is not None:
    root.after_cancel(ui._speed_after)
    ui._speed_after = None
print("speed slider OK:", ui.speed_hint.get())

ui.log("界面测试")
root.update()
ui.thread = None                     # 别让 on_close 去 join 假线程
ui.on_close()
if _backup is not None:
    with open(_cfg_path, "w", encoding="utf-8") as fh:
        fh.write(_backup)
print("UI OK")
