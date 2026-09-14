from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=1)
def catalog():
    text = files("m7manager").joinpath("resources/instance_catalog.json").read_text("utf-8")
    value = json.loads(text)
    types = value.get("types")
    if not isinstance(types, dict) or not types:
        raise RuntimeError("副本目录无效")
    return value


def dungeon_types():
    return tuple(catalog()["types"])


def instances(instance_type):
    entry = catalog()["types"].get(instance_type)
    return dict(entry["instances"]) if entry else {}


def max_batch(instance_type):
    entry = catalog()["types"].get(instance_type)
    if not entry:
        raise ValueError("不支持的副本类型")
    return entry["max_batch"]


def catalog_metadata():
    value = catalog()
    return {
        "source_commit": value["source_commit"],
        "source_sha256": value["source_sha256"],
        "verified_image_digests": list(value.get("verified_image_digests", [])),
    }
