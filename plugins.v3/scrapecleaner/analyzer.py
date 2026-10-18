# -*- coding: utf-8 -*-
"""
刮削残留文件的扫描对账引擎（纯逻辑，不依赖 MoviePilot 运行时）
===============================================================

设计目标
--------
本模块只做一件事：给定一个媒体库目录树，算出「哪些刮削文件已经没有主了」，
以及「为什么判断它没有主」。它**不删除任何东西**，也不联网、不读宿主数据库。

这样做的好处是核心判断逻辑可以脱离 MoviePilot 独立单元测试——清理类插件
最怕的就是删错文件，所以这部分必须能被穷举验证。

核心概念
--------
1. **媒体文件**（media file）：真正的内容本体，如 .mkv / .mp4。它是「主」。
2. **刮削文件**（scrap file）：元数据、图片、字幕等附属文件，如 .nfo / .jpg /
   .srt。它必须「有主」才应该保留。
3. **归属绑定**（binding）：刮削文件与媒体文件（或媒体目录）之间的关联规则。
   本引擎支持三档绑定强度：

   - `stem`：文件名主干完全匹配。`Show.S01E01.mkv` ↔ `Show.S01E01.nfo`、
     `Show.S01E01.chs.srt`、`Show.S01E01-thumb.jpg`。
   - `dir`：目录级共享文件，只跟「所在目录」绑定。如 `tvshow.nfo`、`poster.jpg`、
     `fanart.jpg`、`season.nfo`。它们代表整个目录，一个目录里有多媒体文件也只需
     要一份。
   - `compound`：复合文件名，如 `movie-zh.nfo`、`movie.chs.ass`。

4. **目录存活判定**（directory survival）：这是本引擎与常见实现最大的区别。
   一个目录级共享文件只有在**整个目录树都不再包含任何媒体文件**时才判定为残留。
   因此引擎会自底向上聚合「子树内媒体文件数」。

判定规则（按优先级）
--------------------
对每个刮削文件 S，依次判断：

R1. S 在 `protected_patterns` 里（如 `.trickplay` 目录、`theme.mp3`）→ 保留，理由 PROTECTED。
R2. S 是目录级共享文件 → 看所在目录的子树媒体文件数：
    - > 0 → 保留，理由 MEDIA_PRESENT
    - == 0 → 残留，理由 ORPHAN_DIR_SHARED
R3. S 是普通刮削文件 → 找同目录下的媒体文件，看是否有 stem/compound 匹配：
    - 有匹配 → 保留，理由 MEDIA_PRESENT
    - 无匹配 → 残留，理由 ORPHAN_STEM

R0. **残留降级（在 R1~R5 全部走完之后单独生效）**：只有「别的规则都判成残留」
    的文件才会经过这一步；它的作用是把残留改回保留，绝不会反过来把保留改成
    残留，因此不会破坏上面任何一条判断：
    - 文件修改时间距今不足 `min_age_seconds` → 保留，理由 RECENT。
      刚整理完 / 正在转移的目录里，视频可能还没落盘，此时按「没主」删掉是最
      致命的误删；拿不准就保留。
    - 命中的自定义正则非法等配置问题只降级为「忽略该条规则」并写进 report.errors，
      配置写错绝不能让整个扫描崩掉。

自定义正则（配合内置规则，只做扩充）
------------------------------------
- `media_patterns` / `scrap_patterns`：用 `re.search` 匹配**完整文件名**（含后缀），
  用于识别内置后缀表没覆盖的怪后缀或自定义命名。
- `dir_shared_patterns`：匹配**不含后缀的主干**，把用户自己的命名当作目录级共享文件。
  只做「加保护」：进这一档的文件按目录存活判定，而目录存活判定比 stem 匹配更宽松。

安全阀
------
除了上面的规则，引擎还会输出若干「保护信号」，供上层插件做二次确认：
- 目录内媒体文件数为 0 但目录内有子目录含媒体文件（说明是扁平命名，需人工判断）
- 刮削文件的 stem 与任何已知媒体都不匹配，但同目录存在本体文件（.iso / .m2ts 等
  未被识别为媒体的），可能误判
"""
from __future__ import annotations

import fnmatch
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional

# ---------------------------------------------------------------
# 文件类型常量
# ---------------------------------------------------------------

# 被视为「媒体本体」的后缀。命中即认为该文件是内容主体，其存在的目录视为存活。
MEDIA_EXTENSIONS: frozenset[str] = frozenset({
    ".mkv", ".mp4", ".avi", ".rmvb", ".rm", ".wmv", ".mov", ".m4v", ".flv",
    ".ts", ".m2ts", ".mts", ".mpg", ".mpeg", ".vob", ".iso", ".webm", ".3gp",
    ".asf", ".divx", ".f4v", ".ogm", ".ogv", ".m2v", ".dat",
    # 音乐本体，音乐库同样需要刮削文件归属判断
    ".flac", ".ape", ".wav", ".dsf", ".dff", ".m4a", ".aac", ".ogg", ".wv",
})

# 刮削文件后缀：元数据 / 图片 / 字幕。
SCRAP_EXTENSIONS: frozenset[str] = frozenset({
    # 元数据
    ".nfo", ".xml", ".json",
    # 图片
    ".jpg", ".jpeg", ".png", ".webp", ".tbn", ".gif", ".bmp", ".svg",
    # 字幕
    ".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup", ".pgs", ".smi",
    ".rt", ".sbv", ".ttml", ".dfxp",
    # 播放列表 / 章节
    ".m3u", ".m3u8", ".cue", ".chapters",
    # 音乐相关附属
    ".lrc", ".log", ".accurip", ".sfv", ".md5",
})

