# 当前：20261115-R1已获用户授权，M1通过，受限队列执行中

先查看训练页真实状态和 docs/修订训练-工程验收与运行记录-20261115.md。使用python -m mdva.training.reviewed_queue；旧队列禁止，C0/新FL/预测/最终测试不自动启动。以下待验收文字为历史快照。

# 最新计划：20261115-R1，待用户验收

训练保持停止。先读 docs/模型训练-诊断后修订计划与验收-20261115.md；机器草案 recognition-revision-20261115-proposal.json 不可执行。先修保存/恢复，再分批补AR/CH对照；不直接继续旧42项队列。

# 当前任务：效果诊断已交付，训练保持停止

先读取 docs/模型效果诊断-执行与验收-20261115.md 和 out/model-diagnostics/20261115-03/interpretation.md。用户确认后续取舍、WinError 5修复与恢复验证通过前，不运行unpause或continue。

# 模型实验接续入口

先读 `docs/模型训练-M0至M6实施与接续总账-20261112.md`，再运行：

```bat
conda activate mdva311
python -m mdva.training status
```

实验协议及数据：`out/training-20261112-v2/protocol.json`、`out/pretraining-20261112/dataset-ready/`。
网页入口：`/training`。运行状态从本地磁盘读取，不依赖浏览器连接。

`python -m mdva.training continue` 顺序继续尚未完成的识别实验。`pause` 请求epoch边界保存，`unpause`清除标记。
任何配置、源码或数据变更必须先审计实验身份。不要覆盖或删除旧检查点来绕过版本检查。

权重、运行记录和原始数据不纳入Git；源码与本接续协议在仓库保留。旧 `dataset-v1` 是失败证据，不能用于训练。

识别v1保留数值溢出失败证据；当前识别v2从头训练。当前预测关联v2由active-forecast.json指向，继续用 python -m mdva.training.forecast_revision。不得引用已被替代的参数行级结果作独立AR日结论。

网页顶部的“训练进度与接续日志”与 out/training-20261112-v2/训练进度与接续.md 是实时接续入口；先核对状态，再继续，不能仅凭聊天快照判断进程已停。
