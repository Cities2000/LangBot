# 企业微信 → LangBot → Dify 完整数据流与通信分析

## 一、总体架构

```
企业微信AI Bot ←→ LangBot(中间层) ←→ Dify(后端AI)
   (WS/HTTP)        (Pipeline)         (SSE/HTTP)
```

LangBot 是核心中间层，有两个通信入口：

- **WebSocket模式** (`WecomBotWsClient`): 持久连接到 `wss://openws.work.weixin.qq.com`
- **Webhook模式** (`WecomBotClient`): 企微通过 HTTP POST 回调到 LangBot

---

## 二、完整数据流（以WebSocket模式为例）

### 阶段1：企微 → LangBot（消息接收）

1. **企微推送消息帧** → `ws_client.py:470 _handle_message_callback()`
   - 帧结构: `{cmd: "aibot_msg_callback", headers: {req_id}, body: {加密消息体}}`
   - 生成 `stream_id` 和 `req_id` 映射，存入 `_stream_ids[msg_id]` 和 `_stream_sessions[msg_id]`

2. **消息解密与解析** → `api.py:380 parse_wecom_bot_message()`
   - 解密AES加密的文件（图片/语音/文件用per-message aeskey）
   - 按msgtype（text/image/voice/video/file/mixed/link）解析为统一dict
   - 提取 `userid`, `username`, `chatid`, `msgid`, `aibotid` 等字段
   - 图片自动下载并转为base64 data URI

3. **构造事件对象** → `wecombotevent.py:WecomBotEvent`
   - 封装为dict子类，提供 `.content`, `.userid`, `.stream_id` 等属性访问

### 阶段2：LangBot内部转换

4. **Adapter层转换** → `wecombot.py:396 on_message()`
   - `WecomBotEventConverter.target2yiri()` 将企微事件转为LangBot统一事件（`FriendMessage`/`GroupMessage`）
   - `WecomBotMessageConverter.target2yiri()` 将消息转为 `MessageChain`（Plain/Image/File/Voice等）
   - 关键数据: `source_platform_object` 保留了原始 `WecomBotEvent`（含 `stream_id`, `message_id`）

5. **触发回调** → `listeners[EventType](event, adapter)` 进入Pipeline

### 阶段3：Pipeline处理

6. **RuntimePipeline.run()** → `pipelinemgr.py:114`
   - 记录监控: `monitoring_helper.record_query_start()` → 生成 `message_id`，记录 status=`pending`
   - 建立 `stream_id → monitoring_message_id` 映射（`adapter.on_monitoring_message_created()`）

7. **责任链执行** (`_execute_from_stage`)，依次经过：
   - **RespruleStage**: 响应规则匹配
   - **BansessStage**: 会话封禁检查
   - **CntfilterStage**: 内容过滤
   - **PreProcessorStage** (`preproc.py`):
     - 获取session、conversation、prompt
     - 设置query.variables（`session_id`, `conversation_id`, `sender_name`等）
     - 将MessageChain转为 `provider_message.Message`（content为ContentElement列表）
     - 触发 `PromptPreProcessing` 插件事件
   - **MessageProcessorStage** (`process.py` → `chat.py`):
     - 查找Runner（如 `dify-service-api`）
     - 调用 `runner.run(query)` — **这里进入Dify调用**

### 阶段4：LangBot → Dify（API调用）

8. **DifyServiceAPIRunner** → `difysvapi.py`
   - `_preprocess_user_message()`:
     - 提取纯文本 `plain_text`
     - 图片/文件: 通过 `dify_client.upload_file()` **先上传到Dify**，获得 `upload_file_id`
     - 返回 `(plain_text, [{type, id}])`

9. **调用Dify API** → `dify_client.chat_messages()` / `workflow_run()`
   - 协议: **SSE (Server-Sent Events)**，通过 `httpx.stream('POST', ...)`
   - 请求头: `Authorization: Bearer {api_key}`
   - 请求体: `{inputs, query, user, response_mode: "streaming", conversation_id, files}`
   - 响应: 逐行解析 `data:` 前缀的SSE chunk，`json.loads` 转为dict

10. **Dify返回chunk处理**（以chat模式为例）→ `_chat_messages_chunk()`
    - `event=message`: 累积 `basic_mode_pending_chunk += chunk['answer']`
    - `event=message_end`: 标记 `is_final=True`
    - `event=node_finished` (workflow模式): 提取answer输出
    - 每8个chunk或最终chunk → `yield MessageChunk(content=..., is_final=...)`
    - 思维链处理: `<think>`/`</think>` 标签的提取与过滤