# 目录级共享文件名（小写、不含扩展名的 stem）。
# 这些文件代表「整个目录」，而非某一个媒体文件，因此按目录存活判定。
DIR_SHARED_STEMS: frozenset[str] = frozenset({
    # 电影 / 剧集通用
    "poster", "fanart", "backdrop", "folder", "banner", "clearart", "clearlogo",
    "logo", "disc", "discart", "thumb", "landscape", "keyart", "characterart",
    "season", "seasonall", "season-specials", "tvshow", "show", "movie",
    "collection", "set", "default", "metadata", "movieposter", "showposter",
    # Emby / Jellyfin 风格
    "poster-1", "fanart-1", "backdrop-1", "banner-1", "thumb-1",
})

# 默认受保护的模式：媒体服务器自己生成的目录/文件，不参与清理。
DEFAULT_PROTECTED_PATTERNS: tuple[str, ...] = (
    "*.trickplay",
    "*.actors",
    "*.deleted",
    "theme.*",
    "*.edl",
)

# 媒体服务器常放在库根目录的配置文件夹，整目录跳过。
DEFAULT_SKIP_DIRS: frozenset[str] = frozenset({
    "@eaDir", "#recycle", "$RECYCLE.BIN", "System Volume Information",
    ".@__thumb", "@Recycle", ".davfs2", ".AppleDouble", ".Trashes",
})

# 复合文件名的分隔符：`movie-zh.nfo`、`movie.chs.ass` 等。
COMPOUND_SEPARATORS: tuple[str, ...] = ("-", ".", "_", " ")


class Reason(str, Enum):
    """判定理由，用于报告展示与排错。"""

    # —— 保留类 ——
    PROTECTED = "protected"                    # 命中受保护模式
    MEDIA_PRESENT = "media_present"            # 同目录存在对应媒体文件
    DIR_ALIVE = "dir_alive"                    # 目录级文件，目录内还有媒体
    GENERIC_AMBIGUOUS = "generic_ambiguous"    # 通用名文件，多媒体共存无法归属
    RECENT = "recent"                          # 修改时间太近，处于保护期内
    # —— 残留类 ——
    ORPHAN_STEM = "orphan_stem"                # 无同名媒体文件
    ORPHAN_DIR_SHARED = "orphan_dir_shared"    # 目录级文件且目录已空


# 残留理由集合，便于上层过滤
ORPHAN_REASONS: frozenset[Reason] = frozenset({
    Reason.ORPHAN_STEM,
    Reason.ORPHAN_DIR_SHARED,
})


# 通用附属文件名（不含扩展名）。这些文件不隶属某个具体媒体，而是「跟着目录里
# 唯一的那个媒体走」，常见于电影：`subs.zh.srt`、`chinese.srt`。
# 判定规则：仅当目录内只有一个媒体文件时，才认为它们属于该媒体；
# 目录内有多个媒体文件时无法确定归属，保守处理为「保留」（不删）。
GENERIC_STEMS: frozenset[str] = frozenset({
    "subs", "sub", "subtitles", "subtitle", "字幕", "中字", "简中", "繁中",
    "chs", "cht", "sc", "tc", "gb", "big5", "zh", "en", "eng", "cn",
    "sample", "trailer", "featurette", "extras",
    "score", "soundtrack", "ost",
})


@dataclass(frozen=True)
class ScrapEntry:
    """一个刮削文件的判定结果。"""

    path: str
    size: int
    reason: Reason
    # 人类可读的解释，直接展示在插件页面上
    detail: str
    # 文件是否属于目录级共享文件
    dir_shared: bool = False

    @property
    def is_orphan(self) -> bool:
        """该文件是否被判定为残留（即可以删除）。"""
        return self.reason in ORPHAN_REASONS


@dataclass
class DirStats:
    """单个目录的聚合统计。"""

    path: str
    # 本目录（不含子目录）中的媒体文件数
    media_count: int = 0
    # 整棵子树（含子目录）中的媒体文件数
    subtree_media_count: int = 0
    # 本目录中的刮削文件数
    scrap_count: int = 0


