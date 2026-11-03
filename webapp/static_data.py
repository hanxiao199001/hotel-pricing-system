"""静态示例数据读取。

未实现的模块（差评、口碑、报表、客源、协同、集团驾驶舱）的数据放在
data/static.json 里，用 schema 化的 block 描述页面结构，前端统一渲染。
这样以后要换成真实数据时，只要产出同样结构的 block，前端不用改。
"""
import copy
import json
import os

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "static.json")

_cache = None


def load() -> dict:
    global _cache
    if _cache is None:
        with open(_PATH, "r", encoding="utf-8") as f:
            _cache = json.load(f)
    return copy.deepcopy(_cache)


def list_modules() -> list:
    data = load()
    return [
        {
            "key": key,
            "title": mod.get("title"),
            "subtitle": mod.get("subtitle"),
            "status": mod.get("status", "static"),
            "statusNote": mod.get("statusNote"),
        }
        for key, mod in data.get("modules", {}).items()
    ]


def get_module(key: str) -> dict:
    data = load()
    mod = data.get("modules", {}).get(key)
    if mod is None:
        raise KeyError(key)
    out = copy.deepcopy(mod)
    out["key"] = key
    out["meta"] = data.get("meta", {})
    return out
