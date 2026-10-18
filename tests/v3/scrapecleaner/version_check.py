# -*- coding: utf-8 -*-
"""
版本一致性门禁
--------------
官方 `.github/scripts/check_plugin_versions.py` 的核心校验：
`package.v3.json` 里的 `version`、插件类里的 `plugin_version`、
`history` 顶部最新版本，三者必须一致；`history` 需按语义版本降序。

这里做轻量复刻，不依赖宿主环境。
"""
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

REPO = Path(r"D:\AIHarness\N001_本地部署\MoviePilot-Plugins")
PLUGIN_ID = "ScrapeCleaner"
PLUGIN_DIR = REPO / "plugins.v3" / PLUGIN_ID.lower()
INDEX = REPO / "package.v3.json"

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"[OK  ] {name}")
    else:
        print(f"[FAIL] {name}  {detail}")
        FAILURES.append(name)


def semver_key(version: str):
    """把 v1.2.3 / 1.2.3 解析成可比较的元组。"""
    cleaned = version.lstrip("v")
    parts = re.split(r"[.\-+]", cleaned)
    numbers = []
    for part in parts:
        try:
            numbers.append(int(part))
        except ValueError:
            break
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers)


def main() -> int:
    print("=== 文件布局 ===")
    check("插件目录存在", PLUGIN_DIR.is_dir(), str(PLUGIN_DIR))
    check("目录名 = 类名小写", PLUGIN_DIR.name == PLUGIN_ID.lower(), PLUGIN_DIR.name)
    check("__init__.py 存在", (PLUGIN_DIR / "__init__.py").is_file())
    check("package.v3.json 存在", INDEX.is_file())
    check("图标存在", (REPO / "icons" / "clean.png").is_file())
    check("测试不在插件源码目录里",
          not any(PLUGIN_DIR.glob("test_*.py")) and not (PLUGIN_DIR / "tests").exists(),
          "测试应放在 tests/v3/ 下")

    print("\n=== 索引元数据 ===")
    index = json.loads(INDEX.read_text(encoding="utf-8"))
    check("索引使用插件 ID 作为键", PLUGIN_ID in index, str(list(index)))
    entry = index.get(PLUGIN_ID, {})
    for field in ("name", "description", "version", "icon", "author", "level", "history"):
        check(f"索引含 {field}", field in entry, str(list(entry)))
    check("声明 system_version >=3.0.0",
          str(entry.get("system_version", "")).startswith(">="), entry.get("system_version"))

    print("\n=== 版本三处一致 ===")
    source = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'plugin_version\s*=\s*"([^"]+)"', source)
    check("代码里有 plugin_version", match is not None, "")
    code_version = match.group(1) if match else ""
    index_version = str(entry.get("version", ""))
    history = entry.get("history") or {}
    history_versions = list(history)

    print(f"       代码 plugin_version = {code_version}")
    print(f"       索引 version        = {index_version}")
    print(f"       history 顶部         = {history_versions[0] if history_versions else '(空)'}")

    check("代码版本 == 索引版本", code_version == index_version,
          f"{code_version} != {index_version}")
    check("history 非空", bool(history_versions), "")
    if history_versions:
        top = history_versions[0].lstrip("v")
        check("history 顶部 == 当前版本", top == code_version,
              f"{history_versions[0]} != {code_version}")
        keys = [semver_key(v) for v in history_versions]
        check("history 按语义版本降序", keys == sorted(keys, reverse=True), str(history_versions))

    print("\n=== 类元信息与索引一致 ===")
    for field, index_key in (("plugin_name", "name"), ("plugin_icon", "icon"),
                             ("plugin_author", "author")):
        m = re.search(rf'{field}\s*=\s*"([^"]+)"', source)
        actual = m.group(1) if m else None
        check(f"{field} == 索引 {index_key}", actual == entry.get(index_key),
              f"代码={actual!r} 索引={entry.get(index_key)!r}")

    print("\n=== 序号/前缀 ===")
    m = re.search(r'plugin_config_prefix\s*=\s*"([^"]+)"', source)
    check("config_prefix 位于类中且以下划线结尾",
          m is not None and m.group(1).endswith("_"), m.group(1) if m else "缺失")

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过: {FAILURES}")
        return 1
    print("✅ 版本一致性检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
