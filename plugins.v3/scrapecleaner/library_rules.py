# -*- coding: utf-8 -*-
"""
多媒体库分别配置策略（纯逻辑，不依赖 MoviePilot 运行时）
========================================================

为什么需要
----------
不同媒体库的"该怎么清理"往往不一样：

- 电影库不希望删掉任何图片（海报墙需要）；
- 剧集库可以放开了清；
- 某个库整个是只读的、或者存着别人的收藏，干脆完全排除；
- NAS 上的库用回收站，本地盘上的库直接删。

只配一个全局策略做不到这些，只能全部按最保守的来，等于没用。

配置格式
--------
为了让用户不用学 YAML，采用**一行一条**的简单格式，用 `|` 分段：

    <路径前缀> | <键=值> [| <键=值> ...]

例如：

    /media/movies | mode=recycle | keep_days=60 | skip_dir_shared=true
    /media/tv | mode=remove
    /media/readonly | disabled=true

支持的键见 `SUPPORTED_KEYS`。匹配规则是**最长前缀优先**，
没有命中任何规则的路径使用全局策略。

设计取向
--------
配置写错是常态，所以解析必须"宁可少生效，不可错生效"：
- 无法识别的键 → 忽略并记入 warnings
- 值非法 → 回落到该键的默认值，并记入 warnings
- 整行无法解析 → 跳过该行，记入 warnings
绝不让一条写错的规则影响其它规则。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


# 支持的策略键及其类型。类型用于校验，避免 "true" 被当成字符串。
SUPPORTED_KEYS: dict[str, str] = {
    "mode": "str",             # recycle / remove
    "recycle_dir": "str",      # 该库单独的回收目录
    "keep_days": "int",        # 回收站保留天数
    "disabled": "bool",        # 整个库完全不处理
    "min_age_minutes": "int",  # 新文件保护期（分钟）
    "skip_dir_shared": "bool", # 是否跳过目录级共享文件（海报墙保护）
    "exclude": "str",          # 该库额外的排除关键字，逗号分隔
    "max_files": "int",        # 该库的单次扫描上限
}


@dataclass
class LibraryRule:
    """一条媒体库策略。"""

    prefix: str
    options: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        """取某个策略值。"""
        return self.options.get(key, default)

    def __repr__(self) -> str:
        return f"LibraryRule({self.prefix!r}, {self.options!r})"


@dataclass
class ParsedRules:
    """解析结果。"""

    rules: list[LibraryRule] = field(default_factory=list)
    # 无法解析的行/键，给用户看
    warnings: list[str] = field(default_factory=list)


def _norm(path: str) -> str:
    """规范化路径用于前缀比较：统一分隔符、去掉尾部分隔符。"""
    cleaned = path.strip().strip('"').strip("'")
    if not cleaned:
        return ""
    normalized = os.path.normpath(cleaned)
    return normalized


def _parse_bool(value: str) -> Optional[bool]:
    """解析布尔值。"""
    lowered = value.strip().lower()
    if lowered in ("true", "1", "yes", "on", "是", "开"):
        return True
    if lowered in ("false", "0", "no", "off", "否", "关"):
        return False
    return None


def _coerce(key: str, raw_value: str) -> tuple[Any, Optional[str]]:
    """
    把字符串值转换成键对应的类型。

    :return: (值, 错误信息或 None)
    """
    kind = SUPPORTED_KEYS[key]
    value = raw_value.strip()
    if not value:
        return None, f"{key} 的值为空，已忽略"

    if kind == "bool":
        parsed = _parse_bool(value)
        if parsed is None:
            return None, f"{key}={value!r} 不是合法布尔值（用 true/false）"
        return parsed, None

    if kind == "int":
        try:
            return int(value), None
        except ValueError:
            return None, f"{key}={value!r} 不是整数"

    if key == "mode":
        if value not in ("recycle", "remove"):
            return None, f"mode={value!r} 只能是 recycle 或 remove"
        return value, None

    return value, None


def parse_library_rules(text: str) -> ParsedRules:
    """
    解析多库策略配置。

    :param text: 每行一条 `<路径> | key=value | key=value`
    :return: 解析结果（含 warnings）
    """
    result = ParsedRules()
    if not text or not text.strip():
        return result

    for line_number, raw_line in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        line = raw_line.strip()
        # 允许用 # 开头写注释，方便用户自己标注
        if not line or line.startswith("#"):
            continue

        segments = [segment.strip() for segment in line.split("|")]
        prefix = _norm(segments[0]) if segments else ""
        if not prefix:
            result.warnings.append(f"第 {line_number} 行缺少路径，已跳过：{line}")
            continue
        if not segments[1:]:
            result.warnings.append(
                f"第 {line_number} 行只有路径没有策略，已按默认处理：{prefix}"
            )

        options: dict[str, Any] = {}
        duplicate = False
        for segment in segments[1:]:
            if not segment:
                continue
            if "=" not in segment:
                result.warnings.append(
                    f"第 {line_number} 行片段 {segment!r} 不是 key=value 格式，已忽略"
                )
                continue
            key, _, raw_value = segment.partition("=")
            key = key.strip().lower()
            if key not in SUPPORTED_KEYS:
                result.warnings.append(
                    f"第 {line_number} 行不认识的策略键 {key!r}，已忽略"
                    f"（支持：{', '.join(SUPPORTED_KEYS)}）"
                )
                continue
            if key in options:
                duplicate = True
                result.warnings.append(
                    f"第 {line_number} 行重复设置了 {key}，以最后一次为准"
                )
            value, error = _coerce(key, raw_value)
            if error:
                result.warnings.append(f"第 {line_number} 行：{error}")
                continue
            options[key] = value

        if duplicate and not options:
            continue
        result.rules.append(LibraryRule(prefix=prefix, options=options))

    # 路径重复时以后出现的为准（用户通常写在下面的是更具体的覆盖）
    deduped: dict[str, LibraryRule] = {}
    for rule in result.rules:
        if rule.prefix in deduped:
            result.warnings.append(f"路径 {rule.prefix} 配置了多次，以最后一次为准")
        deduped[rule.prefix] = rule
    result.rules = list(deduped.values())
    return result


def _is_under(path: str, prefix: str) -> bool:
    """
    判断 path 是否位于 prefix 之下。

    用 commonpath 而不是字符串 startswith：后者会把
    `/media/tv2` 误判为 `/media/tv` 的子路径。
    """
    try:
        target = os.path.abspath(os.path.normpath(path))
        base = os.path.abspath(os.path.normpath(prefix))
        return os.path.commonpath([target, base]) == base
    except (ValueError, OSError):
        # 不同驱动器时 commonpath 抛 ValueError
        return False


def resolve_rule(path: str, rules: Iterable[LibraryRule]) -> Optional[LibraryRule]:
    """
    找出适用于 path 的策略，**最长前缀优先**。

    最长前缀优先是必要的：用户可能同时配了 `/media` 和 `/media/readonly`，
    对后者中的文件应该用更具体的规则。
    """
    best: Optional[LibraryRule] = None
    best_length = -1
    for rule in rules:
        if not _is_under(path, rule.prefix):
            continue
        if len(rule.prefix) > best_length:
            best = rule
            best_length = len(rule.prefix)
    return best


def effective_options(
    path: str,
    rules: Iterable[LibraryRule],
    global_defaults: dict[str, Any],
) -> dict[str, Any]:
    """
    合并出某个路径实际生效的策略。

    规则里没写的键回落到全局默认值，而不是回落成 None——
    否则用户只写了 `mode=remove` 时，其它键会变成空值导致行为异常。

    :param path: 待判断的文件/目录路径
    :param rules: 已解析的库规则
    :param global_defaults: 全局策略
    :return: 合并后的策略字典
    """
    merged = dict(global_defaults)
    rule = resolve_rule(path, rules)
    if rule:
        merged.update(rule.options)
        merged["_matched_prefix"] = rule.prefix
    return merged


def is_disabled(path: str, rules: Iterable[LibraryRule]) -> bool:
    """该路径是否落在被完全排除的库里。"""
    rule = resolve_rule(path, rules)
    return bool(rule and rule.get("disabled") is True)


def extra_excludes(path: str, rules: Iterable[LibraryRule]) -> tuple[str, ...]:
    """取该路径所属库追加的排除关键字。"""
    rule = resolve_rule(path, rules)
    if not rule:
        return ()
    raw = rule.get("exclude") or ""
    return tuple(
        item.strip() for item in str(raw).replace("，", ",").split(",") if item.strip()
    )


def describe_rules(rules: Iterable[LibraryRule]) -> list[str]:
    """把规则渲染成便于展示/排错的一行行文本（插件页面用）。"""
    lines: list[str] = []
    for rule in rules:
        if rule.options:
            detail = ", ".join(f"{key}={value}" for key, value in rule.options.items())
            lines.append(f"{rule.prefix}  →  {detail}")
        else:
            lines.append(f"{rule.prefix}  →  （使用全局默认）")
    return lines
