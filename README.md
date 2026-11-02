# 创作工作台 StudioWright

一个连接 Flow 创作服务和 Gemini 对话服务的工作台，包含 Ubuntu 服务端和 Windows 桌面操作端。Windows 端使用独立窗口，支持系统托盘和局域网发现。

## 安装使用

Windows、Ubuntu 安装包如已发布，可在本仓库的「Releases」下载。完整步骤见 [安装说明](docs/安装说明.md)。Ubuntu 可复用已运行的服务，Windows 安装时可选择创建桌面快捷方式。

## 项目内容

- `packaging/ubuntu/snapshot`：工作台、Flow 和 Gemini 集成的程序源码快照；`server` 包含安装和局域网发现组件。
- `packaging/ubuntu/pooldesk-upstream`：原有 PoolDesk 请求核心与上游参考代码，相关许可保留在 `snapshot/workbench`。
- `packaging/windows`：Windows 独立窗口、安装程序、账号助手及图标源码。

Ubuntu 与 Windows 打包脚本分别为 `packaging/ubuntu/build_server.py` 和 `packaging/windows/build_client.py`。构建脚本所需的第三方运行库和离线依赖包不放入源码仓库；现成安装包中包含相应依赖。自行重新打包时，需要先为 Ubuntu 准备 `wheelhouse-linux/flow`、`wheelhouse-linux/gemini`，并为 Windows 的 `client-payload` 准备 Python 嵌入式运行环境、WebView2 SDK 文件和编译后的桌面程序。`fetch_webview.py` 可下载并校验 WebView2 SDK。这份源码快照的全新环境重新打包尚未单独验证。

## 数据和许可

仓库与安装包不包含现有账号登录信息、密钥、素材和对话记录。迁移这些数据需要另行处理，不能仅靠安装包恢复。

本项目包含第三方组件，请遵循各自许可证。Flow 源码的许可证位于 `packaging/ubuntu/snapshot/flow/source/LICENSE`，PoolDesk 与 Hanaoka Gemini-API 的许可说明位于 `packaging/ubuntu/snapshot/workbench/POOL_DESK_NOTICE.md` 和 `POOL_DESK_LICENSE`；noVNC 等组件保留各自的许可文件。
