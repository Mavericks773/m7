"""Pinned upstream methods, executed with isolated collaborators (no game imports).

The fixture preserves method behavior from the recorded GPL-3.0 upstream source;
AST serialization removes only formatting/comments/imports and unrelated methods.
These tests verify source contracts, not the contents of an installed Docker image.
"""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from m7manager.dungeon_config import power_plan_patch

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/upstream_dungeon_contract.json").read_text("utf-8")
)
PLAN = [["拟造花萼（金）", "回忆之蕾", 3], ["侵蚀隧洞", "睿治之径", 2]]


def upstream(key, **collaborators):
    scope = {"log": Mock(), **collaborators}
    exec(compile(FIXTURE["sources"][key]["code"], FIXTURE["sources"][key]["path"], "exec"), scope)
    return scope


@pytest.mark.parametrize("keep", [False, True])
@pytest.mark.parametrize(
    "results,remaining",
    [
        ([3, 2], []),
        ([1, 0], [["拟造花萼（金）", "回忆之蕾", 2], PLAN[1]]),
        ([0], PLAN),
        ([RuntimeError("battle failed"), 2], PLAN[:1]),
    ],
)
def test_upstream_plan_deduction_retention_and_failure(results, remaining, keep):
    data = {"power_plan": deepcopy(PLAN), "power_plan_keep": keep}
    cfg = SimpleNamespace(
        get_value=lambda k, default: data.get(k, default),
        set_value=lambda k, v: data.__setitem__(k, v),
    )
    power = upstream("power", cfg=cfg, Instance=SimpleNamespace(validate_instance=lambda *_: True))[
        "Power"
    ]
    power.process = Mock(side_effect=results)
    power.execute_power_plan()
    assert data["power_plan"] == (PLAN if keep else remaining)
    assert power.process.call_args_list[0].kwargs["planned_attempts"] == 3


def test_plan_fallback_and_retry_lookup_use_same_names():
    data = power_plan_patch(PLAN, False, "侵蚀隧洞", "睿治之径", 6)
    cfg = SimpleNamespace(
        **data,
        get_value=lambda k, default: data.get(k, default),
        set_value=lambda k, v: data.__setitem__(k, v),
    )
    instance = upstream("instance", cfg=cfg)["Instance"]
    instance.validate_instance = lambda *_: True
    for kind, name, _ in PLAN:
        assert instance.get_current_instance_name(kind) == name
    power = upstream("power", cfg=cfg, Instance=instance)["Power"]
    power.preprocess = Mock()
    power.process = Mock(side_effect=[3, 2, 6])
    power.run()
    assert [call.args[:2] for call in power.process.call_args_list] == [
        ("拟造花萼（金）", "回忆之蕾"),
        ("侵蚀隧洞", "睿治之径"),
        ("侵蚀隧洞", "睿治之径"),
    ]
    assert power.process.call_args_list[-1].kwargs == {}


@pytest.mark.parametrize(
    "reward,power,attempts", [(0, 120, 0), (3, 20, 0), (3, 60, 2), (1, 180, 1)]
)
def test_weekly_uses_reward_count_and_power(reward, power, attempts):
    cfg = SimpleNamespace(
        build_target_enable=False, instance_names={"历战余响": "毁灭的开端"}, save_timestamp=Mock()
    )
    auto = Mock()
    auto.ocr_result = [(None, (f"{reward}/3", 1))]
    instance = Mock()
    weekly = upstream(
        "weekly",
        cfg=cfg,
        auto=auto,
        screen=Mock(),
        time=Mock(),
        Power=SimpleNamespace(get=lambda: power),
        Instance=instance,
        BuildTarget=Mock(),
    )["Echoofwar"]
    weekly.start()
    if attempts:
        instance.run.assert_called_once_with("历战余响", "毁灭的开端", attempts, 1)
    else:
        instance.run.assert_not_called()
    assert cfg.save_timestamp.called is (reward == 0)


@pytest.mark.parametrize(
    "weekday,refreshed,power_enabled,expected_weekly",
    [
        (2, True, True, False),
        (4, True, True, True),
        (7, True, True, True),
        (1, True, True, False),
        (4, False, True, False),
        (4, True, False, False),
    ],
)
def test_weekly_schedule_gate_and_order(weekday, refreshed, power_enabled, expected_weekly):
    cfg = SimpleNamespace(
        reward_enable=False,
        build_target_enable=False,
        daily_enable=False,
        power_enable=power_enabled,
        echo_of_war_enable=True,
        echo_of_war_timestamp=100,
        refresh_hour=4,
        echo_of_war_start_day_of_week=4,
    )
    events = []
    daily = upstream(
        "daily",
        cfg=cfg,
        activity=SimpleNamespace(start=lambda: events.append("activity")),
        Date=SimpleNamespace(is_next_mon_x_am=lambda *_: refreshed),
        datetime=SimpleNamespace(
            date=SimpleNamespace(today=lambda: SimpleNamespace(isoweekday=lambda: weekday))
        ),
        Echoofwar=SimpleNamespace(start=lambda: events.append("weekly")),
        Power=SimpleNamespace(run=lambda: events.append("power")),
    )["Daily"]
    daily.prepare_daily()
    assert events == ["activity"] + (["weekly"] if expected_weekly else []) + (
        ["power"] if power_enabled else []
    )
