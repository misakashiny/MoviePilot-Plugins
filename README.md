# MoviePilot-Plugins（个人插件仓库）

MoviePilot V3 插件市场仓库。在主程序里把本仓库地址加入 `PLUGIN_MARKET` 即可看到下面的插件。

## 插件列表

| 插件 ID | 名称 | 版本 | 说明 |
| --- | --- | --- | --- |
| `ScrapeCleaner` | 刮削残留清理 | 1.0.0 | 扫描对账式清理媒体库中已无对应视频的刮削文件（nfo/图片/字幕），先出报告、人工确认后再删，带回收站与五道安全阀 |

每个插件的详细说明见 `plugins.v3/<插件id>/README.md`。

## 目录结构

```
MoviePilot-Plugins/
├── plugins.v3/                    # V3 插件源码
│   └── scrapecleaner/             # 目录名 = 插件类名小写
│       ├── __init__.py            # 主类 ScrapeCleaner
│       ├── analyzer.py            # 纯逻辑：扫描判定（不依赖宿主）
│       ├── deleter.py             # 纯逻辑：删除执行（不依赖宿主）
│       └── README.md
├── tests/v3/                      # 测试：不能放进插件源码目录
│   └── scrapecleaner/
├── icons/                         # 插件图标
└── package.v3.json                # 市场索引
```

测试刻意放在 `tests/` 而不是插件目录：插件市场同步时会把插件源码目录整体复制到运行目录，测试混在里面会被一起带过去。

## 开发规范要点

按 [MoviePilot 插件开发指南（V3）](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/Plugin_Development.md)：

- 类名 `XxxPlugin` ↔ 目录名 `xxxplugin`（小写）
- `plugin_version`、`package.v3.json` 的 `version`、`history` 顶部版本，三处必须一致
- 只从 `app.sdk.*` 导入宿主能力，不用 `app.core.*` / `app.helper.*` / `app.utils.*`
- 运行数据写 `get_data_path()`，不写源码目录
- `get_api()` 的路由必须显式写 `auth: "bear"`（缺省是 `apikey`）

## 跑测试

```bash
python tests/v3/scrapecleaner/static_check.py     # 编译 + 未使用导入
python tests/v3/scrapecleaner/scenario_check.py   # 扫描判定（14 个场景）
python tests/v3/scrapecleaner/deleter_check.py    # 安全阀（7 个用例）
python tests/v3/scrapecleaner/contract_check.py   # 宿主契约冒烟测试
python tests/v3/scrapecleaner/version_check.py    # 版本一致性门禁
python tests/v3/scrapecleaner/demo_library.py     # 真实库演练（只打印，不断言）
```

除 `contract_check.py`（用最小 mock 顶替宿主）外都能在裸 Python 环境跑，不需要 MoviePilot 依赖。

## 许可

MIT
