# -*- coding: utf-8 -*-
"""
宿主适配层：把插件的纯逻辑模块接到 MoviePilot 宿主能力上
=========================================================

这里集中处理「宿主 API 的具体调用方式」，让 `analyzer` / `deleter` /
`history` / `exporter` 这几个纯逻辑模块保持不依赖宿主，可以独立测试。

为什么单独一个文件
------------------
宿主 API 有几个容易踩的点，集中在这里便于统一处理与注释：

1. **`TransferHistoryOper.delete()` 对转移任务记录是静默无效的**
   （SQL 里带 `transfer_task_id IS NULL`），必须删后复查。
2. **`get_by_dest()` 返回单条不是列表**，批量场景要用 `get_by()`。
3. **导入可能因宿主版本差异失败**，所以全部做惰性导入：
   没装某个能力时插件其它功能照常可用，而不是整个插件加载失败。
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from app.sdk.logging import logger

from .history import HistoryCleanResult, HistoryRecord, clean_transfer_history


class TransferHistoryAdapter:
    """
    转移记录适配器。

    宿主没提供 `TransferHistoryOper` 时不报错，只是 `available` 为 False，
    调用方据此跳过转移记录清理即可。
    """

    def __init__(self) -> None:
        self._oper: Optional[Any] = None
        self._error: Optional[str] = None
        try:
            # 惰性导入：不同宿主版本这个模块的位置可能不同
            from app.db.oper.transferhistory import TransferHistoryOper

            self._oper = TransferHistoryOper()
        except Exception as error:  # noqa: BLE001 - 任何导入失败都只降级
            self._error = str(error)
            logger.debug(f"刮削残留清理：转移记录能力不可用 - {error}")

    @property
    def available(self) -> bool:
        """宿主是否提供转移记录能力。"""
        return self._oper is not None

    @property
    def error(self) -> Optional[str]:
        """不可用时的原因。"""
        return self._error

    def fetch_by_dest_prefix(self, dest_prefix: str, limit: int = 500) -> list[HistoryRecord]:
        """
        按目标路径前缀抓取转移记录。

        宿主的 `get_by_dest` 要求**完整路径精确匹配**，而我们要清的是一个目录
        下的所有记录，所以这里遍历目录下的文件逐个查——用一个受限的样本量
        避免在大库上扫太久。

        :param dest_prefix: 目标所在目录
        :param limit: 最多取多少条，防止极端情况拖慢
        """
        if not self._oper:
            return []
        import os

        if not os.path.isdir(dest_prefix):
            return []
        records: list[HistoryRecord] = []
        try:
            with os.scandir(dest_prefix) as entries:
                for entry in entries:
                    if len(records) >= limit:
                        break
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    record = self._fetch_one(entry.path)
                    if record:
                        records.append(record)
        except OSError as error:
            logger.warning(f"刮削残留清理：读取目录失败 {dest_prefix} - {error}")
        return records

    def _fetch_one(self, dest: str) -> Optional[HistoryRecord]:
        """按精确目标路径查一条记录，并转换成纯数据结构。"""
        if not self._oper:
            return None
        try:
            raw = self._oper.get_by_dest(dest)
        except Exception as error:  # noqa: BLE001
            logger.debug(f"刮削残留清理：查询转移记录失败 {dest} - {error}")
            return None
        if not raw:
            return None
        return _to_record(raw)

    def fetch_by_title(self, title: str) -> list[HistoryRecord]:
        """
        按标题抓取转移记录。

        用于「整个媒体目录都没了」的场景：这时目录下的文件名已经不存在，
        没法按 dest 精确查，只能按标题批量捞。
        """
        if not self._oper or not title:
            return []
        try:
            rows = self._oper.get_by(title=title) or []
        except Exception as error:  # noqa: BLE001
            logger.debug(f"刮削残留清理：按标题查询转移记录失败 {title} - {error}")
            return []
        records: list[HistoryRecord] = []
        for raw in rows:
            record = _to_record(raw)
            if record:
                records.append(record)
        return records

    def clean(self, records: Iterable[HistoryRecord]) -> HistoryCleanResult:
        """
        清理失效的转移记录。

        删除后用 `get_by_id` 复查：宿主的 `delete()` 对属于转移任务的记录
        是静默无效的，不复查就会把「没删掉」误报成「删掉了」。
        """
        records = list(records)
        if not self._oper or not records:
            return HistoryCleanResult()

        def delete_one(history_id: int) -> None:
            self._oper.delete(history_id)

        def verify_one(history_id: int) -> bool:
            # get_by_id 或 get 都可能存在，按能力探测
            getter = getattr(self._oper, "get_by_id", None) or getattr(self._oper, "get", None)
            if getter is None:
                # 没有复查能力时不能假设成功，抛错让上层记为失败
                raise RuntimeError("宿主未提供转移记录复查接口")
            return bool(getter(history_id))

        return clean_transfer_history(
            records, delete_func=delete_one, verify_func=verify_one
        )


def _to_record(raw: Any) -> Optional[HistoryRecord]:
    """
    把宿主的 ORM 记录转成纯数据结构。

    只取需要的字段，避免把 ORM 对象传出会话后被懒加载炸掉。
    """
    try:
        history_id = getattr(raw, "id", None)
        if history_id is None:
            return None
        return HistoryRecord(
            history_id=int(history_id),
            src=str(getattr(raw, "src", "") or ""),
            dest=str(getattr(raw, "dest", "") or ""),
            transfer_task_id=getattr(raw, "transfer_task_id", None),
        )
    except (TypeError, ValueError) as error:
        logger.debug(f"刮削残留清理：转移记录字段解析失败 - {error}")
        return None
