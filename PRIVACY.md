# yoya 隐私说明

最后更新：2026-08-24

yoya 是运行在用户 Mac 上的本地桌面应用。项目维护者不运营 yoya 后端，也不会通过 yoya 收集遥测、分析数据、广告标识符或用户账户信息。

## 数据如何流动

- 用户输入、所选工作目录中的相关内容、附件以及 Agent 生成的上下文，会按用户指令发送给 Cursor 服务，用于运行 Cursor Agent。
- Cursor API Key 默认保存在 macOS 钥匙串中。旧版本保存在 `~/Library/Application Support/yoya/config.json` 的密钥会在首次启动时迁移。
- Agent、主对话、讨论、设置与日志保存在 `~/Library/Application Support/yoya/`。
- 上传文件与 Agent 产物保存在所选工作目录的 `.agent/uploads/` 和 `.agent/outputs/`。
- yoya 本身不把这些数据发送给项目维护者或其他分析服务。

## 本地 Agent 权限

主 Agent 默认启用 Cursor 沙箱，只能在所选工作目录内读写；网络访问默认受限。讨论 Agent 进一步限制为只读工具。用户仍应只选择自己信任且已备份的目录，并在执行高风险指令前审阅内容。

## 第三方服务

yoya 使用 Cursor Python SDK。发送给 Cursor 的数据受 Cursor 自身条款和隐私政策约束。yoya 与 Cursor / Anysphere 无隶属、授权或背书关系。

## 删除数据

- 在 Agent 设置中执行“重置 Agent”可清空该 Agent 的对话、讨论、上传与产物。
- 删除 `~/Library/Application Support/yoya/` 可移除本地应用数据。
- Cursor API Key 可在 macOS“钥匙串访问”中删除，服务名称为 `com.shinanwu.yoya`。

## 联系

隐私问题请通过仓库的 GitHub Issues 提交；安全问题请按 [SECURITY.md](SECURITY.md) 中的私密渠道报告。
