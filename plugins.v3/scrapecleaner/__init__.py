# -*- coding: utf-8 -*-
"""
MoviePilot V3 刮削残留清理插件（扫描对账式）
============================================

与官方 `scrapefileclean` 的区别
-------------------------------
官方插件的触发点是「源文件被删除的那个瞬间」：它监控下载目录，靠 (dev, inode)
识别硬链接，源文件一消失就联动清理。这要求删除动作必须被它感知到；如果是
qBittorrent 自己删文件、其它插件清理种子、或者插件当时没运行，就完全捕捉不到，
媒体库里会缓慢积累一批「视频早没了、nfo 和图片还留着」的残留。

本插件走相反的路：**定期（或手动）全库扫描对账**。它不关心文件是怎么没的，
只关心「现在这个刮削文件还有没有主」。因此：

- 存量残留能一次扫出来，不需要等到下一次删除事件；
- 天然不会误删——不删除任何正在被引用的文件；
- 先出报告、人工确认后才删，删之前还有五道安全阀复核。

本插件与官方插件可以并行使用，互不冲突。

架构
----
- `analyzer.py`：纯判断逻辑，给定目录树算出哪些刮削文件没有主；
- `deleter.py`：纯执行逻辑，带五道安全阀，支持回收站与空目录清理；
- `__init__.py`（本文件）：MoviePilot 集成层，负责配置、定时任务、API 与页面。

判断与执行两块刻意做成了不依赖 MoviePilot 的纯 Python 模块，可以脱离宿主
单元测试——清理类插件删错的代价太高，核心逻辑必须能被穷举验证。
"""
from __future__ import annotations

import os
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger
from fastapi import Body

from app.schemas.types import MessageType
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.plugin import _PluginBase
from app.schemas.types import EventType

try:
    from app.schemas import Response
except ImportError:  # pragma: no cover - 兼容个别版本导出的位置差异
    from app.schemas.response import Response  # type: ignore


def _response_class():
    """
    取「原生响应」的类，用于 `get_api()` 里声明 `response_class`。

    按官方文档要求，文件类端点必须显式声明 `response_class` 并在 OpenAPI
    写明 content。类本身在调用时才取（不在模块导入期），这样插件加载
    不依赖具体的 Web 框架位置。
    """
    try:
        from starlette.responses import Response as _Response
    except ImportError:  # pragma: no cover
        try:
            from fastapi.responses import Response as _Response  # type: ignore
        except ImportError:
            return None
    return _Response


def _file_response(content: bytes, media_type: str, headers: Dict[str, str]):
    """
    构造一个文件下载响应。

    为什么要惰性导入：不同宿主/测试环境里 `starlette` 与 `fastapi` 的可用性
    不一样，在模块顶层硬导入会让整个插件加载失败（插件不该因为一个下载端点
    的依赖而整体不可用）。这里按需导入，失败时退回纯文本响应并说明原因。
    """
    try:
        from starlette.responses import Response as _Response
    except ImportError:  # pragma: no cover
        try:
            from fastapi.responses import Response as _Response  # type: ignore
        except ImportError:
            return _plain_response(
                "当前宿主缺少文件响应支持，无法导出", status_code=500
            )
    return _Response(content=content, media_type=media_type, headers=headers)


def _plain_response(content: str, status_code: int = 200, media_type: str = "text/plain"):
    """构造纯文本响应（同样是惰性导入）。"""
    try:
        from starlette.responses import PlainTextResponse
    except ImportError:  # pragma: no cover
        try:
            from fastapi.responses import PlainTextResponse  # type: ignore
        except ImportError:
            return content
    return PlainTextResponse(content=content, status_code=status_code, media_type=media_type)

from .analyzer import (
    DEFAULT_PROTECTED_PATTERNS,
    AnalyzerOptions,
    ScanReport,
    ScrapEntry,
    analyze_directory,
)
from .deleter import DeleteMode, DeleteResult, execute_delete
from .exporter import export as export_report_file
from .exporter import summary_text
from .host_adapter import TransferHistoryAdapter
from .library_rules import describe_rules, effective_options, parse_library_rules, resolve_rule

# 扫描报告在插件数据目录里的文件名
REPORT_DATA_KEY = "last_report"
HISTORY_DATA_KEY = "clean_history"

# 判定理由的中文展示名（页面表格用）
_REASON_LABELS: Dict[str, str] = {
    "orphan_stem": "无同名媒体文件",
    "orphan_dir_shared": "目录已空",
    "protected": "受保护",
    "media_present": "有对应媒体",
    "dir_alive": "目录仍有媒体",
    "generic_ambiguous": "通用名无法归属",
    "recent": "修改时间过近（保护中）",
}


