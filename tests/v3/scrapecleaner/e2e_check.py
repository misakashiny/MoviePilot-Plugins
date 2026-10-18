# -*- coding: utf-8 -*-
"""
插件主类的端到端测试
--------------------
复用 contract_check 的最小宿主 mock，但更进一步：真实创建目录树，
走完「配置 → 扫描 → 存报告 → 删除 → 报告更新 → 历史记录」全链路。

测的是 `ScrapeCleaner` 这个集成层，不是纯逻辑模块。
"""
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


# 复用 contract_check 里的宿主 mock 与加载器
import contract_check  # noqa: E402

plugin_module = contract_check._load_plugin()
cls = plugin_module.ScrapeCleaner
DeleteMode = plugin_module.DeleteMode


def make_instance(lib: Path, **overrides):
    """构造一个启用状态、指向临时媒体库的插件实例。"""
    instance = cls()
    config = {
        "enabled": True,
        "library_dirs": str(lib),
        "delete_mode": DeleteMode.REMOVE,
        "remove_empty_dirs": True,
        # 本测试关注删除流程本身，关掉新文件保护期（默认 1 天）以免
        # 刚创建的测试文件被判为「受保护」。保护期本身在 features_check 里单测。
        "min_age_minutes": 0,
        **overrides,
    }
    # get_data_path 依赖插件数据目录；mock 下用临时目录
    instance.get_data_path = lambda: lib.parent / "_data"
    (lib.parent / "_data").mkdir(exist_ok=True)
    instance.init_plugin(config)
    return instance


