# -*- coding: utf-8 -*-
"""analyzer 的场景化验证脚本：构造真实目录树，检查判定结果是否符合预期。

这个脚本用 `assert` 硬校验，任何一条不符合都会失败——清理插件的判定逻辑
必须先在这里全绿，才允许接进 MoviePilot。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "plugins.v3"))

# analyzer / deleter 是刻意做成「不依赖 MoviePilot 运行时」的纯逻辑模块。
# 但如果用 `from scrapecleaner import analyzer` 导入，Python 会先执行包的
# __init__.py（其中 import 了 apscheduler 等宿主依赖），在没装宿主环境的
# 机器上就跑不了。这里手工构造一个包壳，让纯逻辑模块可以独立加载。
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


_pure = _load_pure_modules()
an = _pure["analyzer"]


def build(root: Path, specs):
    """按 (相对路径, 字节数) 列表创建文件。使用正确的路径拼接。"""
    for rel, size in specs:
        target = root.joinpath(*rel.replace("\\", "/").split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * size)


SCENARIOS = {
    # 健康的剧集目录：分区目录、季目录、双集
    "健康剧集": [
        ("剧A (2020)/tvshow.nfo", 100),
        ("剧A (2020)/poster.jpg", 100),
        ("剧A (2020)/Season 01/season.nfo", 100),
        ("剧A (2020)/Season 01/A.S01E01.mkv", 2000),
        ("剧A (2020)/Season 01/A.S01E01.nfo", 100),
        ("剧A (2020)/Season 01/A.S01E01.chs.srt", 100),
        ("剧A (2020)/Season 01/A.S01E02.mkv", 2000),
        ("剧A (2020)/Season 01/A.S01E02.nfo", 100),
    ],
    # E02 视频被删，刮削文件残留
    "部分残留": [
        ("剧B (2021)/tvshow.nfo", 100),
        ("剧B (2021)/Season 01/B.S01E01.mkv", 2000),
        ("剧B (2021)/Season 01/B.S01E01.nfo", 100),
        ("剧B (2021)/Season 01/B.S01E02.nfo", 100),
        ("剧B (2021)/Season 01/B.S01E02.srt", 100),
        ("剧B (2021)/Season 01/B.S01E02-thumb.jpg", 100),
    ],
    # 整部剧蒸发，只剩刮削文件
    "整剧残留": [
        ("剧C (2019)/tvshow.nfo", 100),
        ("剧C (2019)/poster.jpg", 100),
        ("剧C (2019)/fanart.jpg", 100),
        ("剧C (2019)/Season 01/season.nfo", 100),
        ("剧C (2019)/Season 01/C.S01E01.nfo", 100),
    ],
    # 健康电影
    "健康电影": [
        ("电影D (2018)/movie.mkv", 2000),
        ("电影D (2018)/movie.nfo", 100),
        ("电影D (2018)/poster.jpg", 100),
        ("电影D (2018)/subs.zh.srt", 100),
    ],
    # 电影被删，只剩刮削
    "电影残留": [
        ("电影E (2017)/movie.nfo", 100),
        ("电影E (2017)/poster.jpg", 100),
        ("电影E (2017)/fanart.jpg", 100),
    ],
    # 受保护文件
    "受保护": [
        ("电影F (2016)/F.mkv", 2000),
        ("电影F (2016)/theme.mp3", 100),
        ("电影F (2016)/F.trickplay/a.jpg", 100),
    ],
    # 陷阱：字幕命名与媒体不一致（无法自动归属，应判残留但需人工确认）
    "命名不一致": [
        ("剧G (2022)/Season 01/G.S01E01.mkv", 2000),
        ("剧G (2022)/Season 01/1.简体.srt", 100),
        ("剧G (2022)/Season 01/G.S01E10.mkv", 2000),
    ],
    # 多版本共用一个目录：1080p 和 2160p 同名不同后缀
    "多版本": [
        ("电影H (2015)/H.1080p.mkv", 2000),
        ("电影H (2015)/H.2160p.mkv", 2000),
        ("电影H (2015)/H.1080p.nfo", 100),
        ("电影H (2015)/H.2160p.nfo", 100),
        ("电影H (2015)/poster.jpg", 100),
    ],
    # 空目录（无任何文件）
    "空壳": [
        ("剧I (2014)/Season 01/.keep", 0),
    ],
    # 季集号兜底：视频用拼音命名，字幕只写 S01E01，两者名字对不上但同属一集
    "季集号兜底": [
        ("剧J (2020)/Season 01/Chen.Mo.De.Zhen.Xiang.S01E01.mkv", 2000),
        ("剧J (2020)/Season 01/S01E01.chs.srt", 100),
        ("剧J (2020)/Season 01/S01E01.cht.ass", 100),
        ("剧J (2020)/Season 01/S01E02.mkv", 2000),
        ("剧J (2020)/Season 01/S01E02.srt", 100),
    ],
    # 季集号不应跨集误认：E03 的视频删了，字幕不能因为存在 E01 就被认领
    "季集号不跨集": [
        ("剧K (2019)/Season 02/K.S02E01.mkv", 2000),
        ("剧K (2019)/Season 02/S02E03.chs.srt", 100),
    ],
    # 季号必须对齐：S02E01 的视频不能认领 S01E01 的字幕
    "季号必须对齐": [
        ("剧L (2018)/Season 02/L.S02E01.mkv", 2000),
        ("剧L (2018)/Season 02/S01E01.chs.srt", 100),
    ],
    # 中文季集写法
    "中文季集": [
        ("剧M (2017)/Season 01/M第01集.mkv", 2000),
        ("剧M (2017)/Season 01/第01集.srt", 100),
    ],
}

EXPECT_ORPHANS = {
    "部分残留": {
        "剧B (2021)/Season 01/B.S01E02.nfo",
        "剧B (2021)/Season 01/B.S01E02.srt",
        "剧B (2021)/Season 01/B.S01E02-thumb.jpg",
    },
    "整剧残留": {
        "剧C (2019)/tvshow.nfo",
        "剧C (2019)/poster.jpg",
        "剧C (2019)/fanart.jpg",
        "剧C (2019)/Season 01/season.nfo",
        "剧C (2019)/Season 01/C.S01E01.nfo",
    },
    "电影残留": {
        "电影E (2017)/movie.nfo",
        "电影E (2017)/poster.jpg",
        "电影E (2017)/fanart.jpg",
    },
    "命名不一致": {"剧G (2022)/Season 01/1.简体.srt"},
    "健康剧集": set(),
    "健康电影": set(),
    "受保护": set(),
    "多版本": set(),
    "空壳": set(),
    # 季集号兜底：三份字幕都应被认领，不是残留
    "季集号兜底": set(),
    # E03 没有对应视频，E01 的存在不能救它
    "季集号不跨集": {"剧K (2019)/Season 02/S02E03.chs.srt"},
    # 季号不匹配 → 字幕确实无主
    "季号必须对齐": {"剧L (2018)/Season 02/S01E01.chs.srt"},
    # 「第01集」写法应能互相匹配
    "中文季集": set(),
}


def main() -> int:
    failures = 0
    for name, specs in SCENARIOS.items():
        root = Path(tempfile.mkdtemp(prefix="scr_")) / "lib"
        root.mkdir()
        try:
            build(root, specs)
            report = an.analyze_directory(str(root))
            got = {
                Path(entry.path).relative_to(root).as_posix()
                for entry in report.orphans
            }
            want = EXPECT_ORPHANS[name]
            status = "OK  " if got == want else "FAIL"
            if got != want:
                failures += 1
            print(f"[{status}] {name}: 残留 {len(got)}/{len(want)} 个, 扫描 {report.scanned_files} 文件")
            if got != want:
                print(f"        缺少: {sorted(want - got)}")
                print(f"        多余: {sorted(got - want)}")
            if name in ("整剧残留", "部分残留", "电影残留"):
                empty = report.empty_dirs()
                rel_empty = sorted(Path(d).relative_to(root).as_posix() for d in empty)
                print(f"        会变空的目录: {rel_empty}")
        finally:
            shutil.rmtree(root.parent, ignore_errors=True)

    print()
    if failures:
        print(f"❌ {failures} 个场景不符合预期")
        return 1
    print("✅ 全部场景通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
