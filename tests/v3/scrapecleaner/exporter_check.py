# -*- coding: utf-8 -*-
"""exporter 测试：CSV/JSON 导出格式、编码、内容正确性。"""
import csv
import io
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PKG_DIR = _REPO_ROOT / "plugins.v3" / "scrapecleaner"

import importlib.util  # noqa: E402
import types  # noqa: E402

_package = types.ModuleType("scrapecleaner")
_package.__path__ = [str(_PKG_DIR)]
sys.modules["scrapecleaner"] = _package
for _name in ("analyzer", "deleter", "exporter"):
    _spec = importlib.util.spec_from_file_location(
        f"scrapecleaner.{_name}", _PKG_DIR / f"{_name}.py"
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[f"scrapecleaner.{_name}"] = _mod
    _spec.loader.exec_module(_mod)
    setattr(_package, _name, _mod)

an = sys.modules["scrapecleaner.analyzer"]
ex = sys.modules["scrapecleaner.exporter"]

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"[OK  ] {name}")
    else:
        print(f"[FAIL] {name}  {detail}")
        FAILURES.append(name)


def make_report():
    """构造一个包含各种判定理由的报告。"""
    report = an.ScanReport(scanned_files=100, scan_time="2026-10-18 12:34:56")
    report.entries = [
        an.ScrapEntry(
            path="/media/电影/消失的爱人/gone.nfo", size=3500,
            reason=an.Reason.ORPHAN_STEM, detail="同目录没有同名媒体文件",
        ),
        an.ScrapEntry(
            path="/media/电影/消失的爱人/poster.jpg", size=300000,
            reason=an.Reason.ORPHAN_DIR_SHARED, detail="目录级文件，且目录内已无任何媒体文件",
            dir_shared=True,
        ),
        an.ScrapEntry(
            path="/media/电影/健康片/healthy.nfo", size=100,
            reason=an.Reason.MEDIA_PRESENT, detail="对应媒体文件：healthy.mkv",
        ),
        an.ScrapEntry(
            path="/media/电影/健康片/poster.jpg", size=200000,
            reason=an.Reason.DIR_ALIVE, detail="目录级文件，目录内仍有 1 个媒体文件",
            dir_shared=True,
        ),
    ]
    report.errors = [("/media/坏目录", "权限不足")]
    return report


def main() -> int:
    report = make_report()

    print("=== 1. CSV 默认只导出残留 ===")
    result = ex.export_csv(report)
    check("行数 = 残留数 2", result.row_count == 2, str(result.row_count))
    check("文件名是 .csv", result.filename.endswith(".csv"), result.filename)
    check("mime 正确", "text/csv" in result.mime, result.mime)

    print("\n=== 2. CSV 编码必须是 UTF-8 BOM（Excel 不乱码）===")
    check("以 BOM 开头", result.content.startswith(b"\xef\xbb\xbf"),
          str(result.content[:6]))
    text = result.content.decode("utf-8-sig")
    check("去掉 BOM 后能正常解码", "消失的爱人" in text, text[:80])

    print("\n=== 3. CSV 内容正确 ===")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    check("表头列数正确", len(rows[0]) == len(ex.CSV_COLUMNS), str(list(rows[0].keys())))
    check("表头是中文", "完整路径" in rows[0], str(list(rows[0].keys())))
    check("第一行是残留", rows[0]["是否残留"] == "是", rows[0]["是否残留"])
    check("理由有中文说明", rows[0]["理由说明"] == "无同名媒体文件", rows[0]["理由说明"])
    check("目录级标记正确", rows[1]["目录级文件"] == "是", rows[1]["目录级文件"])
    check("大小可读", rows[1]["大小"] == "293.0 KB", rows[1]["大小"])
    check("路径是完整路径", rows[0]["完整路径"].startswith("/media/"), rows[0]["完整路径"])

    print("\n=== 4. CSV 导出全部 ===")
    result_all = ex.export_csv(report, orphans_only=False)
    check("全部 4 行", result_all.row_count == 4, str(result_all.row_count))
    text_all = result_all.content.decode("utf-8-sig")
    check("包含保留项", "healthy.nfo" in text_all, "")
    check("保留项标记为否", "否" in text_all, "")

    print("\n=== 5. CSV 转义：逗号与引号 ===")
    weird = an.ScanReport(scanned_files=1, scan_time="2026-10-18 12:00:00")
    weird.entries = [an.ScrapEntry(
        path='/media/A, B/"quoted"/x.nfo', size=10,
        reason=an.Reason.ORPHAN_STEM, detail='含,逗号 and "引号"',
    )]
    r = ex.export_csv(weird)
    parsed = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))
    check("逗号和引号被正确转义",
          parsed[0]["完整路径"] == '/media/A, B/"quoted"/x.nfo', parsed[0]["完整路径"])
    check("detail 字段完整", parsed[0]["判定依据"] == '含,逗号 and "引号"', parsed[0]["判定依据"])

    print("\n=== 6. JSON 导出 ===")
    jr = ex.export_json(report)
    check("JSON 文件名", jr.filename.endswith(".json"), jr.filename)
    payload = json.loads(jr.content.decode("utf-8"))
    check("JSON 可解析", isinstance(payload, dict), str(type(payload)))
    check("含统计字段",
          all(k in payload for k in ("scanned_files", "orphan_count", "orphan_bytes", "kept_count")),
          str(list(payload)))
    check("orphan_count = 2", payload["orphan_count"] == 2, str(payload["orphan_count"]))
    check("kept_count = 2", payload["kept_count"] == 2, str(payload["kept_count"]))
    check("entries 2 条", len(payload["entries"]) == 2, str(len(payload["entries"])))
    check("errors 保留", len(payload["errors"]) == 1, str(payload["errors"]))
    check("中文不转义（可读）", "消失的爱人".encode("utf-8") in jr.content, "")

    print("\n=== 7. export() 分发 ===")
    check("csv 分发", ex.export(report, "csv").filename.endswith(".csv"))
    check("json 分发", ex.export(report, "json").filename.endswith(".json"))
    check("大写 JSON 也认", ex.export(report, "JSON").filename.endswith(".json"))
    check("未知格式回落到 csv", ex.export(report, "xlsx").filename.endswith(".csv"))

    print("\n=== 8. 时间戳来自 scan_time ===")
    check("文件名含扫描时间", "20261018-123456" in result.filename, result.filename)
    no_time = an.ScanReport(scanned_files=0)
    check("无时间时也能生成文件名", ex.export_csv(no_time).filename.startswith("scrapecleaner-"),
          ex.export_csv(no_time).filename)
    bad_time = an.ScanReport(scanned_files=0, scan_time="不是时间")
    check("非法时间不崩溃", ex.export_csv(bad_time).filename.endswith(".csv"),
          ex.export_csv(bad_time).filename)

    print("\n=== 9. 空报告 ===")
    empty = ex.export_csv(an.ScanReport(scanned_files=0))
    check("空报告只有表头", empty.row_count == 0, str(empty.row_count))
    header = empty.content.decode("utf-8-sig").strip().splitlines()
    check("表头存在", len(header) == 1 and "完整路径" in header[0], str(header))

    print("\n=== 10. summary_text ===")
    summary = ex.summary_text(report)
    check("摘要含数量", "2 个残留" in summary, summary)
    check("摘要含目录", "消失的爱人" in summary, summary)
    clean = ex.summary_text(an.ScanReport(scanned_files=50))
    check("无残留时有明确说明", "未发现" in clean, clean)

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过: {FAILURES}")
        return 1
    print("✅ 导出功能测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
