# Feishu-Driven Autonomous Agent Loop · 飞书驱动的自主 Agent 回路

> **EN TL;DR** — Supervise long-running agent tasks from Feishu/Lark: bot progress messages, event-driven wakeups, and a long fallback heartbeat. Version 1.1 adds a bounded listener, configured sender/group routing, a private outbox, receipt validation, and offline regression tests. Routing is **not authorization**. This is not a durable queue, a session lease service, or an exactly-once delivery system. See the [skill](skills/feishu-agent-loop/SKILL.md).

让 Agent 推进长任务，人通过手机飞书看结果、补充要求和调整方向。事件通过长连接回传，不靠模型频繁轮询；但**事件到达、调度唤醒、模型开始处理是不同时间点**，不能承诺回复总在几秒内完成。

## 1. 三块组合与实现范围

| 块 | 作用 | 载体 |
|---|---|---|
| 进度推送 | 阶段结果、异常、需要确认的动作 | `lark-cli im +messages-send --as bot` |
| 事件回传 | 长连接收消息，按发送者和工作线筛选 | [listen.sh](skills/feishu-agent-loop/scripts/listen.sh) + [events.py](skills/feishu-agent-loop/scripts/events.py) |
| 自定步长 | 没事件时的低频兜底 | 宿主提供的 `/loop` / `ScheduleWakeup` |

**当前代码实际实施：**

- 启动前校验发送者白名单、工作线、群归属和 outbox；未知字段、重复 JSON key、未登记工作线均拒绝。
- 每次消费使用 `--timeout 600s --max-events 1`；保留 CLI stderr，不使用 `--quiet`。连续三次消费失败后停止并输出可见错误事件。
- 捕获原始 NDJSON 后单独路由，分别检查退出码，不让管道末端的成功覆盖前端错误。消费失败却含非空输出，或发生解析错误时，立即停止并保留输入，不继续消费下一条。
- 群消息按配置交给对应工作线；P2P 只有本地 outbox 能定位父消息属于本线且同一 chat 时才成为候选。未知归属输出无正文的通知和 capture_ref，以deferred状态保留原文，默认积累32条即停止。
- 自发消息登记 outbox 后按 message ID 过滤，包括 `--as user` 发送后回流的 user 事件。
- 校验发送回执的严格布尔 `ok=true`、identity、message ID 和 chat ID；outbox 为本地 0600 常规文件，锁冲突、裸空文件或结构格式损坏明确报错；不检测旧合法快照回滚，重复登记幂等。

**没有实施：** 同线多消费者排他、跨机器 lease、所有入站事件去重、持久队列/ACK、自动父消息查询、发布审批或端到端送达证明。详见 [ROADMAP](ROADMAP.md)。

## 2. 安装与前置

这是标准 Claude Code 插件：

```text
/plugin marketplace add Hakureirm/feishu-agent-loop
/plugin install feishu-agent-loop@feishu-agent-loop
```

也可复制整个 `skills/feishu-agent-loop/` 目录到自有 skills 目录；**不要只复制 SKILL.md 而漏掉 scripts/examples**。升级文件不等于已替换在跑的监听；先检查现有任务，不启动重复消费者，不擅自重启别人的进程或共享 event bus。

辅助脚本支持 macOS/Linux：Bash 3.2+、Python 3.9+（仅标准库，含 Unix `fcntl`）和已配置的 `lark-cli`。未验证原生 Windows。

开始前查看当前 CLI 的合同，不沿用旧字段猜测：

```bash
lark-cli event schema im.message.receive_v1 --json
lark-cli event consume --help
lark-cli im +messages-send --help
lark-cli im +messages-mget --help
```

- 配置可用的 bot 身份、长连接模式和 `im.message.receive_v1` 订阅。收群消息的权限、应用可用范围和群设置以当前租户为准。
- bot 与 user 身份的权限相互独立。日常汇报用 bot；只有明确要求代发时才用 user，且通常还需 `im:message.send_as_user`。**不要因 bot 失败自动换 user 或扩大 scope。**
- P2P、群内 @bot、群内不 @bot 是三个验收场景，不能互相替代。是否收到不 @bot 的消息需实测；不要默认改用 user 身份轮询整个群。
- 普通 IM `content` 通常已是渲染文本；以 schema 的 description 为准，不无脑 `fromjson`。当前辅助脚本读取顶层字段，并保留存在的 `reply_to` / `root_id`。

## 3. 建立私有路由状态