@dataclass
class ScanReport:
    """一次扫描的完整结果。"""

    entries: list[ScrapEntry] = field(default_factory=list)
    dir_stats: dict[str, DirStats] = field(default_factory=dict)
    # 扫描过程中无法访问的路径及原因
    errors: list[tuple[str, str]] = field(default_factory=list)
    # 实际扫描的文件总数（用于提示"扫了多少"）
    scanned_files: int = 0
    # 跳过的目录
    skipped_dirs: list[str] = field(default_factory=list)
    # 扫描完成时间（由调用方填充，便于报告展示）
    scan_time: str = ""

    @property
    def orphans(self) -> list[ScrapEntry]:
        """全部残留文件。"""
        return [entry for entry in self.entries if entry.is_orphan]

    @property
    def kept(self) -> list[ScrapEntry]:
        """全部保留文件。"""
        return [entry for entry in self.entries if not entry.is_orphan]

    @property
    def orphan_bytes(self) -> int:
        """残留文件总字节数。"""
        return sum(entry.size for entry in self.orphans)

    def orphans_in_dir(self, directory: str) -> list[ScrapEntry]:
        """取某个目录（非递归）下的残留文件。"""
        return [
            entry for entry in self.orphans
            if os.path.dirname(entry.path) == directory
        ]

    def empty_dirs(self) -> list[str]:
        """
        计算「删完残留刮削文件后会变空」的目录。

        从深到浅处理：一个目录只有在自身及所有子目录都变空时才算空目录，
        这样父目录能在子目录清空后也被识别出来。
        """
        # 收集所有涉及到的目录
        candidates: set[str] = set()
        for entry in self.entries:
            candidates.add(os.path.dirname(entry.path))

        # 残留文件不会被删除的目录 = 仍有文件
        remaining: dict[str, int] = {}
        deletable = {entry.path for entry in self.orphans}
        for directory in candidates:
            remaining[directory] = 0

        # 直接统计：目录下还有多少文件在当前扫描记录中（非残留的那些）
        for entry in self.entries:
            directory = os.path.dirname(entry.path)
            if entry.path not in deletable:
                remaining[directory] = remaining.get(directory, 0) + 1

        # 自底向上：目录为空且所有子目录也为空 → 该目录会变空
        empty: set[str] = set()
        for directory in sorted(candidates, key=lambda item: item.count(os.sep), reverse=True):
            if remaining.get(directory, 0) > 0:
                continue
            has_files = False
            try:
                # 目录里可能还有未进入扫描记录的文件（如媒体文件），必须实测
                if os.path.isdir(directory):
                    pending = deletable
                    has_files = any(
                        os.path.join(directory, name) not in pending
                        and not os.path.isdir(os.path.join(directory, name))
                        for name in os.listdir(directory)
                    )
                has_child_left = any(
                    os.path.dirname(child) == directory and child not in empty
                    for child in candidates
                )
            except OSError:
                continue
            if not has_files and not has_child_left:
                empty.add(directory)
        return sorted(empty)


def _norm(path: str) -> str:
    """统一路径比较口径：去掉尾部分隔符并规范大小写（Windows 不敏感）。"""
    normalized = os.path.normpath(path)
    return normalized


def _ext(name: str) -> str:
    """取小写扩展名。"""
    return os.path.splitext(name)[1].lower()


def _stem(name: str) -> str:
    """取不含扩展名的文件名主干。"""
    return os.path.splitext(name)[0]


def _normalize_ext(ext: str) -> str:
    """
    把用户填写的后缀统一成小写、带点、无前后空白的形式。

    **必须有这一个函数**：宿主页面上没人会老实填 `.rmvb7`，写成 `rmvb7`、
    ` .RMVB7` 都很常见。若不归一化，`is_media_file` 拿 `rmvb7` 去和
    `".rmvb7"` 比永远不相等，用户会以为规则没生效，进而把配置越改越激进。
    这样调用方既可以只传纯增量，也可以直接塞「内置集合 ∪ 用户增量」的并集，
    两种口径结果一致。
    """
    cleaned = ext.strip().lower()
    if not cleaned:
        return ""
    return cleaned if cleaned.startswith(".") else f".{cleaned}"


def _norm_exts(exts: Iterable[str]) -> frozenset[str]:
    """批量归一化后缀集合。"""
    return frozenset(
        normalized for normalized in (_normalize_ext(item) for item in exts) if normalized
    )


def _ext_hit(ext: str, builtin: frozenset[str], extra: Iterable[str]) -> bool:
    """
    后缀是否命中「内置 ∪ 增量」。

    增量里也可能混进了内置后缀本身（历史写法把「内置 ∪ 用户」整体传进来），
    归一化之后两边比较是幂等的，重复出现不会有副作用。
    """
    if ext in builtin:
        return True
    return any(ext == _normalize_ext(item) for item in extra)


def is_media_file(
    name: str,
    extra_media_exts: Iterable[str] = (),
    patterns: Iterable["re.Pattern[str]"] = (),
) -> bool:
    """
    判断文件名是否为媒体本体。

    内置后缀优先；`extra_media_exts` 是用户追加的后缀，`patterns` 是用户追加的
    正则（`re.search` 匹配完整文件名）。三者任一命中即可。
    """
    ext = _ext(name)
    if ext and _ext_hit(ext, MEDIA_EXTENSIONS, extra_media_exts):
        return True
    return _match_patterns(name, patterns)


def is_scrap_file(
    name: str,
    extra_scrap_exts: Iterable[str] = (),
    patterns: Iterable["re.Pattern[str]"] = (),
) -> bool:
    """判断文件名是否为刮削文件。口径与 `is_media_file` 相同。"""
    ext = _ext(name)
    if ext and _ext_hit(ext, SCRAP_EXTENSIONS, extra_scrap_exts):
        return True
    return _match_patterns(name, patterns)


def is_dir_shared(stem: str, patterns: Iterable["re.Pattern[str]"] = ()) -> bool:
    """
    判断刮削文件主干是否为「目录级共享名」。

    注意 `season01-poster` 这类带序号的变体也算目录级：它们是季目录的封面，
    不隶属于某一个具体剧集文件。用户自定义的 `dir_shared_patterns` 在最后兜底，
    只做扩充（进这一档更保守：按目录存活判定，而不是更严格的 stem 匹配）。
    """
    lowered = stem.lower()
    if lowered in DIR_SHARED_STEMS:
        return True
    # season01-poster / poster-season01 之类的变体
    tokens = [token for token in lowered.replace("_", "-").replace(".", "-").split("-") if token]
    if tokens:
        # 去掉纯数字或 seasonNN / specials 之类的前缀后，剩余部分是否全部是共享名
        meaningful = [
            token for token in tokens
            if not token.isdigit() and not (token.startswith("season") and token[6:].isdigit())
        ]
        if meaningful and all(token in DIR_SHARED_STEMS for token in meaningful):
            return True
    return _match_patterns(stem, patterns)


