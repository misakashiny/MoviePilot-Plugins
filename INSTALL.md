# 安装到 MoviePilot（Docker + GitHub 插件市场）

本文档针对 **Docker/docker-compose 部署 + 能访问 GitHub** 的环境。

---

## 前置条件

| 项 | 要求 | 为什么要 |
| --- | --- | --- |
| MoviePilot | **v3.0.0 及以上** | 插件用了 `app.sdk.*`，V2 没有这两个接口 |
| GitHub 账号 | 一个 | 存放插件仓库 |
| GitHub Token | 建议配置 | 不配会很快触发 API 限流 |

**先确认你的 MP 版本**：MoviePilot 界面右下角或「设置 → 关于」里能看到版本号。如果还是 v2.x，需要先升级主程序。

---

## 第一步：把仓库推到 GitHub

### 1.1 在 GitHub 创建空仓库

打开 https://github.com/new

- **Repository name**：`MoviePilot-Plugins`（名字随意，但建议用这个惯例名）
- **Public**（强烈建议公开 —— 私有仓库 MP 拉取需要额外配 Token 权限）
- **不要**勾选 "Add a README file"（否则会和本地冲突）

### 1.2 推送本地代码

仓库已经初始化并提交好了，直接加远程地址推上去。

在你的机器上（`D:\AIHarness\N001_本地部署\MoviePilot-Plugins`）：

```powershell
cd "D:\AIHarness\N001_本地部署\MoviePilot-Plugins"

# 把 <你的用户名> 换成你的 GitHub 用户名
git remote add origin https://github.com/<你的用户名>/MoviePilot-Plugins.git
git push -u origin main
```

如果提示要认证，用 **Personal Access Token** 当密码（GitHub 早就禁用了账号密码）：
https://github.com/settings/tokens → Generate new token (classic) → 勾选 `repo` 权限。

### 1.3 确认分支是 main

MP 的插件市场**只读 main 分支**，这点很关键。

本地已经是 `main`（初始化时就指定了 `-b main`），推上去就是对的。

验证：
```powershell
git branch
# 应该显示  * main
```

---

## 第二步：在 MoviePilot 里添加插件市场

### 2.1 找到插件市场设置

打开 MoviePilot → 左侧「插件」→ 点右上角的**图标/设置**（不同版本位置略有差异，通常在插件页顶部）

### 2.2 添加仓库地址

在 **PLUGIN_MARKET** 里添加：

```
https://github.com/<你的用户名>/MoviePilot-Plugins
```

规则：
- **不带 `.git` 后缀**、**不带末尾斜杠**
- 多个地址用英文逗号 `,` 分隔
- V2 版本是点界面图标维护；V3 也可以在「设置 → 插件」的 `PLUGIN_MARKET` 变量里改

示例（和官方仓库共存）：
```
https://github.com/jxxghp/MoviePilot-Plugins,https://github.com/<你的用户名>/MoviePilot-Plugins
```

### 2.3 配置 GitHub Token（强烈建议）

MP 访问 GitHub API 有频率限制，不配 Token 很容易刷不出来。

在环境变量里加：

```
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
```

Token 创建：https://github.com/settings/tokens → **classic** → 勾选 `public_repo` 就够。

如果**拉取插件文件**失败（能进市场但下载不下来），再加代理：

```
GITHUB_PROXY=https://ghfast.top/     # 或你常用的加速站
```

### 2.4 重启容器

环境变量改动必须重启才生效：

```bash
docker-compose down
docker-compose up -d
```

或：
```bash
docker restart <容器名>
```

---

## 第三步：安装插件

1. MoviePilot → **插件** → 右上角**刷新/同步**按钮
2. 在插件市场里搜索「**刮削残留清理**」
3. 点安装

装完会在「已安装」列表里出现，图标是一个青色圆角方块 + 白色文档 + 绿色对勾。

---

## 第四步：首次配置（重要）

**不要直接开「直接删除」就开始跑。** 按下面的顺序来：

### 4.1 配置媒体库目录

