from __future__ import annotations

import io
import json
import os
import re
import shutil
from importlib.resources import files
from pathlib import Path

from ruamel.yaml import YAML

BASELINE = "5e70b0261a99f6666e99a64a0ffae7488db0fa2b"
DEFAULTS = {
    "daily_enable": True,
    "power_enable": True,
    "fight_enable": False,
    "universe_enable": False,
    "currencywars_enable": False,
    "weekly_divergent_enable": False,
    "forgottenhall_enable": False,
    "purefiction_enable": False,
    "apocalyptic_enable": False,
    "cloud_game_use_paid_time": False,
    "cloud_game_max_queue_time": 15,
    "cloud_game_login_timeout": 10,
    "log_retention_days": 7,
    "after_finish": "Exit",
    "cloud_game_enable": True,
    "browser_headless_enable": True,
    "browser_headless_restart_on_not_logged_in": False,
    "scheduled_run_enable": False,
    "scheduled_tasks": [],
}
EDITABLE = {
    "daily_enable",
    "power_enable",
    "cloud_game_use_paid_time",
    "cloud_game_max_queue_time",
    "cloud_game_login_timeout",
    "log_retention_days",
}
SENSITIVE = re.compile(r"password|secret|token|cookie|webhook|notify_|account|telemetry_id", re.I)


def atomic_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def account_dir(root: Path, account_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", account_id):
        raise ValueError("账号 ID 无效")
    path = (root / "accounts" / account_id).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("账号路径不在数据目录内")
    return path


def validate_patch(patch):
    for key, value in patch.items():
        if key not in EDITABLE:
            raise ValueError(f"不支持修改设置：{key}")
        if key.endswith("enable") or key == "cloud_game_use_paid_time":
            if type(value) is not bool:
                raise ValueError(f"{key} 必须是布尔值")
        elif type(value) is not int or not 1 <= value <= 120:
            raise ValueError(f"{key} 必须为 1～120 的整数")


def load_config(path: Path):
    value = YAML().load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("配置文件必须是 YAML 映射")
    return value


def dump_config(value):
    stream = io.StringIO()
    YAML().dump(value, stream)
    return stream.getvalue()


def initialize(root: Path, account_id: str):
    path = account_dir(root, account_id)
    path.mkdir(parents=True, exist_ok=False)
    for name in ("browser-profile", "logs", "backups"):
        (path / name).mkdir()
    template = files("m7manager").joinpath("resources/config.example.yaml").read_text("utf-8")
    config = YAML().load(template)
    config.update(DEFAULTS)
    atomic_text(path / "config.yaml", dump_config(config))
    atomic_text(path / "template-version.txt", BASELINE + "\n")
    return path


def apply_pending(root, account, run_id):
    """Caller must first remove the previous container (file bind mount inode)."""
    path = account_dir(root, account["id"])
    patch = json.loads(account["pending_config"])
    validate_patch(patch)
    config = load_config(path / "config.yaml")
    if patch:
        shutil.copy2(path / "config.yaml", path / "backups" / f"{run_id}.yaml")
        config.update(patch)
        atomic_text(path / "config.yaml", dump_config(config))
    return config


def redacted_config(config):
    # Plain mapping intentionally discards YAML comments that might contain secrets.
    return {
        key: ("[REDACTED]" if SENSITIVE.search(str(key)) else value)
        for key, value in config.items()
        if isinstance(value, (str, int, float, bool, type(None)))
    }
