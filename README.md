# MDVA：本地化多模态科研标注平台

版本 **0.1.1**。主入口为本地浏览器界面，Python 提供本机 HTTP 服务、SQLite 存储与数据处理。支持图像 / 立方体 / 时序 / 事件 / 帧序列，当前真实数据工作流以太阳结构图像分割为主。

## 启动

Windows 在项目目录双击 **run.bat**，或运行：

```powershell
python main.py
```

打开 http://127.0.0.1:8765 。首次运行会创建工作区；默认不再启动旧 Tkinter 客户端。项目中的 `datas of importance/Situation Awareness Dataset` 存在时自动登记为 `ssafd` 只读源。已经启动同一工作区的新服务时重复运行会打开现有网页；旧服务或其他程序占用端口时会明确报错，不自动终止它。

显式指定目录：

```powershell
python main.py serve --data data --out out --source "ssafd=datas of importance/Situation Awareness Dataset" --open
```

`--source 名称=目录` 可重复使用，路径只在启动时登记。`--db` 仅用于已有的 `mdva.sqlite`，优先于 `--data`，不会为拼错的路径新建空库。服务默认只监听 `127.0.0.1`。

## 安装与前端构建

Python 3.10+。已有依赖环境可以直接启动，不要求 Git 或 editable 安装；新环境可运行 `install.bat`，或：

```powershell
python -m pip install -e ".[science,dev]"
```

随源码及 wheel 分发网页构建产物，普通使用不需要 Node。修改前端后运行：

```powershell
cd frontend
npm ci
npm run typecheck
npm run lint
npm run build
```

产物由同一个 Python 服务托管，不使用外部 CDN。Python 包版本、运行时版本和 CITATION 均为 0.1.1，版本来自 `mdva.__version__`。

## 真实太阳数据

支持以下 SSAFD 目录结构：

```text
Situation Awareness Dataset/
  AR/AR_image/*.jpg
  AR/AR_npy/*.npy
  AR/MASK-NOAA.xlsx
  CH/CH_image/*.jpg
  CH/CH_npy/*.npy
  FL/FL_image/*.jpg
  FL/FL_npy/*.npy
```

管理台点击「导入太阳数据」→ 选择已登记源 → 预检 → 全量索引或各类别 30 对验收样本。预检读取图像和 NPY 头部、NOAA 对应表，不把全部像素载入内存。导入异常与完整配对清单持久化在数据库，任务报告列出缺失与无效文件。

当前本机实测数据：

| 类别 | 图像 | 掩膜 | 同名配对 | 仅图像 | 仅掩膜 |
|---|---:|---:|---:|---:|---:|
| AR | 5,092 | 4,308 | 4,308 | 784 | 0 |
| CH | 3,831 | 3,824 | 3,813 | 18 | 11 |
| FL | 4,625 | 3,836 | 3,793 | 832 | 43 |
| 合计 | 13,548 | 11,968 | 11,914 | 1,634 | 54 |

**类别取 AR / CH / FL 目录，掩膜非零像素值是实例编号。** AR 使用 `FILENAME + MASK_NUM` 关联 NOAA 编号，冲突与未匹配保留。无掩膜图像保留为未标注样本，孤立掩膜不伪装成图像。通用扫描不再把二维整型 NPY 掩膜当成时序；此类载体请使用专用数据源导入。

源图像与掩膜保持只读；数据库使用 `@ssafd/相对路径` 引用，迁移数据目录后可用同名 `--source` 重新定位。SQLite 和派生产物放在独立工作区，不写入原始源目录。

## 标注、清洗与增强

管理台提供服务端分页、类别/状态/模态筛选、搜索和可见区缩略图。选中图像后点击「去标注」。画布可显示原始掩膜、调整透明度、筛选实例及查看 NOAA 关联。

「复制轮廓到人工草稿」建立独立人工版本，支持多边形顶点编辑、撤销重做、快捷键、保存和审核。**多边形是轮廓近似，不能替代含孔洞的原始像素真值。** 本版不含像素画笔或橡皮。

清洗与增强页面明确显示输入版本与处理范围：管理台当前页、已选条目或整个版本。先预览再执行，执行产生新版本。增强同时变换图像、原始实例掩膜与人工几何；来源组不跨训练/验证/测试集。回退只处理任务登记的派生产物；撤销清单会在任何删除/移动之前整体校验。

同步长任务的「停止等待」只会断开前端请求，服务端会继续完成并记录任务。可在右上角任务清单查看真实完成或失败状态。

## 导出

- **原始分割真值**：COCO，`annotation_source=original`。保留实例编号、精确像素、孔洞及不连通区域，附带原始编号掩膜、像素外接检测框和 NOAA 元数据；分割使用 COCO 未压缩 RLE。
- **当前人工标注**：COCO / YOLO / VOC / 多模态 JSON。YOLO 分割选 `task=segment`；检测时可开启 `polygons_as_boxes` 从多边形派生检测框。VOC 的分割多边形为扩展字段，标准检测读方使用外接框。
- 每次默认导出到独立目录，记录输入版本、参数、种子、跳过原因及裁切报告。点击「回读校验」核对结构、数量和坐标；原始掩膜导出额外检查栅格和 RLE 的像素一致性。

原始掩膜和人工修订分开导出。YOLO/VOC 不会被宣称为原始像素真值的无损格式。

## 算法与限制

「算法」页面提供可用后端、能力说明、失败原因、实测、推荐与门槛。深度学习后端仍为占位，不参与有效推荐或实际推理。随附演示实测表不能当作 SSAFD 上的准确率结论。

本版面向本机单用户，没有账号系统、真实多人协作、模型再训练或 token 鉴权。离线指运行时无需云服务；安装依赖需要联网或提前准备离线包。时序/事件/立方体引擎保留，旧桌面查看器仅保留源码。

## 验证与交接

```powershell
python -m pytest tests -q -ra
python scripts/verify_ssafd.py --source "datas of importance/Situation Awareness Dataset"
```

第二条在独立 `out/acceptance/real` 工作区验证 AR/CH/FL 各 30 对数据，包含原始掩膜逐像素导出、人工标注保存与重启读取、审核、清洗回退、增强重放、四格式回读和源文件 SHA-256 核对。结果写入 `out/acceptance/real/report.json`。它是工程验收，不是模型精度实验。

详细变更和当前限制见 [接手修复记录](docs/接手修复记录-20261107.md)。此前交接资料作为历史证据保留，其冻结、未合入、旧测试数量和“客户端入口”等状态可能已变化，以当前代码与本轮验收为准。

## 许可与引用

GPL-3.0，见 [LICENSE](LICENSE)。软件作者与引用信息见 [CITATION.cff](CITATION.cff)。仓库最初包含 MulimgViewer 上游查看器，本项目当前默认入口为自有 MDVA 网页工作流；保留相关版权与许可。数据集本身的许可及引用义务独立于软件许可，交付与公开数据时应保留原数据来源信息。
