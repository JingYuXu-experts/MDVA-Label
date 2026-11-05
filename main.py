#!/usr/bin/env python
"""一键启动：双击 run.bat，或者 python main.py。

这里唯一做的事是把 src/ 放进 sys.path——不需要再设 PYTHONPATH，
也不需要先把项目 pip 安装进环境。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))


def main() -> int:
    try:
        from mdva.app import run
    except ImportError as exc:
        print(f"导入失败：{exc}\n")
        print("当前 Python：", sys.executable)
        print("缺的是这个解释器所在的包。请双击 install.bat 装依赖，")
        print("或者在已装好依赖的环境里运行：python main.py")
        return 1
    return run()


if __name__ == "__main__":
    sys.exit(main())
