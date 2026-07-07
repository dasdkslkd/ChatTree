# chat/chat_manager.py - 适配延迟加载
from typing import Any, Callable, List, Optional, Dict, AsyncIterator
import uuid
import asyncio  
import json
import base64
from copy import deepcopy
from contextlib import suppress
from time import time
from .conversation import Conversation
from .node import NodeManager
from .compact import (
    COMPACT_MAX_OUTPUT_TOKENS,
    POST_COMPACT_MAX_CHARS_PER_FILE,
    extract_mentioned_import_filenames,
    format_restored_file_context,
    get_auto_compact_threshold,
    get_compact_prompt,
    microcompact_messages,
)
from ..config.types import Message, Role, StreamChunk, StreamStatus, StreamController, GenerationInfo
from ..storage.chat_storage import ChatStorage
from ..storage.prompt_storage import PromptStorage
from ..model.model_manager import ModelManager
from ..model.usage import add_usage, estimated_usage, usage_total
from ..utils.logger import setup_logger
from ..config.config import cfg
from ..workspace import build_default_workspace, normalize_workspace
from ..capabilities.prompting import (
    collect_skill_injection_names,
)
from ..prompts import PromptBuilder, PromptBuildRequest
from ..prompts.types import RuntimePromptContext
from ..slash import (
    SlashCommandDispatcher,
    SlashDispatchKind,
    SlashDispatchResult,
    SlashToolPolicy,
)
from ..tools.security.permissions import PermissionMode, normalize_permission_mode
from ..tools.agent_tools import AGENT_TOOL_NAMES, LEGACY_AGENT_TOOL_NAMES
from ..tasks import TaskRecord, TaskStatus
from ..notifications.task_notifications import parse_task_notification_content
from .turn_timeline import (
    has_blocking_plan_tool_result,
    should_emit_as_intermediate_text,
)

logger = setup_logger('ChatManager')

MULTI_AGENT_REJECTION_TOKENS = (
    "不要用 subagent",
    "不要使用subagent",
    "不要使用 subagent",
    "do not use subagent",
    "without subagent",
)

MULTI_AGENT_REQUEST_TOKENS = (
    "subagent",
    "子agent",
    "子代理",
    "开agent",
    "开 agent",
    "派agent",
    "派 agent",
    "使用agent",
    "使用 agent",
    "delegate",
    "parallel agent",
    "fork agent",
    "workflow",
    "工作流",
)

PARALLEL_READ_ONLY_TOOL_NAMES = {
    "fetch_url",
    "list_available_tools",
    "list_files",
    "read_file",
    "read_tool_result",
    "search_files",
    "web_search",
}


def _tool_call_function_name(tool_call: Dict[str, Any]) -> str:
    fn = tool_call.get("function") or {}
    return str(fn.get("name") or "")


def _is_parallel_read_only_tool(name: str) -> bool:
    if name in PARALLEL_READ_ONLY_TOOL_NAMES:
        return True
    if name.startswith("mcp__"):
        lowered = name.lower()
        return lowered.endswith("__read_file") or lowered.endswith("__list_files") or lowered.endswith("__search_files")
    return False


