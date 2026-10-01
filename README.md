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
- 接触器命令与实际反馈分开记录：保护只产生“请求闭合/断开”，`Measurement` 中的
  `charge_contactor_feedback` / `discharge_contactor_feedback` 才是物理观察状态；
  命令已发出不代表物理动作已完成。
- 反馈与命令不一致须持续 `contactor_mismatch.trigger_delay`（默认 0.5 s）才确认
  `charge_contactor_mismatch` 或 `discharge_contactor_mismatch`。迟到的反馈在确认前
  到达会取消计时；反馈抖动会重新计时；始终不动（触点烧结/机构卡死）在确认后锁定，
  并强制断开双侧接触器。
- 两只串联接触器分别记录命令时间、反馈时间和当前观测值。单只迟到、单只卡住和两侧
  反馈/电压证据互相矛盾都沿同一条记录保留，不能因为另一只接触器反馈正常而清除已确认
  的卡住故障。
- 双侧安全断开必须再做负载侧电压验证：`Measurement.load_voltage` 是断开后的持续观测
  依据；两只反馈均为断开后，负载电压还必须持续低于
  `post_disconnect_voltage.recovery_threshold` 达到 `recovery_delay`。
- 验证未完成、负载电压观测缺失、电压未按预期下降或短暂恢复，都会分别保留
  `waiting_for_contactor_feedback:*`、`load_voltage_observation_missing`、
  `open_feedback_contradicts_voltage` 与 `voltage_recovered` 原因；人工复位不能跳过
  未完成或失败的验证。
- 验证事件记录 `actual_disconnect_contactor`，用持续低压证据说明真正完成电气隔离的是
  哪一只接触器或双侧共同完成；只有反馈断开不能直接允许再次上电。
- 验证期间命令短暂重开又继续断开时，原双反馈断开时间和已积累的低压观察不被清空；
  相关计时器和故障原因写入快照，重启或实验重开后继续等待，已确认的粘连/矛盾故障
  仍保持锁定。
- 等待确认期间命令再次变化（如中途重开/重合）会清空已等待时间，必要等待不得被
  缩短。
- 不一致故障同样锁定：反馈恢复一致并持续 `recovery_delay` 后仅允许人工复位；
  期间短暂恢复会重新计时。复位时只要其它保护仍有效，对应接触器就不会恢复闭合。
- `VirtualContactor` 用虚拟时间模拟动作延迟、抖动与卡死；接触器请求与观察状态都
  写入快照，重新上电后可继续核对不一致故障的锁定与复位前置条件。
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

人工观测脚本会生成在触发/恢复阈值之间抖动的原始样本，并保存完整测量、命令、反馈、
负载电压依据与决策轨迹。实验时可调整两只 `VirtualContactor` 的 `actuation_delay`、
`stuck` 和动作顺序，在 JSONL 中比较不同故障注入位置的命令—反馈—电压时间线。
