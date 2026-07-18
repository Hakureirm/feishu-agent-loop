---
name: feishu-agent-loop
description: 让长时间自主任务(/loop、多小时评测/训练/迁移)通过飞书(Lark)被人在手机上遥控——Agent 用 bot 主动推 markdown 进度,用户回复经长连接回调秒级唤醒 Agent 改方向。当用户说"用飞书盯着/汇报进度/我在飞书上指挥你/边跑边报"时启用。依赖已登录的 lark-cli + 一个 bot 应用。
---

# Feishu-Driven Autonomous Agent Loop

把一个跑很久的自主任务,变成"人在飞书上遥控、Agent 边跑边报"的闭环。三块:**bot 推进度** + **长连接回调唤醒** + **/loop 自定步长**。

## 何时用
- 用户要跑一个数十分钟到数小时的自主任务(评测、训练、批量迁移、压测…)且希望**在手机飞书上盯着并随时改方向**。
- 触发词:"用飞书汇报/盯着"、"我在飞书上指挥"、"边跑边报进度到飞书"、"回复能唤醒你"。

## 前置检查(先做)
1. `lark-cli` 已登录:`lark-cli api GET /open-apis/authen/v1/user_info` 回 code 0。
2. 有 bot 应用,权限含 `im:message`(发)+ `im:message.p2p_msg:readonly`(收 P2P)。
3. 开发者后台 → 事件与回调 → **长连接模式** → 已订阅 `im.message.receive_v1`(没订阅则连上也 RECEIVED:0)。
4. 拿到 bot↔用户的 **P2P 会话 id**(`oc_...`)或用户 `open_id`(`ou_...`)。
   - 查 P2P 会话最近消息:`lark-cli api GET /open-apis/im/v1/messages --as bot --params '{"container_id_type":"chat","container_id":"<oc_...>","sort_type":"ByCreateTimeDesc","page_size":5}'`

## 步骤

### 1. 装回调(常驻 Monitor,唤醒源)
把下面挂成 persistent Monitor —— 用户一回消息就以 `<task-notification>` 唤醒你的 `/loop`:
```bash
while true; do
  lark-cli event consume im.message.receive_v1 --as bot --quiet --timeout 600s \
    --jq '{from:(.sender_id//"?"),mtype:(.message_type//"?"),content:(.content//"")}'
done
```
- **必须** `--timeout`(有界化):无界 consume 在 Monitor 里读 stdin=EOF 会秒退。
- jq 字段在**顶层**:`.sender_id`/`.message_type`/`.content`。别用嵌套路径。
- `lark-cli event status` 见 `Active consumers=1` 即成。只装一次;后续 loop 先 TaskList,已在跑就跳过。

### 2. 发进度(每次必验 ok)
```bash
lark-cli im +messages-send --as bot --chat-id <oc_...> \
  --markdown "## 标题
| 指标 | 值 |
|---|---|
| a | 1 |" \
  --idempotency-key <本条唯一key>
```
- 结构化数据(对比表/曲线/配置)用 `--markdown`(飞书渲染富文本表格);纯文字用 `--text`。
- **验证送达**:解析返回 JSON,必须 `ok==true` 且 `data.message_id` 以 `om_` 开头。返回了 JSON envelope ≠ 送达。
- 不确定上条成没成 → 带 `--idempotency-key` 重发不重复。

### 3. 自定步长
- 用 `/loop <任务>`;每轮末尾 `ScheduleWakeup`(兜底心跳 1200–1800s)。真正的即时唤醒靠步骤 1 的回调事件。
- 被 `<task-notification>` 唤醒(用户回复 / 后台任务完成)→ 处理 → 再 `ScheduleWakeup` 续。

## 绝不要
- ❌ 用 `im +send` 或 `--receive-id*` 发消息 —— 那是错命令/错 flag,返回 `ok:false` **静默不送达**。只用 `+messages-send` + `--chat-id`/`--user-id`。
- ❌ 发完不验 `ok:true`+`message_id` 就当发出去了。
- ❌ 无 `--timeout` 挂 consume(秒退)。
- ❌ 把 bot↔用户 P2P 会话和用户自聊会话搞混。

## 汇报节奏建议
- 关键里程碑(阶段完成 / 数字锁定 / 需决策)立即发。
- 慢进度(单步数十分钟)别每 10 分钟刷"还在跑";有实质进展或完成才发。
- 需要用户拍板时,把选项列清楚发过去,默认值写明,等回调。

## 参考
同目录 `README.md` 有完整模式说明 + 真实样例时间线 + 6 个血泪坑。相关:仓库自带的 `lark-*` 系列 skill(wiki/doc/im/…)。
