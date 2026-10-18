# -*- coding: utf-8 -*-
"""history_view 测试：整理历史列表的判定与筛选。"""
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
for _name in ("analyzer", "history_view"):
    _spec = importlib.util.spec_from_file_location(
        f"scrapecleaner.{_name}", _PKG_DIR / f"{_name}.py"
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[f"scrapecleaner.{_name}"] = _mod
    _spec.loader.exec_module(_mod)
    setattr(_package, _name, _mod)

hv = sys.modules["scrapecleaner.history_view"]

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"[OK  ] {name}")
    else:
        print(f"[FAIL] {name}  {detail}")
        FAILURES.append(name)


class FakeRecord:
    """模拟宿主的 TransferHistory ORM 记录。"""

    def __init__(self, **kwargs):
        self.id = kwargs.get("id", 1)
        self.title = kwargs.get("title", "")
        self.year = kwargs.get("year", "")
        self.type = kwargs.get("type", "")
        self.seasons = kwargs.get("seasons", "")
        self.episodes = kwargs.get("episodes", "")
        self.image = kwargs.get("image", "")
        self.src = kwargs.get("src", "")
        self.dest = kwargs.get("dest", "")
        self.date = kwargs.get("date", "2026-01-01 00:00:00")
        self.transfer_task_id = kwargs.get("transfer_task_id", None)


