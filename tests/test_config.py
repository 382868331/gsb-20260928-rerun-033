from battery_platform.protection import ProtectionConfig


def test_config_can_be_loaded_from_test_dictionary():
    config = ProtectionConfig.from_dict(
        {
            "expected_cells": 2,
            "over_voltage": {
                "trigger_threshold": 4.15,
                "recovery_threshold": 4.05,
                "trigger_delay": 0.2,
                "recovery_delay": 0.4,
            },
            "over_temperature": {
                "trigger_threshold": 50,
                "recovery_threshold": 45,
                "trigger_delay": 0,
                "recovery_delay": 0,
            },
        }
    )

    assert config.expected_cells == 2
    assert config.over_voltage.trigger_threshold == 4.15
    assert config.over_voltage.recovery_delay == 0.4
    assert config.over_temperature.trigger_threshold == 50
    assert config.under_voltage.trigger_threshold == 3.00
