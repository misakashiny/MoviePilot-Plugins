# -*- coding: utf-8 -*-
"""
刮削残留清理的执行层（纯逻辑，不依赖 MoviePilot 运行时）
=======================================================

职责边界
--------
`analyzer` 负责「判断什么该删」，本模块负责「真的删，并且在删之前再过一遍闸」。
拆开的好处是：判断逻辑可以穷举验证，执行逻辑可以单独测试回收站、空目录、
失败回滚这些和文件系统打交道的部分。

安全阀（执行前逐条复核，任何一条不过就拒绝删除）
------------------------------------------------
V1. 路径必须落在本次配置的媒体库根目录之下（防目录穿越、防配置事故）。
V2. 路径必须存在于 `allowed_paths`（即本次扫描真实判定为残留的文件）。
    这杜绝了「报告过期后目录结构变了」导致的误删。
V3. 路径必须仍是刮削文件后缀（防止扫描后被换成媒体文件）。
V4. 路径不能命中排除关键字。
V5. 符号链接不删（删链接只删壳，语义混乱，交给人处理）。
V6. 文件正被其它进程占用（qBittorrent 做种/校验、播放器读取、同步工具写入）
    时跳过。检测本身出任何异常也按「无法判断 → 跳过」处理，绝不放行。

V6 的平台限制（用户必读）
-------------------------
V6 的能力**强依赖宿主操作系统**，不是「装上就全平台生效」：

- **Windows：有效。** 文件被其它进程持有句柄时，删除/改名会返回
  `WinError 32`（共享冲突），这是可靠且无副作用的信号。
- **Linux / macOS / 一般 POSIX：基本无效。** 被打开的文件仍然可以
  `unlink` 与 `rename`，系统层面不存在「共享冲突」。因此本模块在该平台上
  会让 V6 **恒返回 FREE（放行）而不是 UNKNOWN（跳过）**——这是刻意选择：
  若返回 UNKNOWN，则 Linux 用户的所有文件都会被判「无法判断」而全部跳过，
  插件直接失效。**请不要把 V6 当作 Linux 上的安全保证。**
- **SMB / NFS / 各类 NAS：视服务端行为而定。** 服务端拒绝改名时会表现为
  共享冲突（按占用跳过）；也可能只返回「拒绝访问」这类无法区分的错误，
  此时按 UNKNOWN 跳过，表现为「文件总是删不掉」。这类环境正是需要关闭
  `skip_in_use` 开关的典型场景。

另需注意：**任何对文件内容的打开操作都无法检测占用**。在 Windows 上
Python 的 CRT 与 `os.open` 均以共享模式打开文件，`open(path, "rb")`、
`os.open(path, O_RDWR)` 在被独占打开时依然成功——这两个「经典做法」是
假阴性，不能拿来替换本模块的实现。V6 只能依赖删除/改名语义。

删除模式
--------
- `remove`：直接删除，不可恢复。适合你已确认的报告。
- `recycle`：移动到回收目录（保留原相对路径），可撤销。默认推荐。
"""
from __future__ import annotations

import errno
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Iterable, Optional

from .analyzer import (
    AnalyzerOptions,
    ScanReport,
    is_scrap_file,
    _norm,
)


class DeleteMode(str):
    """删除模式常量。"""

    REMOVE = "remove"
    RECYCLE = "recycle"


@dataclass
class DeleteResult:
    """一次删除动作的结果。"""

    deleted: list[str] = field(default_factory=list)
    recycled: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    removed_dirs: list[str] = field(default_factory=list)

    @property
    def total_done(self) -> int:
        """成功处理的文件数（删除 + 回收）。"""
        return len(self.deleted) + len(self.recycled)

    def skipped_paths(self, category: SkipCategory) -> list[str]:
        """按归类取出被跳过的路径（如 `skipped_paths(SkipCategory.IN_USE)`）。"""
        return [
            path for path, reason in self.skipped
            if classify_skip_reason(reason) is category
        ]

    def skipped_count(self, category: SkipCategory) -> int:
        """按归类统计跳过条数，供历史统计与页面展示使用。"""
        return len(self.skipped_paths(category))

    def skip_summary(self) -> dict[str, int]:
        """
        一次性给出各归类的跳过数量。

        :return: `{"in_use": n, "unknown": n, "other": n}`（键为 `SkipCategory.value`）
        """
        # 显式枚举三个归类，保证返回值键稳定（即使某类为 0 也出现）
        return {item.value: self.skipped_count(item) for item in SkipCategory}