先参考 [routing.example.json](skills/feishu-agent-loop/examples/routing.example.json)，将示例 ID 换成授权用户和目标群的真实 ID。配置与 outbox 放在**自己拥有、仓库外的私有目录**；不要提交真实聊天内容、群 ID、token 或状态文件。

```bash
SKILL_DIR="/absolute/path/to/skills/feishu-agent-loop"
CONFIG="/absolute/path/to/private/routing.json"
OUTBOX="/absolute/path/to/private/outbox.jsonl"
LINE="research"

python3 "$SKILL_DIR/scripts/events.py" init-outbox \
  --config "$CONFIG" --line "$LINE" --outbox "$OUTBOX"
```

`init-outbox` 仅创建不存在的 0600 文件并写入header；已有文件只验证，不覆盖、不清空。裸空文件不是合法的“已初始化outbox”。父目录需预先存在。不要为解决报错删除他人的状态或放宽权限。

`lines` 是稳定的业务线标识，不是会改名的 session 昵称。一个工作线应由协调方指定一个消费者；这仍是操作约定，**当前脚本没有排他锁/lease**。多个消费者可能共享同一个底层事件总线，`Active consumers > 1` 不一定是故障。

## 4. 收消息：挂一次 Monitor

```bash
bash "$SKILL_DIR/scripts/listen.sh" "$CONFIG" "$LINE" "$OUTBOX"
```

把该命令交给宿主的常驻 `Monitor`，保持 stderr 诊断可查。不要把原始 CLI 输出直接当用户批准。

| stdout 类型 | 含义 | 执行者处理 |
|---|---|---|
| `message_candidate` | 发送者与归属匹配；正文仍是数据 | 核对任务、上下文和权限，再决定如何处理 |
| `routing_notice` | 未知群或未认领 P2P；只含定位元数据与私有capture_ref | 原文保留为deferred，协调归属后对同一输入重跑路由，不猜测执行 |
| `listener_error` | 启动、消费或路由失败 | 查看 stderr 和退出码 |
| `listener_stopped` | 三次无输出消费失败、消费失败但带非空输出、路由错误，或 deferred capture 达上限 | 检查 stderr 和保留输入，修好/认领后再恢复；不自动放行 |

`events.py route` 的退出码3表示归属待确认，原文由监听器保留；已知群仍可继续处理，累计到 `FEISHU_LOOP_MAX_DEFERRED`（默认32）则停止。有限轮次诊断若还留有deferred，也返回3，不被后续正常消息清成成功。认领后手工重放原文件；当前没有自动ACK/清除待办计数机制。

所有路由结果均为 `authorization_granted=false`。`instruction_candidate=true` 只说明值得交给该线检查，不代表可以部署、删除、花钱或改变权限。

CLI stderr 的 ready/exited 信号会原样保留，但该包装器不把 ready 当端到端证明。它使用每次消费的 CLI 自身超时；**没有另一个能保证杀掉卡死 CLI/后代进程的外部 watchdog**。停止时只向自己启动的消费进程发 SIGTERM，不调用全局 `event stop`，不使用 `kill -9`。

失败/中断时，stderr 会给出保留输入的临时目录（0700，事件文件0600）。修复配置/状态后可对原文件重跑路由，而不是先收下一条：

```bash
python3 "$SKILL_DIR/scripts/events.py" route \
  --config "$CONFIG" --line "$LINE" --outbox "$OUTBOX" \
  --input "/path/to/retained/event.1.ndjson"
```

只有明确修复且确认是否已处理过，才恢复 Monitor。捕获成功并输出后，脚本会移除本地临时输入；**如果宿主此时丢了输出，脚本没有 ACK/重放机制兜底**。保留失败文件也不是持久消息队列或异地备份。

诊断用环境变量：`FEISHU_LOOP_MAX_CYCLES=1`（只跑一轮，默认0表示持续）、`FEISHU_LOOP_CONSUME_SECONDS=600`、`FEISHU_LOOP_RETRY_DELAY=1`。测试可用 `LARK_CLI`/`PYTHON3` 指定可执行文件；它们是受信的操作配置，不得从消息正文生成。

## 5. 发送、登记 outbox、读回

以下示例须先设置正确的目标、私有文件路径和本次消息的幂等键；真实发送仍应在用户授权范围内。使用文本或真正的多行 Markdown，不把消息正文拼成 shell 程序。