插件配置页 → 「媒体库目录」，每行一个，填**容器内路径**：

```
/media/电影
/media/剧集
```

> ⚠️ **关键**：这里要填 **MoviePilot 容器里看到的路径**，不是 NAS 宿主机上的路径。
> 如果你在 docker-compose 里映射的是 `/volume1/video/movies:/media/电影`，
> 那这里就填 `/media/电影`。
>
> 不确定的话，可以先用 `docker exec -it <容器名> ls /media` 看一眼。

### 4.2 保持默认的安全设置

| 配置 | 建议值 | 说明 |
| --- | --- | --- |
| 删除方式 | **移入回收站** | 保留默认，先别改成直接删除 |
| 回收目录 | 留空 | 自动放在插件数据目录下 |
| 回收站保留天数 | 30 | 后悔药有效期 |
| 新文件保护期 | **1440（1 天）** | 默认值，别动 |
| 跳过被占用文件 | **开** | Windows 有效；NAS/Linux 上无效但也没有副作用 |
| 联动清理转移记录 | 先关 | 确认前面都正常了再开 |
| 定时扫描 | 先留空 | 手动验证通过后再加 cron |

### 4.3 保存并启用

保存配置 → 打开「启用插件」→ 再保存一次。

---

## 第五步：先小范围试跑

**强烈建议先拿一个小的库试**，别一上来就扫整个主库。

### 5.1 限制扫描范围

临时把「媒体库目录」改成一个子目录（比如 `/media/电影/某个小分类`），或者用一个文件数量不多的库。

### 5.2 手动扫描

进插件详情页 → 点「**立即扫描**」

等它跑完（大库可能要几分钟），看报告：
- 共扫描多少文件
- 发现多少残留
- 明细表格里的「判定依据」列 —— **这一列是给人看的，逐条检查它说得对不对**

### 5.3 导出报告核对

想仔细看的话，导出 CSV 用表格打开：

```
http://<你的MP地址>:3000/api/v1/plugin/ScrapeCleaner/export?fmt=csv
```

（需要登录态，建议在已登录的浏览器里直接访问）

CSV 列里有「是否残留 / 判定理由 / 判定依据」，可以排序筛选。

### 5.4 确认无误再删

在详情页点「清理本组」或「清理全部」，或者去配置页用「精细清理」逐条挑。

**用回收站模式**，删完去回收目录看看，确认内容都对。

### 5.5 恢复测试

万一删错了，回收目录结构是：

```
<插件数据目录>/recycle/<时间戳>/<库目录名>/<原相对路径>
```

直接挪回原位即可。建议你**故意删一个再恢复一次**，确认整条链路是通的。

---

## 第六步：正式启用

小库验证通过后：

1. 媒体库目录改回完整的库路径
2. 配置定时扫描，例如 `0 4 * * *`（每天凌晨 4 点）
3. 打开通知（可选）

> **定时扫描只出报告和通知，不会自动删除。**
> 删除永远需要你手动点按钮确认 —— 这是刻意的设计。

---

## 更新插件

改了代码之后：

```powershell
cd "D:\AIHarness\N001_本地部署\MoviePilot-Plugins"
# 改完代码，记得同时改版本号（plugin_version 和 package.v3.json 两处）
git add -A
git commit -m "fix: 修复 xxx"
git push
```

然后在 MP 插件市场里点「更新」。

**版本号必须改**，否则 MP 认为没有更新。两个地方要一起改：

| 位置 | 字段 |
| --- | --- |
| `plugins.v3/scrapecleaner/__init__.py` | `plugin_version = "1.1.0"` |
| `package.v3.json` | `"version": "1.1.0"` 和 `history` 顶部 |

改完跑一下版本检查：
```powershell
python tests/v3/scrapecleaner/version_check.py
```

---

## 常见问题

### 插件市场里看不到插件

按顺序排查：

