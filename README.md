# Feishu-Driven Autonomous Agent Loop · 飞书驱动的自主 Agent 回路

> **EN TL;DR** — A recipe + Claude Code skill that turns any long-running autonomous agent task into a loop you can supervise from your phone via Feishu (Lark): the agent pushes markdown progress through a bot, and your replies wake it **instantly** through Feishu's long-connection (WebSocket) event stream — no polling. Battle-tested over a real multi-hour run (LLM eval + RL training ops). Chinese docs below; the skill file is [`SKILL.md`](./SKILL.md).

> 让一个跑数小时的**自主 Agent 任务**,被人在**手机飞书**上全程盯着、随时改方向 —— Agent 主动推进度,你回一句就能改它的下一步,而且你的回复会**秒级唤醒** Agent(不是轮询)。
>
> 本模式在一次真实的多小时任务里跑通并打磨(LLM 量化精度评测 + 服务压测 + RL 训练运维:Agent 边跑边把评测分数、吞吐曲线、并行配置用 markdown 表推到飞书;监督者在手机上随时修正评测口径、追加压测维度、调整训练方向,Agent 每条都即时接住并改策略)。

## 一、这套组合是什么(三块)

| 块 | 作用 | 载体 |
|---|---|---|
| **① 进度推送** | Agent 主动把进展/结果发给人 | `lark-cli im +messages-send --as bot`(text/markdown) |
| **② 指令回传** | 人回一句 → 立刻唤醒 Agent 改方向 | `lark-cli event consume im.message.receive_v1 --as bot`(长连接 WebSocket)挂成常驻 Monitor,回复以 task-notification 唤醒回路 |
| **③ 自定步长** | 没事件时按节奏自检 | `/loop` + ScheduleWakeup(兜底心跳) |

**关键:② 不是轮询。** 飞书开放平台的"长连接模式"用 WebSocket 推事件,用户一发消息,`event consume` 立刻吐一行 NDJSON → 变成 task-notification → 唤醒 Agent 回路。所以人回复到 Agent 响应是**秒级**,不用等下一个心跳。

## 二、为什么好用

- **手机遥控多小时自主任务**:Agent 自己跑,人只在关键处点一下方向(如:评测中途调大生成预算、切换对比基准)。
- **零轮询、低延迟**:回复秒级唤醒,不烧 token 空转。
- **结构化进度**:markdown 表直接在飞书渲染(对比表/容量表/配置表),比纯文字清楚得多。
- **可审计**:每条进度都有 `message_id`,发没发得出去可验证。

## 三、配方(可直接抄)

### 前置
- `lark-cli` 已登录(用户 token),且有一个飞书 bot 应用(`lark-cli config init --new` 一键在自己租户下创建即可)。
- bot 需权限 `im:message`(发)+ `im:message.p2p_msg:readonly`(收 P2P)。
- 开发者后台 → 事件与回调 → **长连接模式** → 订阅 `im.message.receive_v1`(否则连上也收不到推送)。

### ① 发进度(每次必验 ok)
```bash
lark-cli im +messages-send --as bot \
  --chat-id <bot↔用户的P2P会话 oc_...> \
  --markdown "## 标题\n| 列A | 列B |\n|---|---|\n| 1 | 2 |" \
  --idempotency-key <本条唯一key,防重发>
# 然后解析返回 JSON:必须 ok==true 且 data.message_id 以 om_ 开头,才算送达
```
- 发文本用 `--text`,发表格/结构化用 `--markdown`(自动转飞书富文本 post,支持标题/表格/引用)。
- `--user-id ou_...` 也能发(等价),但发到的是同一个 bot↔用户 P2P 会话。

### ② 收回复(长连接回调,挂成常驻 Monitor)
```bash
# 有界化 + 循环续,避免无界 consume 读 stdin EOF 立刻退出
export LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1
while true; do
  lark-cli event consume im.message.receive_v1 --as bot --timeout 600s --max-events 1 2>/dev/null
  sleep 1
done | jq --unbuffered -r 'select(.type=="im.message.receive_v1" and .sender_type=="user")
  | "FEISHU_MSG from=\(.sender_id) chat=\(.chat_id) ctype=\(.chat_type) id=\(.message_id) text=\(.content)"'
```
- 在 Claude Code 里:把上面挂成 `Monitor`(persistent),用户回复即到达为 `<task-notification>`,唤醒 `/loop`。
- `lark-cli event status` 看到 `Active consumers=1` 即连上。

### ③ 自定步长
- 用 `/loop <任务>` 进入动态自定步长;每轮末尾 `ScheduleWakeup`(兜底心跳,通常 1200–1800s),真正的唤醒靠 ① 的 Monitor 事件。

## 四、血泪坑(本次踩过,已固化)