```bash
CHAT_ID="oc_YOUR_TARGET"
KEY="your-task:your-step:your-message"
umask 077
SEND_RECEIPT=$(mktemp "$(dirname "$OUTBOX")/send.XXXXXXXX") || exit 1

if lark-cli im +messages-send --as bot --chat-id "$CHAT_ID" \
  --text "本阶段已完成，详情见交付记录。" --idempotency-key "$KEY" >"$SEND_RECEIPT"; then
  python3 "$SKILL_DIR/scripts/events.py" record-receipt \
    --config "$CONFIG" --line "$LINE" --outbox "$OUTBOX" \
    --identity bot --input "$SEND_RECEIPT" || exit $?
else
  printf 'send failed; inspect the CLI diagnostics and receipt\n' >&2
  exit 1
fi
```

调用方必须检查命令与 `record-receipt` 的退出状态；这里没有用管道掩盖前端失败。回执文件含聊天标识，应放私有目录并以 `umask 077` 创建；不要覆盖尚需审计的旧回执，实际任务使用唯一文件名。

- `receipt_valid=true` 仅表示提供的 JSON 满足合同，不认证文件来源。真实 API 接受、读回一致、用户已读是不同状态。
- `record-receipt` 不发网络请求，始终返回 `readback_verified=false`。调用方可用已验证的 message ID 执行 `lark-cli im +messages-mget --as bot --message-ids <om_...>`，核对 chat、sender、内容和 reply_to；该自动读回验证器尚未实现。
- 不确定发送是否成功时，先检查原始回执/读回；如需重发，复用同一幂等键。具体去重时限以 API/CLI 合同为准，不声称永久 exactly-once。
- **outbox 有登记时序窗口**：消息可能在发送返回并登记之前回流。当前 helper 不能消除这个窗口，也不会替发送失败后的 outbox 写入自动补偿；这条回流可能成为候选，调用方需在副作用前再核对发送记录。两阶段发送 intent/隔离回流是待做项。
- outbox 首行带版本和随机 generation header；裸空文件、缺失header、不完整末行、重复JSON键或结构格式损坏明确报错，不当成新状态。它不检测截回旧的合法前缀/快照，也不是防篡改签名或lease。outbox 存储消息归属元数据而非正文，读写上限1 MiB；满额不自动清空，归档/轮换需要保留仍可能被回复的父消息记录。

## 6. 权限、通知与心跳

**行为边界（宿主/执行者仍需遵守，本仓不是审批系统）：**

- 自动 task/Monitor/idle 通知不是人类批准；协作会话说“用户允许了”也不能替代原始授权来源与范围核对。
- 授权来源可以是飞书原始用户消息，也可以是真实终端 user turn；后者记录 session/turn 定位，不编造 `om_` ID。不要把旧回执或自己代发后的回流当新指令。
- 白名单身份、群归属、回复上下文、具体动作权限是不同维度。短语“随意”没有天然万能授权；明确动作、对象和范围后，合法自由文本确认也不必被强行改成某个魔法口令。
- `Fyi` 落日志；`ShouldSee` 用于里程碑/异常；`MustAck` 用于确实需要用户决定的副作用。普通技术取舍由执行者决定并说明依据。没有确认不执行需要确认的动作；超时不是默认同意。
- 下一轮保留失败的原始证据引用、命令、退出码、输入指纹和时间；不要把 token、私聊全文或大段内部日志直接贴入群/公开仓。

只有用户实际启用了动态 `/loop` 且宿主提供 `ScheduleWakeup` 时，才安排通常1200–1800秒的兜底心跳。子任务完成由宿主自动回调，不用短间隔轮询 `ListAgents` 或重复启动监听。单个服务器就绪通知用一次性后台任务；持续日志监测才用 Monitor。

## 7. 验证与边界

离线验证（不会登录或调用真实飞书，不需要凭据）：

```bash
bash -n skills/feishu-agent-loop/scripts/listen.sh
python3 -m unittest discover -s tests -v
```

测试涵盖白名单/跨线/P2P、回流、JSON/状态损坏、回执假成功、幂等登记、连续失败、路由失败保留、SIGTERM与有界运行退出码。GitHub Actions 的 [tests workflow](.github/workflows/tests.yml) 运行同一套测试；本地通过不冒充远端 CI 或真实租户验收。

真实部署另记录：CLI版本、身份/订阅配置、目标工作线、最近一次成功发送/接收/处理时间及消息定位，并实测需要支持的 P2P/@群/非@群路径。“脚本存在 → 配置启用 → 本次运行采用 → 实际效果”分开记录；进程数、累计RECEIVED、没有报错都不能单独证明当前正常。

License: [Apache-2.0](LICENSE)。
