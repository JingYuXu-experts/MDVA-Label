# 多模态科研数据可视化与并行分析基础平台（mdva）

把**图像、数据立方体、时序、事件、帧序列/视频**五种科研数据统一到一套数据模型里浏览、比对和分析，
并且能在无图形界面的环境下以命令行与批处理方式使用。

本科毕业设计配套工程。核心引擎 `src/mdva/` 不依赖任何窗口系统：同一套代码既给界面调用，
也进脚本和定时任务——这是"平台"而不是"工具"的意思。

```
io (L1 数据接入)  →  render (L2 渲染可视化)  →  analysis / pipeline (L3 分析与并行)
        └──────────────────  core：统一数据模型 + 注册表  ──────────────────┘
```

仓库里有两部分代码：

| 目录 | 是什么 | 依赖 |
| --- | --- | --- |
| `src/mdva/` | **核心引擎**（本项目新增），CLI、批处理、全部测试都在这里 | numpy/scipy/pandas/Pillow，科研格式按需 |
| `src/mulimgviewer/` | 早期沿用的 wxPython 图形界面 | wxPython 等，见 `requirements/gui.txt` |

---

## 1. 怎么打开

**双击 `run.bat` 就行。** 它自己找可用的 Python，找到一个能开就直接开窗口，找不到会告诉你先双击 `install.bat`。

打开后是一个窗口程序，界面全中文，左侧按**学科分组 → 目录 → 数据项**三级展开。
**默认只给五个动作**，位深那些参数收在右上角「显示高级参数」里：

```
┌ 打开目录… │ 生成示例数据… │ 一键演示 │ 导出拼图 PNG │ 批量分析… │ 整理选中 ▾ │ ☐ 显示高级参数 ┐
├──────────────────┬──────────────────────────────────────────────────────────────────────────┤
│ 分组 / 选择       │ 模态  位置       数据项            起始时刻  │                            │
│ ☑ 航空航天 7/已选7│       payload    SYNTH-CUBE        01-01 04:00│  勾选的每一项在这里出图，   │
│   ☑ payload 3/3  │ 立方体 payload    SYNTH-CUBE        01-01 04:00│  每格下面写着 模态 · 路径 · │
│   ☑ sequence 1/1 │ 图像  payload     SUN-DISK          01-01 03:03│  数值范围 · 尺寸，          │
│   ☑ telemetry 3/3│ 视频  sequence    solar_evolution   01-01 01:46│  双击任意一格放大细看        │
│ ☐ 图像分割 60/0   │ 时序  telemetry   engineering.csv   01-01 00:00│                            │
│ ☐ 医学 15/0       │ 事件  telemetry   operations.json   01-01 01:46│                            │
├──────────────────┴──────────────────────────────────────────────────────────────────────────┤
│ 时间游标  ├────────●──────┤                                    2025-01-01 01:46:51.000        │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

- **一键演示** 点一下就够看懂整个平台：勾上航空航天那一组，把游标停到 `truth.json` 里那个
  "同时命中遥测 / 事件 / 载荷图像 / 帧序列"的时间窗，四路一起有内容
- **点分组名**整组勾选/取消，分组行实时显示"共几项 / 已选几项"；点数据项前的方框单勾
- **拖底部时间游标**：所有有时间基准的项一起跟着走 —— 遥测曲线上出现黄色游标线、事件轴换窗口、
  载荷图像按观测时刻切换、帧序列按帧率取帧。这就是"跨模态联动"。时间轴范围跟着勾选走，
  只勾医学时不会为了迁就五小时长的遥测把医学压成一个点
- **切「切片序号」**（高级里）：没有绝对时刻的数据（分割掩膜、CT 断层）按序号浏览，
  同一个滑块对四种模态分别是 图像切片 / 视频帧 / 时序通道 / 事件等级
- **区间 / 拉伸 / 伪彩 / 旋转 / 翻转**（高级里）：改完立刻生效，没有"应用"按钮。
  **影响不到当前勾选的参数会自动置灰**，状态栏同时说明原因
  （例如"位深参数只影响其中 4 路图像，时序与事件不受影响"）
- **批量分析…**：把勾选的几路交给并行流水线（可选算子、并发数、线程/进程），
  结果表格点一行就在下方显示那一路的标注图，同时导出 `metrics.csv`、`batch_report.json`、
  单张标注图与总览图
- **整理选中 ▾**：复制到… / 移动到… / 撤销上次整理。落盘按"来源目录名/文件名"，
  多模型输出同名也不会互相覆盖，每次操作写一份清单可整批撤销
- **双击任意一格**：弹出放大窗，自带切片滑块，用来看点源和类别边界
- **导出拼图 PNG**：把当前这一屏按当前单格尺寸存成论文用图

界面只用 Python 自带的 tkinter，不依赖 wxPython，也不需要额外装任何包。

**拖动为什么不卡**：`ttk.Scale` 每移动一个像素派发一次事件，旧写法每次都销毁重建整张网格。
现在改成控件只建一次、游标只重画"会随时间变"的格子（单帧图像不重画）、Tk 图片对象原地
`paste` 复用、重画请求合并到下一帧。实测（勾选 7 路、单格 360×260）每步 72.6 ms → 24.4 ms，
一次 40 个事件的拖动 2919 ms → 114 ms；整组 60 张静态图时拖一步 351.7 ms → 0.9 ms。
基准脚本与逐像素一致性验证在 `code_optimization/`，过程见 `code_optimization/report.md`。

命令行入口在 `mdva`（`pip install -e .` 之后）或 `python -m mdva`（免安装，需 `PYTHONPATH=src`），
批处理与无界面场景用它，见第 1.3 节。

### 1.1 建环境（第一次，或换机器）

```bash
conda env create -f environment.yml     # 或直接双击 install.bat
conda activate mdva
```

本机现成的环境名就是 `mdva`（Python 3.10），已装齐。

### 1.2 装成命令（可选）

```bash
pip install -e .
mdva --help
```

不装也行，所有命令都能用模块形式跑（Windows PowerShell 用 `$env:PYTHONPATH="src"`）：

```bash
PYTHONPATH=src python -m mdva --help
```

### 1.3 六条命令看完主要能力

```bash
# ① 生成三类示例数据（全部程序合成，带真值，不需要任何外部数据源）
mdva datasets data --seed 7

