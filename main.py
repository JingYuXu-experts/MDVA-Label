#!/usr/bin/env python
"""一键启动：双击 run.bat，或者 python main.py。

默认初始化本地工作区并打开网页版；带参数时转交 CLI。
直接加载 src/，不需要先安装项目。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))


def main() -> int:
    try:
        from mdva.cli import main as run
    except ImportError as exc:
        print(f"导入失败：{exc}\n")
        print("当前 Python：", sys.executable)
        print("缺的是这个解释器所在的包。请双击 install.bat 装依赖，")
        print("或者在已装好依赖的环境里运行：python main.py")
        return 1
    if len(sys.argv) > 1:
        return run(sys.argv[1:])
    root = Path(__file__).resolve().parent
    (root / 'data').mkdir(exist_ok=True)
    args = ['serve', '--data', str(root / 'data'), '--out', str(root / 'out'), '--open', '--reuse']
    source = root / 'datas of importance' / 'Situation Awareness Dataset'
    if source.is_dir():
        args.extend(['--source', 'ssafd=' + str(source)])
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
