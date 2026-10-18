# mp115-plugins

MoviePilot 插件仓库（**V2 + V3 双版本**）。

## 插件列表

| 插件 | 版本 | V2 | V3 | 说明 |
|---|---|---|---|---|
| [硬盘秒传](plugins.v2/mp115cloudpush/) | v1.0.1 | ✅ | ✅ | 115 网盘本地文件秒传：CK/扫码登录、目录树浏览、选择本地整个文件夹批量秒传、监控 qB 下载完成秒级秒传、定时重试 |

> 同一个插件 ID（`Mp115CloudPush`）在两个代际各有一份实现，MoviePilot 会按自身版本自动选用对应目录：
> V2 环境读 `package.v2.json` → `plugins.v2/`；V3 环境读 `package.v3.json` → `plugins.v3/`。

## 安装方式

在 MoviePilot 中把本仓库加入插件市场：

```
设置 → 插件 → 插件市场地址（PLUGIN_MARKET）
```

填入：

```
https://github.com/dxmsk/mp115-plugins
```

保存后刷新插件市场，到「未安装」列表搜 **硬盘秒传**。

> 多个插件市场地址之间用英文逗号分隔。

### 拉不到索引时

GitHub raw 在国内网络下可能超时（MoviePilot 拉索引的超时是 15 秒）。可在环境变量中配置镜像：

```yaml
GITHUB_PROXY=https://ghfast.top/
```

配置后重启容器再刷新。

## 插件说明

### 硬盘秒传（Mp115CloudPush）

把本地（含 qBittorrent 下载完成的）媒体文件以**秒传**方式推送到 115 网盘，全程只读本地文件。

- 🔑 **CK Cookie 登录**：填入网页 Cookie 即可，无需申请开放平台应用
- 📱 **扫码登录自动填 CK**：发起扫码 → 115 App 扫码 → Cookie 自动获取并保存
- 📁 **目录树明文可见**：网盘全目录（含递归子树）可浏览
- 🗂️ **选择本地整个文件夹批量秒传**：递归扫描其下媒体文件逐个尝试
- ⚡ **监控 qB 下载完成秒传**：轮询下载器，下载完成即就地秒传
- 🧹 **秒传成功且文件仍在 MP 整理队列时，自动移除该队列项**
- 🔁 **定时重试**：秒传失败且尚未被整理成功的文件按间隔自动重试
- 🛡️ **只做「快传」**：未命中秒传时**不会**把文件本体上传，绝不移动/改名/删除源文件

依赖：`p115client`（随插件源码安装）。

## 目录结构

```
mp115-plugins/
├── plugins.v2/mp115cloudpush/   # V2 实现（app.plugins._PluginBase 等旧路径）
├── plugins.v3/mp115cloudpush/   # V3 实现（app.sdk.* 稳定 SDK）
├── tests/v2/mp115cloudpush/     # V2 单测
├── tests/v3/mp115cloudpush/     # V3 单测
├── icons/115pan_A.png
├── package.v2.json              # V2 插件市场索引
├── package.v3.json              # V3 插件市场索引
├── pytest.ini
└── LICENSE
```

## 开发与测试

测试需要 MoviePilot 后端置于本仓库**同级目录**（或设置 `MOVIEPILOT_BACKEND_PATH`），
并使用带后端依赖的解释器：

```bash
<workspace>/.venv/bin/python -m pytest tests/v2   # V2 实现
<workspace>/.venv/bin/python -m pytest tests/v3   # V3 实现
```

详见 [tests/README.md](tests/README.md)。

## 许可

GPL-3.0，见 [LICENSE](LICENSE)。
