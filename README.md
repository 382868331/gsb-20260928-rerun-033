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
- 接触器诊断分别记录“命令状态”与“反馈观察状态”：命令发出后，反馈在 `contactor_feedback.mismatch_delay`
- 内迟到只更新观察状态，不视为故障；持续不一致（卡住、粘连或反馈缺失）超过该时间才确认
- `charge_contactor_feedback` / `discharge_contactor_feedback` 故障，单侧故障只阻断本侧。
- 接触器故障默认锁定；命令与反馈重新持续一致达到 `recovery_delay` 后才允许人工复位，短暂恢复会重新计时；
- 命令沿变化（如保护中途重开）会重新开始必要等待，重复同一命令不会缩短计时。
- 人工复位要求该原因已恢复且不再有效；任一仍有效的保护不会因为另一项复位而被解除。
- 重新上电由 `repower()` 核对前置条件：双侧均无活动/锁定原因，且当前反馈确认为断开；
- 条件不满足时记录 `repower_blocked`，满足时记录 `power_on_ready` 并允许后续闭合。
- 每次更新后可调用 `JsonlEventRecorder.record_controller_state()` 保存触发和恢复计时器；
  `restore_controller()` 用最后快照跨重启续跑，`replay_recording()` 则从首个样本重建并逐时刻核对事件、接触器和人工复位许可。

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
