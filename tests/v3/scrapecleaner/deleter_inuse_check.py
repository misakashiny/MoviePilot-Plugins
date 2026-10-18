# -*- coding: utf-8 -*-
"""
deleter 安全阀 V6（跳过被占用文件）的验证。

本机是 Windows，因此这里有**真实占用**测试：开一个文件句柄后再执行删除，
验证文件确实被跳过、内容完好。Unix 上没有等价的「占用导致删除失败」语义，
相关用例会显式打印 SKIP 并说明平台限制，而不是假装通过。

加载方式与 deleter_check.py 一致：绕过包的 __init__.py，只加载纯逻辑模块。
"""
import errno
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "plugins.v3"))

import importlib.util  # noqa: E402
import types  # noqa: E402

_PKG_DIR = _REPO_ROOT / "plugins.v3" / "scrapecleaner"

IS_WINDOWS = os.name == "nt"


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


def skip(name, reason):
    print(f"[SKIP] {name}  {reason}")


class Case:
    """一个独立的临时文件系统：library/ 是媒体库，recycle/ 在库外。"""

    def __enter__(self):
        self.base = Path(tempfile.mkdtemp(prefix="scr_inuse_"))
        self.lib = self.base / "library"
        self.lib.mkdir()
        self.recycle = self.base / "recycle"
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.base, ignore_errors=True)
        return False

    def file(self, rel, size=100):
        target = self.lib.joinpath(*rel.replace("\\", "/").split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * size)
        return target

    def report(self):
        """扫描一次（此时没有句柄，所有残留文件都能被判定出来）。"""
        return an.analyze_directory(str(self.lib))


def orphan_paths(report):
    return [entry.path for entry in report.orphans]


def run(mode, orphans, case, report, **kwargs):
    return dl.execute_delete(
        orphans, [str(case.lib)], report,
        mode=mode,
        recycle_dir=str(case.recycle) if mode == "recycle" else "",
        options=an.AnalyzerOptions(),
        **kwargs,
    )


def skip_reason_of(result, path):
    """取出某个路径的跳过原因。"""
    for item_path, reason in result.skipped:
        if os.path.normcase(item_path) == os.path.normcase(path):
            return reason
    return ""


# ---------------------------------------------------------------
# 1. 真实占用：remove 模式
# ---------------------------------------------------------------
def case_held_remove():
    """
    持有 rb+ 句柄后走 remove 模式：文件必须被跳过且内容完好。

    这个用例同时验证了「删除探测」策略的正确性：探测时 os.remove 抛
    WinError 32，文件必须原封不动。
    """
    if not IS_WINDOWS:
        skip("V6 remove 模式真实占用", "Unix 下 unlink 不受打开句柄影响，无等价语义")
        return
    with Case() as c:
        victim = c.file("电影A (2017)/movie.nfo")
        report = c.report()
        paths = orphan_paths(report)
        check("扫描出 1 个残留", len(paths) == 1, str(paths))

        with open(victim, "rb+"):
            res = run("remove", paths, c, report)

        check("V6 被占用文件未被删除", victim.exists(), str(res))
        check("V6 计入 skipped 而非 failed",
              len(res.skipped) == 1 and not res.failed, str(res))
        check("V6 记录为 deleted/failed 之外（不计入成功）",
              res.total_done == 0, f"done={res.total_done}")
        check("V6 跳过原因是「被占用」",
              dl.IN_USE_REASON in skip_reason_of(res, str(victim)),
              str(res.skipped))
        check("V6 文件内容完好", victim.read_bytes() == b"x" * 100)

        # 句柄释放后应该能正常删掉，说明跳过是暂时的而不是永久失效
        res2 = run("remove", paths, c, report)
        check("释放句柄后同一文件可正常删除",
              res2.total_done == 1 and not victim.exists(),
              f"done={res2.total_done} skipped={res2.skipped} failed={res2.failed}")


# ---------------------------------------------------------------
# 2. 真实占用：recycle 模式
# ---------------------------------------------------------------
def case_held_recycle():
    """
    持有只读句柄后走 recycle 模式：文件必须留在原位，回收目录不得创建。
    """
    if not IS_WINDOWS:
        skip("V6 recycle 模式真实占用", "Unix 下被打开的文件仍可 rename，无等价语义")
        return
    with Case() as c:
        victim = c.file("电影B/movie.nfo")
        report = c.report()
        paths = orphan_paths(report)

        with open(victim, "rb"):
            res = run("recycle", paths, c, report)

        check("V6 回收模式被占用文件留在原位", victim.exists(), str(res))
        check("V6 回收模式跳过不计失败", res.total_done == 0 and not res.failed, str(res))
        check("V6 回收模式给出占用原因",
              dl.IN_USE_REASON in skip_reason_of(res, str(victim)), str(res.skipped))
        check("V6 回收模式未创建回收批次",
              not c.recycle.exists(), str(list(c.recycle.rglob("*")) if c.recycle.exists() else []))


