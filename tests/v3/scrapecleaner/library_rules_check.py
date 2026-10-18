# -*- coding: utf-8 -*-
"""library_rules 测试：多库策略解析与匹配。重点验证「配置写错时不会误生效」。"""
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PKG_DIR = _REPO_ROOT / "plugins.v3" / "scrapecleaner"

import importlib.util  # noqa: E402
import types  # noqa: E402

_package = types.ModuleType("scrapecleaner")
_package.__path__ = [str(_PKG_DIR)]
sys.modules["scrapecleaner"] = _package
_spec = importlib.util.spec_from_file_location(
    "scrapecleaner.library_rules", _PKG_DIR / "library_rules.py"
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["scrapecleaner.library_rules"] = _mod
_spec.loader.exec_module(_mod)
lr = _mod

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"[OK  ] {name}")
    else:
        print(f"[FAIL] {name}  {detail}")
        FAILURES.append(name)


def main() -> int:
    print("=== 1. 基本解析 ===")
    text = """
# 这是注释
/media/movies | mode=recycle | keep_days=60
/media/tv | mode=remove
/media/readonly | disabled=true
"""
    parsed = lr.parse_library_rules(text)
    check("解析出 3 条规则", len(parsed.rules) == 3, str(parsed.rules))
    check("没有警告", parsed.warnings == [], str(parsed.warnings))
    movies = [r for r in parsed.rules if "movies" in r.prefix][0]
    check("mode=recycle", movies.get("mode") == "recycle", str(movies.options))
    check("keep_days 是 int 60", movies.get("keep_days") == 60,
          f"{movies.get('keep_days')!r} {type(movies.get('keep_days'))}")

    print("\n=== 2. 布尔值解析 ===")
    parsed = lr.parse_library_rules("/a | disabled=true\n/b | disabled=false\n/c | disabled=1\n/d | disabled=开")
    got = {r.prefix: r.get("disabled") for r in parsed.rules}
    check("true → True", got.get(os.path.normpath("/a")) is True, str(got))
    check("false → False", got.get(os.path.normpath("/b")) is False, str(got))
    check("1 → True", got.get(os.path.normpath("/c")) is True, str(got))
    check("开 → True", got.get(os.path.normpath("/d")) is True, str(got))

    print("\n=== 3. 非法值不生效且给警告 ===")
    parsed = lr.parse_library_rules(
        "/a | mode=delete\n"
        "/b | keep_days=abc\n"
        "/c | disabled=maybe\n"
        "/d | 不认识的键=1\n"
        "/e | 没有等号\n"
    )
    check("非法 mode 被拒绝",
          all(r.get("mode") is None for r in parsed.rules), str([r.options for r in parsed.rules]))
    check("非法 keep_days 被拒绝",
          all(r.get("keep_days") is None for r in parsed.rules), str([r.options for r in parsed.rules]))
    check("非法 bool 被拒绝",
          all(r.get("disabled") is None for r in parsed.rules), str([r.options for r in parsed.rules]))
    check("产生了警告", len(parsed.warnings) >= 5, f"{len(parsed.warnings)} 条: {parsed.warnings}")
    check("警告里说明了合法值",
          any("recycle 或 remove" in w for w in parsed.warnings), str(parsed.warnings))

    print("\n=== 4. 一行写错不影响其它行 ===")
    parsed = lr.parse_library_rules(
        "/good | mode=recycle\n"
        "/bad | mode=xxx\n"
        "/good2 | keep_days=30\n"
    )
    good = [r for r in parsed.rules if r.prefix.endswith("good")]
    good2 = [r for r in parsed.rules if r.prefix.endswith("good2")]
    check("好行正常解析", bool(good) and good[0].get("mode") == "recycle", str(parsed.rules))
    check("坏行不影响后面的行", bool(good2) and good2[0].get("keep_days") == 30, str(parsed.rules))

    print("\n=== 5. 最长前缀优先 ===")
    parsed = lr.parse_library_rules(
        "/media | mode=recycle\n"
        "/media/readonly | disabled=true\n"
    )
    inside = os.path.join("/media", "readonly", "Movie", "a.nfo")
    other = os.path.join("/media", "movies", "b.nfo")
    rule_inside = lr.resolve_rule(inside, parsed.rules)
    rule_other = lr.resolve_rule(other, parsed.rules)
    check("更具体的规则生效",
          rule_inside is not None and rule_inside.get("disabled") is True, str(rule_inside))
    check("其它路径用短前缀规则",
          rule_other is not None and rule_other.get("mode") == "recycle", str(rule_other))
    check("is_disabled 生效", lr.is_disabled(inside, parsed.rules) is True)
    check("其它路径未被禁用", lr.is_disabled(other, parsed.rules) is False)

    print("\n=== 6. 前缀不能靠字符串匹配（/media/tv vs /media/tv2）===")
    parsed = lr.parse_library_rules("/media/tv | mode=remove")
    tv2_path = os.path.join("/media", "tv2", "x.nfo")
    check("tv2 不算在 tv 下", lr.resolve_rule(tv2_path, parsed.rules) is None,
          str(lr.resolve_rule(tv2_path, parsed.rules)))
    tv_path = os.path.join("/media", "tv", "x.nfo")
    check("tv 自身命中", lr.resolve_rule(tv_path, parsed.rules) is not None)

    print("\n=== 7. 合并全局默认值 ===")
    parsed = lr.parse_library_rules("/media/movies | mode=remove")
    defaults = {"mode": "recycle", "keep_days": 30, "remove_empty_dirs": True}
    merged = lr.effective_options(os.path.join("/media", "movies", "a.nfo"), parsed.rules, defaults)
    check("规则覆盖 mode", merged["mode"] == "remove", str(merged))
    check("未指定的键回落到全局", merged["keep_days"] == 30, str(merged))
    check("全局其它键保留", merged["remove_empty_dirs"] is True, str(merged))
    check("标记了命中的前缀", merged.get("_matched_prefix"), str(merged))

    no_rule = lr.effective_options("/other/a.nfo", parsed.rules, defaults)
    check("没命中任何规则时全是默认值", no_rule == defaults, str(no_rule))

    print("\n=== 8. 追加排除关键字 ===")
    parsed = lr.parse_library_rules("/media/tv | exclude=@eaDir, .actors")
    excludes = lr.extra_excludes(os.path.join("/media", "tv", "a.nfo"), parsed.rules)
    check("解析出 2 个关键字", excludes == ("@eaDir", ".actors"), str(excludes))
    check("不在该库下返回空", lr.extra_excludes("/other/a.nfo", parsed.rules) == ())

    print("\n=== 9. 空输入与纯注释 ===")
    check("空字符串安全", lr.parse_library_rules("").rules == [])
    check("纯注释安全", lr.parse_library_rules("# nothing\n\n# more").rules == [])
    check("None 安全", lr.parse_library_rules(None or "").rules == [])

    print("\n=== 10. 重复路径以最后一次为准 ===")
    parsed = lr.parse_library_rules("/media | mode=recycle\n/media | mode=remove")
    check("只剩 1 条", len(parsed.rules) == 1, str(parsed.rules))
    check("取最后一次的值", parsed.rules[0].get("mode") == "remove", str(parsed.rules))
    check("提示了重复", any("多次" in w for w in parsed.warnings), str(parsed.warnings))

    print("\n=== 11. 只有路径没有策略 ===")
    parsed = lr.parse_library_rules("/media/movies")
    check("仍生成规则（用默认）", len(parsed.rules) == 1, str(parsed.rules))
    check("options 为空", parsed.rules[0].options == {}, str(parsed.rules[0].options))
    check("给出提示", any("只有路径" in w for w in parsed.warnings), str(parsed.warnings))

    print("\n=== 12. describe_rules ===")
    parsed = lr.parse_library_rules("/media/a | mode=remove\n/media/b")
    lines = lr.describe_rules(parsed.rules)
    check("渲染出 2 行", len(lines) == 2, str(lines))
    check("空策略的显示说明", any("全局默认" in line for line in lines), str(lines))

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过: {FAILURES}")
        return 1
    print("✅ 多库策略测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
