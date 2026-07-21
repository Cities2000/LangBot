from __future__ import annotations

import asyncio

from ...core import app
from langbot_plugin.api.entities.builtin.provider import message as provider_message, prompt as provider_prompt
import langbot_plugin.api.entities.builtin.provider.session as provider_session
import langbot_plugin.api.entities.builtin.pipeline.query as pipeline_query


class SessionManager:
    """会话管理器"""

    ap: app.Application

    session_list: list[provider_session.Session]

    _session_index: dict[tuple[str, str, str, str], provider_session.Session]
    """索引字典，key = (bot_uuid, pipeline_uuid, launcher_type, launcher_id)"""

    _session_lock: asyncio.Lock
    """并发创建会话时的锁"""

    def __init__(self, ap: app.Application):
        self.ap = ap
        self.session_list = []
        self._session_index = {}
        self._session_lock = asyncio.Lock()

    async def initialize(self):
        pass

    @staticmethod
    def _get_session_key(query: pipeline_query.Query) -> tuple[str, str, str, str]:
        """根据查询生成会话的唯一键"""
        launcher_type = getattr(query.launcher_type, "value", str(query.launcher_type))
        return (
            str(getattr(query, "bot_uuid", "") or ""),
            str(getattr(query, "pipeline_uuid", "") or ""),
            str(launcher_type),
            str(query.launcher_id),
        )

    async def get_session(self, query: pipeline_query.Query) -> provider_session.Session:
        """获取会话"""
        key = self._get_session_key(query)

        # 快速路径：无需锁
        existing = self._session_index.get(key)
        if existing is not None:
            return existing

        # 慢路径：需要创建新会话（需锁保护）
        async with self._session_lock:
            # 双重检查
            existing = self._session_index.get(key)
            if existing is not None:
                return existing

            session_concurrency = self.ap.instance_config.data['concurrency']['session']

            session = provider_session.Session(
                launcher_type=query.launcher_type,
                launcher_id=query.launcher_id,
                sender_id=query.sender_id,
            )
            session._semaphore = asyncio.Semaphore(session_concurrency)
            self.session_list.append(session)
            self._session_index[key] = session
            return session

    async def get_conversation(
        self,
        query: pipeline_query.Query,
        session: provider_session.Session,
        prompt_config: list[dict],
        pipeline_uuid: str,
        bot_uuid: str,
    ) -> provider_session.Conversation:
        """获取对话或创建对话"""

        if not session.conversations:
            session.conversations = []

        # set prompt
        prompt_messages = []

        for prompt_message in prompt_config:
            prompt_messages.append(provider_message.Message(**prompt_message))

        prompt = provider_prompt.Prompt(
            name='default',
            messages=prompt_messages,
        )

        if session.using_conversation is None or session.using_conversation.pipeline_uuid != pipeline_uuid:
            conversation = provider_session.Conversation(
                prompt=prompt,
                messages=[],
                pipeline_uuid=pipeline_uuid,
                bot_uuid=bot_uuid,
            )
            session.conversations.append(conversation)
            session.using_conversation = conversation

        return session.using_conversation