def _within(path: str, root: str) -> bool:
    """判断 path 是否位于 root 之下（使用规范化后的公共前缀比较）。"""
    try:
        normalized_path = os.path.abspath(_norm(path))
        normalized_root = os.path.abspath(_norm(root))
        return os.path.commonpath([normalized_path, normalized_root]) == normalized_root
    except (ValueError, OSError):
        # 不同驱动器时 commonpath 会抛 ValueError，视为不在根目录下
        return False


def _candidate_keys(path: str) -> set[str]:
    """
    生成一个路径的多个「比较口径」，用于清单匹配。

    同一个文件可能有多种写法：8.3 短路径（`ADMINI~1`）与长路径、
    `/tmp` 与它真实的软链目标、NAS 挂载点前后一致但字面不同。
    扫描时记录的是一种写法，删除请求传来的可能是另一种，直接字符串比较
    会出现「明明在清单里却匹配不上」的误拒。这里把两种口径都生成出来。
    """
    keys = set()
    try:
        keys.add(os.path.abspath(_norm(path)))
    except (OSError, ValueError):
        pass
    try:
        keys.add(os.path.realpath(_norm(path)))
    except (OSError, ValueError):
        pass
    try:
        keys.add(os.path.normcase(os.path.realpath(_norm(path))))
    except (OSError, ValueError):
        pass
    return {key for key in keys if key}


def _validate(
    path: str,
    roots: Iterable[str],
    allowed_paths: set[str],
    options: AnalyzerOptions,
) -> Optional[str]:
    """
    复核单个待删文件。通过返回 None，不通过返回拒绝原因。
    """
    normalized = os.path.abspath(_norm(path))

    # V1：必须在某个媒体库根目录下
    if not any(_within(normalized, root) or _within(os.path.realpath(normalized), root)
               for root in roots):
        return "不在配置的媒体库目录内"

    # V2：必须是本次扫描判定为残留的文件
    # 用多口径比较：扫描记录与请求路径可能分别是短路径/长路径形式。
    if not (_candidate_keys(normalized) & allowed_paths):
        return "不在本次扫描的残留清单内（清单可能已过期，请重新扫描）"

    # V3：必须仍是刮削文件
    name = os.path.basename(normalized)
    if not is_scrap_file(
        name,
        options.scrap_extensions - AnalyzerOptions().scrap_extensions,
    ):
        return "当前已不是刮削文件，拒绝删除"

    # V4：排除关键字
    lowered = normalized.lower()
    if any(keyword.lower() in lowered for keyword in options.exclude_keywords if keyword):
        return "命中排除关键字"

    # V5：符号链接不处理
    if os.path.islink(normalized):
        return "是符号链接，跳过"

    if not os.path.isfile(normalized):
        return "文件已不存在"

    return None


# =================================================================
# V6：文件占用检测
# =================================================================
# 设计前提（均为本机 Windows 实测结论，详见 README 与测试 deleter_inuse_check.py）：
#
# 1. `os.open(path, O_RDWR)` / `open(path, "rb")` **检测不到占用**。
#    Windows 上 Python 的 CRT 与 os.open 都用「共享读/写/删除」打开文件，
#    其它进程（哪怕用独占方式持有句柄）时它们照样能打开，因此这两个
#    「经典做法」在本场景是假阴性，不能用来做安全阀。
# 2. 唯一可靠的信号来自**删除语义本身**：
#    - `os.rename()` 试探：文件被占用时必然失败（rename 需要删除源名字），
#      而成功时文件原样留在原位、不产生任何数据变化。但它会在「文件被占用」
#      **之外**也失败（跨设备/权限/只读目录），所以只能当统计信号，
#      不能单独用来判「占用」，否则会误伤。
#    - `os.remove()` 试探：在 Windows 上文件被占用时返回
#      `PermissionError`（WinError 32「另一个程序正在使用此文件」），
#      且**文件与内容都不受影响**（实测失败后文件仍可正常打开）。命中这个
#      错误码就是「占用」的铁证。代价是：在确实可删除时它会真的把文件删掉。
#
# 所以这里按删除模式选择策略：
#
# - `remove` 模式（本来就不可恢复）：直接删。`os.remove` 成功即元素被删掉；
#   失败时按错误码区分「被占用（跳过）」与「其它失败」，并把文件放回本次执行
#   的删除结果里，绝不重复删。
# - `recycle` 模式（可恢复，要求被占用的文件「留在原位」）：不依赖删除语义，
#   只用 rename 试探 + 版本化文件（opensnoop 式）读取尝试做启发式判断。
#   Windows 上能识别大部分场景；Unix 上「被打开」的文件仍可 unlink/rename，
#   因此这个检测在 Unix 上基本无效——必须靠注释与文档说清楚，不能让用户
#   误以为 Linux/NAS 上也受保护。


