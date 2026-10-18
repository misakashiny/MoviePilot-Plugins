# -*- coding: utf-8 -*-
"""
宿主契约冒烟测试
----------------
在没有 MoviePilot 宿主的机器上，用**最小 mock** 顶替 `app.*` 与 `fastapi`，
真实 import 插件的 `__init__.py`，然后检查：

1. 模块能否 import（语法、导入路径、装饰器执行期是否正常）；
2. 插件类能否实例化；
3. `get_api()` 每个路由都用 `auth="bear"`，且没有 `response_model`
   （宿主行为：auth 缺省会静默降级成 apikey；response_model 会裁字段）；
4. 每个 endpoint 的参数都带 `Body(...)` 默认值（POST 才收得到 JSON body）；
5. `get_form()` / `get_page()` 返回结构是「list[dict] + 可 JSON 序列化的模型」；
6. `get_command()` / `get_service()` 结构正确。

这是「真实加载检查」的降级替代：它抓不到宿主真实的权限与渲染行为，
但能抓住绝大多数写错的签名和契约。
"""
import json
import sys
import types
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PKG_DIR = _REPO_ROOT / "plugins.v3" / "scrapecleaner"

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"[OK  ] {name}")
    else:
        print(f"[FAIL] {name}  {detail}")
        FAILURES.append(name)