### 阶段5：Dify结果 → 企微（流式回传）

11. **chat.py流式输出** → 每个 `MessageChunk` yield 一个 `StageProcessResult(CONTINUE)`
    - 触发 `_check_output()` → `adapter.reply_message_chunk()`

12. **WecomBotAdapter.reply_message_chunk()** → `wecombot.py:358`
    - **WebSocket模式**: `bot.push_stream_chunk(msg_id, content, is_final)` → `ws_client.py:261`
      - 通过 `reply_stream(req_id, stream_id, content, finish, feedback_id)` 发送WS帧
      - 帧结构: `{cmd: "aibot_respond_msg", headers: {req_id}, body: {msgtype: "stream", stream: {id, content, finish, feedback}}}`
      - 内容去重: 相同content不重复发送
      - 最终帧携带 `feedback_id`，用于后续点赞/点踩
    - **Webhook模式**: 写入 `StreamSessionManager` 队列，企微刷新时消费

13. **企微消费流式响应**（Webhook模式）:
    - 企微定期POST刷新请求 → `_handle_post_followup_response()` → `stream_sessions.consume()` 从队列取chunk → 加密返回

### 阶段6：监控与反馈

14. **监控记录**:
    - `record_query_start()`: 记录用户消息，status=`pending`
    - `record_query_success()`: 更新 status=`success`，写入query.variables
    - `record_query_response()`: 记录bot回复消息
    - `record_query_error()`: 记录错误，写入 `monitoring_errors` 表
    - `record_llm_call()`: 记录LLM调用（token数、耗时、费用）
    - 所有数据持久化到数据库: `monitoring_messages`, `monitoring_llm_calls`, `monitoring_errors`, `monitoring_sessions`

15. **用户反馈**（点赞/点踩）:
    - 企微推送 `feedback_event` → 通过 `feedback_id` 查找 `StreamSession`
    - 转为 `FeedbackEvent` → 通过 `stream_id` 映射到 `monitoring_message_id`

---

## 三、关键数据结构对照

| 环节 | 标识符 | 用途 |
|------|--------|------|
| 企微消息 | `msgid` | 消息唯一ID，去重 |
| 企微流式 | `stream_id` | 流式会话标识 |
| 企微请求 | `req_id` | WS帧请求标识，ACK匹配 |
| 企微反馈 | `feedback_id` | 关联点赞/点踩到消息 |
| Dify对话 | `conversation_id` | 会话持续 |
| Dify用户 | `user` = `{launcher_type}_{launcher_id}` | Dify侧用户标识 |
| LangBot监控 | `monitoring_message_id` | 监控系统消息ID |
| LangBot会话 | `session_id` = `{launcher_type}_{launcher_id}` | 会话标识 |

---

## 四、接口数据监控点

| 监控点 | 位置 | 可观测内容 |
|--------|------|------------|
| **WS帧接收** | `ws_client._handle_frame()` | cmd类型、req_id、消息体 |
| **消息解析** | `api.parse_wecom_bot_message()` | msgtype、content、userid、chatid |
| **Pipeline入口** | `pipelinemgr.process_query()` | query_id、pipeline_name、bot_name |
| **Dify请求** | `dify_client.chat_messages()` | inputs、query、user、conversation_id、files |
| **Dify响应chunk** | `difysvapi._chat_messages_chunk()` | event类型、answer内容、conversation_id |
| **流式回传** | `ws_client.reply_stream()` | stream_id、content、finish、feedback_id |
| **监控记录** | `monitoring_helper` / `MonitoringService` | 全量消息/错误/LLM调用/会话 |
| **反馈事件** | `wecombot._on_feedback()` | feedback_type、content、session关联 |

监控数据存储在5张数据库表中：`monitoring_messages`、`monitoring_llm_calls`、`monitoring_errors`、`monitoring_sessions`、`monitoring_embedding_calls`，通过 `MonitoringService` 的 HTTP API 可以查询。

---

## 五、WebSocket时间控制与断连机制

### 5.1 心跳与连接存活检测

核心在 `ws_client.py:385-410` 的 `_heartbeat_loop()`：

```python
# 默认参数（构造函数 line 60-63）
heartbeat_interval = 30.0        # 每30秒发一次ping
_max_missed_pong = 2             # 连续2次未收到pong响应 → 判定连接死亡
```

