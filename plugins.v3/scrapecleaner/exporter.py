# -*- coding: utf-8 -*-
"""
扫描报告导出（纯逻辑，不依赖 MoviePilot 运行时）
================================================

支持 CSV 和 JSON 两种格式。

为什么用 CSV 而不是 Excel
-------------------------
导出报告的主要用途是「在表里筛选、排序、存档」。CSV 不需要额外依赖
（openpyxl 之类的宿主不一定有），Excel/WPS/Numbers 都能直接打开。

**中文编码的坑**：直接写 UTF-8 的 CSV，在中文版 Excel 里双击打开会乱码，
因为 Excel 默认按 GBK 猜编码。所以要写 **UTF-8 with BOM**（`utf-8-sig`），
Excel 看到 BOM 就会正确识别。

安全考虑
--------
报告里包含完整文件路径，属于用户的本地信息。
导出时默认**不写入文件系统**，而是返回 bytes 交给插件写进自己的数据目录，
避免插件在媒体库里意外产生文件（媒体库里多出来的 .csv 会被下一次扫描
当成刮削文件，虽然 CSV 不在刮削后缀里，但没必要冒险）。
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .analyzer import ScanReport, ScrapEntry


# CSV 列名。顺序即导出顺序。
CSV_COLUMNS: tuple[tuple[str, str], ...] = (
    ("path", "完整路径"),
    ("name", "文件名"),
    ("dir", "所在目录"),
    ("is_orphan", "是否残留"),
    ("reason", "判定理由"),
    ("reason_label", "理由说明"),
    ("detail", "判定依据"),
    ("dir_shared", "目录级文件"),
    ("size", "字节数"),
    ("size_human", "大小"),
)

# 判定理由的中文展示名
_REASON_LABELS: dict[str, str] = {
    "orphan_stem": "无同名媒体文件",
    "orphan_dir_shared": "目录已无媒体",
    "protected": "受保护",
    "media_present": "有对应媒体",
    "dir_alive": "目录仍有媒体",
    "generic_ambiguous": "通用名无法归属",
    "recent": "修改时间在保护期内",
}


@dataclass
class ExportResult:
    """一次导出的产物。"""

    filename: str
    content: bytes
    row_count: int
    mime: str


def _human_size(size: int) -> str:
    """字节数转可读大小。"""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


def entry_to_row(entry: ScrapEntry) -> dict[str, Any]:
    """把一条扫描结果转成一行导出数据。"""
    import os

    return {
        "path": entry.path,
        "name": os.path.basename(entry.path),
        "dir": os.path.dirname(entry.path),
        "is_orphan": "是" if entry.is_orphan else "否",
        "reason": entry.reason.value,
        "reason_label": _REASON_LABELS.get(entry.reason.value, entry.reason.value),
        "detail": entry.detail,
        "dir_shared": "是" if entry.dir_shared else "否",
        "size": entry.size,
        "size_human": _human_size(entry.size),
    }


def export_csv(
    report: ScanReport,
    orphans_only: bool = True,
    include_kept: bool = False,
) -> ExportResult:
    """
    导出为 CSV。

    :param report: 扫描报告
    :param orphans_only: 只导出残留项（默认）。False 时导出全部。
    :param include_kept: 兼容用，等价于 orphans_only=False
    :return: 导出结果（内容为带 BOM 的 UTF-8 字节）
    """
    if include_kept or not orphans_only:
        entries: list[ScrapEntry] = list(report.entries)
    else:
        entries = list(report.orphans)

    buffer = io.StringIO(newline="")
    # header 要用中文列名，所以先写表头行，再按顺序写数据行。
    # 注意：不能直接用 DictWriter(fieldnames=英文 key)，那样表头会变成英文。
    writer = csv.writer(buffer)
    writer.writerow([label for _, label in CSV_COLUMNS])
    for entry in entries:
        row = entry_to_row(entry)
        writer.writerow([row.get(key, "") for key, _ in CSV_COLUMNS])

    stamp = _stamp(report)
    return ExportResult(
        filename=f"scrapecleaner-{stamp}.csv",
        # utf-8-sig = UTF-8 with BOM，Excel 才不乱码
        content=buffer.getvalue().encode("utf-8-sig"),
        row_count=len(entries),
        mime="text/csv; charset=utf-8",
    )


def export_json(report: ScanReport, orphans_only: bool = True) -> ExportResult:
    """导出为 JSON（结构更完整，便于程序处理）。"""
    entries = report.orphans if orphans_only else report.entries
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scan_time": report.scan_time,
        "scanned_files": report.scanned_files,
        "orphan_count": len(report.orphans),
        "orphan_bytes": report.orphan_bytes,
        "kept_count": len(report.entries) - len(report.orphans),
        "entries": [entry_to_row(entry) for entry in entries],
        "errors": [{"path": path, "error": reason} for path, reason in report.errors[:200]],
    }
    return ExportResult(
        filename=f"scrapecleaner-{_stamp(report)}.json",
        content=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        row_count=len(entries),
        mime="application/json; charset=utf-8",
    )


def export(
    report: ScanReport,
    fmt: str = "csv",
    orphans_only: bool = True,
) -> ExportResult:
    """
    按格式导出。

    :param fmt: `csv` 或 `json`，其它值按 csv 处理（不报错，方便调用方）
    """
    if str(fmt).lower() == "json":
        return export_json(report, orphans_only=orphans_only)
    return export_csv(report, orphans_only=orphans_only)


def _stamp(report: ScanReport) -> str:
    """生成用于文件名的时间戳。"""
    if report.scan_time:
        try:
            parsed = datetime.strptime(report.scan_time, "%Y-%m-%d %H:%M:%S")
            return parsed.strftime("%Y%m%d-%H%M%S")
        except ValueError:
            pass
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def summary_text(report: ScanReport) -> str:
    """生成一段人类可读的摘要，用于通知或页面展示。"""
    orphans = report.orphans
    if not orphans:
        return f"扫描 {report.scanned_files} 个文件，未发现残留刮削文件。"
    # 按目录统计，列出前 5 个
    import os

    counts: dict[str, int] = {}
    for entry in orphans:
        directory = os.path.dirname(entry.path)
        counts[directory] = counts.get(directory, 0) + 1
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
    lines = [
        f"扫描 {report.scanned_files} 个文件，发现 {len(orphans)} 个残留"
        f"（{_human_size(report.orphan_bytes)}）。",
        "分布最多的目录：",
    ]
    lines.extend(f"  · {name}（{count} 个）" for name, count in top)
    return "\n".join(lines)