def build_library(root: Path):
    """构造：一个健康电影 + 一个只剩刮削的电影 + 一个残留剧集目录。"""
    files = {
        "电影/健康片/healthy.mkv": 2000,
        "电影/健康片/healthy.nfo": 100,
        "电影/健康片/poster.jpg": 100,
        "电影/已删除片/gone.nfo": 100,
        "电影/已删除片/poster.jpg": 100,
        "电影/已删除片/fanart.jpg": 100,
    }
    for rel, size in files.items():
        target = root.joinpath(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * size)


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="scr_e2e_"))
    try:
        lib = work / "lib"
        lib.mkdir()
        build_library(lib)

        print("=== 1. 扫描并出报告 ===")
        instance = make_instance(lib)
        result = instance.api_scan()
        check("api_scan 成功", result.success is True, str(result.message))
        data = result.data or {}
        check("扫出 3 个残留", data.get("orphan_count") == 3,
              f"实际 {data.get('orphan_count')}: {data.get('groups')}")
        # healthy.nfo 按同名匹配保留；poster.jpg 是目录级文件、目录里有视频所以也保留
        check("扫出 2 个保留", data.get("kept_count") == 2, str(data.get("kept_count")))
        check("报告有分组", len(data.get("groups") or []) >= 1, str(len(data.get("groups") or [])))
        check("报告有 scan_time", bool(data.get("scan_time")), repr(data.get("scan_time")))

        print("\n=== 2. 报告已持久化到插件数据 ===")
        saved = instance.get_data("last_report")
        check("last_report 已保存", isinstance(saved, dict) and saved.get("entries"),
              str(type(saved)))
        # 3 个残留 + 2 个保留 = 5 条
        check("持久化条目数正确", len((saved or {}).get("entries") or []) == 5,
              str(len((saved or {}).get("entries") or [])))

        print("\n=== 3. 拒绝危险请求 ===")
        r = instance.api_delete({"paths": []})
        check("空清单被拒绝", r.success is False, str(r.message))
        r = instance.api_delete({"paths": [str(lib / "电影/健康片/healthy.nfo")]})
        check("删除正常刮削文件被跳过",
              (r.data or {}).get("deleted", 0) == 0 and (r.data or {}).get("skipped"),
              str(r.data))
        check("正常文件仍在", (lib / "电影/健康片/healthy.nfo").exists())
        r = instance.api_delete({"paths": ["/etc/passwd"]})
        check("库外路径被拒绝", (r.data or {}).get("deleted", 0) == 0, str(r.data))

        print("\n=== 4. 正常删除 ===")
        orphans = [entry.path for entry in instance._load_report().orphans]
        r = instance.api_delete({"paths": orphans})
        check("删除成功", r.success is True, str(r.message))
        check("删除了 3 个", (r.data or {}).get("deleted") == 3, str(r.data))
        check("文件真的没了", not any(Path(p).exists() for p in orphans))
        check("健康文件保留", (lib / "电影/健康片/healthy.mkv").exists()
              and (lib / "电影/健康片/healthy.nfo").exists())
        check("空目录被清理", not (lib / "电影/已删除片").exists(),
              str((r.data or {}).get("removed_dirs")))

        print("\n=== 5. 报告与历史已更新 ===")
        after = instance.api_report()
        check("报告的残留清零", (after.data or {}).get("orphan_count") == 0,
              str((after.data or {}).get("orphan_count")))
        history = instance.api_history()
        # 第 3 步「拒绝危险请求」也会记一条（被跳过也是可审计的操作）
        check("历史记录累计 2 条", len(history.data or []) == 2, str(history.data))
        check("最新一条记录了删除数", (history.data or [{}])[0].get("deleted") == 3,
              str((history.data or [{}])[0]))

        print("\n=== 6. 重复删除不会报错 ===")
        r = instance.api_delete({"paths": orphans})
        check("重复删除全部跳过", (r.data or {}).get("deleted", 0) == 0, str(r.data))

        print("\n=== 7. 报告过期（改了媒体库目录）给出可诊断原因 ===")
        # 用独立库，避免受前面已删文件的影响
        work_stale = Path(tempfile.mkdtemp(prefix="scr_e2e_stale_"))
        try:
            lib_stale = work_stale / "lib"
            lib_stale.mkdir()
            build_library(lib_stale)
            instance2 = make_instance(lib_stale)
            instance2.api_scan()
            old_orphans = [entry.path for entry in instance2._load_report().orphans]
            check("过期测试前有残留", len(old_orphans) == 3, str(len(old_orphans)))

            other = work_stale / "别的库"
            other.mkdir()
            instance2._library_dirs = str(other)
            r = instance2.api_delete({"paths": old_orphans})
            skipped = (r.data or {}).get("skipped") or []
            check("全部被跳过", (r.data or {}).get("deleted", 0) == 0, str(r.data))
            check("提示了重新扫描",
                  any("重新扫描" in str(item.get("reason", "")) for item in skipped),
                  str(skipped[:2]))
            check("文件真的没被删", all(Path(p).exists() for p in old_orphans))
        finally:
            shutil.rmtree(work_stale, ignore_errors=True)

        print("\n=== 8. 回收站模式可恢复 ===")
        work2 = Path(tempfile.mkdtemp(prefix="scr_e2e2_"))
        try:
            lib2 = work2 / "lib"
            lib2.mkdir()
            build_library(lib2)
            recycle = work2 / "recycle"
            instance3 = make_instance(lib2, delete_mode=DeleteMode.RECYCLE,
                                      recycle_dir=str(recycle))
            instance3.api_scan()
            orphans3 = [entry.path for entry in instance3._load_report().orphans]
            r = instance3.api_delete({"paths": orphans3})
            check("回收成功", (r.data or {}).get("recycled") == 3, str(r.data))
            check("原位置已清空", not any(Path(p).exists() for p in orphans3))
            recovered = [p for p in recycle.rglob("*") if p.is_file()]
            check("回收目录里有 3 个文件", len(recovered) == 3,
                  str([p.name for p in recovered]))
            # 模拟"恢复"：把文件挪回去
            for src in recovered:
                name = src.name
                dest = lib2 / "电影/已删除片" / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
            check("文件可以恢复", (lib2 / "电影/已删除片/gone.nfo").exists())
        finally:
            shutil.rmtree(work2, ignore_errors=True)

        print("\n=== 9. 未启用时 API 拒绝 ===")
        instance4 = cls()
        instance4.get_data_path = lambda: work / "_data4"
        instance4.init_plugin({"enabled": False, "library_dirs": str(lib)})
        r = instance4.api_scan()
        check("未启用时扫描被拒绝", r.success is False, str(r.message))

    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过: {FAILURES}")
        return 1
    print("✅ 端到端测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