**时间线：**

```
T+0s    发送 ping → _missed_pong_count += 1 (=1)
T+30s   发送 ping → _missed_pong_count += 1 (=2)
T+60s   _missed_pong_count(2) >= _max_missed_pong(2) → 主动关闭WS，触发重连
```

即**最长60秒**未收到心跳响应就判定连接断开。每次收到 pong ACK 时（`_handle_frame` line 462-465），`_missed_pong_count` 重置为0。

**判断逻辑（line 393-398）：**
```python
if self._missed_pong_count >= self._max_missed_pong:
    # 连接被认为已死，主动close
    await self._ws.close()
    break  # 跳出心跳循环 → _listen_loop也结束 → _connect_once返回 → 进入重连
```

### 5.2 认证超时

`_wait_for_auth()` (line 365-383)：

```python
msg = await asyncio.wait_for(self._ws.receive(), timeout=10.0)
```

- **认证超时: 10秒**，超时后返回 `False`，`_connect_once` 直接 return（不进入listen循环），回到 `connect()` 的重连循环

### 5.3 重连策略（指数退避）

`connect()` (line 108-139)：

```python
# 默认参数
max_reconnect_attempts = -1      # -1 = 无限重试
reconnect_base_delay = 1.0       # 首次等待1秒
reconnect_max_delay = 30.0       # 最大等待30秒
```

**退避公式：**
```
delay = min(base_delay * 2^(attempt-1), max_delay)
```

| 重试次数 | 等待时间 |
|---------|---------|
| 第1次 | 1s |
| 第2次 | 2s |
| 第3次 | 4s |
| 第4次 | 8s |
| 第5次 | 16s |
| 第6次+ | 30s（上限） |

重连成功后 `_reconnect_attempts` 重置为0（line 323）。

### 5.4 企微断连（被动断开）

当企微侧断开时，`_listen_loop()` (line 412-433) 检测到：

```python
elif msg.type in (aiohttp.WSMsgType.ERROR,
                  aiohttp.WSMsgType.CLOSED,
                  aiohttp.WSMsgType.CLOSING):
    break  # 跳出listen循环
```

`_listen_loop` 退出 → `_connect_once` 的 finally 块清理WS和session → 返回到 `connect()` 的 while循环 → 进入指数退避重连。

断连时还会清理挂起的ACK（line 344）：
```python
self._clear_pending_acks('Connection closed')
# 对所有 pending future 设置 ConnectionError 异常
```

### 5.5 LangBot主动断开

`disconnect()` (line 141-154)：

```python
self._running = False           # 1. 标记停止，阻止重连循环
heartbeat_task.cancel()          # 2. 取消心跳任务
reply_workers全部cancel          # 3. 取消回复队列工作协程
ws.close()                       # 4. 关闭WebSocket
session.close()                  # 5. 关闭HTTP session
```

关键：`_running = False` 使得 `connect()` 的 while循环退出，**不会触发重连**。

### 5.6 回复ACK超时

`_send_and_wait_ack()` (line 654-671)：

```python
self._reply_ack_timeout = 5.0   # 发送回复后5秒内未收到ACK → 超时
```

超时后不重试，只打warning日志，返回 `None`。

### 5.7 Stream会话过期

`StreamSessionManager.cleanup()` (api.py line 221-244)：

```python
ttl = 60                         # 普通会话60秒过期
_FEEDBACK_SESSION_TTL = 600      # 有feedback_id的会话10分钟过期
```

在Webhook模式中，每次POST回调时触发 `cleanup()`（api.py line 977）。WebSocket模式中stream生命周期由 `push_stream_chunk` 管理，最终chunk发送后立即清除映射。

### 5.8 时间控制全景总结

| 场景 | 超时/间隔 | 行为 |
|------|----------|------|
| 心跳发送 | 每30秒 | 发ping帧 |
| 心跳超时 | 连续2次无pong（~60秒） | 主动close → 重连 |
| 认证超时 | 10秒 | 放弃本次连接 → 重连 |
| 重连退避 | 1s→2s→4s→...→30s | 指数退避，默认无限重试 |
| ACK超时 | 5秒 | 不重试，warn日志 |
| Stream会话过期 | 60秒/10分钟(有feedback) | 清除映射，消息丢弃 |
| 主动断开 | 立即 | `_running=False`，不重连 |