# ---------------------------------------------------------------
# 最小宿主 mock
# ---------------------------------------------------------------
def _install_mocks():
    """在没有 MoviePilot 的环境里，构造足够跑通 import 的最小桩。"""

    def module(name, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        sys.modules[name] = mod
        return mod

    class _Logger:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

        def debug(self, *a, **k):
            pass

    class _Event:
        def __init__(self, event_data=None):
            self.event_data = event_data or {}

    class _EventManager:
        def register(self, *args, **kwargs):
            def decorator(func):
                return func
            return decorator

    class _PluginBase:
        """_PluginBase 的最小桩：只提供插件会用到的方法。"""

        def __init__(self):
            self._config = {}
            self._data = {}
            self.messages = []

        def get_data_path(self):
            return Path(".")

        def save_data(self, key, value, plugin_id=None):
            self._data[key] = value

        def get_data(self, key=None, plugin_id=None):
            return self._data.get(key)

        def del_data(self, key, plugin_id=None):
            return self._data.pop(key, None)

        def update_config(self, config, plugin_id=None):
            self._config = config
            return True

        def get_config(self, plugin_id=None):
            return self._config

        def post_message(self, **kwargs):
            self.messages.append(kwargs)

    class _CronTrigger:
        @staticmethod
        def from_crontab(expr):
            return f"cron:{expr}"

    class _MessageType:
        Plugin = "插件"
        SiteMessage = "站点"

    class _EventType:
        PluginAction = "PluginAction"

    class _Response:
        """Response 的宽松替身：只校验字段名。"""
        _fields = {"success", "message", "data"}

        def __init__(self, success, message="", data=None, **kwargs):
            unknown = set(kwargs) - self._fields
            if unknown:
                raise TypeError(f"Response 不接受额外字段: {unknown}")
            self.success = success
            self.message = message or ""
            self.data = data

        def __class_getitem__(cls, item):
            return cls

    def _body(default=None, **kwargs):
        return default

    module("app")
    module("app.sdk")
    module("app.sdk.logging", logger=_Logger())
    module("app.sdk.events", Event=_Event, eventmanager=_EventManager())
    module("app.sdk.plugin", _PluginBase=_PluginBase)
    module("app.schemas")
    module("app.schemas.types", MessageType=_MessageType, EventType=_EventType)
    module("app.schemas.response", Response=_Response)
    sys.modules["app.schemas"].Response = _Response
    module("apscheduler")
    module("apscheduler.triggers")
    module("apscheduler.triggers.cron", CronTrigger=_CronTrigger)
    module("fastapi", Body=_body)


def _load_plugin():
    """以包的形式加载插件，使其相对导入（from .analyzer import ...）成立。"""
    import importlib.util

    _install_mocks()

    package = types.ModuleType("scrapecleaner")
    package.__path__ = [str(_PKG_DIR)]
    sys.modules["scrapecleaner"] = package

    for name in ("analyzer", "deleter"):
        spec = importlib.util.spec_from_file_location(
            f"scrapecleaner.{name}", _PKG_DIR / f"{name}.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"scrapecleaner.{name}"] = mod
        spec.loader.exec_module(mod)
        setattr(package, name, mod)

    spec = importlib.util.spec_from_file_location(
        "scrapecleaner.__init__", _PKG_DIR / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scrapecleaner.__init__"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    print("=== 1. 导入 ===")
    try:
        plugin_module = _load_plugin()
        print("[OK  ] 插件模块 import 成功")
    except Exception as error:
        import traceback
        traceback.print_exc()
        print(f"[FAIL] 插件 import 失败: {error}")
        return 1

    cls = plugin_module.ScrapeCleaner

    print("\n=== 2. 元信息 ===")
    check("plugin_name 非空", bool(cls.plugin_name), repr(cls.plugin_name))
    check("plugin_version 是 x.y.z", len(cls.plugin_version.split(".")) == 3, cls.plugin_version)
    check("plugin_icon 非空", bool(cls.plugin_icon), repr(cls.plugin_icon))
    check("plugin_config_prefix 以下划线结尾", cls.plugin_config_prefix.endswith("_"),
          cls.plugin_config_prefix)
    check("类名与目录名一致（小写）", cls.__name__.lower() == _PKG_DIR.name,
          f"{cls.__name__} vs {_PKG_DIR.name}")

    print("\n=== 3. 实例化与生命周期 ===")
    try:
        instance = cls()
        print("[OK  ] 实例化成功")
    except Exception as error:
        import traceback
        traceback.print_exc()
        return 1

    try:
        instance.init_plugin({"enabled": False, "library_dirs": "/tmp/lib"})
        print("[OK  ] init_plugin(未启用) 不报错")
        instance.init_plugin({"enabled": True, "library_dirs": ""})
        print("[OK  ] init_plugin(启用但无目录) 不报错")
        instance.init_plugin({
            "enabled": True,
            "library_dirs": "/tmp/lib",
            "delete_mode": "recycle",
            "scan_cron": "0 4 * * *",
            "max_files": "5000",
            "keep_days": "abc",       # 故意给非法值
            "pending_paths": "/x/y.nfo",
        })
        print("[OK  ] init_plugin(完整配置 + 非法值) 不报错")
        check("非法 keep_days 回落到默认", instance._keep_days == 30, instance._keep_days)
        check("max_files 字符串被转成 int", instance._max_files == 5000, instance._max_files)
        check("pending_paths 不进入实例状态", not hasattr(instance, "_pending_paths"))
        instance.stop_service()
        print("[OK  ] stop_service 不报错")
        instance.stop_service()
        print("[OK  ] stop_service 可重复调用")
    except Exception as error:
        import traceback
        traceback.print_exc()
        print(f"[FAIL] 生命周期异常: {error}")
        FAILURES.append("生命周期")

    print("\n=== 4. get_api 契约 ===")
    apis = instance.get_api()
    check("至少注册 1 个路由", len(apis) >= 1, str(len(apis)))
    for route in apis:
        path = route.get("path", "?")
        # 宿主行为：auth 缺省为 apikey，非 bear 会静默降级
        check(f"{path} auth 显式为 bear", route.get("auth") == "bear", repr(route.get("auth")))
        # 宿主对插件路由不包 envelope；声明 response_model 会裁字段。
        # 例外：文件/流等「原生响应」端点按官方文档**必须**显式声明
        # response_model=None 并写明 OpenAPI content。
        # 判据用「键是否存在」而不是「response_class 是否非空」——
        # 测试环境可能没有 starlette，response_class 会是 None。
        is_native = "response_model" in route and route.get("response_model") is None
        if is_native:
            check(f"{path} 原生端点写了 OpenAPI content",
                  bool((route.get("responses") or {}).get(200, {}).get("content")),
                  repr(route.get("responses")))
        else:
            check(f"{path} 未声明 response_model", "response_model" not in route,
                  repr(route.get("response_model")))
        check(f"{path} 有 endpoint", callable(route.get("endpoint")), "")
        check(f"{path} 有 summary", bool(route.get("summary")), "")

    print("\n=== 5. POST endpoint 必须能收 JSON body ===")
    # 不能用 inspect.signature 检查：桩里 `Body(default=None)` 会被求值成 None，
    # 运行时签名看起来「没有默认值」，会得到假的通过。这里直接读源码 AST。
    import ast

    source = (_PKG_DIR / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    post_endpoints = {
        route["endpoint"].__name__: route["path"]
        for route in apis
        if "POST" in route.get("methods", [])
    }
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in post_endpoints:
            continue
        checked += 1
        params = node.args.args[1:]  # 跳过 self
        if not params:
            print(f"[OK  ] {node.name} 无参数（合法）")
            continue
        defaults = node.args.defaults
        ok = len(defaults) == len(params) and all(
            isinstance(default, ast.Call) and getattr(default.func, "id", "") == "Body"
            for default in defaults
        )
        detail = ", ".join(
            f"{arg.arg}={ast.unparse(default)}" for arg, default in zip(params, defaults)
        ) or "无默认值"
        check(f"{post_endpoints[node.name]} 参数使用 Body(...)", ok, detail)
    check("至少检查到一个 POST endpoint", checked >= 1, str(checked))

    print("\n=== 6. get_form 结构 ===")
    try:
        form, defaults = instance.get_form()
        check("get_form 返回 (list, dict)", isinstance(form, list) and isinstance(defaults, dict),
              f"{type(form)} / {type(defaults)}")
        json.dumps(form, ensure_ascii=False)
        print("[OK  ] form 可 JSON 序列化")
        json.dumps(defaults, ensure_ascii=False)
        print("[OK  ] defaults 可 JSON 序列化")
        check("defaults 含 pending_paths", "pending_paths" in defaults, str(list(defaults)))
        check("defaults 含 enabled", "enabled" in defaults, "")
        # 找出所有 props.model，确认都指向 defaults 里的键
        models = set()

        def walk(node):
            if isinstance(node, dict):
                model = (node.get("props") or {}).get("model")
                if isinstance(model, str):
                    models.add(model)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(form)
        missing = sorted(m for m in models if m not in defaults)
        check("form 里所有 model 键都在 defaults 中", not missing, str(missing))
        check("form 里用到了 model（说明是真绑定）", bool(models), str(models))
    except Exception as error:
        import traceback
        traceback.print_exc()
        print(f"[FAIL] get_form 异常: {error}")
        FAILURES.append("get_form")

    print("\n=== 7. get_page 结构（未启用 / 无目录 / 正常）===")
    try:
        instance._enabled = False
        page_disabled = instance.get_page()
        check("未启用时返回提示", isinstance(page_disabled, list) and len(page_disabled) >= 1, "")
        json.dumps(page_disabled, ensure_ascii=False)

        instance._enabled = True
        instance._library_dirs = ""
        page_nodir = instance.get_page()
        check("无目录时返回提示", isinstance(page_nodir, list) and len(page_nodir) >= 1, "")

        instance._library_dirs = "/tmp/lib"
        page = instance.get_page()
        check("正常时返回 list", isinstance(page, list), str(type(page)))
        json.dumps(page, ensure_ascii=False)
        print("[OK  ] page 可 JSON 序列化")

        # 检查 events.click 契约：必须有 api + method
        events_found = []

        def walk_events(node):
            if isinstance(node, dict):
                events = node.get("events")
                if isinstance(events, dict):
                    for name, spec in events.items():
                        events_found.append((name, spec))
                for value in node.values():
                    walk_events(value)
            elif isinstance(node, list):
                for value in node:
                    walk_events(value)

        walk_events(page)
        check("页面里有 events 交互", bool(events_found), str(events_found[:2]))
        for name, spec in events_found:
            if not isinstance(spec, dict):
                check(f"events.{name} 是 dict", False, repr(spec))
                continue
            api_path = spec.get("api", "")
            check(f"events.{name} 的 api 以 plugin/ 开头", api_path.startswith("plugin/"), api_path)
            check(f"events.{name} 的 api 用类名", cls.__name__ in api_path, api_path)
            check(f"events.{name} 有 method", bool(spec.get("method")), repr(spec))
            check(f"events.{name} 的 api 不含 /api/v1/", "/api/v1/" not in api_path, api_path)
    except Exception as error:
        import traceback
        traceback.print_exc()
        print(f"[FAIL] get_page 异常: {error}")
        FAILURES.append("get_page")

    print("\n=== 8. get_command / get_service ===")
    try:
        commands = cls.get_command()
        check("get_command 返回 list", isinstance(commands, list), str(type(commands)))
        for cmd in commands:
            check("command 有 cmd/event/desc/action", all(cmd.get(k) for k in ("cmd", "event", "desc")),
                  str(cmd))
            check("command 的 data.action 存在", bool((cmd.get("data") or {}).get("action")), str(cmd))

        instance._enabled = True
        instance._scan_cron = "0 4 * * *"
        services = instance.get_service()
        check("get_service 返回 list", isinstance(services, list), str(type(services)))
        if services:
            svc = services[0]
            check("service 有 id/name/trigger/func/kwargs",
                  all(k in svc for k in ("id", "name", "trigger", "func", "kwargs")), str(svc.keys()))
            check("service func 可调用", callable(svc["func"]), "")
            check("service 的 kwargs 是 dict", isinstance(svc["kwargs"], dict), str(type(svc["kwargs"])))

        instance._scan_cron = "这不是cron"
        # 桩里的 CronTrigger 不校验，真宿主会抛异常；这里只确认不崩溃
        instance.get_service()
        print("[OK  ] 非法 cron 不导致崩溃")
    except Exception as error:
        import traceback
        traceback.print_exc()
        print(f"[FAIL] command/service 异常: {error}")
        FAILURES.append("command/service")

    print("\n=== 9. 自定义后缀真的生效 ===")
    try:
        import tempfile

        workdir = Path(tempfile.mkdtemp(prefix="scr_contract_"))
        try:
            lib = workdir / "lib"
            lib.mkdir()
            (lib / "空目录").mkdir()
            (lib / "空目录" / "a.nfo").write_bytes(b"x" * 10)
            # 一个自定义后缀的残留文件
            (lib / "空目录" / "b.myext").write_bytes(b"x" * 10)

            # 1) 不认识 .myext 时，它不算刮削文件
            instance._library_dirs = str(lib)
            instance._extra_scrap_exts = ""
            instance._enabled = True
            # 关掉新文件保护期（默认 1 天），否则刚创建的测试文件会被判受保护
            instance._min_age_minutes = 0
            report_default = instance.scan()
            names_default = {Path(e.path).name for e in report_default.entries}
            check("默认不认识 .myext", "b.myext" not in names_default, str(names_default))

            # 2) 配置了额外后缀后，它被当作刮削文件并判为残留
            instance._extra_scrap_exts = ".myext"
            report_extra = instance.scan()
            orphans = {Path(e.path).name for e in report_extra.orphans}
            check("自定义后缀被识别为刮削文件", "b.myext" in orphans, str(orphans))

            # 3) 走真实的 execute_delete 全链路：自定义后缀必须能真的删掉
            from scrapecleaner.deleter import execute_delete

            options = instance._analyzer_options()
            options.scrap_extensions = instance._effective_scrap_exts()
            target = lib / "空目录" / "b.myext"
            result = execute_delete(
                [str(target)],
                [str(lib)],
                report_extra,
                mode="remove",
                options=options,
            )
            check("自定义后缀的文件能通过安全阀并被删除",
                  result.total_done == 1 and not target.exists(),
                  f"done={result.total_done} skipped={result.skipped} failed={result.failed}")

            # 4) 未在清单内的自定义后缀文件仍然不能删（V2 依然生效）
            other = lib / "空目录" / "c.myext"
            other.write_bytes(b"x" * 10)
            result2 = execute_delete(
                [str(other)], [str(lib)], report_extra, mode="remove", options=options,
            )
            check("清单外的自定义后缀文件仍被拒绝",
                  result2.total_done == 0 and other.exists(),
                  f"done={result2.total_done}")
        finally:
            import shutil

            shutil.rmtree(workdir, ignore_errors=True)
    except Exception as error:
        import traceback

        traceback.print_exc()
        print(f"[FAIL] 自定义后缀测试异常: {error}")
        FAILURES.append("自定义后缀")

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} 项未通过: {FAILURES}")
        return 1
    print("✅ 宿主契约冒烟测试全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
