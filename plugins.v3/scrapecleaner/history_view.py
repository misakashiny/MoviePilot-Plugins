# -*- coding: utf-8 -*-
"""
整理历史视图（纯逻辑，不依赖 MoviePilot 运行时）
================================================

为什么加这个模块
----------------
最初的实现只做「扫描对账」：遍历整个媒体库，推断哪些刮削文件已经没有主。
这种方式能找出客观的残留，但有个使用上的问题——**你得先跑一遍全库扫描，
再逐条核对判定依据**，对"我就想看一眼整理过的东西、挑几条删掉"这种需求来说
太重了。

MoviePilot 主程序的「整理历史」本身就是一个列表：每条记录有标题、源路径、
目标路径、时间。这些记录天然回答了「这个文件是从哪来的、整理到哪去了」。
本模块把它转换成可直接操作的数据：

- 每条记录都去检查 **目标路径是否还存在**；
- 目标不存在 → 这条记录已经失效，对应的媒体库文件已经被删了；
- 目标存在 → 正常，不该动。

这样做的好处是不需要扫描全库：数据库查询 + 每条记录一次 `os.path.exists`，
几十毫秒就能出结果，而且结果就是用户熟悉的「整理历史列表」。

设计取向
--------
- 状态判定必须**保守**：只有能确认"目标确实不存在"才判为失效。
  路径为空、存储不可达、判断过程出错，一律算「未知」，不给删除按钮。
- 所有 ORM 对象在这里立刻转成纯数据结构，避免会话释放后懒加载报错。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable, Optional


class HistoryState(str, Enum):
    """整理历史记录的目标文件当前状态。"""

    # 目标文件还在：正常，不该动
    ALIVE = "alive"
    # 目标文件已不存在：整理过，但后来被删了
    MISSING = "missing"
    # 无法判断（路径为空 / 存储不可达 / 判断出错）
    UNKNOWN = "unknown"


@dataclass
class HistoryItem:
    """一条整理历史（已转成纯数据，可直接 JSON 序列化）。"""

    history_id: int
    title: str
    year: str
    mtype: str
    season: str
    episode: str
    image: str
    # 下载目录里的源路径
    src: str
    # 媒体库里的目标路径
    dest: str
    date: str
    # 目标文件的当前状态
    state: HistoryState = HistoryState.UNKNOWN
    # 状态说明（给人看的）
    detail: str = ""
    # 是否为转移任务记录（宿主不允许插件删除这类记录）
    protected: bool = False

    @property
    def deletable(self) -> bool:
        """是否允许在本插件里删除对应的媒体库文件。"""
        return self.state is HistoryState.MISSING and not self.protected

    def to_row(self) -> dict[str, Any]:
        """转成页面表格的一行。"""
        return {
            "id": self.history_id,
            "title": self.title or "-",
            "season_episode": _season_episode_text(self.season, self.episode),
            "dest": self.dest or "-",
            "src": self.src or "-",
            "date": self.date or "-",
            "state": self.state.value,
            "state_label": _STATE_LABELS[self.state],
            "detail": self.detail,
            "deletable": self.deletable,
        }


_STATE_LABELS: dict[HistoryState, str] = {
    HistoryState.ALIVE: "✅ 正常",
    HistoryState.MISSING: "🗑️ 文件已丢失",
    HistoryState.UNKNOWN: "❔ 无法判断",
}


def _season_episode_text(season: str, episode: str) -> str:
    """把季集拼成 S01E01 形式，缺失的部分省略。"""
    if not season and not episode:
        return ""
    parts = []
    if season:
        try:
            parts.append(f"S{int(season):02d}")
        except (TypeError, ValueError):
            parts.append(f"S{season}")
    if episode:
        try:
            parts.append(f"E{int(episode):02d}")
        except (TypeError, ValueError):
            parts.append(f"E{episode}")
    return "".join(parts)


def record_to_item(raw: Any) -> Optional[HistoryItem]:
    """
    把宿主 ORM 记录转成纯数据结构。

    只取需要的字段，并且**立刻**读取所有属性——ORM 对象离开会话后
    再读属性会触发懒加载报错。
    """
    try:
        history_id = getattr(raw, "id", None)
        if history_id is None:
            return None
        return HistoryItem(
            history_id=int(history_id),
            title=str(getattr(raw, "title", "") or ""),
            year=str(getattr(raw, "year", "") or ""),
            mtype=str(getattr(raw, "type", "") or ""),
            season=str(getattr(raw, "seasons", "") or ""),
            episode=str(getattr(raw, "episodes", "") or ""),
            image=str(getattr(raw, "image", "") or ""),
            src=str(getattr(raw, "src", "") or ""),
            dest=str(getattr(raw, "dest", "") or ""),
            date=str(getattr(raw, "date", "") or ""),
            protected=bool(getattr(raw, "transfer_task_id", None)),
        )
    except (TypeError, ValueError):
        return None


def classify_item(
    item: HistoryItem,
    exists: Optional[Callable[[str], bool]] = None,
) -> HistoryItem:
    """
    判断这条记录的目标文件当前是否还存在。

    :param item: 待判断记录
    :param exists: 存在性判断函数，默认 os.path.exists（做成可注入便于单测）
    :return: 填入 state / detail 后的记录
    """
    check = exists or os.path.exists

    if not item.dest:
        item.state = HistoryState.UNKNOWN
        item.detail = "这条记录没有目标路径"
        return item

    if item.protected:
        item.state = HistoryState.UNKNOWN
        item.detail = "属于转移任务的记录，不参与清理"
        return item

    try:
        still_exists = check(item.dest)
    except OSError as error:
        item.state = HistoryState.UNKNOWN
        item.detail = f"无法访问路径：{error}"
        return item

    if still_exists:
        item.state = HistoryState.ALIVE
        item.detail = "目标文件存在"
    else:
        item.state = HistoryState.MISSING
        item.detail = "目标文件已不存在"
    return item


def build_items(
    raws: Iterable[Any],
    exists: Optional[Callable[[str], bool]] = None,
) -> list[HistoryItem]:
    """
    把一批 ORM 记录转成带状态判断的列表。

    输出按时间倒序（新的在上），与主程序整理历史的排序一致。
    """
    items: list[HistoryItem] = []
    for raw in raws:
        item = record_to_item(raw)
        if item is None:
            continue
        items.append(classify_item(item, exists))
    # 主程序按 id 降序返回，这里再排一次保证顺序稳定
    items.sort(key=lambda entry: (entry.date, entry.history_id), reverse=True)
    return items


@dataclass
class HistorySummary:
    """列表的汇总统计，显示在页头。"""

    total: int = 0
    alive: int = 0
    missing: int = 0
    unknown: int = 0

    @classmethod
    def from_items(cls, items: Iterable[HistoryItem]) -> "HistorySummary":
        summary = cls()
        for item in items:
            summary.total += 1
            if item.state is HistoryState.ALIVE:
                summary.alive += 1
            elif item.state is HistoryState.MISSING:
                summary.missing += 1
            else:
                summary.unknown += 1
        return summary


def filter_items(
    items: Iterable[HistoryItem],
    *,
    keyword: str = "",
    state: str = "",
    limit: int = 0,
) -> list[HistoryItem]:
    """
    按关键字/状态筛选列表。

    :param keyword: 匹配标题、源路径或目标路径（不区分大小写）
    :param state: `alive` / `missing` / `unknown`，空表示全部
    :param limit: 最多返回多少条，0 表示不限制
    """
    lowered = keyword.strip().lower()
    result: list[HistoryItem] = []
    for item in items:
        if state and item.state.value != state:
            continue
        if lowered:
            haystack = f"{item.title}\n{item.src}\n{item.dest}".lower()
            if lowered not in haystack:
                continue
        result.append(item)
        if limit and len(result) >= limit:
            break
    return result


def collect_sibling_scrap_files(
    dest: str,
    scrap_exts: Iterable[str],
    exists: Optional[Callable[[str], bool]] = None,
) -> list[str]:
    """
    找出与某条整理记录同名的刮削文件（.nfo / 图片 / 字幕）。

    场景：整理历史里记录的是一条视频（`Show.S01E01.mkv`），
    但它旁边还留着 `Show.S01E01.nfo`、`Show.S01E01.chs.srt`。
    用户勾选删除时，这些同类文件通常也应该一起走。

    **只被动返回候选，不判断该不该删**——是否真的删除由上层结合
    扫描报告与安全阀决定。

    :param dest: 整理记录的目标路径
    :param scrap_exts: 刮削文件后缀（已带点）
    :param exists: 存在性判断函数
    """
    if not dest:
        return []
    directory = os.path.dirname(dest)
    if not directory or not os.path.isdir(directory):
        return []

    stem = os.path.splitext(os.path.basename(dest))[0].lower()
    candidates: list[str] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                name = entry.name
                ext = os.path.splitext(name)[1].lower()
                if ext not in set(scrap_exts):
                    continue
                file_stem = os.path.splitext(name)[0].lower()
                # 同名或同名带后缀（.chs / -thumb）都算
                if file_stem == stem or file_stem.startswith(stem + ".") \
                        or file_stem.startswith(stem + "-") \
                        or file_stem.startswith(stem + "_"):
                    candidates.append(entry.path)
    except OSError:
        return []
    return sorted(candidates)
