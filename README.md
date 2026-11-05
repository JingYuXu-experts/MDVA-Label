
# MDVA — 本地化多模态智能标注平台

完全离线、只绑 `127.0.0.1` 的本地多模态数据标注平台：统一接入图像 / 数据立方 / 时序 / 事件 / 视频五种模态，提供渲染、分析、标注、清洗、增强、导出全链路能力。主入口为本地 Web 界面，零出站请求，不依赖任何云端服务。

**本项目独立于知名开源标注项目 [labelme](https://github.com/wkentaro/labelme)，无任何代码或组织关联。** 本项目基于上游开源项目 [MulimgViewer](https://github.com/ninebell-gg/MulimgViewer)（多图像浏览器）二次开发，保留其许可证与署名，详见文末"来源与许可"。

## 核心能力

- **5 种模态统一模型**：image / cube / series / event / video 统一到 `DataItem`，跨模态仅靠参考时间 `t_ref`（UTC 秒）联动
- **13 个可替换算法后端**：9 个经典算子 + 视觉提示（模板匹配）+ 3 个深度模型占位（见"关于深度模型"）；能力探测、实测表驱动的推荐排序与准入门槛
- **标注内核**：SQLite 单一真源、版本 DAG（parent/op/params/stats 可追溯）、draft→submitted→approved/rejected 四态审核机与角色越权拦截、视觉提示传播、并行预标注、主动学习
- **清洗与增强**：预览与执行共用同一代码路径（预览数字=实际执行条数）；增强配方 + seed 谱系逐字节可重放；一键回退只删登记在册且字节校验一致的产物，**原件永不删除**
- **4 种导出格式**：COCO / YOLO / VOC / 多模态 JSON，训练/验证/测试划分，导出后回读校验，附带可离线运行的 `train.py` 与 `DATASET.md`
- **63 个 HTTP API 端点**：一次调用 = 一个界面动作；Web 层零新增依赖（Python 标准库 `http.server` 实现）

规模：自研引擎 `src/mdva` 约 16,000 行，测试约 7,300 行（620+ 用例全绿）；三分学科示例数据集（航天 7 路 / 医学 15 路 / CV 分割 60 路，带 truth.json 供评测）。

## 安装

要求：Python 3.10（conda 环境），git（`pip install -e .` 依赖 setuptools_scm 从 git 取版本）。

```bash
git clone <你的仓库地址> && cd <仓库目录>
pip install -e .            # 基础引擎（numpy/opencv/astropy 等见 requirements/）
pip install -r requirements/web.txt   # 可选：Web 层零新增依赖，此文件仅为说明
```

## 前端构建

Web 界面基于 Vite + Vue 3 + TypeScript + Pinia：

```bash
cd frontend
npm install
npm run build    # 产物输出到 src/mdva/web/static
```

未构建前端时，服务首页会显示构建指引说明页（非报错）。

## 启动

```bash
python -m mdva serve --data ./data --db ./out/mdva.db --out ./out --open
# 浏览器打开 http://127.0.0.1:8765
```

CLI 共 14 个子命令：`scan / render / mosaic / analyze / timeaxis / batch / speedup / datasets / clean / augment / export / validate / bench / serve`。

## 安全与使用边界

- **仅限本机单用户使用**：服务只绑 `127.0.0.1`；`--host 0.0.0.0` 未带 `--allow-remote` 时拒绝启动
- 完全离线：进程不发起任何出站请求，`tests/mdva/test_offline.py` 静态扫描源码 + 动态拦截 socket 双重把关
- 可选本地 token 鉴权：启动时随机生成，注入 `index.html`，防同机其他进程读取
- 接口只认工作区名字、不认任意路径，杜绝路径穿越

## 关于深度模型

按项目决策，本平台**不下载、不内置任何大模型权重**（T-Rex2 / GroundingDINO / YOLO-World）。三个深度后端为**占位实现**：`available()` 恒返回 False 并说明原因，不参与推荐排序与准入门槛判定。全部可用后端在 CPU 上运行，零显存依赖。接口层保留统一的后端注册机制，未来可自行接入本地 checkpoint。

## 界面入口说明

| 入口 | 技术 | 定位 |
|---|---|---|
| Web 界面（`python -m mdva serve`） | Vue 3 SPA + 标准库 HTTP 服务 | **主入口**，六视图：管理台/标注画布/清洗/增强/导出/后端 |
| `src/mdva/app.py` | tkinter | 离线轻量查看器（备用） |
| `src/mulimgviewer`（上游） | wxPython | 上游多图像浏览器，保留作离线查看器 |

## 测试

```bash
pip install -e .[dev]   # 或安装 pytest
pytest                  # 620+ passed；GUI 弹窗测试需显式 MDVA_GUI_TESTS=1
```

## 项目结构

```
src/mdva/          自研引擎：io / render / analysis / annotate / clean / augment / export / backends / web / cli
src/mulimgviewer/  上游遗留（wxPython 离线查看器）
frontend/          Vue 3 前端源码（构建产物入 src/mdva/web/static）
tests/mdva/        全量测试（含断网自检 test_offline.py）
data/              三分学科示例数据集
experiments/       S5 实验固化脚本（E1–E6 一键复现）
```

## 来源与许可

- 本项目整体以 **GPL-3.0** 许可发布（见 [LICENSE](LICENSE)）
- 上游项目：[MulimgViewer](https://github.com/ninebell-gg/MulimgViewer)（多图像浏览器），其版权与许可声明原样保留于 LICENSE、源码头部与 docs/；本仓库 `src/mulimgviewer` 为上游代码，仅作离线查看器保留
- 参考基准：百度 EasyData 智能数据服务平台（流程参照）、IDEA 研究院 T-Rex Label（视觉提示交互参照）；本项目为独立本地化重实现，不冒称原创流程，亦不代表上述任何机构
- 第三方依赖与模型引用按各自许可与引用要求署名（详见计划书参考文献节）