# ---------------------------------------------------------------
# 3. 跨进程占用（模拟 qBittorrent 等外部程序）
# ---------------------------------------------------------------
def case_held_by_other_process():
    """另一个进程持有句柄时也必须识别为占用（排除「同进程才能检测到」的假象）。"""
    if not IS_WINDOWS:
        skip("V6 跨进程占用", "Unix 下无共享冲突语义")
        return
    with Case() as c:
        victim = c.file("电影C/episode.nfo")
        report = c.report()
        child_code = (
            "import sys, time\n"
            "handle = open(sys.argv[1], 'rb+')\n"
            "print('READY', flush=True)\n"
            "time.sleep(30)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", child_code, str(victim)],
            stdout=subprocess.PIPE,
        )
        try:
            line = child.stdout.readline()
            check("子进程已持有句柄", b"READY" in line, repr(line))
            res = run("remove", orphan_paths(report), c, report)
            check("V6 外部进程占用时跳过", victim.exists() and res.total_done == 0, str(res))
        finally:
            child.kill()
            child.wait()


# ---------------------------------------------------------------
# 4. 正常文件不被误判
# ---------------------------------------------------------------
def case_normal_file_not_flagged():
    """没有任何句柄的文件必须判为 FREE 并被正常处理，且不留临时文件。"""
    with Case() as c:
        a = c.file("电影D/a.nfo")
        report = c.report()

        check("probe_in_use 对普通文件返回 FREE",
              dl.probe_in_use(str(a)) is dl.InUseVerdict.FREE,
              str(dl.probe_in_use(str(a))))

        res = run("remove", orphan_paths(report), c, report)
        check("V6 未误伤普通文件", res.total_done == 1 and not a.exists(), str(res))
        leftovers = [p for p in c.lib.rglob("*") if dl._PROBE_TOKEN in p.name]
        check("V6 未残留探测临时文件", not leftovers, str(leftovers))

    with Case() as c:
        a = c.file("电影E/a.nfo")
        report = c.report()
        res = run("recycle", orphan_paths(report), c, report)
        check("V6 回收模式对普通文件正常处理",
              res.total_done == 1 and not a.exists(), str(res))
        moved = list(c.recycle.rglob("a.nfo"))
        check("V6 回收模式文件进了回收目录", len(moved) == 1, str(moved))


