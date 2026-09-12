# Reset Beacon → 飞书

一个轻量、可重复执行的监控器，每五分钟读取 Reset Beacon 的 History、Alerts 和 Health，并把新的重置事件、概率预警及接口故障发送到飞书群。

## 行为

- History 转发 `intent`、`scheduled`、`completed`、`policy_change` 及后续修正。
- Alerts 独立转发已发布的通知，包括没有 `postId` 的概率预警。
- History 和 Alert 即使引用同一帖子也分别发送，因为两者的发布时间与语义不同。
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

## 中文翻译（可选）

配置以下三项后，History 和 Alerts 原文发送成功后，会调用 OpenAI 兼容接口翻译正文，并追加发送一张标注“中文译文”的卡片。三项全部留空时保持原来的发送逻辑，无需额外开关；只填部分配置会报配置错误。

```dotenv
OPENAI_BASE_URL=https://your-provider.example/v1
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=your-model-name
OPENAI_REASONING_EFFORT=
TRANSLATION_TIMEOUT_SECONDS=60
TRANSLATION_MAX_RETRIES=3
```

`OPENAI_BASE_URL` 填 API 基础地址，包含服务商要求的路径前缀；程序自动追加 `/chat/completions`。请求使用 Bearer 密钥、`model` 和 `messages`，返回读取 `choices[0].message.content`，要求 `finish_reason=stop`。接口格式参考 [OpenAI Chat Completions 官方文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。HTTP 429、5xx、网络错误及无效译文会在本轮有限重试；其他 HTTP 错误留待下轮处理。超时单位为秒，重试配置表示每轮最多尝试次数。

`OPENAI_REASONING_EFFORT` 可选，留空时不发送该参数，沿用接口默认行为。程序接受 `none`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max`，实际支持范围由模型和服务商决定（例如 GPT-6 Astra 支持 `low` 到 `max` 的五档）。可以对比 `medium` 与 `high` 的译文质量和耗时；提高档位可能增加耗时和费用，不保证译文一定更好。若请求超过默认 60 秒，可相应增加 `TRANSLATION_TIMEOUT_SECONDS`。不支持参数时报错，不会静默移除参数降级；清空该配置即可恢复默认行为。仅填写推理档位不会启用翻译。

仅正文交给模型翻译为简体中文，提示词面向中文技术社区，要求忠实原意、避免直译腔，并在输出前核对数字、否定、时间、语气和术语；标题保留原文并添加译文标记，状态字段、时间、预测风险提示和来源按钮保留。启动通知和接口故障通知不调用翻译。提示词或推理档位变更只影响之后新生成的译文，已缓存的待发送译文仍复用。

仍沿用原文去重，只为成功发送的原文附加一条中文发送记录，不按译文内容另行去重。原文成功与中文待发送记录在同一个 SQLite 事务中保存；翻译或中文投递失败时，后续轮次只重试中文。生成的译文先保存，飞书重试时复用，重启后仍有效。不会给启用前已经发送或已设为基线的旧消息补发译文。

移除全部 OpenAI 配置后停止翻译及中文投递，已有中文待发送记录保留，重新配置后继续。`delivered`/`pending` 仍统计原文，中文历史单独使用 `translation_delivered`/`translation_pending`。中文失败会使本轮退出码为 `1`。

`--dry-run` 只预览原文，不调用模型、不生成译文；`health` 显示 `translationConfigured`，不探测模型接口。systemd 部署时把这些变量填入 `/etc/resetbeacon-feishu-monitor.env`，下一轮任务读取生效。

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

## 去重与投递

History 使用 `item.id` 作为 Post ID；Alerts 优先使用 `postId`，没有 `postId` 时退回使用 `alert.id`。每个 Post 在 History、Alerts 内分别最多形成一条投递，后续状态、修正或正文变化只更新 SQLite 中保存的当前 payload，不再重复通知。

History 和 Alerts 独立去重，不跨接口合并。同一帖子先进入 History、后形成 Alert 时仍会收到两条消息；没有 `postId` 的概率 Alert 也会独立发送，并明确说明不是 OpenAI 官方承诺。

整体投递语义为 at-least-once：投递成功响应丢失时极端情况下可能重复一条，但已抓到且尚未成功投递的消息不会因重启丢失。

## 测试

```bash
uv run pytest
```

测试只使用本地 fixture 和 `httpx.MockTransport`，不依赖实时网络或真实飞书 Webhook。

## systemd

`deploy/` 提供 oneshot service 和五分钟 timer。服务器先执行 `uv sync --locked` 生成锁定的 `.venv`；systemd 直接调用该虚拟环境，不要求运行时通过 uv 启动。当前单元使用部署路径 `/data/resetbeacon-feishu-monitor`、运行用户 `resetbeacon`、环境文件 `/etc/resetbeacon-feishu-monitor.env`。

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