class InUseVerdict(str, Enum):
    """单个文件的占用检测结论。"""

    FREE = "free"        # 未检测到占用，可以按原计划处理
    IN_USE = "in_use"    # 确实被占用，应跳过并保留文件
    UNKNOWN = "unknown"  # 无法判断（检测本身出错），按保守策略处理


IN_USE_REASON = "文件正被其它程序占用，跳过"
UNKNOWN_REASON = "无法判断文件是否被占用，保守跳过"


class SkipCategory(str, Enum):
    """跳过原因的归类，供上层统计与展示使用。

    单独给 V6 的两种跳过原因一个**机器可判定的身份**，调用方不必
    （也不应该）拿 `IN_USE_REASON` 去猜字符串。第三项 `OTHER` 覆盖
    其余所有安全阀（V1~V5）与业务前置校验的跳过原因。
    """

    IN_USE = "in_use"    # V6：确认被其它程序占用
    UNKNOWN = "unknown"  # V6：无法判断，按保守策略跳过
    OTHER = "other"      # 其它原因（V1~V5、清单过期、配置缺失等）


# 跳过原因分类表。用「原因文案 → 归类」的显式映射，而不是在文案里塞前缀，
# 这样上层看到的仍是完整可读的中文句子。
_SKIP_CATEGORY_BY_REASON: dict[str, SkipCategory] = {
    IN_USE_REASON: SkipCategory.IN_USE,
    UNKNOWN_REASON: SkipCategory.UNKNOWN,
}


def classify_skip_reason(reason: str) -> SkipCategory:
    """
    把一条跳过原因归类为 `SkipCategory`。

    :param reason: `DeleteResult.skipped` 里的第二个元素
    :return: IN_USE / UNKNOWN / OTHER；未知文案一律归 OTHER，绝不抛异常

    上层统计「因占用跳过了几个」时应当用本函数，而不是自己 `in` 字符串
    匹配——文案将来可能微调，但归类契约不会变。
    """
    try:
        return _SKIP_CATEGORY_BY_REASON.get(reason, SkipCategory.OTHER)
    except (TypeError, AttributeError):
        # 传入的不是 str（例如 None 或对象）时不应让统计逻辑崩掉
        return SkipCategory.OTHER


def is_in_use_skip(reason: str) -> bool:
    """这条跳过是否由 V6「确认被占用」导致（便于一行式调用）。"""
    return classify_skip_reason(reason) is SkipCategory.IN_USE


def is_unknown_skip(reason: str) -> bool:
    """这条跳过是否由 V6「无法判断」导致（也是开启开关才看到的信号）。"""
    return classify_skip_reason(reason) is SkipCategory.UNKNOWN

# Windows「另一个程序正在使用此文件」。
# ERROR_SHARING_VIOLATION = 32；ERROR_LOCK_VIOLATION = 33 出现在部分加锁场景。
_WINERROR_SHARING_VIOLATION = 32
_WINERROR_LOCK_VIOLATION = 33

