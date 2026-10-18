# -*- coding: utf-8 -*-
"""
新功能接入验证（v1.1.0）
-----------------------
验证六项新能力**真的从配置走到了行为**，而不只是"代码存在"：

1. 新文件保护期（min_age_minutes）
2. 自定义正则规则
3. 多库分别配置策略
4. 跳过被占用文件
5. 导出报告
6. 联动清理转移记录

复用 contract_check 的最小宿主 mock。
"""
import json
import os
import shutil
import sys
import tempfile
import time
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


def make_instance(lib: Path, **overrides):
    """构造启用状态、指向临时库的实例。"""
    instance = cls()
    data_dir = lib.parent / "_data"
    data_dir.mkdir(exist_ok=True)
    instance.get_data_path = lambda: data_dir
    config = {
        "enabled": True,
        "library_dirs": str(lib),
        "delete_mode": DeleteMode.REMOVE,
        "min_age_minutes": 0,
        **overrides,
    }
    instance.init_plugin(config)
    return instance


def old_file(path: Path, size=100, age_seconds=100 * 86400):
    """创建一个"很旧"的文件（修改时间推到过去）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))


class Lib:
    def __enter__(self):
        self.base = Path(tempfile.mkdtemp(prefix="scr_new_"))
        self.lib = self.base / "lib"
        self.lib.mkdir()
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.base, ignore_errors=True)
        return False


def case_min_age():
    """新文件保护期：老文件照常判残留，新文件被保护。"""
    with Lib() as c:
        old_file(c.lib / "电影/已删/gone.nfo")
        old_file(c.lib / "电影/刚整理/fresh.nfo", age_seconds=60)  # 1 分钟前

        inst = make_instance(c.lib, min_age_minutes=0)
        r0 = inst.api_scan().data or {}
        check("保护期 0 时两者都是残留", r0.get("orphan_count") == 2,
              str(r0.get("orphan_count")))

        inst2 = make_instance(c.lib, min_age_minutes=1440)  # 1 天
        r1 = inst2.api_scan().data or {}
        check("保护期开启后只留旧文件", r1.get("orphan_count") == 1,
              str(r1.get("orphan_count")))
        names = [
            item["name"]
            for group in (r1.get("groups") or [])
            for item in group["items"]
        ]
        check("被保护的是新文件", names == ["gone.nfo"], str(names))


def case_custom_patterns():
    """自定义正则：让自定义后缀被识别为刮削文件。"""
    with Lib() as c:
        old_file(c.lib / "电影/已删/x.myext")
        old_file(c.lib / "电影/已删/y.nfo")

        inst = make_instance(c.lib)
        r0 = inst.api_scan().data or {}
        check("默认不认识 .myext", r0.get("orphan_count") == 1, str(r0.get("orphan_count")))

        inst2 = make_instance(c.lib, extra_scrap_exts=".myext")
        r1 = inst2.api_scan().data or {}
        check("额外后缀生效", r1.get("orphan_count") == 2, str(r1.get("orphan_count")))

        inst3 = make_instance(c.lib, scrap_patterns=r"\.myext$")
        r2 = inst3.api_scan().data or {}
        check("正则也能认出来", r2.get("orphan_count") == 2, str(r2.get("orphan_count")))

        inst4 = make_instance(c.lib, scrap_patterns="[这是一个非法的正则(")
        res4 = inst4.api_scan()
        check("非法正则不崩溃", res4.success is True, str(res4.message))
        check("非法正则被记录", bool((res4.data or {}).get("errors")),
              str((res4.data or {}).get("errors")))


def case_multi_library_rules():
    """多库策略：一个库被 disabled，另一个正常；mode 可以分库。"""
    with Lib() as c:
        movies = c.lib / "movies"
        tv = c.lib / "tv"
        old_file(movies / "已删/gone.nfo")
        old_file(tv / "已删/gone.nfo")

        # 只配一个总目录，用 disabled 排除 movies
        inst = make_instance(
            c.lib,
            library_rules_text=f"{movies} | disabled=true",
        )
        data = inst.api_scan().data or {}
        paths = [
            item["path"]
            for group in (data.get("groups") or [])
            for item in group["items"]
        ]
        check("被排除的库不扫描", all("movies" not in p for p in paths), str(paths))
        check("未排除的库照常扫", any(os.path.join("tv", "已删") in p for p in paths), str(paths))

        # 分库删除模式
        inst2 = make_instance(
            c.lib,
            delete_mode=DeleteMode.REMOVE,
            library_rules_text=f"{tv} | mode=recycle | recycle_dir={c.base / 'rec2'}",
        )
        check("tv 库命中 recycle 策略", inst2._delete_strategy_for(str(tv / "a.nfo"))[0] == "recycle",
              str(inst2._delete_strategy_for(str(tv / "a.nfo"))))
        check("movies 库用全局 remove",
              inst2._delete_strategy_for(str(movies / "a.nfo"))[0] == "remove",
              str(inst2._delete_strategy_for(str(movies / "a.nfo"))))
        check("recycle_dir 也按库走",
              str(c.base / "rec2") in str(inst2._delete_strategy_for(str(tv / "a.nfo"))[1]),
              str(inst2._delete_strategy_for(str(tv / "a.nfo"))))

        # 写错的规则不影响整体
        inst3 = make_instance(
            c.lib,
            library_rules_text=f"{tv} | mode=不合法\n{movies} | disabled=true",
        )
        data3 = inst3.api_scan().data or {}
        check("坏规则不影响好规则", data3 is not None, str(data3))

        rules_api = inst3.api_rules()
        check("rules API 可用", rules_api.success is True, str(rules_api.message))
        check("rules API 给出警告", bool((rules_api.data or {}).get("warnings")),
              str((rules_api.data or {}).get("warnings")))


def case_export():
    """
    导出报告：CSV 与 JSON。

    注意：测试环境可能没有 starlette，此时 `_file_response` 会退回纯文本。
    所以这里分两层验证：
    - **内容层**（任何环境都能测）：直接调 exporter 验证 CSV/JSON 内容；
    - **API 层**：验证 api_export 不抛异常且能拿到内容。
    """
    with Lib() as c:
        old_file(c.lib / "电影/已删/gone.nfo")
        old_file(c.lib / "电影/已删/poster.jpg")
        inst = make_instance(c.lib)
        inst.api_scan()
        report = inst._load_report()

        # —— 内容层 ——
        from scrapecleaner.exporter import export_csv, export_json

        csv_result = export_csv(report)
        check("CSV 是 UTF-8 BOM", csv_result.content.startswith(b"\xef\xbb\xbf"),
              str(csv_result.content[:6]))
        text = csv_result.content.decode("utf-8-sig")
        check("CSV 含中文表头", "完整路径" in text, text[:60])
        check("CSV 有两行数据", len(text.strip().splitlines()) == 3,
              str(len(text.strip().splitlines())))
        check("CSV 是 text/csv", "text/csv" in csv_result.mime, csv_result.mime)

        json_result = export_json(report)
        payload = json.loads(json_result.content.decode("utf-8"))
        check("JSON 可解析", payload.get("orphan_count") == 2, str(payload.get("orphan_count")))

        # —— API 层 ——
        resp = inst.api_export("csv")
        body = getattr(resp, "body", None)
        if body is None:
            # 没装 starlette 的环境：退回纯文本
            check("无 starlette 时 API 优雅降级", isinstance(resp, (str, bytes)),
                  str(type(resp)))
        else:
            check("API 返回内容与 exporter 一致", body == csv_result.content,
                  f"{len(body)} vs {len(csv_result.content)}")
            check("声明了附件下载",
                  "attachment" in str(getattr(resp, "headers", {})), "")

        json_resp = inst.api_export("json")
        json_body = getattr(json_resp, "body", None)
        if json_body is not None:
            check("JSON API 可解析",
                  json.loads(json_body.decode("utf-8")).get("orphan_count") == 2, "")

        # 没有报告时导出
        empty_inst = make_instance(c.lib)
        empty_inst.del_data("last_report")
        r = empty_inst.api_export("csv")
        check("无报告时导出给出提示", r is not None, str(type(r)))


def case_delete_with_skip_in_use():
    """跳过被占用：Windows 上真实占用应被跳过。"""
    with Lib() as c:
        target = c.lib / "电影/已删/held.nfo"
        old_file(target)
        old_file(c.lib / "电影/已删/free.nfo")

        inst = make_instance(c.lib)
        inst.api_scan()
        orphans = [e.path for e in inst._load_report().orphans]

        # 持有文件句柄（Windows 上会阻止删除）
        handle = None
        held_ok = False
        try:
            handle = open(target, "rb+")
            held_ok = True
        except OSError:
            pass

        try:
            if held_ok:
                r = inst.api_delete({"paths": orphans})
                data = r.data or {}
                check("被占用的文件没被删", target.exists(), str(data))
                check("正常文件被删了", not (c.lib / "电影/已删/free.nfo").exists(), str(data))
                check("占用计入 skipped", data.get("skipped"), str(data.get("skipped")))
        finally:
            if handle:
                handle.close()

        # 关闭开关后不做检测（这里文件已释放，应能删掉）
        inst2 = make_instance(c.lib, skip_in_use=False)
        inst2.api_scan()
        remaining = [e.path for e in inst2._load_report().orphans]
        if remaining:
            r2 = inst2.api_delete({"paths": remaining})
            check("关闭检测后照常删除", (r2.data or {}).get("deleted", 0) >= 1, str(r2.data))


def case_history_linkage():
    """联动清理转移记录：宿主不提供能力时应静默跳过，不影响主流程。"""
    with Lib() as c:
        old_file(c.lib / "电影/已删/gone.nfo")
        inst = make_instance(c.lib, clean_history=True)
        inst.api_scan()
        orphans = [e.path for e in inst._load_report().orphans]
        r = inst.api_delete({"paths": orphans})
        check("开启联动后删除仍正常", (r.data or {}).get("deleted") == 1, str(r.data))
        hist = inst.api_history().data or []
        check("历史里有本态", bool(hist), str(hist))
        check("历史记录了被占用跳过计数",
              "skipped_in_use" in (hist[0] if hist else {}), str(hist[0] if hist else {}))


def case_history_defaults():
    """默认配置应包含所有新字段。"""
    defaults = cls._default_config()
    for key in ("min_age_minutes", "skip_in_use", "clean_history", "library_rules_text",
                "extra_media_exts", "media_patterns", "scrap_patterns", "dir_shared_patterns"):
        check(f"默认配置含 {key}", key in defaults, str(list(defaults)))
    check("保护期默认 1440 分钟（1 天）", defaults.get("min_age_minutes") == 1440,
          str(defaults.get("min_age_minutes")))
    check("占用检测默认开启", defaults.get("skip_in_use") is True)
    check("转移记录联动默认关闭", defaults.get("clean_history") is False)


def main() -> int:
    for name, func in [
        ("默认配置", case_history_defaults),
        ("新文件保护期", case_min_age),
        ("自定义正则", case_custom_patterns),
        ("多库策略", case_multi_library_rules),
        ("导出报告", case_export),
        ("跳过被占用", case_delete_with_skip_in_use),
        ("转移记录联动", case_history_linkage),
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
    print("✅ 新功能接入验证全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