def main() -> int:
    print("=== 1. 目标存在 → ALIVE ===")
    item = hv.classify_item(
        hv.HistoryItem(history_id=1, title="测试", year="2020", mtype="电影",
                       season="", episode="", image="", src="/dl/a.mkv",
                       dest="/lib/a.mkv", date="2026-01-01"),
        exists=lambda p: True,
    )
    check("状态是 ALIVE", item.state is hv.HistoryState.ALIVE, str(item.state))
    check("不可删除", item.deletable is False)
    check("有说明", item.detail == "目标文件存在", item.detail)

    print("\n=== 2. 目标不存在 → MISSING（要删的那种）===")
    item = hv.classify_item(
        hv.HistoryItem(history_id=2, title="测试", year="2020", mtype="电影",
                       season="", episode="", image="", src="/dl/b.mkv",
                       dest="/lib/b.mkv", date="2026-01-01"),
        exists=lambda p: False,
    )
    check("状态是 MISSING", item.state is hv.HistoryState.MISSING, str(item.state))
    check("可删除", item.deletable is True)
    check("有说明", item.detail == "目标文件已不存在", item.detail)

    print("\n=== 3. 无目标路径 → UNKNOWN ===")
    item = hv.classify_item(
        hv.HistoryItem(history_id=3, title="", year="", mtype="", season="",
                       episode="", image="", src="", dest="", date=""),
        exists=lambda p: False,
    )
    check("状态是 UNKNOWN", item.state is hv.HistoryState.UNKNOWN, str(item.state))
    check("不可删除", item.deletable is False)

    print("\n=== 4. 转移任务记录 → 受保护 ===")
    item = hv.classify_item(
        hv.HistoryItem(history_id=4, title="t", year="", mtype="", season="",
                       episode="", image="", src="", dest="/lib/c.mkv", date="",
                       protected=True),
        exists=lambda p: False,
    )
    check("即使目标不存在也判 UNKNOWN", item.state is hv.HistoryState.UNKNOWN, str(item.state))
    check("不可删除", item.deletable is False)
    check("说明了原因", "转移任务" in item.detail, item.detail)

    print("\n=== 5. 判断过程抛异常 → UNKNOWN（保守）===")
    def boom(p):
        raise OSError("网络不可达")

    item = hv.classify_item(
        hv.HistoryItem(history_id=5, title="", year="", mtype="", season="",
                       episode="", image="", src="", dest="/nas/x.mkv", date=""),
        exists=boom,
    )
    check("异常时判 UNKNOWN", item.state is hv.HistoryState.UNKNOWN, str(item.state))
    check("不可删除", item.deletable is False)
    check("说明里有原因", "无法访问" in item.detail, item.detail)

    print("\n=== 6. ORM 转纯数据 ===")
    raw = FakeRecord(id=10, title="流浪地球", year="2019", type="电影",
                     src="/downloads/x.mkv", dest="/media/movies/x.mkv",
                     date="2026-10-01 12:00:00")
    item = hv.record_to_item(raw)
    check("id 正确", item.history_id == 10, str(item.history_id))
    check("标题正确", item.title == "流浪地球", item.title)
    check("dest 正确", item.dest == "/media/movies/x.mkv", item.dest)
    check("无 transfer_task_id 时不受保护", item.protected is False)

    raw2 = FakeRecord(id=11, transfer_task_id="task-1")
    check("有 transfer_task_id 时受保护", hv.record_to_item(raw2).protected is True)

    print("\n=== 7. build_items 批量 + 排序 ===")
    raws = [
        FakeRecord(id=1, title="A", dest="/lib/a", date="2026-01-01 10:00:00"),
        FakeRecord(id=2, title="B", dest="/lib/b", date="2026-02-01 10:00:00"),
        FakeRecord(id=3, title="C", dest="/lib/c", date="2026-03-01 10:00:00"),
    ]
    exists_map = {"/lib/a": True, "/lib/b": False, "/lib/c": False}
    items = hv.build_items(raws, exists=lambda p: exists_map.get(p, False))
    check("3 条记录", len(items) == 3, str(len(items)))
    check("按日期倒序（最新在前）", items[0].history_id == 3, str([i.history_id for i in items]))
    check("状态各就各位",
          [i.state.value for i in items] == ["missing", "missing", "alive"],
          str([i.state.value for i in items]))

    print("\n=== 8. 汇总统计 ===")
    summary = hv.HistorySummary.from_items(items)
    check("total=3", summary.total == 3, str(summary.total))
    check("alive=1", summary.alive == 1, str(summary.alive))
    check("missing=2", summary.missing == 2, str(summary.missing))

    print("\n=== 9. 筛选 ===")
    check("按状态筛 missing", len(hv.filter_items(items, state="missing")) == 2)
    check("按状态筛 alive", len(hv.filter_items(items, state="alive")) == 1)
    check("空状态返回全部", len(hv.filter_items(items, state="")) == 3)
    check("limit 生效", len(hv.filter_items(items, limit=1)) == 1)
    check("关键字匹配标题", len(hv.filter_items(items, keyword="A")) == 1,
          str([i.title for i in hv.filter_items(items, keyword="A")]))
    check("关键字不区分大小写", len(hv.filter_items(items, keyword="a")) >= 1)

    print("\n=== 10. 季集拼接 ===")
    check("S01E01", hv._season_episode_text("1", "1") == "S01E01",
          hv._season_episode_text("1", "1"))
    check("只有季", hv._season_episode_text("2", "") == "S02")
    check("只有集", hv._season_episode_text("", "5") == "E05")
    check("都为空", hv._season_episode_text("", "") == "")
    check("非数字不崩", hv._season_episode_text("特别篇", "x") == "S特别篇Ex",
          hv._season_episode_text("特别篇", "x"))

    print("\n=== 11. 行数据可 JSON 序列化 ===")
    row = items[0].to_row()
    import json
    json.dumps(row, ensure_ascii=False)
    check("to_row 可序列化", True)
    check("含可删标记", "deletable" in row, str(list(row)))
    check("含中文状态", row["state_label"] in ("✅ 正常", "🗑️ 文件已丢失", "❔ 无法判断"),
          row["state_label"])

    print("\n=== 12. 查找同名刮削文件 ===")
    work = Path(tempfile.mkdtemp(prefix="scr_hv_"))
    try:
        (work / "Show.S01E01.mkv").write_bytes(b"v")
        (work / "Show.S01E01.nfo").write_bytes(b"n")
        (work / "Show.S01E01.chs.srt").write_bytes(b"s")
        (work / "Show.S01E01-thumb.jpg").write_bytes(b"j")
        (work / "Show.S01E02.nfo").write_bytes(b"other")
        (work / "poster.jpg").write_bytes(b"p")

        exts = {".nfo", ".srt", ".jpg"}
        found = hv.collect_sibling_scrap_files(str(work / "Show.S01E01.mkv"), exts)
        names = {Path(p).name for p in found}
        check("找到同名 nfo", "Show.S01E01.nfo" in names, str(names))
        check("找到 chs 字幕", "Show.S01E01.chs.srt" in names, str(names))
        check("找到 thumb 图", "Show.S01E01-thumb.jpg" in names, str(names))
        check("不含其它集", "Show.S01E02.nfo" not in names, str(names))
        check("不含目录级海报", "poster.jpg" not in names, str(names))
        check("不含视频本身", "Show.S01E01.mkv" not in names, str(names))

        check("空路径返回空", hv.collect_sibling_scrap_files("", exts) == [])
        check("目录不存在返回空",
              hv.collect_sibling_scrap_files(str(work / "nope" / "x.mkv"), exts) == [])
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过: {FAILURES}")
        return 1
    print("✅ 整理历史视图测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
