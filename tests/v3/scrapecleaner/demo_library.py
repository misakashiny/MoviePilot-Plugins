# -*- coding: utf-8 -*-
"""
真实感媒体库演练
----------------
构造一个接近真实使用、且**混入了各种刁钻情况**的媒体库，跑完整扫描并打印
分组报告，人工核对判定是否符合直觉。

这个脚本不 assert，只输出——它用来「看一眼结果对不对」，
自动断言由 scenario_check.py 负责。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PKG_DIR = _REPO_ROOT / "plugins.v3" / "scrapecleaner"

import importlib.util  # noqa: E402
import types  # noqa: E402

_package = types.ModuleType("scrapecleaner")
_package.__path__ = [str(_PKG_DIR)]
sys.modules["scrapecleaner"] = _package
for _name in ("analyzer", "deleter"):
    _spec = importlib.util.spec_from_file_location(
        f"scrapecleaner.{_name}", _PKG_DIR / f"{_name}.py"
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[f"scrapecleaner.{_name}"] = _mod
    _spec.loader.exec_module(_mod)
    setattr(_package, _name, _mod)

an = sys.modules["scrapecleaner.analyzer"]


# 每个条目: (相对路径, 字节数)。用真实感的文件名。
LIBRARY = [
    # ===== 正常剧集：EMBY 风格结构 =====
    ("剧集/权力的游戏 (2011)/tvshow.nfo", 4000),
    ("剧集/权力的游戏 (2011)/poster.jpg", 300000),
    ("剧集/权力的游戏 (2011)/fanart.jpg", 800000),
    ("剧集/权力的游戏 (2011)/Season 01/season.nfo", 500),
    ("剧集/权力的游戏 (2011)/Season 01/season01-poster.jpg", 200000),
    ("剧集/权力的游戏 (2011)/Season 01/Game.of.Thrones.S01E01.1080p.BluRay.mkv", 3000000000),
    ("剧集/权力的游戏 (2011)/Season 01/Game.of.Thrones.S01E01.1080p.BluRay.nfo", 3000),
    ("剧集/权力的游戏 (2011)/Season 01/Game.of.Thrones.S01E01.1080p.BluRay.chs.srt", 40000),
    ("剧集/权力的游戏 (2011)/Season 01/Game.of.Thrones.S01E01.1080p.BluRay.cht.ass", 50000),
    ("剧集/权力的游戏 (2011)/Season 01/Game.of.Thrones.S01E02.1080p.BluRay.mkv", 3000000000),
    ("剧集/权力的游戏 (2011)/Season 01/Game.of.Thrones.S01E02.1080p.BluRay.nfo", 3000),

    # ===== 剧集：E03 被删了，残留刮削 =====
    ("剧集/绝命毒师 (2008)/tvshow.nfo", 4000),
    ("剧集/绝命毒师 (2008)/poster.jpg", 300000),
    ("剧集/绝命毒师 (2008)/Season 02/season.nfo", 500),
    ("剧集/绝命毒师 (2008)/Season 02/Breaking.Bad.S02E01.720p.mkv", 1500000000),
    ("剧集/绝命毒师 (2008)/Season 02/Breaking.Bad.S02E01.720p.nfo", 3000),
    ("剧集/绝命毒师 (2008)/Season 02/Breaking.Bad.S02E03.720p.nfo", 3000),
    ("剧集/绝命毒师 (2008)/Season 02/Breaking.Bad.S02E03.720p.chs.srt", 40000),
    ("剧集/绝命毒师 (2008)/Season 02/Breaking.Bad.S02E03.720p-thumb.jpg", 150000),

    # ===== 剧集：整部被删，只剩刮削（典型存量残留）=====
    ("剧集/绝命律师 (2015)/tvshow.nfo", 4000),
    ("剧集/绝命律师 (2015)/poster.jpg", 300000),
    ("剧集/绝命律师 (2015)/fanart.jpg", 800000),
    ("剧集/绝命律师 (2015)/Season 01/season.nfo", 500),
    ("剧集/绝命律师 (2015)/Season 01/Better.Call.Saul.S01E01.nfo", 3000),
    ("剧集/绝命律师 (2015)/Season 01/Better.Call.Saul.S01E01.chs.srt", 40000),

    # ===== 正常电影 =====
    ("电影/肖申克的救赎 (1994)/The.Shawshank.Redemption.1994.1080p.mkv", 8000000000),
    ("电影/肖申克的救赎 (1994)/The.Shawshank.Redemption.1994.1080p.nfo", 3500),
    ("电影/肖申克的救赎 (1994)/poster.jpg", 300000),
    ("电影/肖申克的救赎 (1994)/fanart.jpg", 800000),
    ("电影/肖申克的救赎 (1994)/subs.zh.srt", 40000),
    ("电影/肖申克的救赎 (1994)/subs.en.srt", 45000),

    # ===== 电影被删，只剩刮削 =====
    ("电影/消失的爱人 (2014)/Gone.Girl.2014.1080p.nfo", 3500),
    ("电影/消失的爱人 (2014)/poster.jpg", 300000),
    ("电影/消失的爱人 (2014)/fanart.jpg", 800000),

    # ===== 多版本共存 =====
    ("电影/沙丘 (2021)/Dune.2021.1080p.mkv", 8000000000),
    ("电影/沙丘 (2021)/Dune.2021.2160p.mkv", 25000000000),
    ("电影/沙丘 (2021)/Dune.2021.1080p.nfo", 3500),
    ("电影/沙丘 (2021)/Dune.2021.2160p.nfo", 3500),
    ("电影/沙丘 (2021)/poster.jpg", 300000),

    # ===== 受保护内容 =====
    ("电影/奥本海默 (2023)/Oppenheimer.2023.2160p.mkv", 30000000000),
    ("电影/奥本海默 (2023)/theme.mp3", 5000000),
    ("电影/奥本海默 (2023)/Oppenheimer.2023.2160p.trickplay/320x180-00001.jpg", 20000),

    # ===== 刁钻：字幕命名与视频不一致 =====
    ("剧集/沉默的真相 (2020)/Season 01/Chen.Mo.De.Zhen.Xiang.S01E01.mkv", 2000000000),
    ("剧集/沉默的真相 (2020)/Season 01/S01E01.chs.srt", 40000),

    # ===== 刁钻：季目录里只剩刮削，但剧集根目录还有别的季 =====
    ("剧集/三体 (2023)/tvshow.nfo", 4000),
    ("剧集/三体 (2023)/poster.jpg", 300000),
    ("剧集/三体 (2023)/Season 01/San.Ti.S01E01.mkv", 2000000000),
    ("剧集/三体 (2023)/Season 01/San.Ti.S01E01.nfo", 3000),
    ("剧集/三体 (2023)/Season 02/season.nfo", 500),
    ("剧集/三体 (2023)/Season 02/San.Ti.S02E01.nfo", 3000),
    ("剧集/三体 (2023)/Season 02/San.Ti.S02E01.chs.srt", 40000),

    # ===== 系统目录（应被跳过）=====
    ("电影/@eaDir/poster.jpg", 1000),
]


def build(root: Path):
    for rel, size in LIBRARY:
        target = root.joinpath(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        # 用稀疏文件避免真的写几十 GB
        with open(target, "wb") as fh:
            fh.truncate(size)


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="scr_demo_"))
    lib = base / "media"
    lib.mkdir()
    build(lib)
    try:
        report = an.analyze_directory(str(lib))
        orphans = report.orphans
        kept = report.kept

        print("=" * 78)
        print(f"扫描文件数: {report.scanned_files}  条目: {len(report.entries)}")
        print(f"残留: {len(orphans)} 个 / {_human(report.orphan_bytes)}")
        print(f"保留: {len(kept)} 个")
        if report.skipped_dirs:
            print(f"跳过的系统目录: {[Path(d).name for d in report.skipped_dirs]}")
        if report.errors:
            print(f"错误: {report.errors[:3]}")

        print("\n" + "=" * 78)
        print("【判定为残留的】")
        for entry in orphans:
            rel = Path(entry.path).relative_to(lib).as_posix()
            print(f"  {_REASON[entry.reason.value]:<12} {rel}")
            print(f"  {'':<12}   ↳ {entry.detail}")

        print("\n" + "=" * 78)
        print("【判定为保留的】")
        for entry in kept:
            rel = Path(entry.path).relative_to(lib).as_posix()
            print(f"  {_REASON[entry.reason.value]:<12} {rel}")

        print("\n" + "=" * 78)
        print("【删完会变空的目录】")
        for directory in report.empty_dirs():
            print("  ", Path(directory).relative_to(lib).as_posix())
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return 0


_REASON = {
    "protected": "受保护",
    "media_present": "有媒体",
    "dir_alive": "目录存活",
    "orphan_stem": "★残留",
    "orphan_dir_shared": "★残留(目录级)",
    "generic_ambiguous": "通用名跳过",
}


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TB"


if __name__ == "__main__":
    sys.exit(main())
