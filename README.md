# 虚拟电池组保护逻辑

首轮实现 Python 侧单体状态观测、保护决策和可回放记录，暂不包含均衡与寿命估计。

## 分层

- VirtualBatteryPack 只生成原始电压、温度测量；缺失值用 None 表示，不输出任何保护结论。
- ProtectionController 独立判断过压、欠压、过温和测量缺失，执行触发计时、恢复计时和锁定。
- JsonlEventRecorder 将原始测量和保护事件逐行写入 JSONL，便于离线回放。

## 保护语义

- 异常必须持续达到 trigger_delay 才触发，单次短尖峰不会动作。
- 过压断开充电接触器，欠压断开放电接触器，过温和测量缺失断开双侧接触器。
- 默认保护原因会锁定；恢复值持续达到 recovery_delay 后，只记录“可恢复”，仍需人工复位。
- 恢复阈值与触发阈值分离，边界来回抖动不会反复开闭接触器。
- 任意缺失测量或通道数量错误都立即视为测量故障，不能按正常值放行。

恢复计时只有在越过恢复阈值后才开始；测量落在触发与恢复阈值之间（例如 4.15V
介于 4.20V 触发与 4.10V 恢复之间）时保持保护动作并拒绝人工复位。恢复期间重新
越界会清零计时并重新开始，同时记录 `recovery_interrupted`。多种保护原因各自
独立触发、计时、锁定和复位，解除其中一种不会放开其他原因仍在断开的接触器。

## 记录重放核对

`replay_protection(path)` 用 JSONL 中的原始测量重新驱动一个全新的
`ProtectionController`，逐条比对事件类型、接触器状态、活动与锁定原因，而不是
只检查日志里是否写出阈值。重启造成末行 JSON 截断时会忽略该残行，仍可核对此前
的完整记录：

~~~python
from battery_platform.recorder import replay_protection

result = replay_protection("protection_log.jsonl")
assert result.consistent
assert result.controller.charge_contactor_closed
~~~

## 测试配置

可直接在测试中构造配置，也可从字典加载：

~~~python
from battery_platform.protection import ProtectionConfig

config = ProtectionConfig.from_dict({
    "expected_cells": 4,
    "over_voltage": {
        "trigger_threshold": 4.20,
        "recovery_threshold": 4.10,
        "trigger_delay": 1.0,
        "recovery_delay": 1.0,
        "locked": True,
        "direction": "above",
    },
    "under_voltage": {
        "trigger_threshold": 3.00,
        "recovery_threshold": 3.10,
        "trigger_delay": 1.0,
        "recovery_delay": 1.0,
        "locked": True,
        "direction": "below",
    },
})
~~~

## 验证

~~~bash
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python -m battery_platform.manual_observe --output protection_log.jsonl
~~~

人工观测脚本会生成在触发/恢复阈值之间抖动的原始样本，并保存完整测量与决策轨迹。
