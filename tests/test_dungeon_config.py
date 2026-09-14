import pytest

from m7manager.config import validate_patch
from m7manager.dungeon_catalog import catalog_metadata, dungeon_types, instances, max_batch
from m7manager.dungeon_config import (
    fixed_dungeon_patch,
    is_fixed_mode,
    merge_config_patch,
    validate_fixed_mode,
    validate_selection,
)


def test_catalog_has_supported_types_and_location_keys():
    assert dungeon_types() == (
        "拟造花萼（金）",
        "拟造花萼（赤）",
        "凝滞虚影",
        "侵蚀隧洞",
        "饰品提取",
    )
    assert instances("拟造花萼（赤）")["收容舱段"] == "毁灭之蕾"
    assert "毁灭之蕾" not in instances("拟造花萼（赤）")
    assert max_batch("侵蚀隧洞") == 6
    assert len(catalog_metadata()["source_commit"]) == 40


@pytest.mark.parametrize(
    "instance_type,instance_name,count",
    [
        ("侵蚀隧洞", "回忆之蕾", 1),
        ("未知类型", "未知副本", 1),
        ("侵蚀隧洞", "睿治之径", True),
        ("侵蚀隧洞", "睿治之径", 0),
        ("侵蚀隧洞", "睿治之径", 7),
    ],
)
def test_invalid_selection_is_rejected(instance_type, instance_name, count):
    with pytest.raises(ValueError):
        validate_selection(instance_type, instance_name, count)


def test_fixed_patch_disables_all_target_overrides():
    patch = fixed_dungeon_patch("侵蚀隧洞", "睿治之径", 6)
    assert patch["instance_names"] == {"侵蚀隧洞": "睿治之径"}
    assert patch["instance_names_challenge_count"] == {"侵蚀隧洞": 6}
    assert patch["power_plan"] == []
    assert not any(
        patch[key]
        for key in (
            "build_target_enable",
            "power_plan_keep",
            "echo_of_war_enable",
            "activity_gardenofplenty_enable",
            "activity_realmofthestrange_enable",
            "activity_planarfissure_enable",
            "merge_immersifier",
        )
    )
    assert is_fixed_mode(patch)


def test_nested_patch_preserves_other_types_and_progress_fields():
    base = {
        "instance_names": {"拟造花萼（金）": "回忆之蕾", "侵蚀隧洞": "霜风之径"},
        "instance_names_challenge_count": {"拟造花萼（金）": 24, "侵蚀隧洞": 3},
        "last_run_timestamp": 123,
    }
    merged = merge_config_patch(base, fixed_dungeon_patch("侵蚀隧洞", "睿治之径", 6))
    assert merged["instance_names"]["拟造花萼（金）"] == "回忆之蕾"
    assert merged["instance_names"]["侵蚀隧洞"] == "睿治之径"
    assert merged["instance_names_challenge_count"]["拟造花萼（金）"] == 24
    assert merged["last_run_timestamp"] == 123
    validate_fixed_mode(merged)


def test_patch_validation_rejects_description_nonempty_plan_and_unsupported_enable():
    with pytest.raises(ValueError):
        validate_patch({"instance_names": {"拟造花萼（赤）": "毁灭之蕾"}})
    with pytest.raises(ValueError):
        validate_patch({"power_plan": [["侵蚀隧洞", "睿治之径", 1]]})
    validate_patch({"build_target_enable": True})
    with pytest.raises(ValueError):
        validate_patch({"echo_of_war_enable": True})