class ScrapeCleaner(_PluginBase):
    """刮削残留清理插件主类。"""

    # -----------------------------------------------------------
    # 插件元信息（必须与 package.v3.json 完全一致）
    # -----------------------------------------------------------
    plugin_name = "刮削残留清理"
    plugin_desc = (
        "扫描对账式清理：找出媒体库里已经没有对应视频的刮削文件（nfo/图片/字幕），"
        "先出报告、人工确认后再删，带回收站与多道安全阀，配合目录级文件保护"
    )
    plugin_icon = "clean.png"
    plugin_version = "1.1.0"
    plugin_author = "misakashiny"
    author_url = "https://github.com/misakashiny"
    plugin_config_prefix = "scrapecleaner_"
    plugin_order = 60
    auth_level = 1

    # -----------------------------------------------------------
    # 运行状态
    # -----------------------------------------------------------
    _enabled: bool = False
    _notify: bool = False
    _delete_mode: str = DeleteMode.RECYCLE
    _recycle_dir: str = ""
    _keep_days: int = 30
    _remove_empty_dirs: bool = True

    # 库目录（每行一个）
    _library_dirs: str = ""
    _exclude_keywords: str = ""
    _extra_scrap_exts: str = ""
    _extra_media_exts: str = ""
    _extra_protected: str = ""
    _max_files: int = 200_000

    # —— 新增能力（v1.1.0）——
    # 新文件保护期（分钟），0 = 不保护
    _min_age_minutes: int = 0
    # 自定义正则规则（原始字符串，由 analyzer 编译）
    _media_patterns: str = ""
    _scrap_patterns: str = ""
    _dir_shared_patterns: str = ""
    # 删除前检测文件占用
    _skip_in_use: bool = True
    # 联动清理失效的转移记录
    _clean_history: bool = False
    # 多媒体库分别配置策略（每行一条）
    _library_rules_text: str = ""
    # 解析后的规则（init_plugin 时构建）
    _library_rules: List[Any] = []

    # 定时扫描 cron（空 = 不定时）
    _scan_cron: str = ""

    # 最近一次扫描报告（内存 + 持久化）
    _last_report: Optional[ScanReport] = None
    _last_scan_time: str = ""
    # 扫描/删除互斥，避免用户连点导致并发改同一个目录
    _lock = threading.Lock()
    _scanning: bool = False

    # -----------------------------------------------------------
    # 配置项默认值（供 get_form 使用）
    # -----------------------------------------------------------
    @staticmethod
    def _default_config() -> Dict[str, Any]:
        """返回配置页面的默认模型。"""
        return {
            "enabled": False,
            "notify": False,
            "library_dirs": "",
            "exclude_keywords": "",
            "extra_scrap_exts": "",
            "extra_protected": "",
            "delete_mode": DeleteMode.RECYCLE,
            "recycle_dir": "",
            "keep_days": 30,
            "remove_empty_dirs": True,
            "max_files": 200_000,
            "scan_cron": "",
            # —— 新增能力（v1.1.0）——
            # 默认 1 天：刚整理的刮削文件不该马上被判残留。
            # 库里已有的老文件不受影响，所以这个默认值不会降低清理效果。
            "min_age_minutes": 1440,
            "extra_media_exts": "",
            "media_patterns": "",
            "scrap_patterns": "",
            "dir_shared_patterns": "",
            "skip_in_use": True,
            "clean_history": False,
            "library_rules_text": "",
            # 纯前端编辑区：不入库，只为让 VTextarea 有初值
            "pending_paths": "",
        }

    # -----------------------------------------------------------
    # 生命周期
    # -----------------------------------------------------------
    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """
        读取配置并建立本次运行状态。

        允许重复调用：宿主在保存配置、重载插件时会再次进入这里，
        因此方法内部必须先释放旧资源再重建。
        """
        self.stop_service()

        config = config or {}
        defaults = self._default_config()
        merged = {**defaults, **{k: v for k, v in config.items() if v is not None}}

        # `pending_paths` 只是配置页上的临时编辑区，不能留在配置里，
        # 否则每次保存都会把一长串路径写进数据库。
        if config.get("pending_paths"):
            persisted = {k: v for k, v in merged.items() if k != "pending_paths"}
            try:
                self.update_config(persisted)
            except Exception as error:  # 保存失败不该阻断初始化
                logger.error(f"刮削残留清理：剥离临时字段失败 - {error}")

        self._enabled = bool(merged.get("enabled"))
        self._notify = bool(merged.get("notify"))
        self._library_dirs = str(merged.get("library_dirs") or "")
        self._exclude_keywords = str(merged.get("exclude_keywords") or "")
        self._extra_scrap_exts = str(merged.get("extra_scrap_exts") or "")
        self._extra_protected = str(merged.get("extra_protected") or "")
        self._delete_mode = str(merged.get("delete_mode") or DeleteMode.RECYCLE)
        if self._delete_mode not in (DeleteMode.RECYCLE, DeleteMode.REMOVE):
            self._delete_mode = DeleteMode.RECYCLE
        # 回收目录默认放在插件自己的数据目录，绝对不能落在媒体库内
        self._recycle_dir = str(merged.get("recycle_dir") or "").strip()
        if not self._recycle_dir:
            self._recycle_dir = str(self.get_data_path() / "recycle")
        self._keep_days = self._to_int(merged.get("keep_days"), 30, 0, 3650)
        self._remove_empty_dirs = bool(merged.get("remove_empty_dirs", True))
        self._max_files = self._to_int(merged.get("max_files"), 200_000, 1_000, 2_000_000)
        self._scan_cron = str(merged.get("scan_cron") or "").strip()

        # —— 新增能力 ——
        self._min_age_minutes = self._to_int(merged.get("min_age_minutes"), 1440, 0, 525_600)
        self._extra_media_exts = str(merged.get("extra_media_exts") or "")
        self._media_patterns = str(merged.get("media_patterns") or "")
        self._scrap_patterns = str(merged.get("scrap_patterns") or "")
        self._dir_shared_patterns = str(merged.get("dir_shared_patterns") or "")
        self._skip_in_use = bool(merged.get("skip_in_use", True))
        self._clean_history = bool(merged.get("clean_history", False))
        self._library_rules_text = str(merged.get("library_rules_text") or "")

        # 多库策略：解析失败只提示不阻断，绝不让一条写错的规则影响整个插件
        self._library_rules = []
        if self._library_rules_text.strip():
            parsed = parse_library_rules(self._library_rules_text)
            self._library_rules = parsed.rules
            for warning in parsed.warnings:
                logger.warning(f"刮削残留清理：多库策略配置有问题 - {warning}")
            if parsed.rules:
                logger.info(
                    f"刮削残留清理：已加载 {len(parsed.rules)} 条媒体库策略"
                )

        if not self._enabled:
            logger.info("刮削残留清理插件未启用")
            return

        dirs = self.library_dirs()
        if not dirs:
            logger.warning("刮削残留清理：未配置媒体库目录，插件不会执行任何清理")
            return
        logger.info(
            f"刮削残留清理已启用：媒体库 {len(dirs)} 个目录，"
            f"删除模式={self._delete_mode}，回收目录={self._recycle_dir}"
        )

    @staticmethod
    def _to_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        """把配置值安全地转换为范围内的整数。"""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return max(minimum, min(maximum, parsed))

    def get_state(self) -> bool:
        """返回插件当前是否启用。"""
        return self._enabled

    def stop_service(self) -> None:
        """释放资源。本插件不持有长期线程，扫描期间只占用一次锁。"""
        self._scanning = False

    # -----------------------------------------------------------
    # 配置解析
    # -----------------------------------------------------------
    def library_dirs(self) -> List[str]:
        """解析媒体库目录列表（每行一个，去空去重保序）。"""
        seen: set[str] = set()
        result: List[str] = []
        for line in self._library_dirs.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            candidate = line.strip().strip('"').strip("'")
            if not candidate:
                continue
            normalized = os.path.normpath(candidate)
            if normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    @staticmethod
    def _split_lines(text: str) -> Tuple[str, ...]:
        """把多行/逗号分隔的文本拆成元组。"""
        cleaned = text.replace("，", ",").replace("\r\n", "\n").replace("\r", "\n")
        items: List[str] = []
        for chunk in cleaned.replace(",", "\n").split("\n"):
            item = chunk.strip()
            if item and item not in items:
                items.append(item)
        return tuple(items)

    def _analyzer_options(self) -> AnalyzerOptions:
        """
        按当前配置构造扫描参数。

        字段语义（与 analyzer 的约定一致）：

        - `extra_scrap_exts` / `extra_media_exts`：**增量**后缀集合，
          与内置集合取并集，不再是「整个集合」。
        - `*_patterns`：用户自定义正则，**传原始字符串**，
          由 `analyzer.compile_patterns()` 统一编译；
          编译失败会被收集进扫描报告的 errors，而不会让扫描崩掉。
        - `min_age_seconds`：新文件保护期，0 表示不保护。
        """
        extra_protected = self._split_lines(self._extra_protected)
        return AnalyzerOptions(
            extra_scrap_exts=_to_ext_set(self._split_lines(self._extra_scrap_exts)),
            extra_media_exts=_to_ext_set(self._split_lines(self._extra_media_exts)),
            media_patterns=self._split_lines(self._media_patterns),
            scrap_patterns=self._split_lines(self._scrap_patterns),
            dir_shared_patterns=self._split_lines(self._dir_shared_patterns),
            min_age_seconds=self._min_age_minutes * 60,
            protected_patterns=tuple(DEFAULT_PROTECTED_PATTERNS) + extra_protected,
            exclude_keywords=self._split_lines(self._exclude_keywords),
            max_files=self._max_files,
        )

    def _effective_scrap_exts(self) -> frozenset[str]:
        """内置 + 用户在配置里追加的刮削后缀（供 deleter 校验使用）。"""
        from .analyzer import SCRAP_EXTENSIONS

        return frozenset(SCRAP_EXTENSIONS | _to_ext_set(self._split_lines(self._extra_scrap_exts)))

    # -----------------------------------------------------------
    # 扫描
    # -----------------------------------------------------------
    def scan(self) -> ScanReport:
        """
        扫描所有配置的媒体库目录，返回合并后的报告。

        本方法**只读文件系统**，不删除任何文件。

        多媒体库策略在这里生效：被 `disabled=true` 排除的目录直接跳过，
        带 `min_age_minutes` / `max_files` 的库用自己那套参数扫。
        """
        report = ScanReport()
        base_options = self._analyzer_options()

        for root in self.library_dirs():
            # 整个库被排除时不做任何扫描
            if self._is_library_disabled(root):
                logger.info(f"刮削残留清理：跳过被排除的媒体库 {root}")
                report.skipped_dirs.append(root)
                continue
            options = self._options_for_root(root, base_options)
            part = analyze_directory(root, options)

            # 扫描根内部可能有被 disabled 的子目录（比如只把「纪录片」子库排除掉），
            # 这些条目要在这里滤掉——analyzer 不知道多库策略。
            if self._library_rules:
                part.entries = [
                    entry for entry in part.entries
                    if not self._is_library_disabled(entry.path)
                ]

            report.entries.extend(part.entries)
            report.dir_stats.update(part.dir_stats)
            report.errors.extend(part.errors)
            report.skipped_dirs.extend(part.skipped_dirs)
            report.scanned_files += part.scanned_files

        report.entries.sort(key=lambda entry: entry.path)
        return report

    def _is_library_disabled(self, path: str) -> bool:
        """
        该路径是否被多库策略中的 `disabled=true` 排除。

        用路径匹配而不是「等于某个扫描根」：用户可能只把扫描根下的某个子库排除，
        这种情况下不能把整个扫描根跳过。
        """
        if not self._library_rules:
            return False
        rule = resolve_rule(path, self._library_rules)
        return bool(rule and rule.get("disabled") is True)

    def _options_for_root(self, root: str, base: AnalyzerOptions) -> AnalyzerOptions:
        """
        按多库策略为某个媒体库派生扫描参数。

        没配策略或策略里没写某个键时，完全继承全局参数——
        所以「不配置多库策略」的行为与以前一模一样。
        """
        if not self._library_rules:
            return base
        merged = effective_options(root, self._library_rules, {})
        if not merged:
            return base

        # 用 dataclasses.replace 做浅拷贝，避免修改 base 影响其它库
        from dataclasses import replace

        overrides: Dict[str, Any] = {}
        if "min_age_minutes" in merged:
            overrides["min_age_seconds"] = int(merged["min_age_minutes"]) * 60
        if "max_files" in merged:
            overrides["max_files"] = int(merged["max_files"])
        if "exclude" in merged:
            extra = tuple(
                item.strip()
                for item in str(merged["exclude"]).replace("，", ",").split(",")
                if item.strip()
            )
            overrides["exclude_keywords"] = tuple(base.exclude_keywords) + extra
        if not overrides:
            return base
        try:
            return replace(base, **overrides)
        except TypeError:
            # 宿主/上游字段变化时退回全局参数，不让单个库的配置拖垮整次扫描
            logger.warning(f"刮削残留清理：媒体库 {root} 的策略无法应用，使用全局参数")
            return base

    def _store_report(self, report: ScanReport) -> None:
        """把扫描报告持久化，供页面刷新后仍能看到上次结果。"""
        self._last_report = report
        self._last_scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        report.scan_time = self._last_scan_time
        try:
            payload = {
                "scan_time": self._last_scan_time,
                "scanned_files": report.scanned_files,
                "entries": [
                    {
                        "path": entry.path,
                        "size": entry.size,
                        "reason": entry.reason.value,
                        "detail": entry.detail,
                        "dir_shared": entry.dir_shared,
                    }
                    for entry in report.entries
                ],
                "errors": report.errors[:200],
            }
            self.save_data(REPORT_DATA_KEY, payload)
        except Exception as error:  # 持久化失败不应影响扫描结果本身
            logger.error(f"刮削残留清理：保存扫描报告失败 - {error}")

    def _load_report(self) -> Optional[ScanReport]:
        """从插件数据恢复上次扫描报告。"""
        if self._last_report is not None:
            return self._last_report
        try:
            payload = self.get_data(REPORT_DATA_KEY)
            if not payload:
                return None
            from .analyzer import Reason

            report = ScanReport()
            report.scanned_files = int(payload.get("scanned_files") or 0)
            for item in payload.get("entries") or []:
                try:
                    reason = Reason(item.get("reason"))
                except ValueError:
                    continue
                report.entries.append(ScrapEntry(
                    path=item.get("path", ""),
                    size=int(item.get("size") or 0),
                    reason=reason,
                    detail=item.get("detail", ""),
                    dir_shared=bool(item.get("dir_shared")),
                ))
            report.errors = [
                (item[0], item[1])
                for item in (payload.get("errors") or [])
                if isinstance(item, (list, tuple)) and len(item) == 2
            ]
            self._last_report = report
            self._last_scan_time = str(payload.get("scan_time") or "")
            report.scan_time = self._last_scan_time
            return report
        except Exception as error:
            logger.error(f"刮削残留清理：读取扫描报告失败 - {error}")
            return None

    # -----------------------------------------------------------
    # 定时服务
    # -----------------------------------------------------------
    def get_service(self) -> List[Dict[str, Any]]:
        """按配置的 cron 注册定时扫描任务。"""
        if not self.get_state() or not self._scan_cron:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._scan_cron)
        except (ValueError, TypeError) as error:
            logger.error(f"刮削残留清理：定时表达式无效（{self._scan_cron}）- {error}")
            return []
        return [
            {
                "id": "ScrapeCleaner.Scan",
                "name": "刮削残留清理定时扫描",
                "trigger": trigger,
                "func": self.scheduled_scan,
                "kwargs": {},
            }
        ]

    def scheduled_scan(self) -> None:
        """定时扫描入口：扫描并通知结果（不自动删除）。"""
        if not self.get_state():
            return
        report = self._run_scan_safely()
        if report is None:
            return
        if self._notify:
            if report.orphans:
                self.post_message(
                    mtype=MessageType.Plugin,
                    title="🧹 刮削残留扫描完成",
                    # summary_text 会给出残留最多的几个目录，比单纯报个数有用
                    text=f"{summary_text(report)}\n\n请到插件页面确认后再执行清理。",
                )
            else:
                self.post_message(
                    mtype=MessageType.Plugin,
                    title="🧹 刮削残留扫描完成",
                    text=summary_text(report),
                )
        self._purge_expired_recycle()

    def _run_scan_safely(self) -> Optional[ScanReport]:
        """带互斥锁的扫描，避免并发扫描同一批目录。"""
        if not self._lock.acquire(blocking=False):
            logger.warning("刮削残留清理：上一次扫描尚未结束，本次跳过")
            return None
        try:
            self._scanning = True
            start = datetime.now()
            report = self.scan()
            elapsed = (datetime.now() - start).total_seconds()
            self._store_report(report)
            logger.info(
                f"刮削残留清理：扫描完成，{report.scanned_files} 个文件，"
                f"残留 {len(report.orphans)} 个，耗时 {elapsed:.1f}s"
            )
            if report.errors:
                logger.warning(f"刮削残留清理：扫描期间有 {len(report.errors)} 个路径无法访问")
            return report
        except Exception as error:
            logger.error(f"刮削残留清理：扫描失败 - {error}")
            return None
        finally:
            self._scanning = False
            self._lock.release()

    # -----------------------------------------------------------
    # 删除
    # -----------------------------------------------------------
    def perform_delete(self, paths: List[str]) -> DeleteResult:
        """
        执行删除。只会删除「本次扫描判定为残留」的文件。

        安全方向说明：报告只用作**允许清单**，真正的删除还要经过
        `deleter` 的五道阀（含重新 stat 文件）。因此即使报告过期，
        最坏结果是「该删的没删掉」，不会出现「删了不该删的」。

        :param paths: 用户在页面上勾选的路径
        :return: 执行结果
        """
        result = DeleteResult()
        report = self._load_report()
        if report is None:
            for path in paths:
                result.skipped.append((path, "没有可用的扫描报告，请先执行扫描"))
            return result

        roots = self.library_dirs()
        if not roots:
            for path in paths:
                result.skipped.append((path, "未配置媒体库目录，拒绝执行"))
            return result

        # 报告与当前配置的库目录对不上时，给出可诊断的原因而不是让用户困惑
        mismatched = [path for path in paths if not _under_any_root(path, roots)]
        if mismatched:
            stale = DeleteResult()
            for path in mismatched:
                stale.skipped.append((
                    path,
                    "该路径不在当前配置的媒体库目录下；"
                    "如果你刚改过媒体库目录，请重新扫描后再清理",
                ))
            rest = [path for path in paths if path not in set(mismatched)]
            if rest:
                done = self._run_delete(rest, report)
                stale.deleted, stale.recycled = done.deleted, done.recycled
                stale.skipped = stale.skipped + done.skipped
                stale.failed = done.failed
                stale.removed_dirs = done.removed_dirs
            return stale

        return self._run_delete(paths, report)

    def _run_delete(self, paths: List[str], report: ScanReport) -> DeleteResult:
        """
        执行删除并处理报告/历史/通知（调用方已完成前置检查）。

        多库策略在这里生效：如果某条路径所在的库单独配置了 `mode` / `recycle_dir`，
        就按那个库的策略删，而不是一刀切用全局策略。
        """
        options = self._analyzer_options()
        result = DeleteResult()

        # 按「实际生效的删除策略」分组，避免每个文件都起一次 execute_delete
        # （那会让空目录清理反复扫描同一批目录）。
        buckets: Dict[tuple[str, str], List[str]] = {}
        for path in paths:
            mode, recycle_dir = self._delete_strategy_for(path)
            buckets.setdefault((mode, recycle_dir), []).append(path)

        delete_options = options
        for (mode, recycle_dir), group in buckets.items():
            part = execute_delete(
                paths=group,
                roots=self.library_dirs(),
                report=report,
                mode=mode,
                recycle_dir=recycle_dir,
                options=delete_options,
                remove_empty_dirs=self._remove_empty_dirs,
                skip_in_use=self._skip_in_use,
            )
            result.deleted.extend(part.deleted)
            result.recycled.extend(part.recycled)
            result.skipped.extend(part.skipped)
            result.failed.extend(part.failed)
            result.removed_dirs.extend(part.removed_dirs)

        # 从报告里移除已处理项，避免页面重复展示
        handled = set(result.deleted) | set(result.recycled)
        if handled:
            report.entries = [
                entry for entry in report.entries if entry.path not in handled
            ]
            self._store_report(report)

        # 联动清理转移记录（可选）
        history_result = None
        if self._clean_history and handled:
            history_result = self._clean_transfer_history(sorted(handled))

        self._append_history(result, history_result)
        if self._notify and result.total_done:
            extra = ""
            if history_result is not None and history_result.total_done:
                extra = f"\n已清理转移记录 {history_result.total_done} 条。"
            self.post_message(
                mtype=MessageType.Plugin,
                title="🧹 刮削残留清理完成",
                text=(
                    f"已处理 {result.total_done} 个残留文件，"
                    f"清理空目录 {len(result.removed_dirs)} 个。"
                    + (f"\n失败 {len(result.failed)} 个。" if result.failed else "")
                    + extra
                ),
            )
        return result

    def _delete_strategy_for(self, path: str) -> tuple[str, str]:
        """
        返回某个路径实际生效的 (删除模式, 回收目录)。

        没有多库策略时直接用全局配置；某条策略只写了 `mode` 没写 `recycle_dir`
        时，回收目录继承全局的。
        """
        if not self._library_rules:
            return self._delete_mode, self._recycle_dir
        rule = resolve_rule(path, self._library_rules)
        if rule is None:
            return self._delete_mode, self._recycle_dir

        mode = str(rule.get("mode") or self._delete_mode)
        if mode not in (DeleteMode.RECYCLE, DeleteMode.REMOVE):
            mode = self._delete_mode
        recycle_dir = str(rule.get("recycle_dir") or self._recycle_dir)
        return mode, recycle_dir

    def _clean_transfer_history(self, deleted_paths: List[str]):
        """
        清理与已删文件相关的失效转移记录。

        只在用户显式开启 `clean_history` 时调用。宿主不提供相关能力时
        静默跳过（返回 None），不影响主流程。
        """
        adapter = TransferHistoryAdapter()
        if not adapter.available:
            logger.info("刮削残留清理：宿主不支持转移记录操作，跳过联动清理")
            return None

        records = []
        seen_dirs: set[str] = set()
        for path in deleted_paths:
            directory = os.path.dirname(path)
            # 同一目录只抓一次：一个剧集目录可能有几十个文件，
            # 每个都去扫目录会把数据库调用放大几十倍
            if directory in seen_dirs:
                continue
            seen_dirs.add(directory)
            records.extend(adapter.fetch_by_dest_prefix(directory))

        if not records:
            return None

        # 去重（同一 id 可能被多个目录抓到）
        unique = {record.history_id: record for record in records}
        result = adapter.clean(unique.values())
        logger.info(
            f"刮削残留清理：转移记录清理完成，删除 {len(result.deleted)} 条，"
            f"受保护 {len(result.protected)} 条，仍有效 {len(result.kept_alive)} 条"
        )
        return result

    def _append_history(self, result: DeleteResult, history_result: Any = None) -> None:
        """把本次执行记录追加到历史，最多保留 50 条。"""
        try:
            history = self.get_data(HISTORY_DATA_KEY) or []
            if not isinstance(history, list):
                history = []
            # skip_summary() 的三个键恒定存在、恒定是 int，可直接入档
            try:
                skip_summary = result.skip_summary()
            except Exception:
                skip_summary = {"in_use": 0, "unknown": 0, "other": len(result.skipped)}
            entry: Dict[str, Any] = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "mode": self._delete_mode,
                "deleted": len(result.deleted),
                "recycled": len(result.recycled),
                "skipped": len(result.skipped),
                # 跳过原因细分：因占用 / 因无法判断 / 其它
                "skipped_in_use": skip_summary.get("in_use", 0),
                "skipped_unknown": skip_summary.get("unknown", 0),
                "failed": [(path, reason) for path, reason in result.failed][:50],
                "removed_dirs": len(result.removed_dirs),
            }
            if history_result is not None:
                entry["history_deleted"] = len(getattr(history_result, "deleted", []))
                entry["history_protected"] = len(getattr(history_result, "protected", []))
            history.insert(0, entry)
            self.save_data(HISTORY_DATA_KEY, history[:50])
        except Exception as error:
            logger.error(f"刮削残留清理：写入历史失败 - {error}")

    def _purge_expired_recycle(self) -> None:
        """清理回收站里超过保留天数的内容。"""
        if self._keep_days <= 0 or not self._recycle_dir:
            return
        if not os.path.isdir(self._recycle_dir):
            return
        cutoff = datetime.now().timestamp() - self._keep_days * 86400
        try:
            for batch in os.listdir(self._recycle_dir):
                batch_path = os.path.join(self._recycle_dir, batch)
                if not os.path.isdir(batch_path):
                    continue
                if os.path.getmtime(batch_path) > cutoff:
                    continue
                self._remove_tree(batch_path)
                logger.info(f"刮削残留清理：回收站批次已过期清除 {batch_path}")
        except OSError as error:
            logger.error(f"刮削残留清理：清理回收站失败 - {error}")

    @staticmethod
    def _remove_tree(path: str) -> None:
        """递归删除目录（仅用于插件自己的回收目录）。"""
        import shutil

        shutil.rmtree(path, ignore_errors=True)

    # -----------------------------------------------------------
    # API
    # -----------------------------------------------------------
    def get_api(self) -> List[Dict[str, Any]]:
        """
        注册插件 API。

        三点必须注意（都是宿主真实行为，写错接口就调不通）：

        1. `auth` 必须显式写 `"bear"`。宿主的默认值是 `"apikey"`
           （`app/adapters/web/plugin/routes.py` 中 `api.pop("auth", "apikey")`），
           而且非 `"bear"` 的任何值都会**静默降级**为 apikey 校验，不报错。
        2. 插件路由用 `route_class_override=APIRoute`，宿主**不会**替我们包
           `{success, message, data}`，并且声明了 `response_model` 后
           FastAPI 会按它过滤字段——所以这里不声明 `response_model`，
           直接返回 `schemas.Response`，避免字段被意外裁掉。
        3. POST 的入参用 `Body(...)` 接收：`get_page()` 的 `events.click`
           会把 `params` 原样作为 JSON body 发送，`Body(...)` 正好接住。
        """
        return [
            {
                "path": "/scan",
                "endpoint": self.api_scan,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "扫描媒体库，找出残留刮削文件",
            },
            {
                "path": "/report",
                "endpoint": self.api_report,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取上次扫描报告",
            },
            {
                "path": "/delete",
                "endpoint": self.api_delete,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "删除选中的残留文件",
            },
            {
                "path": "/history",
                "endpoint": self.api_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取清理历史",
            },
            {
                "path": "/export",
                "endpoint": self.api_export,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "导出上次扫描报告（CSV/JSON）",
                # 这是文件下载端点，不是普通 JSON，必须显式声明原生响应
                "response_model": None,
                "response_class": _response_class(),
                "responses": {
                    200: {
                        "content": {
                            "text/csv": {"schema": {"type": "string"}},
                            "application/json": {"schema": {"type": "string"}},
                        }
                    }
                },
            },
            {
                "path": "/rules",
                "endpoint": self.api_rules,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查看实际生效的多库策略",
            },
        ]

    def api_scan(self) -> Response[dict]:
        """执行一次扫描并返回结果。"""
        if not self.get_state():
            return Response(success=False, message="插件未启用")
        if not self.library_dirs():
            return Response(success=False, message="未配置媒体库目录")
        report = self._run_scan_safely()
        if report is None:
            return Response(success=False, message="扫描失败或已有扫描在进行中，请查看日志")
        return Response(success=True, message="扫描完成", data=self._report_to_dict(report))

    def api_report(self) -> Response[dict]:
        """返回上次扫描报告（没有则返回空结构，不算失败）。"""
        report = self._load_report()
        if report is None:
            return Response(success=True, data={
                "scan_time": "",
                "scanned_files": 0,
                "orphans": [],
                "kept_count": 0,
                "orphan_bytes": 0,
                "errors": [],
            })
        return Response(success=True, data=self._report_to_dict(report))

    def api_delete(self, payload: Dict[str, Any] = Body(default=None)) -> Response[dict]:
        """
        删除选中的残留文件。

        只接受「本次扫描报告里判定为残留」的路径，其余一律拒绝——
        即使前端传来的路径被篡改，执行层也会拦住。
        """
        if not self.get_state():
            return Response(success=False, message="插件未启用")
        paths = (payload or {}).get("paths") or []
        if not isinstance(paths, list) or not paths:
            return Response(success=False, message="没有选择要删除的文件")
        # 防止一次请求传入过多路径导致长时间占用
        if len(paths) > 5000:
            return Response(success=False, message="单次最多处理 5000 个文件")
        if not self._lock.acquire(blocking=False):
            return Response(success=False, message="正在执行其它操作，请稍后再试")
        try:
            result = self.perform_delete([str(path) for path in paths])
        finally:
            self._lock.release()
        return Response(
            success=not result.failed,
            message=(
                f"处理完成：删除 {len(result.deleted)} 个，回收 {len(result.recycled)} 个，"
                f"跳过 {len(result.skipped)} 个，失败 {len(result.failed)} 个"
            ),
            data={
                "deleted": len(result.deleted),
                "recycled": len(result.recycled),
                "skipped": [{"path": path, "reason": reason} for path, reason in result.skipped[:200]],
                "failed": [{"path": path, "reason": reason} for path, reason in result.failed[:200]],
                "removed_dirs": result.removed_dirs[:200],
            },
        )

    def api_history(self) -> Response[list]:
        """返回清理历史。"""
        try:
            history = self.get_data(HISTORY_DATA_KEY) or []
        except Exception as error:
            logger.error(f"刮削残留清理：读取历史失败 - {error}")
            history = []
        return Response(success=True, data=history if isinstance(history, list) else [])

    def api_export(self, fmt: str = "csv"):
        """
        导出上次扫描报告。

        这是**文件下载**端点，所以不返回 JSON envelope——直接回一个
        `Response` 带上正确的 Content-Type 与 `Content-Disposition`，
        浏览器点链接就会下载。

        CSV 用 UTF-8 BOM 编码，中文版 Excel 双击打开不会乱码。
        """
        if not self.get_state():
            return _plain_response(content="插件未启用", status_code=400, media_type="text/plain")
        report = self._load_report()
        if report is None:
            return _plain_response(
                content="还没有扫描报告，请先执行扫描", status_code=404, media_type="text/plain"
            )
        try:
            exported = export_report_file(report, fmt=fmt, orphans_only=True)
        except Exception as error:
            logger.error(f"刮削残留清理：导出报告失败 - {error}")
            return _plain_response(
                content=f"导出失败：{error}", status_code=500, media_type="text/plain"
            )
        return _file_response(
            content=exported.content,
            media_type=exported.mime,
            headers={
                # filename*= 让中文/特殊字符文件名也能正确编码
                "Content-Disposition": f"attachment; filename=\"{exported.filename}\"",
            },
        )

    def api_rules(self) -> Response[dict]:
        """
        返回实际生效的多库策略（含解析警告），方便用户确认配置有没有写对。
        """
        parsed = parse_library_rules(self._library_rules_text)
        return Response(
            success=True,
            data={
                "rules": describe_rules(parsed.rules),
                "warnings": parsed.warnings,
                "global": {
                    "delete_mode": self._delete_mode,
                    "recycle_dir": self._recycle_dir,
                    "keep_days": self._keep_days,
                    "min_age_minutes": self._min_age_minutes,
                    "skip_in_use": self._skip_in_use,
                },
            },
        )

    def _report_to_dict(self, report: ScanReport) -> Dict[str, Any]:
        """把扫描报告转成前端易用的结构。"""
        orphans = report.orphans
        # 按「所在媒体目录」聚合，方便用户成组勾选
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for entry in orphans:
            directory = os.path.dirname(entry.path)
            grouped.setdefault(directory, []).append({
                "path": entry.path,
                "name": os.path.basename(entry.path),
                "size": entry.size,
                "reason": entry.reason.value,
                "detail": entry.detail,
                "dir_shared": entry.dir_shared,
            })
        return {
            "scan_time": report.scan_time,
            "scanned_files": report.scanned_files,
            "orphan_count": len(orphans),
            "orphan_bytes": report.orphan_bytes,
            "kept_count": len(report.entries) - len(orphans),
            "mode": "recycle" if self._delete_mode == DeleteMode.RECYCLE else "remove",
            "groups": [
                {
                    "dir": directory,
                    "count": len(items),
                    "bytes": sum(item["size"] for item in items),
                    "items": items,
                }
                for directory, items in sorted(grouped.items())
            ],
            "errors": [{"path": path, "error": reason} for path, reason in report.errors[:100]],
        }

    # -----------------------------------------------------------
    # 命令
    # -----------------------------------------------------------
    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程命令，可通过聊天消息触发扫描。"""
        return [
            {
                "cmd": "/scrapecleaner_scan",
                "event": EventType.PluginAction,
                "desc": "刮削残留清理：立即扫描",
                "category": "插件命令",
                "data": {"action": "scrapecleaner_scan"},
            }
        ]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event) -> None:
        """处理远程命令，只响应属于本插件的 action。"""
        event_data = event.event_data or {}
        if event_data.get("action") != "scrapecleaner_scan":
            return
        if not self.get_state():
            self.post_message(
                mtype=MessageType.Plugin,
                title="刮削残留清理",
                text="插件未启用，无法扫描。",
            )
            return
        report = self._run_scan_safely()
        if report is None:
            self.post_message(
                mtype=MessageType.Plugin,
                title="刮削残留清理",
                text="扫描失败，请查看日志。",
            )
            return
        self.post_message(
            mtype=MessageType.Plugin,
            title="🧹 刮削残留扫描完成",
            text=(
                f"扫描 {report.scanned_files} 个文件，"
                f"发现 {len(report.orphans)} 个残留刮削文件"
                f"（{_human_size(report.orphan_bytes)}）。\n"
                f"请到插件页面确认后清理。"
            ),
        )


    # -----------------------------------------------------------
    # 配置页面
    # -----------------------------------------------------------
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        返回配置页面（Vuetify JSON 模式）与默认模型。

        页面结构分成四块：总览说明 / 媒体库与排除 / 删除策略 / 扫描计划。
        """
        return [
            {
                "component": "VForm",
                "content": [
                    # ---------- 总览 ----------
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "title": "🧹 刮削残留清理（扫描对账式）",
                                    "text": (
                                        "扫描媒体库，找出「已经没有对应视频」的刮削文件"
                                        "（.nfo / 图片 / 字幕），先出报告，人工确认后再删。"
                                        "与官方「源文件联动清理」不同，本插件不依赖删除事件，"
                                        "能把已经积累的存量残留一次扫出来。"
                                    ),
                                },
                            }],
                        }],
                    },
                    # ---------- 开关 ----------
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "notify", "label": "扫描/清理后发送通知"},
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "remove_empty_dirs",
                                        "label": "删除后清理空目录",
                                    },
                                }],
                            },
                        ],
                    },
                    # ---------- 媒体库目录 ----------
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "title": "📁 媒体库目录",
                                    "text": (
                                        "填写媒体库的根目录，每行一个（容器内路径）。"
                                        "建议直接填电影库和剧集库的根目录，插件会递归扫描。"
                                    ),
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextarea",
                                    "props": {
                                        "model": "library_dirs",
                                        "label": "媒体库目录",
                                        "rows": 4,
                                        "placeholder": "/media/电影\n/media/剧集",
                                        "hint": "每行一个绝对路径",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextarea",
                                    "props": {
                                        "model": "exclude_keywords",
                                        "label": "排除关键字",
                                        "rows": 4,
                                        "hint": "路径包含任一行关键字则跳过，每行一个",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    # ---------- 文件类型 ----------
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "title": "🏷️ 文件类型",
                                    "text": (
                                        "插件内置了常见刮削后缀（nfo/xml/jpg/png/srt/ass 等），"
                                        "以及受保护模式（*.trickplay、theme.* 等）。"
                                        "一般不需要修改，只有用了特殊命名规范时再补充。"
                                    ),
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "extra_scrap_exts",
                                        "label": "额外刮削后缀",
                                        "placeholder": ".我的后缀,.另一个",
                                        "hint": "逗号或换行分隔，会追加到内置后缀",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "extra_protected",
                                        "label": "额外受保护模式",
                                        "placeholder": "*.我的标记",
                                        "hint": "通配符匹配文件名或父目录名，命中的文件永不清理",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    # ---------- 删除策略 ----------
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "warning",
                                    "variant": "tonal",
                                    "title": "🗑️ 删除策略",
                                    "text": (
                                        "推荐使用「移入回收站」，删错了还能找回。"
                                        "回收目录必须位于媒体库之外，否则会被下一次扫描再次判定。"
                                    ),
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "delete_mode",
                                        "label": "删除方式",
                                        "items": [
                                            {"title": "移入回收站（推荐）", "value": "recycle"},
                                            {"title": "直接删除", "value": "remove"},
                                        ],
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "recycle_dir",
                                        "label": "回收目录",
                                        "hint": "留空则用插件数据目录下的 recycle",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "keep_days",
                                        "label": "回收站保留天数",
                                        "type": "number",
                                        "hint": "0 表示永久保留",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    # ---------- 安全与保护 ----------
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "title": "🛡️ 安全保护",
                                    "text": (
                                        "「新文件保护期」用来避免误删刚整理、刮削文件刚写入但视频还没就位的文件。"
                                        "建议保持默认的 1 天——库里已有的老文件不受影响。"
                                    ),
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "min_age_minutes",
                                        "label": "新文件保护期（分钟）",
                                        "type": "number",
                                        "hint": "修改时间在此之内的文件不清理。0 = 关闭",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "skip_in_use",
                                        "label": "跳过被占用文件",
                                        "hide-details": False,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "clean_history",
                                        "label": "联动清理转移记录",
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "warning",
                                    "variant": "tonal",
                                    "density": "compact",
                                    "text": (
                                        "「跳过被占用文件」在 Windows 上有效（能识别 qBittorrent 做种/校验、"
                                        "播放器读取等占用）；Linux/NAS 上由于系统限制基本无效。"
                                        "如果在 NAS 上出现「全部文件都被跳过」，请关闭本项。"
                                    ),
                                },
                            }],
                        }],
                    },
                    # ---------- 自定义规则 ----------
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "title": "⚙️ 自定义规则（正则，一般不用填）",
                                    "text": (
                                        "用于小众命名规范。填错不会让插件崩溃——"
                                        "写错的正则会记进扫描报告的「错误」里并被忽略。"
                                    ),
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "extra_media_exts",
                                        "label": "额外媒体后缀",
                                        "placeholder": ".rmvb7,.ts",
                                        "hint": "额外视为「视频」的后缀",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "media_patterns",
                                        "label": "媒体文件名正则",
                                        "placeholder": r"\.m2ts$",
                                        "hint": "正则匹配到的文件视为媒体",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "scrap_patterns",
                                        "label": "刮削文件名正则",
                                        "placeholder": r"\.myext$",
                                        "hint": "正则匹配到的文件视为刮削文件",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "dir_shared_patterns",
                                        "label": "目录级文件名正则",
                                        "placeholder": r"^MyStudio\.Artwork$",
                                        "hint": "匹配文件名主干，视为整个目录的共享文件",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    # ---------- 多库策略 ----------
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "title": "📚 多媒体库分别配置策略（高级）",
                                    "text": (
                                        "不同库用不同策略。每行一条，用 | 分段："
                                        "「路径 | 键=值 | 键=值」。"
                                        "可用键：mode(recycle/remove)、recycle_dir、keep_days、"
                                        "disabled、min_age_minutes、exclude。"
                                        "最长路径前缀优先，没命中的走全局设置。"
                                    ),
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VTextarea",
                                "props": {
                                    "model": "library_rules_text",
                                    "label": "多库策略",
                                    "rows": 5,
                                    "placeholder": (
                                        "/media/movies | mode=recycle | keep_days=60\n"
                                        "/media/tv | mode=remove\n"
                                        "/media/readonly | disabled=true"
                                    ),
                                    "hint": "以 # 开头的行为注释；写错的键/值会被忽略并在日志里提示",
                                    "persistent-hint": True,
                                },
                            }],
                        }],
                    },
                    # ---------- 扫描计划 ----------
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "title": "⏰ 扫描计划",
                                    "text": (
                                        "定时扫描只做扫描和通知，不会自动删除——"
                                        "删除必须由你在插件详情页确认。留空则只在手动触发时扫描。"
                                    ),
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "scan_cron",
                                        "label": "定时扫描（cron）",
                                        "placeholder": "0 4 * * *",
                                        "hint": "5 段 cron，如 0 4 * * * 表示每天 4 点",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "max_files",
                                        "label": "单次扫描文件上限",
                                        "type": "number",
                                        "hint": "防止误配超大目录拖垮宿主",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                        ],
                    },
                    # ---------- 精细清理 ----------
                    # 这一段是「勾选删除」的替代方案：get_page() 没有 model，
                    # 做不了复选框；而 get_form() 的 model 是真双向绑定，
                    # 所以让用户在这里编辑待删清单，等价于逐条反选。
                    {
                        "component": "VRow",
                        "content": [{
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [{
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "title": "✂️ 精细清理（可编辑清单）",
                                    "text": (
                                        "这里用来「挑着删」。点「载入上次扫描的残留清单」把路径灌进下面"
                                        "的文本框，删掉你不想动的那几行，再点「执行清理」。"
                                        "只有本次扫描确实判定为残留的路径才会被真正删除，"
                                        "手工改出来的路径会被执行层拒绝。"
                                    ),
                                },
                            }],
                        }],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [{
                                    "component": "VTextarea",
                                    "props": {
                                        "model": "pending_paths",
                                        "label": "待清理路径（每行一个）",
                                        "rows": 8,
                                        "hint": "只删你想删的行，其余保持原样",
                                        "persistent-hint": True,
                                    },
                                }],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VBtn",
                                        "props": {
                                            "variant": "tonal",
                                            "prepend-icon": "mdi-format-list-bulleted",
                                            "block": True,
                                            "class": "mb-2",
                                        },
                                        "text": "载入上次扫描的残留清单",
                                        "events": {"click": {
                                            "api": f"plugin/{self.__class__.__name__}/report",
                                            "method": "get",
                                            "params": {},
                                        }},
                                    },
                                    {
                                        "component": "VBtn",
                                        "props": {
                                            "variant": "tonal",
                                            "color": "error",
                                            "prepend-icon": "mdi-delete",
                                            "block": True,
                                        },
                                        "text": "执行清理",
                                        # onclick 在 get_form() 里是可执行 JS，
                                        # 且 `with(model)` 注入了表单状态
                                        "onclick": (
                                            "function(e) {"
                                            " var raw = (model.pending_paths || '');"
                                            " var paths = raw.split('\\n').map(function(s){return s.trim();})"
                                            "   .filter(function(s){return s.length > 0;});"
                                            " if (!paths.length) { alert('清单为空，先把要删的路径粘到上面'); return; }"
                                            " if (!confirm('即将处理 ' + paths.length + ' 个路径，确认继续？')) { return; }"
                                            " window.MoviePilotAPI.post('plugin/" + self.__class__.__name__ + "/delete',"
                                            "   { paths: paths })"
                                            "  .then(function(r) {"
                                            "    var msg = r && r.message ? r.message : (r ? '处理完成' : '无响应');"
                                            "    alert(msg);"
                                            "    if (r && r.data && (r.data.skipped || []).length) {"
                                            "      console.warn('被跳过的项', r.data.skipped);"
                                            "    }"
                                            "  })"
                                            "  .catch(function(err) { console.error(err); alert('执行失败，详见控制台'); });"
                                            "}"
                                        ),
                                    },
                                ],
                            },
                        ],
                    },
                ],
            }
        ], self._default_config()

    # -----------------------------------------------------------
    # 详情页面
    # -----------------------------------------------------------
    def _action_button(
        self,
        text: str,
        icon: str,
        path: str,
        params: Optional[dict] = None,
        color: str = "",
        block: bool = False,
    ) -> dict:
        """
        构造一个「点击即调用插件 API」的按钮。

        这是 `get_page()` 里唯一支持的交互方式：`PageRender.vue` 只认
        `config.events`，且每个事件体固定是
        `commonAction(api, method, params)`，不接受 JS 表达式、也没有
        `model` 上下文。所以**行相关的参数必须在 Python 端算好写进 params**，
        前端拿不到「当前行」。

        `method` 为 `"post"` 时 `params` 原样作为 JSON body 发送，
        正好对上后端 `payload: dict = Body(...)`。
        """
        event: Dict[str, Any] = {
            "api": f"plugin/{self.__class__.__name__}{path}",
            "method": "post",
        }
        if params is not None:
            event["params"] = params
        props: Dict[str, Any] = {
            "variant": "tonal",
            "prepend-icon": icon,
            "block": block,
        }
        if color:
            props["color"] = color
        return {"component": "VBtn", "props": props, "text": text, "events": {"click": event}}

    def get_page(self) -> Optional[List[dict]]:
        """
        返回详情页面：展示最近一次扫描结果，并提供分组/整体清理按钮。

        设计说明（很重要）
        ------------------
        MoviePilot V3 的 Vuetify 页面有**两个独立渲染器**：

        - `get_form()` → `FormRender.vue`：有 `model`，支持 v-model 与真 JS；
        - `get_page()` → `PageRender.vue`：**没有 `model`**，只能靠
          `events.click` 触发固定的 API 调用，且每次调用后整页重绘。

        所以在 `get_page()` 里做「复选框勾选多选」是不可能的：`props.model`
        不会生效，勾选状态还会在重绘后丢失。这里改用
        「每组一个删除按钮 + 一个全部删除按钮」，参数全部在 Python 端算好；
        需要精细挑选时，去配置页用 `pending_paths` 文本域编辑（那里 `model`
        是真的双向绑定）。
        """
        if not self.get_state():
            return [{
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "text": "插件未启用，请先在配置页开启。",
                },
            }]

        if not self.library_dirs():
            return [{
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "text": "尚未配置媒体库目录，请先在配置页填写。",
                },
            }]

        mode_label = "移入回收站" if self._delete_mode == DeleteMode.RECYCLE else "直接删除"
        widgets: List[dict] = [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "class": "mb-4",
                    "title": "🧹 刮削残留清理",
                    "text": (
                        f"当前删除方式：{mode_label}。"
                        "定时扫描只出报告不删除，删除必须在这里点按钮确认。"
                    ),
                },
            },
        ]

        report = self._load_report()
        if report is None:
            widgets.append({
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "title": "还没有扫描结果",
                    "text": "点下面的「立即扫描」开始，或配置定时扫描。",
                },
            })
            widgets.append(self._action_button(
                "立即扫描", "mdi-magnify", "/scan", {"action": "scan"}, color="primary", block=True,
            ))
            return widgets

        orphans = report.orphans
        kept = len(report.entries) - len(orphans)

        widgets.append({
            "component": "VAlert",
            "props": {
                "type": "success" if not orphans else "warning",
                "variant": "tonal",
                "class": "mb-4",
                "title": f"上次扫描：{report.scan_time or '未知时间'}",
                "text": (
                    f"共扫描 {report.scanned_files} 个文件，"
                    f"发现 {len(orphans)} 个残留刮削文件"
                    f"（{_human_size(report.orphan_bytes)}），"
                    f"保留 {kept} 个正常刮削文件。"
                ),
            },
        })

        # ---------- 操作按钮区 ----------
        buttons: List[dict] = [
            self._action_button("立即扫描", "mdi-magnify", "/scan", {"action": "scan"}),
        ]
        if orphans:
            all_paths = [entry.path for entry in orphans]
            buttons.append(self._action_button(
                f"清理全部 {len(orphans)} 个残留",
                "mdi-delete-sweep",
                "/delete",
                {"paths": all_paths},
                color="error",
            ))
        widgets.append({
            "component": "div",
            "props": {"class": "d-flex flex-wrap ga-2 mb-4"},
            "content": buttons,
        })

        # ---------- 按目录分组明细 ----------
        grouped: Dict[str, List[ScrapEntry]] = {}
        for entry in orphans:
            grouped.setdefault(os.path.dirname(entry.path), []).append(entry)

        if grouped:
            # 用 VDataTable 展示明细（V3 里 headers/items 是有效的，
            # 只有 VTable 需要手写 thead/tbody，两者别搞混）
            detail_rows: List[dict] = []
            for entry in orphans[:500]:
                relative_dir = os.path.dirname(entry.path)
                detail_rows.append({
                    "name": os.path.basename(entry.path),
                    "dir": relative_dir,
                    "reason": _REASON_LABELS.get(entry.reason.value, entry.reason.value),
                    "size": _human_size(entry.size),
                })
            widgets.append({
                "component": "VDataTable",
                "props": {
                    "density": "compact",
                    "class": "mb-4",
                    "headers": [
                        {"title": "文件", "key": "name"},
                        {"title": "所在目录", "key": "dir"},
                        {"title": "判定原因", "key": "reason"},
                        {"title": "大小", "key": "size"},
                    ],
                    "items": detail_rows,
                    "items-per-page": 15,
                },
            })
            if len(orphans) > 500:
                widgets.append({
                    "component": "VAlert",
                    "props": {
                        "type": "info",
                        "variant": "tonal",
                        "class": "mb-4",
                        "text": f"明细仅显示前 500 条，共 {len(orphans)} 条。",
                    },
                })

            # 每个目录一个「清理本组」按钮，最多列出残留最多的 30 个目录
            group_buttons: List[dict] = []
            for directory, items in sorted(
                grouped.items(), key=lambda kv: len(kv[1]), reverse=True
            )[:30]:
                group_buttons.append(self._action_button(
                    f"清理本组 {len(items)} 个 · {os.path.basename(directory) or directory}",
                    "mdi-folder-remove",
                    "/delete",
                    {"paths": [entry.path for entry in items]},
                ))
            widgets.append({"component": "h3", "props": {"class": "mt-4 mb-2"}, "text": "按目录清理"})
            widgets.append({
                "component": "div",
                "props": {"class": "d-flex flex-column ga-2 mb-4"},
                "content": group_buttons,
            })
        else:
            widgets.append({
                "component": "VAlert",
                "props": {
                    "type": "success",
                    "variant": "tonal",
                    "text": "✅ 没有发现残留刮削文件，媒体库很干净。",
                },
            })

        # ---------- 清理历史 ----------
        try:
            history = self.get_data(HISTORY_DATA_KEY) or []
        except Exception:
            history = []
        if history:
            widgets.append({"component": "h3", "props": {"class": "mt-4 mb-2"}, "text": "最近清理记录"})
            widgets.append({
                "component": "VDataTable",
                "props": {
                    "density": "compact",
                    "headers": [
                        {"title": "时间", "key": "time"},
                        {"title": "方式", "key": "mode"},
                        {"title": "处理", "key": "done"},
                        {"title": "跳过", "key": "skipped"},
                        {"title": "清空目录", "key": "dirs"},
                    ],
                    "items": [
                        {
                            "time": item.get("time", ""),
                            "mode": "回收站" if item.get("mode") == DeleteMode.RECYCLE else "直接删除",
                            "done": int(item.get("deleted", 0)) + int(item.get("recycled", 0)),
                            "skipped": int(item.get("skipped", 0)),
                            "dirs": int(item.get("removed_dirs", 0)),
                        }
                        for item in history[:10]
                    ],
                    "items-per-page": 10,
                },
            })

        return widgets


def _to_ext_set(items: Tuple[str, ...]) -> frozenset[str]:
    """
    把用户输入的后缀列表规范化成「带点、小写」的集合。

    用户常写成 `nfo` 而不是 `.nfo`，两种都接受；空项与重复项自动去掉。
    """
    result = set()
    for item in items:
        cleaned = item.strip().lower()
        if not cleaned:
            continue
        if not cleaned.startswith("."):
            cleaned = f".{cleaned}"
        result.add(cleaned)
    return frozenset(result)


def _under_any_root(path: str, roots: List[str]) -> bool:
    """判断路径是否位于任一给定根目录之下（用于给出更友好的跳过原因）。"""
    try:
        target = os.path.abspath(os.path.normpath(path))
    except (OSError, ValueError):
        return False
    for root in roots:
        try:
            base = os.path.abspath(os.path.normpath(root))
            if os.path.commonpath([target, base]) == base:
                return True
        except (OSError, ValueError):
            # 不同驱动器时 commonpath 抛 ValueError，视为不在其下
            continue
    return False


def _human_size(size: int) -> str:
    """把字节数转成人类可读的大小。"""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"
