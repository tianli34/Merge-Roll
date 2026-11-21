# -*- coding: utf-8 -*-
"""冒烟测试：完整走一遍 main()，800ms 后自动关窗，验证启动不再抛异常。"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tkinter as tk
import app

_orig = tk.Tk.mainloop

def _auto_close(self, *a, **kw):
    self.after(800, self.destroy)
    _orig(self, *a, **kw)

tk.Tk.mainloop = _auto_close

cfg_path = os.path.join(app.APP_DIR, "config.json")
backup = open(cfg_path, encoding="utf-8").read() if os.path.exists(cfg_path) else None

results = []
try:
    for mode in ("merge", "url", "click", "scroll"):
        raw = json.loads(backup) if backup else {}
        raw["page_mode"] = mode
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, ensure_ascii=False, indent=2)
        app.main()
        results.append(mode)
        print(f"startup OK: {mode}")
finally:
    if backup is not None:
        open(cfg_path, "w", encoding="utf-8").write(backup)

assert not os.path.exists(os.path.join(app.APP_DIR, "error.log")), "启动写了 error.log"
print("全部启动冒烟通过:", results)