def _is_dir_shared_stem(stem: str, patterns: Iterable["re.Pattern[str]"] = ()) -> bool:
    """
    目录级共享名判定（供 `analyze_directory` 使用）。

    与公开的 `is_dir_shared` 的区别只有一条：**用户正则先判**。

    为什么要让正则先判：`is_dir_shared` 会把主干按 `-`/`.`/`_` 拆词，只要拆完
    剩下的词里有内置共享名（movie/poster/show…）就整体算目录级。所以
    `MyStudio.Artwork` 这种「用户在 dir_shared 里点名要保护」的文件，
    在配置正则之后仍然会被内置规则抢先命中，用户的规则等于没生效。

    让正则先判必须在安全方向上成立：R2（目录级）**比** R3（同名匹配）更宽松——
    目录里还有媒体就保留。因此「提前进 R2」只可能把保留改成保留、把残留的
    理由从 ORPHAN_STEM 换成 ORPHAN_DIR_SHARED，**不会新增任何残留判定**。
    """
    return _match_patterns(stem, patterns) or is_dir_shared(stem)


# ---------------------------------------------------------------
# 自定义正则规则
# ---------------------------------------------------------------

# 一条用户规则最多被视为「异常配置」的长度。正则引擎对超长/嵌套量词的
# 回溯是指数级的，用户误粘一段文本进来就可能把宿主线程卡死几十分钟。
MAX_PATTERN_LENGTH = 1000


def compile_patterns(
    patterns: Iterable[str],
    report: Optional["ScanReport"] = None,
    label: str = "",
) -> tuple["re.Pattern[str]", ...]:
    """
    把用户配置的正则串编译成 pattern 元组，**任何一条出错都不抛异常**。

    清理插件是无人值守的定时任务，配置写错必须退化成「少识别几个文件」，
    而不是让整个插件崩掉、或者（更糟）让调用方拿到半截状态。因此：
    - 空串 / 纯空白直接跳过（配置页留空是常态，不算错误）；
    - 编译失败 → 记进 report.errors 并忽略该条；
    - 超长 → 同上，避免 ReDoS 把宿主线程拖死。
    """
    compiled: list[re.Pattern[str]] = []
    for raw in patterns or ():
        pattern = str(raw).strip() if raw is not None else ""
        if not pattern:
            continue
        if len(pattern) > MAX_PATTERN_LENGTH:
            if report is not None:
                report.errors.append((
                    label or "自定义规则",
                    f"正则过长（{len(pattern)} > {MAX_PATTERN_LENGTH} 字符），已忽略：{pattern[:40]}…",
                ))
            continue
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error as error:
            if report is not None:
                report.errors.append((label or "自定义规则", f"正则无效，已忽略：{pattern}（{error}）"))
    return tuple(compiled)


def _match_patterns(target: str, patterns: Iterable["re.Pattern[str]"]) -> bool:
    """
    用 `re.search` 逐个尝试用户正则。

    `re.search` 而不是 `fullmatch`：用户写 `\\.rmvb7?$` 或 `主题曲` 都是常见预期，
    要求写完整锚定反而不友好。

    这里刻意捕获 `Exception` 而不是只捕 `re.error`：调用方（`compile_patterns`）
    编译好之后，正常执行理论上不会抛异常，但**一旦抛了就绝不能让异常往外冒**——
    一个判断函数把 AttributeError/TypeError 抛到 `analyze_directory` 外面，
    整个扫描会崩，定时任务会静默失效；更糟的是如果哪天有人在上层加了
    `except: return 残留`，异常就会把文件推向删除那一侧。统一在这里吞成
    「不命中」，正好对应「拿不准就保留」（不命中 = 不额外认定为媒体/刮削，
    或者不额外认定为受保护 → 但后者见 `is_dir_shared` 的说明）。
    """
    for pattern in patterns:
        try:
            if pattern.search(target):
                return True
        except Exception:  # noqa: BLE001 - 用户规则出错不能让扫描崩溃
            continue
    return False


def _format_recent_detail(age_seconds: float, min_age_seconds: int) -> str:
    """
    生成「修改于 X 分钟前，未超过保护期 Y 分钟」的说明文案。

    年龄按分钟向下取整；小于 1 分钟时按秒显示。这样「保护期 1 分钟、年龄 70 秒」
    不会被显示成「修改于 1 分钟前，未超过保护期 1 分钟」这种自相矛盾的话。
    """
    min_minutes = max(1, int(round(min_age_seconds / 60)))
    if age_seconds < 60:
        age_text = f"{age_seconds:.0f} 秒"
    else:
        age_text = f"{int(age_seconds // 60)} 分钟"
    return f"文件修改于 {age_text}前，未超过保护期 {min_minutes} 分钟"


def _bindings_for(stem: str) -> set[str]:
    """
    计算一个刮削文件主干可能对应的媒体主干集合。

    例：`Show.S01E01.chs` → {"show.s01e01.chs", "show.s01e01"}
        `movie-zh`         → {"movie-zh", "movie"}
    """
    lowered = stem.lower()
    results = {lowered}
    # 逐级剥离复合后缀：`.chs`、`-zh`、`.zh-CN` 等
    current = lowered
    for _ in range(4):
        for separator in COMPOUND_SEPARATORS:
            if separator not in current:
                continue
            head, _, tail = current.rpartition(separator)
            if not head or not tail:
                continue
            results.add(head)
            current = head
    return results


