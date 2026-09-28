# 电池保护研发平台（首轮）

本轮实现虚拟电池组的过压、欠压、过温和测量缺失保护。模拟器只产生单体电压、温度及缺失值等原始观测；保护状态机独立判断，不接收模拟器给出的结论。

## 能力

- 触发阈值、释放阈值、触发持续时间、恢复持续时间和锁定开关均可通过 `ProtectionConfig` 配置。
- 一次短尖峰不会触发；异常持续达到配置时长后断开接触器。
- 触发值与释放值之间为迟滞区，边界抖动不会反复分合。
- 任一电压或温度缺失都会形成 `measurement_invalid`，不能按正常测量处理。
- 锁定保护必须人工 `reset`，且当前观测满足恢复持续时间后才允许解除。
- JSONL 保存原始观测、保护事件和逐拍决策；事件包含动作、原因、恢复条件及对应测量。
- 重放只读取原始测量并重新执行判断逻辑，可验证记录可回放。

## 运行

```bash
python -m pytest -q
python -m battery_protection.observe --output run.jsonl
```

核心组件：

- `battery_protection/simulator.py`：虚拟电池组，输出 `Measurement`。
- `battery_protection/controller.py`：保护判断、迟滞计时、锁定和人工复位。
- `battery_protection/recorder.py`：JSONL 记录与原始测量重放。
- `battery_protection/observe.py`：边界抖动人工观测场景。
