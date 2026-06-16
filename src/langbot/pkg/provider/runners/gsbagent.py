# Deployment path: LangBot src/langbot/pkg/provider/runners/gsbagent.py
# Source maintained in GsbAgent repo at src/langbot/runners/gsbagent.py

from __future__ import annotations

import base64
import json
import typing

import httpx

from .. import runner
from ...core import app
from ...utils import image
import langbot_plugin.api.entities.builtin.pipeline.query as pipeline_query
import langbot_plugin.api.entities.builtin.provider.message as provider_message


@runner.runner_class('gsb-agent')
class GsbAgentRunner(runner.RequestRunner):
    """GsbAgent service API request runner.

    Calls GsbAgent's unified /api endpoints and maps AgentEvent streams
    to LangBot Message/MessageChunk objects.
    """

    def __init__(self, ap: app.Application, pipeline_config: dict):
        self.ap = ap
        self.pipeline_config = pipeline_config

        gsb_config = self.pipeline_config['ai']['gsb-agent']
        self.base_url = gsb_config['base-url'].rstrip('/')
        self.api_key = gsb_config.get('api-key', '')
        self.default_agent = gsb_config.get('default-agent', '')
        self.timeout = gsb_config.get('timeout', 120)

    def _get_headers(self) -> dict[str, str]:
        """Return request headers with optional Authorization."""
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'
        return headers

    async def _get_agent_info(self, agent_id: str) -> dict:
        """获取 Agent 信息，包含解析后的 stream_placeholder。"""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f'{self.base_url}/api/agents/{agent_id}',
                headers=self._get_headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get('data', data)

    async def _get_encoding_aes_key(self, query: pipeline_query.Query) -> str:
        """从 LangBot bot 配置获取 EncodingAESKey（用于企微文件解密）。

        查找路径：
        1. query.bot_uuid → platform_mgr.get_bot_by_uuid() → bot_entity.adapter_config
        2. adapter_config['EncodingAESKey'] (WebSocket 模式)
        3. adapter_config.get('EncodingAESKey', '') (兼容不同大小写)
        """
        if not query.bot_uuid:
            return ''

        try:
            bot = await self.ap.platform_mgr.get_bot_by_uuid(query.bot_uuid)
            if not bot:
                return ''
            adapter_config = bot.bot_entity.adapter_config or {}
            # 尝试多种可能的 key 名称
            for key in ('EncodingAESKey', 'encoding_aes_key', 'encodingAESKey'):
                if adapter_config.get(key):
                    return adapter_config[key]
        except Exception as e:
            self.ap.logger.warning(f'GsbAgent: failed to get EncodingAESKey: {e}')

        return ''

    async def _extract_text_and_files(
        self, query: pipeline_query.Query
    ) -> tuple[str, list[str], dict[str, bytes]]:
        """Extract text content and file IDs from the user message.

        Returns (plain_text, file_ids, file_contents).
        file_contents: {filename: bytes} — 下载解密后的文件内容，直接交给 Agent
        """
        plain_text = ''
        file_ids: list[str] = []
        file_contents: dict[str, bytes] = {}

        if isinstance(query.user_message.content, str):
            plain_text = query.user_message.content
        elif isinstance(query.user_message.content, list):
            for ce in query.user_message.content:
                if ce.type == 'text':
                    plain_text += ce.text
                elif ce.type == 'image_base64':
                    # Upload image to GsbAgent and get a file_id
                    image_b64, image_format = await image.extract_b64_and_format(ce.image_base64)
                    file_bytes = base64.b64decode(image_b64)
                    file_id = await self._upload_file(
                        f'image.{image_format}', file_bytes, f'image/{image_format}',
                        f'{query.session.launcher_type.value}_{query.session.launcher_id}'
                    )
                    if file_id:
                        file_ids.append(file_id)
                elif ce.type == 'file_url':
                    file_url = getattr(ce, 'file_url', None)
                    # ContentElement 可能用不同属性名暴露文件名
                    file_name = (
                        getattr(ce, 'file_name', None)
                        or getattr(ce, 'name', None)
                        or getattr(ce, 'filename', None)
                        or 'file'
                    )
                    if file_url:
                        try:
                            # api.py 把 per_msg_aeskey 拼接在 URL 后面: url?aeskey=xxx
                            # 下载时需要移除，用原始 URL 下载；解密时用提取的 aeskey
                            download_url = file_url
                            per_msg_aeskey = ''
                            if '?aeskey=' in file_url:
                                idx = file_url.index('?aeskey=')
                                download_url = file_url[:idx]
                                per_msg_aeskey = file_url[idx + len('?aeskey='):]

                            # per_msg_aeskey 优先（与 _safe_download 保持一致），平台 EncodingAESKey 兜底
                            aeskey = per_msg_aeskey or await self._get_encoding_aes_key(query)

                            headers = {
                                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                            }
                            async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                                resp = await client.get(download_url, headers=headers)
                                resp.raise_for_status()
                                data = resp.content

                            # 如果 Runner 拿到的文件名没有扩展名，尝试从 Content-Disposition 提取
                            if '.' not in file_name or file_name == 'file':
                                cd = resp.headers.get('content-disposition', '')
                                if cd:
                                    import re
                                    from urllib.parse import unquote
                                    m = re.search(r"filename\*=UTF-8''([^;\s]+)", cd, re.IGNORECASE)
                                    if m:
                                        file_name = unquote(m.group(1))
                                    else:
                                        m = re.search(r'filename="?([^";\s]+)"?', cd, re.IGNORECASE)
                                        if m:
                                            file_name = unquote(m.group(1))

                            if aeskey:
                                from langbot.libs.wecom_ai_bot_api.api import _decrypt_file
                                data = _decrypt_file(data, aeskey)

                            file_contents[file_name] = data
                        except Exception as e:
                            self.ap.logger.warning(f'GsbAgent: file download/upload failed: {e}')

        if not plain_text:
            plain_text = self.pipeline_config['ai']['gsb-agent'].get('base-prompt', '')

        return plain_text, file_ids, file_contents

    async def _upload_file(
        self, filename: str, data: bytes, content_type: str, user: str
    ) -> str | None:
        """Upload a file to GsbAgent and return the file_id.

        Returns None if the upload fails (caller should handle gracefully).
        """
        try:
            headers = {}
            if self.api_key:
                headers['Authorization'] = f'Bearer {self.api_key}'
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f'{self.base_url}/api/files/upload',
                    headers=headers,
                    files={'file': (filename, data, content_type)},
                    data={'user': user},
                )
                resp.raise_for_status()
                result = resp.json()
                return result.get('data', result).get('file_id')
        except Exception as e:
            self.ap.logger.warning(f'GsbAgent: file upload failed: {e}')
            return None

    async def _execute_blocking(
        self, query: pipeline_query.Query, agent_id: str, payload: dict
    ) -> typing.AsyncGenerator[provider_message.Message, None]:
        """Blocking mode: wait for the complete result and yield Messages.

        Handles both plain-text and file-containing responses.  When files
        are present, image files are yielded first (as image content
        elements) followed by any text result.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f'{self.base_url}/api/agents/{agent_id}/execute',
                headers=self._get_headers(),
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        result = data.get('data', data)

        # Handle responses that include files
        files = result.get('files', [])
        if files:
            for f in files:
                if f.get('file_type') == 'image':
                    url = f.get('url', '')
                    full_url = url if url.startswith('http') else f'{self.base_url}{url}'
                    yield provider_message.Message(
                        role='assistant',
                        content=[provider_message.ContentElement.from_image_url(full_url)],
                    )
                elif f.get('file_type') == 'word':
                    filename = f.get('filename', 'file')
                    url = f.get('url', '')
                    full_url = url if url.startswith('http') else f'{self.base_url}{url}'
                    if self._current_platform in ('wecom', 'wecombot'):
                        yield provider_message.Message(
                            role='assistant',
                            content=f'\U0001f4ce {filename}\n下载链接：{full_url}',
                        )
                    else:
                        yield provider_message.Message(
                            role='assistant',
                            content=f'\U0001f4ce {filename}: {full_url}',
                        )
                else:
                    filename = f.get('filename', 'file')
                    url = f.get('url', '')
                    if url:
                        full_url = url if url.startswith('http') else f'{self.base_url}{url}'
                        yield provider_message.Message(
                            role='assistant',
                            content=f'\U0001f4ce {filename}: {full_url}',
                        )
                    else:
                        yield provider_message.Message(
                            role='assistant',
                            content=f'\U0001f4ce {filename}',
                        )
            text_content = result.get('result', '')
            if isinstance(text_content, dict):
                text_content = json.dumps(text_content, ensure_ascii=False, indent=2)
            if text_content:
                yield provider_message.Message(role='assistant', content=str(text_content))
        else:
            content = self._format_result(result)
            yield provider_message.Message(role='assistant', content=content)

        # Update conversation ID for multi-turn tracking
        conv_id = result.get('conversation_id')
        if conv_id and not query.session.using_conversation.uuid:
            query.session.using_conversation.uuid = conv_id

    async def _execute_streaming(
        self, query: pipeline_query.Query, agent_id: str, payload: dict
    ) -> typing.AsyncGenerator[provider_message.MessageChunk, None]:
        """Streaming mode: consume SSE AgentEvent stream and yield MessageChunks.

        Event type mapping:
          agent_message  -> text content
          agent_thought  -> prefixed with thinking emoji
          agent_tool_call -> tool_calls list
          agent_file (image) -> image content element
          agent_file (other) -> file link text
          agent_end      -> is_final=True sentinel
          agent_error    -> error text with is_final=True
        """
        stream_timeout = httpx.Timeout(self.timeout, read=600.0)
        msg_seq = 0

        async with httpx.AsyncClient(timeout=stream_timeout) as client:
            async with client.stream(
                'POST',
                f'{self.base_url}/api/agents/{agent_id}/execute',
                headers=self._get_headers(),
                json=payload,
            ) as resp:
                resp.raise_for_status()

                async for line in resp.aiter_lines():
                    if not line.startswith('data:'):
                        continue

                    event = json.loads(line[5:])
                    event_type = event.get('type', '')

                    if event_type == 'agent_message':
                        msg_seq += 1
                        yield provider_message.MessageChunk(
                            role='assistant', content=event.get('content', ''), is_final=False
                        )

                    elif event_type == 'agent_thought':
                        msg_seq += 1
                        yield provider_message.MessageChunk(
                            role='assistant',
                            content=f'\U0001f4ad {event.get("content", "")}',
                            is_final=False,
                        )

                    elif event_type == 'agent_tool_call':
                        msg_seq += 1
                        yield provider_message.MessageChunk(
                            role='assistant',
                            content=None,
                            tool_calls=[
                                provider_message.ToolCall(
                                    id=event.get('id', ''),
                                    type='function',
                                    function=provider_message.FunctionCall(
                                        name=event.get('tool', ''),
                                        arguments=json.dumps(event.get('arguments', {})),
                                    ),
                                )
                            ],
                        )

                    elif event_type == 'agent_file':
                        msg_seq += 1
                        file_url = event.get('url', '')
                        file_type = event.get('file_type', '')
                        filename = event.get('filename', 'file')

                        if file_type == 'image' and file_url:
                            full_url = file_url if file_url.startswith('http') else f'{self.base_url}{file_url}'
                            yield provider_message.MessageChunk(
                                role='assistant',
                                content=[provider_message.ContentElement.from_image_url(full_url)],
                                is_final=False,
                            )
                        elif file_type == 'word' and file_url:
                            full_url = file_url if file_url.startswith('http') else f'{self.base_url}{file_url}'
                            if self._current_platform in ('wecom', 'wecombot'):
                                # 企业微信（标准+智能机器人）：发送文件标记，后续可由企微插件处理为文件附件
                                yield provider_message.MessageChunk(
                                    role='assistant',
                                    content=f'\U0001f4ce {filename}',
                                    is_final=False,
                                )
                                yield provider_message.MessageChunk(
                                    role='assistant',
                                    content=f'[file:{filename}|{full_url}]',
                                    is_final=False,
                                )
                            else:
                                # WeComCS / 其他平台：降级为下载链接
                                yield provider_message.MessageChunk(
                                    role='assistant',
                                    content=f'\U0001f4ce {filename}: {full_url}',
                                    is_final=False,
                                )
                        else:
                            full_url = file_url if file_url.startswith('http') else f'{self.base_url}{file_url}'
                            if file_url:
                                yield provider_message.MessageChunk(
                                    role='assistant',
                                    content=f'\U0001f4ce {filename}: {full_url}',
                                    is_final=False,
                                )
                            else:
                                yield provider_message.MessageChunk(
                                    role='assistant',
                                    content=f'\U0001f4ce {filename}',
                                    is_final=False,
                                )

                    elif event_type == 'agent_end':
                        if event.get('conversation_id') and not query.session.using_conversation.uuid:
                            query.session.using_conversation.uuid = event['conversation_id']
                        # is_final=True chunk serves as the end sentinel; content is empty
                        yield provider_message.MessageChunk(
                            role='assistant', content='', is_final=True
                        )

                    elif event_type == 'agent_error':
                        # Agent-level error: yield error message (visible to user) rather than raise
                        msg_seq += 1
                        yield provider_message.MessageChunk(
                            role='assistant',
                            content=f'❌ {event.get("message", "Unknown error")}',
                            is_final=True,
                        )

    def _format_result(self, result: dict) -> str:
        """Format a blocking-mode result dict into a text string."""
        if isinstance(result, dict):
            if 'result' in result:
                r = result['result']
                if isinstance(r, str):
                    return r
                return json.dumps(r, ensure_ascii=False, indent=2)
            return json.dumps(result, ensure_ascii=False, indent=2)
        return str(result)

    @staticmethod
    def _spo_get(spo: typing.Any, key: str, default: typing.Any = '') -> typing.Any:
        """Safe getter for source_platform_object (dict or Pydantic model)."""
        if isinstance(spo, dict):
            return spo.get(key, default)
        return getattr(spo, key, default)

    @staticmethod
    def _spo_has(spo: typing.Any, key: str) -> bool:
        """Check key existence in source_platform_object (dict or Pydantic model)."""
        if isinstance(spo, dict):
            return key in spo
        return hasattr(spo, key)

    def _extract_platform_vars(self, query: pipeline_query.Query) -> dict[str, typing.Any]:
        """Extract WeCom fields from source_platform_object for all three platforms.

        Platform detection by key signature:
          - wecombot: has 'aibotid' or 'chatid'
          - wecom: has 'MsgType' or 'AgentID'
          - wecomcs: has 'external_userid' or 'open_kfid'
        """
        if not query.message_event:
            return {}
        spo = getattr(query.message_event, 'source_platform_object', None)
        if spo is None:
            return {}

        if self._spo_has(spo, 'aibotid') or self._spo_has(spo, 'chatid'):
            return self._extract_wecombot_vars(spo)
        if self._spo_has(spo, 'MsgType') or self._spo_has(spo, 'AgentID'):
            return self._extract_wecom_standard_vars(spo)
        if self._spo_has(spo, 'external_userid') or self._spo_has(spo, 'open_kfid'):
            return self._extract_wecomcs_vars(spo)
        return {}

    @staticmethod
    def _filter_vars(vars_: dict[str, typing.Any]) -> dict[str, typing.Any]:
        """Remove empty values and non-serializable entries."""
        return {
            k: v for k, v in vars_.items()
            if v not in (None, '', [], {})
            and isinstance(v, (str, int, float, bool, dict, list))
        }

    def _extract_wecombot_vars(self, spo: typing.Any) -> dict[str, typing.Any]:
        """Extract wecombot (智能机器人) platform variables."""
        g = self._spo_get
        return self._filter_vars({
            'platform': 'wecombot',
            'msgtype': g(spo, 'msgtype', ''),
            'userid': g(spo, 'userid', ''),
            'sender_name': g(spo, 'username', ''),
            'chatid': g(spo, 'chatid', ''),
            'group_name': g(spo, 'chatname', ''),
            'message_id': g(spo, 'msgid', ''),
            'ai_bot_id': g(spo, 'aibotid', ''),
            'feedback_id': g(spo, 'feedback_id', ''),
            'stream_id': g(spo, 'stream_id', ''),
            'req_id': g(spo, 'req_id', ''),
            'picurl': g(spo, 'picurl', ''),
            'images': g(spo, 'images'),
            'file': g(spo, 'file'),
            'voice': g(spo, 'voice'),
            'video': g(spo, 'video'),
            'link': g(spo, 'link'),
            'location': g(spo, 'location'),
            'attachments': g(spo, 'attachments'),
            'quote': g(spo, 'quote'),
        })

    def _extract_wecom_standard_vars(self, spo: typing.Any) -> dict[str, typing.Any]:
        """Extract wecom (标准企业微信) platform variables."""
        g = self._spo_get
        return self._filter_vars({
            'platform': 'wecom',
            'msgtype': g(spo, 'MsgType', ''),
            'userid': g(spo, 'FromUserName', ''),
            'receiver_id': g(spo, 'ToUserName', ''),
            'message_id': g(spo, 'MsgId', ''),
            'agent_id': g(spo, 'AgentID', ''),
            'media_id': g(spo, 'MediaId', ''),
            'picurl': g(spo, 'PicUrl', ''),
            'timestamp': g(spo, 'CreateTime', ''),
            'event_key': g(spo, 'EventKey', ''),
        })

    def _extract_wecomcs_vars(self, spo: typing.Any) -> dict[str, typing.Any]:
        """Extract wecomcs (企业微信客服) platform variables."""
        g = self._spo_get
        return self._filter_vars({
            'platform': 'wecomcs',
            'msgtype': g(spo, 'msgtype', ''),
            'userid': g(spo, 'external_userid', ''),
            'receiver_id': g(spo, 'open_kfid', ''),
            'message_id': g(spo, 'msgid', ''),
            'picurl': g(spo, 'picurl', ''),
            'timestamp': g(spo, 'send_time', ''),
        })

    @staticmethod
    def _parse_inline_params(text: str) -> tuple[str, dict[str, str]]:
        """从消息文本中解析 key=value 结构化参数。

        支持的格式（每行一个参数）：
          key=value
          key="value with spaces"
          key='value with spaces'

        参数行从消息文本中移除，剩余文本作为 task 返回。
        不匹配参数格式的行原样保留。
        """
        import re

        param_pattern = re.compile(
            r'^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*'
            r'(?:"([^"]*)"|\'([^\']*)\'|(\S+))\s*$'
        )

        lines = text.split('\n')
        task_lines: list[str] = []
        params: dict[str, str] = {}

        for line in lines:
            m = param_pattern.match(line)
            if m:
                key = m.group(1)
                value = m.group(2) or m.group(3) or m.group(4)
                params[key] = value
            else:
                task_lines.append(line)

        clean_text = '\n'.join(task_lines).strip()
        return clean_text, params

    async def run(
        self, query: pipeline_query.Query
    ) -> typing.AsyncGenerator[provider_message.Message | provider_message.MessageChunk, None]:
        """Main entry point: determine agent, build payload, execute.

        The agent ID is resolved from (in priority order):
          1. query.variables['gsb_agent_id'] (set by a plugin)
          2. pipeline_config default-agent

        Streaming vs blocking is decided by the adapter's capability.
        """
        # Write WeCom vars to query.variables IMMEDIATELY at the start
        # This ensures LangBot monitoring can capture them in record_query_success
        wecom_vars = self._extract_platform_vars(query)
        self._current_platform = wecom_vars.get('platform', '')

        # Debug logging for WeCom variable extraction
        import logging as _logging
        _logger = _logging.getLogger('gsbagent')
        spo = getattr(query.message_event, 'source_platform_object', None) if query.message_event else None
        spo_info = 'None' if spo is None else (
            f'type={type(spo).__name__}, keys={list(spo.keys()) if isinstance(spo, dict) else dir(spo)[:10]}'
        )
        _logger.debug(
            f'[WeCom Debug] spo={spo_info}, '
            f'wecom_vars={wecom_vars}, '
            f'query.variables before={list(query.variables.keys())}'
        )
        
        for k, v in wecom_vars.items():
            var_key = f'wecom_{k}' if not k.startswith('wecom_') else k
            if var_key not in query.variables:
                query.variables[var_key] = v
        
        _logger.debug(
            f'[WeCom Debug] query.variables after={list(query.variables.keys())}'
        )

        # Resolve Agent ID: variable override > config default
        agent_id = query.variables.get('gsb_agent_id', '') or self.default_agent
        if not agent_id:
            raise ValueError(
                'GsbAgent default-agent not configured, and gsb_agent_id variable not set'
            )

        cov_id = (getattr(query.session.using_conversation, 'uuid', None) or None) if query.session.using_conversation else None
        plain_text, file_ids, file_contents = await self._extract_text_and_files(query)
        self.ap.logger.info(
            f'GsbAgent DEBUG: text_len={len(plain_text)}, file_ids={file_ids}, '
            f'file_contents_keys={list(file_contents.keys())}, '
            f'file_contents_sizes={{k: len(v) for k, v in file_contents.items()}}'
        )

        # 拦截会话命令（不转发给 GsbAgent，直接在 Runner 层处理）
        # 命令早于 stream 判断，但下游 chat handler 会根据 is_stream 设置 resp_message_id（仅 MessageChunk 有此字段）
        try:
            is_stream = await query.adapter.is_stream_output_supported()
        except AttributeError:
            is_stream = False

        def _cmd_reply(text: str):
            if is_stream:
                return provider_message.MessageChunk(role='assistant', content=text, is_final=True)
            return provider_message.Message(role='assistant', content=text)

        command = plain_text.strip().lower()
        if command in ('/new', '/clear'):
            old_conv_id = cov_id
            # 重置 LangBot 会话：清空消息+uuid，但保留 conversation 对象本身
            # 否则 chat.py:147 会因 using_conversation is None 而报错
            if query.session.using_conversation is not None:
                query.session.using_conversation.uuid = None
                query.session.using_conversation.messages = []
            # 通知 GsbAgent 清空旧 session（非关键，失败时 TTL 自动清理）
            if old_conv_id:
                try:
                    async with httpx.AsyncClient(timeout=10) as client:
                        await client.post(
                            f'{self.base_url}/api/admin/session/clear',
                            headers=self._get_headers(),
                            json={'conversation_id': old_conv_id},
                        )
                except Exception:
                    pass
            yield _cmd_reply('✨ 已开启新对话，历史记录已清空。')
            return
        if command in ('/help',):
            # 优先展示当前 Agent 的 usage_guide，无则展示通用帮助
            try:
                agent_info = await self._get_agent_info(agent_id)
                usage_guide = agent_info.get('usage_guide', '')
            except Exception:
                usage_guide = ''
            if usage_guide:
                help_text = usage_guide
            else:
                help_text = '可用命令：\n/new 或 /clear — 开启新对话\n/help — 显示帮助'
            yield _cmd_reply(help_text)
            return

        # 从消息中解析 key=value 结构化参数（禁止覆盖内部字段）
        task_text, inline_params_raw = self._parse_inline_params(plain_text)
        inline_params = {
            k: v for k, v in inline_params_raw.items()
            if k not in ('conversation_id', 'sender_id', 'launcher_type', 'launcher_id', 'extra')
        }

        # task_text 为空时回退到原文
        task_text = task_text or plain_text

        payload = {
            'task': task_text,
            'params': {
                'conversation_id': cov_id,
                'sender_id': str(query.sender_id),
                'launcher_type': query.session.launcher_type.value,
                'launcher_id': str(query.session.launcher_id),
                **inline_params,
            },
            'stream': True,
        }

        if file_ids:
            payload['files'] = [{'file_id': fid} for fid in file_ids]

        # 下载解密后的文件内容：base64 编码传给 Agent
        if file_contents:
            payload['params']['_files'] = [
                {'filename': fn, 'content_base64': base64.b64encode(data).decode('utf-8')}
                for fn, data in file_contents.items()
            ]

        # Build extra dict: WeCom vars + existing query.variables (excluding internal keys)
        extra_vars = dict(wecom_vars)
        for k, v in query.variables.items():
            if k not in ('_pipeline_bound_plugins', 'gsb_agent_id',
                         'conversation_id', 'session_id', 'msg_create_time'):
                extra_vars[k] = v
        if extra_vars:
            payload['params']['extra'] = extra_vars

        msg_seq = 0
        if is_stream:
            # 解析流式占位提示词: Agent 级 > 全局默认 > 硬编码回退
            stream_placeholder = '信息已收到，分析中...'
            try:
                agent_info = await self._get_agent_info(agent_id)
                stream_placeholder = agent_info.get('stream_placeholder', stream_placeholder)
            except Exception:
                pass
            # Send instant reply to WeCom immediately
            msg_seq += 1
            yield provider_message.MessageChunk(
                role='assistant',
                content=stream_placeholder,
                is_final=False,
            )
            
            async for msg in self._execute_streaming(query, agent_id, payload):
                msg_seq += 1
                msg.msg_sequence = msg_seq
                yield msg
        else:
            payload['stream'] = False
            async for msg in self._execute_blocking(query, agent_id, payload):
                msg.msg_sequence = 1
                yield msg
