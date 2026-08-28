# Reset Beacon → 飞书

一个轻量、可重复执行的监控器，每五分钟读取 Reset Beacon 的 History、Alerts 和 Health，并把新的重置事件、概率预警及接口故障发送到飞书群。

## 行为

- History 是主数据源：转发 `intent`、`scheduled`、`completed`、`policy_change` 及后续修正。
- Alerts 补充 History 没有覆盖的通知，尤其是没有 `postId` 的概率预警。
- Alert 的 `postId` 已存在于 History 时自动抑制，避免同一帖子发两遍。
- Health 的上游内部 `degraded`/`recovered` 状态不推送；接口连续失败三次及恢复仍会通知。
- 首次真实运行不补发历史，只发送一条“Codex 重置监控已启动”。
- SQLite 保存当前状态和待投递消息；飞书确认业务成功后才标记 delivered。
- 任一 Reset Beacon 端点失败不会阻塞其他端点；连续三次失败时告警，恢复时通知。
- 进程锁防止 systemd timer 重入。

## 安装

项目使用 `uv` 管理 Python 3.11+ 与锁定依赖：

```bash
cd /path/to/resetbeacon-feishu-monitor
uv sync --extra dev
cp .env.example .env
```

程序不主动读取 `.env`，本地运行时可由 shell 导入：

```bash
set -a
. ./.env
set +a
```

必须配置：

```dotenv
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/...
```

机器人启用签名校验时再配置：

```dotenv
FEISHU_SIGNING_SECRET=...
```

Webhook 和签名密钥只从环境变量读取，不会写入日志。建议给机器人配置安全关键词 `Codex`；所有消息标题都包含该词。

## 使用

真实拉取并预览，不写飞书、不初始化基线：

```bash
uv run python -m monitor run-once --dry-run
```

执行一轮：

```bash
uv run python -m monitor run-once
```

检查配置、SQLite 和 Reset Beacon 健康状态：

```bash
uv run python -m monitor health
```

退出码：`0` 成功，`1` 本轮存在源或投递错误，`2` 配置错误，`3` 已有实例运行。

## 去重与合并

History 使用 `item.id`，Alerts 使用 `alert.id` 保存当前 payload hash。同一 ID 的状态、修正或正文发生实质变化时会形成一条新投递。

跨接口合并规则：

1. Alert 有 `postId`，且 History 当前或历史中存在相同 ID：保存 Alert，但不再次发送。
2. Alert 没有 `postId`：作为独立概率预警发送，并明确说明不是 OpenAI 官方承诺。
3. 新 History item 始终优先，消息使用它的事件类型与官方来源链接。

整体投递语义为 at-least-once：投递成功响应丢失时极端情况下可能重复一条，但已抓到且尚未成功投递的消息不会因重启丢失。

## 测试

```bash
uv run pytest
```

测试只使用本地 fixture 和 `httpx.MockTransport`，不依赖实时网络或真实飞书 Webhook。

## systemd

当前部署服务器通过 SSH 别名 `tokyo-dev` 访问。`deploy/` 提供 oneshot service 和五分钟 timer。服务器先执行 `uv sync --locked` 生成锁定的 `.venv`；systemd 直接调用该虚拟环境，不要求运行时通过 uv 启动。当前单元使用部署路径 `/data/resetbeacon-feishu-monitor`、运行用户 `resetbeacon`、环境文件 `/etc/resetbeacon-feishu-monitor.env`。

```bash
sudo cp deploy/resetbeacon-feishu-monitor.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now resetbeacon-feishu-monitor.timer
systemctl list-timers resetbeacon-feishu-monitor.timer
journalctl -u resetbeacon-feishu-monitor.service
```

状态数据库默认位于 `./data/monitor.sqlite3`，目录必须允许 service 用户写入。

## 已知风险

- Reset Beacon 是第三方服务，History、Alerts 和 Health 共享同一故障域，并不构成独立冗余。
- 概率 Alert 可能误报；消息会明确标记为模型预测。
- Reset Beacon 可能晚于 X 原帖发现事件。若将来要求完整、低延迟地转发 Tibo 的每条帖子，应改接 X 官方 API。