class ChatManager:
    """延迟加载模型的聊天管理器"""
    
    def __init__(
        self,
        model_manager: ModelManager,
        storage: ChatStorage,
        prompts: PromptStorage,
        tool_manager=None,
        task_ledger=None,
        plan_ledger=None,
        chat_repository=None,
        transcript_projection=None,
    ):
        self.model_manager = model_manager
        self.storage = storage
        self.prompts = prompts
        self.tool_manager = tool_manager
        self.task_ledger = task_ledger
        self.plan_ledger = plan_ledger
        self.chat_repository = chat_repository
        self.transcript_projection = transcript_projection
        if chat_repository is not None:
            store = getattr(tool_manager, "tool_result_store", None)
            if store is not None and getattr(store, "sqlite_repository", None) is None:
                store.sqlite_repository = chat_repository
        self.tool_orchestrator = None
        self.capability_registry = None
        self.slash_dispatcher = SlashCommandDispatcher()
        self.current_conversation: Optional[Conversation] = None
        self._active_controllers: Dict[str, StreamController] = {}  # node_id -> controller
        # 每对话异步锁，串行化同一对话的 load-modify-save 临界区。
        # 注意：仅在单 uvicorn 进程下有效（main.py reload=True 默认单 worker）。
        # 若将来以 --workers N 多进程部署，进程内锁无法跨进程互斥，需改用
        # 文件锁或单写队列——届时再处理，本次范围外。
        self._conv_locks: Dict[str, asyncio.Lock] = {}

    def _lock_for(self, conversation_id: str) -> asyncio.Lock:
        """获取（或创建）指定对话的异步锁。"""
        lock = self._conv_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._conv_locks[conversation_id] = lock
        return lock

    def get_conversation(self, conversation_id: str) -> Optional[Conversation]:
        """加载一个独立的 Conversation（不触碰共享 current_conversation）。

        供非流式读路由使用，避免并发请求互相覆盖单例字段。
        """
        data = self.storage.load(conversation_id)
        if not data:
            return None
        return Conversation.from_dict(data)

    async def create_visible_user_anchor_node(
        self,
        *,
        conversation_id: str,
        content: str,
        parent_node_id: Optional[str] = None,
        model_id: Optional[str] = None,
        tool_permission_mode: Optional[str] = None,
        slash_metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Create a visible user-only node for detached slash runs."""
        async with self._lock_for(conversation_id):
            conversation = self.get_conversation(conversation_id)
            if conversation is None:
                raise ValueError("对话不存在")
            if parent_node_id and parent_node_id in conversation.nodes:
                conversation.switch_to_node(parent_node_id)
            current_node_id = conversation.current_node_id
            parent_tool_permission_mode = None
            if current_node_id and current_node_id in conversation.nodes:
                parent_tool_permission_mode = conversation.nodes[current_node_id].get("tool_permission_mode")
            eff_tool_permission_mode = normalize_permission_mode(
                tool_permission_mode
                if tool_permission_mode not in (None, "")
                else parent_tool_permission_mode or "ask_always"
            )
            user_msg = Message({
                "id": str(uuid.uuid4()),
                "role": Role.USER,
                "content": content,
                "name": None,
                "tool_calls": None,
                "tool_call_id": None,
                "timestamp": int(time()),
            })
            if slash_metadata:
                user_msg["slash_command"] = dict(slash_metadata)
            new_node = NodeManager.create_node(
                user_message=user_msg,
                parent_id=current_node_id,
                model_id=model_id or conversation.current_model,
                tool_permission_mode=eff_tool_permission_mode,
            )
            conversation.add_node(new_node, parent_id=current_node_id)
            self._save(conversation)
            return str(new_node["id"])
    
    def create_conversation(
        self,
        title: str = '',
        prompt_id: Optional[str] = None,
        prompt_mode: str = "override",
        workspace: Optional[Dict[str, Any]] = None,
        multi_agent_mode: Optional[str] = None,
    ) -> Conversation:
        """
        创建新对话（不实例化模型，只保存配置ID）
        """
        # 创建对话，只保存model_id字符串引用
        workspace_context = normalize_workspace(
            workspace,
            build_default_workspace(cfg.data if isinstance(cfg.data, dict) else None),
        )
        conversation = Conversation(title=title, workspace=workspace_context)
        conversation.metadata["multi_agent_mode"] = self._normalize_multi_agent_mode(multi_agent_mode)
        
        # 初始化系统消息
        conversation.initialize_with_system_message(None)
        if prompt_id:
            system_prompt = self.prompts.load(prompt_id)
            if system_prompt:
                conversation.metadata["selected_system_prompt"] = {
                    "id": prompt_id,
                    "mode": self._normalize_selected_system_prompt_mode(prompt_mode),
                    "content": system_prompt,
                }

        # 直接持久化新对话（不依赖共享 current_conversation 做后续保存）
        self._save(conversation)
        self.current_conversation = conversation
        logger.info(f"对话创建成功 id: {conversation.metadata['id']}")
        return conversation
    
    def load_conversation(self, conversation_id: str) -> bool:
        """加载对话"""
        data = self.storage.load(conversation_id)
        if data:
            self.current_conversation = Conversation.from_dict(data)
            return True
        return False
    
    def save_conversation(self):
        """保存当前对话"""
        if self.current_conversation:
            self.storage.save(self.current_conversation.to_dict())
    
    def list_conversations(self) -> List[Dict[str, Any]]:
        """列出所有对话"""
        default_workspace = build_default_workspace(cfg.data if isinstance(cfg.data, dict) else None)
        conversations = self.storage.list()
        for item in conversations:
            item["workspace"] = normalize_workspace(item.get("workspace"), default_workspace)
            if not item.get("model_id") or not item.get("provider_id"):
                loaded = self.get_conversation(item["id"])
                if loaded is not None:
                    model_id, provider_id = self._model_summary_for_conversation(loaded)
                    item["model_id"] = item.get("model_id") or model_id or ""
                    item["provider_id"] = item.get("provider_id") or provider_id or ""
        return conversations
    
    def delete_conversation(self, conversation_id: str):
        """删除对话"""
        self.storage.delete(conversation_id)
        if self.chat_repository is not None:
            with suppress(Exception):
                self.chat_repository.delete_conversation(conversation_id)
        if self.current_conversation and self.current_conversation.metadata["id"] == conversation_id:
            self.current_conversation = None
    
    async def update_conversation_title(self, conversation_id: str, title: str) -> bool:
        """更新对话标题（锁内 load-modify-save）"""
        async with self._lock_for(conversation_id):
            data = self.storage.load(conversation_id)
            if not data:
                return False
            data["metadata"]["title"] = title
            data["metadata"]["updated_at"] = int(time())
            self.storage.save(data)
        return True

    async def update_conversation_model(
        self,
        conversation_id: str,
        model_id: str,
        provider_id: str,
        reasoning_effort: Optional[str] = None,
        thinking_enabled: Optional[bool] = None,
    ) -> bool:
        """更新对话的默认模型及推理设置（锁内 load-modify-save）。

        reasoning_effort / thinking_enabled 显式传入时写入（None 也会写入，
        表示"清除/不发送"），与 model_id 一起按对话持久化。
        """
        async with self._lock_for(conversation_id):
            data = self.storage.load(conversation_id)
            if not data:
                return False
            data["metadata"]["model_id"] = model_id
            data["metadata"]["provider_id"] = provider_id
            data["metadata"]["reasoning_effort"] = reasoning_effort
            data["metadata"]["thinking_enabled"] = thinking_enabled
            data["metadata"]["updated_at"] = int(time())
            self.storage.save(data)
        return True

    async def update_conversation_multi_agent_mode(
        self,
        conversation_id: str,
        multi_agent_mode: str,
    ) -> bool:
        """更新对话的 multi-agent 工具暴露策略（锁内 load-modify-save）。"""
        mode = self._normalize_multi_agent_mode(multi_agent_mode)
        async with self._lock_for(conversation_id):
            data = self.storage.load(conversation_id)
            if not data:
                return False
            data["metadata"]["multi_agent_mode"] = mode
            data["metadata"]["updated_at"] = int(time())
            self.storage.save(data)
        return True

    async def switch_node(self, conversation_id: str, node_id: str) -> Optional[str]:
        """切换对话当前节点（锁内 load-modify-save）；成功返回新的 current_node_id，失败返回 None。"""
        async with self._lock_for(conversation_id):
            conversation = self.get_conversation(conversation_id)
            if not conversation:
                return None
            if not conversation.switch_to_node(node_id):
                return None
            self._save(conversation)
            return conversation.current_node_id

    async def delete_node(self, conversation_id: str, node_id: str) -> Optional[Dict[str, Optional[str]]]:
        """删除节点及其子树（锁内 load-modify-save）。对话不存在返回 None。"""
        async with self._lock_for(conversation_id):
            conversation = self.get_conversation(conversation_id)
            if not conversation:
                return None
            node = conversation.nodes.get(node_id)
            parent_id = node.get("parent_id") if node else None
            conversation.del_node(node_id)
            self._save(conversation)
            if self.chat_repository is not None:
                with suppress(Exception):
                    self.chat_repository.delete_node(
                        conversation_id,
                        node_id,
                        new_current_node_id=conversation.current_node_id,
                    )
            return {
                "deleted_node_id": node_id,
                "new_current_node_id": conversation.current_node_id,
                "parent_node_id": parent_id,
            }

    def _save(self, conversation: Conversation):
        """保存一个 Conversation 并清空其待删集合。"""
        self.storage.save(conversation.to_dict())
        self._sqlite_ensure_conversation(conversation)
        if conversation.current_node_id:
            try:
                self._sqlite_ensure_branch(
                    conversation,
                    conversation.current_node_id,
                    provider_id=conversation.metadata.get("provider_id"),
                    model_id=conversation.metadata.get("model_id"),
                    focus_node_id=conversation.current_node_id,
                )
            except Exception as exc:
                logger.warning("SQLite branch ensure failed: %s", exc, exc_info=True)
        conversation._deleted_node_ids.clear()

    def _mark_conversation_updated_at(self, conversation: Conversation, updated_at: int):
        conversation.metadata["updated_at"] = max(
            int(conversation.metadata.get("updated_at") or 0),
            updated_at,
        )

    def _sqlite_enabled(self) -> bool:
        return self.chat_repository is not None and self.transcript_projection is not None

    def _sqlite_ensure_conversation(self, conversation: Conversation) -> None:
        if self.chat_repository is None:
            return
        metadata = conversation.metadata
        try:
            self.chat_repository.ensure_conversation(
                metadata["id"],
                title=str(metadata.get("title") or ""),
                provider_id=metadata.get("provider_id"),
                model_id=metadata.get("model_id"),
                workspace=metadata.get("workspace") if isinstance(metadata.get("workspace"), dict) else None,
            )
        except Exception as exc:
            logger.warning("SQLite conversation ensure failed: %s", exc, exc_info=True)

    def _role_value(self, role: Any) -> str:
        return str(getattr(role, "value", role))

    def _sqlite_ensure_branch(
        self,
        conversation: Conversation,
        node_id: str,
        *,
        provider_id: Optional[str],
        model_id: Optional[str],
        focus_node_id: Optional[str] = None,
    ) -> None:
        if not self._sqlite_enabled():
            return
        repo = self.chat_repository
        metadata = conversation.metadata
        repo.ensure_conversation(
            metadata["id"],
            title=str(metadata.get("title") or ""),
            provider_id=provider_id or metadata.get("provider_id"),
            model_id=model_id or metadata.get("model_id"),
            workspace=metadata.get("workspace") if isinstance(metadata.get("workspace"), dict) else None,
        )
        chain = conversation.get_node_chain(node_id)
        for item in chain:
            parent_id = item.get("parent_id")
            if parent_id == "None":
                parent_id = None
            child_order = 0
            if parent_id and parent_id in conversation.nodes:
                siblings = conversation.nodes[parent_id].get("children_ids") or []
                with suppress(ValueError):
                    child_order = siblings.index(item["id"])
            repo.ensure_node(
                metadata["id"],
                item["id"],
                parent_id,
                child_order=child_order,
                model_id=item.get("model_id") or model_id,
                provider_id=provider_id,
                tool_permission_mode=item.get("tool_permission_mode"),
                focus=item["id"] == focus_node_id,
            )

    def _persist_sqlite_user_turn(
        self,
        *,
        conversation: Conversation,
        node: Dict[str, Any],
        user_msg: Message,
        provider_id: Optional[str],
        model_id: Optional[str],
        run_id: Optional[str],
    ) -> None:
        if not self._sqlite_enabled():
            return
        conversation_id = conversation.metadata["id"]
        node_id = node["id"]
        try:
            self._sqlite_ensure_branch(
                conversation,
                node_id,
                provider_id=provider_id,
                model_id=model_id,
                focus_node_id=conversation.current_node_id,
            )
            message_id = self.chat_repository.add_message(
                conversation_id,
                node_id,
                role=self._role_value(user_msg.get("role") or Role.USER),
                content=str(user_msg.get("content") or ""),
                subtype=user_msg.get("subtype"),
                hidden=bool(user_msg.get("is_hidden_from_transcript")),
                metadata={
                    key: value
                    for key, value in dict(user_msg).items()
                    if key not in {"id", "role", "content", "subtype"}
                },
                message_id=user_msg.get("id"),
            )
            if not user_msg.get("is_hidden_from_transcript") and user_msg.get("subtype") == "task_notification":
                payload = parse_task_notification_content(user_msg.get("content"))
                props = {
                    key: value
                    for key, value in payload.items()
                    if key not in {"content"}
                }
                props["content"] = payload.get("content") or str(user_msg.get("content") or "")
                self.transcript_projection.upsert_message_item(
                    conversation_id,
                    node_id,
                    message_id,
                    "task_notification",
                    local_order=10,
                    status=str(payload.get("source_status") or ""),
                    summary=str(payload.get("summary") or "Task notification"),
                    preview=str(payload.get("summary") or payload.get("content") or "Task notification"),
                    props=props,
                )
            elif not user_msg.get("is_hidden_from_transcript"):
                self.transcript_projection.upsert_message_item(
                    conversation_id,
                    node_id,
                    message_id,
                    "user_message",
                    local_order=10,
                )
            if run_id:
                self.transcript_projection.upsert_run_draft(
                    conversation_id,
                    node_id,
                    run_id=run_id,
                    status="running",
                    preview="",
                    local_order=20,
                )
        except Exception as exc:
            logger.warning("SQLite transcript user turn write failed: %s", exc, exc_info=True)

    def _persist_sqlite_control_event_turn(
        self,
        *,
        conversation: Conversation,
        node: Dict[str, Any],
        control_event: Dict[str, Any],
        provider_id: Optional[str],
        model_id: Optional[str],
        run_id: Optional[str],
    ) -> None:
        if not self._sqlite_enabled():
            return
        conversation_id = conversation.metadata["id"]
        node_id = node["id"]
        try:
            self._sqlite_ensure_branch(
                conversation,
                node_id,
                provider_id=provider_id,
                model_id=model_id,
                focus_node_id=conversation.current_node_id,
            )
            self.transcript_projection.upsert_control_event(
                conversation_id,
                node_id,
                event_type=str(control_event.get("event_type") or "control_event"),
                plan_id=control_event.get("plan_id"),
                run_id=run_id,
                status=control_event.get("status"),
                preview=str(control_event.get("preview") or "")[:4096],
                local_order=15,
                visibility="hidden",
                anchor_node_id=node.get("parent_id"),
                props={
                    key: value
                    for key, value in dict(control_event).items()
                    if key not in {"preview"}
                },
            )
            if run_id:
                self.transcript_projection.upsert_run_draft(
                    conversation_id,
                    node_id,
                    run_id=run_id,
                    status="running",
                    preview="",
                    local_order=20,
                    anchor_node_id=node.get("parent_id"),
                    props={
                        "after_control_event": True,
                        "plan_id": control_event.get("plan_id"),
                    },
                )
        except Exception as exc:
            logger.warning("SQLite transcript control event write failed: %s", exc, exc_info=True)

    def _assistant_process_preview(
        self,
        *,
        tool_interactions: List[Dict[str, Any]],
        reasoning: str,
    ) -> str:
        parts: list[str] = []
        for interaction in tool_interactions:
            assistant = interaction.get("assistant") or {}
            for call in assistant.get("tool_calls") or []:
                fn = call.get("function") or {}
                name = fn.get("name")
                if name:
                    parts.append(f"tool: {name}")
            for tool_message in interaction.get("tools") or []:
                name = tool_message.get("name")
                if name and f"tool: {name}" not in parts:
                    parts.append(f"tool: {name}")
        if reasoning:
            parts.append(reasoning[:300])
        return "\n".join(parts)[:4096] or "Assistant process"

    def _persist_sqlite_tool_metadata(
        self,
        *,
        conversation_id: str,
        node_id: str,
        run_id: Optional[str],
        assistant_message_id: Optional[str],
        tool_calls: List[Dict[str, Any]],
        tool_messages: List[Message],
    ) -> None:
        if self.chat_repository is None:
            return
        for index, call in enumerate(tool_calls):
            fn = call.get("function") or {}
            self.chat_repository.add_tool_call(
                conversation_id,
                node_id,
                tool_call_id=call.get("id"),
                name=str(fn.get("name") or ""),
                arguments=fn.get("arguments"),
                call_index=index,
                status="complete",
                run_id=run_id,
                assistant_message_id=assistant_message_id,
            )
        for message in tool_messages:
            raw_output = str(message.get("raw_content") or message.get("content") or "")
            tool_call_id = message.get("tool_call_id")
            if tool_call_id and not self.chat_repository.tool_call_exists(
                conversation_id,
                str(tool_call_id),
            ):
                self.chat_repository.add_tool_call(
                    conversation_id,
                    node_id,
                    tool_call_id=tool_call_id,
                    name=str(message.get("name") or ""),
                    arguments=None,
                    call_index=0,
                    status="complete",
                    run_id=run_id,
                    assistant_message_id=assistant_message_id,
                )
            self.chat_repository.add_tool_result(
                conversation_id,
                node_id,
                tool_result_id=message.get("tool_result_id"),
                tool_call_id=tool_call_id,
                output=raw_output,
                status="complete",
                run_id=run_id,
                metadata={
                    "tool_name": message.get("name"),
                    "tool_result_id": message.get("tool_result_id"),
                    "model_visible_content": message.get("model_visible_content"),
                },
            )

    def _persist_sqlite_assistant_turn(
        self,
        *,
        conversation: Conversation,
        node: Dict[str, Any],
        assistant_msg: Message,
        provider_id: Optional[str],
        model_id: Optional[str],
        run_id: Optional[str],
        generation_status: str,
        tool_interactions: List[Dict[str, Any]],
        tool_messages: List[Message],
        tool_calls: List[Dict[str, Any]],
        transcript_continuation: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self._sqlite_enabled():
            return
        conversation_id = conversation.metadata["id"]
        node_id = node["id"]
        try:
            self._sqlite_ensure_branch(
                conversation,
                node_id,
                provider_id=provider_id,
                model_id=model_id,
                focus_node_id=conversation.current_node_id,
            )
            content = str(assistant_msg.get("content") or "")
            assistant_message_id: Optional[str] = None
            if content:
                assistant_message_id = self.chat_repository.add_message(
                    conversation_id,
                    node_id,
                    role=self._role_value(assistant_msg.get("role") or Role.ASSISTANT),
                    content=content,
                    metadata={
                        key: value
                        for key, value in dict(assistant_msg).items()
                        if key not in {"id", "role", "content"}
                    },
                    message_id=assistant_msg.get("id"),
                )
            if tool_calls or tool_messages:
                self._persist_sqlite_tool_metadata(
                    conversation_id=conversation_id,
                    node_id=node_id,
                    run_id=run_id,
                    assistant_message_id=assistant_message_id,
                    tool_calls=tool_calls,
                    tool_messages=tool_messages,
                )
            process_preview = self._assistant_process_preview(
                tool_interactions=tool_interactions,
                reasoning=str(assistant_msg.get("reasoning") or ""),
            )
            generation_info = assistant_msg.get("generation_info") or {}
            duration_ms = generation_info.get("duration_ms")
            process_props: dict[str, Any] = {}
            if isinstance(duration_ms, (int, float)) and duration_ms >= 0:
                process_props["duration"] = int(duration_ms)
            timeline_blocks: list[dict[str, Any]] = []
            for interaction in tool_interactions:
                assistant = interaction.get("assistant") or {}
                if assistant.get("reasoning"):
                    timeline_blocks.append({"type": "reasoning", "content": assistant["reasoning"]})
                elif interaction.get("reasoning"):
                    timeline_blocks.append({"type": "reasoning", "content": interaction["reasoning"]})
                if assistant.get("content"):
                    timeline_blocks.append({"type": "content", "content": assistant["content"]})

                calls = assistant.get("tool_calls") or interaction.get("tool_calls") or []
                results = interaction.get("tools") or interaction.get("tool_results") or []
                results_by_call_id = {
                    str(result.get("tool_call_id") or ""): result
                    for result in results
                }
                for call in calls:
                    result = results_by_call_id.get(str(call.get("id") or ""))
                    timeline_blocks.append({
                        "type": "tool_call",
                        "tool_call": call,
                        "tool_result": result,
                    })
            continuation_meta = dict(transcript_continuation or {})
            is_transcript_continuation = bool(continuation_meta)
            continuation_appended = False
            if tool_interactions or assistant_msg.get("reasoning"):
                process_message_id = self.chat_repository.add_message(
                    conversation_id,
                    node_id,
                    role="assistant",
                    content=process_preview,
                    subtype="assistant_process",
                    hidden=is_transcript_continuation,
                    metadata={
                        "tool_interactions": tool_interactions,
                        "timeline": timeline_blocks,
                        "reasoning": assistant_msg.get("reasoning"),
                        "duration": process_props.get("duration"),
                        "transcript_continuation": continuation_meta or None,
                    },
                    message_id=f"{assistant_msg.get('id')}:process",
                )
                if is_transcript_continuation:
                    base_node_id = str(
                        continuation_meta.get("continuation_of_node_id")
                        or node.get("parent_id")
                        or ""
                    )
                    continuation_props = {
                        **continuation_meta,
                        "continuation_of_node_id": base_node_id,
                        "timeline": timeline_blocks,
                        "reasoning": assistant_msg.get("reasoning"),
                        **process_props,
                    }
                    appended_to = self.transcript_projection.append_process_continuation(
                        conversation_id,
                        base_node_id,
                        message_id=process_message_id,
                        run_id=run_id,
                        status=generation_status,
                        preview=process_preview,
                        marker=str(continuation_meta.get("marker") or ""),
                        props=continuation_props,
                    )
                    continuation_appended = appended_to is not None
                    if appended_to is None:
                        self.transcript_projection.upsert_message_item(
                            conversation_id,
                            node_id,
                            process_message_id,
                            "assistant_process",
                            local_order=25,
                            status=generation_status,
                            preview=process_preview,
                            anchor_node_id=base_node_id or None,
                            props=continuation_props,
                        )
                else:
                    self.transcript_projection.upsert_message_item(
                        conversation_id,
                        node_id,
                        process_message_id,
                        "assistant_process",
                        local_order=25,
                        status=generation_status,
                        preview=process_preview,
                        props=process_props or None,
                    )
            if is_transcript_continuation and not continuation_appended:
                base_node_id = str(
                    continuation_meta.get("continuation_of_node_id")
                    or node.get("parent_id")
                    or ""
                )
                marker_message_id = assistant_message_id or f"{assistant_msg.get('id')}:continuation"
                appended_to = self.transcript_projection.append_process_continuation(
                    conversation_id,
                    base_node_id,
                    message_id=marker_message_id,
                    run_id=run_id,
                    status=generation_status,
                    preview="",
                    marker=str(continuation_meta.get("marker") or ""),
                    props={
                        **continuation_meta,
                        "continuation_of_node_id": base_node_id,
                        "timeline": [],
                        "reasoning": None,
                        **process_props,
                    },
                )
                continuation_appended = appended_to is not None
            plan_control_only = has_blocking_plan_tool_result(tool_messages)
            if content and not plan_control_only and assistant_message_id:
                self.transcript_projection.upsert_message_item(
                    conversation_id,
                    node_id,
                    assistant_message_id,
                    "assistant_answer",
                    local_order=30,
                    status=generation_status,
                )
            if run_id:
                self.transcript_projection.upsert_run_draft(
                    conversation_id,
                    node_id,
                    run_id=run_id,
                    status=generation_status,
                    preview=content[:4096],
                    local_order=20,
                    visibility="hidden" if is_transcript_continuation else "main",
                    anchor_node_id=(
                        str(continuation_meta.get("continuation_of_node_id") or "")
                        or None
                    ) if is_transcript_continuation else None,
                    props=continuation_meta or None,
                )
        except Exception as exc:
            logger.warning("SQLite transcript assistant turn write failed: %s", exc, exc_info=True)

    def _provider_for_model(self, model_id: Optional[str]) -> Optional[str]:
        if not model_id:
            return None
        matches = [
            provider_id
            for provider_id, models in self.model_manager.model_list.items()
            if model_id in models
        ]
        # 旧数据只有 node.model_id、没有 provider_id 时，同名模型可能属于多个
        # provider，无法精确恢复原供应商。此时宁可返回空让前端不显示 provider，
        # 也不要猜第一个匹配项造成模型切换卡误报。
        return matches[0] if len(matches) == 1 else None

    def _current_branch_model(self, conversation: Conversation) -> Optional[str]:
        current = conversation.nodes.get(conversation.current_node_id or "")
        if current and current.get("model_id"):
            return current.get("model_id")
        for node in reversed(conversation.get_node_chain(conversation.current_node_id)):
            if node.get("model_id"):
                return node.get("model_id")
        return None

    def _recent_active_skill_names(
        self,
        conversation: Conversation,
        *,
        max_nodes: int = 4,
    ) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        checked = 0
        for node in reversed(conversation.get_node_chain(conversation.current_node_id)):
            checked += 1
            for name in node.get("active_skill_names") or []:
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
            if checked >= max_nodes:
                break
        return names

    def _model_summary_for_conversation(
        self,
        conversation: Conversation,
    ) -> tuple[Optional[str], Optional[str]]:
        model_id = conversation.metadata.get("model_id") or self._current_branch_model(conversation)
        provider_id = (
            conversation.metadata.get("provider_id")
            or conversation.current_provider
            or self._provider_for_model(model_id)
        )
        return model_id, provider_id

    def _set_conversation_model_metadata(
        self,
        conversation: Conversation,
        *,
        provider_id: str,
        model_id: str,
    ) -> None:
        conversation.current_provider = provider_id
        conversation.current_model = model_id
        conversation.metadata["provider_id"] = provider_id
        conversation.metadata["model_id"] = model_id

    def _dispatch_slash_content(self, content: str) -> SlashDispatchResult:
        return self.slash_dispatcher.dispatch(content)

    def _slash_command_metadata(
        self,
        slash_result: SlashDispatchResult,
    ) -> Dict[str, Any]:
        command_name = slash_result.canonical_name or slash_result.command_name or ""
        return {
            "command": command_name,
            "kind": slash_result.kind.value,
            "args": slash_result.args,
            "original_input": slash_result.original_input,
            "tool_policy": slash_result.tool_policy.value,
            "persistence_policy": slash_result.persistence_policy.value,
            "run_kind": slash_result.run_kind,
        }

    def _slash_runtime_error(self, slash_result: SlashDispatchResult) -> str:
        if slash_result.error:
            return slash_result.error
        command_name = slash_result.canonical_name or slash_result.command_name or "unknown"
        if slash_result.kind == SlashDispatchKind.SUBAGENT:
            return f"Slash command '/{command_name}' 是后台 subagent 命令，必须由消息 SSE 入口分派。"
        if slash_result.kind == SlashDispatchKind.WORKFLOW:
            return f"Slash command '/{command_name}' 是后台 workflow 命令，必须由消息 SSE 入口分派。"
        return f"Slash command '/{command_name}' 暂不可用。"

    def _build_prompt_messages(
        self,
        conversation: Conversation,
        skill_names: List[str],
    ) -> List[Message]:
        base_messages = self._prepare_messages_for_api_with_conversation(conversation)
        custom_prompt, custom_mode = self._selected_system_prompt(conversation)
        latest_user_content = self._latest_user_content(conversation)
        built_messages = PromptBuilder(self.capability_registry).build(
            PromptBuildRequest(
                base_messages=base_messages,
                active_skill_names=skill_names,
                runtime_context=self._runtime_prompt_context(
                    "main",
                    conversation,
                    latest_user_content=latest_user_content,
                ),
                custom_system_prompt=custom_prompt,
                custom_system_prompt_mode=custom_mode,
            )
        )
        return [Message(message) for message in built_messages]

    async def _send_side_question_stream(
        self,
        *,
        conversation: Conversation,
        content: str,
        provider: Any,
        target_model: str,
        eff_effort: Optional[str],
        eff_thinking: Optional[bool],
        run_id: Optional[str],
    ) -> AsyncIterator[StreamChunk]:
        base_messages = self._prepare_messages_for_api_with_conversation(conversation)
        custom_prompt, custom_mode = self._selected_system_prompt(conversation)
        messages = [
            Message(message)
            for message in PromptBuilder(self.capability_registry).build(
                PromptBuildRequest(
                    base_messages=base_messages,
                    active_skill_names=[],
                    include_available_capabilities=False,
                    runtime_context=self._runtime_prompt_context("side_question", conversation),
                    custom_system_prompt=custom_prompt,
                    custom_system_prompt_mode=custom_mode,
                )
            )
        ]
        messages.append(Message({"role": Role.USER, "content": content}))

        controller = StreamController(
            node_id=f"side_{run_id or uuid.uuid4().hex}",
            conversation_id=conversation.metadata["id"],
            run_id=run_id,
        )
        controller_key = run_id or controller.node_id
        self._active_controllers[controller_key] = controller
        try:
            yield StreamChunk(
                status=StreamStatus.START,
                content=None,
                node_id=None,
                target_node_id=None,
                conversation_id=conversation.metadata["id"],
                run_id=run_id,
                tokens_used=0,
            )

            tokens_used = 0
            async for chunk in provider.generate_response_stream(
                model=target_model,
                messages=messages,
                stream_controller=controller,
                tools=None,
                tool_choice=None,
                reasoning_effort=eff_effort,
                thinking_enabled=eff_thinking,
            ):  # type: ignore
                if await controller.is_stopped():
                    yield StreamChunk(
                        status=StreamStatus.STOPPED,
                        content="",
                        node_id=None,
                        target_node_id=None,
                        conversation_id=conversation.metadata["id"],
                        run_id=run_id,
                        error=None,
                        tokens_used=tokens_used,
                    )
                    break
                chunk["node_id"] = None
                chunk["target_node_id"] = None
                chunk["conversation_id"] = conversation.metadata["id"]
                if run_id:
                    chunk["run_id"] = run_id
                if chunk.get("tokens_used"):
                    tokens_used = chunk.get("tokens_used", tokens_used) or tokens_used
                if chunk.get("status") == StreamStatus.COMPLETE and not chunk.get("tokens_used"):
                    chunk["tokens_used"] = tokens_used
                yield chunk
        finally:
            self._active_controllers.pop(controller_key, None)

    async def send_message_stream(
        self,
        conversation_id: str,
        content: str,
        model_id: Optional[str] = None,
        provider_id: Optional[str] = None,
        parent_node_id: Optional[str] = None,
        focus_new_node: bool = True,
        reasoning_effort: Optional[str] = None,
        thinking_enabled: Optional[bool] = None,
        import_files: Optional[List[Dict[str, Any]]] = None,
        image_refs: Optional[List[Dict[str, Any]]] = None,
        tool_permission_mode: Optional[str] = None,
        message_subtype: Optional[str] = None,
        hidden_user_message: bool = False,
        run_id: Optional[str] = None,
        control_event: Optional[Dict[str, Any]] = None,
        continuation_messages: Optional[List[Message]] = None,
        suppress_user_message: bool = False,
        transcript_continuation: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[StreamChunk]:
        """
        异步流式发送消息
        前端可以：for chunk in stream: 实时更新UI
        """
        # 预加载（只读）用于解析模型/提供商，不做任何修改或保存。
        # 真正的树修改在锁内重新加载最新快照，避免并发覆盖 root.children_ids。
        conversation_data = self.storage.load(conversation_id)
        if not conversation_data:
            logger.error(f"对话 {conversation_id} 不存在")
            yield StreamChunk(
                status=StreamStatus.ERROR,
                node_id=None,
                conversation_id=conversation_id,
                run_id=run_id,
                content="",
                error="对话不存在",
                tokens_used=0
            )
            return

        preview = Conversation.from_dict(conversation_data)
        # 确定模型：请求值 > 会话 metadata > 当前分支节点 > 第一个可用模型。
        target_model = model_id or preview.current_model or self._current_branch_model(preview)
        if not target_model:
            # 尝试获取第一个可用的模型
            for provider, models in self.model_manager.model_list.items():
                if models:
                    target_model = models[0]
                    logger.info(f"使用默认模型: {target_model}")
                    break
        
        if not target_model:
            yield StreamChunk(
                status=StreamStatus.ERROR,
                node_id=None,
                conversation_id=conversation_id,
                run_id=run_id,
                content="",
                error="未指定模型ID",
                tokens_used=0
            )
            return
        
        # 获取提供商
        target_provider = (
            provider_id
            or preview.current_provider
            or self._provider_for_model(target_model)
        )

        logger.info(f"Stream: model={target_model}, provider={target_provider}, model_list_keys={list(self.model_manager.model_list.keys())}")

        if not target_provider:
            yield StreamChunk(
                status=StreamStatus.ERROR,
                content="",
                node_id=None,
                conversation_id=conversation_id,
                run_id=run_id,
                error=f"无法找到模型 {target_model} 对应的提供商",
                tokens_used=0
            )
            return

        slash_result = self._dispatch_slash_content(content)
        if slash_result.kind in {
            SlashDispatchKind.SUBAGENT,
            SlashDispatchKind.WORKFLOW,
            SlashDispatchKind.ERROR,
        }:
            yield StreamChunk(
                status=StreamStatus.ERROR,
                content="",
                node_id=None,
                conversation_id=conversation_id,
                run_id=run_id,
                error=self._slash_runtime_error(slash_result),
                tokens_used=0,
            )
            return
        model_content = slash_result.model_input or content
        
        provider = self.model_manager.get_model(target_provider, True)
        if not provider:
            logger.error(f"无法初始化提供商 {target_provider} (is_async=True)")
            yield StreamChunk(
                status=StreamStatus.ERROR,
                content="",
                node_id=None,
                conversation_id=conversation_id,
                run_id=run_id,
                error=f"无法初始化提供商 {target_provider}",
                tokens_used=0
            )
            return
        
        # 解析有效推理参数：请求传入 > 对话 metadata > 模型默认；再按模型元数据校验。
        # metadata 不支持的档位/开关会被规范化为 None（不发送），保护配错的第三方模型。
        from ..model.model_metadata import normalize_effort, normalize_thinking
        if hasattr(self.model_manager, "get_model_metadata"):
            meta = self.model_manager.get_model_metadata(target_provider, target_model)
        else:
            meta = {}

        if not parent_node_id:
            auto_result = await self._auto_compact_if_needed(
                conversation_id,
                target_model=target_model,
                target_provider=target_provider,
                model_context_window=meta.get("context_length"),
            )
            if auto_result.get("was_compacted"):
                latest_preview = self.get_conversation(conversation_id)
                if latest_preview is not None:
                    preview = latest_preview

        conv_meta = preview.metadata
        effort_spec = meta.get("reasoning_effort") or {}
        thinking_spec = meta.get("thinking") or {}
        eff_effort = (
            reasoning_effort
            if reasoning_effort is not None
            else conv_meta.get("reasoning_effort")
            if conv_meta.get("reasoning_effort") is not None
            else effort_spec.get("default")
        )
        eff_thinking = (
            thinking_enabled
            if thinking_enabled is not None
            else conv_meta.get("thinking_enabled")
            if conv_meta.get("thinking_enabled") is not None
            else (thinking_spec.get("default_enabled") if thinking_spec.get("toggleable") else None)
        )
        eff_effort = normalize_effort(eff_effort, meta)
        eff_thinking = normalize_thinking(eff_thinking, meta)
        logger.info(f"Stream reasoning: effort={eff_effort}, thinking={eff_thinking}")
        if slash_result.kind == SlashDispatchKind.SIDE_QUESTION:
            side_run_context = Conversation.from_dict(preview.to_dict())
            if parent_node_id and parent_node_id in side_run_context.nodes:
                side_run_context.switch_to_node(parent_node_id)
            async for chunk in self._send_side_question_stream(
                conversation=side_run_context,
                content=model_content,
                provider=provider,
                target_model=target_model,
                eff_effort=eff_effort,
                eff_thinking=eff_thinking,
                run_id=run_id,
            ):
                yield chunk
            return
        requested_parent_node_id = parent_node_id
        if not requested_parent_node_id:
            yield StreamChunk(
                status=StreamStatus.ERROR,
                content="",
                node_id=None,
                conversation_id=conversation_id,
                run_id=run_id,
                error="parent_node_id is required",
                tokens_used=0,
            )
            return

        control_event_payload = dict(control_event or {})
        is_control_event_turn = bool(control_event_payload)
        user_msg: Optional[Message] = None
        if not is_control_event_turn and not suppress_user_message:
            # 创建用户消息
            user_msg = Message({
                "id": str(uuid.uuid4()),
                "role": Role.NOTIFY if message_subtype == "task_notification" else Role.USER,
                "content": model_content,
                "name": None,
                "tool_calls": None,
                "tool_call_id": None,
                "timestamp": int(time())
            })
            if message_subtype:
                user_msg["subtype"] = message_subtype
            if hidden_user_message:
                user_msg["is_hidden_from_transcript"] = True
            if slash_result.kind == SlashDispatchKind.MAIN_PROMPT:
                user_msg["slash_command"] = self._slash_command_metadata(slash_result)
        normalized_import_files = self._normalize_import_file_refs(import_files)
        if user_msg is not None and normalized_import_files:
            user_msg["import_files"] = normalized_import_files
        normalized_image_refs = self._normalize_image_refs(image_refs)
        if user_msg is not None and normalized_image_refs:
            user_msg["image_refs"] = normalized_image_refs

        skill_names: list[str] = []
        await self._restore_plan_snapshot_from_conversation(preview)
        pending_plan_context = await self._consume_plan_context(conversation_id)
        plan_context_permission_mode = self._plan_context_permission_mode(pending_plan_context)
        active_plan_permission_mode = await self._active_plan_permission_mode(conversation_id)
        plan_snapshot_after_context = await self._plan_snapshot_for_metadata(conversation_id)
        requested_tool_permission_mode = (
            normalize_permission_mode(tool_permission_mode)
            if tool_permission_mode not in (None, "")
            else None
        )
        if (
            requested_tool_permission_mode == "plan"
            and plan_context_permission_mode != "plan"
            and active_plan_permission_mode != "plan"
        ):
            yield StreamChunk(
                status=StreamStatus.ERROR,
                content="",
                node_id=None,
                conversation_id=conversation_id,
                run_id=run_id,
                error="计划模式必须通过 enter_plan_mode 创建 PlanSession，不能作为普通工具权限直接发送。",
                tokens_used=0,
            )
            return

        # ── 临界区 1（锁内）：重新加载最新快照 + 建节点 + 立即保存 user 消息 ──
        # 锁内重载确保看到其他并发流刚提交的兄弟节点，add_node 不会丢失 root 引用。
        # 立即落盘是为了让前端的 userMsgLanded 判定能尽快看到真实 user 消息。
        async with self._lock_for(conversation_id):
            conversation = self.get_conversation(conversation_id)
            if conversation is None:
                yield StreamChunk(
                    status=StreamStatus.ERROR, content="", node_id=None,
                    conversation_id=conversation_id, run_id=run_id, error="对话不存在", tokens_used=0)
                return
            if requested_parent_node_id not in conversation.nodes:
                yield StreamChunk(
                    status=StreamStatus.ERROR, content="", node_id=None,
                    conversation_id=conversation_id, run_id=run_id, error="父节点不存在", tokens_used=0)
                return
            current_node_id = requested_parent_node_id
            parent_tool_permission_mode = None
            if current_node_id and current_node_id in conversation.nodes:
                parent_tool_permission_mode = conversation.nodes[current_node_id].get("tool_permission_mode")
            if (
                parent_tool_permission_mode == "plan"
                and plan_context_permission_mode != "plan"
                and active_plan_permission_mode != "plan"
            ):
                parent_tool_permission_mode = None
            eff_tool_permission_mode = normalize_permission_mode(
                requested_tool_permission_mode
                if requested_tool_permission_mode
                else plan_context_permission_mode or active_plan_permission_mode or parent_tool_permission_mode or "ask_always"
            )
            if (
                self.capability_registry is not None
                and not hidden_user_message
                and not is_control_event_turn
                and not suppress_user_message
            ):
                skill_names = collect_skill_injection_names(
                    model_content,
                    self.capability_registry,
                    active_skill_names=self._recent_active_skill_names(conversation),
                )
            new_node = NodeManager.create_node(
                user_message=user_msg,
                parent_id=current_node_id,
                model_id=target_model,
                tool_permission_mode=eff_tool_permission_mode,
            )
            if skill_names:
                new_node["active_skill_names"] = skill_names
            conversation.add_node(new_node, parent_id=current_node_id, focus=focus_new_node)
            self._set_conversation_model_metadata(
                conversation,
                provider_id=target_provider,
                model_id=target_model,
            )
            if plan_snapshot_after_context is not None:
                conversation.metadata["plan_ledger"] = plan_snapshot_after_context
            self._update_branch_usage_for_node(
                conversation,
                new_node["id"],
                model_context_window=meta.get("context_length"),
            )
            self._save(conversation)
            if is_control_event_turn:
                self._persist_sqlite_control_event_turn(
                    conversation=conversation,
                    node=new_node,
                    control_event=control_event_payload,
                    provider_id=target_provider,
                    model_id=target_model,
                    run_id=run_id,
                )
            elif user_msg is not None:
                self._persist_sqlite_user_turn(
                    conversation=conversation,
                    node=new_node,
                    user_msg=user_msg,
                    provider_id=target_provider,
                    model_id=target_model,
                    run_id=run_id,
                )

        # 创建流控制器（在锁外，避免把网络流式包进锁里阻塞同对话其他分支）
        controller = StreamController(
            node_id=new_node["id"],
            conversation_id=conversation.metadata["id"],
            run_id=run_id,
        )
        self._active_controllers[new_node["id"]] = controller
        yield StreamChunk(
            status=StreamStatus.START,
            content=None,
            node_id=new_node["id"],
            anchor_node_id=current_node_id,
            target_node_id=new_node["id"],
            conversation_id=conversation_id,
            run_id=run_id,
            tokens_used=0,
            tool_permission_mode=new_node.get("tool_permission_mode"),
        )

        # 准备消息链。即使调用方要求不切换 UI 焦点，模型也必须基于刚创建的
        # new_node 回复，否则后台通知这类 focus_new_node=False 的消息不会进入上下文。
        prompt_conversation = conversation
        if conversation.current_node_id != new_node["id"]:
            prompt_conversation = Conversation.from_dict(conversation.to_dict())
            prompt_conversation.switch_to_node(new_node["id"])
        messages = self._build_prompt_messages(prompt_conversation, skill_names)
        messages.extend(self._plan_context_messages(pending_plan_context))
        if continuation_messages:
            self._apply_continuation_messages(messages, continuation_messages)

        workspace_context = normalize_workspace(
            preview.metadata.get("workspace"),
            build_default_workspace(cfg.data if isinstance(cfg.data, dict) else None),
        )

        multi_agent_mode = self._resolve_multi_agent_mode(
            self._multi_agent_intent_text(prompt_conversation, model_content),
            prompt_conversation.metadata if prompt_conversation is not None else (preview.metadata if preview is not None else {}),
        )
        available_tools = self.tool_manager.get_openai_tools() if self.tool_manager else []
        tools = self._filter_agent_tools_for_mode(available_tools, multi_agent_mode)
        tools = self._filter_plan_tools_for_mode(tools, new_node.get("tool_permission_mode") or "default")
        tools = tools or None
        if slash_result.tool_policy == SlashToolPolicy.DISABLED:
            tools = None
        max_tool_rounds = int(cfg.data.get("tools", {}).get("max_rounds", 5)) if isinstance(cfg.data, dict) else 5

        total_content = ""
        total_reasoning = ""
        tokens_used = 0
        usage_info = None
        start_time = time()  # 记录开始时间
        generation_status = "completed"  # 默认状态
        error_message = None
        final_content = ""
        final_reasoning = ""
        persisted_final_content: Optional[str] = None

        try:
            all_tool_calls: List[Dict[str, Any]] = []
            all_tool_messages: List[Message] = []
            tool_interactions: List[Dict[str, Any]] = []
            all_approval_events: List[Dict[str, Any]] = []
            tool_round = 0
            task_guard_nudge_count = 0
            plan_guard_nudge_count = 0
            max_task_guard_nudges = self._max_task_guard_nudges(cfg.data)
            max_plan_guard_nudges = 3

            while True:
                if await controller.is_stopped():
                    generation_status = "stopped"
                    yield StreamChunk(
                        status=StreamStatus.STOPPED,
                        content="",
                        node_id=new_node["id"],
                        target_node_id=new_node["id"],
                        conversation_id=conversation_id,
                        run_id=run_id,
                        error=None,
                        tokens_used=tokens_used,
                    )
                    break

                round_content = ""
                round_reasoning = ""
                round_status = "completed"
                complete_chunk = None
                round_tool_calls: List[Dict[str, Any]] = []
                defer_round_content = (
                    await self._has_open_tasks(conversation_id)
                    or await self._has_active_plan_mode(conversation_id, new_node.get("tool_permission_mode"))
                )
                deferred_content_chunks: List[Dict[str, Any]] = []

                # provider 引用已在循环前捕获（见上方 get_model）。即便此刻 config 变更
                # 重建了 model_manager，在途流仍用这个局部 provider，不受影响。
                # 不要在循环内重新读取 self.model_manager。
                async for chunk in provider.generate_response_stream(
                    model=target_model,
                    messages=messages,
                    stream_controller=controller,
                    tools=tools,
                    tool_choice="auto" if tools else None,
                    reasoning_effort=eff_effort,
                    thinking_enabled=eff_thinking,
                ): # type: ignore
                    if r := chunk.get("reasoning"):
                        total_reasoning += r
                        round_reasoning += r
                    if data := chunk.get("content"):
                        total_content += data
                        round_content += data
                    if chunk.get("tool_calls"):
                        round_tool_calls = self._merge_tool_call_lists(round_tool_calls, chunk.get("tool_calls") or [])
                    elif chunk.get("tool_call"):
                        embedded = chunk.get("tool_call") or {}
                        if embedded.get("tool_calls"):
                            round_tool_calls = self._merge_tool_call_lists(round_tool_calls, embedded.get("tool_calls") or [])

                    chunk_status = chunk.get("status")
                    if chunk_status == StreamStatus.ERROR:
                        generation_status = "error"
                        error_message = chunk.get("error")
                        round_status = "error"
                    elif chunk_status == StreamStatus.STOPPED:
                        generation_status = "stopped"
                        round_status = "stopped"
                    if chunk_status == StreamStatus.COMPLETE:
                        tokens_used = chunk.get("tokens_used", tokens_used) or tokens_used
                        usage_info = chunk.get("usage_info") or usage_info
                        tokens_used = usage_total(usage_info, tokens_used)
                        complete_chunk = chunk
                        continue

                    chunk["conversation_id"] = conversation_id
                    if run_id:
                        chunk["run_id"] = run_id
                    if defer_round_content and chunk.get("content"):
                        deferred_content_chunks.append(dict(chunk))
                    else:
                        yield chunk

                if round_status != "completed":
                    final_content = round_content
                    final_reasoning = round_reasoning
                    if complete_chunk:
                        complete_chunk["conversation_id"] = conversation_id
                        complete_chunk["run_id"] = run_id
                        complete_chunk["target_node_id"] = new_node["id"]
                        yield complete_chunk
                    break

                if not round_tool_calls:
                    needs_plan_nudge = await self._needs_plan_mode_nudge(
                        conversation_id,
                        new_node.get("tool_permission_mode") or "default",
                    )
                    if needs_plan_nudge and tools and plan_guard_nudge_count < max_plan_guard_nudges:
                        plan_guard_nudge_count += 1
                        messages.append({
                            "role": "system",
                            "content": self._plan_mode_nudge(attempt=plan_guard_nudge_count),
                        })
                        continue
                    if needs_plan_nudge:
                        guard_message = self._plan_guard_blocked_message()
                        final_content = guard_message
                        persisted_final_content = guard_message
                        total_content = guard_message
                        yield StreamChunk(
                            status=StreamStatus.CONTENT,
                            content=guard_message,
                            node_id=new_node["id"],
                            target_node_id=new_node["id"],
                            conversation_id=conversation_id,
                            run_id=run_id,
                            error=None,
                            tokens_used=0,
                        )
                        final_reasoning = round_reasoning
                        if complete_chunk:
                            complete_chunk["conversation_id"] = conversation_id
                            complete_chunk["run_id"] = run_id
                            complete_chunk["target_node_id"] = new_node["id"]
                            yield complete_chunk
                        break
                    needs_task_nudge, open_tasks = await self._needs_task_completion_nudge(
                        conversation_id,
                        round_content,
                    )
                    if needs_task_nudge and tools and task_guard_nudge_count < max_task_guard_nudges:
                        task_guard_nudge_count += 1
                        messages.append({
                            "role": "system",
                            "content": self._task_completion_nudge(open_tasks, attempt=task_guard_nudge_count),
                        })
                        continue
                    if needs_task_nudge:
                        guard_message = self._task_guard_blocked_message(open_tasks)
                        final_content = guard_message
                        persisted_final_content = guard_message
                        total_content = guard_message
                        yield StreamChunk(
                            status=StreamStatus.CONTENT,
                            content=guard_message,
                            node_id=new_node["id"],
                            target_node_id=new_node["id"],
                            conversation_id=conversation_id,
                            run_id=run_id,
                            error=None,
                            tokens_used=0,
                        )
                    else:
                        for deferred_chunk in deferred_content_chunks:
                            yield deferred_chunk
                        final_content = round_content
                        persisted_final_content = round_content
                    final_reasoning = round_reasoning
                    if complete_chunk:
                        complete_chunk["conversation_id"] = conversation_id
                        complete_chunk["run_id"] = run_id
                        complete_chunk["target_node_id"] = new_node["id"]
                        yield complete_chunk
                    break

                if not self.tool_manager:
                    logger.warning("Model requested tools but no ToolManager is configured")
                    for deferred_chunk in deferred_content_chunks:
                        yield deferred_chunk
                    final_content = round_content
                    persisted_final_content = round_content
                    final_reasoning = round_reasoning
                    if complete_chunk:
                        complete_chunk["conversation_id"] = conversation_id
                        complete_chunk["run_id"] = run_id
                        complete_chunk["target_node_id"] = new_node["id"]
                        yield complete_chunk
                    break

                if tool_round >= max_tool_rounds:
                    error_message = f"工具调用轮数超过上限 {max_tool_rounds}"
                    generation_status = "error"
                    yield StreamChunk(
                        status=StreamStatus.ERROR,
                        content="",
                        node_id=new_node["id"],
                        target_node_id=new_node["id"],
                        conversation_id=conversation_id,
                        run_id=run_id,
                        error=error_message,
                        tokens_used=tokens_used,
                    )
                    break

                round_has_tool_calls = bool(round_tool_calls)
                round_text_is_intermediate = should_emit_as_intermediate_text(
                    has_tool_calls=round_has_tool_calls,
                    plan_or_task_guard_active=defer_round_content,
                )

                if not round_text_is_intermediate:
                    for deferred_chunk in deferred_content_chunks:
                        yield deferred_chunk
                tool_round += 1
                assistant_tool_message = {
                    "role": "assistant",
                    "content": round_content,
                    "tool_calls": round_tool_calls,
                }
                messages.append(assistant_tool_message)
                if round_text_is_intermediate:
                    for deferred_chunk in deferred_content_chunks:
                        deferred_chunk.setdefault("event_type", "process_content")
                        yield deferred_chunk
                approval_events: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()

                async def emit_tool_event(event: Dict[str, Any]):
                    await approval_events.put(event)
                    if (
                        event.get("event_type") == "tool_approval_request"
                        and await controller.is_stopped()
                    ):
                        approval_manager = getattr(
                            getattr(self, "tool_orchestrator", None),
                            "approval_manager",
                            None,
                        )
                        if approval_manager is not None:
                            approval_manager.cancel_for_node(new_node["id"])

                execute_task = asyncio.create_task(
                    self._execute_tool_calls(
                        round_tool_calls,
                        node_id=new_node["id"],
                        conversation_id=conversation_id,
                        emit_event=emit_tool_event,
                        workspace=workspace_context,
                        permission_mode=new_node.get("tool_permission_mode") or "default",
                        run_context={
                            "run_id": run_id,
                            "run_kind": "chat",
                            "root_run_id": run_id,
                            "conversation_id": conversation_id,
                            "anchor_node_id": current_node_id,
                            "node_id": new_node["id"],
                            "task_summary": model_content[:160],
                        },
                    )
                )
                event_get_task = asyncio.create_task(approval_events.get())
                async def wait_controller_stop():
                    while not await controller.is_stopped():
                        await asyncio.sleep(0.05)
                    return True
                stop_task = asyncio.create_task(wait_controller_stop())
                try:
                    while True:
                        done, _ = await asyncio.wait(
                            {execute_task, event_get_task, stop_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if stop_task in done and stop_task.result():
                            generation_status = "stopped"
                            round_status = "stopped"
                            execute_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await execute_task
                            yield StreamChunk(
                                status=StreamStatus.STOPPED,
                                content="",
                                node_id=new_node["id"],
                                target_node_id=new_node["id"],
                                conversation_id=conversation_id,
                                run_id=run_id,
                                error=None,
                                tokens_used=tokens_used,
                            )
                            break
                        if event_get_task in done:
                            event = event_get_task.result()
                            if str(event.get("event_type", "")).startswith("tool_approval_"):
                                all_approval_events.append(deepcopy(event))
                            yield self._tool_event_stream_chunk(
                                event,
                                node_id=new_node["id"],
                                conversation_id=conversation_id,
                            )
                            event_get_task = asyncio.create_task(approval_events.get())
                        if execute_task in done:
                            if event_get_task.done():
                                event = event_get_task.result()
                                if str(event.get("event_type", "")).startswith("tool_approval_"):
                                    all_approval_events.append(deepcopy(event))
                                yield self._tool_event_stream_chunk(
                                    event,
                                    node_id=new_node["id"],
                                    conversation_id=conversation_id,
                                )
                                event_get_task = asyncio.create_task(approval_events.get())
                            while not approval_events.empty():
                                event = approval_events.get_nowait()
                                if str(event.get("event_type", "")).startswith("tool_approval_"):
                                    all_approval_events.append(deepcopy(event))
                                yield self._tool_event_stream_chunk(
                                    event,
                                    node_id=new_node["id"],
                                    conversation_id=conversation_id,
                                )
                            tool_messages = await execute_task
                            next_permission_mode = self._permission_mode_after_plan_tools(
                                tool_messages,
                                new_node.get("tool_permission_mode") or "default",
                            )
                            if next_permission_mode != new_node.get("tool_permission_mode"):
                                new_node["tool_permission_mode"] = next_permission_mode
                                yield StreamChunk(
                                    status=StreamStatus.CONTENT,
                                    content=None,
                                    node_id=new_node["id"],
                                    target_node_id=new_node["id"],
                                    conversation_id=conversation_id,
                                    run_id=run_id,
                                    error=None,
                                    tokens_used=0,
                                    event_type="permission_mode_changed",
                                    tool_permission_mode=next_permission_mode,
                                )
                                if slash_result.tool_policy != SlashToolPolicy.DISABLED:
                                    tools = self._filter_agent_tools_for_mode(available_tools, multi_agent_mode)
                                    tools = self._filter_plan_tools_for_mode(tools, next_permission_mode) or None
                            break
                finally:
                    if not event_get_task.done():
                        event_get_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await event_get_task
                    if not stop_task.done():
                        stop_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await stop_task
                    if not execute_task.done():
                        execute_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await execute_task
                if round_status != "completed":
                    break
                model_tool_messages = self._apply_round_tool_result_budget(tool_messages)
                messages.extend(model_tool_messages)
                all_tool_calls.extend(round_tool_calls)
                all_tool_messages.extend(tool_messages)
                tool_interactions.append({
                    "assistant": assistant_tool_message,
                    "tools": tool_messages,
                    "reasoning": round_reasoning or None,
                })
                for tool_msg in tool_messages:
                    yield StreamChunk(
                        status=StreamStatus.CONTENT,
                        content=None,
                        node_id=new_node["id"],
                        target_node_id=new_node["id"],
                        conversation_id=conversation_id,
                        run_id=run_id,
                        error=None,
                        tokens_used=0,
                        event_type="tool_result",
                        tool_call={
                            "tool_call_id": tool_msg.get("tool_call_id"),
                            "name": tool_msg.get("name"),
                            "content": tool_msg.get("content"),
                            "raw_content": tool_msg.get("raw_content"),
                            "model_visible_content": tool_msg.get("model_visible_content"),
                            "tool_result_id": tool_msg.get("tool_result_id"),
                        },
                    )
                if has_blocking_plan_tool_result(tool_messages):
                    final_content = ""
                    persisted_final_content = ""
                    final_reasoning = round_reasoning
                    if complete_chunk:
                        complete_chunk["conversation_id"] = conversation_id
                        complete_chunk["run_id"] = run_id
                        complete_chunk["target_node_id"] = new_node["id"]
                        yield complete_chunk
                    break

            # 检查是否被手动停止
            if await controller.is_stopped():
                generation_status = "stopped"

        except Exception as e:
            generation_status = "error"
            error_message = str(e) or e.__class__.__name__
            logger.exception(f"流式生成出错: {error_message}")
            yield StreamChunk(
                status=StreamStatus.ERROR,
                content="",
                node_id=new_node["id"],
                target_node_id=new_node["id"],
                conversation_id=conversation_id,
                run_id=run_id,
                error=error_message,
                tokens_used=tokens_used,
            )
        finally:
            # 计算用时
            duration_ms = int((time() - start_time) * 1000)
            if usage_info is None:
                usage_info = estimated_usage(tokens_used)
            tokens_used = usage_total(usage_info, tokens_used)
            completion_timestamp = int(time())
            plan_snapshot_for_save = await self._plan_snapshot_for_metadata(conversation_id)

            # 创建生成信息（tokens_used 来自流中捕获的最终值）
            generation_info: GenerationInfo = {
                "duration_ms": duration_ms,
                "status": generation_status,
                "error_message": error_message,
                "tokens_used": tokens_used,
                "usage_info": usage_info
            }
            needs_final_task_nudge, final_open_tasks = await self._needs_task_completion_nudge(
                conversation_id,
                persisted_final_content if persisted_final_content is not None else (final_content if tool_interactions else total_content),
            )
            if needs_final_task_nudge or task_guard_nudge_count > 0:
                generation_info["task_guard"] = {
                    "open_task_count": len(final_open_tasks),
                    "nudged": task_guard_nudge_count > 0,
                    "nudge_count": task_guard_nudge_count,
                }

            # 助手消息（包含生成信息）
            assistant_msg = Message({
                "id": str(uuid.uuid4()),
                "role": Role.ASSISTANT,
                "content": persisted_final_content if persisted_final_content is not None else (final_content if tool_interactions else total_content),
                "name": None,
                "tool_calls": all_tool_calls or None,
                "tool_call_id": None,
                "tool_results": all_tool_messages or None,
                "tool_interactions": tool_interactions or None,
                "approval_events": all_approval_events or None,
                "reasoning": (final_reasoning if tool_interactions else total_reasoning) or None,
                "timestamp": completion_timestamp,
                "generation_info": generation_info
            })

            # ── 临界区 2（锁内）：重新加载最新快照，仅在本节点上挂助手消息 + 累加 token + 保存 ──
            # 必须锁内重载：流式期间其他并发流可能已提交兄弟节点，直接保存临界区 1 的
            # 旧 conversation 会覆盖掉它们对 root.children_ids 的修改。
            # 必须在路由发送 [DONE] 之前完成（save-before-[DONE] 不变量）。
            async with self._lock_for(conversation_id):
                latest = self.get_conversation(conversation_id)
                if latest is not None and new_node["id"] in latest.nodes:
                    latest.nodes[new_node["id"]]["tool_permission_mode"] = new_node.get("tool_permission_mode")
                    NodeManager.add_assistant_message(latest.nodes[new_node["id"]], assistant_msg)
                    if all_tool_messages:
                        NodeManager.add_tool_messages(latest.nodes[new_node["id"]], all_tool_messages)
                    self._set_conversation_model_metadata(
                        latest,
                        provider_id=target_provider,
                        model_id=target_model,
                    )
                    if plan_snapshot_for_save is not None:
                        latest.metadata["plan_ledger"] = plan_snapshot_for_save
                    self._update_token_stats_for_conversation(latest, target_provider, tokens_used)
                    self._update_branch_usage_for_node(
                        latest,
                        new_node["id"],
                        model_context_window=meta.get("context_length"),
                    )
                    self._mark_conversation_updated_at(latest, completion_timestamp)
                    self._save(latest)
                else:
                    # 极端情况：节点已被并发删除——退回到只保存本节点，避免丢消息
                    NodeManager.add_assistant_message(new_node, assistant_msg)
                    if all_tool_messages:
                        NodeManager.add_tool_messages(new_node, all_tool_messages)
                    new_node["total_tokens"] = usage_total(usage_info, tokens_used)
                    new_node["branch_usage_info"] = usage_info
                    new_node["usage"] = self._node_usage_snapshot(
                        turn_usage=usage_info,
                        branch_usage=usage_info,
                        model_context_window=meta.get("context_length"),
                    )
                    self._set_conversation_model_metadata(
                        conversation,
                        provider_id=target_provider,
                        model_id=target_model,
                    )
                    if plan_snapshot_for_save is not None:
                        conversation.metadata["plan_ledger"] = plan_snapshot_for_save
                    self._mark_conversation_updated_at(conversation, completion_timestamp)
                    self.storage.save({
                        "metadata": conversation.metadata,
                        "nodes": [new_node],
                        "current_node_id": conversation.current_node_id,
                        "root_node_id": conversation.root_node_id,
                    })

            latest_for_transcript = self.get_conversation(conversation_id)
            if latest_for_transcript is not None:
                self._persist_sqlite_assistant_turn(
                    conversation=latest_for_transcript,
                    node=latest_for_transcript.nodes.get(new_node["id"], new_node),
                    assistant_msg=assistant_msg,
                    provider_id=target_provider,
                    model_id=target_model,
                    run_id=run_id,
                    generation_status=generation_status,
                    tool_interactions=tool_interactions,
                    tool_messages=all_tool_messages,
                    tool_calls=all_tool_calls,
                    transcript_continuation=transcript_continuation,
                )

            # 清理控制器
            if new_node["id"] in self._active_controllers:
                del self._active_controllers[new_node["id"]]

    async def continue_plan_tool_result_stream(
        self,
        *,
        conversation_id: str,
        plan_id: str,
        tool_result_content: str,
        tool_call_id: str,
        tool_name: str,
        model_id: Optional[str] = None,
        provider_id: Optional[str] = None,
        node_id: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        thinking_enabled: Optional[bool] = None,
        tool_permission_mode: Optional[str] = None,
        run_id: Optional[str] = None,
        continuation_of_run_id: Optional[str] = None,
        continuation_marker: Optional[str] = None,
    ) -> AsyncIterator[StreamChunk]:
        marker = continuation_marker or (
            "计划已批准，开始实现"
            if normalize_permission_mode(tool_permission_mode) != "plan"
            else "计划反馈已提交，继续计划"
        )
        transcript_continuation = {
            "origin": "plan_tool_result_continuation",
            "plan_id": plan_id,
            "tool_name": tool_name,
            "tool_result_for": tool_call_id,
            "continuation_of_node_id": node_id,
            "continuation_of_run_id": continuation_of_run_id,
            "marker": marker,
        }
        continuation_message = Message({
            "id": str(uuid.uuid4()),
            "role": Role.TOOL,
            "content": tool_result_content,
            "name": tool_name,
            "tool_call_id": tool_call_id,
            "timestamp": int(time()),
            "model_visible_content": tool_result_content,
            "raw_content": tool_result_content,
        })
        async for chunk in self.send_message_stream(
            conversation_id=conversation_id,
            content="",
            model_id=model_id,
            provider_id=provider_id,
            parent_node_id=node_id,
            reasoning_effort=reasoning_effort,
            thinking_enabled=thinking_enabled,
            import_files=None,
            image_refs=None,
            tool_permission_mode=tool_permission_mode,
            message_subtype="plan_tool_result_continuation",
            run_id=run_id,
            continuation_messages=[continuation_message],
            suppress_user_message=True,
            transcript_continuation=transcript_continuation,
        ):
            yield chunk

    async def continue_plan_action_stream(
        self,
        *,
        conversation_id: str,
        content: str,
        model_id: Optional[str] = None,
        provider_id: Optional[str] = None,
        node_id: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        thinking_enabled: Optional[bool] = None,
        tool_permission_mode: Optional[str] = None,
        message_subtype: str,
        plan_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> AsyncIterator[StreamChunk]:
        """Continue after a structured plan-mode response, without a visible user turn."""
        plan_ledger = getattr(self, "plan_ledger", None)
        if plan_ledger is None:
            yield StreamChunk(
                status=StreamStatus.ERROR,
                node_id=None,
                conversation_id=conversation_id,
                run_id=run_id,
                content="",
                error="Plan ledger is not configured",
                tokens_used=0,
            )
            return

        await self._restore_plan_snapshot_from_conversation(self.get_conversation(conversation_id))
        try:
            current = await plan_ledger.get_active_or_awaiting(conversation_id)
            if current is None:
                raise ValueError("active plan session is required")
            if plan_id and current.plan_id != plan_id:
                raise ValueError("plan not found")
            status = getattr(getattr(current, "status", None), "value", getattr(current, "status", None))
            if message_subtype == "plan_approval_response":
                if status != "awaiting_approval":
                    raise ValueError("plan must be awaiting approval")
                plan = await plan_ledger.approve_plan(
                    conversation_id=conversation_id,
                    plan_id=current.plan_id,
                )
                tool_call_id = plan.exit_tool_call_id or ""
                if not tool_call_id:
                    raise ValueError("approved plan has no exit_plan_mode tool_call_id")
                tool_result_content = plan_ledger.approved_tool_result_content(plan)
                tool_name = "exit_plan_mode"
                continuation_permission_mode = plan.previous_permission_mode
            elif message_subtype == "plan_question_response":
                if status != "awaiting_question":
                    raise ValueError("plan must be awaiting question")
                plan = await plan_ledger.answer_question(
                    conversation_id=conversation_id,
                    plan_id=current.plan_id,
                    answer=content,
                )
                tool_call_id = plan.question_tool_call_id or ""
                if not tool_call_id:
                    raise ValueError("answered plan has no ask_user_question tool_call_id")
                tool_result_content = plan_ledger.question_answer_tool_result_content(plan)
                tool_name = "ask_user_question"
                continuation_permission_mode = "plan"
            else:
                raise ValueError("unsupported plan action response")
            await plan_ledger.consume_pending_context(conversation_id)
        except Exception as exc:
            yield StreamChunk(
                status=StreamStatus.ERROR,
                node_id=None,
                conversation_id=conversation_id,
                run_id=run_id,
                content="",
                error=str(exc),
                tokens_used=0,
            )
            return

        async for chunk in self.continue_plan_tool_result_stream(
            conversation_id=conversation_id,
            plan_id=current.plan_id,
            tool_result_content=tool_result_content,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            model_id=model_id,
            provider_id=provider_id,
            node_id=node_id,
            reasoning_effort=reasoning_effort,
            thinking_enabled=thinking_enabled,
            tool_permission_mode=continuation_permission_mode,
            run_id=run_id,
            continuation_of_run_id=plan.submitted_run_id or plan.entered_run_id,
            continuation_marker=(
                "计划已批准，开始实现"
                if message_subtype == "plan_approval_response"
                else "计划反馈已提交，继续计划"
            ),
        ):
            yield chunk

    async def stop_stream(self, node_id: str) -> bool:
        """终止指定节点的流式生成（同步置位停止标志，不用 fire-and-forget task）。"""
        controller = self._active_controllers.get(node_id)
        if controller:
            await controller.stop()
            approval_manager = getattr(
                getattr(self, "tool_orchestrator", None),
                "approval_manager",
                None,
            )
            if approval_manager is not None:
                approval_manager.cancel_for_node(node_id)
            logger.info(f"已请求终止节点 {node_id} 的流")
            return True
        return False

    async def stop_all_streams(self):
        """终止所有活跃流"""
        for node_id in list(self._active_controllers.keys()):
            await self.stop_stream(node_id)

    def _is_compact_boundary_node(self, node: Dict[str, Any]) -> bool:
        system_message = node.get("system_message") or {}
        return (
            system_message.get("role") in (Role.SYSTEM, "system")
            and system_message.get("subtype") == "compact_boundary"
        )

    def _selected_system_prompt(self, conversation: Conversation) -> tuple[Optional[str], str]:
        prompt = conversation.metadata.get("selected_system_prompt") or {}
        if not isinstance(prompt, dict):
            return None, "override"
        content = prompt.get("content")
        if not isinstance(content, str) or not content.strip():
            return None, "override"
        return content, self._normalize_selected_system_prompt_mode(
            str(prompt.get("mode") or "override")
        )

    def _runtime_prompt_context(
        self,
        runtime: str,
        conversation: Optional[Conversation] = None,
        *,
        latest_user_content: str = "",
    ) -> RuntimePromptContext:
        details = self._runtime_context_details(conversation)
        if runtime == "side_question":
            return RuntimePromptContext(
                name="side_question",
                content="\n".join([
                    "## Runtime Context",
                    "",
                    "Runtime mode: side question (/btw)",
                    *details,
                    "- Answer only the side question using the current conversation context.",
                    "- Keep the run read-only: do not call tools, edit files, or create a main-branch response.",
                    "- Preserve selected system prompt semantics: default core prompt, override custom prompt, or appended custom prompt.",
                ]),
                metadata={"runtime_mode": "side_question"},
            )
        multi_agent_mode = self._resolve_multi_agent_mode(
            self._multi_agent_intent_text(conversation, latest_user_content),
            conversation.metadata if conversation is not None else {},
        )
        multi_agent_lines: list[str] = []
        if multi_agent_mode != "none":
            multi_agent_lines = [
                "- Multi-agent tools are available in this conversation when tool schemas include `spawn_agent`.",
                "- If the user explicitly asks to use a subagent, agent, forked agent, or workflow, your first relevant action must be `spawn_agent` or the appropriate agent/workflow tool call.",
                "- Do not replace an explicit subagent request with direct shell commands, file tools, or a natural-language claim that a subagent was started.",
                "- Use `wait_agent` when you need the delegated result before answering. Use notification delivery for background work.",
            ]
            if multi_agent_mode == "proactive":
                multi_agent_lines.append("- You may proactively delegate independent multi-step investigation or verification work to subagents.")
            else:
                multi_agent_lines.append("- Do not proactively spawn agents unless the user explicitly requested agent delegation.")
        task_lines = self._format_open_tasks_for_prompt(conversation)
        plan_lines = self._plan_mode_runtime_lines(conversation)
        return RuntimePromptContext(
            name="main",
                content="\n".join([
                    "## Runtime Context",
                    "",
                    "Runtime mode: main chat",
                    *details,
                    "- This is the primary persisted conversation branch.",
                    "- Use tools only when they are provided for this call and follow the active permission mode.",
                    *multi_agent_lines,
                    *task_lines,
                    *plan_lines,
                    "- Preserve selected system prompt semantics: default core prompt, override custom prompt, or appended custom prompt.",
            ]),
            metadata={"runtime_mode": "main"},
        )

    def _plan_mode_runtime_lines(self, conversation: Optional[Conversation]) -> list[str]:
        if getattr(self, "plan_ledger", None) is None:
            return []
        active_mode = self._current_node_permission_mode(conversation)
        if active_mode == "plan":
            return [
                "",
                "Plan mode is active:",
                "- You are in a read-only planning phase. Inspect, search, compare approaches, and reason only with read-only tools.",
                "- Do not edit files, run implementation commands, start implementation work, change configuration, commit, or claim changes were made.",
                "- Do not write the full plan in assistant text. The user will review the plan card.",
                "- Call update_plan whenever the plan artifact needs to be created or changed.",
                "- When the plan changes, call `update_plan` with either `replace` or `apply_patch`.",
                "- Call exit_plan_mode with no arguments when the artifact is ready for approval.",
                "- Your turn must end with exactly one structured plan-mode action: call `ask_user_question` if a genuine user decision is required, or call `exit_plan_mode` with no arguments when ready for approval.",
                "- Do not ask whether the plan is acceptable in text; `exit_plan_mode` is the only plan-approval path.",
                "- If the user changes direction while you are in plan mode, update the plan-mode work instead of implementing until a plan is approved.",
            ]
        return [
            "",
            "Plan mode rules:",
            "- Use `enter_plan_mode` only when the user explicitly asks for planning/exploration before implementation, or when the implementation approach has genuine ambiguity and user sign-off would prevent significant rework.",
            "- Do not enter plan mode merely because the task is large. If the path is clear, even across multiple files, proceed with implementation using the existing codebase patterns.",
            "- When the user asks you to implement now, directly execute, or complete the change, start working instead of planning unless continuing would violate safety or permission rules.",
            "- Prefer direct implementation for small fixes, clear bug fixes after diagnosis, specific instructions, and features that follow an obvious existing pattern.",
            "- Use `ask_user_question` in plan mode only for genuine user decisions that block planning; do not use it to ask whether the completed plan is acceptable.",
            "- When plan mode is active, call `update_plan` to write the plan artifact. When the plan is ready, call `exit_plan_mode` with no arguments and wait for user approval before implementing.",
        ]

    def _format_open_tasks_for_prompt(self, conversation: Optional[Conversation]) -> list[str]:
        task_ledger = getattr(self, "task_ledger", None)
        if conversation is None or task_ledger is None:
            return []
        conversation_id = str((conversation.metadata or {}).get("id") or "")
        if not conversation_id:
            return []
        try:
            tasks = task_ledger.list_open_tasks_snapshot(conversation_id, limit=8)
        except Exception:
            logger.exception("Failed to snapshot open tasks for prompt")
            return []
        if not tasks:
            return []
        lines = ["", "Open Tasks:"]
        for task in tasks:
            owner = f"{task.owner_type.value}"
            if task.owner_run_id:
                owner += f" {task.owner_run_id}"
            evidence = f" blocked: {self._compact_task_text(task.evidence_summary, 90)}" if task.status == TaskStatus.BLOCKED and task.evidence_summary else ""
            title = self._compact_task_text(task.title or task.detail or task.task_id, 120)
            lines.append(f"- {task.task_id} [{task.status.value}] {title} (owner: {owner}){evidence}")
        lines.extend([
            "",
            "TaskLedger rules:",
            "- If you create a task, update it to completed, blocked, or cancelled.",
            "- Before final response, all open tasks must be resolved or explicitly marked blocked with evidence.",
        ])
        return lines

    @staticmethod
    def _compact_task_text(value: Any, max_chars: int) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 3)].rstrip() + "..."

    async def _consume_plan_context(self, conversation_id: str) -> list[Dict[str, Any]]:
        plan_ledger = getattr(self, "plan_ledger", None)
        if plan_ledger is None or not conversation_id:
            return []
        try:
            items = await plan_ledger.consume_pending_context(conversation_id)
        except Exception:
            logger.exception("Failed to consume plan context")
            return []
        return [item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in items]

    async def _restore_plan_snapshot_from_conversation(self, conversation: Optional[Conversation]) -> None:
        plan_ledger = getattr(self, "plan_ledger", None)
        if plan_ledger is None or conversation is None:
            return
        conversation_id = str((conversation.metadata or {}).get("id") or "")
        snapshot = (conversation.metadata or {}).get("plan_ledger")
        if not conversation_id or not isinstance(snapshot, dict):
            return
        try:
            current = await plan_ledger.snapshot(conversation_id)
            if current.get("plans") or current.get("pending_context"):
                return
            await plan_ledger.load_snapshot(conversation_id, snapshot)
        except Exception:
            logger.exception("Failed to restore plan ledger snapshot")

    async def restore_plan_snapshot(self, conversation_id: str) -> None:
        await self._restore_plan_snapshot_from_conversation(self.get_conversation(conversation_id))

    async def _answer_pending_plan_question_from_user_message(self, conversation_id: str, content: Any) -> None:
        plan_ledger = getattr(self, "plan_ledger", None)
        answer = str(content or "").strip()
        if plan_ledger is None or not conversation_id or not answer:
            return
        try:
            current = await plan_ledger.get_active_or_awaiting(conversation_id)
            if current is None or getattr(current.status, "value", current.status) != "awaiting_question":
                return
            await plan_ledger.answer_question(
                conversation_id=conversation_id,
                plan_id=current.plan_id,
                answer=answer,
            )
        except Exception:
            logger.exception("Failed to answer pending plan question from user message")

    async def _approve_pending_plan_from_user_message(self, conversation_id: str, content: Any) -> None:
        plan_ledger = getattr(self, "plan_ledger", None)
        message = str(content or "").strip()
        if plan_ledger is None or not conversation_id or message != "继续实现已批准的计划。":
            return
        try:
            current = await plan_ledger.get_active_or_awaiting(conversation_id)
            if current is None or getattr(current.status, "value", current.status) != "awaiting_approval":
                return
            await plan_ledger.approve_plan(
                conversation_id=conversation_id,
                plan_id=current.plan_id,
            )
        except Exception:
            logger.exception("Failed to approve pending plan from user message")

    async def _plan_snapshot_for_metadata(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        plan_ledger = getattr(self, "plan_ledger", None)
        if plan_ledger is None or not conversation_id:
            return None
        try:
            return await plan_ledger.snapshot(conversation_id)
        except Exception:
            logger.exception("Failed to snapshot plan ledger")
            return None

    async def persist_plan_snapshot(self, conversation_id: str) -> None:
        snapshot = await self._plan_snapshot_for_metadata(conversation_id)
        if snapshot is None:
            return
        async with self._lock_for(conversation_id):
            conversation = self.get_conversation(conversation_id)
            if conversation is None:
                return
            conversation.metadata["plan_ledger"] = snapshot
            self._save(conversation)

    def _plan_context_permission_mode(self, context_items: list[Dict[str, Any]]) -> Optional[str]:
        for item in reversed(context_items):
            mode = item.get("permission_mode")
            if mode:
                return normalize_permission_mode(mode)
        return None

    async def _active_plan_permission_mode(self, conversation_id: str) -> Optional[str]:
        plan_ledger = getattr(self, "plan_ledger", None)
        if plan_ledger is None or not conversation_id:
            return None
        try:
            current = await plan_ledger.get_active_or_awaiting(conversation_id)
        except Exception:
            logger.exception("Failed to inspect active plan permission mode")
            return None
        return "plan" if current is not None else None

    def _plan_context_messages(self, context_items: list[Dict[str, Any]]) -> list[Message]:
        messages: list[Message] = []
        for item in context_items:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            messages.append(Message({
                "role": "system",
                "content": "\n".join([
                    "<system-reminder>",
                    content,
                    "</system-reminder>",
                ]),
            }))
        return messages

    def _apply_continuation_messages(
        self,
        messages: list[Message],
        continuation_messages: list[Message],
    ) -> None:
        for continuation in continuation_messages:
            tool_call_id = str(continuation.get("tool_call_id") or "")
            name = str(continuation.get("name") or "")
            replacement = {
                "role": "tool",
                "content": str(continuation.get("model_visible_content") or continuation.get("content") or ""),
                "tool_call_id": tool_call_id,
                "name": name,
            }
            for index in range(len(messages) - 1, -1, -1):
                message = messages[index]
                if (
                    str(message.get("role") or "") == "tool"
                    and str(message.get("tool_call_id") or "") == tool_call_id
                    and (not name or str(message.get("name") or "") == name)
                ):
                    messages[index] = Message(replacement)
                    break
            else:
                messages.append(Message(replacement))

    def _permission_mode_after_plan_tools(self, tool_messages: list[Message], current_mode: str) -> str:
        mode = normalize_permission_mode(current_mode)
        for message in tool_messages:
            name = str(message.get("name") or "")
            payload = self._json_tool_payload(message.get("raw_content") or message.get("content"))
            if name == "enter_plan_mode" and payload.get("permission_mode") == "plan":
                mode = "plan"
            elif name == "exit_plan_mode" and payload.get("status") == "awaiting_approval":
                mode = "plan"
            elif name == "ask_user_question" and payload.get("status") == "awaiting_question":
                mode = "plan"
        return mode

    def _plan_tool_paused_turn(self, tool_messages: list[Message]) -> bool:
        return has_blocking_plan_tool_result(tool_messages)

    @staticmethod
    def _json_tool_payload(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        if not isinstance(value, str):
            return {}
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _max_task_guard_nudges(config_data: Any) -> int:
        if not isinstance(config_data, dict):
            return 3
        tasks_config = config_data.get("tasks", {})
        if not isinstance(tasks_config, dict):
            return 3
        try:
            return max(1, int(tasks_config.get("max_guard_nudges", 3)))
        except (TypeError, ValueError):
            return 3

    async def _needs_task_completion_nudge(self, conversation_id: str, assistant_text: str) -> tuple[bool, list[TaskRecord]]:
        task_ledger = getattr(self, "task_ledger", None)
        if task_ledger is None or not conversation_id:
            return False, []
        try:
            open_tasks = await task_ledger.list_open_tasks(conversation_id)
        except Exception:
            logger.exception("Failed to list open tasks for completion guard")
            return False, []
        if not open_tasks:
            return False, []
        for task in open_tasks:
            if task.status != TaskStatus.BLOCKED:
                return True, open_tasks
            evidence = (task.evidence_summary or "").strip()
            if not (task.evidence_run_id or evidence):
                return True, open_tasks
        return False, open_tasks

    async def _has_open_tasks(self, conversation_id: str) -> bool:
        task_ledger = getattr(self, "task_ledger", None)
        if task_ledger is None or not conversation_id:
            return False
        try:
            return bool(await task_ledger.list_open_tasks(conversation_id))
        except Exception:
            logger.exception("Failed to check open tasks for stream guard")
            return False

    async def _has_active_plan_mode(self, conversation_id: str, permission_mode: Any) -> bool:
        needs_nudge = await self._needs_plan_mode_nudge(conversation_id, permission_mode)
        return needs_nudge

    async def _needs_plan_mode_nudge(self, conversation_id: str, permission_mode: Any) -> bool:
        if normalize_permission_mode(permission_mode) != "plan":
            return False
        plan_ledger = getattr(self, "plan_ledger", None)
        if plan_ledger is None or not conversation_id:
            return False
        try:
            current = await plan_ledger.get_active_or_awaiting(conversation_id)
        except Exception:
            logger.exception("Failed to inspect active plan mode")
            return False
        return bool(current is not None and getattr(current.status, "value", current.status) == "active")

    def _plan_mode_nudge(self, *, attempt: int = 1) -> str:
        return "\n".join([
            "<system-reminder>",
            "Plan mode final response was discarded because plan mode can only end by calling a plan-mode tool.",
            "You are already in plan mode. Continue read-only planning.",
            "Do not write the full plan in assistant text. Call update_plan to create or revise the plan artifact.",
            "At the end of this turn, you MUST call exactly one of these tools:",
            "- ask_user_question: only when a genuine user decision is required to continue planning.",
            "- exit_plan_mode: with no arguments, when the plan artifact is ready for user approval.",
            "Do not ask for plan approval in plain text. Do not edit files, run implementation commands, or claim the plan is approved.",
            f"Attempt: {attempt}",
            "</system-reminder>",
        ])

    def _plan_guard_blocked_message(self) -> str:
        return "\n".join([
            "计划模式仍在等待模型调用 `ask_user_question` 或 `exit_plan_mode`。",
            "已丢弃普通最终回复，避免绕过计划审批流程。",
        ])

    def _task_completion_nudge(self, open_tasks: list[TaskRecord], *, attempt: int = 1) -> str:
        task_lines = [
            f"- {task.task_id} [{task.status.value}] {self._compact_task_text(task.title or task.detail, 120)}"
            for task in open_tasks[:8]
        ]
        return "\n".join([
            "<system-reminder>",
            "Previous final response was discarded: TaskLedger still has unresolved work.",
            "Continue the task. Use tools to complete open tasks, or mark them blocked with evidence before replying.",
            f"Attempt: {attempt}",
            "Open tasks:",
            *task_lines,
            "</system-reminder>",
        ])

    def _task_guard_blocked_message(self, open_tasks: list[TaskRecord]) -> str:
        task_lines = [
            f"- {task.task_id}: {self._compact_task_text(task.title or task.detail, 120)}"
            for task in open_tasks[:8]
        ]
        return "\n".join([
            "仍有未完成任务，当前无法直接给出最终回复。",
            "需要先通过 TaskLedger 将任务标记为 completed、blocked 或 cancelled，并提供证据。",
            *task_lines,
        ])

    def _latest_user_content(self, conversation: Conversation) -> str:
        node = conversation.nodes.get(conversation.current_node_id or "")
        message = (node or {}).get("user_message") or {}
        content = message.get("content")
        return content if isinstance(content, str) else str(content or "")

    def _multi_agent_intent_text(
        self,
        conversation: Optional[Conversation],
        current_user_input: str = "",
    ) -> str:
        """Keep explicit delegation intent across short clarification turns."""
        current = current_user_input if isinstance(current_user_input, str) else str(current_user_input or "")
        parts = [current] if current.strip() else []
        if conversation is None:
            return "\n".join(parts)

        seen = {current}
        try:
            chain = self._model_node_chain(conversation, include_messages_to_keep=False)
        except Exception:
            return "\n".join(parts)

        for node in reversed(chain):
            message = (node or {}).get("user_message") or {}
            content = message.get("content")
            text = content if isinstance(content, str) else str(content or "")
            if not text.strip() or text in seen:
                continue
            lowered = text.lower()
            if any(token in lowered for token in MULTI_AGENT_REQUEST_TOKENS) and not any(
                token in lowered for token in MULTI_AGENT_REJECTION_TOKENS
            ):
                parts.append(text)
                seen.add(text)
            if len(parts) >= 6:
                break
        return "\n".join(parts)

    def _resolve_multi_agent_mode(
        self,
        user_input: str,
        conversation_metadata: Dict[str, Any],
    ) -> str:
        mode = self._normalize_multi_agent_mode(conversation_metadata.get("multi_agent_mode"))
        if mode in {"none", "proactive"}:
            return mode
        text = (user_input or "").lower()
        if any(token in text for token in MULTI_AGENT_REJECTION_TOKENS):
            return "none"
        return mode if any(token in text for token in MULTI_AGENT_REQUEST_TOKENS) else "none"

    @staticmethod
    def _normalize_multi_agent_mode(mode: Any) -> str:
        try:
            value = str(mode or "explicit_request_only")
        except Exception:
            value = "explicit_request_only"
        return value if value in {"none", "explicit_request_only", "proactive"} else "explicit_request_only"

    def _filter_agent_tools_for_mode(
        self,
        tools: List[Dict[str, Any]],
        mode: str,
    ) -> List[Dict[str, Any]]:
        filtered: List[Dict[str, Any]] = []
        for tool in tools:
            name = str((tool.get("function") or {}).get("name") or "")
            if name in LEGACY_AGENT_TOOL_NAMES:
                continue
            if name in AGENT_TOOL_NAMES and mode == "none":
                continue
            filtered.append(tool)
        return filtered

    def _filter_plan_tools_for_mode(
        self,
        tools: List[Dict[str, Any]],
        mode: str,
    ) -> List[Dict[str, Any]]:
        normalized_mode = normalize_permission_mode(mode)
        plan_only = {"update_plan", "exit_plan_mode", "ask_user_question"}
        filtered: List[Dict[str, Any]] = []
        for tool in tools:
            name = str((tool.get("function") or {}).get("name") or "")
            if name in plan_only and normalized_mode != "plan":
                continue
            if name == "enter_plan_mode" and normalized_mode == "plan":
                continue
            filtered.append(tool)
        return filtered

    def _runtime_context_details(self, conversation: Optional[Conversation]) -> list[str]:
        if conversation is None:
            return []
        metadata = conversation.metadata or {}
        prompt = metadata.get("selected_system_prompt") or {}
        workspace = normalize_workspace(
            metadata.get("workspace"),
            build_default_workspace(cfg.data if isinstance(cfg.data, dict) else None),
        )
        workspace_roots = workspace.get("workspace_roots") or []
        cwd = workspace.get("cwd") or ""
        mode = self._normalize_selected_system_prompt_mode(str(prompt.get("mode") or "override")) if prompt else "none"
        permission_mode = self._current_node_permission_mode(conversation)
        return [
            f"- Conversation id: {metadata.get('id') or ''}",
            f"- Current node id: {conversation.current_node_id or ''}",
            f"- Current tool permission mode: {permission_mode}",
            f"- Provider/model: {(metadata.get('provider_id') or conversation.current_provider or '')}/{(metadata.get('model_id') or conversation.current_model or '')}",
            f"- Workspace cwd: {cwd}",
            f"- Workspace roots: {', '.join(map(str, workspace_roots[:3])) if workspace_roots else 'none'}",
            f"- Selected system prompt mode: {mode}",
        ]

    def _current_node_permission_mode(self, conversation: Optional[Conversation]) -> str:
        if conversation is None:
            return "default"
        node = conversation.nodes.get(conversation.current_node_id or "") or {}
        return normalize_permission_mode(node.get("tool_permission_mode") or "default")

    @staticmethod
    def _normalize_selected_system_prompt_mode(mode: str) -> str:
        return mode if mode in {"override", "append"} else "override"

    def _model_node_chain(
        self,
        conversation: Conversation,
        *,
        include_messages_to_keep: bool = True,
    ) -> List[Dict[str, Any]]:
        """返回发给模型的节点链：root system + 最新 compact 后的有效上下文。"""
        chain = conversation.get_node_chain(conversation.current_node_id)
        if not chain:
            return []

        latest_boundary_index = None
        for index, node in enumerate(chain):
            if self._is_compact_boundary_node(node):
                latest_boundary_index = index

        if latest_boundary_index is None:
            return chain

        root = chain[0]
        compact_node = chain[latest_boundary_index]
        compact_meta = (compact_node.get("system_message") or {}).get("compact_metadata") or {}
        keep_count = max(int(compact_meta.get("messages_to_keep") or 0), 0) if include_messages_to_keep else 0
        kept_nodes = [
            node for node in chain[1:latest_boundary_index]
            if not self._is_compact_boundary_node(node)
        ][-keep_count:] if keep_count else []
        compact_tail = [compact_node, *kept_nodes, *chain[latest_boundary_index + 1:]]
        if root["id"] == compact_node["id"]:
            return compact_tail
        return [root, *compact_tail]

    def _resolve_model_for_conversation(
        self,
        conversation: Conversation,
        model_id: Optional[str] = None,
        provider_id: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        target_model = (
            model_id
            or conversation.metadata.get("model_id")
            or conversation.current_model
            or self._current_branch_model(conversation)
        )
        if not target_model:
            for _, models in self.model_manager.model_list.items():
                if models:
                    target_model = models[0]
                    break

        target_provider = (
            provider_id
            or conversation.metadata.get("provider_id")
            or conversation.current_provider
            or self._provider_for_model(target_model)
        )

        return target_model, target_provider

    def _rough_token_count_for_messages(self, messages: List[Dict[str, Any]]) -> int:
        total_chars = 0
        for message in messages:
            total_chars += len(str(message.get("content") or ""))
        return max(total_chars // 4, len(messages))

    def _restore_import_file_context(
        self,
        conversation_id: str,
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        restored = []
        for filename in extract_mentioned_import_filenames(messages):
            content = self.storage.read_import_file(conversation_id, filename)
            if content is None:
                continue
            truncated = len(content) > POST_COMPACT_MAX_CHARS_PER_FILE
            restored.append({
                "filename": filename,
                "content": content[:POST_COMPACT_MAX_CHARS_PER_FILE],
                "truncated": truncated,
            })
        return restored

    def _normalize_import_file_refs(
        self,
        import_files: Optional[List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        if not isinstance(import_files, list):
            return []
        normalized: List[Dict[str, Any]] = []
        seen = set()
        for file_ref in import_files:
            filename = file_ref.get("filename") if isinstance(file_ref, dict) else file_ref
            if not isinstance(filename, str):
                continue
            filename = filename.strip()
            if not filename or filename in seen:
                continue
            seen.add(filename)
            normalized.append({"filename": filename})
        return normalized

    def _normalize_image_refs(
        self,
        image_refs: Optional[List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        if not isinstance(image_refs, list):
            return []
        normalized: List[Dict[str, Any]] = []
        seen = set()
        for image_ref in image_refs:
            if not isinstance(image_ref, dict):
                continue
            filename = image_ref.get("filename")
            if not isinstance(filename, str):
                continue
            filename = filename.strip()
            if not filename or filename in seen:
                continue
            mime_type = image_ref.get("mime_type")
            if not isinstance(mime_type, str) or not mime_type.startswith("image/"):
                mime_type = "image/png"
            seen.add(filename)
            normalized.append({"filename": filename, "mime_type": mime_type})
        return normalized

    def _format_user_content_with_images(
        self,
        conversation_id: str,
        content: Any,
        image_refs: List[Dict[str, Any]],
    ) -> Any:
        if not image_refs:
            return content
        blocks: List[Dict[str, Any]] = []
        if isinstance(content, list):
            blocks.extend(content)
        elif str(content):
            blocks.append({"type": "text", "text": str(content)})
        for image_ref in image_refs:
            filename = image_ref.get("filename")
            mime_type = image_ref.get("mime_type") or "image/png"
            if not isinstance(filename, str):
                continue
            raw = self.storage.read_import_file_bytes(conversation_id, filename)
            if raw is None:
                blocks.append({"type": "text", "text": f"[Attached image `{filename}` is no longer available.]"})
                continue
            encoded = base64.b64encode(raw).decode("ascii")
            blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
            })
        return blocks or content

    def _import_reference_scan_messages(
        self,
        conversation: Conversation,
        *,
        include_messages_to_keep: bool = True,
    ) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        for node in self._model_node_chain(
            conversation,
            include_messages_to_keep=include_messages_to_keep,
        ):
            msg = node.get("user_message")
            if msg:
                messages.append(dict(msg))
        return messages

    def _format_import_file_context_message(
        self,
        conversation_id: str,
        import_files: List[Dict[str, Any]],
    ) -> Optional[Message]:
        parts = [
            "<system-reminder>",
            "The user attached the following file references. These may or may not be related to the current task.",
        ]
        added = 0
        for file_ref in import_files:
            filename = file_ref.get("filename") if isinstance(file_ref, dict) else None
            if not isinstance(filename, str) or not filename:
                continue
            content = self.storage.read_import_file(conversation_id, filename)
            if content is None:
                parts.append(f"\nUser attached file `{filename}`, but it is no longer available.")
                added += 1
                continue
            truncated = len(content) > POST_COMPACT_MAX_CHARS_PER_FILE
            visible_content = content[:POST_COMPACT_MAX_CHARS_PER_FILE]
            suffix = " (truncated)" if truncated else ""
            parts.append(
                f"\nUser attached file `{filename}`{suffix}:\n"
                f"<file name=\"{filename}\">\n{visible_content}\n</file>"
            )
            if truncated:
                parts.append(
                    "The file was truncated for model context. Ask the user or use available file tools if more content is needed."
                )
            added += 1
        if added == 0:
            return None
        parts.append("</system-reminder>")
        return Message({
            "id": str(uuid.uuid4()),
            "role": Role.SYSTEM,
            "content": "\n".join(parts),
            "timestamp": int(time()),
            "is_visible_in_transcript_only": True,
        })

    def _current_context_tokens(self, conversation: Conversation) -> int:
        current = conversation.nodes.get(conversation.current_node_id or "")
        usage = current.get("usage") if current else None
        if usage:
            active = usage.get("active_context_usage") or usage.get("branch_usage")
            if active and active.get("total_tokens"):
                return int(active.get("total_tokens") or 0)
        return self._rough_token_count_for_messages(self._prepare_messages_for_api_with_conversation(conversation))

    async def _auto_compact_if_needed(
        self,
        conversation_id: str,
        *,
        target_model: str,
        target_provider: str,
        model_context_window: Optional[int],
    ) -> Dict[str, Any]:
        if not model_context_window:
            return {"was_compacted": False}
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            return {"was_compacted": False}
        current_node = conversation.nodes.get(conversation.current_node_id or "")
        if current_node and self._is_compact_boundary_node(current_node):
            return {"was_compacted": False}

        token_usage = self._current_context_tokens(conversation)
        threshold = get_auto_compact_threshold(model_context_window)
        if token_usage < threshold:
            return {"was_compacted": False, "token_usage": token_usage, "threshold": threshold}

        result = await self.compact_conversation(
            conversation_id,
            model_id=target_model,
            provider_id=target_provider,
            trigger="auto",
        )
        return {
            "was_compacted": True,
            "token_usage": token_usage,
            "threshold": threshold,
            "result": result,
        }

    async def compact_conversation(
        self,
        conversation_id: str,
        custom_instructions: Optional[str] = None,
        model_id: Optional[str] = None,
        provider_id: Optional[str] = None,
        trigger: str = "manual",
        suppress_follow_up_questions: bool = True,
        messages_to_keep: int = 1,
    ) -> Dict[str, Any]:
        """手动执行 Claude Code 风格上下文压缩，并把结果追加为当前分支节点。"""
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            raise ValueError("对话不存在")

        target_model, target_provider = self._resolve_model_for_conversation(
            conversation,
            model_id=model_id,
            provider_id=provider_id,
        )
        if not target_model:
            raise ValueError("未指定模型ID")
        if not target_provider:
            raise ValueError(f"无法找到模型 {target_model} 对应的提供商")

        provider = self.model_manager.get_model(target_provider, False)
        if not provider:
            raise ValueError(f"无法初始化提供商 {target_provider}")

        messages_to_summarize = self._prepare_messages_for_api_with_conversation(
            conversation,
            include_messages_to_keep=False,
        )
        summary_request = {
            "role": "user",
            "content": get_compact_prompt(custom_instructions),
        }
        compact_messages = [*messages_to_summarize, summary_request]
        restored_files = self._restore_import_file_context(
            conversation_id,
            self._import_reference_scan_messages(conversation, include_messages_to_keep=False),
        )
        summary, tokens_used = provider.generate_response(
            target_model,
            compact_messages,
            max_tokens=COMPACT_MAX_OUTPUT_TOKENS,
            temperature=0,
            tools=None,
            tool_choice=None,
        )
        pre_tokens = self._rough_token_count_for_messages(messages_to_summarize)

        async with self._lock_for(conversation_id):
            latest = self.get_conversation(conversation_id)
            if latest is None:
                raise ValueError("对话不存在")
            parent_id = latest.current_node_id
            compact_node = NodeManager.create_compact_node(
                parent_id=parent_id,
                summary=summary,
                trigger="auto" if trigger == "auto" else "manual",
                pre_tokens=pre_tokens,
                model_id=target_model,
                last_pre_compact_message_id=parent_id,
                messages_to_keep=messages_to_keep,
                restored_files=restored_files,
                suppress_follow_up_questions=suppress_follow_up_questions,
            )
            compact_node["usage"] = self._node_usage_snapshot(
                turn_usage=estimated_usage(tokens_used),
                branch_usage=estimated_usage(0),
                model_context_window=self._model_context_window(target_provider, target_model),
            )
            latest.add_node(compact_node, parent_id=parent_id)
            self._mark_conversation_updated_at(latest, int(time()))
            self._save(latest)

        return {
            "conversation_id": conversation_id,
            "node_id": compact_node["id"],
            "pre_tokens": pre_tokens,
            "tokens_used": tokens_used,
            "trigger": compact_node["system_message"]["compact_metadata"]["trigger"],
        }

    def _prepare_messages_for_api_with_conversation(
        self,
        conversation: Conversation,
        *,
        include_messages_to_keep: bool = True,
    ) -> List[Message]:
        """准备API调用的消息列表（使用指定的 conversation）。

        工具轮次接缝：除 role/content 外，**存在即保留** tool_calls / tool_call_id /
        name，使未来的工具调用消息能完整流到 provider，而当前纯文本路径形状不变。
        """
        msg_dict = []

        def append_message(msg: Optional[Message]):
            if not msg:
                return
            if msg.get("subtype") == "compact_boundary":
                return
            role = msg["role"]
            if not isinstance(role, str) or role.startswith("Role."):
                role = role.value if hasattr(role, 'value') else str(role).split(".")[-1].lower()
            if role == "notify":
                role = "user"
            content = msg.get("content") or ""
            if role == "tool" and msg.get("model_visible_content") is not None:
                content = str(msg.get("model_visible_content") or "")
            if role == "user" and msg.get("image_refs"):
                content = self._format_user_content_with_images(
                    conversation.metadata["id"],
                    content,
                    msg.get("image_refs") or [],
                )
            out: Dict[str, Any] = {
                "role": role,
                "content": content,
            }
            if msg.get("tool_calls"):
                out["tool_calls"] = msg["tool_calls"]
            if msg.get("tool_call_id"):
                out["tool_call_id"] = msg["tool_call_id"]
            if msg.get("name"):
                out["name"] = msg["name"]
            msg_dict.append(out)

        node_chain = self._model_node_chain(
            conversation,
            include_messages_to_keep=include_messages_to_keep,
        )
        for node in node_chain:
            append_message(node.get("system_message"))
            user_message = node.get("user_message")
            append_message(user_message)
            if user_message:
                import_files = user_message.get("import_files") or []
                if import_files:
                    append_message(
                        self._format_import_file_context_message(
                            conversation.metadata["id"],
                            import_files,
                        )
                    )
            if self._is_compact_boundary_node(node):
                restored_files = ((node.get("system_message") or {}).get("compact_metadata") or {}).get("restored_files") or []
                if restored_files:
                    append_message(Message({
                        "id": f"{node['id']}:restored_files",
                        "role": Role.SYSTEM,
                        "content": format_restored_file_context(restored_files),
                        "timestamp": int(node.get("timestamp") or time()),
                    }))
            assistant = node.get("assistant_message")
            if assistant and assistant.get("tool_interactions"):
                for interaction in assistant.get("tool_interactions") or []:
                    interaction_assistant = interaction.get("assistant")
                    append_message(interaction_assistant)
                    paired_tools = self._paired_tool_messages_for_context(
                        interaction_assistant,
                        interaction.get("tools") or [],
                    )
                    for tool_msg in self._apply_round_tool_result_budget(paired_tools):
                        append_message(tool_msg)
                final_assistant = dict(assistant)
                final_assistant.pop("tool_calls", None)
                final_assistant.pop("tool_results", None)
                final_assistant.pop("tool_interactions", None)
                if final_assistant.get("content") or final_assistant.get("reasoning"):
                    append_message(final_assistant)
            else:
                append_message(assistant)
                if assistant and assistant.get("tool_calls"):
                    paired_tools = self._paired_tool_messages_for_context(
                        assistant,
                        node.get("tool_messages", []),
                    )
                    for tool_msg in self._apply_round_tool_result_budget(paired_tools):
                        append_message(tool_msg)

        return microcompact_messages(msg_dict)

    def _paired_tool_messages_for_context(
        self,
        assistant_message: Optional[Message],
        tool_messages: List[Message],
    ) -> List[Message]:
        if not assistant_message:
            return []
        tool_calls = assistant_message.get("tool_calls") or []
        if not tool_calls:
            return []

        by_call_id: Dict[str, Message] = {}
        for message in tool_messages:
            call_id = message.get("tool_call_id")
            if call_id and call_id not in by_call_id:
                by_call_id[call_id] = message

        paired: List[Message] = []
        for call in tool_calls:
            call_id = call.get("id")
            if not call_id:
                continue
            message = by_call_id.get(call_id)
            if message:
                paired.append(Message(dict(message)))
                continue
            fn = call.get("function") or {}
            paired.append(
                Message({
                    "id": str(uuid.uuid4()),
                    "role": Role.TOOL,
                    "content": f"Tool result missing for tool_call_id {call_id}.",
                    "name": fn.get("name") or "",
                    "tool_call_id": call_id,
                    "timestamp": int(time()),
                })
            )
        return paired

    def _merge_tool_call_lists(
        self,
        current: List[Dict[str, Any]],
        incoming: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        by_id = {call.get("id") or str(index): call for index, call in enumerate(current)}
        for index, call in enumerate(incoming):
            key = call.get("id") or str(index)
            by_id[key] = call
        return list(by_id.values())

    def _tool_result_preview_chars(self) -> int:
        tools_config = cfg.data.get("tools", {}) if isinstance(cfg.data, dict) else {}
        return int(tools_config.get("max_result_length", 8000))

    def _round_tool_result_budget_chars(self) -> int:
        tools_config = cfg.data.get("tools", {}) if isinstance(cfg.data, dict) else {}
        default_budget = self._tool_result_preview_chars() * 4
        return int(tools_config.get("max_round_result_length", default_budget))

    def _read_tool_result_hint(self, tool_result_id: str, offset: int = 0) -> str:
        args = json.dumps(
            {"tool_result_id": tool_result_id, "offset": offset},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return f"read_tool_result({args})"

    def _parse_command_tool_result(self, raw_result: str) -> Optional[Dict[str, Any]]:
        try:
            parsed = json.loads(raw_result)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        command_keys = {"command", "cwd", "exit_code", "stdout", "stderr", "timed_out"}
        if not command_keys.issubset(parsed.keys()):
            return None
        return parsed

    def _format_command_tool_result(
        self,
        *,
        raw_result: str,
        tool_result_id: str,
    ) -> str:
        parsed = self._parse_command_tool_result(raw_result)
        if parsed is None:
            return raw_result
        timed_out = parsed.get("timed_out")
        if isinstance(timed_out, bool):
            timed_out_text = str(timed_out).lower()
        else:
            timed_out_text = str(timed_out)
        stdout = str(parsed.get("stdout") or "")
        stderr = str(parsed.get("stderr") or "")
        return "\n".join(
            [
                f"Command: {parsed.get('command', '')}",
                f"Cwd: {parsed.get('cwd', '')}",
                f"Exit code: {parsed.get('exit_code', '')}",
                f"Timed out: {timed_out_text}",
                f"tool_result_id: {tool_result_id}",
                f"read_more: {self._read_tool_result_hint(tool_result_id, 0)}",
                "",
                "Stdout:",
                stdout if stdout else "(empty)",
                "",
                "Stderr:",
                stderr if stderr else "(empty)",
            ]
        )

    def _format_persisted_tool_result(
        self,
        *,
        raw_result: str,
        name: str,
        tool_result_id: str,
    ) -> str:
        command_result = self._parse_command_tool_result(raw_result)
        if command_result is not None:
            return self._format_command_tool_result(
                raw_result=raw_result,
                tool_result_id=tool_result_id,
            )

        preview_chars = self._tool_result_preview_chars()
        preview = raw_result[:preview_chars]
        has_more = len(raw_result) > len(preview)
        payload: Dict[str, Any] = {
            "tool_result_id": tool_result_id,
            "total_chars": len(raw_result),
            "truncated": has_more,
            "preview": preview,
        }
        if has_more:
            payload["read_more"] = self._read_tool_result_hint(tool_result_id, len(preview))
        return json.dumps(payload, ensure_ascii=False)

    def _persist_model_visible_tool_result(
        self,
        *,
        raw_result: str,
        name: str,
        conversation_id: str,
        node_id: str,
        tool_call_id: Optional[str],
    ) -> Dict[str, Optional[str]]:
        store = getattr(self.tool_manager, "tool_result_store", None)
        if store is None:
            return {"content": raw_result, "tool_result_id": None}

        record = store.save_result(
            content=raw_result,
            tool_name=name,
            conversation_id=conversation_id,
            node_id=node_id,
            tool_call_id=tool_call_id,
        )
        tool_result_id = str(record["id"])
        return {
            "content": self._format_persisted_tool_result(
                raw_result=raw_result,
                name=name,
                tool_result_id=tool_result_id,
            ),
            "tool_result_id": tool_result_id,
        }

    def _build_model_visible_tool_result(
        self,
        *,
        raw_result: str,
        name: str,
        conversation_id: str,
        node_id: str,
        tool_call_id: Optional[str],
    ) -> str:
        return str(
            self._persist_model_visible_tool_result(
                raw_result=raw_result,
                name=name,
                conversation_id=conversation_id,
                node_id=node_id,
                tool_call_id=tool_call_id,
            )["content"]
        )

    def _summarize_persisted_tool_result(self, message: Message) -> str:
        tool_result_id = message.get("tool_result_id")
        if not tool_result_id:
            return str(message.get("content") or "")
        return "\n".join([
            f"persisted: {tool_result_id}",
            f"read_more: {self._read_tool_result_hint(str(tool_result_id), 0)}",
        ])

    def _apply_round_tool_result_budget(self, tool_messages: List[Message]) -> List[Message]:
        budget = self._round_tool_result_budget_chars()
        if budget <= 0:
            return [Message(dict(message)) for message in tool_messages]

        out = [Message(dict(message)) for message in tool_messages]

        def visible_len(message: Message) -> int:
            return len(str(message.get("model_visible_content") or message.get("content") or ""))

        total = sum(visible_len(message) for message in out)
        while total > budget:
            candidates = [
                (visible_len(message), index)
                for index, message in enumerate(out)
                if (
                    message.get("tool_result_id")
                    and not message.get("_round_budget_shortened")
                    and len(self._summarize_persisted_tool_result(message)) < visible_len(message)
                )
            ]
            if not candidates:
                break
            _, index = max(candidates)
            if "raw_content" not in out[index]:
                out[index]["raw_content"] = str(
                    out[index].get("model_visible_content") or out[index].get("content") or ""
                )
            shortened = self._summarize_persisted_tool_result(out[index])
            out[index]["content"] = shortened
            out[index]["model_visible_content"] = shortened
            out[index]["_round_budget_shortened"] = True
            new_total = sum(visible_len(message) for message in out)
            if new_total >= total:
                break
            total = new_total
        for message in out:
            message.pop("_round_budget_shortened", None)
        return out

    def _tool_event_stream_chunk(
        self,
        event: Dict[str, Any],
        *,
        node_id: str,
        conversation_id: str,
    ) -> StreamChunk:
        return StreamChunk(
            status=StreamStatus.CONTENT,
            content=None,
            node_id=node_id,
            conversation_id=conversation_id,
            error=None,
            tokens_used=0,
            event_type=event.get("event_type"),
            approval=event.get("approval"),
        )

    def _model_visible_tool_message(
        self,
        message: Message,
        *,
        name: str,
        conversation_id: Optional[str],
        node_id: str,
        tool_call_id: Optional[str],
    ) -> Message:
        result = str(message.get("content") or "")
        raw_result = result
        tool_result_id = None
        if conversation_id and name != "read_tool_result":
            persisted = self._persist_model_visible_tool_result(
                raw_result=result,
                name=name,
                conversation_id=conversation_id,
                node_id=node_id,
                tool_call_id=tool_call_id,
            )
            result = str(persisted["content"] or "")
            tool_result_id = persisted.get("tool_result_id")
        out = Message(dict(message))
        out["content"] = result
        out["raw_content"] = raw_result
        out["model_visible_content"] = result
        if tool_result_id:
            out["tool_result_id"] = tool_result_id
        out["name"] = out.get("name") or name
        out["tool_call_id"] = out.get("tool_call_id") or tool_call_id
        out["node_id"] = node_id
        if not out.get("id"):
            out["id"] = str(uuid.uuid4())
        if not out.get("timestamp"):
            out["timestamp"] = int(time())
        return out

    async def _execute_tool_calls(
        self,
        tool_calls: List[Dict[str, Any]],
        node_id: str,
        conversation_id: Optional[str] = None,
        emit_event: Optional[Callable[[Dict[str, Any]], Any]] = None,
        workspace: Optional[Dict[str, Any]] = None,
        permission_mode: PermissionMode = "default",
        run_context: Optional[Dict[str, Any]] = None,
    ) -> List[Message]:
        results: List[Message] = []
        current_permission_mode = permission_mode
        batch: List[Dict[str, Any]] = []

        async def flush_read_only_batch() -> None:
            nonlocal batch, current_permission_mode
            if not batch:
                return
            messages = await asyncio.gather(*[
                self._execute_single_tool_call(
                    tool_call,
                    node_id=node_id,
                    conversation_id=conversation_id,
                    emit_event=emit_event,
                    workspace=workspace,
                    permission_mode=current_permission_mode,
                    run_context=run_context,
                )
                for tool_call in batch
            ])
            for message in messages:
                results.append(message)
                current_permission_mode = self._permission_mode_after_plan_tools(
                    [message],
                    current_permission_mode,
                )
            batch = []

        for tool_call in tool_calls:
            name = _tool_call_function_name(tool_call)
            if _is_parallel_read_only_tool(name):
                batch.append(tool_call)
                continue

            await flush_read_only_batch()
            model_message = await self._execute_single_tool_call(
                tool_call,
                node_id=node_id,
                conversation_id=conversation_id,
                emit_event=emit_event,
                workspace=workspace,
                permission_mode=current_permission_mode,
                run_context=run_context,
            )
            results.append(model_message)
            current_permission_mode = self._permission_mode_after_plan_tools(
                [model_message],
                current_permission_mode,
            )

        await flush_read_only_batch()
        return results

    async def _execute_single_tool_call(
        self,
        tool_call: Dict[str, Any],
        *,
        node_id: str,
        conversation_id: Optional[str] = None,
        emit_event: Optional[Callable[[Dict[str, Any]], Any]] = None,
        workspace: Optional[Dict[str, Any]] = None,
        permission_mode: PermissionMode = "default",
        run_context: Optional[Dict[str, Any]] = None,
    ) -> Message:
        fn = tool_call.get("function") or {}
        name = str(fn.get("name") or "")
        arguments = self._parse_tool_arguments(fn.get("arguments"))
        tool_orchestrator = getattr(self, "tool_orchestrator", None)
        call_run_context = dict(run_context or {})
        call_run_context["tool_call_id"] = tool_call.get("id")
        if tool_orchestrator:
            try:
                message = await tool_orchestrator.execute_tool_call(
                    tool_call,
                    conversation_id or "",
                    node_id,
                    emit_event=emit_event,
                    workspace=workspace,
                    permission_mode=permission_mode,
                    run_context=call_run_context,
                )
            except TypeError as exc:
                error_text = str(exc)
                if "unexpected keyword argument 'run_context'" in error_text:
                    message = await tool_orchestrator.execute_tool_call(
                        tool_call,
                        conversation_id or "",
                        node_id,
                        emit_event=emit_event,
                        workspace=workspace,
                        permission_mode=permission_mode,
                    )
                elif "unexpected keyword argument 'permission_mode'" in error_text:
                    message = await tool_orchestrator.execute_tool_call(
                        tool_call,
                        conversation_id or "",
                        node_id,
                        emit_event=emit_event,
                        workspace=workspace,
                    )
                elif "unexpected keyword argument 'workspace'" in error_text:
                    message = await tool_orchestrator.execute_tool_call(
                        tool_call,
                        conversation_id or "",
                        node_id,
                        emit_event=emit_event,
                    )
                else:
                    raise
            return self._model_visible_tool_message(
                message,
                name=name,
                conversation_id=conversation_id,
                node_id=node_id,
                tool_call_id=tool_call.get("id"),
            )

        if not self.tool_manager:
            raw_result = json.dumps({"error": "Tool manager is not configured"}, ensure_ascii=False)
        else:
            try:
                raw_result = await self.tool_manager.execute_tool(
                    name,
                    arguments,
                    workspace=workspace,
                    runtime_context=call_run_context,
                )
            except TypeError as exc:
                if "unexpected keyword argument 'workspace'" not in str(exc):
                    raise
                raw_result = await self.tool_manager.execute_tool(name, arguments)
        return self._model_visible_tool_message(
            Message({
                "id": str(uuid.uuid4()),
                "role": Role.TOOL,
                "content": raw_result,
                "name": name,
                "tool_calls": None,
                "tool_call_id": tool_call.get("id"),
                "node_id": node_id,
                "timestamp": int(time()),
            }),
            name=name,
            conversation_id=conversation_id,
            node_id=node_id,
            tool_call_id=tool_call.get("id"),
        )

    def _parse_tool_arguments(self, raw_arguments: Any) -> Dict[str, Any]:
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if raw_arguments is None or raw_arguments == "":
            return {}
        if isinstance(raw_arguments, str):
            try:
                parsed = json.loads(raw_arguments)
                return parsed if isinstance(parsed, dict) else {"value": parsed}
            except json.JSONDecodeError:
                return {"arguments": raw_arguments}
        return {"arguments": raw_arguments}

    def _format_messages_for_api(self, messages: List[Message]) -> List[Message]:
        """兼容旧调试脚本：消息在当前实现中已是 provider 可接收格式。"""
        return messages

    def _update_token_stats_for_conversation(self, conversation: Conversation, provider: str, tokens: int):
        """更新token统计（使用指定的 conversation）"""
        if provider not in conversation.metadata["total_tokens"]:
            conversation.metadata["total_tokens"][provider] = 0
        conversation.metadata["total_tokens"][provider] += tokens

    def _usage_from_message(self, msg: Optional[Message]):
        if not msg:
            return None
        generation_info = msg.get("generation_info") or {}
        usage_info = generation_info.get("usage_info")
        if usage_info:
            return usage_info
        tokens = generation_info.get("tokens_used")
        if tokens:
            return estimated_usage(tokens)
        return None

    def _node_usage_snapshot(
        self,
        *,
        turn_usage,
        branch_usage,
        model_context_window: Optional[int] = None,
    ) -> Dict[str, Any]:
        return {
            "turn_usage": turn_usage or estimated_usage(0),
            "branch_usage": branch_usage or estimated_usage(0),
            "active_context_usage": branch_usage or estimated_usage(0),
            "model_context_window": model_context_window,
        }

    def _model_context_window(self, provider_id: Optional[str], model_id: Optional[str]) -> Optional[int]:
        if not provider_id or not model_id or not hasattr(self.model_manager, "get_model_metadata"):
            return None
        try:
            meta = self.model_manager.get_model_metadata(provider_id, model_id)
        except Exception:
            return None
        return meta.get("context_length") if isinstance(meta, dict) else None

    def _branch_usage_for_node(self, conversation: Conversation, node_id: str):
        usage = None
        for node in conversation.get_node_chain(node_id):
            usage = add_usage(usage, self._usage_from_message(node.get("assistant_message")))
        return usage or estimated_usage(0)

    def _update_branch_usage_for_node(
        self,
        conversation: Conversation,
        node_id: str,
        model_context_window: Optional[int] = None,
    ):
        node = conversation.nodes.get(node_id)
        if not node:
            return
        turn_usage = self._usage_from_message(node.get("assistant_message")) or estimated_usage(0)
        branch_usage = self._branch_usage_for_node(conversation, node_id)
        node["branch_usage_info"] = branch_usage
        node["total_tokens"] = usage_total(branch_usage)
        node["usage"] = self._node_usage_snapshot(
            turn_usage=turn_usage,
            branch_usage=branch_usage,
            model_context_window=model_context_window,
        )

    def get_conversation_history(self) -> List[Message]:
        """获取对话历史"""
        if self.current_conversation:
            return self.current_conversation.get_message_chain_from_node(self.current_conversation.current_node_id)
        return []
