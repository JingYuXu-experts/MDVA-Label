"""拖动重绘路径的基线 / 优化版对比基准。

基线 = 旧实现：滑块每来一个事件就把整张网格销毁重建（渲染全部格子 + 新建 Tk 图片 + 重排控件）。
优化版 = 新实现：控件只建一次，只重画"会随游标变"的格子，原地 paste 更新图片，事件合并到下一帧。

跑法：E:/ANACONDA/envs/mdva/python.exe code_optimization/bench_drag.py
"""

import statistics
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import tkinter as tk  # noqa: E402
from PIL import Image, ImageTk  # noqa: E402

from mdva import render  # noqa: E402
from mdva.app import Viewer  # noqa: E402

STEPS = 40
COUNT = {"render": 0, "photo": 0, "destroy": 0}


def photo_pixels(photo):
    """把 Tk 图片真正写出来再比。

    np.asarray(PhotoImage) 只会得到一个 0 维对象数组，拿它比"逐像素一致"
    是自欺欺人——比的是不是同一份数据，而是对象身份。
    """
    import tempfile

    tmp = Path(tempfile.mktemp(suffix=".ppm"))
    photo._PhotoImage__photo.write(str(tmp), format="ppm")
    try:
        return np.array(Image.open(tmp))
    finally:
        tmp.unlink(missing_ok=True)


def instrument():
    """给 render_item 和 PhotoImage 计数，用来证明"少干了活"而不只是"看着快了"。

    两条路径都必须走被替换后的名字，否则计数会漏掉一边。
    """
    real_ri = render.render_item
    real_pi = ImageTk.PhotoImage

    def ri(*a, **kw):
        COUNT["render"] += 1
        return real_ri(*a, **kw)

    def pi(*a, **kw):
        if a or kw:                        # 空构造只是占位，不算一次分配
            COUNT["photo"] += 1
        return real_pi(*a, **kw)

    render.render_item = ri                # app 里是 from . import render，改模块属性即可
    ImageTk.PhotoImage = pi
    return real_ri


def legacy_paint(v):
    """旧 refresh() 的等价实现：全销毁 + 全重画 + 全新建图片。"""
    size = v.cell_size()
    sl = v.window()
    t = None if v.mode.get() == "切片序号" else v.cursor()
    v.photos_keep.clear()
    for child in v.grid.winfo_children():
        COUNT["destroy"] += 1
        child.destroy()
    items = v.selected()
    cols = 2
    for n, item in enumerate(items):
        row, col = divmod(n, cols)
        cell = tk.Frame(v.grid, bg="#18181c")
        cell.grid(row=row * 2, column=col, padx=8, pady=(8, 0), sticky="n")
        res = render.render_item(item, size=size, window=sl, style=v.style(),
                                   cursor_t=t)
        img = Image.fromarray(np.ascontiguousarray(v._transform(res.composited())))
        photo = ImageTk.PhotoImage(img)
        v.photos_keep.append(photo)
        view = tk.Label(cell, image=photo, bg="#18181c")
        view.pack()
        tk.Label(v.grid, text=f"{item.id}   {res.subtitle}", fg="#c8ccd8",
                 bg="#101014", wraplength=size[0],
                 font=("Microsoft YaHei", 9)).grid(row=row * 2 + 1, column=col, sticky="w")


def measure(fn, v, label):
    """把滑块从头拖到尾，记录每一步耗时与总工作量。"""
    lo, hi = float(v.slider["from"]), float(v.slider["to"])
    for step in range(STEPS):
        v.slider.set(lo + (hi - lo) * step / (STEPS - 1))
        v.cursor_label.config(text="")
        fn(v)
        v.root.update_idletasks()
    return v


def timed(fn, v):
    before = dict(COUNT)
    marks = []
    for step in range(STEPS):
        v.slider.set(step / (STEPS - 1) * float(v.slider["to"]))
        t0 = time.perf_counter()
        fn(v)
        v.root.update_idletasks()
        marks.append((time.perf_counter() - t0) * 1000)
    return marks, {k: COUNT[k] - before[k] for k in COUNT}