# ===== 季集号匹配 =====
# 国内剧集常见「视频用拼音/英文命名、字幕只用 S01E01 命名」：
#     Chen.Mo.De.Zhen.Xiang.S01E01.mkv
#     S01E01.chs.srt            ← 名字完全对不上，但确实是这一集
# 所以除了 stem 匹配，还要按「季集号」兜底匹配，否则会误删正常字幕。
_SEASON_EPISODE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # S01E01 / s01e01 / S01.E01 / S01 E01
    re.compile(r"[sS](\d{1,2})[\s._-]*[eE](\d{1,4})"),
    # 第01季第01集
    re.compile(r"第\s*(\d{1,2})\s*季.*?第\s*(\d{1,4})\s*[集话話]"),
    # 1x01
    re.compile(r"(?<!\d)(\d{1,2})[xX](\d{1,4})(?!\d)"),
)

# 没有季信息、只有集号的情况：第01集 / EP01 / E01
_EPISODE_ONLY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"第\s*(\d{1,4})\s*[集话話]"),
    re.compile(r"(?<![A-Za-z0-9])[eE][pP]?\.?\s*(\d{1,4})(?!\d)"),
)

# 季集号 -> 原始文件名，用于在 detail 里提示"匹配到哪一集"
EpisodeKey = tuple[Optional[int], int]


def extract_episode_key(name: str) -> Optional[EpisodeKey]:
    """
    从文件名中提取「季集号」，用于兜底归属判断。

    :return: (季号或 None, 集号)；识别不到返回 None
    """
    stem = _stem(name)
    for pattern in _SEASON_EPISODE_PATTERNS:
        match = pattern.search(stem)
        if match:
            try:
                return int(match.group(1)), int(match.group(2))
            except (IndexError, ValueError):
                continue
    for pattern in _EPISODE_ONLY_PATTERNS:
        match = pattern.search(stem)
        if match:
            try:
                return None, int(match.group(1))
            except (IndexError, ValueError):
                continue
    return None


def _episode_key_matches(
    scrap_key: EpisodeKey,
    media_keys: dict[EpisodeKey, str],
) -> Optional[str]:
    """
    按季集号匹配，返回匹配到的媒体文件名。

    规则：集号必须相等；双方都有季号时季号也必须相等。
    （刮削文件常省略季号，此时只比集号。）
    """
    scrap_season, scrap_episode = scrap_key
    for (media_season, media_episode), media_name in media_keys.items():
        if media_episode != scrap_episode:
            continue
        if scrap_season is not None and media_season is not None:
            if scrap_season != media_season:
                continue
        return media_name
    return None


def _is_generic(stem: str) -> bool:
    """
    判断刮削文件主干是否为「通用附属名」。

    `subs.zh`、`chinese.chs`、`中字` 这类名字本身不含片名，靠「剥离复合后缀
    后首段命中 GENERIC_STEMS」来识别。
    """
    lowered = stem.lower()
    if lowered in GENERIC_STEMS:
        return True
    # 取第一个分隔符之前的片段，例如 `subs.zh` → `subs`
    head = lowered
    for separator in COMPOUND_SEPARATORS:
        if separator in head:
            head = head.split(separator, 1)[0]
    return head in GENERIC_STEMS


def _matches_media(scrap_stem: str, media_stems: set[str]) -> Optional[str]:
    """返回与该刮削文件匹配的媒体主干；不匹配返回 None。"""
    if not media_stems:
        return None
    lowered = scrap_stem.lower()
    if lowered in media_stems:
        return lowered
    for candidate in _bindings_for(lowered):
        if candidate in media_stems:
            return candidate
    # 反向兼容：媒体主干以刮削主干开头（如媒体 `Movie`、刮削 `Movie.2020.1080p.nfo`）
    for media_stem in media_stems:
        if lowered.startswith(f"{media_stem}.") or lowered.startswith(f"{media_stem}-"):
            return media_stem
    return None


