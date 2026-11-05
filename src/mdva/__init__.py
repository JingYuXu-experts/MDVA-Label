"""多模态科研数据可视化与并行分析核心引擎。

与界面层完全解耦：本包不导入任何 GUI 库，可被 CLI、服务层或桌面/前端壳调用。
分层：io(L1) -> render(L2) -> analysis/pipeline(L3)。
"""

__version__ = "0.1.0"

from .core.model import (
    DataItem,
    EventTable,
    Modality,
    Slice,
    TimeSeries,
)

__all__ = [
    "DataItem",
    "EventTable",
    "Modality",
    "Slice",
    "TimeSeries",
    "__version__",
]