def main():
    instrument()
    root = tk.Tk()
    root.withdraw()
    v = Viewer(root, autoload=False)
    v.load(str(ROOT / "data"))
    root.deiconify()
    for _ in range(8):
        root.update()
    v.check_all(False)
    top = v.tree.get_children()[0]
    v.tree.focus(top)
    v.check_group(True)
    root.update_idletasks()
    v.rebuild()
    root.update_idletasks()
    n_items = len(v.selected())
    print(f"分组：航空航天　勾选 {n_items} 路　单格 {v.cell.get()}")

    # ---- 正确性：同一游标位置，两条路径必须逐像素一致
    v.slider.set(0.42 * float(v.slider["to"]))
    v.paint(all_tiles=True)
    root.update_idletasks()
    new_bytes = [photo_pixels(p) for p in v.photos]
    v.photos_keep = []
    legacy_paint(v)
    root.update_idletasks()
    old_bytes = [photo_pixels(p) for p in v.photos_keep]
    same = len(old_bytes) == len(new_bytes) and all(
        np.array_equal(a, b) for a, b in zip(old_bytes, new_bytes))
    print(f"逐像素一致：{'PASS' if same else 'FAIL'}（{len(new_bytes)} 格）")

    # ---- 基线
    v.rebuild()
    root.update_idletasks()
    base_ms, base_work = timed(legacy_paint, v)
    print(f"基线（旧：销毁重建）    每步中位 {statistics.median(base_ms):6.2f} ms  "
          f"p95 {sorted(base_ms)[int(STEPS*0.95)]:6.2f} ms  总 {sum(base_ms):7.1f} ms  "
          f"渲染 {base_work['render']} 次  新建图片 {base_work['photo']} 张  "
          f"销毁控件 {base_work['destroy']} 个")
    base_peak = max(base_ms)

    # ---- v1：控件不重建 + 只重画会变的格子 + 原地 paste
    v.rebuild()
    root.update_idletasks()
    v1_ms, v1_work = timed(lambda x: x.paint(all_tiles=False), v)
    print(f"v1（选择性重画）        每步中位 {statistics.median(v1_ms):6.2f} ms  "
          f"p95 {sorted(v1_ms)[int(STEPS*0.95)]:6.2f} ms  总 {sum(v1_ms):7.1f} ms  "
          f"渲染 {v1_work['render']} 次  新建图片 {v1_work['photo']} 张  "
          f"销毁控件 {v1_work['destroy']} 个")

    # ---- v2：事件合并（一次拖动 = 一次重画，而不是 40 次）
    v.rebuild()
    root.update_idletasks()
    v.slider.set(0.0)
    v.paint(all_tiles=True)
    root.update_idletasks()
    for k in COUNT:
        COUNT[k] = 0
    t0 = time.perf_counter()
    for step in range(1, STEPS + 1):
        v.slider.set(step / STEPS * float(v.slider["to"]))
        v._on_slider(str(step))          # 模拟 Tk 的 command 回调，中途不泵事件
    # 拖动期间主循环被事件占住，只有空闲时才 flush
    flushed = 0
    deadline = time.perf_counter() + 2.0
    while time.perf_counter() < deadline and flushed < 3:
        root.update_idletasks()
        flushed += 1
        time.sleep(0.02)
    burst = (time.perf_counter() - t0) * 1000
    print(f"v2（{STEPS} 次拖动事件合并后）总 {burst:7.1f} ms  "
          f"渲染 {COUNT['render']} 次（基线同量事件要 {n_items * STEPS} 次）  "
          f"新建图片 {COUNT['photo']} 张")

    print(f"\n加速比：v1 {sum(base_ms)/max(sum(v1_ms),1e-9):.2f}x   "
          f"最坏一步 {base_peak:.1f} ms -> {max(v1_ms):.1f} ms")

    # ---- 最坏情况：整组 60 张静态图（分割输出），拖游标时它们一张都不该重画
    v.check_all(False)
    seg = next(i for i in v.tree.get_children() if v.group_label[i] == "图像分割")
    v.tree.focus(seg)
    v.check_group(True)
    root.update_idletasks()          # 让勾选排的那次重建先落地，别混进计数
    n_seg = len(v.selected())

    def one_drag(painter):
        for k in COUNT:
            COUNT[k] = 0
        t0 = time.perf_counter()
        v.slider.set(0.3 if painter is legacy_paint else 0.6)
        painter(v)
        root.update_idletasks()
        return (time.perf_counter() - t0) * 1000, COUNT["render"]

    leg_ms, leg_r = one_drag(lambda x: legacy_paint(x))
    opt_ms, opt_r = one_drag(lambda x: x.paint(all_tiles=False))
    print(f"静态组（{n_seg} 路图像）拖一步：基线 {leg_ms:6.1f} ms / 重画 {leg_r:3d} 格   "
          f"优化后 {opt_ms:6.1f} ms / 重画 {opt_r:3d} 格")

    tracemalloc.start()
    for step in range(STEPS):
        v.slider.set(step / (STEPS - 1) * float(v.slider["to"]))
        v.paint(all_tiles=False)
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"当前选择下再拖一轮的 Python 侧峰值内存：{peak/1024:.0f} KiB")
    root.destroy()
    return 0 if same else 1


if __name__ == "__main__":
    raise SystemExit(main())