@dataclass
class AnalyzerOptions:
    """
    扫描参数。

    后缀相关字段的语义（重构后）
    ----------------------------
    历史版本里 `media_extensions` / `scrap_extensions` 既被当成「全量集合」用，
    又被 `analyze_directory` 用 `options.scrap_extensions - SCRAP_EXTENSIONS`
    做差集来反推「用户增量」。这是个隐式约定：一旦调用方传进来的集合里混进了
    内置后缀以外的东西，或者干脆只传增量，判断结果就会静默跑偏。

    现在拆成两组明确的字段：
    - `media_extensions` / `scrap_extensions`：**生效的全量集合**，默认等于内置常量。
      只用于展示和统计，判定时拆成下面的「增量」再走。
    - `extra_media_exts` / `extra_scrap_exts`：**用户追加的增量后缀**。判定函数
      `is_media_file` / `is_scrap_file` 内部一律按「内置 ∪ 增量」比，并且会把两种
      写法（`.rmvb7` / `rmvb7`）都归一化。

    兼容性：如果只设置了全量集合（老代码 `options.scrap_extensions = ...` 的写法），
    会自动从「全量 − 内置」推出增量，老调用方不用改也能正确工作。
    """

    media_extensions: frozenset[str] = MEDIA_EXTENSIONS
    scrap_extensions: frozenset[str] = SCRAP_EXTENSIONS
    # 用户追加的后缀（纯增量）。为空时从全量集合里反推，见 resolved_extra_* 属性
    extra_media_exts: frozenset[str] = frozenset()
    extra_scrap_exts: frozenset[str] = frozenset()
    # 用户自定义正则（**原始字符串**，由 analyze_directory 负责编译，
    # 这样编译错误能落进当次 report.errors 而不是在构造 options 时抛出来）
    media_patterns: tuple[str, ...] = ()
    scrap_patterns: tuple[str, ...] = ()
    dir_shared_patterns: tuple[str, ...] = ()
    protected_patterns: tuple[str, ...] = DEFAULT_PROTECTED_PATTERNS
    skip_dirs: frozenset[str] = DEFAULT_SKIP_DIRS
    # 单次扫描最多处理多少文件，防止误配到超大目录时把宿主拖死
    max_files: int = 200_000
    # 路径包含任一关键字则跳过（保留官方插件的习惯）
    exclude_keywords: tuple[str, ...] = ()
    # 「新文件保护期」：修改时间距今不足这么多秒的刮削文件，
    # 即使判定为残留也降级为保留（理由 RECENT）。0 = 关闭。
    min_age_seconds: int = 0

    @property
    def resolved_extra_media_exts(self) -> frozenset[str]:
        """真正生效的媒体后缀增量（显式增量 ∪ 全量减内置）。"""
        derived = frozenset(_norm_exts(self.media_extensions)) - MEDIA_EXTENSIONS
        return _norm_exts(self.extra_media_exts) | derived

    @property
    def resolved_extra_scrap_exts(self) -> frozenset[str]:
        """真正生效的刮削后缀增量（显式增量 ∪ 全量减内置）。"""
        derived = frozenset(_norm_exts(self.scrap_extensions)) - SCRAP_EXTENSIONS
        return _norm_exts(self.extra_scrap_exts) | derived

    def set_extra_scrap_exts(self, exts: Iterable[str]) -> "AnalyzerOptions":
        """
        设置刮削后缀增量的便捷方法，原地修改并返回 self。

        对外暴露这个而不是让调用方直接改 dataclass 字段，是因为旧代码里
        `options.scrap_extensions = <全量集合>` 的写法已经散落在各层
        （初始化时算一次、扫描时再算一次），这里顺手提供统一入口。
        """
        self.extra_scrap_exts = _norm_exts(exts)
        return self


def _is_protected(name: str, patterns: Iterable[str], parents: Iterable[str] = ()) -> bool:
    """
    判断文件是否命中受保护模式。

    同时匹配文件名与任意层父目录名，这样 `*.trickplay` 这类「目录级」保护
    才能生效——Jellyfin 会把缩略图放在 `Movie.trickplay/` 目录里，
    只比对文件名是抓不到的。
    """
    lowered = name.lower()
    for pattern in patterns:
        lowered_pattern = pattern.lower()
        if fnmatch.fnmatch(lowered, lowered_pattern):
            return True
        for parent in parents:
            if fnmatch.fnmatch(parent.lower(), lowered_pattern):
                return True
    return False


def _parent_names(path: str, root: str) -> list[str]:
    """列出 path 相对于 root 之间所有父目录的名字（不含 root 自身）。"""
    names: list[str] = []
    current = os.path.dirname(path)
    root_norm = _norm(root)
    while True:
        if _norm(current) == root_norm or os.path.dirname(current) == current:
            break
        names.append(os.path.basename(current))
        current = os.path.dirname(current)
    return names



def _is_excluded(path: str, keywords: Iterable[str]) -> bool:
    """路径是否命中排除关键字。"""
    if not keywords:
        return False
    lowered = path.lower()
    return any(keyword.lower() in lowered for keyword in keywords if keyword)


def _min_age_guard(min_age_seconds: object) -> Optional[float]:
    """
    把配置里的保护期换算成「秒」，非法值一律视为「关闭保护」。

    配置来自 Web 表单，可能是 "600"、600.0、None、甚至是用户手抖粘进来的文本。
    这里用宽松转换而不是让 int() 抛异常：保护期配错只应导致「少保护」，
    而「少保护」的方向需要**明确**——所以非法值退回 0（关闭）是刻意的，
    不是兜底糊弄：打开一个自己都没测过的默认保护期，比明说不保护更容易被
    误以为是「已经安全了」。
    """
    try:
        seconds = float(min_age_seconds)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not seconds > 0:  # 同时挡住 nan / 负数 / 0
        return None
    return seconds


def _apply_min_age_guard(
    entry: ScrapEntry,
    scrap_stats: dict[str, tuple[int, Optional[float]]],
    guard_seconds: float,
    now: float,
    report: ScanReport,
) -> Optional[ScrapEntry]:
    """
    对一条「已判定为残留」的条目做新文件保护检查。

    :return: 需要**替换**的新条目（降级为保留）；None 表示保持原残留判定。
    """
    _size, mtime = scrap_stats.get(entry.path, (0, None))
    if mtime is None:
        # 拿不到 mtime 就无从判断年龄。按规矩「不保护（保持残留）」，
        # 但必须让用户看见这件事，否则他没法解释为什么某个刚建的文件被判残留。
        report.errors.append((entry.path, "无法获取修改时间，跳过新文件保护检查"))
        return None

    age = now - mtime
    # 年龄为负说明文件的 mtime 在未来（时钟回拨、跨机器复制、或 NAS 时间不同步）。
    # 这种文件「看起来刚被改过」，按保守方向保护；同时留一条 error 方便排查。
    if age < 0:
        report.errors.append((entry.path, "文件修改时间晚于系统时间（时钟可能不同步），按保护处理"))
        age = 0.0
    if age >= guard_seconds:
        return None

    return ScrapEntry(
        path=entry.path,
        size=entry.size,
        reason=Reason.RECENT,
        detail=(
            f"{_format_recent_detail(age, int(guard_seconds))}"
            f"（原判定：{entry.reason.value}）"
        ),
        dir_shared=entry.dir_shared,
    )


