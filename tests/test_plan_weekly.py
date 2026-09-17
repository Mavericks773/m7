import pytest

from m7manager.config import account_dir, dump_config, load_config, validate_patch
from m7manager.dungeon_catalog import instances
from m7manager.dungeon_config import (
    fixed_dungeon_patch,
    merge_config_patch,
    power_plan_patch,
    validate_manual_power,
    validate_plan,
    weekly_patch,
)
from m7manager.services import Manager
from m7manager.ui import SettingsDialog

from .fakes import IMAGE, FakeRuntime

PLAN = [["拟造花萼（金）", "回忆之蕾", 3], ["侵蚀隧洞", "睿治之径", 2]]


@pytest.fixture
def manager(tmp_path):
    m = Manager(tmp_path, FakeRuntime(lambda: 1000), lambda: 1000)
    m.set_image(IMAGE)
    m.add_account("A")
    yield m
    m.close()


def edit(m, patch, version=None):
    a = m.snapshot()["accounts"][0]
    m.edit_account(
        a["id"], "A", True, "04:15", False, 3600, patch, expected_dungeon_version=version
    )


def save_plan(m, keep=False):
    version = m.snapshot()["accounts"][0]["dungeon"]["version"]
    edit(m, power_plan_patch(PLAN, keep, "侵蚀隧洞", "睿治之径", 6), version)


@pytest.mark.parametrize(
    "plan",
    [
        None,
        {},
        [["历战余响", "毁灭的开端", 1]],
        [["侵蚀隧洞", "睿治之径", True]],
        [["侵蚀隧洞", "睿治之径", 0]],
        [["侵蚀隧洞", "睿治之径", 1000]],
        [["侵蚀隧洞", "睿治之径", 1.5]],
        [["侵蚀隧洞", "不存在", 1]],
        [["侵蚀隧洞", "睿治之径"]],
        PLAN * 11,
        [["侵蚀隧洞", "睿治之径", 1], ["侵蚀隧洞", "霜风之径", 1]],
    ],
)
def test_plan_rejects_invalid_entries(plan):
    with pytest.raises(ValueError):
        validate_plan(plan)


def test_plan_maps_recovery_targets_and_rejects_conflicting_fallback():
    patch = power_plan_patch(PLAN, True, "侵蚀隧洞", "睿治之径", 6)
    validate_patch(patch)
    validate_manual_power(patch)
    assert patch["instance_names"]["拟造花萼（金）"] == "回忆之蕾"
    assert patch["power_plan_keep"]
    assert "echo_of_war_enable" not in patch
    with pytest.raises(ValueError, match="兜底"):
        power_plan_patch(PLAN, False, "侵蚀隧洞", "霜风之径", 6)


@pytest.mark.parametrize("day", [True, 0, 8, "1"])
def test_weekly_day_is_strict(day):
    with pytest.raises(ValueError):
        validate_patch({"echo_of_war_start_day_of_week": day})


def test_weekly_only_preserves_ordinary_target_and_does_not_reset_progress(manager):
    save_plan(manager)
    before = manager.snapshot()["accounts"][0]
    name = next(iter(instances("历战余响")))
    edit(manager, weekly_patch(True, 4, name), before["dungeon"]["version"])
    after = manager.snapshot()["accounts"][0]["dungeon"]
    assert after["power_plan"] == PLAN
    assert after["instance_name"] == "睿治之径"
    assert after["weekly_name"] == name and after["weekly_day"] == 4
    assert not manager.snapshot()["accounts"][0]["config"]["build_target_enable"]


def test_plan_requires_version_and_rejects_stale_edit(manager):
    patch = power_plan_patch(PLAN, False, "侵蚀隧洞", "睿治之径", 6)
    with pytest.raises(ValueError, match="版本"):
        edit(manager, patch)
    old = manager.snapshot()["accounts"][0]["dungeon"]["version"]
    save_plan(manager)
    with pytest.raises(ValueError, match="变化"):
        edit(manager, patch, old)


