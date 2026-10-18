# -*- coding: utf-8 -*-
"""
deleter 的安全阀验证。

每个用例使用**独立**的临时目录，避免前一个用例修改文件系统后影响后一个
（例如把文件写回去会让 V2「清单内」校验失效，但那并不是 bug）。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "plugins.v3"))

# 与 scenario_check 相同的做法：绕过包的 __init__.py，只加载纯逻辑模块，
# 这样即使本机没有装 MoviePilot 宿主依赖也能跑单元测试。
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
dl = _pure["deleter"]

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"[OK  ] {name}")
    else:
        print(f"[FAIL] {name}  {detail}")
        FAILURES.append(name)


def build(root: Path, specs):
    for rel, size in specs:
        target = root.joinpath(*rel.replace("\\", "/").split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * size)


class Case:
    """一个独立的临时文件系统。"""

    def __enter__(self):
        self.base = Path(tempfile.mkdtemp(prefix="scr_del_"))
        self.lib = self.base / "library"
        self.lib.mkdir()
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.base, ignore_errors=True)
        return False

    def build(self, specs):
        build(self.lib, specs)


def case_v2_stale_manifest():
    """V2：清单外的正常刮削文件不能被删。"""
    with Case() as c:
        c.build([
            ("健康电影/movie.mkv", 2000),
            ("健康电影/movie.nfo", 100),
        ])
        report = an.analyze_directory(str(c.lib))
        healthy_nfo = str(c.lib / "健康电影" / "movie.nfo")
        res = dl.execute_delete([healthy_nfo], [str(c.lib)], report,
                                mode="remove", options=an.AnalyzerOptions())
        check("V2 正常刮削文件未被删除", os.path.exists(healthy_nfo), str(res))
        check("V2 记录了跳过原因",
              any("不在本次扫描" in reason for _, reason in res.skipped), str(res.skipped))


def case_v1_outside_root():
    """V1：媒体库外的文件不能被删。"""
    with Case() as c:
        victim = c.base / "outside" / "important.nfo"
        victim.parent.mkdir()
        victim.write_bytes(b"y" * 50)
        c.build([("电影E (2017)/movie.nfo", 100)])
        report = an.analyze_directory(str(c.lib))
        res = dl.execute_delete([str(victim)], [str(c.lib)], report,
                                mode="remove", options=an.AnalyzerOptions())
        check("V1 库外文件未被删除", victim.exists(), str(res))
        check("V1 记录了跳过原因",
              any("不在配置的媒体库目录" in reason for _, reason in res.skipped), str(res.skipped))


def case_v3_not_scrap():
    """V3：伪造报告中把媒体文件标为残留，执行层必须拒绝。"""
    with Case() as c:
        c.build([("健康电影/movie.mkv", 2000)])
        media = c.lib / "健康电影" / "movie.mkv"
        forged = an.ScanReport(entries=[
            an.ScrapEntry(path=str(media), size=10,
                          reason=an.Reason.ORPHAN_STEM, detail="伪造的残留条目"),
        ])
        res = dl.execute_delete([str(media)], [str(c.lib)], forged,
                                mode="remove", options=an.AnalyzerOptions())
        check("V3 媒体文件拒绝删除", media.exists(), str(res))
        check("V3 记录了跳过原因",
              any("已不是刮削文件" in reason for _, reason in res.skipped), str(res.skipped))


def case_v5_symlink():
    """V5：符号链接跳过。"""
    with Case() as c:
        c.build([("电影/an.nfo", 100)])
        link = c.lib / "电影" / "link.nfo"
        try:
            os.symlink(c.lib / "电影" / "an.nfo", link)
        except (OSError, NotImplementedError):
            print("[SKIP] V5 当前系统不支持创建符号链接")
            return
        report = an.analyze_directory(str(c.lib))
        res = dl.execute_delete([str(link)], [str(c.lib)], report,
                                mode="remove", options=an.AnalyzerOptions())
        check("V5 符号链接未被删除", os.path.islink(link), str(res))


def case_recycle_happy_path():
    """回收模式的正常流程。"""
    with Case() as c:
        c.build([
            ("电影E (2017)/movie.nfo", 100),
            ("电影E (2017)/poster.jpg", 100),
            ("电影E (2017)/Season 01/ep.nfo", 100),
            ("健康电影/movie.mkv", 2000),
            ("健康电影/movie.nfo", 100),
        ])
        report = an.analyze_directory(str(c.lib))
        orphans = [entry.path for entry in report.orphans]
        check("扫描出 3 个残留", len(orphans) == 3, f"实际 {len(orphans)}: {orphans}")

        recycle = c.base / "recycle"
        res = dl.execute_delete(orphans, [str(c.lib)], report, mode="recycle",
                                recycle_dir=str(recycle), options=an.AnalyzerOptions())
        check("回收模式全部成功", res.total_done == 3,
              f"done={res.total_done} skipped={res.skipped} failed={res.failed}")
        check("原位置已清空", not any(os.path.exists(p) for p in orphans))
        moved = {p.relative_to(recycle).name for p in recycle.rglob("*") if p.is_file()}
        check("回收目录保留了全部文件", moved == {"movie.nfo", "poster.jpg", "ep.nfo"}, str(moved))
        check("空目录被清理", len(res.removed_dirs) >= 1, str(res.removed_dirs))
        check("媒体库根目录未被删除", c.lib.exists())
        check("健康电影未受影响", (c.lib / "健康电影" / "movie.mkv").exists()
              and (c.lib / "健康电影" / "movie.nfo").exists())


def case_recycle_guard():
    """回收目录位于库内 / 未配置时，必须拒绝而不是硬删。"""
    with Case() as c:
        c.build([("X/a.nfo", 10)])
        report = an.analyze_directory(str(c.lib))
        paths = [entry.path for entry in report.orphans]

        res = dl.execute_delete(paths, [str(c.lib)], report, mode="recycle",
                                recycle_dir=str(c.lib / "_recycle"),
                                options=an.AnalyzerOptions())
        check("回收目录在库内时全部拒绝", res.total_done == 0 and len(res.skipped) == len(paths), str(res.skipped))

        res = dl.execute_delete(paths, [str(c.lib)], report, mode="recycle",
                                recycle_dir="", options=an.AnalyzerOptions())
        check("回收目录缺失时不硬删", res.total_done == 0, str(res))
        check("拒绝后文件仍在", all(os.path.exists(p) for p in paths))


def case_remove_mode():
    """直接删除模式。"""
    with Case() as c:
        c.build([("Y/b.jpg", 10), ("Y/c.mkv", 2000)])
        report = an.analyze_directory(str(c.lib))
        paths = [entry.path for entry in report.orphans]
        res = dl.execute_delete(paths, [str(c.lib)], report, mode="remove",
                                options=an.AnalyzerOptions())
        check("直接删除成功", res.total_done == 1 and not os.path.exists(paths[0]), str(res))
        check("同目录媒体文件保留", (c.lib / "Y" / "c.mkv").exists())


def main() -> int:
    for name, func in [
        ("V2 过期清单", case_v2_stale_manifest),
        ("V1 目录穿越", case_v1_outside_root),
        ("V3 非刮削文件", case_v3_not_scrap),
        ("V5 符号链接", case_v5_symlink),
        ("回收模式", case_recycle_happy_path),
        ("回收目录保护", case_recycle_guard),
        ("直接删除", case_remove_mode),
    ]:
        print(f"\n=== {name} ===")
        func()

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过: {FAILURES}")
        return 1
    print("✅ 安全阀全部生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
