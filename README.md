<p align="center">
  <img src="public/app-logo-hero.png" alt="尤雅" width="192" />
</p>

<h1 align="center">尤雅 · ElegantAgent</h1>

<p align="center">
  一款 macOS 桌面 Agent 应用，基于 <a href="https://cursor.com/docs/sdk/python">Cursor Python SDK</a>，<br />
  让你把注意力留给问题本身，而不是工具本身。
</p>

<p align="center">
  <strong>中心思想</strong>：大道至简<br />
  <strong>指导方针</strong>：第一性原理<br />
  <strong>最终目标</strong>：优雅地使用 Agent
</p>

---

## 为什么是尤雅

大多数 Agent 工具把「能做什么」堆得很满，却很少有人认真回答：**怎样才算用得舒服**。

尤雅从三个问题出发：

1. **什么必须留在界面上？** —— 对话、上下文、必要的控制，其余一律让位。
2. **什么应该从第一性原理重建？** —— 输入、附件、讨论、记忆注入，不照搬 IDE，只为 Agent 场景设计。
3. **怎样才算优雅？** —— 少打断、少层级、少心智负担；你表达意图，Agent 处理细节。

因此尤雅选择 **Pi 式极简界面** + **单 Agent 单主线对话** + **选中即可讨论** 的轻量协作方式，而不是再造一个臃肿工作台。

## 核心能力

- **多 Agent 管理**：每个 Agent 绑定独立工作目录、模型与 Soul / Rules / Skills / Memory 配置
- **混排输入**：文字与图片、路径引用在同一输入区自然混排，所见即所得
- **讨论侧栏**：选中主对话片段即可发起只读讨论，不污染主线上下文
- **流式对话**：工具调用、思考过程与 Markdown 实时呈现
- **本地优先**：配置与聊天记录保存在本机，API Key 不上传

## 用户安装（macOS）

1. 在 [Releases](https://github.com/ShinanWu/ElegantAgent/releases) 下载 `CursorAgentPi.pkg`（或本地构建产物）
2. 双击安装包，按向导完成安装
3. 在「应用程序」中打开 **尤雅**，填写 [Cursor API Key](https://cursor.com/dashboard/integrations) 即可开始

配置与数据目录：

`~/Library/Application Support/CursorAgentPi/`

## 开发者

```bash
git clone https://github.com/ShinanWu/ElegantAgent.git
cd ElegantAgent
./install.sh
source .venv/bin/activate
python run.py
```

复制 `.env.example` 为 `.env`，填入 `CURSOR_API_KEY` 与默认工作目录。

### 打包发布

```bash
./scripts/build_macos.sh
```

产物：`dist/CursorAgentPi.pkg`

打包脚本使用清华 PyPI 镜像，并内置前端静态资源，安装后可离线使用界面。

### 重新生成图标

将高清 logo 置于 `packaging/AppIconSource.png`，然后：

```bash
python packaging/make_icon.py
```

## 配置项

| 字段 | 说明 |
|------|------|
| `api_key` | Cursor API Key |
| `default_cwd` | 默认工作目录 |
| `default_model` | 默认模型，如 `composer-2.5` |

## 技术栈

- Python · FastAPI · WebSocket · pywebview
- 原生 Cursor SDK（local agent + bridge）
- 无框架前端（Vanilla JS + contenteditable composer）

## License

[MIT](LICENSE)