# ② 枚举数据项：只读文件头不读数据，目录里项目再多也不会把数据吃进内存
mdva scan data/aerospace

# ③ 渲染单个数据项：区间估计 + 拉伸 + 伪彩都可换，FITS/TIFF/时序/视频一视同仁
mdva render data/aerospace/payload/solar_disk.fits --interval zscale --colormap inferno --out out/solar.png

# ④ 跨模态时间联动：同一时间窗把遥测、事件、载荷图像、帧序列画进一张图
#    窗口取值见 data/aerospace/truth.json 的 linkage_window，那一窗能同时命中四种模态
mdva timeaxis data/aerospace --out out/linkage.png

# ⑤ 并行批量分析：导出 metrics.csv、batch_report.json 与总览图
mdva batch data/cv_segmentation --analyzers bg_stats,image_quality --workers 4 --out out/batch

# ⑥ 测并行加速比（论文实验数据）
mdva speedup data/aerospace/payload --analyzers bg_stats,image_quality,star_detect
```

跑测试确认环境没问题：

```bash
python -m pytest tests -q                      # 310 通过 / 18 跳过，约 65 秒
MDVA_GUI_TESTS=1 python -m pytest tests -q     # 326 通过，连会弹窗的界面测试一起跑
```

---

## 2. 功能

### 2.1 数据接入（`mdva.io`）

五种模态共用一个 `DataItem`，靠 `t_ref`（UTC 秒）互相定位；加载一律**惰性**，`scan` 只探测头。

| 模态 | 格式 | 说明 |
| --- | --- | --- |
| `image` | FITS、16/32 位科学 TIFF、PNG/JPEG/BMP、**调色板掩膜 PNG** | EXIF/`DATE-OBS`/`MJD-OBS` 提取时刻，`t_end = DATE-OBS + EXPTIME` |
| `cube` | 3 维 FITS / 多页 TIFF | 沿第 0 轴的切片序列，用于指向漂移、能段切片 |
| `series` | CSV/TSV、Parquet、HDF5、npy/npz | 自动识别时间列（日期串 / 绝对 UTC 秒 / 相对秒 / 采样序号四种兜底）；`Calibration` 区分**模拟量**（工程值 = 码值×scale+offset）与**离散量**（枚举编码，绝不插值或求均值） |
| `event` | JSON、JSONL、CSV、纯文本日志 | 告警、指令序列、观测窗口，带等级与持续时间 |
| `video` | 帧目录、`.npy` 帧栈（内存映射）、容器视频 | 帧目录自带 `meta.json` 即被识别为一路序列；没有绝对时刻就只按序号浏览，**不假装参与时间联动** |

标定表、真值表、元数据（`*.calib.json`、`meta.json`、`truth.json`、`manifest.json`）都是旁路文件，
扫描器自动识别并跳过，不会混进数据项；时序文件旁边的标定侧车会被自动挂上。

### 2.2 渲染与可视化（`mdva.render`）

- **位深与拉伸**：5 种区间估计（`minmax` `percent` `sigma` `zscale` `median`）× 8 种拉伸
  （`linear` `sqrt` `power` `gamma` `log` `asinh` `histeq` `clahe`）× 6 种伪彩
  （`gray` `viridis` `inferno` `plasma` `hot` `cool`）。zscale 与 asinh 直接用 astropy 的实现，不自己造。
- **彩色与标签分道**：RGB 帧三通道共用一组上下界（逐通道各自拉伸会把颜色平衡抹平）；
  调色板掩膜按类别配色并用最近邻缩放（双线性会在类别边界插出不存在的第三种标签）。
- **长时序降采样**：min-max 包络（保尖峰）、LTTB（保形状）、LOD 金字塔三档；
  配套 `anomaly_retention` 指标量化"抽稀后异常幅度还剩多少"，让"该用哪种降采样"变成可测的问题。
- **标注叠加**：点/框/线/文字 + 栅格占用式标签避让，冲突时只丢文字不丢标记。
- **网格总图**：`render_mosaic` 把任意模态拼成一张对比大图，直接出论文用图。

### 2.3 分析算子（`mdva.analysis`）

9 个内置算子，注册表驱动，插件目录热加载：

| 类别 | 算子 | 做什么 |
| --- | --- | --- |
| 图像 | `bg_stats` | 背景均值/中位/σ、峰值、动态范围 |
| | `star_detect` | 点源提取，对数抛物线亚像元质心（实测中位误差 <0.35 px） |
| | `cosmic_reject` | 中值滤波残差法剔除宇宙线 |
| | `image_quality` | 信噪比、调焦度、高频能量占比、饱和与坏像元比例 |
| 时序 | `rolling_stats` | 滚动统计 |
| | `dynamic_threshold` | 滚动中位 ± k·MAD 动态阈值，替代人工静态门限 |
| | `changepoint` | 二分定位漂移与趋势突变 |
| | `correlation` | 通道相关矩阵 |
| | `iforest` | 孤立森林多通道联合异常 |

所有算子入口强制 `to_gray()`（内含 float64 转换）：uint16 相减会把负残差回绕成大正数，
静默出错而不报错，这类 bug 靠约定挡不住。

### 2.4 并行批处理与数据整理（`mdva.pipeline`、`mdva.collect`）

JSON/YAML 配置驱动的流水线：加载 → 一串算子 → 汇总导出。thread / process 双执行器，
单个文件失败只记一条错误不中断整批，导出 `metrics.csv` + `batch_report.json` + 标注图与总览图。
`spec.uris` 非空时按界面勾选的那几路跑（不必重扫目录），`spec.index` 决定立方体/视频取哪一帧。
每张标注图单独落盘为 `来源目录_文件名.png` 并回填到记录的 `annotated` 字段，界面上点一行就能看那一路。

实测（24 帧等代价 FITS，三算子，线程池）：1 worker 2.70 s → 2 → 1.54 s（1.79×）→
4 → 1.02 s（2.75×）→ 8 → 0.93 s（3.00×），结果逐字段一致。
进程池反而更慢（4 worker 2.48 s）：Windows 下每进程启动 0.3–0.5 s，而单帧只需约 0.1 s。
所以默认线程池，只有单任务耗时远大于进程启动成本时才值得换进程。

`mdva.collect` 管"挑出来之后放哪儿"：复制或移动到指定目录，同名自动编号而不是静默覆盖，
帧目录整棵树搬运；每次操作在目标目录写一份 `mdva_manifest.json`，
`undo` 逆序回放——复制的删掉、移动的搬回，删之前核对字节数与修改时间，
对不上的宁可跳过也不删。这份清单落在数据目录里也不会被扫描器当成一路数据。

### 2.5 示例数据集（`mdva datasets`）

三类，各自压引擎不同的能力，全部程序合成、每类带 `truth.json` 供量化评测。
目录名即界面里的分组名：

| 目录 | 界面分组 | 内容 | 压测点 |
| --- | --- | --- | --- |
| `aerospace` | 航空航天 | 六通道工程遥测 + 标定表 + 指令事件 + 星下点轨迹 + X 射线星场 + 太阳盘面 + 数据立方体 + 太阳演化帧序列 | 高动态位深、跨模态时间联动、离散量语义 |
| `medical` | 医学 | CT 断层帧栈（uint16 HU）+ ICU 生理信号（三导联 ECG/SpO₂/呼吸，含一过性低氧）+ H&E 病理 RGB 与掩膜 | 窗宽窗位、彩色帧、长时序尖峰保留 |
| `cv_segmentation` | 图像分割 | 12 场景 × 原图 + 真值掩膜 + unet/deeplabv3/segformer 三种典型失败模式的预测 + 指标表 | 跨目录并行比对、批量算子 |

观测时刻刻意与指令事件对齐，所以一个时间窗能同时框住四种模态——`truth.json` 里的
`linkage_window` 就是那一段。

### 2.6 界面

**`src/mdva/app.py`（本项目新增，推荐）** —— 标准库 tkinter 写的查看器，就是 `run.bat` 打开的那个窗口。
全中文；左侧三级分组树（学科 → 目录 → 数据项，分组行三态勾选并实时报数）；右侧网格并行出图；
底部游标做跨模态联动，可切「切片序号」浏览无绝对时刻的数据；位深参数、旋转翻转改完即生效，
**影响不到当前勾选的参数自动置灰并在状态栏说明原因**；双击单格放大细看；
批量分析、复制/移动/撤销整理、导出拼图都在界面里直接调 L1–L3，界面本身不含任何算法。
默认只暴露五个动作，其余收在「显示高级参数」里。
重绘走"控件只建一次 + 选择性重画 + 请求合并"的调度，见第 1 节末尾的实测数字。
不依赖 wxPython，不依赖任何第三方 GUI 库。

**`src/mulimgviewer/`（沿用的旧界面）** —— `python MulimgViewer.py`，需要
`requirements/gui.txt` 那套依赖（含 wxPython 与系统 FFmpeg），与引擎不在同一环境。
它是改造的起点，功能上不含上面这些新能力。

---

## 3. 目录结构

```
run.bat / install.bat   双击启动 / 双击装依赖
main.py                 启动入口（负责把 src/ 加进 sys.path）
src/mdva/
  core/       model.py（DataItem/TimeSeries/EventTable/Slice）  registry.py  font.py  timefmt.py
  io/         image.py  series.py  event.py  video.py  timeutil.py     # L1
  render/     stretch.py  downsample.py  overlay.py  image.py  series.py  layout.py   # L2
  analysis/   base.py  image_ops.py  series_ops.py                     # L3
  pipeline.py cli.py  app.py（tkinter 查看器）  datasets.py  synth.py  collect.py
