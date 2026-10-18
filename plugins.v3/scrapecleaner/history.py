# -*- coding: utf-8 -*-
"""
转移记录清理（纯逻辑，不依赖 MoviePilot 运行时）
================================================

为什么单独做一层
----------------
宿主的 `TransferHistoryOper` 只有 `get_by_dest(dest)` / `get_by_src(src)`
这种**单条**查询，而 `delete(historyid)` 内部带了
`TransferHistory.transfer_task_id.is_(None)` 条件——也就是说，
**属于转移任务的记录宿主自己会保护，删不掉**。

这带来两个必须处理的问题：

1. 只凭单个路径查不出「一组」记录。真正要判断的是
   「这条转移记录指向的目标文件是否还存在」，
   所以要先拿到记录，再看文件系统。
2. `delete()` 静默无效的情况必须能被区分出来并报告，
   否则用户会以为清理了、其实没清。

本模块把「该删哪些记录」的判断做成纯函数，宿主调用留成接口，
这样判断逻辑可以脱离数据库单元测试。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional


@dataclass
class HistoryRecord:
    """一条转移记录中与本插件相关的字段。"""

    history_id: int
    src: str
    dest: str
    # 属于转移任务时为非空，宿主不允许插件删除这类记录
    transfer_task_id: Optional[str] = None

    @property
    def protected(self) -> bool:
        """该记录是否被转移任务保护（插件删不掉也不该删）。"""
        return bool(self.transfer_task_id)


@dataclass
class HistoryCleanResult:
    """转移记录清理结果。"""

    deleted: list[int] = field(default_factory=list)
    protected: list[int] = field(default_factory=list)
    failed: list[tuple[int, str]] = field(default_factory=list)
    # 记录还在、但目标文件仍存在的，跳过不删
    kept_alive: list[int] = field(default_factory=list)

    @property
    def total_done(self) -> int:
        """实际删除成功的记录数。"""
        return len(self.deleted)


def classify_records(
    records: Iterable[HistoryRecord],
    check_exists: Optional[Callable[[str], bool]] = None,
) -> tuple[list[HistoryRecord], list[HistoryRecord], list[HistoryRecord]]:
    """
    把转移记录分成三组：该删的、受保护的、目标仍存活的。

    :param records: 待判断的记录
    :param check_exists: 判断路径是否存在的函数，默认 os.path.exists。
        做成可注入是为了让单测不依赖真实文件系统。
    :return: (待删除, 受保护, 仍存活)
    """
    exists = check_exists or os.path.exists
    candidates: list[HistoryRecord] = []
    protected: list[HistoryRecord] = []
    alive: list[HistoryRecord] = []

    for record in records:
        if record.protected:
            protected.append(record)
            continue
        # 目标文件还在 → 这条记录是有效的，不能删
        if record.dest and exists(record.dest):
            alive.append(record)
            continue
        # 目标没了、源还在（下载目录里的原始文件没删）→ 也不算失效，
        # 因为用户可能只是删了媒体库里的一份
        if record.src and exists(record.src):
            alive.append(record)
            continue
        candidates.append(record)

    return candidates, protected, alive


def clean_transfer_history(
    records: Iterable[HistoryRecord],
    delete_func: Callable[[int], Any],
    verify_func: Optional[Callable[[int], bool]] = None,
    check_exists: Optional[Callable[[str], bool]] = None,
) -> HistoryCleanResult:
    """
    清理失效的转移记录。

    宿主 `delete()` 对属于转移任务的记录是**静默无效**的（不会抛异常，
    只是 SQL 里多了个 `transfer_task_id IS NULL` 条件）。
    因此删除后需要用 `verify_func` 复查，否则会把「没删掉」误报成「删掉了」。

    :param records: 待判断的记录
    :param delete_func: 实际删除函数，接收 history_id
    :param verify_func: 复查函数，返回该记录是否仍存在；不传则跳过复查
    :param check_exists: 路径存在性判断，默认 os.path.exists
    :return: 清理结果
    """
    result = HistoryCleanResult()
    candidates, protected, alive = classify_records(records, check_exists)
    result.protected = [record.history_id for record in protected]
    result.kept_alive = [record.history_id for record in alive]

    for record in candidates:
        try:
            delete_func(record.history_id)
        except Exception as error:
            result.failed.append((record.history_id, str(error)))
            continue

        if verify_func is not None:
            try:
                still_exists = verify_func(record.history_id)
            except Exception as error:
                # 复查本身失败时按「已删除」处理会更危险，这里按失败上报
                result.failed.append((record.history_id, f"删除后复查失败：{error}"))
                continue
            if still_exists:
                # 宿主因为 transfer_task_id 条件没删掉
                result.protected.append(record.history_id)
                continue

        result.deleted.append(record.history_id)

    return result


def collect_dest_paths(records: Iterable[HistoryRecord]) -> list[str]:
    """从记录里取出所有目标路径（用于批量判断存在性）。"""
    return [record.dest for record in records if record.dest]


def group_by_dest_dir(records: Iterable[HistoryRecord]) -> dict[str, list[HistoryRecord]]:
    """
    按目标所在目录分组。

    转移记录是按文件记的，一个文件夹的整理会产生多条记录。
    按目录分组便于上层做「整个目录没了才清理」的批量判断。
    """
    grouped: dict[str, list[HistoryRecord]] = {}
    for record in records:
        if not record.dest:
            continue
        directory = os.path.dirname(os.path.normpath(record.dest))
        grouped.setdefault(directory, []).append(record)
    return grouped


def dir_has_any_file(directory: str) -> bool:
    """
    判断目录是否还有普通文件。

    用 `os.scandir` 而不是 `listdir`：目录很大时前者能提前短路，
    而且不需要把整个目录项列表读进内存。
    目录不存在或没权限时返回 False（视为可清理）。
    """
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_file(follow_symlinks=False):
                    return True
    except OSError:
        return False
    return False
