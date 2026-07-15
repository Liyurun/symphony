"""Symphony Textual TUI 客户端包。

对外导出应用类 ``SymphonyTUI`` 与便捷启动函数 ``run_tui``。
"""

from symphony.tui.app import SymphonyTUI, run_tui

__all__ = ["SymphonyTUI", "run_tui"]
