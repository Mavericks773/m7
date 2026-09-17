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
    "echo_of_war_start_day_of_week",
    "instance_type",
    "instance_names",
    "instance_names_challenge_count",
    "power_plan",
    *FIXED_MODE_DISABLED_FIELDS,
}
PLAN_LIMIT = 20
PLAN_COUNT_LIMIT = 999


def validate_plan(plan):
    if not isinstance(plan, list) or len(plan) > PLAN_LIMIT:
        raise ValueError(f"体力计划必须是列表，最多 {PLAN_LIMIT} 项")
    targets = {}
    for item in plan:
        if not isinstance(item, list) or len(item) != 3:
            raise ValueError("每项计划必须包含副本类型、名称和次数")
        kind, name, count = item
        validate_selection(kind, name, 1)
        if type(count) is not int or not 1 <= count <= PLAN_COUNT_LIMIT:
            raise ValueError(f"计划挑战次数须为 1～{PLAN_COUNT_LIMIT} 的整数")
        if kind in targets and targets[kind] != name:
            raise ValueError("当前上游重试按类型定位；同一类型的计划必须选择同一个副本")
        targets[kind] = name
    return targets


def power_plan_patch(plan, keep, instance_type, instance_name, batch_count):
    targets = validate_plan(plan)
    if type(keep) is not bool:
        raise ValueError("计划保留模式必须是布尔值")
    if instance_type in targets and targets[instance_type] != instance_name:
        raise ValueError("兜底副本与同类型计划必须使用相同名称，避免恢复时切换目标")
    patch = fixed_dungeon_patch(instance_type, instance_name, batch_count)
    # Weekly choices are independent and are preserved when changing the plan.
    patch.pop("echo_of_war_enable")
    patch["power_plan"] = deepcopy(plan)
    patch["power_plan_keep"] = keep
    patch["instance_names"].update(targets)
    for kind in targets:
        patch["instance_names_challenge_count"].setdefault(kind, max_batch(kind))
    return patch


def weekly_patch(enabled, day, name):
    patch = {"echo_of_war_enable": enabled}
    if enabled:
        if type(day) is not int or not 1 <= day <= 7:
            raise ValueError("周本开始星期须为 1～7")
        if not isinstance(name, str) or name not in instances("历战余响") or name == "无":
            raise ValueError("请选择目录中的历战余响副本")
        patch.update(
            {
                "echo_of_war_start_day_of_week": day,
                "instance_names": {"历战余响": name},
                "build_target_enable": False,
            }
        )
    return patch


def validate_manual_power(config):
    kind = config.get("instance_type")
    names = config.get("instance_names", {})
    counts = config.get("instance_names_challenge_count", {})
    validate_selection(kind, names.get(kind), counts.get(kind))
    targets = validate_plan(config.get("power_plan", []))
    for target_type, name in targets.items():
        if names.get(target_type) != name:
            raise ValueError("计划副本与恢复定位配置不一致，请重新应用体力计划")
        validate_selection(target_type, name, counts.get(target_type))
    overrides = set(FIXED_MODE_DISABLED_FIELDS) - {"power_plan_keep", "echo_of_war_enable"}
    if any(config.get(key) for key in overrides):
        raise ValueError("手选副本存在覆盖目标的设置：请关闭培养目标、双倍活动和优先合成沉浸器")


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
        "power_plan": deepcopy(config.get("power_plan", [])),
        "power_plan_keep": config.get("power_plan_keep", False),
        "weekly_enabled": config.get("echo_of_war_enable", False),
        "weekly_day": config.get("echo_of_war_start_day_of_week", 1),
        "weekly_name": names.get("历战余响") if isinstance(names, dict) else None,
        "fixed_mode": is_fixed_mode(config),
        "instance_type": instance_type,
        "instance_name": names.get(instance_type) if isinstance(names, dict) else None,
        "batch_count": counts.get(instance_type) if isinstance(counts, dict) else None,
        "conflicts": [
            key for key in (*FIXED_MODE_DISABLED_FIELDS, "power_plan") if config.get(key)
        ],
    }


def dungeon_fingerprint(config):
    state = {key: config.get(key) for key in sorted(DUNGEON_STATE_FIELDS)}
    encoded = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
