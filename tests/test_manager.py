import json
from datetime import datetime

import pytest

from m7manager.config import account_dir, dump_config, load_config
from m7manager.dungeon_config import fixed_dungeon_patch
from m7manager.services import Manager

from .fakes import IMAGE, FakeRuntime


@pytest.fixture
def context(tmp_path):
    now = [datetime.fromisoformat("2026-09-08T00:00:00+08:00").timestamp()]

    def clock():
        return now[0]

    runtime = FakeRuntime(clock)
    manager = Manager(tmp_path / "中文 空格", runtime, clock)
    manager.set_image(IMAGE)
    yield manager, runtime, now
    manager.close()


def test_two_accounts_isolated_and_third_rejected(context):
    m, _, _ = context
    a, b = m.add_account("账号 A"), m.add_account("账号 B")
    assert account_dir(m.root, a) != account_dir(m.root, b)
    config = load_config(account_dir(m.root, a) / "config.yaml")
    assert config["daily_enable"] and config["after_finish"] == "Exit"
    assert len(config) > 100  # Full upstream template, not a minimal stub.
    with pytest.raises(ValueError):
        m.add_account("第三个")


def test_manual_deduplication_and_serial_queue(context):
    m, rt, _ = context
    a, b = m.add_account("A"), m.add_account("B")
    first = m.enqueue(a)
    assert m.enqueue(a) == first
    m.enqueue(b)
    m.tick()
    assert len(m.store.active()) == 1
    assert m.store.active()[0]["account_id"] == a
    assert m.enqueue(a) == first
    rt.finish(m.store.active()[0]["container_id"])
    m.tick()
    assert m.store.active()[0]["account_id"] == b
    old = m.store.one("SELECT * FROM runs WHERE account_id=?", (a,))
    assert old["state"] == "EXITED" and old["business_result"] == "UNCONFIRMED"


def test_parallel_and_cancel_isolation(context):
    m, rt, _ = context
    a, b = m.add_account("A"), m.add_account("B")
    m.set_concurrency(2)
    m.enqueue(a)
    m.enqueue(b)
    m.tick()
    assert len(m.store.active()) == 2
    m.cancel(a)
    m.tick()
    m.tick()
    assert len(m.store.active()) == 1
    assert m.store.active()[0]["account_id"] == b
    assert len([c for c in rt.calls if c[0] == "stop"]) == 1


@pytest.mark.parametrize("fault", ["fail_after_create", "fail_after_start"])
def test_recover_uncertain_create_start_without_duplicate(context, fault):
    m, rt, _ = context
    a = m.add_account("A")
    setattr(rt, fault, True)
    m.enqueue(a)
    m.tick()
    assert m.store.active()[0]["state"] == "RECONCILING"
    m.tick()
    assert m.store.active()[0]["state"] == "RUNNING"
    assert len([c for c in rt.calls if c[0] == "create"]) == 1


def test_restart_adopts_live_container(context):
    m, rt, now = context
    a = m.add_account("A")
    m.enqueue(a)
    m.tick()
    restarted = Manager(m.root, rt, lambda: now[0])
    restarted.tick()
    assert len(restarted.store.active()) == 1
    assert len([c for c in rt.calls if c[0] == "create"]) == 1
    restarted.close()


def test_disconnect_preserves_lock_and_timeout_survives_recovery(context):
    m, rt, now = context
    a = m.add_account("A")
    m.enqueue(a)
    m.tick()
    rt.available = False
    now[0] += 4000
    m.tick()
    assert m.store.active()[0]["state"] == "RECONCILING"
    rt.available = True
    m.tick()
    m.tick()
    assert not m.store.active()
    assert m.store.one("SELECT * FROM runs")["state"] == "TIMED_OUT"


@pytest.mark.parametrize("code,oom,result", [(1, False, "NONZERO_EXIT"), (137, True, "OOM")])
def test_exit_classification(context, code, oom, result):
    m, rt, _ = context
    a = m.add_account("A")
    m.enqueue(a)
    m.tick()
    rt.finish(m.store.active()[0]["container_id"], code, oom)
    m.tick()
    row = m.store.one("SELECT * FROM runs")
    assert row["process_result"] == result and row["state"] == "FAILED"


def test_pending_config_preserves_upstream_progress(context):
    m, rt, _ = context
    a = m.add_account("A")
    m.enqueue(a)
    m.tick()
    path = account_dir(m.root, a) / "config.yaml"
    before = path.read_text(encoding="utf-8")
    m.edit_account(a, "Renamed", True, "05:00", False, 3600, {"power_enable": False})
    assert path.read_text(encoding="utf-8") == before
    config = load_config(path)
    config["last_run_timestamp"] = 123456789
    path.write_text(dump_config(config), encoding="utf-8")
    rt.finish(m.store.active()[0]["container_id"])
    m.tick()
    m.enqueue(a)
    m.tick()
    assert load_config(path)["last_run_timestamp"] == 123456789
    assert load_config(path)["power_enable"] is False
    assert [c[0] for c in rt.calls] == ["create", "start", "remove", "create", "start"]


