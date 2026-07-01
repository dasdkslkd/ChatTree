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

logger = setup_logger('ChatManager')

class ChatManager:
    """延迟加载模型的聊天管理器"""
    
    def __init__(self, model_manager: ModelManager, storage: ChatStorage, prompts: PromptStorage, tool_manager=None):
        self.model_manager = model_manager
        self.storage = storage
        self.prompts = prompts
        self.tool_manager = tool_manager
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
    
    def create_conversation(
        self,
        title: str = '',
        prompt_id: Optional[str] = None,
        prompt_mode: str = "override",
        workspace: Optional[Dict[str, Any]] = None,
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
            return {
                "deleted_node_id": node_id,
                "new_current_node_id": conversation.current_node_id,
                "parent_node_id": parent_id,
            }

    def _save(self, conversation: Conversation):
        """保存一个 Conversation 并清空其待删集合。"""
        self.storage.save(conversation.to_dict())
        conversation._deleted_node_ids.clear()

    def _mark_conversation_updated_at(self, conversation: Conversation, updated_at: int):
        conversation.metadata["updated_at"] = max(
            int(conversation.metadata.get("updated_at") or 0),
            updated_at,
        )

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
        built_messages = PromptBuilder(self.capability_registry).build(
            PromptBuildRequest(
                base_messages=base_messages,
                active_skill_names=skill_names,
                runtime_context=self._runtime_prompt_context("main", conversation),
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
        node_id: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        thinking_enabled: Optional[bool] = None,
        import_files: Optional[List[Dict[str, Any]]] = None,
        image_refs: Optional[List[Dict[str, Any]]] = None,
        tool_permission_mode: Optional[str] = None,
        run_id: Optional[str] = None,
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

        if not node_id:
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
            if node_id and node_id in side_run_context.nodes:
                side_run_context.switch_to_node(node_id)
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
        requested_parent_node_id = node_id or preview.current_node_id
        if not node_id and requested_parent_node_id in self._active_controllers:
            active_parent = preview.nodes.get(requested_parent_node_id, {}).get("parent_id")
            if active_parent:
                requested_parent_node_id = active_parent

        # 创建用户消息
        user_msg = Message({
            "id": str(uuid.uuid4()),
            "role": Role.USER,
            "content": model_content,
            "name": None,
            "tool_calls": None,
            "tool_call_id": None,
            "timestamp": int(time())
        })
        if slash_result.kind == SlashDispatchKind.MAIN_PROMPT:
            user_msg["slash_command"] = self._slash_command_metadata(slash_result)
        normalized_import_files = self._normalize_import_file_refs(import_files)
        if normalized_import_files:
            user_msg["import_files"] = normalized_import_files
        normalized_image_refs = self._normalize_image_refs(image_refs)
        if normalized_image_refs:
            user_msg["image_refs"] = normalized_image_refs

        skill_names: list[str] = []
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
            if node_id:
                conversation.switch_to_node(node_id)
            elif requested_parent_node_id and requested_parent_node_id in conversation.nodes:
                parent_id = requested_parent_node_id
                if parent_id in self._active_controllers:
                    parent_id = conversation.nodes.get(parent_id, {}).get("parent_id") or parent_id
                conversation.switch_to_node(parent_id)
            current_node_id = conversation.current_node_id
            parent_tool_permission_mode = None
            if current_node_id and current_node_id in conversation.nodes:
                parent_tool_permission_mode = conversation.nodes[current_node_id].get("tool_permission_mode")
            eff_tool_permission_mode = normalize_permission_mode(
                tool_permission_mode
                if tool_permission_mode not in (None, "")
                else parent_tool_permission_mode or "ask_always"
            )
            if self.capability_registry is not None:
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
            conversation.add_node(new_node, parent_id=current_node_id)
            self._set_conversation_model_metadata(
                conversation,
                provider_id=target_provider,
                model_id=target_model,
            )
            self._update_branch_usage_for_node(
                conversation,
                new_node["id"],
                model_context_window=meta.get("context_length"),
            )
            self._save(conversation)

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
            target_node_id=new_node["id"],
            conversation_id=conversation_id,
            run_id=run_id,
            tokens_used=0,
        )

        # 准备消息链（使用锁内加载的最新 conversation）
        messages = self._build_prompt_messages(conversation, skill_names)

        workspace_context = normalize_workspace(
            preview.metadata.get("workspace"),
            build_default_workspace(cfg.data if isinstance(cfg.data, dict) else None),
        )

        tools = self.tool_manager.get_openai_tools() if self.tool_manager else []
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

        try:
            all_tool_calls: List[Dict[str, Any]] = []
            all_tool_messages: List[Message] = []
            tool_interactions: List[Dict[str, Any]] = []
            all_approval_events: List[Dict[str, Any]] = []
            tool_round = 0

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
                    final_content = round_content
                    final_reasoning = round_reasoning
                    if complete_chunk:
                        complete_chunk["conversation_id"] = conversation_id
                        complete_chunk["run_id"] = run_id
                        complete_chunk["target_node_id"] = new_node["id"]
                        yield complete_chunk
                    break

                if not self.tool_manager:
                    logger.warning("Model requested tools but no ToolManager is configured")
                    final_content = round_content
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

                tool_round += 1
                assistant_tool_message = {
                    "role": "assistant",
                    "content": round_content,
                    "tool_calls": round_tool_calls,
                }
                messages.append(assistant_tool_message)
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
                    )
                )
                event_get_task = asyncio.create_task(approval_events.get())
                try:
                    while True:
                        done, _ = await asyncio.wait(
                            {execute_task, event_get_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
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
                            break
                finally:
                    if not event_get_task.done():
                        event_get_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await event_get_task
                    if not execute_task.done():
                        execute_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await execute_task
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

            # 创建生成信息（tokens_used 来自流中捕获的最终值）
            generation_info: GenerationInfo = {
                "duration_ms": duration_ms,
                "status": generation_status,
                "error_message": error_message,
                "tokens_used": tokens_used,
                "usage_info": usage_info
            }

            # 助手消息（包含生成信息）
            assistant_msg = Message({
                "id": str(uuid.uuid4()),
                "role": Role.ASSISTANT,
                "content": final_content if tool_interactions else total_content,
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
                    NodeManager.add_assistant_message(latest.nodes[new_node["id"]], assistant_msg)
                    if all_tool_messages:
                        NodeManager.add_tool_messages(latest.nodes[new_node["id"]], all_tool_messages)
                    self._set_conversation_model_metadata(
                        latest,
                        provider_id=target_provider,
                        model_id=target_model,
                    )
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
                    self._mark_conversation_updated_at(conversation, completion_timestamp)
                    self.storage.save({
                        "metadata": conversation.metadata,
                        "nodes": [new_node],
                        "current_node_id": conversation.current_node_id,
                        "root_node_id": conversation.root_node_id,
                    })

            # 清理控制器
            if new_node["id"] in self._active_controllers:
                del self._active_controllers[new_node["id"]]

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

    def _runtime_prompt_context(self, runtime: str, conversation: Optional[Conversation] = None) -> RuntimePromptContext:
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
        return RuntimePromptContext(
            name="main",
                content="\n".join([
                    "## Runtime Context",
                    "",
                    "Runtime mode: main chat",
                    *details,
                    "- This is the primary persisted conversation branch.",
                    "- Use tools only when they are provided for this call and follow the active permission mode.",
                    "- Preserve selected system prompt semantics: default core prompt, override custom prompt, or appended custom prompt.",
            ]),
            metadata={"runtime_mode": "main"},
        )

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
        return [
            f"- Conversation id: {metadata.get('id') or ''}",
            f"- Current node id: {conversation.current_node_id or ''}",
            f"- Provider/model: {(metadata.get('provider_id') or conversation.current_provider or '')}/{(metadata.get('model_id') or conversation.current_model or '')}",
            f"- Workspace cwd: {cwd}",
            f"- Workspace roots: {', '.join(map(str, workspace_roots[:3])) if workspace_roots else 'none'}",
            f"- Selected system prompt mode: {mode}",
        ]

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
    ) -> List[Message]:
        results: List[Message] = []
        for tool_call in tool_calls:
            fn = tool_call.get("function") or {}
            name = fn.get("name", "")
            arguments = self._parse_tool_arguments(fn.get("arguments"))
            tool_orchestrator = getattr(self, "tool_orchestrator", None)
            if tool_orchestrator:
                try:
                    message = await tool_orchestrator.execute_tool_call(
                        tool_call,
                        conversation_id or "",
                        node_id,
                        emit_event=emit_event,
                        workspace=workspace,
                        permission_mode=permission_mode,
                    )
                except TypeError as exc:
                    error_text = str(exc)
                    if "unexpected keyword argument 'permission_mode'" in error_text:
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
                results.append(
                    self._model_visible_tool_message(
                        message,
                        name=name,
                        conversation_id=conversation_id,
                        node_id=node_id,
                        tool_call_id=tool_call.get("id"),
                    )
                )
                continue

            if not self.tool_manager:
                raw_result = json.dumps({"error": "Tool manager is not configured"}, ensure_ascii=False)
            else:
                try:
                    raw_result = await self.tool_manager.execute_tool(name, arguments, workspace=workspace)
                except TypeError as exc:
                    if "unexpected keyword argument 'workspace'" not in str(exc):
                        raise
                    raw_result = await self.tool_manager.execute_tool(name, arguments)
            results.append(
                self._model_visible_tool_message(
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
            )
        return results

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
