"""桌面查看器：按学科分组浏览、跨模态时间联动、批量分析与数据整理。

只用标准库 tkinter，不依赖 wxPython，也不需要额外装包——
"双击就能跑"这件事不应该取决于环境是否干净。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import numpy as np

from . import io, render
from .core.model import Modality, Slice
from .io.timeutil import from_utc_seconds

MAX_AUTO_CHECK = 12
_REPO = Path(__file__).resolve().parents[2]

MODALITY_CN = {"image": "图像", "cube": "立方体", "series": "时序",
               "event": "事件", "video": "视频"}
INTERVAL_CN = {"minmax": "极值", "percent": "百分位", "sigma": "西格玛",
               "zscale": "Z 缩放", "median": "中值"}
STRETCH_CN = {"linear": "线性", "sqrt": "平方根", "power": "幂", "gamma": "伽马",
              "log": "对数", "asinh": "反双曲", "histeq": "直方图均衡",
              "clahe": "自适应均衡"}
COLORMAP_CN = {"gray": "灰度", "viridis": "翠绿", "inferno": "炎",
               "plasma": "等离子", "hot": "热力", "cool": "冷色"}
ANALYZER_CN = {
    "bg_stats": "背景统计：中位水平与噪声标准差",
    "image_quality": "图像质量：饱和度、有效位深、坏像元",
    "star_detect": "星点 / 热点检测",
    "cosmic_reject": "宇宙线野值剔除",
    "dynamic_threshold": "动态阈值告警",
    "rolling_stats": "滑动均值与标准差",
    "changepoint": "变点检测（均值突变）",
    "iforest": "孤立森林异常检测",
    "correlation": "通道互相关",
}
_INTERVAL_OF = {v: k for k, v in INTERVAL_CN.items()}
_STRETCH_OF = {v: k for k, v in STRETCH_CN.items()}
_CMAP_OF = {v: k for k, v in COLORMAP_CN.items()}


def _cn(mapping: dict, key: str) -> str:
    return mapping.get(key, key or "--")


def _short_time(seconds) -> str:
    """列表里放不下一整串 ISO 时间，留"月-日 时:分"，够定位了。"""
    text = from_utc_seconds(seconds)
    return text if text == "--" else f"{text[5:7]}-{text[8:10]} {text[11:16]}"


def _is_color(item) -> bool:
    """彩色帧不吃伪彩 LUT，所以"伪彩"这一栏对它本来就该是灰的。"""
    shape = tuple(item.shape or ())
    return len(shape) == 3 and shape[-1] in (3, 4)


class Viewer:
    def __init__(self, root, autoload: bool = True):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.items: List = []
        self.root_dir: Optional[Path] = None
        self.group_label: dict = {}
        self.cells: List[dict] = []
        self._want: Optional[str] = None
        self._scheduled = False
        self._adv_shown = False
        self.t_lo = self.t_hi = 0.0
        self.has_time = False
        self.zooms = []
        self.last_manifest: Optional[Path] = None

        root.title("mdva 多模态科研数据可视化与并行分析平台")
        root.geometry("1480x860")
        root.configure(bg="#101014")

        self._build_toolbar()
        self._build_body()
        self._build_axis()

        self.status = ttk.Label(root, anchor="w", padding=(8, 4),
                                text="打开一个目录开始：FITS / TIFF / PNG / 掩膜 / CSV / Parquet / "
                                     "HDF5 / JSON 事件 / npy 帧栈 / 帧目录 / 视频")
        self.status.pack(fill="x")
        if autoload:
            root.after(150, self._autoload)

    # ------------------------------------------------------------------ 界面骨架

    @property
    def photos(self) -> list:
        """当前在屏的 Tk 图片对象（测试与导出用）。"""
        return [c["photo"] for c in self.cells if c.get("photo")]

    def _combo(self, parent, label, mapping, default, width=9, key=""):
        from tkinter import ttk

        ttk.Label(parent, text=label).pack(side="left", padx=(12, 2))
        var = self.tk.StringVar(value=_cn(mapping, default))
        box = ttk.Combobox(parent, textvariable=var, width=width, state="readonly",
                           values=[_cn(mapping, k) for k in mapping])
        box.pack(side="left")
        # 选完立刻生效。留着"应用"按钮等于让用户猜哪些控件是活的
        var.trace_add("write", lambda *_: self.request("all"))
        setattr(self, "box_" + key, box)
        return var

    def _build_toolbar(self):
        from tkinter import ttk

        self.mode = self.tk.StringVar(value="时间联动")
        self.cell = self.tk.StringVar(value="360 x 260")
        self.rotate = self.tk.StringVar(value="不旋转")
        self.flip = self.tk.StringVar(value="不翻转")

        bar = ttk.Frame(self.root, padding=(6, 4))
        bar.pack(side="top", fill="x")
        for text, command in (("打开目录…", self.open_dir),
                              ("生成示例数据…", self.make_data),
                              ("一键演示", self.demo)):
            ttk.Button(bar, text=text, command=command).pack(side="left")
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
        for text, command in (("导出拼图 PNG", self.export), ("批量分析…", self.batch)):
            ttk.Button(bar, text=text, command=command).pack(side="left", padx=1)

        pick = ttk.Menubutton(bar, text="整理选中 ▾")
        menu = self.tk.Menu(pick, tearoff=0)
        menu.add_command(label="复制到…", command=lambda: self.arrange("copy"))
        menu.add_command(label="移动到…", command=lambda: self.arrange("move"))
        menu.add_command(label="撤销上次整理", command=self.undo_arrange)
        pick["menu"] = menu
        pick.pack(side="left", padx=1)

        self.adv_toggle = ttk.Checkbutton(bar, text="显示高级参数", command=self._show_adv)
        self.adv_toggle.pack(side="left", padx=12)

        # 高级参数默认收起：区间/拉伸/伪彩这些不改画面内容，只改"怎么看"，
        # 一上来全摊开就是十几个控件，谁都不想点
        self.adv = ttk.Frame(self.root, padding=(6, 2))
        ttk.Label(self.adv, text="浏览方式").pack(side="left")
        for name in ("时间联动", "切片序号"):
            ttk.Radiobutton(self.adv, text=name, value=name, variable=self.mode,
                            command=self._on_mode).pack(side="left", padx=2)
        ttk.Label(self.adv, text="单格").pack(side="left", padx=(14, 2))
        for preset in ("240 x 170", "360 x 260", "512 x 380"):
            ttk.Radiobutton(self.adv, text=preset.split(" x ")[0], value=preset,
                            variable=self.cell,
                            command=lambda: self.request("build")).pack(side="left")

        self.interval_var = self._combo(self.adv, "灰度区间", INTERVAL_CN, "zscale", 8, "interval")
        self.stretch_var = self._combo(self.adv, "拉伸", STRETCH_CN, "linear", 8, "stretch")
        self.colormap_var = self._combo(self.adv, "伪彩", COLORMAP_CN, "gray", 7, "colormap")

        ttk.Label(self.adv, text="旋转").pack(side="left", padx=(12, 2))
        self.rotate_box = ttk.Combobox(self.adv, textvariable=self.rotate, width=6,
                                       state="readonly",
                                       values=["不旋转", "90 度", "180 度", "270 度"])
        self.rotate_box.pack(side="left")
        ttk.Label(self.adv, text="翻转").pack(side="left", padx=(8, 2))
        self.flip_box = ttk.Combobox(self.adv, textvariable=self.flip, width=6,
                                     state="readonly", values=["不翻转", "水平", "垂直"])
        self.flip_box.pack(side="left")
        for var in (self.rotate, self.flip):
            var.trace_add("write", lambda *_: self.request("all"))
        ttk.Button(self.adv, text="全部选中",
                   command=lambda: self.check_all(True)).pack(side="left", padx=(12, 1))
        ttk.Button(self.adv, text="全部取消",
                   command=lambda: self.check_all(False)).pack(side="left")

    def _show_adv(self):
        if self.adv_toggle.instate(["selected"]):
            self.adv.pack(side="top", fill="x")
        else:
            self.adv.pack_forget()

    def _build_body(self):
        from tkinter import ttk

        body = ttk.Panedwindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body)
        self.tree = ttk.Treeview(left, columns=("modality", "where", "name", "time"),
                                 show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="分组 / 选择")
        self.tree.column("#0", width=168, stretch=True)
        for key, title, width in (("modality", "模态", 52), ("where", "位置", 80),
                                  ("name", "数据项", 172), ("time", "起始时刻", 78)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, stretch=False)
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<Double-1>", self.on_double)
        self.tree.tag_configure("leaf_checked", foreground="#0a7d3c")
        self.tree.tag_configure("leaf_open", foreground="#202020")
        self.tree.tag_configure("group", foreground="#1d4ed8")
        self.tree.tag_configure("category", foreground="#111111")
        body.add(left, weight=0)

        right = ttk.Frame(body)
        self.canvas = self.tk.Canvas(right, highlightthickness=0, bg="#101014")
        vsb = ttk.Scrollbar(right, orient="vertical", command=self.canvas.yview)
        self.grid = ttk.Frame(self.canvas)
        self.grid_id = self.canvas.create_window((0, 0), window=self.grid, anchor="nw")
        self.grid.bind("<Configure>",
                       lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self.grid_id, width=e.width))
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        body.add(right, weight=5)

    def _build_axis(self):
        from tkinter import ttk

        axis = ttk.Frame(self.root, padding=(8, 4))
        axis.pack(fill="x")
        self.axis_label = ttk.Label(axis, text="时间游标", width=10)
        self.axis_label.pack(side="left")
        self.slider = ttk.Scale(axis, from_=0.0, to=1.0, command=self._on_slider)
        self.slider.pack(side="left", fill="x", expand=True, padx=8)
        self.cursor_label = ttk.Label(axis, text="无时间基准", width=30)
        self.cursor_label.pack(side="right")

    # ------------------------------------------------------------------ 分组加载

    def _async(self, work, done):
        """后台线程只算数，结果经队列回主线程。

        线程里直接调 widget.after 在事件循环被占住时会抛
        "main thread is not in main loop"，批量分析一卡就正好占住它。
        """
        import queue
        import threading

        box = queue.Queue()

        def runner():
            try:
                box.put((True, work()))
            except Exception as exc:
                box.put((False, exc))

        def poll():
            try:
                ok, payload = box.get_nowait()
            except queue.Empty:
                self.root.after(80, poll)
                return
            self.root.configure(cursor="")
            done(payload, not ok)

        self.root.configure(cursor="watch")
        threading.Thread(target=runner, daemon=True).start()
        self.root.after(80, poll)

    def _autoload(self):
        """旁边就有 data/ 示例数据时直接加载，三个学科分组一次全进来。"""
        root = _REPO / "data"
        if root.is_dir() and any(root.iterdir()):
            self.load(str(root))
            return
        self.rebuild()

    def make_data(self):
        from tkinter import filedialog

        target = filedialog.askdirectory(title="示例数据生成到哪里")
        if not target:
            return

        from . import datasets

        def done(result, failed):
            if failed:
                self.status.config(text=f"生成失败：{result}")
                return
            self.status.config(text="示例数据已生成，按学科分成三个分组")
            self.load(str(Path(target)))

        self.status.config(text="正在生成三类示例数据…（十几秒，界面可继续操作）")
        self._async(lambda: datasets.build_all(target, seed=7), done)

    def open_dir(self):
        from tkinter import filedialog

        path = filedialog.askdirectory(title="选择数据目录")
        if path:
            self.load(path)

    def _rel_parts(self, uri: str) -> tuple:
        """数据项相对加载根目录的目录链，就是它在树里的分组路径。"""
        src = Path(uri)
        parts = ()
        if self.root_dir is not None:
            try:
                parts = src.relative_to(self.root_dir).parts[:-1]
            except ValueError:
                parts = ()
        if not parts:
            parts = (src.parent.name or "根目录",)
        return parts

    def load(self, path):
        root = Path(path).resolve()
        try:
            self.status.config(text=f"正在探测 {root} …（只读文件头，不读数据）")
            self.root.update_idletasks()
            items = io.scan(str(root))
        except Exception as exc:
            self.status.config(text=f"扫描失败：{exc}")
            return
        if not items:
            self.status.config(text=f"{root} 下没有可识别的数据项")
            return

        self.root_dir = root
        self.items = items
        self._build_tree(items)
        # 一进来就画几十路会卡住，先给个能立刻看到结果的子集
        self._set_leaves(self._leaves()[:MAX_AUTO_CHECK], True)
        self._sync_groups()
        # 时间跨度跟着勾选走，所以要等勾选定下来再算
        self._update_span(keep=False)
        cats = self._category_names()
        self.status.config(
            text=f"{root.name}：{len(items)} 项，分 {len(cats)} 组（{'、'.join(cats)}），"
                 f"已显示前 {min(len(items), MAX_AUTO_CHECK)} 项，点分组名可整组勾选")
        self.rebuild()

    def _build_tree(self, items):
        for row in self.tree.get_children():
            self.tree.delete(row)
        self.group_label = {}
        nodes = {}
        for i, item in enumerate(items):
            parent = ""
            for depth, part in enumerate(self._rel_parts(item.uri)):
                key = f"{key_of(parent)}/{part}"
                iid = f"g:{key}"
                if iid not in nodes:
                    title = _category_title(part, depth == 0)
                    self.tree.insert(parent, "end", iid=iid, text=f"☐ {title}",
                                     values=("", "", "", ""),
                                     tags=("category" if depth == 0 else "group",),
                                     open=True)
                    self.group_label[iid] = title
                    nodes[iid] = True
                parent = iid
            self.tree.insert(parent, "end", iid=str(i), text="☐", values=(
                _cn(MODALITY_CN, item.modality), Path(item.uri).parent.name,
                item.title or item.id, _short_time(item.t_ref)), tags=("leaf_open",))

    def _category_names(self) -> list:
        return [self.group_label[iid].lstrip() for iid in self.tree.get_children()]

    # ------------------------------------------------------------------ 勾选

    def _leaves(self, iid="") -> list:
        out = []
        for child in self.tree.get_children(iid or ""):
            if child.startswith("g:"):
                out.extend(self._leaves(child))
            else:
                out.append(child)
        return out

    def is_checked(self, iid) -> bool:
        return self.tree.item(iid, "text").startswith("☑")

    def is_partial(self, iid) -> bool:
        return self.tree.item(iid, "text").startswith("◪")

    def on_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid or self.tree.identify_column(event.x) != "#0":
            return
        if iid.startswith("g:"):
            self._set_leaves(self._leaves(iid), not self.is_checked(iid))
        else:
            self._set_leaves([iid], not self.is_checked(iid))
        self._sync_groups()
        self._update_span(keep=True)
        self.request("build")

    def on_double(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and not iid.startswith("g:"):
            self.zoom(int(iid))

    def _set_leaves(self, iids, on: bool):
        for iid in iids:
            self.tree.item(iid, text="☑" if on else "☐",
                           tags=("leaf_checked" if on else "leaf_open",))

    def check_group(self, on: bool):
        sel = self.tree.focus()
        if not sel:
            self.status.config(text="先在左侧点一个分组或数据项")
            return
        iids = self._leaves(sel) if sel.startswith("g:") else [sel]
        self._set_leaves(iids, on)
        self._sync_groups()
        self._update_span(keep=True)
        self.request("build")

    def check_all(self, on: bool):
        self._set_leaves(self._leaves(), on)
        self._sync_groups()
        self._update_span(keep=True)
        self.request("build")

    def _sync_groups(self) -> int:
        """分组节点显示三态与子树项数：全选 ☑、部分 ◪、未选 ☐。返回选中叶子数。"""
        total = 0
        for iid in self.group_label:
            leaves = self._leaves(iid)
            n = sum(1 for x in leaves if self.is_checked(x))
            glyph = "☑" if leaves and n == len(leaves) else ("◪" if n else "☐")
            self.tree.item(iid, text=f"{glyph} {self.group_label[iid]}")
            self.tree.set(iid, "name", f"{len(leaves)} 项 / 已选 {n}")
        for iid in self._leaves():
            if self.is_checked(iid):
                total += 1
        return total

    def selected(self) -> List:
        return [self.items[int(i)] for i in self._leaves() if self.is_checked(i)]

    # ------------------------------------------------------------------ 时间轴 / 序号

    def _update_span(self, keep: bool = True):
        """时间跨度跟着勾选走：三个学科各自的纪元混在一条轴上会把各自数据压成一个点。"""
        items = self.selected() or self.items
        lo, hi = io.common_span(items)
        has = bool(np.isfinite(lo) and np.isfinite(hi) and hi > lo)
        frac = 0.5
        if keep and self.has_time and self.slider["to"]:
            frac = float(self.slider.get()) / float(self.slider["to"])
        self.has_time = has
        self.t_lo, self.t_hi = (lo, hi) if has else (0.0, 1.0)
        if self.mode.get() == "切片序号":
            self._show_index_axis(keep=keep)
            return
        self.slider.configure(state="normal" if has else "disabled",
                              to=max(self.t_hi - self.t_lo, 1e-9) if has else 1.0)
        self.slider.set(max(0.0, min(1.0, frac)) * float(self.slider["to"]))
        self.axis_label.config(text="时间游标")
        self.cursor_label.config(text=from_utc_seconds(self.cursor()) if has else "无时间基准")

    def _max_views(self) -> int:
        items = self.selected() or self.items
        return max([1] + [int(i.n_views or 1) for i in items])

    def _show_index_axis(self, keep: bool = False):
        n = self._max_views()
        self.slider.configure(state="normal", to=max(n - 1, 1))
        if not keep:
            self.slider.set(0.0)
        self.cursor_label.config(text=f"序号 0 ~ {n - 1}（图像切片 / 视频帧 / 时序通道 / 事件等级）")

    def _on_mode(self):
        if self.mode.get() == "切片序号":
            self._show_index_axis()
        else:
            self._update_span(keep=False)
        self.request("all")

    def cursor(self):
        return self.t_lo + float(self.slider.get()) if self.has_time else None

    def index(self) -> int:
        return int(round(float(self.slider.get())))

    def _on_slider(self, _value):
        if self.mode.get() == "切片序号":
            self.cursor_label.config(
                text=f"当前序号 {self.index()} / {int(float(self.slider['to']))}")
        else:
            if not self.has_time:
                return
            self.cursor_label.config(text=from_utc_seconds(self.cursor()))
        self.request("cursor")

    def window(self) -> Slice:
        """时间窗以游标为中心，跨度取全局跨度的 6%。"""
        if self.mode.get() == "切片序号":
            return Slice(index=self.index())
        t = self.cursor()
        if t is None:
            return Slice.full()
        half = max(1e-6, (self.t_hi - self.t_lo) * 0.03)
        return Slice.at_time(max(self.t_lo, t - half), min(self.t_hi, t + half))

    def window_tuple(self):
        if self.mode.get() == "切片序号" or not self.has_time:
            return None
        t = self.cursor()
        half = max(1e-6, (self.t_hi - self.t_lo) * 0.03)
        return (max(self.t_lo, t - half), min(self.t_hi, t + half))

    # ------------------------------------------------------------------ 渲染

    def cell_size(self):
        w, h = self.cell.get().split(" x ")
        return int(w), int(h)

    def style(self) -> dict:
        return {"interval": _INTERVAL_OF.get(self.interval_var.get(), "zscale"),
                "stretch": _STRETCH_OF.get(self.stretch_var.get(), "linear"),
                "colormap": _CMAP_OF.get(self.colormap_var.get(), "gray")}

    def _transform(self, rgb: np.ndarray) -> np.ndarray:
        k = {"不旋转": 0, "90 度": 1, "180 度": 2, "270 度": 3}.get(self.rotate.get(), 0)
        out = np.rot90(rgb, k) if k else rgb
        if self.flip.get() == "水平":
            out = np.fliplr(out)
        elif self.flip.get() == "垂直":
            out = np.flipud(out)
        return out

    def request(self, kind: str = "cursor"):
        """把重画请求合并到下一帧。

        滑块拖动一次会派发几十次 command，逐次重画就是让界面永远在补
        半小时前的那一步——用户看到的"卡顿"其实是积压的事件。
        """
        if kind == "build":
            self._want = "build"
        elif self._want != "build":
            self._want = "all" if (kind == "all" or self._want == "all") else "cursor"
        if not self._scheduled:
            self._scheduled = True
            self.root.after_idle(self._flush)

    def _flush(self):
        self._scheduled = False
        want, self._want = self._want, None
        if want == "build":
            self.rebuild()
        elif want:
            self.paint(all_tiles=want == "all")

    def rebuild(self):
        """勾选、单格尺寸变了才走这里：控件重建一次。"""
        from tkinter import ttk

        self._want = None          # rebuild 自己会把所有格子画一遍，排队中的请求作废
        for child in self.grid.winfo_children():
            child.destroy()
        self.cells = []
        items = self.selected()
        if not items:
            ttk.Label(self.grid, text="在左侧点分组名勾选（点分组=整组），这里就会出图",
                      foreground="#c8ccd8", background="#101014").grid(row=0, column=0,
                                                                       padx=12, pady=12)
            self._tune_state()
            return

        size = self.cell_size()
        # 首帧还没完成排版时 winfo_width 是 1，按 1 列排会把窗口撑得极窄；
        # 但也不能直接拿 900 兜底，否则右侧那一列会被画布裁掉
        width = self.canvas.winfo_width()
        avail = width if width > 120 else 900
        cols = max(1, min(4, (avail - 32) // (size[0] + 20) or 1))
        for n, item in enumerate(items):
            row, col = divmod(n, cols)
            frame = self.tk.Frame(self.grid, bg="#18181c")
            frame.grid(row=row * 2, column=col, padx=8, pady=(8, 0), sticky="n")
            cap = self.tk.Label(self.grid, text="", fg="#c8ccd8", bg="#101014",
                                wraplength=size[0], justify="left",
                                font=("Microsoft YaHei", 9))
            cap.grid(row=row * 2 + 1, column=col, sticky="w", pady=(2, 8))
            cell = {"item": item, "index": self.items.index(item), "frame": frame,
                    "cap": cap, "photo": None, "size": None, "key": None, "view": None}
            view = self.tk.Label(frame, bg="#18181c")
            view.bind("<Double-1>", lambda _e, i=cell["index"]: self.zoom(i))
            cell["view"] = view
            self.cells.append(cell)
        self.paint(all_tiles=True)

    def _moves_with_cursor(self, item) -> bool:
        """单帧图像跟游标无关：拖时间轴时重画它纯属浪费。"""
        if item.modality in (Modality.SERIES.value, Modality.EVENT.value):
            return True
        if item.modality in (Modality.CUBE.value, Modality.VIDEO.value):
            return int(item.n_views or 1) > 1
        return False

    def paint(self, all_tiles: bool = False):
        """只重画"该重画"的格子，并把 Tk 图片对象原地更新，不重建控件。"""
        sl = self.window()
        t = None if self.mode.get() == "切片序号" else self.cursor()
        style = self.style()
        sig = (self.cell.get(), self.rotate.get(), self.flip.get(),
               style["interval"], style["stretch"], style["colormap"])
        failed = 0
        for cell in self.cells:
            item = cell["item"]
            if not self._alive(cell):
                continue        # 网格已被 rebuild 拆掉，排队中的这次重画直接作废
            if not all_tiles and not self._moves_with_cursor(item):
                continue
            key = (sl.index, sl.t0, sl.t1) + sig
            if key == cell["key"]:
                continue
            cell["key"] = key
            try:
                res = render.render_item(item, size=self.cell_size(), window=sl,
                                         style=style, cursor_t=t)
                self._show_tile(cell, res)
            except Exception as exc:
                failed += 1
                self._show_error(cell, item, exc)
        self._tune_state(failed)

    @staticmethod
    def _alive(cell) -> bool:
        try:
            return bool(cell["view"].winfo_exists())
        except Exception:
            return False

    def _show_tile(self, cell, res):
        from PIL import Image, ImageTk

        img = Image.fromarray(np.ascontiguousarray(self._transform(res.composited())))
        photo = cell["photo"]
        if photo is None or (photo.width(), photo.height()) != img.size:
            # 尺寸变了只能新建；否则原地 paste，省掉一次 Tk 图片分配
            photo = ImageTk.PhotoImage(img)
            cell["photo"] = photo
            cell["view"].config(image=photo)
            cell["view"].pack()
        else:
            photo.paste(img)
        item = cell["item"]
        label = f"{Path(item.uri).parent.name}/{item.id}"
        if item.title and item.title != item.id:
            label = f"{item.title} · {label}"
        cell["cap"].config(text=f"{_cn(MODALITY_CN, item.modality)} · {label}   "
                                f"{res.subtitle}"[:160])

    def _show_error(self, cell, item, exc):
        cell["view"].config(text=f"{item.id}\n渲染失败", fg="#e06c75", image="")
        cell["view"].pack()
        cell["cap"].config(text=f"{item.id}  {type(exc).__name__}: {exc}"[:160])

    def _tune_state(self, failed: int = 0):
        """让控件"活不活"看得见：影响不到当前勾选的参数直接置灰并说明原因。"""
        items = [c["item"] for c in self.cells]
        array = [i for i in items if i.modality in (Modality.IMAGE.value, Modality.CUBE.value,
                                                    Modality.VIDEO.value)]
        scalar = [i for i in array if not _is_color(i) and not i.meta.get("palette")]
        for box, live in ((getattr(self, "box_interval", None), bool(array)),
                          (getattr(self, "box_stretch", None), bool(array)),
                          (getattr(self, "box_colormap", None), bool(scalar)),
                          (getattr(self, "rotate_box", None), bool(array)),
                          (getattr(self, "flip_box", None), bool(array))):
            if box is not None:
                box.configure(state="readonly" if live else "disabled")

        if self._want is None and not self.cells:
            self.status.config(text="左侧还没有勾选任何数据项")
            return
        n = len(items)
        why = (f"位深参数只影响其中 {len(array)} 路图像，时序与事件不受影响"
               if array and len(array) < n else
               "这一组里没有图像类数据，位深参数点不动是应该的" if not array else "")
        head = (f"{n} 路" + ("，" + str(failed) + " 路渲染失败" if failed else
                             ("，拖动下方游标四路一起走" if self.has_time
                              else "，改用「切片序号」浏览"))
                if n else "没有勾选")
        self.status.config(text=head + (f"（{why}）" if why and not failed else ""))

    def demo(self):
        """一键演示：勾上跨模态那一组，游标停在"一个窗同时命中四种模态"的位置。"""
        if not self.items:
            self._autoload()
            if not self.items:
                self.status.config(text="还没有数据：先点「生成示例数据…」")
                return
        self.mode.set("时间联动")
        self.check_all(False)
        top = next((iid for iid in self.tree.get_children()
                    if self.group_label[iid] == "航空航天"), "")
        if top:
            self.tree.focus(top)
            self.check_group(True)
        else:
            self._set_leaves(self._leaves()[:8], True)
            self._sync_groups()
        link = self._linkage_window()
        if link:
            mid = 0.5 * (link[0] + link[1])
            self.slider.set(max(0.0, min(self.t_hi - self.t_lo, mid - self.t_lo)))
            self.cursor_label.config(text=from_utc_seconds(self.cursor()))
        self.paint(all_tiles=True)
        self.status.config(text="演示：这一窗同时框住遥测、事件、载荷图像与帧序列，"
                                "拖游标看四路一起走（高级参数在右上角）")

    def _linkage_window(self):
        """示例数据把"四种模态同时有内容"的那一段写进了 truth.json，直接跳过去。"""
        import json

        roots = [self.root_dir] if self.root_dir else []
        roots += [_REPO / "data", _REPO]
        for base in roots:
            for truth in sorted(Path(base).glob("**/aerospace/truth.json"))[:1]:
                try:
                    win = json.loads(truth.read_text(encoding="utf-8")).get("linkage_window")
                except Exception:
                    continue
                if win and len(win) == 2:
                    return (float(win[0]), float(win[1]))
        return None

    def refresh(self):
        """兼容旧调用点：整格重建。"""
        self.rebuild()

    def zoom(self, index: int):
        win = _ZoomWindow(self.root, self.items[index], self.style(), self.window(),
                          self.cell_size())
        self.zooms.append(win)

        def close():
            win.top.destroy()
            if win in self.zooms:
                self.zooms.remove(win)

        win.top.protocol("WM_DELETE_WINDOW", close)

    # ------------------------------------------------------------------ 导出

    def export(self):
        from tkinter import filedialog

        items = self.selected()
        if not items:
            self.status.config(text="先勾选几路数据")
            return
        path = filedialog.asksaveasfilename(defaultextension=".png",
                                            filetypes=[("PNG", "*.png")], title="导出拼图")
        if not path:
            return
        size = self.cell_size()
        sl = self.window()
        results = [r for r in (self._render_one(it, size, sl) for it in items) if r is not None]
        if not results:
            self.status.config(text="没有可渲染的结果")
            return
        from dataclasses import replace

        rotated = [replace(r, rgb=self._transform(r.rgb)) for r in results]
        cols = min(len(rotated), 4)
        img = render.render_mosaic(rotated, rows=-(-len(rotated) // cols), cols=cols, cell=size)
        from PIL import Image

        Image.fromarray(img).save(str(Path(path)))
        self.status.config(text=f"已导出 {path}（{len(results)} 路，单格 {size[0]}x{size[1]}）")

    def _render_one(self, item, size, sl):
        try:
            return render.render_item(item, size=size, window=sl, style=self.style())
        except Exception:
            return None

    # ------------------------------------------------------------------ 批量分析

    def batch(self):
        from tkinter import filedialog, ttk

        items = self.selected()
        if not items:
            self.status.config(text="先勾选要做批量分析的数据项")
            return

        top = self.tk.Toplevel(self.root)
        top.title(f"并行批量分析（已勾选 {len(items)} 项）")
        top.geometry("560x420")
        names = list(ANALYZER_CN)

        box = self.tk.Listbox(top, selectmode=self.tk.EXTENDED, exportselection=False,
                              font=("Microsoft YaHei", 10))
        for n in names:
            box.insert("end", ANALYZER_CN[n])
        box.pack(fill="both", expand=True, padx=10, pady=8)
        box.selection_set(0)

        row = self.ttk.Frame(top, padding=(10, 0))
        row.pack(fill="x")
        self.ttk.Label(row, text="并发线程").pack(side="left")
        workers = self.tk.StringVar(value="4")
        self.ttk.Spinbox(row, from_=1, to=16, width=4, textvariable=workers).pack(side="left")
        self.ttk.Label(row, text="执行器").pack(side="left", padx=(12, 2))
        executor = self.tk.StringVar(value="线程")
        self.ttk.Combobox(row, textvariable=executor, width=5, state="readonly",
                          values=["线程", "进程"]).pack(side="left")
        out_dir = self.tk.StringVar(value=str(_REPO / "out" / "batch"))
        self.ttk.Label(row, text="输出").pack(side="left", padx=(12, 2))
        self.ttk.Entry(row, textvariable=out_dir, width=22).pack(side="left", fill="x", expand=True)

        def run_batch():
            chosen = [names[i] for i in box.curselection()]
            if not chosen:
                self.status.config(text="至少选一个算子")
                return
            from . import pipeline

            spec = pipeline.PipelineSpec(
                source=str(self.root_dir or "."),
                out=out_dir.get(),
                uris=[i.uri for i in items],
                stages=[pipeline.Stage(n) for n in chosen],
                render_style=self.style(),
                window=self.window_tuple(),
                index=self.index() if self.mode.get() == "切片序号" else 0,
                workers=int(workers.get() or 1),
                executor="process" if executor.get() == "进程" else "thread",
                cell=self.cell_size(),
                cols=min(4, len(items)),
            )
            self.status.config(text=f"正在并行跑 {len(items)} 项 × {len(chosen)} 个算子…")

            def done(result, failed):
                if failed:
                    self.status.config(text=f"批量分析失败：{result}")
                    return
                s = result["summary"]
                self.status.config(
                    text=f"批量分析：{s['n_ok']}/{s['n_items']} 成功，{s['workers']} 并发 "
                         f"{s['elapsed']}s（{s['throughput']} 项/秒），报告与标注图在 {spec.out}")
                _show_report(self.root, result, spec.out)

            self._async(lambda: pipeline.run(spec, progress=False), done)

        bar = self.ttk.Frame(top, padding=10)
        bar.pack(fill="x")
        self.ttk.Button(bar, text="开始并行分析", command=run_batch).pack(side="left")
        self.ttk.Button(bar, text="关闭", command=top.destroy).pack(side="right")

    # ------------------------------------------------------------------ 数据整理

    def arrange(self, mode: str):
        from tkinter import filedialog, messagebox

        from . import collect

        items = self.selected()
        if not items:
            self.status.config(text="先勾选要整理的数据项")
            return
        title = "把勾选的数据项复制到哪个目录" if mode == "copy" else "把勾选的数据项移到哪个目录"
        target = filedialog.askdirectory(title=title)
        if not target:
            return
        try:
            pairs = collect.plan(items, target)
        except Exception as exc:
            self.status.config(text=f"整理预览失败：{exc}")
            return
        head = "\n".join(f"{src.name}  ->  {dst.relative_to(Path(target))}"
                         for src, dst in pairs[:8])
        more = f"\n…共 {len(pairs)} 项" if len(pairs) > 8 else ""
        if not messagebox.askyesno("确认整理",
                                   f"{('复制' if mode == 'copy' else '移动')} {len(pairs)} 项到\n"
                                   f"{target}\n\n{head}{more}\n\n"
                                   f"同名文件会自动加序号，操作会写入可撤销清单。"):
            return
        try:
            coll = collect.collect(items, target, mode=mode)
        except Exception as exc:
            self.status.config(text=f"整理失败：{exc}")
            return
        self.last_manifest = coll.manifest_path
        s = coll.summary()
        text = (f"已{'复制' if mode == 'copy' else '移动'} {s['copied'] + s['moved']} 项到 {target}，"
                f"清单 {coll.manifest_path.name}，可用「撤销上次整理」还原")
        if s["skipped"]:
            text += f"，跳过 {s['skipped']} 项"
        self.status.config(text=text)
        if mode == "move":
            self.load(str(self.root_dir) if self.root_dir else target)

    def undo_arrange(self):
        from tkinter import filedialog, messagebox

        from . import collect

        manifest = self.last_manifest
        if manifest is None or not Path(manifest).is_file():
            target = filedialog.askdirectory(title="选择上次整理落到的目录")
            if not target:
                return
            manifest = collect.latest_manifest(target)
            if manifest is None:
                self.status.config(text=f"{target} 下没有 {collect.MANIFEST_NAME} 整理清单")
                return
        if not messagebox.askyesno("撤销整理", f"按清单还原\n{manifest}\n\n"
                                              f"复制的会删掉，移动的会搬回原处。继续？"):
            return
        try:
            report = collect.undo(manifest)
        except Exception as exc:
            self.status.config(text=f"撤销失败：{exc}")
            return
        self.status.config(text=f"撤销完成：删除 {report['removed']} 项，"
                                f"搬回 {report['restored']} 项，"
                                f"因对不上而拒绝处理 {report['refused']} 项")
        if report["refused"]:
            messagebox.showwarning("部分未处理",
                                   f"{report['refused']} 个文件与清单记录的字节数或修改时间对不上，"
                                   f"为避免误删已跳过。")
        self.last_manifest = None
        if self.root_dir:
            self.load(str(self.root_dir))


def key_of(iid: str) -> str:
    """分组 iid -> 分组 key，用于拼下一层。"""
    return iid[2:] if iid.startswith("g:") else ""


def _category_title(part: str, top: bool) -> str:
    """分组树里放短名，长标题留给状态栏和清单。"""
    if not top:
        return part
    try:
        from . import datasets

        meta = datasets.CATEGORIES.get(part)
        if meta:
            return meta.get("short") or meta["title"]
    except Exception:
        pass
    return part


class _ZoomWindow:
    """单图放大：自带切片/时间游标，用来核对细节与标注。"""

    def __init__(self, parent, item, style, window, cell):
        import tkinter as tk
        from tkinter import ttk

        self.item = item
        self.style = style
        self.photos = []
        size = (max(760, cell[0] * 2), max(520, cell[1] * 2))

        top = tk.Toplevel(parent)
        self.top = top
        top.title(f"{item.title or item.id}　{_cn(MODALITY_CN, item.modality)}")
        top.geometry(f"{size[0] + 40}x{size[1] + 120}")
        top.configure(bg="#101014")
        self.canvas = tk.Canvas(top, highlightthickness=0, bg="#101014")
        self.canvas.pack(fill="both", expand=True)
        bar = ttk.Frame(top, padding=(8, 6))
        bar.pack(fill="x")
        self.n = max(int(item.n_views or 1), 1)
        self.has_time = item.t_ref is not None
        self.label = ttk.Label(bar, text="", width=52)
        self.label.pack(side="right")
        self.slider = ttk.Scale(bar, from_=0.0, to=max(self.n - 1, 1),
                                command=self._on_slide)
        self.slider.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Label(bar, text="切片" if self.n > 1 else "时间").pack(side="left")
        if self.n == 1 and self.has_time:
            lo, hi = item.time_span
            self.t_lo, self.t_hi = lo, hi
            self.slider.configure(to=max(hi - lo, 1e-9))
        top.bind("<Escape>", lambda _e: top.destroy())
        top.attributes("-topmost", True)
        self._render(0)

    def _on_slide(self, _v):
        self._render(float(self.slider.get()))

    def _render(self, value: float):
        from tkinter import ttk

        if self.n > 1:
            sl = Slice(index=int(round(value)))
        elif self.has_time:
            half = max(1e-6, (self.t_hi - self.t_lo) * 0.15)
            t = self.t_lo + value
            sl = Slice.at_time(max(self.t_lo, t - half), min(self.t_hi, t + half))
        else:
            sl = Slice.full()
        try:
            res = render.render_item(self.item, size=self.canvas_size(), window=sl,
                                     style=self.style, cursor_t=None)
            photo = _photo(res.composited())
            self.photos.append(photo)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor="nw", image=photo)
            self.canvas.configure(scrollregion=(0, 0, photo.width(), photo.height()))
            self.label.config(text=res.subtitle)
        except Exception as exc:
            self.label.config(text=f"渲染失败 {type(exc).__name__}: {exc}")

    def canvas_size(self):
        w = max(self.canvas.winfo_width(), 640)
        h = max(self.canvas.winfo_height(), 460)
        return (int(w), int(h))


def _show_report(parent, result, out_dir):
    """批量结果：上面是指标表，点一行就在下面看对应的标注图。"""
    import tkinter as tk
    from tkinter import ttk

    s = result["summary"]
    top = tk.Toplevel(parent)
    top.title(f"批量分析结果　{s['n_ok']}/{s['n_items']} 成功 · "
              f"{s['workers']} 并发 · {s['elapsed']}s")
    top.geometry("980x660")

    table = ttk.Frame(top)
    table.pack(fill="x")
    tree = ttk.Treeview(table, height=8,
                        columns=("name", "modality", "metrics", "flags",
                                 "elapsed", "error"),
                        show="headings")
    for key, title, width, stretch in (
            ("name", "数据项", 190, False), ("modality", "模态", 58, False),
            ("metrics", "指标", 330, True), ("flags", "异常标记", 62, False),
            ("elapsed", "耗时", 56, False), ("error", "错误", 170, False)):
        tree.heading(key, text=title)
        tree.column(key, width=width, anchor="w", stretch=stretch)
    vsb = ttk.Scrollbar(table, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.pack(side="left", fill="both", expand=True)
    vsb.pack(side="right", fill="y")

    rows = {}
    for r in result["records"]:
        metrics = "; ".join(f"{k}={v:.4g}" if isinstance(v, float) else f"{k}={v}"
                            for k, v in list(r["metrics"].items())[:6])
        uri = Path(r["uri"])
        # 多模型对比时文件名一模一样，只报文件名等于没报
        iid = tree.insert("", "end", values=(
            f"{uri.parent.name}/{uri.name}", _cn(MODALITY_CN, r["modality"]), metrics,
            len(r["flags"]), f"{r['elapsed']:.2f}s", r["error"]))
        rows[iid] = r.get("annotated", "")

    preview = tk.Label(top, bg="#101014", fg="#c8ccd8",
                       text="点击上面任意一行查看标注图")
    preview.pack(fill="both", expand=True)
    info = ttk.Label(top, anchor="w", padding=(8, 3),
                     text=f"报告与标注图目录：{out_dir}")
    info.pack(fill="x")

    holder = []

    def show(_e=None, iid=None):
        path = rows.get(iid or "")
        if not path or not Path(path).is_file():
            preview.config(image="", text=(f"这一项没有标注图：{path}" if iid
                                           else "点击上面任意一行查看标注图"))
            return
        from PIL import Image, ImageTk

        img = Image.open(path)
        img.thumbnail((940, 420))
        photo = ImageTk.PhotoImage(img)
        holder.append(photo)
        preview.config(image=photo, text="")

    def current():
        # 键盘选择时 focus 可能为空，只认 focus 会让预览停在占位文字上
        return tree.focus() or (tree.selection() and tree.selection()[0]) or ""

    tree.bind("<<TreeviewSelect>>", lambda e: show(e, current()))


def _photo(rgb: np.ndarray):
    from PIL import Image, ImageTk

    return ImageTk.PhotoImage(Image.fromarray(np.ascontiguousarray(rgb)))


def run() -> int:
    try:
        import numpy  # noqa: F401
        import PIL  # noqa: F401
    except ImportError as exc:
        print(f"缺少依赖：{exc}")
        print("请先双击 install.bat，或在装好依赖的环境里运行 python main.py")
        return 1

    import tkinter as tk

    if tk.TkVersion < 8.5:
        print("这个 Python 没有带 tkinter，装不了图形界面；请改用 conda 的 python")
        return 1

    app = tk.Tk()
    Viewer(app)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
