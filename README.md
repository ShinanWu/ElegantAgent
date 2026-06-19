# 尤雅

仿 [Pi Coding Agent](https://github.com/badlogic/pi-mono) 风格的 Cursor Agent 桌面应用，基于 [Cursor Python SDK](https://cursor.com/docs/sdk/python)。

## 用户安装（macOS）

1. 双击 `dist/CursorAgentPi.pkg`
2. 在安装向导中点击「继续」完成安装
3. 在「应用程序」中打开 **尤雅**，按引导填写 API Key 即可使用

配置与聊天记录保存在：

`~/Library/Application Support/CursorAgentPi/`

## 开发者

```bash
cd ~/Projects/cursor-agent-gui
./install.sh
source .venv/bin/activate
python run.py
```

### 打包发布

```bash
./scripts/build_macos.sh
```

产物：

- `dist/CursorAgentPi.pkg` — macOS 安装包

打包脚本会走清华 PyPI 镜像，并内置前端资源（离线可用）。

## 功能

- 深色 Pi 风格聊天界面，流式输出
- 多会话管理
- 首次启动引导配置 API Key
- 工具调用与 Markdown 渲染
- 无需安装 Python / Node（应用已内置）

## 配置项

| 字段 | 说明 |
|------|------|
| `api_key` | Cursor API Key（[控制台获取](https://cursor.com/dashboard/integrations)） |
| `default_cwd` | 默认工作目录 |
| `default_model` | 默认模型，如 `composer-2.5` |

开发模式仍可使用项目根目录的 `.env`（见 `.env.example`）。