def analyze_directory(
    root: str,
    options: Optional[AnalyzerOptions] = None,
) -> ScanReport:
    """
    扫描一个媒体库根目录，输出每个刮削文件的归属判定。

    本函数只读文件系统，绝不修改任何文件。所有判断在内存中完成，
    由调用方（插件）决定是否执行删除。

    :param root: 媒体库根目录
    :param options: 扫描参数
    :return: 扫描报告
    """
    options = options or AnalyzerOptions()
    report = ScanReport()
    root = _norm(root)

    if not os.path.isdir(root):
        report.errors.append((root, "目录不存在或不可访问"))
        return report

    # ---- 第一遍：遍历目录树，收集每个目录的媒体与刮削清单 ----
    # 目录 -> 直接子文件的媒体主干集合
    dir_media_stems: dict[str, set[str]] = {}
    # 目录 -> 直接子文件的相邻元素（媒体文件名，用于 detail 文案）
    dir_media_names: dict[str, dict[str, str]] = {}
    # 目录 -> 季集号到媒体文件名的映射（用于兜底归属）
    dir_media_episodes: dict[str, dict[EpisodeKey, str]] = {}
    # 目录 -> 直接子刮削文件 (文件名)
    dir_scrap_names: dict[str, list[str]] = {}
    # 目录 -> 直接子目录列表
    dir_children: dict[str, list[str]] = {}
    # 所有刮削文件的绝对路径 -> (大小, 修改时间)；mtime 拿不到时为 None。
    # 为什么要在第一遍就 stat 出来：后面「新文件保护期」要用 mtime，
    # 第三遍再 stat 一次等于对每个刮削文件多做一次系统调用；媒体库动辄几十万
    # 文件，在机械硬盘 / 网络盘上这是实打实的分钟级开销。
    scrap_stats: dict[str, tuple[int, Optional[float]]] = {}

    # 自定义正则只编译一次。配置写错时 compile_patterns 负责吞掉异常并记 errors，
    # 这里拿到的就是「能用那几条」。
    media_patterns = compile_patterns(options.media_patterns, report, "媒体正则")
    scrap_patterns = compile_patterns(options.scrap_patterns, report, "刮削正则")
    dir_shared_patterns = compile_patterns(options.dir_shared_patterns, report, "目录共享正则")
    extra_media_exts = options.resolved_extra_media_exts
    extra_scrap_exts = options.resolved_extra_scrap_exts

    stack = [root]
    while stack:
        directory = stack.pop()
        if _is_excluded(directory, options.exclude_keywords):
            continue
        if report.scanned_files >= options.max_files:
            report.errors.append((directory, f"已达单次扫描上限 {options.max_files} 个文件，提前结束"))
            break
        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            report.errors.append((directory, str(error)))
            continue

        media_stems: set[str] = set()
        media_names: dict[str, str] = {}
        # 季集号 -> 媒体文件名，用于「字幕只写 S01E01」的兜底匹配
        media_episodes: dict[EpisodeKey, str] = {}
        scrap_names: list[str] = []
        children: list[str] = []

        for entry in entries:
            report.scanned_files += 1
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in options.skip_dirs:
                        report.skipped_dirs.append(entry.path)
                        continue
                    children.append(entry.path)
                    stack.append(entry.path)
                    continue
            except OSError as error:
                report.errors.append((entry.path, str(error)))
                continue

            name = entry.name
            # 媒体判定排在刮削判定之前：两条正则都命中时按媒体处理（更保守，
            # 把未知文件当成「主」不会导致删除）。
            if is_media_file(name, extra_media_exts, media_patterns):
                stem = _stem(name).lower()
                media_stems.add(stem)
                media_names.setdefault(stem, name)
                episode_key = extract_episode_key(name)
                if episode_key is not None:
                    media_episodes.setdefault(episode_key, name)
            elif is_scrap_file(name, extra_scrap_exts, scrap_patterns):
                scrap_names.append(name)
                size = 0
                mtime: Optional[float] = None
                try:
                    stat_result = entry.stat(follow_symlinks=False)
                except OSError:
                    # stat 失败：大小按 0 记（只影响报告展示），mtime 记 None，
                    # 后面按「不保护」处理并补一条 error。
                    pass
                else:
                    size = stat_result.st_size
                    mtime = float(stat_result.st_mtime)
                scrap_stats[entry.path] = (size, mtime)

        dir_media_stems[directory] = media_stems
        dir_media_names[directory] = media_names
        dir_media_episodes[directory] = media_episodes
        dir_scrap_names[directory] = scrap_names
        dir_children[directory] = children

    # ---- 第二遍：自底向上聚合子树媒体文件数 ----
    subtree: dict[str, int] = {}
    # 按路径深度倒序处理，保证子目录先算完
    for directory in sorted(dir_media_stems, key=lambda item: item.count(os.sep), reverse=True):
        total = len(dir_media_stems[directory])
        for child in dir_children.get(directory, ()):
            total += subtree.get(child, 0)
        subtree[directory] = total

    for directory, stems in dir_media_stems.items():
        report.dir_stats[directory] = DirStats(
            path=directory,
            media_count=len(stems),
            subtree_media_count=subtree.get(directory, 0),
            scrap_count=len(dir_scrap_names.get(directory, ())),
        )

    # 「现在」只取一次：若在循环里反复取 time.time()，一次长扫描过程中时间会
    # 往前漂，同一个目录里的文件可能因为扫描耗时被算出不同的年龄，结果不可复现。
    now = time.time()

    # ---- 第三遍：逐个刮削文件做归属判定 ----
    # 这一遍只**算出**理由，先不落进 report，统一在后面过一遍「残留降级」。
    # 这样新增的保护逻辑（RECENT）天然满足「只降级残留、绝不影响原本就保留的
    # 文件」——保留类条目根本不会进入降级判定。
    pending: list[ScrapEntry] = []

    for directory, names in dir_scrap_names.items():
        media_stems = dir_media_stems.get(directory, set())
        media_names = dir_media_names.get(directory, {})
        alive = subtree.get(directory, 0) > 0

        for name in names:
            path = os.path.join(directory, name)
            size, mtime = scrap_stats.get(path, (0, None))
            stem = _stem(name)

            # R1：受保护模式（文件名或父目录名命中，如 Movie.trickplay/ 缩略图）
            if _is_protected(name, options.protected_patterns, _parent_names(path, root)):
                pending.append(ScrapEntry(
                    path=path, size=size, reason=Reason.PROTECTED,
                    detail="命中受保护模式，永不清理", dir_shared=False,
                ))
                continue

            # R2：目录级共享文件（用户 dir_shared 正则先判，见 _is_dir_shared_stem）
            if _is_dir_shared_stem(stem, dir_shared_patterns):
                if alive:
                    pending.append(ScrapEntry(
                        path=path, size=size, reason=Reason.DIR_ALIVE, dir_shared=True,
                        detail=f"目录级文件，目录内仍有 {subtree.get(directory, 0)} 个媒体文件",
                    ))
                else:
                    pending.append(ScrapEntry(
                        path=path, size=size, reason=Reason.ORPHAN_DIR_SHARED, dir_shared=True,
                        detail="目录级文件，且目录内已无任何媒体文件",
                    ))
                continue

            # R3：普通刮削文件，按同名匹配
            matched = _matches_media(stem, media_stems)
            if matched:
                pending.append(ScrapEntry(
                    path=path, size=size, reason=Reason.MEDIA_PRESENT, dir_shared=False,
                    detail=f"对应媒体文件：{media_names.get(matched, matched)}",
                ))
                continue

            # R3.5：季集号兜底匹配
            # 国内剧集常出现「视频拼音命名、字幕只写 S01E01」的情况，
            # 名字完全对不上但确实是同一集，必须按季集号认领，否则会误删。
            scrap_key = extract_episode_key(name)
            if scrap_key is not None:
                episode_matched = _episode_key_matches(
                    scrap_key, dir_media_episodes.get(directory, {})
                )
                if episode_matched:
                    season, episode = scrap_key
                    label = (
                        f"S{season:02d}E{episode:02d}" if season is not None
                        else f"E{episode:02d}"
                    )
                    pending.append(ScrapEntry(
                        path=path, size=size, reason=Reason.MEDIA_PRESENT, dir_shared=False,
                        detail=f"按季集号 {label} 匹配到：{episode_matched}",
                    ))
                    continue

            # R4：通用附属文件（subs.zh.srt / chinese.srt / ost.flac）
            if _is_generic(stem):
                if not media_stems:
                    pending.append(ScrapEntry(
                        path=path, size=size, reason=Reason.ORPHAN_STEM, dir_shared=False,
                        detail="通用附属文件，且同目录已无任何媒体文件",
                    ))
                elif len(media_stems) == 1:
                    only = next(iter(media_names.values()))
                    pending.append(ScrapEntry(
                        path=path, size=size, reason=Reason.MEDIA_PRESENT, dir_shared=False,
                        detail=f"通用附属文件，归属目录内唯一媒体：{only}",
                    ))
                else:
                    pending.append(ScrapEntry(
                        path=path, size=size, reason=Reason.GENERIC_AMBIGUOUS, dir_shared=False,
                        detail=f"通用附属文件，但同目录有 {len(media_stems)} 个媒体文件，无法确定归属",
                    ))
                continue

            # R5：无同名媒体的普通刮削文件 → 残留
            detail = "同目录没有同名媒体文件"
            if alive and not media_stems:
                detail = "同目录无媒体文件，但子目录内仍有媒体文件"
            elif media_stems:
                detail = f"同目录媒体文件为：{', '.join(sorted(media_names.values())[:3])}"
            pending.append(ScrapEntry(
                path=path, size=size, reason=Reason.ORPHAN_STEM, dir_shared=False,
                detail=detail,
            ))

    # ---- R0：对「残留」做最后的降级检查 ----
    guard = _min_age_guard(options.min_age_seconds)
    for entry in pending:
        if entry.is_orphan and guard is not None:
            resolved = _apply_min_age_guard(entry, scrap_stats, guard, now, report)
            if resolved is not None:
                entry = resolved
        report.entries.append(entry)

    report.entries.sort(key=lambda item: item.path)
    return report