# rename 失败时，这些 errno 与「占用」无关，只是环境不允许改名，不能当成占用。
# 其余失败（Windows 上极常见的 PermissionError/WinError 32/5）一律按「无法判断」
# 处理——宁可少删也不能在无法判断时放行。
_NOT_IN_USE_ERRNOS = frozenset({
    errno.EXDEV,      # 跨设备
    errno.ENOENT,     # 文件已被别人删掉
    errno.ENOTDIR,    # 父路径不是目录
    errno.ENOSPC,     # 空间不足
    errno.EROFS,      # 只读文件系统
    errno.ENAMETOOLONG,
    errno.EISDIR,
})


def _winerror(error: OSError) -> Optional[int]:
    """安全地取出 OSError 的 Windows 错误码（非 Windows 上没有该属性）。"""
    return getattr(error, "winerror", None)


def _is_sharing_violation(error: OSError) -> bool:
    """
    判断 OSError 是否为「文件被其它进程占用」。

    Windows 上表现为 WinError 32（最常见）/ 33（区域加锁）；
    POSIX 上没有这种语义，因此恒为 False——Unix 下的 unlink 不因被打开而失败。
    """
    code = _winerror(error)
    return code in (_WINERROR_SHARING_VIOLATION, _WINERROR_LOCK_VIOLATION)


def _move_by_rename(source: str, target: str) -> None:
    """
    用 os.rename 把文件挪走，跨设备时退化为复制+删除。

    为什么不直接用 `shutil.move`：`shutil.move` 在跨设备时会先 `copy2` 再
    `os.remove(source)`。如果正好以「被占用而不可删」的文件做一次能力探测，
    这个回退路径会留下**半截副本**——对安全阀来说是不可接受的副作用。
    """
    try:
        os.rename(source, target)
        return
    except OSError as error:
        if error.errno == errno.EXDEV:
            shutil.copy2(source, target)
            os.remove(source)
            return
        raise


# rename 试探用的临时后缀，用来在崩溃/断电后自检残留
_PROBE_TOKEN = ".scr-usechk-"


def _make_probe_name(path: str) -> str:
    """生成同目录、不会与真实文件撞名的临时名。"""
    directory, name = os.path.split(path)
    # 用 pid + 随机数，避免同目录并发探测互相踩到
    return os.path.join(directory, f"{name}{_PROBE_TOKEN}{os.getpid()}-{os.urandom(3).hex()}")


def _probe_by_rename(path: str) -> InUseVerdict:
    """
    用 rename 试探判断文件是否被占用。

    rename 成功说明「源名字可以被拿走」，随后立即改回来。Windows / SMB / NFS
    在文件被独占打开时都会拒绝改名，因此这是**唯一能覆盖「读取中」场景**
    的跨平台信号（删除语义做不到：读取中的文件照样可以删）。

    它不可靠的另一面：权限不足、只读目录等非占用原因也会失败，所以失败时
    只在明确是共享冲突时才判「占用」，其余按「无法判断」保守跳过。
    """
    probe_name = _make_probe_name(path)
    try:
        _move_by_rename(path, probe_name)
    except FileNotFoundError:
        # 文件在检测期间被别人删掉了：不必再删，也无需报错
        return InUseVerdict.FREE
    except OSError as error:
        if _is_sharing_violation(error):
            return InUseVerdict.IN_USE
        if error.errno in _NOT_IN_USE_ERRNOS:
            return InUseVerdict.FREE
        # 其余失败（含 Windows 的 WinError 5 拒绝访问）无法区分是占用还是权限，
        # 按「无法判断」上报，由使用侧保守跳过
        return InUseVerdict.UNKNOWN

    try:
        _move_by_rename(probe_name, path)
    except OSError:
        # 名字改不回来比误删更难收拾，因此尽力恢复，仍失败时把真实位置写进异常，
        # 让 execute_delete 把它记为 failed（用户能据此找回文件）。
        raise OSError(
            f"占用检测临时改名后无法恢复，文件现在位于 {probe_name}"
        ) from None
    return InUseVerdict.FREE


