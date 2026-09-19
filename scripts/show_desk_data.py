"""确认界面显示的是**真实数据**——"能渲染"与"有数据"是两回事。"""
import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

# 同两个冒烟脚本：stdout 走管道时用系统 ANSI 码页（cp936），中文指标名会乱码。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

script = Path("src/desk/ui/app.py").resolve()
at = AppTest.from_file(str(script), default_timeout=120)
at.run()

print("=== 总览页 ===")
for m in at.metric:
    print("   %-10s %s" % (m.label, m.value))

radio = at.sidebar.radio[0]
for page in ("系统状态", "动态本体", "文献库"):
    option = next(o for o in radio.options if str(o).endswith(page))
    radio.set_value(option).run()
    print("\n=== %s ===" % page)
    for m in at.metric:
        print("   %-12s %s" % (m.label, m.value))
    if at.dataframe:
        for df in at.dataframe:
            try:
                print("   表格形状:", df.value.shape)
            except Exception:  # noqa: BLE001
                pass
