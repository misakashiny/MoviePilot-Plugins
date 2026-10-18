# -*- coding: utf-8 -*-
"""
analyzer 扩展能力验证
---------------------
覆盖两块「扫描期」新增能力，全部用真实目录树 + 真实文件 stat 验证：

1. **新文件保护期**（`min_age_seconds`）
   - `min_age_seconds=0`（默认）时行为必须与改动前**逐字节一致**（回归基线）
   - 刚写入的残留文件被降级为 `Reason.RECENT`，老文件照常判残留
   - 保护期只「降级残留」，绝不会把原本保留的文件改判成残留
   - 拿不到 mtime / mtime 在未来 / 保护期配置非法 时的保守行为

2. **自定义正则规则**（`media_patterns` / `scrap_patterns` / `dir_shared_patterns`）
   - 内建规则优先，正则只做「额外扩充」
   - 非法正则不崩溃，记进 `report.errors`
   - 重构后的 `extra_media_exts` / `extra_scrap_exts` 语义正确，
     且旧写法（只设置全量 `scrap_extensions`）仍然兼容

本脚本只**新建**，不修改 scenario_check / deleter_check / contract_check / e2e_check。
安全方向：所有断言都按「宁可少删」的预期写——凡是拿不准的情况，期望值是「保留」。
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "plugins.v3"))

# 与其它测试脚本相同的做法：绕过包的 __init__.py，只加载纯逻辑模块，
# 这样即使本机没有装 MoviePilot 宿主依赖，analyzer 的判定逻辑也能独立验证。
import importlib.util  # noqa: E402
import types  # noqa: E402

_PKG_DIR = _REPO_ROOT / "plugins.v3" / "scrapecleaner"


def _load_pure_modules():
    """以包的子模块身份加载纯逻辑模块，但不执行包的 __init__.py。"""
    package = types.ModuleType("scrapecleaner")
    package.__path__ = [str(_PKG_DIR)]
    sys.modules["scrapecleaner"] = package
    loaded = {}
    for name in ("analyzer", "deleter"):
        spec = importlib.util.spec_from_file_location(
            f"scrapecleaner.{name}", _PKG_DIR / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"scrapecleaner.{name}"] = module
        spec.loader.exec_module(module)
        loaded[name] = module
        setattr(package, name, module)
    return loaded


an = _load_pure_modules()["analyzer"]

FAILURES: list[str] = []

# 一个「很久以前」的时间戳：2000-01-01，用来构造「老文件」
OLD_TS = 946_684_800.0


def check(name: str, condition, detail: str = "") -> None:
    if condition:
        print(f"[OK  ] {name}")
    else:
        print(f"[FAIL] {name}  {detail}")
        FAILURES.append(name)


class Case:
    """一个独立的临时媒体库根目录，用例结束整体删除。"""

    def __enter__(self):
        self.base = Path(tempfile.mkdtemp(prefix="scr_ext_"))
        self.lib = self.base / "library"
        self.lib.mkdir()
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.base, ignore_errors=True)
        return False

    def build(self, specs, age_seconds=None):
        """
        按 (相对路径, 字节数) 建文件。

        :param age_seconds: 建完之后把 mtime 统一改成「距今多少秒」，
                            None = 保持文件创建时的真实时间（即「刚写的」）
        """
        created: list[Path] = []
        for rel, size in specs:
            target = self.lib.joinpath(*rel.replace("\\", "/").split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x" * size)
            created.append(target)
        if age_seconds is not None:
            stamp = time.time() - age_seconds
            for target in created:
                os.utime(target, (stamp, stamp))
        return created

    def analyze(self, **option_kwargs):
        options = an.AnalyzerOptions(**option_kwargs)
        return an.analyze_directory(str(self.lib), options)

    def rel(self, path: str) -> str:
        return Path(path).relative_to(self.lib).as_posix()


def orphan_names(report) -> set[str]:
    return {Path(entry.path).name for entry in report.orphans}


def reason_of(report, name: str):
    for entry in report.entries:
        if Path(entry.path).name == name:
            return entry.reason
    return None


def entry_of(report, name: str):
    for entry in report.entries:
        if Path(entry.path).name == name:
            return entry
    return None


# ===============================================================
# 一、回归基线：min_age_seconds=0 必须与改动前完全一致
# ===============================================================
# 这套场景与 scenario_check.py 的用例同构（另取几个刁钻组合），
# 用来证明「新字段默认值 = 行为不变」。
REGRESSION_SPECS = [
    # 健康剧集：全部保留
    ("健康剧集/tvshow.nfo", 100),
    ("健康剧集/poster.jpg", 100),
    ("健康剧集/Season 01/season.nfo", 100),
    ("健康剧集/Season 01/A.S01E01.mkv", 2000),
    ("健康剧集/Season 01/A.S01E01.nfo", 100),
    ("健康剧集/Season 01/A.S01E01.chs.srt", 100),
    # 部分残留
    ("部分残留/Season 01/B.S01E01.mkv", 2000),
    ("部分残留/Season 01/B.S01E01.nfo", 100),
    ("部分残留/Season 01/B.S01E02.nfo", 100),
    # 整剧残留（目录级文件全部无主）
    ("整剧残留/tvshow.nfo", 100),
    ("整剧残留/Season 01/season.nfo", 100),
    ("整剧残留/Season 01/C.S01E01.nfo", 100),
    # 健康电影 + 通用附属
    ("健康电影/movie.mkv", 2000),
    ("健康电影/movie.nfo", 100),
    ("健康电影/subs.zh.srt", 100),
    # 受保护
    ("受保护/F.mkv", 2000),
    ("受保护/theme.mp3", 100),
    ("受保护/F.trickplay/a.jpg", 100),
    # 多版本
    ("多版本/H.1080p.mkv", 2000),
    ("多版本/H.2160p.mkv", 2000),
    ("多版本/H.1080p.nfo", 100),
    ("多版本/poster.jpg", 100),
]

REGRESSION_ORPHANS = {
    "B.S01E02.nfo",
    "tvshow.nfo",
    "season.nfo",
    "C.S01E01.nfo",
}


def case_regression_min_age_zero():
    """min_age_seconds=0：判定结果与改动前完全一致，且不产生 RECENT / errors。"""
    with Case() as c:
        # 关键：把文件 mtime 设成「刚刚」——如果保护期逻辑被误触发，这里立刻暴露
        c.build(REGRESSION_SPECS, age_seconds=0)
        report_default = c.analyze()                       # 默认值
        report_explicit = c.analyze(min_age_seconds=0)     # 显式 0

        for label, report in (("默认", report_default), ("显式 0", report_explicit)):
            got = {c.rel(entry.path).split("/")[-1] for entry in report.orphans}
            check(f"min_age_seconds=0（{label}）残留集合不变", got == REGRESSION_ORPHANS,
                  f"实际 {sorted(got)}")
            check(f"min_age_seconds=0（{label}）无 RECENT 条目",
                  reason_of(report, "B.S01E02.nfo") is not an.Reason.RECENT)
            check(f"min_age_seconds=0（{label}）不产生 errors", not report.errors,
                  str(report.errors[:2]))
            # 15 条刮削条目：media_present 4 + dir_alive 5 + protected 1 + 残留 5
            # （theme.mp3 不是刮削后缀，.trickplay 目录本身不是文件，都不产生条目）
            check(f"min_age_seconds=0（{label}）每条刮削文件都有判定",
                  len(report.entries) == 15, str(len(report.entries)))

        # 两次结果必须完全一致（可复现性）
        a = [(e.path, e.reason, e.size) for e in report_default.entries]
        b = [(e.path, e.reason, e.size) for e in report_explicit.entries]
        check("默认值与显式 0 的判定逐条一致", a == b, "两次结果不同")

        # 目录级文件在多版本目录里仍应保留（回归：R2 没有被正则/重构改动影响）
        check("多版本 poster.jpg 仍保留（目录存活）",
              reason_of(report_default, "poster.jpg") is an.Reason.DIR_ALIVE,
              str(reason_of(report_default, "poster.jpg")))
        # 受保护的 .trickplay 目录：目录本身不是媒体也不是刮削文件，不会进报告；
        # 目录里的文件因为父目录命中 *.trickplay 而判 PROTECTED（R1）。
        trickplay = entry_of(report_default, "a.jpg")
        check("受保护目录内的文件判 PROTECTED",
              trickplay is not None and trickplay.reason is an.Reason.PROTECTED,
              str(trickplay.reason if trickplay else "(无条目)"))


# ===============================================================
# 二、新文件保护期
# ===============================================================
def case_recent_is_protected():
    """刚写入的残留文件被降级为 RECENT；老文件照常判残留。"""
    with Case() as c:
        c.build([
            # 刚整理完、视频还没落盘的场景：全是「刚写的」
            ("新整理/正在转移/x.nfo", 100),
            ("新整理/正在转移/x.srt", 100),
        ], age_seconds=5)

        report = c.analyze(min_age_seconds=3600)
        check("新文件不再判残留", not report.orphans,
              str([c.rel(e.path) for e in report.orphans]))
        check("新文件理由是 RECENT",
              reason_of(report, "x.nfo") is an.Reason.RECENT,
              str(reason_of(report, "x.nfo")))
        entry = entry_of(report, "x.nfo")
        check("RECENT 的 detail 写清了年龄与保护期",
              entry is not None and "未超过保护期" in entry.detail and "60 分钟" in entry.detail,
              entry.detail if entry else "(无条目)")
        check("RECENT 不属于 ORPHAN_REASONS", an.Reason.RECENT not in an.ORPHAN_REASONS)
        check("RECENT 被计入 kept 而不是 orphans",
              entry in report.kept and entry not in report.orphans)

    with Case() as c:
        # 同样的目录结构，但文件是「老的」→ 应当照常判残留
        c.build([
            ("老残留/已删除/x.nfo", 100),
            ("老残留/已删除/x.srt", 100),
        ], age_seconds=7200)
        report = c.analyze(min_age_seconds=3600)
        check("老文件照常判残留", orphan_names(report) == {"x.nfo", "x.srt"},
              str(sorted(orphan_names(report))))
        check("老文件理由仍是 ORPHAN_STEM",
              reason_of(report, "x.nfo") is an.Reason.ORPHAN_STEM,
              str(reason_of(report, "x.nfo")))


def case_recent_boundary():
    """保护期边界：刚好在界外的判残留，界内的保护。"""
    with Case() as c:
        c.build([("边界/a.nfo", 100), ("边界/b.nfo", 100)], age_seconds=600)
        # 文件年龄 600 秒，保护期 600 秒：age >= guard → 不保护
        report = c.analyze(min_age_seconds=600)
        check("年龄等于保护期 → 不保护（判残留）", orphan_names(report) == {"a.nfo", "b.nfo"},
              str(sorted(orphan_names(report))))

        # 保护期设为 601 秒 → 两个文件都在保护期内
        report2 = c.analyze(min_age_seconds=601)
        check("保护期略微放宽 → 全部保护", not report2.orphans,
              str([c.rel(e.path) for e in report2.orphans]))


def case_recent_only_downgrades():
    """保护期只降级残留，原本保留的文件理由不受影响（不被改写成 RECENT）。"""
    with Case() as c:
        c.build([
            ("健康/Dune.mkv", 2000),
            ("健康/Dune.nfo", 100),              # MEDIA_PRESENT（stem 精确匹配）
            ("健康/poster.jpg", 100),            # DIR_ALIVE（目录级共享名）
            ("健康/theme.mkv", 2000),
            # R1 受保护：文件名命中 theme.*。它同时是「无同名媒体」的刮削文件，
            # 最能验证 R1 优先于 R0（否则会被当成残留再降级成 RECENT）。
            ("健康/theme.mkv.nfo", 100),         # PROTECTED
            # 通用附属文件但目录里有多个媒体 → GENERIC_AMBIGUOUS，也必须保持原样
            ("通用/subtitles.srt", 100),
            ("通用/v1.mkv", 2000),
            ("通用/v2.mkv", 2000),
        ], age_seconds=1)  # 全部是刚写的

        report = c.analyze(min_age_seconds=3600)
        reasons = {Path(e.path).name: e.reason for e in report.entries}
        check("MEDIA_PRESENT 保持原样", reasons.get("Dune.nfo") is an.Reason.MEDIA_PRESENT,
              str(reasons.get("Dune.nfo")))
        check("DIR_ALIVE 保持原样", reasons.get("poster.jpg") is an.Reason.DIR_ALIVE,
              str(reasons.get("poster.jpg")))
        check("受保护文件保持 PROTECTED（R1 优先于 R0）",
              reasons.get("theme.mkv.nfo") is an.Reason.PROTECTED,
              str(reasons.get("theme.mkv.nfo")))
        check("GENERIC_AMBIGUOUS 保持原样",
              reasons.get("subtitles.srt") is an.Reason.GENERIC_AMBIGUOUS,
              str(reasons.get("subtitles.srt")))
        check("没有文件被误标为 RECENT",
              not any(entry.reason is an.Reason.RECENT for entry in report.entries),
              str([Path(e.path).name for e in report.entries
                   if e.reason is an.Reason.RECENT]))


def case_recent_dir_shared():
    """目录级残留（ORPHAN_DIR_SHARED）同样受保护期约束，dir_shared 标记保留。"""
    with Case() as c:
        c.build([
            ("整剧已删/tvshow.nfo", 100),
            ("整剧已删/poster.jpg", 100),
        ], age_seconds=10)
        report = c.analyze(min_age_seconds=3600)
        check("目录级残留也被保护", not report.orphans,
              str([c.rel(e.path) for e in report.orphans]))
        entry = entry_of(report, "tvshow.nfo")
        check("RECENT 保留 dir_shared 标记", entry is not None and entry.dir_shared is True,
              str(entry.dir_shared if entry else "(无条目)"))
        check("RECENT 的 detail 记下了原判定",
              entry is not None and "orphan_dir_shared" in entry.detail,
              entry.detail if entry else "(无条目)")


def case_recent_future_mtime():
    """mtime 在未来（时钟不同步）：保守按「刚改过」保护，并记录一条 error。"""
    with Case() as c:
        c.build([("未来/a.nfo", 100)])
        target = c.lib / "未来" / "a.nfo"
        future = time.time() + 86_400
        os.utime(target, (future, future))

        report = c.analyze(min_age_seconds=3600)
        check("未来 mtime 的文件被保护", not report.orphans,
              str([c.rel(e.path) for e in report.orphans]))
        check("未来 mtime 记录了可诊断的 error",
              any("修改时间晚于系统时间" in str(reason) for _, reason in report.errors),
              str(report.errors))


def case_recent_invalid_option():
    """保护期配置非法（None / 文本 / 负数）：不崩溃，视为关闭。"""
    with Case() as c:
        c.build([("残留/a.nfo", 100)], age_seconds=1000)
        for value in (None, "abc", -1, float("nan")):
            try:
                report = c.analyze(min_age_seconds=value)
            except Exception as error:  # noqa: BLE001 - 这里就是要证明「不抛」
                check(f"非法保护期 {value!r} 不抛异常", False, repr(error))
                continue
            check(f"非法保护期 {value!r} 视为关闭（照常判残留）",
                  orphan_names(report) == {"a.nfo"},
                  f"{value!r} → {sorted(orphan_names(report))}")

        # 字符串形式的合法值应当能识别（配置页可能传 "1800"）
        report = c.analyze(min_age_seconds="3600")
        check("字符串形式的保护期生效", not report.orphans,
              str(sorted(orphan_names(report))))


def case_min_age_default():
    """字段默认值必须是 0（关闭），否则等于给所有用户的插件「静默加保护期」。"""
    check("AnalyzerOptions.min_age_seconds 默认 0",
          an.AnalyzerOptions().min_age_seconds == 0,
          str(an.AnalyzerOptions().min_age_seconds))
    check("AnalyzerOptions.media_patterns 默认空元组",
          an.AnalyzerOptions().media_patterns == ())
    check("AnalyzerOptions.scrap_patterns 默认空元组",
          an.AnalyzerOptions().scrap_patterns == ())
    check("AnalyzerOptions.dir_shared_patterns 默认空元组",
          an.AnalyzerOptions().dir_shared_patterns == ())


# ===============================================================
# 三、自定义正则规则
# ===============================================================
def case_invalid_pattern_survives():
    """非法正则：不崩溃，记进 errors，其余规则照常生效。"""
    with Case() as c:
        c.build([
            ("库/a.nfo", 100),
            ("库/b.weird", 100),
        ])
        try:
            report = c.analyze(
                # 三条不同位置各放一条非法正则，确保三处都做了容错
                media_patterns=[r"([unclosed"],
                scrap_patterns=[r"*bad"],
                dir_shared_patterns=[r"(?P<oops"],
            )
        except Exception as error:  # noqa: BLE001 - 这里就是要证明「不抛」
            check("非法正则不抛异常", False, repr(error))
            return

        check("非法正则不抛异常", True)
        check("非法正则记进 errors", len(report.errors) >= 3, str(report.errors))
        check("errors 里说明了是「正则无效」",
              all("正则无效" in str(reason) for _, reason in report.errors
                  if "正则" in str(reason) or True)
              and any("正则无效" in str(reason) for _, reason in report.errors),
              str(report.errors))
        check("errors 带上了原始规则文本",
              any("([unclosed" in str(reason) for _, reason in report.errors),
              str(report.errors))
        # 非法的 scrap 正则被忽略后，.weird 不应当凭空变成刮削文件
        check("非法正则不产生条目", orphan_names(report) == {"a.nfo"},
              str(sorted(orphan_names(report))))

    # 混入一条合法的 + 一条非法的：合法的必须仍然生效
    with Case() as c:
        c.build([("库/主题曲.myt", 100)])
        report = c.analyze(scrap_patterns=[r" ([", r"\.myt$"])
        check("合法规则与非法规则混用时，合法的仍然生效",
              orphan_names(report) == {"主题曲.myt"},
              str(sorted(orphan_names(report))))
        check("只记录非法那一条", len(report.errors) == 1, str(report.errors))

    # 空白串是「配置页没填」，不是错误
    with Case() as c:
        c.build([("库/a.nfo", 100)])
        report = c.analyze(scrap_patterns=("", "   ", None))
        check("空白正则不当成错误", not report.errors, str(report.errors))

    # 超长正则（ReDoS 风险）按配置错误处理，不编译
    with Case() as c:
        c.build([("库/a.nfo", 100)])
        report = c.analyze(scrap_patterns=("a" * (an.MAX_PATTERN_LENGTH + 1),))
        check("超长正则被拒绝并记录",
              any("正则过长" in str(reason) for _, reason in report.errors), str(report.errors))


def case_media_pattern():
    """自定义 media 正则：内置没覆盖的怪后缀也能被认成媒体文件。"""
    with Case() as c:
        c.build([
            ("电影/Dune.rmvb7", 2000),     # 内置表里没有 .rmvb7
            ("电影/Dune.nfo", 100),
        ])
        # 未配置正则：.rmvb7 不是媒体 → nfo 无主
        report_plain = c.analyze()
        check("未配置时 .rmvb7 不被当作媒体",
              orphan_names(report_plain) == {"Dune.nfo"},
              str(sorted(orphan_names(report_plain))))

        # 配置正则后：.rmvb7 成为媒体，nfo 靠 stem 精确匹配找到主。
        # （刻意不用 `Movie` 当片名：`movie` 是内置的目录级共享名，会走 R2，
        #  那样测到的是目录存活判定而不是 stem 匹配。）
        report_ext = c.analyze(media_patterns=(r"\.rmvb7$",))
        check("自定义 media 正则让 nfo 有主",
              reason_of(report_ext, "Dune.nfo") is an.Reason.MEDIA_PRESENT,
              str(reason_of(report_ext, "Dune.nfo")))
        check("自定义 media 正则识别结果计入 media_count",
              report_ext.dir_stats[str(c.lib / "电影")].media_count == 1,
              str(report_ext.dir_stats[str(c.lib / "电影")].media_count))

        # re.search 语义：部分匹配即可（不要求完整锚定）
        report_partial = c.analyze(media_patterns=("rmvb7",))
        check("自定义 media 正则用 search（部分匹配也算）",
              reason_of(report_partial, "Dune.nfo") is an.Reason.MEDIA_PRESENT,
              str(reason_of(report_partial, "Dune.nfo")))

        # IGNORECASE：用小写规则匹配大写文件名
        report_case = c.analyze(media_patterns=(r"\.RMVB7$",))
        check("自定义正则不区分大小写",
              reason_of(report_case, "Dune.nfo") is an.Reason.MEDIA_PRESENT,
              str(reason_of(report_case, "Dune.nfo")))


def case_scrap_pattern():
    """自定义 scrap 正则：把用户自己的怪文件当作刮削文件参与判定。"""
    with Case() as c:
        c.build([
            # 空目录里只有两个自定义后缀的文件 → 两者都该被判残留
            ("空库/A.mysub", 100),
            ("空库/B.other", 100),
            # 有视频的目录：自定义刮削文件应当按 stem 匹配到视频而保留
            ("健康/Dune.mkv", 2000),
            ("健康/Dune.mysub", 100),
        ])
        # 未配置：.mysub / .other 完全不出现在报告里（不认识的类型不碰）
        report_plain = c.analyze()
        names_plain = {Path(e.path).name for e in report_plain.entries}
        check("未配置时自定义后缀文件不参与扫描",
              "A.mysub" not in names_plain and "B.other" not in names_plain,
              str(sorted(names_plain)))

        report_ext = c.analyze(scrap_patterns=(r"\.mysub$", r"\.other$"))
        check("自定义 scrap 正则把文件纳入残留判定",
              orphan_names(report_ext) == {"A.mysub", "B.other"},
              str(sorted(orphan_names(report_ext))))
        # 优先级：内置规则优先，正则只是扩充——有完整同名视频时仍按 stem 保留。
        # 注意必须用完整同名（Dune.mysub ↔ Dune.mkv）：`_matches_media` 的复合剥离
        # 对 `nfo`/`mysub` 这类不带分隔符的主干天然不命中，那是原有 R3 的设计
        # （避免 `x`/`y` 互相认领），不是本次扩展引入的问题。
        check("自定义 scrap 正则命中后仍遵守 stem 归属",
              reason_of(report_ext, "Dune.mysub") is an.Reason.MEDIA_PRESENT,
              str(reason_of(report_ext, "Dune.mysub")))
        # 反向记录既有行为：`A.mysub` 在空目录里被判残留（ORPHAN_STEM），
        # 说明正则只负责「是不是刮削文件」，归属仍然只走 R3/R3.5/R4。
        check("正则只决定「是不是刮削文件」，不改变归属判定",
              "A.mysub" in orphan_names(report_ext),
              str(sorted(orphan_names(report_ext))))

        # 媒体判定排在刮削之前：同时命中两条正则的文件按媒体处理（保守方向）
        report_both = c.analyze(
            media_patterns=(r"\.mysub$",),
            scrap_patterns=(r"\.mysub$",),
        )
        check("媒体与刮削正则同时命中时按媒体处理（保守）",
              "A.mysub" not in {Path(e.path).name for e in report_both.entries},
              str(sorted(Path(e.path).name for e in report_both.entries)))


def case_dir_shared_pattern():
    """自定义 dir_shared 正则：保护用户的目录级共享命名。"""
    with Case() as c:
        c.build([
            ("剧集/tvshow.nfo", 100),          # 内置目录级
            ("剧集/MyStudio.Artwork.jpg", 100),  # 自定义：默认不认识的目录级文件
            ("剧集/Season 01/E01.mkv", 2000),
            # 完全空的剧集：目录级文件会被判 ORPHAN_DIR_SHARED
            ("空剧集/MyStudio.Artwork.jpg", 100),
        ])
        report_plain = c.analyze()
        # 默认：MyStudio.Artwork.jpg 是普通刮削文件，按 stem 判定；
        # 主干 "MyStudio.Artwork" 与 "E01" 无关 → 判残留（保守方向是加保护，
        # 所以这里正是用户需要配 dir_shared 正则的原因）
        check("默认时自定义目录级文件被判残留",
              reason_of(report_plain, "MyStudio.Artwork.jpg") is an.Reason.ORPHAN_STEM,
              str(reason_of(report_plain, "MyStudio.Artwork.jpg")))

        report_ext = c.analyze(dir_shared_patterns=(r"^MyStudio\.",))
        check("自定义 dir_shared 正则让文件按目录存活判定（受保护）",
              reason_of(report_ext, "MyStudio.Artwork.jpg") is an.Reason.DIR_ALIVE,
              str(reason_of(report_ext, "MyStudio.Artwork.jpg")))
        entry = entry_of(report_ext, "MyStudio.Artwork.jpg")
        check("自定义 dir_shared 条目带 dir_shared 标记",
              entry is not None and entry.dir_shared is True,
              str(entry.dir_shared if entry else "(无条目)"))

        # dir_shared 匹配的是「不含后缀的主干」：规则里写不带后缀的片名即可命中
        report_stem = c.analyze(dir_shared_patterns=("mystudio",))
        check("dir_shared 正则匹配主干（不含后缀）",
              reason_of(report_stem, "MyStudio.Artwork.jpg") is an.Reason.DIR_ALIVE,
              str(reason_of(report_stem, "MyStudio.Artwork.jpg")))

        # 空目录里：既然是目录级文件，理由应是 ORPHAN_DIR_SHARED 而不是 ORPHAN_STEM
        empty_entry = [
            e for e in report_ext.entries
            if Path(e.path).name == "MyStudio.Artwork.jpg"
            and Path(e.path).parent.name == "空剧集"
        ]
        check("空目录里的自定义目录级文件判 ORPHAN_DIR_SHARED",
              bool(empty_entry) and empty_entry[0].reason is an.Reason.ORPHAN_DIR_SHARED,
              str([e.reason for e in empty_entry]))

        # dir_shared 匹配的是「不含后缀的主干」：规则里写不带后缀的片名即可命中
        report_stem = c.analyze(dir_shared_patterns=("mystudio",))
        check("dir_shared 正则匹配主干（不含后缀）",
              reason_of(report_stem, "MyStudio.Artwork.jpg") is an.Reason.DIR_ALIVE,
              str(reason_of(report_stem, "MyStudio.Artwork.jpg")))

        # 空目录里：既然是目录级文件，理由应是 ORPHAN_DIR_SHARED 而不是 ORPHAN_STEM
        empty_entry = [
            e for e in report_ext.entries
            if Path(e.path).name == "MyStudio.Artwork.jpg"
            and Path(e.path).parent.name == "空剧集"
        ]
        check("空目录里的自定义目录级文件判 ORPHAN_DIR_SHARED",
              bool(empty_entry) and empty_entry[0].reason is an.Reason.ORPHAN_DIR_SHARED,
              str([e.reason for e in empty_entry]))


# ===============================================================
# 四、后缀字段重构：extra_media_exts / extra_scrap_exts 语义
# ===============================================================
def case_extra_ext_semantics():
    """显式增量后缀生效，且归一化容忍「不带点 / 大写 / 前后空格」。"""
    with Case() as c:
        c.build([
            ("空库/a.myt1", 100),
            ("空库/b.myt2", 100),
            ("空库/c.myt3", 100),
            ("空库/d.myt4", 100),
        ])
        options = an.AnalyzerOptions(
            extra_scrap_exts=frozenset({"myt1", " .MYT2 ", ".myt3"}),
        )
        report = an.analyze_directory(str(c.lib), options)
        got = orphan_names(report)
        check("显式 extra_scrap_exts 生效（不带点/大写/空格都能识别）",
              {"a.myt1", "b.myt2", "c.myt3"} <= got, str(sorted(got)))
        check("未声明的后缀不参与", "d.myt4" not in got, str(sorted(got)))
        check("解析后的增量已归一化",
              options.resolved_extra_scrap_exts == {".myt1", ".myt2", ".myt3"},
              str(sorted(options.resolved_extra_scrap_exts)))

    with Case() as c:
        c.build([
            ("电影/X.weird1", 2000),
            ("电影/X.nfo", 100),
            ("电影/X.weird2", 2000),
        ])
        options = an.AnalyzerOptions(extra_media_exts=frozenset({".weird1"}))
        report = an.analyze_directory(str(c.lib), options)
        check("显式 extra_media_exts 生效：X.nfo 有主",
              reason_of(report, "X.nfo") is an.Reason.MEDIA_PRESENT,
              str(reason_of(report, "X.nfo")))
        check("未声明的媒体后缀不计入 media_count",
              report.dir_stats[str(c.lib / "电影")].media_count == 1,
              str(report.dir_stats[str(c.lib / "电影")].media_count))

    # 显式增量与「全量集合」两种写法应当等价
    c1 = an.AnalyzerOptions(extra_scrap_exts=frozenset({".myt"}))
    c2 = an.AnalyzerOptions(scrap_extensions=an.SCRAP_EXTENSIONS | {".myt"})
    check("显式增量 == 全量集合减内置",
          c1.resolved_extra_scrap_exts == c2.resolved_extra_scrap_exts,
          f"{sorted(c1.resolved_extra_scrap_exts)} != {sorted(c2.resolved_extra_scrap_exts)}")


def case_backward_compat_full_set():
    """
    兼容旧写法：调用方只设置全量 scrap_extensions（内部原来是靠差集反推增量）。

    真实项目里 __init__.py 就是这么写的，重构后必须继续工作。
    """
    with Case() as c:
        c.build([("空库/b.myext", 100), ("空库/a.nfo", 100)])
        options = an.AnalyzerOptions()
        # 旧代码的写法：直接赋全量集合
        options.scrap_extensions = frozenset(an.SCRAP_EXTENSIONS | {".myext"})
        report = an.analyze_directory(str(c.lib), options)
        check("旧写法（只赋全量集合）仍然生效",
              orphan_names(report) == {"a.nfo", "b.myext"},
              str(sorted(orphan_names(report))))

    with Case() as c:
        c.build([("电影/X.mkv2", 2000), ("电影/X.nfo", 100)])
        options = an.AnalyzerOptions()
        options.media_extensions = frozenset(an.MEDIA_EXTENSIONS | {".mkv2"})
        report = an.analyze_directory(str(c.lib), options)
        check("旧写法（全量 media_extensions）仍然生效",
              reason_of(report, "X.nfo") is an.Reason.MEDIA_PRESENT,
              str(reason_of(report, "X.nfo")))

    # set_extra_scrap_exts 便捷方法
    options = an.AnalyzerOptions()
    returned = options.set_extra_scrap_exts(["MYX", ".myy"])
    check("set_extra_scrap_exts 返回自身",
          returned is options, str(type(returned)))
    check("set_extra_scrap_exts 归一化",
          options.extra_scrap_exts == {".myx", ".myy"},
          str(sorted(options.extra_scrap_exts)))


def case_predicate_functions():
    """判定函数的直接单元测试（不涉及文件系统）。"""
    check("is_media_file 内置", an.is_media_file("a.mkv") is True)
    check("is_media_file 增量（不带点）", an.is_media_file("a.rmvb7", ("rmvb7",)) is True)
    check("is_media_file 增量（大写）", an.is_media_file("a.rmvb7", (".RMVB7",)) is True)
    check("is_media_file 正则", an.is_media_file("a.rmvb7", (), [__import__("re").compile(
        r"\.rmvb7$", __import__("re").IGNORECASE)]) is True)
    check("is_media_file 无后缀", an.is_media_file("README") is False)

    check("is_scrap_file 内置", an.is_scrap_file("a.nfo") is True)
    check("is_scrap_file 增量", an.is_scrap_file("a.mysub", (".mysub",)) is True)
    check("is_scrap_file 未知后缀", an.is_scrap_file("a.mysub") is False)

    check("is_dir_shared 内置", an.is_dir_shared("poster") is True)
    check("is_dir_shared 变体", an.is_dir_shared("season01-poster") is True)
    # "Movie.2020" 会被拆成 ["movie", "2020"]，剩下 "movie" 是内置共享名，
    # 所以它本来就属于目录级——这是**既有行为**，这里只做事实记录。
    check("is_dir_shared 复合名按词拆解（既有行为）",
          an.is_dir_shared("Movie.2020") is True,
          str(an.is_dir_shared("Movie.2020")))
    check("is_dir_shared 普通名", an.is_dir_shared("Dune.2021.1080p") is False,
          str(an.is_dir_shared("Dune.2021.1080p")))
    check("is_dir_shared 正则命中", an.is_dir_shared("MyStudio", [
        an.re.compile(r"^mystudio$", an.re.IGNORECASE)]) is True)


def case_pattern_param_robustness():
    """
    判定函数拿到「非编译后 pattern」时绝不能抛异常。

    公开函数 `is_media_file` / `is_scrap_file` / `is_dir_shared` 的 `patterns`
    参数在类型标注上是编译后的 pattern，但它是**公开 API**，调用方可能顺手把
    配置里的原始字符串传进来（或传成单个字符串、None、其它对象）。一旦
    `_match_patterns` 抛出 AttributeError，异常会一路冒到 `analyze_directory`
    外面，整个扫描、进而整个定时任务都会静默失效。

    期望行为：吞掉异常并视为「该条规则不命中」——不命中 = 不额外认定为媒体，
    文件保持原有判定，方向仍然是「保守」（不会因为一条坏规则把文件推向删除）。
    """
    bad_inputs = ["\\.rmvb7$", None, 42, object()]

    for bad in bad_inputs:
        for name, func in (
            ("is_media_file", lambda p: an.is_media_file("a.rmvb7", (), [p])),
            ("is_scrap_file", lambda p: an.is_scrap_file("a.rmvb7", (), [p])),
            ("is_dir_shared", lambda p: an.is_dir_shared("MyStudio", [p])),
        ):
            try:
                result = func(bad)
            except Exception as error:  # noqa: BLE001 - 这里就是要证明「不抛」
                check(f"{name} 接受非法 pattern（{type(bad).__name__}）不抛异常",
                      False, repr(error))
                continue
            check(f"{name} 对非法 pattern（{type(bad).__name__}）返回 False",
                  result is False, repr(result))

    # 单个字符串（而不是 list）也不应崩：字符串是可迭代的，会逐字符去 search，
    # 结果必然是「不命中」，但绝不能抛。
    try:
        an.is_dir_shared("MyStudio", "mystudio")
        check("patterns 传成单个字符串不抛异常", True)
    except Exception as error:  # noqa: BLE001
        check("patterns 传成单个字符串不抛异常", False, repr(error))


# ===============================================================
# 五、组合场景：保护期 + 正则一起用
# ===============================================================
def case_combined():
    """保护期与自定义规则同时生效时互不干扰。"""
    with Case() as c:
        c.build([
            # 老的自定义刮削文件 → 判残留
            ("老库/A.mybox", 100),
            # 新的自定义刮削文件 → RECENT
            ("新库/B.mybox", 100),
        ])
        old = time.time() - 86_400
        os.utime(c.lib / "老库" / "A.mybox", (old, old))

        report = c.analyze(scrap_patterns=(r"\.mybox$",), min_age_seconds=3600)
        check("组合场景：老的判残留", "A.mybox" in orphan_names(report),
              str(sorted(orphan_names(report))))
        check("组合场景：新的被保护",
              reason_of(report, "B.mybox") is an.Reason.RECENT,
              str(reason_of(report, "B.mybox")))
        check("组合场景：protected_patterns 依旧优先于 RECENT",
              reason_of(report, "A.mybox") is an.Reason.ORPHAN_STEM,
              str(reason_of(report, "A.mybox")))

    # 受保护文件即使很新也不该变成 RECENT（R1 先于 R0）。
    # 注意 theme.mp3 不是刮削后缀，根本不会进扫描，必须构造一个「受保护 + 刮削后缀」。
    with Case() as c:
        c.build([("剧/theme.mp3", 100), ("剧/theme.mkv.nfo", 100), ("剧/other.nfo", 100)],
                age_seconds=1)
        report = c.analyze(min_age_seconds=3600)
        check("受保护文件保持 PROTECTED（不降级成 RECENT）",
              reason_of(report, "theme.mkv.nfo") is an.Reason.PROTECTED,
              str(reason_of(report, "theme.mkv.nfo")))
        check("同目录普通新残留被 RECENT 保护",
              reason_of(report, "other.nfo") is an.Reason.RECENT,
              str(reason_of(report, "other.nfo")))


def main() -> int:
    cases = [
        ("回归：min_age_seconds=0", case_regression_min_age_zero),
        ("字段默认值", case_min_age_default),
        ("新文件保护", case_recent_is_protected),
        ("保护期边界", case_recent_boundary),
        ("保护期只降级残留", case_recent_only_downgrades),
        ("目录级残留保护", case_recent_dir_shared),
        ("未来 mtime", case_recent_future_mtime),
        ("非法保护期配置", case_recent_invalid_option),
        ("非法正则容错", case_invalid_pattern_survives),
        ("自定义 media 正则", case_media_pattern),
        ("自定义 scrap 正则", case_scrap_pattern),
        ("自定义 dir_shared 正则", case_dir_shared_pattern),
        ("后缀增量语义", case_extra_ext_semantics),
        ("旧写法兼容", case_backward_compat_full_set),
        ("判定函数单测", case_predicate_functions),
        ("pattern 参数健壮性", case_pattern_param_robustness),
        ("组合场景", case_combined),
    ]

    for title, func in cases:
        print(f"\n=== {title} ===")
        try:
            func()
        except Exception as error:  # noqa: BLE001 - 一个用例炸了要能看见全部结果
            import traceback
            traceback.print_exc()
            print(f"[FAIL] {title} 抛出异常: {error}")
            FAILURES.append(title)

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过: {FAILURES}")
        return 1
    print(f"✅ 扩展能力验证全部通过（{len(cases)} 组用例）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
