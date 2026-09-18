import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

from m7manager.services import Manager
from m7manager.ui import MainWindow, SettingsDialog

from .fakes import IMAGE, FakeRuntime


def test_window_displays_two_accounts_and_emits_commands(qtbot, tmp_path):
    manager = Manager(tmp_path, FakeRuntime(lambda: 1000), lambda: 1000)
    manager.set_image(IMAGE)
    a = manager.add_account("测试账号 A")
    manager.add_account("测试账号 B")
    window = MainWindow(manager, start_worker=False)
    qtbot.addWidget(window)
    window.update_snapshot(manager.snapshot())
    window.show()
    assert window.cards[0].title.text() == "测试账号 A"
    assert not window.add.isEnabled()
    with qtbot.waitSignal(window.command) as signal:
        qtbot.mouseClick(window.cards[0].run_button, Qt.MouseButton.LeftButton)
    assert signal.args == ["run", {"account": a, "task": "main"}]
    window.request_quit()
    manager.close()


def test_settings_preserve_existing_values(qtbot, tmp_path):
    manager = Manager(tmp_path, FakeRuntime(lambda: 1000), lambda: 1000)
    manager.add_account("设置测试")
    dialog = SettingsDialog(manager.snapshot()["accounts"][0])
    qtbot.addWidget(dialog)
    dialog.local_time.setText("06:30")
    dialog.power.setChecked(False)
    dialog.queue_timeout.setValue(25)
    payload = dialog.payload()
    manager.edit_account(**payload)
    updated = manager.snapshot()["accounts"][0]
    assert updated["schedule"]["local_time"] == "06:30"
    assert updated["config"]["power_enable"] is False
    assert updated["config"]["cloud_game_max_queue_time"] == 25
    manager.close()


def test_settings_apply_fixed_dungeon_and_show_account_summary(qtbot, tmp_path):
    manager = Manager(tmp_path, FakeRuntime(lambda: 1000), lambda: 1000)
    manager.add_account("副本测试")
    account = manager.snapshot()["accounts"][0]
    dialog = SettingsDialog(account)
    qtbot.addWidget(dialog)

    assert not dialog.instance_type.isEnabled()
    original_build_target = dialog.build_target.isChecked()
    dialog.apply_fixed.setChecked(True)
    assert not dialog.build_target.isEnabled()
    assert not dialog.build_target.isChecked()
    dialog.apply_fixed.setChecked(False)
    assert dialog.build_target.isEnabled()
    assert dialog.build_target.isChecked() is original_build_target
    dialog.apply_fixed.setChecked(True)
    dialog.instance_type.setCurrentIndex(dialog.instance_type.findData("侵蚀隧洞"))
    dialog.instance_name.setEditText("不存在的副本")
    assert dialog._selected_instance_name() is None
    dialog.instance_name.setCurrentIndex(dialog.instance_name.findData("睿治之径"))
    dialog.challenge_count.setValue(6)
    manager.edit_account(**dialog.payload())

    updated = manager.snapshot()["accounts"][0]
    assert updated["dungeon"]["fixed_mode"]
    assert updated["dungeon"]["instance_name"] == "睿治之径"
    card = MainWindow(manager, start_worker=False)
    qtbot.addWidget(card)
    card.update_snapshot(manager.snapshot())
    assert "侵蚀隧洞 · 睿治之径 · 6 次/批" in card.cards[0].next.text()
    card.request_quit()
    manager.close()


def test_settings_can_enable_build_target(qtbot, tmp_path):
    manager = Manager(tmp_path, FakeRuntime(lambda: 1000), lambda: 1000)
    account_id = manager.add_account("培养目标测试")
    account = manager.snapshot()["accounts"][0]
    manager.edit_account(
        account_id,
        account["display_name"],
        True,
        account["schedule"]["local_time"],
        False,
        account["timeout_seconds"],
        {"build_target_enable": False},
    )
    dialog = SettingsDialog(manager.snapshot()["accounts"][0])
    qtbot.addWidget(dialog)
    assert not dialog.build_target.isChecked()

    dialog.build_target.setChecked(True)
    payload = dialog.payload()
    assert payload["patch"]["build_target_enable"] is True
    manager.edit_account(**payload)
    updated = manager.snapshot()["accounts"][0]
    assert updated["config"]["build_target_enable"] is True
    assert "build_target_enable" in updated["dungeon"]["conflicts"]
    manager.close()