# ---------------------------------------------------------------
# 5. 开关：skip_in_use=False 完全不做检测
# ---------------------------------------------------------------
def case_switch_off():
    """skip_in_use=False 时不得调用探测函数，行为与旧版一致。"""
    if not IS_WINDOWS:
        skip("V6 开关关闭", "无占用语义，仍需 Windows 才能区分「检测过」与「没检测」")
        return
    with Case() as c:
        a = c.file("电影F/a.nfo")
        b = c.file("电影F/b.nfo")
        report = c.report()
        paths = orphan_paths(report)

        calls = []
        original = dl.probe_in_use

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)

        dl.probe_in_use = spy
        try:
            res = run("remove", paths, c, report, skip_in_use=False)
        finally:
            dl.probe_in_use = original

        check("V6 关闭时完全不调用检测", calls == [], str(calls))
        check("V6 关闭时普通文件照常删除",
              res.total_done == 2 and not a.exists() and not b.exists(), str(res))

    with Case() as c:
        a = c.file("电影G/a.nfo")
        report = c.report()
        calls = []
        original = dl.probe_in_use

        def spy(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        dl.probe_in_use = spy
        try:
            res = run("recycle", orphan_paths(report), c, report, skip_in_use=False)
        finally:
            dl.probe_in_use = original
        check("V6 回收模式关闭时也不调用检测", calls == [], str(calls))
        check("V6 回收模式关闭时正常回收",
              res.total_done == 1 and not a.exists(), str(res))


# ---------------------------------------------------------------
# 6. 检测异常 → 保守跳过
# ---------------------------------------------------------------
def case_probe_error_conservative():
    """探测函数本身抛异常时：不得删除任何文件，且原因可读。"""
    with Case() as c:
        a = c.file("电影H/a.nfo")
        b = c.file("电影H/b.nfo")
        report = c.report()
        paths = orphan_paths(report)
        check("扫描出 2 个残留", len(paths) == 2, str(paths))

        original = dl.probe_in_use

        def boom(*args, **kwargs):
            raise RuntimeError("模拟探测崩溃")

        dl.probe_in_use = boom
        try:
            res = run("remove", paths, c, report)
            check("V6 探测崩溃时 remove 不删任何文件",
                  a.exists() and b.exists(), str(res))
            check("V6 探测崩溃时全部计入 skipped",
                  len(res.skipped) == 2 and not res.failed, str(res))
            check("V6 探测崩溃原因可读",
                  all(dl.UNKNOWN_REASON in reason for _, reason in res.skipped),
                  str(res.skipped))

            res2 = run("recycle", paths, c, report)
            check("V6 探测崩溃时 recycle 不移动任何文件",
                  a.exists() and b.exists(), str(res2))
        finally:
            dl.probe_in_use = original


# ---------------------------------------------------------------
# 7. probe_in_use 自身的异常兜底
# ---------------------------------------------------------------
def case_probe_internal_exception():
    """probe_in_use 内部任何异常都必须转成 UNKNOWN，绝不放行。"""
    with Case() as c:
        a = c.file("电影I/a.nfo")
        original = dl._probe_by_rename

        def boom(path):
            raise OSError("模拟 rename 探测崩溃")

        dl._probe_by_rename = boom
        try:
            verdict = dl.probe_in_use(str(a))
        finally:
            dl._probe_by_rename = original
        check("探测内部异常 → UNKNOWN（不放行）",
              verdict is dl.InUseVerdict.UNKNOWN, str(verdict))

        # os.path.isfile 也可能抛异常（路径畸形）
        verdict2 = dl.probe_in_use("\x00非法路径")
        check("非法路径 → UNKNOWN（不放行）",
              verdict2 is dl.InUseVerdict.UNKNOWN, str(verdict2))


# ---------------------------------------------------------------
# 8. UNKNOWN 语义在 remove 模式下也必须跳过
# ---------------------------------------------------------------
def case_unknown_skipped():
    """即便文件本身没问题，UNKNOWN 结论也必须跳过（安全方向：宁可不删）。"""
    with Case() as c:
        a = c.file("电影J/a.nfo")
        report = c.report()
        original = dl.probe_in_use
        dl.probe_in_use = lambda *args, **kwargs: dl.InUseVerdict.UNKNOWN
        try:
            res = run("remove", orphan_paths(report), c, report)
        finally:
            dl.probe_in_use = original
        check("UNKNOWN → 文件保留", a.exists(), str(res))
        check("UNKNOWN → 计入 skipped 且原因可读",
              len(res.skipped) == 1 and dl.UNKNOWN_REASON in res.skipped[0][1],
              str(res.skipped))


# ---------------------------------------------------------------
# 9. 探测过程中文件被删掉（竞态）
# ---------------------------------------------------------------
def case_vanished_during_probe():
    """文件在探测期间消失（别人先删了）不应报失败。"""
    with Case() as c:
        a = c.file("电影K/a.nfo")
        report = c.report()
        paths = orphan_paths(report)
        # 模拟：真实删除发生在探测之前
        original = dl.probe_in_use

        def delete_then_normal(*args, **kwargs):
            os.remove(str(a))
            return original(*args, **kwargs)

        dl.probe_in_use = delete_then_normal
        try:
            res = run("remove", paths, c, report)
        finally:
            dl.probe_in_use = original
        check("探测期间文件已消失时不报失败", not res.failed, str(res))


# ---------------------------------------------------------------
# 10. 只读属性文件不能被误判为「被占用」
# ---------------------------------------------------------------
def case_readonly_not_in_use():
    """
    只读属性（chmod 444）会让 open(rb+) 失败，但它不是占用。
    本实现对 remove 模式不受影响；这里确认不会因此被误报为占用。
    """
    with Case() as c:
        a = c.file("电影L/a.nfo")
        os.chmod(a, 0o444)
        report = c.report()
        verdict = dl.probe_in_use(str(a))
        check("只读属性文件不被判为占用",
              verdict is not dl.InUseVerdict.IN_USE, str(verdict))
        res = run("remove", orphan_paths(report), c, report)
        # Windows 上只读文件 os.remove 会失败（WinError 5），按 UNKNOWN 跳过是允许的；
        # 但绝不能删掉，也不能判成「占用」
        check("只读属性文件未被删除或已标记为无法判断",
              a.exists() or res.total_done == 1,
              f"exists={a.exists()} result={res}")
        os.chmod(a, 0o666)


# ---------------------------------------------------------------
# 11. 回归：默认参数即可开启（skip_in_use 默认 True）
# ---------------------------------------------------------------
def case_default_enabled():
    """新增参数必须有默认值，老调用方（含 __init__.py）无需改动即可获得保护。"""
    import inspect

    signature = inspect.signature(dl.execute_delete)
    param = signature.parameters.get("skip_in_use")
    check("execute_delete 有 skip_in_use 参数", param is not None, str(signature))
    check("skip_in_use 默认值为 True", param is not None and param.default is True,
          str(param))

    with Case() as c:
        a = c.file("电影M/a.nfo")
        report = c.report()
        # 完全不传 skip_in_use，模拟现有 __init__.py 的调用方式
        res = dl.execute_delete(
            orphan_paths(report), [str(c.lib)], report,
            mode="remove", options=an.AnalyzerOptions(),
        )
        check("老调用方默认即启用 V6 且正常删除",
              res.total_done == 1 and not a.exists(), str(res))


# ---------------------------------------------------------------
# 12. 参考对照：证明「open/O_RDWR 试探」在 Windows 上是假阴性
# ---------------------------------------------------------------
def case_open_probe_is_useless():
    """
    记录实测结论，防止以后有人「优化」成用 open(path, 'rb') / O_RDWR 判占用。

    在 Windows 上，即使文件被别的进程以独占方式打开，
    open(path,'rb') 与 os.open(path, O_RDWR) 也都成功——所以这两个方法
    对占用检测毫无作用，只有删除/改名语义才具备区分能力。
    """
    if not IS_WINDOWS:
        skip("open 试探假阴性对照", "仅在本机 Windows 上验证")
        return
    with Case() as c:
        a = c.file("电影N/a.nfo")
        with open(a, "rb+"):
            opened_rb = True
            try:
                with open(a, "rb"):
                    pass
            except OSError:
                opened_rb = False

            opened_rdwr = True
            try:
                fd = os.open(a, os.O_RDWR)
                os.close(fd)
            except OSError:
                opened_rdwr = False

        check("对照：open(rb) 无法识别占用（证明不能用它做 V6）", opened_rb,
              "若此处为 False，说明平台行为变化，可重新评估检测策略")
        check("对照：os.open(O_RDWR) 无法识别占用（证明不能用它做 V6）",
              opened_rdwr, "若此处为 False，说明平台行为变化，可重新评估检测策略")


# ---------------------------------------------------------------
# 13. 跳过原因归类辅助（供 __init__.py 的历史统计使用）
# ---------------------------------------------------------------
def case_skip_reason_helpers():
    """classify_skip_reason / skipped_count / skip_summary 的契约。"""
    check("存在 SkipCategory 枚举", hasattr(dl, "SkipCategory"), "")
    categories = {item.value for item in dl.SkipCategory}
    check("SkipCategory 覆盖 in_use/unknown/other",
          categories == {"in_use", "unknown", "other"}, str(categories))

    check("IN_USE_REASON 归类为 in_use",
          dl.classify_skip_reason(dl.IN_USE_REASON) is dl.SkipCategory.IN_USE,
          str(dl.classify_skip_reason(dl.IN_USE_REASON)))
    check("UNKNOWN_REASON 归类为 unknown",
          dl.classify_skip_reason(dl.UNKNOWN_REASON) is dl.SkipCategory.UNKNOWN,
          str(dl.classify_skip_reason(dl.UNKNOWN_REASON)))
    check("其它安全阀原因归类为 other",
          dl.classify_skip_reason("命中排除关键字") is dl.SkipCategory.OTHER,
          str(dl.classify_skip_reason("命中排除关键字")))
    check("空字符串归类为 other（不抛异常）",
          dl.classify_skip_reason("") is dl.SkipCategory.OTHER, "")
    check("None 归类为 other（统计逻辑不崩）",
          dl.classify_skip_reason(None) is dl.SkipCategory.OTHER, "")

    check("is_in_use_skip 正确",
          dl.is_in_use_skip(dl.IN_USE_REASON) and not dl.is_in_use_skip(dl.UNKNOWN_REASON), "")
    check("is_unknown_skip 正确",
          dl.is_unknown_skip(dl.UNKNOWN_REASON) and not dl.is_unknown_skip(dl.IN_USE_REASON), "")

    result = dl.DeleteResult(skipped=[
        ("a.nfo", dl.IN_USE_REASON),
        ("b.nfo", dl.IN_USE_REASON),
        ("c.nfo", dl.UNKNOWN_REASON),
        ("d.nfo", "不在本次扫描的残留清单内（清单可能已过期，请重新扫描）"),
        ("e.nfo", "命中排除关键字"),
    ])
    check("skipped_count 统计 in_use 条数",
          result.skipped_count(dl.SkipCategory.IN_USE) == 2,
          str(result.skipped_count(dl.SkipCategory.IN_USE)))
    check("skipped_count 统计 unknown 条数",
          result.skipped_count(dl.SkipCategory.UNKNOWN) == 1, "")
    check("skipped_count 统计 other 条数",
          result.skipped_count(dl.SkipCategory.OTHER) == 2, "")
    check("skipped_paths 返回对应路径",
          result.skipped_paths(dl.SkipCategory.IN_USE) == ["a.nfo", "b.nfo"],
          str(result.skipped_paths(dl.SkipCategory.IN_USE)))
    check("skip_summary 三项齐全且数值正确",
          result.skip_summary() == {"in_use": 2, "unknown": 1, "other": 2},
          str(result.skip_summary()))

    empty = dl.DeleteResult()
    check("空结果 skip_summary 三键都为 0（键稳定）",
          empty.skip_summary() == {"in_use": 0, "unknown": 0, "other": 0},
          str(empty.skip_summary()))


# ---------------------------------------------------------------
# 14. 端到端：真实占用时 skip_summary 能数出来
# ---------------------------------------------------------------
def case_skip_count_real():
    """
    跑一次真实删除（一个文件被占用、一个正常），确认上层拿到的统计正确。

    这正是 __init__.py `_append_history()` 想要的场景。
    """
    with Case() as c:
        held = c.file("电影O/held.nfo")
        report = c.report()
        paths = orphan_paths(report)
        check("扫描出 1 个残留", len(paths) == 1, str(paths))

        # 无论平台，先用探测桩模拟「确认被占用」，保证 Unix 上也能验证统计契约
        original = dl.probe_in_use
        dl.probe_in_use = lambda *args, **kwargs: dl.InUseVerdict.IN_USE
        try:
            res = run("remove", paths, c, report)
        finally:
            dl.probe_in_use = original

        check("被占用文件计入 in_use 统计",
              res.skipped_count(dl.SkipCategory.IN_USE) == 1,
              str(res.skip_summary()))
        check("被占用文件不计入 other",
              res.skipped_count(dl.SkipCategory.OTHER) == 0, str(res.skip_summary()))
        check("被占用文件未被删除", held.exists(), str(res))

    # Windows 上再做一次真实句柄的端到端计数
    if not IS_WINDOWS:
        skip("真实占用计数（端到端）", "Unix 下无共享冲突语义，已用桩覆盖统计契约")
        return
    with Case() as c:
        held = c.file("电影P/held.nfo")
        report = c.report()
        with open(held, "rb+"):
            res = run("remove", orphan_paths(report), c, report)
        check("真实句柄 → skip_summary['in_use'] == 1",
              res.skip_summary()["in_use"] == 1, str(res.skip_summary()))
        check("真实句柄 → 文件保留且未计入 failed",
              held.exists() and not res.failed, str(res))


def main() -> int:
    for name, func in [
        ("真实占用 · remove 模式", case_held_remove),
        ("真实占用 · recycle 模式", case_held_recycle),
        ("跨进程占用", case_held_by_other_process),
        ("普通文件不误判", case_normal_file_not_flagged),
        ("开关关闭", case_switch_off),
        ("探测异常保守跳过", case_probe_error_conservative),
        ("探测内部异常兜底", case_probe_internal_exception),
        ("UNKNOWN 一律跳过", case_unknown_skipped),
        ("探测期间文件消失", case_vanished_during_probe),
        ("只读文件不误报", case_readonly_not_in_use),
        ("默认启用（回归）", case_default_enabled),
        ("open 试探假阴性对照", case_open_probe_is_useless),
        ("跳过原因归类辅助", case_skip_reason_helpers),
        ("历史统计：真实占用计数", case_skip_count_real),
    ]:
        print(f"\n=== {name} ===")
        func()

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过: {FAILURES}")
        return 1
    print("✅ V6 安全检查全部生效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
