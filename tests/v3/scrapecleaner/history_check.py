# -*- coding: utf-8 -*-
"""history 模块测试：转移记录失效判断与删除复查。"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PKG_DIR = _REPO_ROOT / "plugins.v3" / "scrapecleaner"
sys.path.insert(0, str(_PKG_DIR))

import importlib.util  # noqa: E402
import types  # noqa: E402

_package = types.ModuleType("scrapecleaner")
_package.__path__ = [str(_PKG_DIR)]
sys.modules["scrapecleaner"] = _package
for _name in ("analyzer", "deleter", "history"):
    _spec = importlib.util.spec_from_file_location(
        f"scrapecleaner.{_name}", _PKG_DIR / f"{_name}.py"
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[f"scrapecleaner.{_name}"] = _mod
    _spec.loader.exec_module(_mod)
    setattr(_package, _name, _mod)

hist = sys.modules["scrapecleaner.history"]

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"[OK  ] {name}")
    else:
        print(f"[FAIL] {name}  {detail}")
        FAILURES.append(name)


def rec(history_id, src="", dest="", task=None):
    return hist.HistoryRecord(
        history_id=history_id, src=src, dest=dest, transfer_task_id=task
    )


def main() -> int:
    print("=== 1. 分类：目标还存在的记录不能删 ===")
    exists_map = {"/lib/a.mkv": True, "/lib/b.mkv": False}
    check_fn = lambda p: exists_map.get(p, False)  # noqa: E731
    records = [
        rec(1, dest="/lib/a.mkv"),          # 目标还在 → 保留
        rec(2, dest="/lib/b.mkv"),          # 目标没了、源也没 → 待删
    ]
    cand, prot, alive = hist.classify_records(records, check_fn)
    check("待删 1 条", [r.history_id for r in cand] == [2], str([r.history_id for r in cand]))
    check("存活 1 条", [r.history_id for r in alive] == [1], str([r.history_id for r in alive]))
    check("受保护 0 条", prot == [], str(prot))

    print("\n=== 2. 分类：转移任务记录受保护 ===")
    records = [rec(3, task="task-abc"), rec(4)]
    cand, prot, alive = hist.classify_records(records, lambda p: False)
    check("任务记录被保护", [r.history_id for r in prot] == [3], str([r.history_id for r in prot]))
    check("普通记录待删", [r.history_id for r in cand] == [4], str([r.history_id for r in cand]))

    print("\n=== 3. 分类：源文件还在的不能删 ===")
    src_exists = {"/dl/x.mkv": True}
    records = [rec(5, src="/dl/x.mkv", dest="/lib/gone.mkv")]
    cand, prot, alive = hist.classify_records(records, lambda p: src_exists.get(p, False))
    check("源还在 → 不算失效", [r.history_id for r in alive] == [5], str([r.history_id for r in alive]))

    print("\n=== 4. 删除并复查：宿主静默不删的情况能被识别 ===")
    # 模拟宿主：transfer_task_id 非空时 delete 静默无效
    store = {1: rec(1), 2: rec(2, task="t1")}

    def fake_delete(history_id):
        if store[history_id].transfer_task_id:
            return  # 静默不删，模拟宿主的 WHERE ... IS NULL
        del store[history_id]

    def fake_verify(history_id):
        return history_id in store

    result = hist.clean_transfer_history(
        [rec(1), rec(2, task="t1")],
        delete_func=fake_delete,
        verify_func=fake_verify,
        check_exists=lambda p: False,
    )
    check("删掉了 1 条", result.deleted == [1], str(result.deleted))
    check("没删掉的被识别为受保护", result.protected == [2], str(result.protected))
    check("没有误报失败", result.failed == [], str(result.failed))

    print("\n=== 5. 删除抛异常记入 failed ===")
    def boom(history_id):
        raise RuntimeError("数据库炸了")

    result = hist.clean_transfer_history(
        [rec(10)], delete_func=boom, check_exists=lambda p: False
    )
    check("异常进入 failed", len(result.failed) == 1 and result.failed[0][0] == 10,
          str(result.failed))
    check("没有记录被当成已删", result.deleted == [], str(result.deleted))

    print("\n=== 6. 复查异常不当成成功 ===")
    result = hist.clean_transfer_history(
        [rec(11)],
        delete_func=lambda i: None,
        verify_func=lambda i: (_ for _ in ()).throw(RuntimeError("查不了")),
        check_exists=lambda p: False,
    )
    check("复查异常进入 failed", len(result.failed) == 1, str(result.failed))
    check("未计入已删", result.deleted == [], str(result.deleted))

    print("\n=== 7. 不传复查函数时按已删处理 ===")
    result = hist.clean_transfer_history(
        [rec(12)], delete_func=lambda i: None, check_exists=lambda p: False
    )
    check("无复查时计入已删", result.deleted == [12], str(result.deleted))

    print("\n=== 8. 按目录分组 ===")
    records = [
        rec(20, dest=os.path.join("/lib", "Show", "E01.mkv")),
        rec(21, dest=os.path.join("/lib", "Show", "E02.mkv")),
        rec(22, dest=os.path.join("/lib", "Other", "x.mkv")),
    ]
    grouped = hist.group_by_dest_dir(records)
    check("分成 2 组", len(grouped) == 2, str(list(grouped)))
    show_key = [k for k in grouped if k.endswith("Show")][0]
    check("Show 组有 2 条", len(grouped[show_key]) == 2, str(len(grouped[show_key])))

    print("\n=== 9. dir_has_any_file 真实文件系统 ===")
    work = Path(tempfile.mkdtemp(prefix="scr_hist_"))
    try:
        empty_dir = work / "empty"
        empty_dir.mkdir()
        has_dir = work / "has"
        has_dir.mkdir()
        (has_dir / "a.nfo").write_bytes(b"x")
        sub_only = work / "subonly"
        (sub_only / "inner").mkdir(parents=True)

        check("空目录返回 False", hist.dir_has_any_file(str(empty_dir)) is False)
        check("有文件的目录返回 True", hist.dir_has_any_file(str(has_dir)) is True)
        check("只有子目录返回 False", hist.dir_has_any_file(str(sub_only)) is False)
        check("不存在的目录返回 False", hist.dir_has_any_file(str(work / "nope")) is False)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n=== 10. 空输入不报错 ===")
    result = hist.clean_transfer_history([], delete_func=lambda i: None)
    check("空输入安全", result.total_done == 0 and result.failed == [])

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过: {FAILURES}")
        return 1
    print("✅ 转移记录测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
