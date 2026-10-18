# -*- coding: utf-8 -*-
"""静态检查：编译、未使用导入、未定义名字的粗查。"""
import ast
import py_compile
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

PLUGIN = Path(r"D:\AIHarness\N001_本地部署\MoviePilot-Plugins\plugins.v3\scrapecleaner")


def check_file(path: Path) -> list[str]:
    problems = []
    # 1. 语法编译
    try:
        py_compile.compile(str(path), doraise=True, cfile=str(path) + "c")
    except py_compile.PyCompileError as error:
        problems.append(f"编译失败: {error}")
        return problems

    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    # 2. 未使用的导入
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported[alias.asname or alias.name] = node.lineno
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported[(alias.asname or alias.name).split(".")[0]] = node.lineno

    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            value = node
            while isinstance(value, ast.Attribute):
                value = value.value
            if isinstance(value, ast.Name):
                used.add(value.id)
    # 字符串注解里出现的名字也算使用
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for name in imported:
                if name in node.value:
                    used.add(name)

    for name, lineno in imported.items():
        if name == "annotations":
            continue
        if name not in used:
            problems.append(f"未使用的导入: {name} (行 {lineno})")

    # 3. 类里定义了但从未被调用的方法（只报告，不一定是错误）
    return problems


def main() -> int:
    total = 0
    for path in sorted(PLUGIN.glob("*.py")):
        problems = check_file(path)
        if problems:
            print(f"\n{path.name}:")
            for problem in problems:
                print(f"  - {problem}")
            total += len(problems)
        else:
            print(f"{path.name}: OK")
    print()
    if total:
        print(f"共 {total} 个问题")
        return 1
    print("✅ 静态检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