src/mulimgviewer/    沿用的旧 wxPython 界面
tests/mdva/          引擎与界面测试
tests/mulimgviewer/  旧界面侧的位深映射测试
data/                示例数据（mdva datasets 生成）
requirements/        分层依赖清单
code_optimization/   界面重绘路径的基准脚本与优化报告
```

---

## 4. 依赖

| 文件 | 包 | 不装会怎样 |
| --- | --- | --- |
| `requirements.txt` | numpy · scipy · pandas · Pillow | 引擎跑不起来 |
| `requirements/science.txt` | astropy（FITS、zscale、asinh）· scikit-image（CLAHE）· scikit-learn（iforest）· h5py · pyarrow · PyYAML · imageio · imageio-ffmpeg · tifffile | 对应格式或算子不可用，其余照常 |
| `requirements/gui.txt` | wxPython · piexif · reportlab · ffmpeg-python | 只影响旧界面 |
| `requirements/dev.txt` | pytest · pre-commit | 只影响开发 |

一次装齐引擎与测试：

```bash
pip install -r requirements.txt -r requirements/science.txt -r requirements/dev.txt
```

分层文件都是**纯包名清单**（不含 `-r`），这样 `pyproject.toml` 的动态依赖解析才不会报错；
`environment.yml` 让 conda 只管解释器、其余一律走 pip，避免同一个带 C 扩展的包被装两份互相遮蔽。

`requirements/verified-py310.txt` 是当前测试全绿时环境里的精确版本快照，复现实验用它。
`imageio-ffmpeg` 自带静态 FFmpeg 二进制，所以引擎看视频**不需要**系统装 FFmpeg；
旧界面的视频导出走 `ffmpeg-python`，那才需要系统可执行文件。

---

## 5. 来源与许可

本仓库是在开源项目 [MulimgViewer](https://github.com/nachifur/MulimgViewer)（GPL-3.0，
作者 Liu Jiawei）基础上做的毕业设计改造：`src/mulimgviewer/` 沿用其图形界面，
`src/mdva/` 及其测试为本次新增实现，未复制旧界面的代码。整体按 GPL-3.0 许可，详见 `LICENSE`。