1. **分支是不是 main？** MP 只读 main 分支
2. **仓库地址写对了吗？** 不带 `.git`、不带末尾斜杠
3. **容器重启了吗？** 环境变量改动要重启
4. **看日志**：`docker logs <容器名> --tail 100 | grep -i plugin`
5. **仓库是私有的？** 私有仓库需要配 `GITHUB_TOKEN`，且 Token 要有 `repo` 权限

### 装上了但插件不显示

检查 `package.v3.json` 的 `system_version` 是 `>=3.0.0`。如果你的 MP 是 v2.x，这个插件**装不上**，需要先升级主程序。

### 扫描结果不对：明明是残留却判成保留

看明细里的「判定依据」，它会说明理由。常见情况：

| 判定依据 | 意思 |
| --- | --- |
| `目录级文件，目录内仍有 N 个媒体文件` | 这是正常保护，防止误删海报墙 |
| `按季集号 S01E01 匹配到：xxx.mkv` | 按季集号认领了，可以检查是不是认错了 |
| `修改于 X 分钟前，未超过保护期` | 新文件保护期命中，等过了保护期再扫 |
| `通用附属文件，归属目录内唯一媒体` | `subs.srt` 这类按「目录唯一视频」归属 |

如果确实判错了，**别急着删**，先把那条记录的「判定依据」发我看看。

### 扫描很慢

- 用 `max_files` 限制单次扫描量
- 用「排除关键字」跳过无关目录
- 用多库策略给大库单独设 `max_files`

### NAS 上「所有文件都被跳过」

这是 V6 占用检测在 SMB/NFS 上的误判。去配置页**关闭「跳过被占用文件」**。

### 导出 CSV 打开乱码

插件已经用 UTF-8 BOM 编码，正常不会乱码。如果还是乱码，用 WPS 或 Excel 的「数据 → 从文本导入」并选 UTF-8。

---

## 附：纯手动安装（不推 GitHub）

如果哪天 GitHub 连不上，可以手动拷文件。

**1. 找到容器的插件目录**

```bash
docker exec -it <容器名> ls /config/plugins
```

`plugins.v3` 通常在 MP 的配置目录下（默认 `/config/plugins.v3`，具体看你的挂载）。

**2. 把插件目录拷进去**

```bash
# 从宿主机拷进容器
docker cp "D:\AIHarness\N001_本地部署\MoviePilot-Plugins\plugins.v3\scrapecleaner" <容器名>:/config/plugins.v3/scrapecleaner
```

如果是 compose 挂载了配置目录，直接拷到宿主机对应位置更简单：

```powershell
# 假设你的 MP 配置目录挂载在 D:\docker\moviepilot\config
Copy-Item -Recurse "D:\AIHarness\N001_本地部署\MoviePilot-Plugins\plugins.v3\scrapecleaner" "D:\docker\moviepilot\config\plugins.v3\"
```

**3. 重启容器**

```bash
docker restart <容器名>
```

手动装的缺点是：市场里不会显示更新，以后得手动重拷。

---

## 附：目录结构参考

推上 GitHub 后，仓库应该长这样（MP 按这个结构找插件）：

```
MoviePilot-Plugins/
├── package.v3.json                      ← 市场索引，必须
├── icons/
│   └── clean.png                        ← 插件图标
├── plugins.v3/
│   └── scrapecleaner/                   ← 目录名 = 插件类名小写
│       ├── __init__.py                  ← 主类 ScrapeCleaner
│       ├── analyzer.py
│       ├── deleter.py
│       ├── ...
│       └── README.md
├── tests/                               ← 不会被拷进运行目录
│   └── v3/scrapecleaner/
└── README.md
```

几个**必须**对上的地方（对不上会装不上或装了不显示）：

| 关系 | 值 |
| --- | --- |
| 主类名 | `ScrapeCleaner` |
| 目录名 | `scrapecleaner`（类名小写） |
| `package.v3.json` 的键 | `ScrapeCleaner` |
| `plugin_version` = `version` = `history` 顶部 | `1.1.0` |
| 图标路径 | `icons/clean.png`，字段 `"icon": "clean.png"` |