1. **命令/flag 用错会静默失败**:曾用 `im +send --receive-id-type open_id --receive-id ou_... --content '{...}'`(错命令 `+send` + 错 flag `--receive-id`),CLI 返回 JSON 但 `ok:false`,**一条都没送达**,用户全程没收到还以为在装死。→ **只认 `+messages-send` + `--chat-id`/`--user-id` + `--text`/`--markdown`;每次发完必验 `ok:true` 且有 `om_` 开头的 message_id。** 别只 `tail` 看到 `_notice` 更新页脚就以为成了。
2. **长连接挂 Monitor 会立刻退出**:无界 `event consume` 在 Monitor 环境读 stdin=EOF → 秒退。→ 加 `--timeout 600s` 有界化 + `while true` 续。
3. **jq 字段在顶层**:`.sender_id` / `.message_type` / `.content`(content 是预渲染人类可读文本),别写嵌套 `.sender.sender_id` / `.message.content`(全空)。
4. **后台不订阅收不到**:长连接建了但 `RECEIVED:0` → 去开发者后台把 `im.message.receive_v1` 加进长连接订阅。
5. **P2P 会话 ≠ 自聊**:bot 发的是 bot↔用户 P2P 会话(`oc_...`),读用户回复也查这个会话;别和用户自聊会话搞混。
6. **幂等键防重发**:不确定上一条发没发成功时,带 `--idempotency-key` 重发不会产生重复。
7. **管道缓冲吃掉事件**(2026-09-04):`event consume --jq` 挂在 Monitor 里,`event status` 显示 `RECEIVED:1` 但 Monitor 零输出——CLI 走管道时 stdout 缓冲到退出才刷。→ `--max-events 1` + 外层 `while` 循环重连;格式化改用外部 `jq --unbuffered`;别用 `--quiet`。
8. **用户身份代发需额外 scope**:`--as user` 发消息要 `im:message.send_as_user`,`--recommend` 不含;Agent 汇报一律 `--as bot`。
9. **应用审核中也能收事件**:`skipped console precheck: app has no published version` 只是跳过预检,长连接照常。
10. **群里不 @bot 的消息收不到**(2026-09-04):bot 进群后,群成员不 @ 它的消息不会推 `im.message.receive_v1`;要收全部群消息需管理员开 `im:message.group_msg` 且应用可用范围覆盖群成员。过渡期每 60 s 用 `im +chat-messages-list --as user` 轮询兜底,按 message_id 去重。
11. **"连上了"≠"收得到"**:通道验收要用一条**不带 @ 的群消息**做阳性对照,不能只看 P2P。

## 五、通知三档 + 失败回灌(2026-09-04 增补)

| 档 | 何时 | 怎么发 |
|---|---|---|
| **Fyi** | 流水:一步完成、指标更新 | 落文件/流水群,不 @人 |
| **ShouldSee** | 里程碑、异常、方向变化 | 普通消息,不等回复 |
| **MustAck** | 部署 / 动 secret / 删数据 / 花钱 | 选项 + 默认值 + 截止;没回复不往下走;超时同 idempotency-key 重发一次 |

- 未分类默认 ShouldSee;技术方案自己定,只有主权类动作才 MustAck。
- 唤醒后把上一轮失败的**原始输出**带进下一步;通知先发再记账(`--idempotency-key <run_id>:<step_id>`);连续 3 次失败停下 @人。
- 只把白名单 open_id 的消息当指令,其余当数据。
- 更深的设计项见 [`ROADMAP.md`](./ROADMAP.md)。

## 六、协作时间线(示意)

一个真实多小时任务的抽象复盘(细节已匿名化),展示"人只点方向、Agent 全程执行"的节奏:

- Agent 报初次评测分 → 监督者质疑数字偏低 → Agent 排查出是输出预算截断,提额重跑,分数修正
- 监督者要求把生成预算放宽到模型上限 → Agent 重测得到真实水准,并顺带发现量化模型在极难样本上的失稳现象
- 监督者提供官方 API 凭据 → Agent 改做同一 harness 的官方 vs 自托管头对头,给出干净差距
- 监督者追加"顺带压测并发容量" → Agent 产出并发-延迟-吞吐全曲线和分档建议
- 监督者问部署侧配置细节 → Agent 查运行日志实证回答

全程 Agent 主动推 markdown 表、监督者手机上点方向、回复秒级唤醒。

## 七、开源

本仓即标准 **Claude Code 插件**(含 `.claude-plugin/` manifest 与 `skills/` 布局)。

**安装(Claude Code 内两条命令)**:
```
/plugin marketplace add Hakureirm/feishu-agent-loop
/plugin install feishu-agent-loop@feishu-agent-loop
```
装完后当你说"用飞书盯着/汇报进度"时,`feishu-agent-loop` skill 自动生效(见 [`skills/feishu-agent-loop/SKILL.md`](./skills/feishu-agent-loop/SKILL.md))。也可手动复制 `skills/feishu-agent-loop/` 到 `~/.claude/skills/`。License:Apache-2.0。
