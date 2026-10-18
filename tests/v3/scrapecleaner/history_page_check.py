# -*- coding: utf-8 -*-
"""
整理历史页面的端到端测试
------------------------
验证「按整理历史删除」这条主路径：

1. 整理历史列表能渲染出正确的状态
2. 按 id 删除时只用数据库里的真实数据，不信任前端
3. 目标文件还在的记录不会被删
4. 同名刮削文件被一起清理
5. 整理记录本身也被清理（且能识别宿主的静默保护）
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PKG_DIR = _REPO_ROOT / "plugins.v3" / "scrapecleaner"
sys.path.insert(0, str(_PKG_DIR))

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"[OK  ] {name}")
    else:
        print(f"[FAIL] {name}  {detail}")
        FAILURES.append(name)


import contract_check  # noqa: E402

plugin_module = contract_check._load_plugin()
cls = plugin_module.ScrapeCleaner
DeleteMode = plugin_module.DeleteMode


class FakeRecord:
    """模拟宿主的 TransferHistory 记录。"""

    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.title = kw.get("title", "")
        self.year = kw.get("year", "")
        self.type = kw.get("type", "")
        self.seasons = kw.get("seasons", "")
        self.episodes = kw.get("episodes", "")
        self.image = kw.get("image", "")
        self.src = kw.get("src", "")
        self.dest = kw.get("dest", "")
        self.date = kw.get("date", "2026-01-01 00:00:00")
        self.transfer_task_id = kw.get("transfer_task_id", None)


class FakeAdapter:
    """模拟 TransferHistoryAdapter，用一个内存字典当数据库。"""

    records: dict = {}

    def __init__(self):
        self.available = True
        self.error = None

    def fetch_all(self, since="1970-01-01"):
        return [r for r in self.records.values() if r.date > since]

    def delete_record(self, history_id):
        record = self.records.get(int(history_id))
        if record is None:
            return
        # 模拟宿主：带 transfer_task_id 的记录删不掉（静默无效）
        if record.transfer_task_id:
            return
        del self.records[int(history_id)]

    def record_exists(self, history_id):
        return int(history_id) in self.records


def make_instance(lib: Path, **overrides):
    inst = cls()
    data_dir = lib.parent / "_data"
    data_dir.mkdir(exist_ok=True)
    inst.get_data_path = lambda: data_dir
    inst.init_plugin({
        "enabled": True,
        "library_dirs": str(lib),
        "delete_mode": DeleteMode.REMOVE,
        "min_age_minutes": 0,
        **overrides,
    })
    return inst


class Lib:
    def __enter__(self):
        self.base = Path(tempfile.mkdtemp(prefix="scr_hpage_"))
        self.lib = self.base / "lib"
        self.lib.mkdir()
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.base, ignore_errors=True)
        return False

    def write(self, rel, size=100):
        p = self.lib.joinpath(*rel.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        return p


def install_fake_adapter():
    """把插件的 TransferHistoryAdapter 替换成内存假实现。"""
    plugin_module.TransferHistoryAdapter = FakeAdapter
    FakeAdapter.records = {}


def case_history_list():
    """整理历史列表：状态标注正确。"""
    with Lib() as c:
        alive = c.write("电影/还在/movie.mkv")
        c.write("电影/还在/movie.nfo")
        gone_parent = c.write("电影/已删/old.nfo")
        gone_dest = str(gone_parent.parent / "old.mkv")  # 目标视频不存在

        FakeAdapter.records = {
            1: FakeRecord(id=1, title="还在的片", dest=str(alive), src="/dl/a.mkv",
                          date="2026-03-01 10:00:00"),
            2: FakeRecord(id=2, title="已删的片", dest=gone_dest, src="/dl/b.mkv",
                          date="2026-02-01 10:00:00"),
            3: FakeRecord(id=3, title="任务片", dest="/lib/task.mkv",
                          transfer_task_id="t1", date="2026-01-01 10:00:00"),
        }
        inst = make_instance(c.lib)
        r = inst.api_history_list()
        check("API 成功", r.success is True, str(r.message))
        data = r.data or {}
        summary = data.get("summary") or {}
        check("总数 3", summary.get("total") == 3, str(summary))
        check("正常 1 条", summary.get("alive") == 1, str(summary))
        check("丢失 1 条", summary.get("missing") == 1, str(summary))
        check("无法判断 1 条（任务记录）", summary.get("unknown") == 1, str(summary))

        items = {item["id"]: item for item in data.get("items") or []}
        check("正常记录可删 = False", items[1]["deletable"] is False, str(items[1]))
        check("丢失记录可删 = True", items[2]["deletable"] is True, str(items[2]))
        check("任务记录可删 = False", items[3]["deletable"] is False, str(items[3]))
        check("有中文状态标签", items[2]["state_label"] == "🗑️ 文件已丢失", items[2]["state_label"])

        # 排序：最新在前
        order = [item["id"] for item in data.get("items") or []]
        check("按日期倒序", order == [1, 2, 3], str(order))

    print()
    return


def case_delete_by_history():
    """按整理历史删除：只删失效的，刮削文件一起走。"""
    with Lib() as c:
        # 记录 1：正常的片
        alive_mkv = c.write("电影/还在/movie.mkv", 2000)
        alive_nfo = c.write("电影/还在/movie.nfo")
        # 记录 2：视频没了，只剩刮削
        gone_dir = c.lib / "电影" / "已删"
        gone_dir.mkdir(parents=True)
        gone_nfo = gone_dir / "old.nfo"
        gone_nfo.write_bytes(b"n" * 100)
        gone_srt = gone_dir / "old.chs.srt"
        gone_srt.write_bytes(b"s" * 100)
        gone_thumb = gone_dir / "old-thumb.jpg"
        gone_thumb.write_bytes(b"j" * 100)
        gone_dest = str(gone_dir / "old.mkv")

        FakeAdapter.records = {
            1: FakeRecord(id=1, title="还在的片", dest=str(alive_mkv), date="2026-03-01"),
            2: FakeRecord(id=2, title="已删的片", dest=gone_dest, date="2026-02-01"),
        }
        inst = make_instance(c.lib)

        # 只删失效的那条
        r = inst.api_history_delete({"ids": [2]})
        check("API 成功", r.success is True, str(r.message))
        data = r.data or {}
        check("删了 3 个刮削文件", data.get("deleted_count") == 3, str(data))
        check("nfo 已删", not gone_nfo.exists())
        check("srt 已删", not gone_srt.exists())
        check("thumb 已删", not gone_thumb.exists())
        check("正常文件没被删", alive_mkv.exists() and alive_nfo.exists())
        check("整理记录 1 条已删", data.get("removed_records") == 1, str(data))
        check("生成空目录被清理", not gone_dir.exists(), str(data.get("removed_dirs")))

    print()

    # 不信任前端：传正常记录的 id 也不会删掉它的文件
    with Lib() as c:
        alive_mkv = c.write("电影/还在/movie.mkv", 2000)
        c.write("电影/还在/movie.nfo")
        FakeAdapter.records = {
            1: FakeRecord(id=1, title="还在的片", dest=str(alive_mkv), date="2026-03-01"),
        }
        inst = make_instance(c.lib)
        r = inst.api_history_delete({"ids": [1]})
        data = r.data or {}
        check("目标还在时删除被跳过", data.get("deleted_count") == 0, str(data))
        check("文件完好", alive_mkv.exists())
        check("跳过了正常记录", (data.get("skipped") or []), str(data.get("skipped")))

    print()

    # 伪造 id 不会影响其它记录
    with Lib() as c:
        c.write("电影/x/a.nfo")
        FakeAdapter.records = {1: FakeRecord(id=1, title="t", dest="/lib/nope.mkv")}
        inst = make_instance(c.lib)
        r = inst.api_history_delete({"ids": [999, 888]})
        check("不存在的 id 有明确提示", r.success is False, str(r.message))
        check("没有文件被误删", (c.lib / "电影/x/a.nfo").exists())


def case_delete_records_protected():
    """属于转移任务的整理记录，宿主会保护，需要被识别出来。"""
    with Lib() as c:
        gone = c.lib / "电影" / "gone.nfo"
        gone.parent.mkdir(parents=True)
        gone.write_bytes(b"n")
        FakeAdapter.records = {
            5: FakeRecord(id=5, title="任务片", dest=str(gone.parent / "gone.mkv"),
                          transfer_task_id="task-9", date="2026-01-01"),
        }
        inst = make_instance(c.lib)
        r = inst.api_history_delete({"ids": [5]})
        data = r.data or {}
        # 任务记录状态是 UNKNOWN，不满足删除条件
        check("任务记录被跳过不删", data.get("deleted_count") == 0, str(data))
        check("刮削文件保留", gone.exists())
        check("记录仍在", 5 in FakeAdapter.records)


def case_recycle_mode():
    """回收站模式下按整理历史删除，文件进回收目录。"""
    with Lib() as c:
        gone_dir = c.lib / "电影" / "已删"
        gone_dir.mkdir(parents=True)
        nfo = gone_dir / "old.nfo"
        nfo.write_bytes(b"n" * 50)
        recycle = c.base / "recycle"

        FakeAdapter.records = {
            7: FakeRecord(id=7, title="回收测试", dest=str(gone_dir / "old.mkv"),
                          date="2026-01-01"),
        }
        inst = make_instance(c.lib, delete_mode=DeleteMode.RECYCLE,
                             recycle_dir=str(recycle))
        r = inst.api_history_delete({"ids": [7]})
        data = r.data or {}
        check("回收成功", data.get("deleted_count") == 1, str(data))
        check("原位置没了", not nfo.exists())
        recovered = [p for p in recycle.rglob("*") if p.is_file()]
        check("回收目录里有文件", len(recovered) == 1, str([p.name for p in recovered]))


def case_page_renders():
    """详情页能渲染出整理历史列表。"""
    with Lib() as c:
        alive = c.write("电影/还在/movie.mkv", 2000)
        FakeAdapter.records = {
            1: FakeRecord(id=1, title="还在的片", dest=str(alive), date="2026-03-01"),
            2: FakeRecord(id=2, title="已删的片", dest="/lib/gone.mkv", date="2026-02-01"),
        }
        inst = make_instance(c.lib)
        page = inst.get_page()
        check("get_page 返回 list", isinstance(page, list), str(type(page)))
        import json
        json.dumps(page, ensure_ascii=False)
        check("页面可 JSON 序列化", True)

        texts = json.dumps(page, ensure_ascii=False)
        check("有整理历史标题", "整理历史" in texts, "")
        check("显示了记录标题", "已删的片" in texts or "还在的片" in texts, "")
        check("有删除按钮", "/history-delete" in texts, "")
        check("有扫描对账部分", "扫描对账" in texts, "")

        # 检查 events 契约
        events = []
        def walk(node):
            if isinstance(node, dict):
                if "events" in node and isinstance(node["events"], dict):
                    for v in node["events"].values():
                        events.append(v)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(page)
        check("有交互事件", bool(events), str(len(events)))
        ok = all(
            isinstance(e, dict) and str(e.get("api", "")).startswith("plugin/")
            for e in events
        )
        check("所有事件 api 路径正确", ok, str(events[:2]))


def main() -> int:
    install_fake_adapter()
    for name, func in [
        ("整理历史列表", case_history_list),
        ("按历史删除", case_delete_by_history),
        ("任务记录保护", case_delete_records_protected),
        ("回收站模式", case_recycle_mode),
        ("详情页渲染", case_page_renders),
    ]:
        print(f"\n=== {name} ===")
        try:
            func()
        except Exception as error:
            import traceback
            traceback.print_exc()
            print(f"[FAIL] {name} 抛异常: {error}")
            FAILURES.append(name)

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过: {FAILURES}")
        return 1
    print("✅ 整理历史页面测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
