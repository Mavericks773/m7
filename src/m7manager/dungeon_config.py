from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from .dungeon_catalog import dungeon_types, instances, max_batch

NESTED_FIELDS = {"instance_names", "instance_names_challenge_count"}
FIXED_MODE_DISABLED_FIELDS = (
    "build_target_enable",
    "power_plan_keep",
    "echo_of_war_enable",
    "activity_gardenofplenty_enable",
    "activity_realmofthestrange_enable",
    "activity_planarfissure_enable",
    "merge_immersifier",
)
DUNGEON_STATE_FIELDS = {
    "instance_type",
    "instance_names",
    "instance_names_challenge_count",
    "power_plan",
    *FIXED_MODE_DISABLED_FIELDS,
}


def merge_config_patch(base, patch):
    result = deepcopy(base)
    for key, value in patch.items():
        if key in NESTED_FIELDS:
            current = result.get(key, {})
            if not isinstance(current, dict):
                current = {}
            current = deepcopy(current)
            current.update(deepcopy(value))
            result[key] = current
        else:
            result[key] = deepcopy(value)
    return result


def fixed_dungeon_patch(instance_type, instance_name, batch_count):
    validate_selection(instance_type, instance_name, batch_count)
    patch = {
        "instance_type": instance_type,
        "instance_names": {instance_type: instance_name},
        "instance_names_challenge_count": {instance_type: batch_count},
        "power_plan": [],
    }
    patch.update({key: False for key in FIXED_MODE_DISABLED_FIELDS})
    return patch


def validate_selection(instance_type, instance_name, batch_count):
    if instance_type not in dungeon_types():
        raise ValueError("不支持的副本类型")
    if not isinstance(instance_name, str) or instance_name not in instances(instance_type):
        raise ValueError("副本名称与副本类型不匹配")
    if instance_name == "无":
        raise ValueError("手选固定副本不能选择“无”")
    if type(batch_count) is not int or not 1 <= batch_count <= max_batch(instance_type):
        raise ValueError(f"该副本每批连续挑战次数须为 1～{max_batch(instance_type)}")


def validate_fixed_mode(config):
    instance_type = config.get("instance_type")
    names = config.get("instance_names", {})
    counts = config.get("instance_names_challenge_count", {})
    if not isinstance(names, dict) or not isinstance(counts, dict):
        raise ValueError("副本名称和挑战次数配置必须是映射")
    validate_selection(instance_type, names.get(instance_type), counts.get(instance_type))
    if config.get("power_plan"):
        raise ValueError("手选固定副本不能同时保留体力计划")
    if any(config.get(key) for key in FIXED_MODE_DISABLED_FIELDS):
        raise ValueError("手选固定副本存在会覆盖目标的上游设置")


def is_fixed_mode(config):
    try:
        validate_fixed_mode(config)
        return True
    except (TypeError, ValueError):
        return False


def dungeon_summary(config):
    instance_type = config.get("instance_type")
    names = config.get("instance_names", {})
    counts = config.get("instance_names_challenge_count", {})
    return {
        "fixed_mode": is_fixed_mode(config),
        "instance_type": instance_type,
        "instance_name": names.get(instance_type) if isinstance(names, dict) else None,
        "batch_count": counts.get(instance_type) if isinstance(counts, dict) else None,
        "conflicts": [
            key
            for key in (*FIXED_MODE_DISABLED_FIELDS, "power_plan")
            if config.get(key)
        ],
    }


def dungeon_fingerprint(config):
    state = {key: config.get(key) for key in sorted(DUNGEON_STATE_FIELDS)}
    encoded = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