def test_running_plan_cannot_be_replaced_and_unrelated_edit_keeps_progress(manager):
    save_plan(manager)
    a = manager.snapshot()["accounts"][0]
    manager.enqueue(a["id"], "power")
    manager.tick()
    a = manager.snapshot()["accounts"][0]
    with pytest.raises(ValueError, match="运行期间"):
        edit(manager, fixed_dungeon_patch("侵蚀隧洞", "睿治之径", 6), a["dungeon"]["version"])
    with pytest.raises(ValueError, match="运行期间"):
        edit(manager, {"power_plan_keep": True}, a["dungeon"]["version"])
    path = account_dir(manager.root, a["id"]) / "config.yaml"
    config = load_config(path)
    config["power_plan"] = [["侵蚀隧洞", "睿治之径", 1]]
    config["echo_of_war_timestamp"] = 789
    path.write_text(dump_config(config), encoding="utf-8")
    edit(manager, {"daily_enable": False})
    # A weekly change while running is deferred and must not copy the old plan.
    edit(manager, weekly_patch(True, 3, next(iter(instances("历战余响")))))
    assert load_config(path)["daily_enable"]
    assert manager.snapshot()["accounts"][0]["run_dungeon"]["power_plan"] == PLAN
    manager.runtime.finish(manager.store.active()[0]["container_id"])
    manager.tick()
    manager.enqueue(a["id"], "power")
    manager.tick()
    next_config = load_config(path)
    assert next_config["power_plan"] == [["侵蚀隧洞", "睿治之径", 1]]
    assert next_config["echo_of_war_timestamp"] == 789
    assert not next_config["daily_enable"]


def test_plan_and_weekly_ui_roundtrip_order_and_unrelated_save(manager, qtbot):
    save_plan(manager, True)
    account = manager.snapshot()["accounts"][0]
    dialog = SettingsDialog(account)
    qtbot.addWidget(dialog)
    assert "power_plan" not in dialog.payload()["patch"]
    dialog.apply_plan.setChecked(True)
    assert "power_plan" not in dialog.payload()["patch"]
    dialog.plan_editor.table.selectRow(1)
    dialog.plan_editor.move_row(-1)
    dialog.apply_weekly.setChecked(True)
    dialog.weekly_enabled.setChecked(True)
    dialog.weekly_name.setCurrentIndex(1)
    dialog.weekly_day.setCurrentIndex(4)
    manager.edit_account(**dialog.payload())
    result = manager.snapshot()["accounts"][0]["dungeon"]
    assert result["power_plan"] == list(reversed(PLAN))
    assert result["weekly_enabled"] and result["weekly_day"] == 5
    assert result["instance_name"] == "睿治之径"
    manager.enqueue(account["id"])
    manager.tick()
    running = SettingsDialog(manager.snapshot()["accounts"][0])
    qtbot.addWidget(running)
    assert not running.apply_plan.isEnabled()
    assert not running.apply_fixed.isEnabled()


def test_plan_ui_delete_and_cancel_preserves_original(manager, qtbot):
    save_plan(manager)
    dialog = SettingsDialog(manager.snapshot()["accounts"][0])
    qtbot.addWidget(dialog)
    dialog.apply_plan.setChecked(True)
    dialog.plan_editor.table.selectRow(0)
    dialog.plan_editor.remove_row()
    assert dialog.plan_editor.value() == PLAN[1:]
    dialog.reject()
    assert manager.snapshot()["accounts"][0]["dungeon"]["power_plan"] == PLAN


def test_weekly_and_plan_accounts_are_isolated(manager):
    second = manager.add_account("B")
    original = load_config(account_dir(manager.root, second) / "config.yaml")
    save_plan(manager)
    edit(manager, weekly_patch(True, 7, next(iter(instances("历战余响")))))
    assert load_config(account_dir(manager.root, second) / "config.yaml") == original
    assert manager.account(second)["pending_config"] == "{}"


def test_fixed_and_weekly_can_be_combined():
    patch = merge_config_patch(
        fixed_dungeon_patch("侵蚀隧洞", "睿治之径", 6),
        weekly_patch(True, 1, next(iter(instances("历战余响")))),
    )
    validate_patch(patch)
    validate_manual_power(patch)
    assert patch["echo_of_war_enable"]
