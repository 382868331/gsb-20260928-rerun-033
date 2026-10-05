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

## 接触器更换

- `register_contactor_replacement()` 为单只接触器登记更换事件和新的部件身份
  （`part_id`）。旧部件的粘连、矛盾反馈等已确认故障连同当时状态归档进
  `part_history`，继续保留在原部件记录中。
- 更换只清除该位置的进行中不一致计时，让新部件从干净状态重新核对；旧故障不会
  无条件套在新部件上，但其它保护原因、另一只接触器的故障和断开验证都不受影响，
  不会因为换了显示名称就把整个系统记为健康。
- 恢复投运前沿原有断开验证继续核对：更换事件记录 `pre_replacement_evidence`，
  之后的验证、复位与拒绝事件都带上当时有效的 `charge_part_id` /
  `discharge_part_id`，可区分哪些证据发生在更换之前、哪些已针对新部件获得。
  另一只接触器仍有故障或电压衰减观测不足时，即使新部件反馈正常也不得上电。
- `retract_contactor_replacement()` 撤回一条误登记的更换说明：已发出的命令和事件
  保留为事实，旧部件身份及其归档故障恢复生效，尚未完成的恢复决定在
  `contactor_replacement_retracted` 事件的 `recovery_conditions` 中重新说明依据。
- 部件身份、历史归档和更换日志都写入快照；中途重开后沿原验证阶段继续，各次上电
 许可、人工复位及拒绝原因都能追到当时有效的部件和观测。`JsonlEventRecorder`
 的 `record_replacement()` / `record_retraction()` 与 `replay_recording()` 支持
 完整回放更换与撤回序列。

## 部件代际与反馈归属

- 每个接触器位置从代际 1 开始；每次更换或撤回更换都推进该位置的代际
  （`part_generations`），事件细节中的 `charge_part_generation` /
  `discharge_part_generation` 标明证据产生时的有效代际。
- `register_part_feedback()` 是携带部件身份的独立反馈入口：只有点名当前在位
  部件的反馈才进入实时状态（`part_feedback_applied`）；已退役部件的迟到反馈
  只归档进该部件的 `part_history` 条目（`late_part_feedback` 事件与
  `late_feedback` 列表），既不污染新部件，也不能借旧件的正常值通过复位验证。
- 无法确认归属的反馈（缺少 `part_id` 或身份从未安装）保持待定
  （`part_feedback_pending` 事件与 `pending_feedback` 列表），并记录
  `missing_part_id` / `unknown_part_id` 原因，不参与任何保护判断。
- 复位仍要求所有现役部件的条件同时满足：一路更换后，另一路旧件的故障与未完成
  的断开验证继续阻止复位。代际、待定反馈和迟到归档都写入快照，保存未验证状态
 重开后反馈归属与计时窗口完整；`record_part_feedback()` 与
  `replay_recording()` 支持完整回放。

## 待定反馈的人工补认

- `pending_feedback_review()` 面向维修人员展示每条待定反馈的原始报告、身份缺失
  原因（`missing_part_id` / `unknown_part_id`）和该位置的完整安装记录
  （`installation_records()`：部件、代际、安装/退役时间）；同一部件多次安装、
  报告早于最近一次更换（迟到）以及更换记录被撤回时，`ambiguity` 明确列出
  `multiple_installations`、`part_installed_multiple_times`、
  `installation_changed_after_report`、`replacement_retracted_after_report`，
  提示哪些反馈仍有歧义。
- `attribute_pending_feedback()` 用确切部件和安装代次形成独立的补认决定，操作者、
  时间和理由写入 `attribution_log`；原反馈不加改写地归档进 `resolved_feedback`。
  只有补认到当前在位部件的当前代际才进入实时状态；补认到已退役代际只归档进该
  部件的历史，不能借历史报告通过本次恢复验证。重复提交同一有效决定只产生
  `part_feedback_attribution_duplicate` 事件，不会重复计入验证。
- `revoke_attribution()` 撤销误认：原决定保留为事实，撤销作为新决定追加（同样记录
  操作者、时间、理由），反馈原样回到待处理列表，其造成的实时观测回滚到补认前的
  值，之后可重新指定。待处理列表、补认历史、已归档反馈和序号都写入快照，保存
  重开后与当前故障判断保持一致；`record_attribution()` /
  `record_attribution_revoke()` 与 `replay_recording()` 支持完整回放。

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