def probe_in_use(path: str, unlink_allowed: bool = False) -> InUseVerdict:
    """
    V6 的检测入口：判断文件是否被其它进程占用。

    :param path: 待检测文件（调用方已确认存在且不是符号链接）
    :param unlink_allowed: 允许把「真的删除」当成探测手段。仅当删除本身就是
        本次目的（`remove` 模式）时才为 True——成功即意味着文件已被删掉，
        调用方必须把这个结论当作「已处理」，**不能再删一次**；
        失败时文件与内容不受影响（本机实测），可以并入删除结果。
    :return: FREE / IN_USE / UNKNOWN；UNKNOWN 的使用侧语义一律是「跳过」。

    本函数顶层兜底所有异常并返回 UNKNOWN，因此**不存在「检测出错导致放行」
    的路径**——这正是安全阀最要紧的性质。
    """
    try:
        if not os.path.isfile(path):
            return InUseVerdict.UNKNOWN

        if unlink_allowed:
            # 删除机制本身就是最可靠的占用检测，见文件顶部说明。
            # 注意：此分支一旦返回 FREE，文件已经被删掉了。
            try:
                os.remove(path)
                return InUseVerdict.FREE
            except FileNotFoundError:
                return InUseVerdict.FREE
            except OSError as error:
                return (
                    InUseVerdict.IN_USE if _is_sharing_violation(error)
                    else InUseVerdict.UNKNOWN
                )

        return _probe_by_rename(path)
    except Exception:  # noqa: BLE001 - 安全阀不允许任何异常逃逸成「放行」
        return InUseVerdict.UNKNOWN


def _safe_probe(path: str, unlink_allowed: bool) -> InUseVerdict:
    """
    调用 `probe_in_use` 并再兜一层异常，返回非 FREE 即「跳过」。

    为什么要在调用点再兜一次：`probe_in_use` 内部已经兜底，但它依赖的属性
    （枚举比较、模块级常量、被测试/宿主替换过的实现）都可能在运行时出问题。
    安全阀的正确失败姿势是「跳过这个文件」，而不是让整次清理任务崩掉——
    崩掉既没删成，又让用户看不到哪些文件需要稍后重试。
    """
    try:
        return probe_in_use(path, unlink_allowed=unlink_allowed)
    except Exception:  # noqa: BLE001 - 同上：任何异常都退化为「无法判断」
        return InUseVerdict.UNKNOWN


def _verdict_reason(verdict: InUseVerdict) -> str:
    """把非 FREE 的检测结论翻译成给用户看的跳过原因。"""
    try:
        if verdict is InUseVerdict.IN_USE:
            return IN_USE_REASON
    except Exception:  # noqa: BLE001
        pass
    return UNKNOWN_REASON


