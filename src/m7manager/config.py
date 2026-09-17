from __future__ import annotations

import io
import json
import os
import re
import shutil
from importlib.resources import files
from pathlib import Path

from ruamel.yaml import YAML

from .dungeon_catalog import dungeon_types, instances, max_batch
from .dungeon_config import FIXED_MODE_DISABLED_FIELDS, merge_config_patch, validate_plan

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
BOOL_EDITABLE = {
    "daily_enable",
    "power_enable",
    "cloud_game_use_paid_time",
    "build_target_enable",
    "power_plan_keep",
    "echo_of_war_enable",
    "activity_gardenofplenty_enable",
    "activity_realmofthestrange_enable",
    "activity_planarfissure_enable",
    "merge_immersifier",
}
INT_EDITABLE = {
    "cloud_game_max_queue_time",
    "cloud_game_login_timeout",
    "log_retention_days",
}
EDITABLE = (
    BOOL_EDITABLE
    | INT_EDITABLE
    | {
        "instance_type",
        "instance_names",
        "instance_names_challenge_count",
        "power_plan",
        "echo_of_war_start_day_of_week",
    }
)
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
    if not isinstance(patch, dict):
        raise ValueError("设置补丁必须是映射")
    for key, value in patch.items():
        if key not in EDITABLE:
            raise ValueError(f"不支持修改设置：{key}")
        if key in BOOL_EDITABLE:
            if type(value) is not bool:
                raise ValueError(f"{key} 必须是布尔值")
            if (
                key in FIXED_MODE_DISABLED_FIELDS
                and key not in {"build_target_enable", "power_plan_keep", "echo_of_war_enable"}
                and value
            ):
                raise ValueError(f"当前版本只允许在应用固定副本时关闭 {key}")
        elif key in INT_EDITABLE and (type(value) is not int or not 1 <= value <= 120):
            raise ValueError(f"{key} 必须为 1～120 的整数")
        elif key == "instance_type" and value not in dungeon_types():
            raise ValueError("不支持的副本类型")
        elif key == "instance_names":
            if not isinstance(value, dict) or not value:
                raise ValueError("副本名称设置必须是非空映射")
            for instance_type, instance_name in value.items():
                if (
                    instance_type not in (*dungeon_types(), "历战余响")
                    or not isinstance(instance_name, str)
                    or instance_name not in instances(instance_type)
                ):
                    raise ValueError("副本名称与副本类型不匹配")
                if instance_name == "无":
                    raise ValueError("手选固定副本不能选择“无”")
        elif key == "instance_names_challenge_count":
            if not isinstance(value, dict) or not value:
                raise ValueError("副本挑战次数设置必须是非空映射")
            for instance_type, count in value.items():
                if instance_type not in dungeon_types():
                    raise ValueError("不支持的副本类型")
                if type(count) is not int or not 1 <= count <= max_batch(instance_type):
                    raise ValueError(
                        f"{instance_type} 每批连续挑战次数须为 1～{max_batch(instance_type)}"
                    )
        elif key == "power_plan":
            validate_plan(value)
        elif key == "echo_of_war_start_day_of_week":
            if type(value) is not int or not 1 <= value <= 7:
                raise ValueError("周本开始星期须为 1～7 的整数")


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
        config = merge_config_patch(config, patch)
        atomic_text(path / "config.yaml", dump_config(config))
    return config


def redacted_config(config):
    # Plain mapping intentionally discards YAML comments that might contain secrets.
    return {
        key: ("[REDACTED]" if SENSITIVE.search(str(key)) else value)
        for key, value in config.items()
        if isinstance(value, (str, int, float, bool, type(None)))
    }