def test_fixed_dungeon_is_isolated_pending_and_recorded_at_start(context):
    m, _, _ = context
    a, b = m.add_account("A"), m.add_account("B")
    before_a = load_config(account_dir(m.root, a) / "config.yaml")
    before_b = load_config(account_dir(m.root, b) / "config.yaml")
    patch = fixed_dungeon_patch("侵蚀隧洞", "睿治之径", 6)

    m.edit_account(a, "A", True, "04:15", False, 3600, patch)
    pending = m.snapshot()["accounts"][0]["dungeon"]
    assert pending["fixed_mode"] and pending["pending"]
    assert pending["instance_name"] == "睿治之径"
    assert load_config(account_dir(m.root, a) / "config.yaml") == before_a
    assert load_config(account_dir(m.root, b) / "config.yaml") == before_b

    m.enqueue(a, "power")
    m.tick()
    applied = load_config(account_dir(m.root, a) / "config.yaml")
    assert (
        applied["instance_names"]["拟造花萼（金）"] == before_a["instance_names"]["拟造花萼（金）"]
    )
    assert applied["instance_names"]["侵蚀隧洞"] == "睿治之径"
    assert applied["build_target_enable"] is False
    run = m.store.active()[0]
    evidence = json.loads(
        (m.root / "runs" / run["id"] / "dungeon-config.json").read_text(encoding="utf-8")
    )
    assert evidence["fixed_mode"]
    assert evidence["instance_type"] == "侵蚀隧洞"
    assert evidence["instance_name"] == "睿治之径"
    assert evidence["batch_count"] == 6
    assert evidence["image_digest"] == IMAGE


def test_incomplete_dungeon_patch_cannot_leave_override_enabled(context):
    m, _, _ = context
    a = m.add_account("A")
    path = account_dir(m.root, a) / "config.yaml"
    current = load_config(path)
    current["build_target_enable"] = True
    path.write_text(dump_config(current), encoding="utf-8")
    with pytest.raises(ValueError, match="覆盖目标"):
        m.edit_account(
            a,
            "A",
            True,
            "04:15",
            False,
            3600,
            {
                "instance_type": "侵蚀隧洞",
                "instance_names": {"侵蚀隧洞": "睿治之径"},
                "instance_names_challenge_count": {"侵蚀隧洞": 6},
            },
        )


def test_stale_dungeon_dialog_cannot_clear_new_plan_progress(context):
    m, _, _ = context
    a = m.add_account("A")
    version = m.snapshot()["accounts"][0]["dungeon"]["version"]
    path = account_dir(m.root, a) / "config.yaml"
    current = load_config(path)
    current["power_plan"] = [["侵蚀隧洞", "睿治之径", 2]]
    path.write_text(dump_config(current), encoding="utf-8")

    with pytest.raises(ValueError, match="设置窗口打开后变化"):
        m.edit_account(
            a,
            "A",
            True,
            "04:15",
            False,
            3600,
            fixed_dungeon_patch("侵蚀隧洞", "睿治之径", 6),
            expected_dungeon_version=version,
        )
    assert load_config(path)["power_plan"] == [["侵蚀隧洞", "睿治之径", 2]]


def test_running_task_keeps_its_dungeon_when_next_run_is_edited(context):
    m, rt, _ = context
    a = m.add_account("A")
    m.enqueue(a, "power")
    m.tick()
    first = m.snapshot()["accounts"][0]
    assert first["run_dungeon"]["instance_name"] == "回忆之蕾"

    m.edit_account(
        a,
        "A",
        True,
        "04:15",
        False,
        3600,
        fixed_dungeon_patch("侵蚀隧洞", "睿治之径", 6),
        expected_dungeon_version=first["dungeon"]["version"],
    )
    during = m.snapshot()["accounts"][0]
    assert during["run_dungeon"]["instance_name"] == "回忆之蕾"
    assert during["dungeon"]["instance_name"] == "睿治之径"
    assert during["dungeon"]["pending"]

    rt.finish(m.store.active()[0]["container_id"])
    m.tick()
    m.enqueue(a, "power")
    m.tick()
    second = m.snapshot()["accounts"][0]
    assert second["run_dungeon"]["instance_name"] == "睿治之径"