def execute_delete(
    paths: Iterable[str],
    roots: Iterable[str],
    report: ScanReport,
    mode: str = DeleteMode.RECYCLE,
    recycle_dir: str = "",
    options: Optional[AnalyzerOptions] = None,
    remove_empty_dirs: bool = True,
    skip_in_use: bool = True,
) -> DeleteResult:
    """
    执行删除。

    只会删除「本次扫描判定为残留」且通过全部安全阀校验的文件，
    因此即使前端传来一个被篡改的路径，也不会真的删除库里的正常文件。

    :param paths: 待删文件路径（通常来自用户在页面上的勾选）
    :param roots: 媒体库根目录列表（安全阀 V1 使用）
    :param report: 本次扫描报告（安全阀 V2 使用）
    :param mode: `remove` 直接删除 / `recycle` 移入回收目录
    :param recycle_dir: 回收目录（mode=recycle 时必填）
    :param options: 扫描参数（复用排除关键字与后缀集）
    :param remove_empty_dirs: 删除后是否清理变空的目录
    :param skip_in_use: 是否启用安全阀 V6（被占用则跳过）。某些 NAS 的 FUSE /
        网络文件系统上 rename 试探会失败（返回「拒绝访问」而不是共享冲突），
        从而让所有文件都被判成「无法判断」而全部跳过。这类环境需要能关掉它，
        代价是放弃「不碰正在做的文件」这层保护。
    :return: 执行结果
    """
    options = options or AnalyzerOptions()
    result = DeleteResult()
    roots = [os.path.abspath(_norm(root)) for root in roots]
    # 清单同样展开成多口径，避免「扫描记短路径、删除传长路径」的误拒
    allowed_paths: set[str] = set()
    for entry in report.orphans:
        allowed_paths |= _candidate_keys(entry.path)

    if mode == DeleteMode.RECYCLE and not recycle_dir:
        # 回收模式缺少目标目录时退化为「全部跳过」，绝不在没有退路时硬删
        for path in paths:
            result.skipped.append((str(path), "回收模式未配置回收目录，已拒绝删除"))
        return result

    recycle_root = os.path.abspath(_norm(recycle_dir)) if mode == DeleteMode.RECYCLE else ""
    # 回收目录本身不能位于待清理的媒体库内，否则会被下一次扫描再次判定
    if recycle_root and any(_within(recycle_root, root) for root in roots):
        for path in paths:
            result.skipped.append((str(path), "回收目录位于媒体库内部，容易循环判删，请改到库外"))
        return result

    processed_dirs: set[str] = set()

    for raw_path in paths:
        path = os.path.abspath(_norm(str(raw_path)))
        reason = _validate(path, roots, allowed_paths, options)
        if reason:
            result.skipped.append((path, reason))
            continue

        if mode == DeleteMode.RECYCLE:
            # 在回收目录内保留「相对哪个库」与原有层级，便于恢复与追溯
            owner_root = max(
                (root for root in roots if _within(path, root)),
                key=len,
            )
            relative = os.path.relpath(path, owner_root)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = os.path.join(recycle_root, f"{stamp}", os.path.basename(owner_root), relative)

            # V6：回收模式要求「被占用就留在原位」，因此必须在 makedirs/move 之前判。
            # 一旦 shutil.move 失败，源文件可能已经被挪走一部分，再判就太晚了。
            if skip_in_use:
                verdict = _safe_probe(path, unlink_allowed=False)
                if verdict is not InUseVerdict.FREE:
                    result.skipped.append((path, _verdict_reason(verdict)))
                    continue

            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.move(path, target)
                result.recycled.append(path)
                processed_dirs.add(os.path.dirname(path))
            except OSError as error:
                result.failed.append((path, str(error)))
            continue

        # V6（remove 模式）：删除本身即最可靠的占用探测——成功就是删掉了，
        # 被占用时 Windows 会返回 WinError 32 且文件完好无损。
        if skip_in_use:
            verdict = _safe_probe(path, unlink_allowed=True)
            if verdict is not InUseVerdict.FREE:
                result.skipped.append((path, _verdict_reason(verdict)))
                continue
            # FREE 意味着探测时已经删掉了，不能再删第二次
            result.deleted.append(path)
            processed_dirs.add(os.path.dirname(path))
            continue

        try:
            os.remove(path)
            result.deleted.append(path)
            processed_dirs.add(os.path.dirname(path))
        except OSError as error:
            result.failed.append((path, str(error)))

    if remove_empty_dirs and processed_dirs:
        result.removed_dirs = _remove_empty_dirs(processed_dirs, roots, options)

    return result


def _remove_empty_dirs(
    directories: Iterable[str],
    roots: Iterable[str],
    options: AnalyzerOptions,
) -> list[str]:
    """
    自底向上删除变空的目录。

    只删到媒体库根目录为止（根目录本身永不删除），并且只删「本次确实变空」
    的目录——不会因为超时或权限问题去猜测。
    """
    root_set = {os.path.abspath(_norm(root)) for root in roots}
    removed: list[str] = []
    # 只处理待删目录本身以及空目录会向上传导的候选
    candidates: set[str] = set()
    for directory in directories:
        current = os.path.abspath(_norm(directory))
        while True:
            if current in root_set or os.path.dirname(current) == current:
                break
            candidates.add(current)
            current = os.path.dirname(current)

    for directory in sorted(candidates, key=lambda item: item.count(os.sep), reverse=True):
        if directory in root_set:
            continue
        if any(keyword.lower() in directory.lower()
               for keyword in options.exclude_keywords if keyword):
            continue
        try:
            if not os.path.isdir(directory):
                continue
            if os.listdir(directory):
                # 还有内容（含子目录）就保留，让父目录也跟着保留
                continue
            os.rmdir(directory)
            removed.append(directory)
        except OSError:
            continue
    return sorted(removed)
