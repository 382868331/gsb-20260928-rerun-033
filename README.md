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
- 等待确认期间命令再次变化（如中途重开/重合）会清空已等待时间，必要等待不得被
  缩短。
- 不一致故障同样锁定：反馈恢复一致并持续 `recovery_delay` 后仅允许人工复位；
  期间短暂恢复会重新计时。复位时只要其它保护仍有效，对应接触器就不会恢复闭合。
- `VirtualContactor` 用虚拟时间模拟动作延迟、抖动与卡死；接触器请求与观察状态都
  写入快照，重新上电后可继续核对不一致故障的锁定与复位前置条件。
- 每次更新后可调用 `JsonlEventRecorder.record_controller_state()` 保存触发和恢复计时器；
  `restore_controller()` 用最后快照跨重启续跑，`replay_recording()` 则从首个样本重建并逐时刻核对事件、接触器和人工复位许可。

## 双接触器保护实验

- 两只串联接触器各自动作、各自反馈：单只迟到（确认延迟内到达）不记故障；
  单只卡住确认 `charge_/discharge_contactor_mismatch` 并锁定；
  两只反馈互相矛盾（同一命令下报告相反状态）单独确认 `feedback_contradiction`。
- 安全断开明确归属：`contactor_open_confirmed` 事件和 `disconnect_completed_by`
  指出实际由哪只完成断开；另一只反馈正常不会清除已确认的粘连故障，
  只有该极自身反馈持续健康并经人工复位才解锁。
- `run_experiment(ExperimentScenario(...))` 可调整两级动作延迟（动作先后）与
  `stuck` 故障注入位置，沿 JSONL 记录逐样本核对命令、反馈与母线电压依据。

## 断开后验证

- 两只接触器都报告断开后进入验证阶段：母线电压必须持续低于
  `post_open_verification.trigger_threshold` 达 `trigger_delay` 才允许再次上电
  （`repower_allowed` / `verification_passed` 事件）。
- 电压未按预期下降、观测缺失、短暂恢复分别保留为
  `voltage_not_decayed` / `observation_missing` / `brief_recovery`，
  验证通过前一直留在 `verification_status.pending_reasons`。
- 验证中途重开只暂停计时，已累计的等待保留，重新断开后继续原等待。
- 人工复位不能略过未完成的验证：验证进行中复位被阻塞并记
  `post_open_verification`；复位事件始终附带当前验证状态。

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
