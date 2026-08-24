<p align="center">
  <img src="public/app-logo-hero.png" alt="yoya" width="192" />
</p>

<h1 align="center">yoya</h1>

<p align="center">
  面向 macOS 的极简本地 Agent 客户端，基于 <a href="https://cursor.com/docs/sdk/python">Cursor Python SDK</a>。<br />
  把注意力留给问题本身，而不是工具本身。
</p>

<p align="center">
  <a href="https://github.com/ShinanWu/ElegantAgent/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/ShinanWu/ElegantAgent/actions/workflows/ci.yml/badge.svg" /></a>
  <a href="https://github.com/ShinanWu/ElegantAgent/releases/latest"><img alt="Release" src="https://img.shields.io/github/v/release/ShinanWu/ElegantAgent" /></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/github/license/ShinanWu/ElegantAgent" /></a>
</p>

> yoya 是独立开源项目，与 Cursor / Anysphere 无隶属、授权或背书关系。使用时需自备 Cursor API Key，并遵守 Cursor 的服务条款。

## 核心能力

- **多 Agent 工作区**：每个 Agent 绑定独立目录、模型，以及 Soul / Rules / Skills / Memory 配置。
- **连续主对话**：本地持久化对话；Cursor 会话失效时自动重建并回填最近上下文。
- **只读讨论侧栏**：选中主对话内容即可讨论，不污染主线；讨论 Agent 仅开放读取工具。
- **原生桌面体验**：轻量 Web UI + pywebview 外壳，支持附件、流式思考/工具轨迹和 Markdown。
- **本地优先**：项目维护者不运营中转后端、不采集遥测；应用服务只监听 `127.0.0.1`。

## 安装

系统要求：Apple Silicon Mac，macOS 11 或更高版本。

1. 从 [最新 Release](https://github.com/ShinanWu/ElegantAgent/releases/latest) 下载 `yoya.pkg` 和 `yoya.pkg.sha256`。
2. 可选但推荐：在终端校验下载文件。

   ```bash
   cd ~/Downloads
   shasum -a 256 -c yoya.pkg.sha256
   ```

3. 双击 `yoya.pkg` 安装，然后从“应用程序”打开 yoya。
4. 首次运行时填写 [Cursor API Key](https://cursor.com/dashboard/api?section=user-keys#user-api-keys)，选择默认工作目录。

当前公开安装包在没有 Apple Developer ID 的情况下会是**未签名、未公证**版本。macOS 若阻止打开，请在“系统设置 → 隐私与安全性”中确认该文件来自本仓库 Release 后选择“仍要打开”。不要对来源不明的安装包绕过 Gatekeeper。

## 安全与隐私

- Cursor API Key 优先保存在 macOS 钥匙串；1.0.x 的本地明文配置会在首次启动时自动迁移。
- 主 Agent 默认启用 Cursor 沙箱，只对所选工作目录开放读写，网络默认受限。
- 讨论 Agent 在沙箱基础上只允许 `read`、`grep`、`glob`、`ls`。
- 本地 HTTP/WebSocket 服务固定监听回环地址，不向局域网公开。
- 对话、设置和日志均在本机；发送给 Cursor 的提示、附件与工作区上下文受 Cursor 自身政策约束。

完整说明见 [隐私说明](PRIVACY.md) 与 [安全策略](SECURITY.md)。Agent 仍可能生成错误或高影响操作，请为重要目录保留备份，并在授权前审阅指令。

## 本地数据

应用数据与日志：

```text
~/Library/Application Support/yoya/
├── agents.json
├── discussions.json
├── combined_summaries.json
├── config.json
└── app.log
```

Agent 上传和输出默认位于所选工作目录的 `.agent/uploads/` 与 `.agent/outputs/`。API Key 不属于项目配置数据；它在钥匙串中的服务名为 `com.shinanwu.yoya`。

## 开发

需要 Apple Silicon Mac、Python 3.12；真实 SDK 验证还需要 Cursor API Key。

```bash
git clone https://github.com/ShinanWu/ElegantAgent.git
cd ElegantAgent
./install.sh
source .venv/bin/activate
cp .env.example .env
python run.py
```

`.env` 仅用于本地开发，可配置 `CURSOR_API_KEY`、`DEFAULT_CWD` 和 `DEFAULT_MODEL`；不要提交密钥。

### 验证

```bash
.venv/bin/python -m compileall -q launcher.py run.py server scripts
.venv/bin/python scripts/verify_regressions.py
.venv/bin/python scripts/verify_summary_feature.py
.venv/bin/python scripts/verify_conversation_restore.py
.venv/bin/python scripts/verify_lifecycle.py
```

前三组覆盖离线回归、总结和对话恢复；`verify_lifecycle.py` 在配置 API Key 后验证真实 Cursor Agent 生命周期。CI 也会完成 PyInstaller arm64 冒烟构建与包元数据检查。

### 构建发布包

```bash
./scripts/build_macos.sh
```

输出为 `dist/yoya.pkg`。脚本从 `VERSION` 读取版本，并固定直接依赖。签名与公证发布需要 Apple Developer ID：

```bash
CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
INSTALLER_SIGN_IDENTITY="Developer ID Installer: Your Name (TEAMID)" \
APPLE_ID="developer@example.com" \
APPLE_TEAM_ID="TEAMID" \
APPLE_APP_PASSWORD="app-specific-password" \
./scripts/build_macos.sh
```

提交改动前请阅读 [贡献指南](CONTRIBUTING.md)。版本变化见 [CHANGELOG](CHANGELOG.md)。

## 技术栈

- Python 3.12 · FastAPI · WebSocket · pywebview
- Cursor Python SDK（local agent + bridge）
- Vanilla JavaScript · HTML · CSS
- PyInstaller · macOS Installer

## License

[MIT](LICENSE)