def test_expired_login_blocks_automatic_not_manual(context):
    m, rt, _ = context
    a = m.add_account("A")
    m.enqueue(a)
    m.tick()
    cid = m.store.active()[0]["container_id"]
    rt.output[cid] = ("请使用手机米游社 APP 扫描二维码登录\n等待云游戏登录超时", "")
    rt.finish(cid, 1)
    m.tick()
    assert m.account(a)["auth_state"] == "REAUTH_REQUIRED"
    assert m.account(a)["blocked_reason"]
    m.enqueue(a)
    m.tick()
    cid = m.store.active()[0]["container_id"]
    rt.output[cid] = ("进入云游戏成功", "")
    m.tick()
    assert m.account(a)["auth_state"] == "READY"
    assert not m.account(a)["blocked_reason"]


def test_missing_container_is_not_replayed(context):
    m, rt, _ = context
    a = m.add_account("A")
    m.enqueue(a)
    m.tick()
    rt.containers.clear()
    m.tick()
    assert m.store.one("SELECT * FROM runs")["error_code"] == "CONTAINER_MISSING"
    assert len([c for c in rt.calls if c[0] == "create"]) == 1


def test_orphan_blocks_dispatch(context):
    m, rt, _ = context
    a = m.add_account("A")
    m.enqueue(a)
    rt.create({"id": "orphan", "account_id": "other"}, m.root)
    m.tick()
    assert not m.store.active() and m.alerts


def test_diagnostic_allowlist(context):
    m, _, _ = context
    m.add_account("PRIVATE_ACCOUNT_NAME")
    report = m.export_diagnostics()
    text = open(report, encoding="utf-8").read()
    assert "PRIVATE_ACCOUNT_NAME" not in text
    assert "browser-profile" not in text and "cookie" not in text
    assert json.loads(text)["app_version"] == "0.1.0"


def test_cancel_queued_without_touching_other_containers(context):
    m, rt, _ = context
    a = m.add_account("A")
    m.enqueue(a)
    m.cancel(a)
    m.tick()
    assert not rt.calls


def test_disabled_account_rejects_manual_and_cancels_queue(context):
    m, _, _ = context
    a = m.add_account("A")
    m.enqueue(a)
    m.edit_account(a, "A", False, "04:15", False, 3600, {})
    assert m.store.one("SELECT state FROM triggers")["state"] == "CANCELLED"
    with pytest.raises(ValueError):
        m.enqueue(a)


def test_invalid_task_and_image_rejected(context):
    m, _, _ = context
    a = m.add_account("A")
    with pytest.raises(ValueError):
        m.enqueue(a, "main; echo unsafe")
    with pytest.raises(ValueError):
        m.set_image("untrusted/image:latest")


def test_database_enforces_one_active_per_account(context):
    import sqlite3

    m, _, _ = context
    a = m.add_account("A")
    m.enqueue(a)
    m.tick()
    run = m.store.active()[0]
    with pytest.raises(sqlite3.IntegrityError):
        m.store.execute(
            """INSERT INTO runs (id,trigger_id,account_id,task,state,image_digest,timeout_seconds,created_at)
            VALUES ('duplicate',?,?,?,'PREPARING',?,3600,0)""",
            (run["trigger_id"], a, "main", IMAGE),
        )


def test_created_container_expires_while_manager_offline(context):
    m, rt, now = context
    a = m.add_account("A")
    rt.fail_after_create = True
    m.enqueue(a)
    m.tick()
    now[0] += 7201
    m.tick()
    assert m.store.one("SELECT state FROM runs")["state"] == "MISSED"
    assert not any(call[0] == "start" for call in rt.calls)


def test_failed_result_file_does_not_resurrect_finished_run(context, monkeypatch):
    from m7manager import services

    m, rt, _ = context
    a = m.add_account("A")
    m.enqueue(a)
    m.tick()
    rt.finish(m.store.active()[0]["container_id"])
    original = services.atomic_text

    def fail_result(path, text):
        if path.name == "result.json":
            raise OSError("disk full")
        original(path, text)

    monkeypatch.setattr(services, "atomic_text", fail_result)
    m.tick()
    assert not m.store.active()
    assert m.store.one("SELECT state FROM runs")["state"] == "EXITED"
    assert m.store.one("SELECT state FROM triggers")["state"] == "EXITED"


def test_shutdown_cancels_pending_and_stops_active(context):
    m, _, _ = context
    a, b = m.add_account("A"), m.add_account("B")
    m.enqueue(a)
    m.enqueue(b)
    m.tick()
    m.shutdown()
    m.tick()
    m.tick()
    assert not m.store.active()
    assert {r["state"] for r in m.store.rows("SELECT state FROM triggers")} == {"CANCELLED"}
