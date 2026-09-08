from unittest.mock import Mock

import pytest

from m7manager.docker_runtime import ACCOUNT, OWNER, RUN, DockerRuntime

from .fakes import IMAGE


def test_create_uses_isolated_mounts_and_single_run(tmp_path):
    runtime = DockerRuntime("installation")
    runtime.client = Mock()
    runtime.client.containers.create.return_value.id = "cid"
    (tmp_path / "config.yaml").write_text("daily_enable: true", encoding="utf-8")
    (tmp_path / "logs").mkdir()
    (tmp_path / "browser-profile").mkdir()
    run = {"id": "run", "account_id": "account", "image_digest": IMAGE, "task": "main"}
    assert runtime.create(run, tmp_path) == "cid"
    args, kwargs = runtime.client.containers.create.call_args
    assert args == (IMAGE, ["python", "main.py", "main"])
    assert kwargs["environment"]["MARCH7TH_AFTER_FINISH"] == "Exit"
    assert kwargs["restart_policy"] == {"Name": "no"}
    assert "ports" not in kwargs and "privileged" not in kwargs
    assert len({mount["Source"] for mount in kwargs["mounts"]}) == 3
    assert kwargs["labels"][RUN] == "run"


def test_reject_foreign_container_before_stop():
    runtime = DockerRuntime("ours")
    runtime.client = Mock()
    container = runtime.client.containers.get.return_value
    container.labels = {OWNER: "someone-else", ACCOUNT: "a", RUN: "r"}
    with pytest.raises(ValueError):
        runtime.stop("cid", "a", "r")
    container.stop.assert_not_called()


def test_reject_account_or_run_mismatch():
    runtime = DockerRuntime("ours")
    runtime.client = Mock()
    runtime.client.containers.get.return_value.labels = {OWNER: "ours", ACCOUNT: "a", RUN: "r"}
    with pytest.raises(ValueError):
        runtime.remove("cid", "b", "r")
    with pytest.raises(ValueError):
        runtime.remove("cid", "a", "another-run")


def test_missing_config_cannot_be_mounted_as_directory(tmp_path):
    runtime = DockerRuntime("ours")
    runtime.client = Mock()
    with pytest.raises(ValueError):
        runtime.create({"image_digest": IMAGE}, tmp_path)
    runtime.client.containers.create.assert_not_called()
