# chat/chat_manager.py - 适配延迟加载
from typing import Any, Callable, List, Optional, Dict, AsyncIterator
import uuid
import asyncio
import json
import base64
from pathlib import Path
from copy import deepcopy
from contextlib import suppress
from inspect import isawaitable
from time import perf_counter, time
from .conversation import Conversation
from .node import NodeManager
from .canonical_reader import (
    BLOCKING_PLAN_TOOLS,
    _tool_result_payload,
    compact_metadata_by_node,
    has_blocking_plan_participation_result,
    latest_assistant_answer,
    messages_by_node as _canonical_messages_by_node,
    process_content_for_node,
    prune_summaries_by_node,
    tool_context_by_node,
    tool_history_by_node,
    turn_usage_for_node,
    usage_from_message,
)
from .compact import (
    COMPACT_MAX_OUTPUT_TOKENS,
    POST_COMPACT_MAX_CHARS_PER_FILE,
    extract_mentioned_import_filenames,
    format_restored_file_context,
    get_auto_compact_threshold,
    get_compact_user_summary_message,
    get_compact_prompt,
    format_compact_summary,
    microcompact_messages,
)
from .prune_summary import (
    PRUNE_BRANCH_DIGEST_MAX_OUTPUT_TOKENS,
    PRUNE_PACKET_BUDGET_CHARS,
    PRUNE_SUMMARY_MAX_OUTPUT_TOKENS,
    build_branch_digest_messages,
    build_prune_context_message,
    build_prune_packets,
    build_prune_summary_messages,
    create_prune_summary_record,
    json_dumps,
)
from .refer_context import (
    ReferContextError,
    build_refer_bundle,
    format_refer_context_message,
    parse_refer_prompt_args,
)
from .tool_result_format import (
    apply_round_tool_result_budget,
    build_model_visible_tool_result,
    persist_model_visible_tool_result,
    parse_command_tool_result,
)
from ..config.types import Message, Role, StreamChunk, StreamStatus, StreamController, GenerationInfo, SCHEMA_VERSION
from ..storage.chat_storage import ChatStorage
from ..storage.prompt_storage import PromptStorage
from ..model.model_manager import ModelManager
from ..model.usage import add_usage, estimated_usage, usage_total
from ..perf import get_profiler
from ..utils.logger import setup_logger
from ..config.config import cfg
from ..workspace import build_default_workspace, normalize_workspace
from ..projects import filter_capability_registry_for_workspace
from ..capabilities.prompting import (
    collect_skill_injection_names,
)
from ..prompts import PromptBuilder, PromptBuildRequest
from ..prompts.runtime_context import (
    agents_instruction_sections,
    format_task_turn_context_for_prompt,
    normalize_selected_system_prompt_mode,
    plan_mode_runtime_lines,
    runtime_context_details,
    runtime_prompt_context,
    selected_system_prompt,
)
from ..prompts.types import RuntimePromptContext
from ..slash import (
    SlashCommandDispatcher,
    SlashDispatchKind,
    SlashDispatchResult,
    SlashToolPolicy,
)
from ..tools.exposure import ToolExposureContext
from ..tools.perf_attrs import summarize_tool_arguments, summarize_tool_result
from ..tools.security.permissions import PermissionMode, normalize_permission_mode
from ..tools.tool_call_scheduler import plan_tool_call_waves, tool_call_function_name
from ..tools.agent_tools import AGENT_TOOL_NAMES, LEGACY_AGENT_TOOL_NAMES
from ..tools.task_tools import (
    TASK_BOUND_RUN_TOOL_NAMES,
    TASK_OBSERVATION_TOOL_NAMES,
    TASK_TOOL_NAMES,
    filter_task_tools_for_context,
)
from ..tasks import (
    TaskContextMode,
    TaskOutcome,
    TaskTurnContext,
    normalize_context_mode,
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


def should_emit_as_intermediate_text(*, has_tool_calls: bool, plan_guard_active: bool) -> bool:
    return has_tool_calls or plan_guard_active

def _configured_default_tool_permission_mode() -> PermissionMode:
    tools_config = cfg.data.get("tools", {}) if isinstance(cfg.data, dict) else {}
    configured = None
    if isinstance(tools_config, dict):
        configured = tools_config.get("default_permission_mode")
    return normalize_permission_mode(configured if configured not in (None, "") else "auto_approve")


def _estimate_stream_tokens(text: str) -> int:
    """Cheap output-token estimate for live throughput telemetry."""
    if not text:
        return 0
    cjk = sum(1 for char in text if "\u3400" <= char <= "\u9fff")
    non_space = sum(1 for char in text if not char.isspace())
    latin_like = max(0, non_space - cjk)
    estimate = cjk + int((latin_like + 3) / 4)
    return max(1, estimate)


def _usage_output_tokens(usage_info: Any) -> int:
    if not isinstance(usage_info, dict):
        return 0
    for key in ("output_tokens", "completion_tokens"):
        value = usage_info.get(key)
        try:
            if value is not None:
                return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


class ChatManager:
    """延迟加载模型的聊天管理器"""
    
    def __init__(
        self,
        model_manager: ModelManager,
        storage: ChatStorage,
        prompts: PromptStorage,
        tool_manager=None,
        task_service=None,
        plan_ledger=None,
        chat_repository=None,
    ):
        self.model_manager = model_manager
        self.storage = storage
        self.prompts = prompts
        self.tool_manager = tool_manager
        self.task_service = task_service
        self.plan_ledger = plan_ledger
        if chat_repository is None:
            from ..persistence.database import SQLitePersistence
            from ..persistence.repository import ChatRepository
            storage_dir = Path(getattr(storage, "storage_dir", "."))
            persistence = SQLitePersistence(storage_dir.parent)
            persistence.initialize()
            chat_repository = ChatRepository(persistence)
        self.chat_repository = chat_repository
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
        """从 canonical SQLite 加载一个独立的 Conversation。"""
        if getattr(self, "chat_repository", None) is None:
            return None
        try:
            row = self.chat_repository.get_conversation(conversation_id)
        except KeyError:
            return None
        return self._conversation_from_repository(row)

    def _conversation_from_repository(self, row: Dict[str, Any]) -> Conversation:
        conversation_id = str(row["id"])
        nodes = self.chat_repository.list_nodes(conversation_id) if self.chat_repository is not None else []
        workspace = row.get("workspace")
        if workspace is None and row.get("workspace_json"):
            workspace = _tool_result_payload(row.get("workspace_json"))
        data = {
            "metadata": {
                "id": conversation_id,
                "title": str(row.get("title") or ""),
                "created_at": int(row.get("created_at") or 0),
                "updated_at": int(row.get("updated_at") or 0),
                "total_tokens": {},
                "schema_version": SCHEMA_VERSION,
                "provider_id": row.get("provider_id"),
                "model_id": row.get("model_id"),
                "reasoning_effort": row.get("reasoning_effort"),
                "thinking_enabled": (
                    None
                    if row.get("thinking_enabled") is None
                    else bool(row.get("thinking_enabled"))
                ),
                "multi_agent_mode": row.get("multi_agent_mode") or "explicit_request_only",
                "workspace": workspace,
            },
            "nodes": nodes,
            "root_node_id": row.get("root_node_id"),
            "current_node_id": row.get("current_node_id"),
        }
        if data["metadata"]["workspace"] is None:
            data["metadata"].pop("workspace", None)
        return Conversation.from_dict(data)

    async def create_visible_user_anchor_node(
        self,
        *,
        conversation_id: str,
        content: str,
        parent_node_id: Optional[str] = None,
        model_id: Optional[str] = None,
        tool_permission_mode: Optional[str] = None,
        task_context_mode: Optional[str] = None,
        slash_metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Create a visible user-only node for detached slash runs."""
        async with self._lock_for(conversation_id):
            conversation = self.get_conversation(conversation_id)
            if conversation is None:
                raise ValueError("对话不存在")
            if not parent_node_id:
                raise ValueError("parent_node_id is required")
            if parent_node_id not in conversation.nodes:
                raise ValueError("父节点不存在")
            current_node_id = parent_node_id
            parent_tool_permission_mode = None
            if current_node_id and current_node_id in conversation.nodes:
                parent_tool_permission_mode = conversation.nodes[current_node_id].get("tool_permission_mode")
            eff_tool_permission_mode = normalize_permission_mode(
                tool_permission_mode
                if tool_permission_mode not in (None, "")
                else parent_tool_permission_mode or _configured_default_tool_permission_mode()
            )
            parent_task_context_mode = "attached"
            if current_node_id and current_node_id in conversation.nodes:
                parent_task_context_mode = str(
                    conversation.nodes[current_node_id].get("task_context_mode") or "attached"
                )
            eff_task_context_mode = normalize_context_mode(
                task_context_mode if task_context_mode not in (None, "") else parent_task_context_mode
            ).value
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
                parent_id=current_node_id,
                model_id=model_id or conversation.current_model,
                tool_permission_mode=eff_tool_permission_mode,
                task_context_mode=eff_task_context_mode,
            )
            conversation.add_node(new_node, parent_id=current_node_id)
            self.chat_repository.save(conversation)
            self.chat_repository.persist_user_turn(
                conversation=conversation,
                node=new_node,
                user_msg=user_msg,
                provider_id=conversation.metadata.get("provider_id"),
                model_id=model_id or conversation.current_model,
                run_id=None,
            )
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
                    "mode": normalize_selected_system_prompt_mode(prompt_mode),
                    "content": system_prompt,
                }

        # 直接持久化新对话（不依赖共享 current_conversation 做后续保存）
        self.chat_repository.save(conversation)
        self.current_conversation = conversation
        logger.info(f"对话创建成功 id: {conversation.metadata['id']}")
        return conversation
    
    def load_conversation(self, conversation_id: str) -> bool:
        """加载对话"""
        conversation = self.get_conversation(conversation_id)
        if conversation:
            self.current_conversation = conversation
            return True
        return False
    
    def save_conversation(self):
        """保存当前对话"""
        if self.current_conversation:
            self.chat_repository.save(self.current_conversation)
    
    def list_conversations(self) -> List[Dict[str, Any]]:
        """列出所有对话"""
        default_workspace = build_default_workspace(cfg.data if isinstance(cfg.data, dict) else None)
        conversations = self.chat_repository.list_conversations() if self.chat_repository is not None else []
        for item in conversations:
            item["workspace"] = normalize_workspace(item.get("workspace"), default_workspace)
            item["node_count"] = str(item.get("node_count", 0))
            if not item.get("model_id") or not item.get("provider_id"):
                loaded = self.get_conversation(item["id"])
                if loaded is not None:
                    model_id, provider_id = self._model_summary_for_conversation(loaded)
                    item["model_id"] = item.get("model_id") or model_id or ""
                    item["provider_id"] = item.get("provider_id") or provider_id or ""
        return conversations
    
    def delete_conversation(self, conversation_id: str):
        """删除对话"""
        if self.chat_repository is not None:
            with suppress(Exception):
                self.chat_repository.delete_conversation(conversation_id)
        if self.current_conversation and self.current_conversation.metadata["id"] == conversation_id:
            self.current_conversation = None
    
    async def update_conversation_title(self, conversation_id: str, title: str) -> bool:
        """更新对话标题（锁内 load-modify-save）"""
        async with self._lock_for(conversation_id):
            if self.chat_repository is None:
                return False
            ok = self.chat_repository.update_conversation(conversation_id, title=title)
            if ok and self.current_conversation and self.current_conversation.metadata["id"] == conversation_id:
                self.current_conversation.metadata["title"] = title
            return ok

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
            if self.chat_repository is None:
                return False
            ok = self.chat_repository.update_conversation(
                conversation_id,
                model_id=model_id,
                provider_id=provider_id,
                reasoning_effort=reasoning_effort,
                thinking_enabled=thinking_enabled,
            )
            if ok and self.current_conversation and self.current_conversation.metadata["id"] == conversation_id:
                self.current_conversation.metadata["model_id"] = model_id
                self.current_conversation.metadata["provider_id"] = provider_id
                self.current_conversation.metadata["reasoning_effort"] = reasoning_effort
                self.current_conversation.metadata["thinking_enabled"] = thinking_enabled
            return ok

    async def update_conversation_multi_agent_mode(
        self,
        conversation_id: str,
        multi_agent_mode: str,
    ) -> bool:
        """更新对话的 multi-agent 工具暴露策略（锁内 load-modify-save）。"""
        mode = self._normalize_multi_agent_mode(multi_agent_mode)
        async with self._lock_for(conversation_id):
            if self.chat_repository is None:
                return False
            ok = self.chat_repository.update_conversation(conversation_id, multi_agent_mode=mode)
            if ok and self.current_conversation and self.current_conversation.metadata["id"] == conversation_id:
                self.current_conversation.metadata["multi_agent_mode"] = mode
            return ok

    async def switch_node(self, conversation_id: str, node_id: str) -> Optional[str]:
        """切换对话当前节点（锁内 load-modify-save）；成功返回新的 current_node_id，失败返回 None。"""
        async with self._lock_for(conversation_id):
            conversation = self.get_conversation(conversation_id)
            if not conversation:
                return None
            if not conversation.switch_to_node(node_id):
                return None
            if self.chat_repository is not None:
                self.chat_repository.update_conversation(conversation_id, current_node_id=node_id)
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
            if self.chat_repository is not None:
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
        chain = conversation.get_node_chain(conversation.current_node_id)
        node_ids = [str(node.get("id")) for node in chain if node.get("id")]
        messages_by_node = _canonical_messages_by_node(self.chat_repository,conversation.metadata["id"], node_ids)
        for node in reversed(chain):
            checked += 1
            node_skill_names: list[str] = []
            for message in reversed(messages_by_node.get(str(node.get("id") or ""), [])):
                value = message.get("active_skill_names")
                if isinstance(value, list):
                    node_skill_names = [str(name) for name in value if name]
                    break
            for name in node_skill_names:
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
            if checked >= max_nodes:
                break
        return names

    def _scoped_capability_registry(self, conversation: Conversation):
        workspace = conversation.metadata.get("workspace") if conversation is not None else None
        return filter_capability_registry_for_workspace(
            self.capability_registry,
            cfg.data if isinstance(cfg.data, dict) else None,
            workspace if isinstance(workspace, dict) else None,
        )

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

    def _get_openai_tools_for_workspace(
        self,
        workspace_context: Dict[str, Any],
        exposure_context: Optional[ToolExposureContext] = None,
    ) -> List[Dict[str, Any]]:
        if not self.tool_manager:
            return []
        try:
            return self.tool_manager.get_openai_tools(
                workspace=workspace_context,
                exposure_context=exposure_context,
            )
        except TypeError as exc:
            if "workspace" not in str(exc) and "exposure_context" not in str(exc):
                raise
            return self.tool_manager.get_openai_tools()

    def _tool_exposure_context(
        self,
        *,
        slash_result: SlashDispatchResult,
        multi_agent_mode: str,
        permission_mode: str,
    ) -> ToolExposureContext:
        allowed_tools = slash_result.allowed_tools
        if slash_result.tool_policy == SlashToolPolicy.READ_ONLY and allowed_tools is None:
            allowed_tools = ("glob", "grep", "read", "web", "enter_plan_mode")
        disallowed_tools = tuple(slash_result.disallowed_tools or ())
        if multi_agent_mode == "none":
            disallowed_tools = (*disallowed_tools, "agent")
        return ToolExposureContext(
            run_kind=slash_result.run_kind or "chat",
            permission_mode=permission_mode,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
        )

    def _build_prompt_messages(
        self,
        conversation: Conversation,
        skill_names: List[str],
        *,
        task_turn_context: Optional[TaskTurnContext] = None,
    ) -> List[Message]:
        if task_turn_context is None:
            task_turn_context = self._start_task_turn_context(conversation)
        base_messages = self._prepare_messages_for_api_with_conversation(conversation)
        custom_prompt, custom_mode = selected_system_prompt(conversation)
        latest_user_content = self._latest_user_content(conversation)
        built_messages = PromptBuilder(self._scoped_capability_registry(conversation)).build(
            PromptBuildRequest(
                base_messages=base_messages,
                active_skill_names=skill_names,
                runtime_context=runtime_prompt_context(
                    "main",
                    conversation,
                    latest_user_content=latest_user_content,
                    task_turn_context=task_turn_context,
                    multi_agent_mode=self._resolve_multi_agent_mode(
                        self._multi_agent_intent_text(conversation, latest_user_content),
                        conversation.metadata if conversation is not None else {},
                    ),
                    permission_mode=self._current_node_permission_mode(conversation),
                    task_context_mode=self._current_node_task_context_mode(conversation),
                    plan_ledger=getattr(self, "plan_ledger", None),
                ),
                extra_sections=agents_instruction_sections(conversation),
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
        custom_prompt, custom_mode = selected_system_prompt(conversation)
        messages = [
            Message(message)
            for message in PromptBuilder(self._scoped_capability_registry(conversation)).build(
                PromptBuildRequest(
                    base_messages=base_messages,
                    active_skill_names=[],
                    include_available_capabilities=False,
                    runtime_context=runtime_prompt_context(
                        "side_question",
                        conversation,
                        permission_mode=self._current_node_permission_mode(conversation),
                        task_context_mode=self._current_node_task_context_mode(conversation),
                    ),
                    extra_sections=agents_instruction_sections(conversation),
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
                if chunk.get("status") == StreamStatus.START:
                    continue
                if chunk.get("status") == StreamStatus.COMPLETE and not chunk.get("tokens_used"):
                    chunk["tokens_used"] = tokens_used
                yield chunk
        finally:
            self._active_controllers.pop(controller_key, None)

    async def _resolve_stream_preload(
        self,
        *,
        conversation_id: str,
        content: str,
        model_id: Optional[str],
        provider_id: Optional[str],
        parent_node_id: Optional[str],
        reasoning_effort: Optional[str],
        thinking_enabled: Optional[bool],
        append_to_existing_node: bool,
        run_id: Optional[str],
    ) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        """预加载阶段：验证并解析模型/提供商/slash/refer/reasoning。
        返回 (context, None) 成功，或 (None, error_message) 失败。
        """
        profiler = get_profiler()
        # 预加载（只读）用于解析模型/提供商，不做任何修改或保存。
        # 真正的树修改在锁内重新加载最新快照，避免并发覆盖 root.children_ids。
        with profiler.span("chat.preload_conversation", conversation_id=conversation_id, run_id=run_id):
            preview = self.get_conversation(conversation_id)
        if preview is None:
            logger.error(f"对话 {conversation_id} 不存在")
            return None, "对话不存在"
        requested_parent_node_id = str(parent_node_id or "").strip()
        if not requested_parent_node_id:
            return None, "parent_node_id is required"
        if requested_parent_node_id not in preview.nodes:
            return None, "父节点不存在"
        preview.switch_to_node(requested_parent_node_id)
        # 确定模型：请求值 > 会话 metadata > 当前分支节点 > 第一个可用模型。
        target_model = model_id or preview.current_model or self._current_branch_model(preview)
        if not target_model:
            for provider, models in self.model_manager.model_list.items():
                if models:
                    target_model = models[0]
                    logger.info(f"使用默认模型: {target_model}")
                    break
        if not target_model:
            return None, "未指定模型ID"
        target_provider = (
            provider_id
            or preview.current_provider
            or self._provider_for_model(target_model)
        )
        logger.info(f"Stream: model={target_model}, provider={target_provider}, model_list_keys={list(self.model_manager.model_list.keys())}")
        if not target_provider:
            return None, f"无法找到模型 {target_model} 对应的提供商"

        slash_result = self._dispatch_slash_content(content)
        if slash_result.kind in {
            SlashDispatchKind.SUBAGENT,
            SlashDispatchKind.WORKFLOW,
            SlashDispatchKind.ERROR,
        }:
            return None, self._slash_runtime_error(slash_result)
        model_content = slash_result.model_input or content
        refer_bundle: Optional[Dict[str, Any]] = None
        if slash_result.kind == SlashDispatchKind.REFER_PROMPT:
            try:
                refer_args = parse_refer_prompt_args(slash_result.args)
                refer_node_ids = [str(node_id) for node_id in preview.nodes.keys()]
                prune_by_node = prune_summaries_by_node(self.chat_repository,
                    preview.metadata["id"],
                    refer_node_ids,
                )
                refer_bundle = build_refer_bundle(
                    preview,
                    refer_args["selectors"],
                    {
                        node_id: [dict(message) for message in messages]
                        for node_id, messages in _canonical_messages_by_node(self.chat_repository,
                            preview.metadata["id"],
                            refer_node_ids,
                        ).items()
                    },
                    tool_context_by_node(self.chat_repository,
                        preview.metadata["id"],
                        refer_node_ids,
                    ),
                    compact_metadata_by_node(self.chat_repository,
                        preview.metadata["id"],
                        refer_node_ids,
                    ),
                    {
                        str(summary.get("id")): summary
                        for summaries in prune_by_node.values()
                        for summary in summaries
                    },
                )
                refer_bundle["prompt"] = refer_args["prompt"]
                model_content = refer_args["prompt"]
            except ReferContextError as exc:
                return None, str(exc)

        provider = self.model_manager.get_model(target_provider, True)
        if not provider:
            logger.error(f"无法初始化提供商 {target_provider} (is_async=True)")
            return None, f"无法初始化提供商 {target_provider}"

        # 解析有效推理参数：请求传入 > 对话 metadata > 模型默认；再按模型元数据校验。
        # metadata 不支持的档位/开关会被规范化为 None（不发送），保护配错的第三方模型。
        from ..model.model_metadata import normalize_effort, normalize_thinking
        if hasattr(self.model_manager, "get_model_metadata"):
            meta = self.model_manager.get_model_metadata(target_provider, target_model)
        else:
            meta = {}

        if not append_to_existing_node:
            with profiler.span(
                "chat.auto_compact_check",
                conversation_id=conversation_id,
                run_id=run_id,
                provider_id=target_provider,
                model_id=target_model,
            ):
                auto_result = await self._auto_compact_if_needed(
                    conversation_id,
                    parent_node_id=requested_parent_node_id,
                    target_model=target_model,
                    target_provider=target_provider,
                    model_context_window=meta.get("context_length"),
                )
            if auto_result.get("was_compacted"):
                compact_node_id = str((auto_result.get("result") or {}).get("node_id") or "")
                if compact_node_id:
                    requested_parent_node_id = compact_node_id
                latest_preview = self.get_conversation(conversation_id)
                if latest_preview is not None:
                    preview = latest_preview
                    preview.switch_to_node(requested_parent_node_id)

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

        return {
            "preview": preview,
            "requested_parent_node_id": requested_parent_node_id,
            "target_model": target_model,
            "target_provider": target_provider,
            "slash_result": slash_result,
            "model_content": model_content,
            "refer_bundle": refer_bundle,
            "provider": provider,
            "meta": meta,
            "eff_effort": eff_effort,
            "eff_thinking": eff_thinking,
        }, None

    async def _create_turn_node(
        self,
        *,
        conversation_id: str,
        requested_parent_node_id: str,
        target_model: str,
        target_provider: str,
        meta: Dict[str, Any],
        model_content: str,
        user_msg: Optional[Message],
        focus_new_node: bool,
        append_to_existing_node: bool,
        requested_tool_permission_mode: Optional[str],
        active_plan_permission_mode: Optional[str],
        task_context_mode: Optional[str],
        hidden_user_message: bool,
        suppress_user_message: bool,
        run_id: Optional[str],
    ) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        """临界区 1（锁内）：重新加载最新快照 + 建节点 + 立即保存 user 消息。
        锁内重载确保看到其他并发流刚提交的兄弟节点，add_node 不会丢失 root 引用。
        立即落盘是为了让前端的 userMsgLanded 判定能尽快看到真实 user 消息。
        返回 (turn_state, None) 成功，或 (None, error_message) 失败。
        """
        profiler = get_profiler()
        with profiler.span("chat.create_turn_node", conversation_id=conversation_id, run_id=run_id):
            async with self._lock_for(conversation_id):
                conversation = self.get_conversation(conversation_id)
                if conversation is None:
                    return None, "对话不存在"
                if requested_parent_node_id not in conversation.nodes:
                    return None, "父节点不存在"
                current_node_id = requested_parent_node_id
                parent_tool_permission_mode = None
                parent_task_context_mode = TaskContextMode.ATTACHED.value
                if current_node_id and current_node_id in conversation.nodes:
                    parent_tool_permission_mode = conversation.nodes[current_node_id].get("tool_permission_mode")
                    parent_task_context_mode = str(
                        conversation.nodes[current_node_id].get("task_context_mode")
                        or TaskContextMode.ATTACHED.value
                    )
                if (
                    parent_tool_permission_mode == "plan"
                    and active_plan_permission_mode != "plan"
                ):
                    parent_tool_permission_mode = None
                eff_tool_permission_mode = normalize_permission_mode(
                    requested_tool_permission_mode
                    if requested_tool_permission_mode
                    else active_plan_permission_mode
                    or parent_tool_permission_mode
                    or _configured_default_tool_permission_mode()
                )
                try:
                    eff_task_context_mode = normalize_context_mode(
                        task_context_mode if task_context_mode not in (None, "") else parent_task_context_mode
                    ).value
                except ValueError:
                    return None, "task_context_mode must be attached or detached"
                skill_names: list[str] = []
                if (
                    self.capability_registry is not None
                    and not hidden_user_message
                    and not suppress_user_message
                ):
                    skill_names = collect_skill_injection_names(
                        model_content,
                        self._scoped_capability_registry(conversation),
                        active_skill_names=self._recent_active_skill_names(conversation),
                    )
                    if skill_names and user_msg is not None:
                        user_msg["active_skill_names"] = skill_names
                if append_to_existing_node:
                    new_node = conversation.nodes[current_node_id]
                    new_node["model_id"] = target_model
                    new_node["tool_permission_mode"] = eff_tool_permission_mode
                    new_node["task_context_mode"] = eff_task_context_mode
                    conversation.switch_to_node(current_node_id)
                else:
                    new_node = NodeManager.create_node(
                        parent_id=current_node_id,
                        model_id=target_model,
                        tool_permission_mode=eff_tool_permission_mode,
                        task_context_mode=eff_task_context_mode,
                    )
                    conversation.add_node(new_node, parent_id=current_node_id, focus=focus_new_node)
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
                self.chat_repository.save(conversation)
                if user_msg is not None:
                    self.chat_repository.persist_user_turn(
                        conversation=conversation,
                        node=new_node,
                        user_msg=user_msg,
                        provider_id=target_provider,
                        model_id=target_model,
                        run_id=run_id,
                    )
                return {
                    "conversation": conversation,
                    "new_node": new_node,
                    "current_node_id": current_node_id,
                    "skill_names": skill_names,
                }, None

    async def _finalize_assistant_turn(
        self,
        *,
        conversation_id: str,
        run_id: Optional[str],
        conversation: Conversation,
        new_node: Dict[str, Any],
        target_provider: str,
        target_model: str,
        meta: Dict[str, Any],
        assistant_message_id: str,
        start_time: float,
        tokens_used: int,
        usage_info: Optional[Dict[str, Any]],
        generation_status: str,
        error_message: Optional[str],
        total_content: str,
        total_reasoning: str,
        final_content: str,
        persisted_final_content: Optional[str],
        process_content_parts: list[str],
        process_parts: list[Dict[str, Any]],
        all_tool_calls: List[Dict[str, Any]],
        all_tool_messages: List[Message],
        all_approval_events: List[Dict[str, Any]],
        append_to_existing_node: bool,
    ) -> None:
        """临界区 2（锁内）+ 转写持久化：重新加载最新快照、挂助手消息、累加 token、保存。
        必须锁内重载：流式期间其他并发流可能已提交兄弟节点，直接保存临界区 1 的
        旧 conversation 会覆盖掉它们对 root.children_ids 的修改。
        必须在路由发送 [DONE] 之前完成（save-before-[DONE] 不变量）。
        """
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
        has_tool_rounds = bool(all_tool_calls or all_tool_messages)
        # 助手消息（包含生成信息）
        assistant_msg = Message({
            "id": assistant_message_id,
            "role": Role.ASSISTANT,
            "content": persisted_final_content if persisted_final_content is not None else (final_content if has_tool_rounds else total_content),
            "name": None,
            "tool_call_id": None,
            "process_content": "".join(process_content_parts) or None,
            "process_parts": process_parts or None,
            "reasoning": total_reasoning or None,
            "timestamp": completion_timestamp,
            "generation_info": generation_info
        })

        assistant_msg_for_transcript = assistant_msg
        persisted_tool_calls = all_tool_calls
        if append_to_existing_node:
            existing_answer = latest_assistant_answer(self.chat_repository, conversation_id, new_node["id"])
            if existing_answer is not None:
                assistant_msg_for_transcript = Message(deepcopy(assistant_msg))
                assistant_msg_for_transcript["id"] = str(existing_answer.get("id") or assistant_message_id)
                assistant_msg_for_transcript["content"] = (
                    str(existing_answer.get("content") or "")
                    + str(assistant_msg.get("content") or "")
                )
                if assistant_msg.get("reasoning"):
                    assistant_msg_for_transcript["reasoning"] = (
                        process_content_for_node(self.chat_repository,
                            conversation_id,
                            new_node["id"],
                            "assistant_process_reasoning",
                        )
                        + str(assistant_msg.get("reasoning") or "")
                    )
                if assistant_msg.get("process_content"):
                    assistant_msg_for_transcript["process_content"] = (
                        process_content_for_node(self.chat_repository,
                            conversation_id,
                            new_node["id"],
                            "assistant_process_content",
                        )
                        + str(assistant_msg.get("process_content") or "")
                    )
        async with self._lock_for(conversation_id):
            latest = self.get_conversation(conversation_id)
            if latest is not None and new_node["id"] in latest.nodes:
                latest_node = latest.nodes[new_node["id"]]
                latest_node["tool_permission_mode"] = new_node.get("tool_permission_mode")
                persisted_tool_messages = all_tool_messages
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
                latest.metadata["updated_at"] = max(
                    int(latest.metadata.get("updated_at") or 0),
                    completion_timestamp,
                )
                self.chat_repository.save(latest)
            else:
                # 极端情况：节点已被并发删除——退回到只保存本节点，避免丢消息
                persisted_tool_messages = all_tool_messages
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
                conversation.metadata["updated_at"] = max(
                    int(conversation.metadata.get("updated_at") or 0),
                    completion_timestamp,
                )
                self.chat_repository.save(conversation)

        try:
            latest_for_transcript = self.get_conversation(conversation_id)
            if latest_for_transcript is not None:
                self.chat_repository.persist_assistant_turn(
                    conversation=latest_for_transcript,
                    node=latest_for_transcript.nodes.get(new_node["id"], new_node),
                    assistant_msg=assistant_msg_for_transcript,
                    provider_id=target_provider,
                    model_id=target_model,
                    run_id=run_id,
                    tool_messages=persisted_tool_messages,
                    tool_calls=persisted_tool_calls,
                    approval_events=all_approval_events,
                    plan_participation_only=has_blocking_plan_participation_result(persisted_tool_messages),
                )
                async with self._lock_for(conversation_id):
                    latest_with_messages = self.get_conversation(conversation_id)
                    if latest_with_messages is not None and new_node["id"] in latest_with_messages.nodes:
                        self._update_branch_usage_for_node(
                            latest_with_messages,
                            new_node["id"],
                            model_context_window=meta.get("context_length"),
                        )
                        self.chat_repository.save(latest_with_messages)
        finally:
            if new_node["id"] in self._active_controllers:
                del self._active_controllers[new_node["id"]]

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
        task_context_mode: Optional[str] = None,
        hidden_user_message: bool = False,
        run_id: Optional[str] = None,
        continuation_messages: Optional[List[Message]] = None,
        suppress_user_message: bool = False,
        append_to_existing_node: bool = False,
    ) -> AsyncIterator[StreamChunk]:
        """
        异步流式发送消息
        前端可以：for chunk in stream: 实时更新UI
        """
        profiler = get_profiler()
        # ── 阶段 1：预加载（只读）── 解析模型/提供商/slash/refer/reasoning，不做修改。
        preload, error = await self._resolve_stream_preload(
            conversation_id=conversation_id,
            content=content,
            model_id=model_id,
            provider_id=provider_id,
            parent_node_id=parent_node_id,
            reasoning_effort=reasoning_effort,
            thinking_enabled=thinking_enabled,
            append_to_existing_node=append_to_existing_node,
            run_id=run_id,
        )
        if error is not None:
            yield StreamChunk(
                status=StreamStatus.ERROR,
                content="",
                node_id=None,
                conversation_id=conversation_id,
                run_id=run_id,
                error=error,
                tokens_used=0,
            )
            return
        preview = preload["preview"]
        requested_parent_node_id = preload["requested_parent_node_id"]
        target_model = preload["target_model"]
        target_provider = preload["target_provider"]
        slash_result = preload["slash_result"]
        model_content = preload["model_content"]
        refer_bundle = preload["refer_bundle"]
        provider = preload["provider"]
        meta = preload["meta"]
        eff_effort = preload["eff_effort"]
        eff_thinking = preload["eff_thinking"]

        if slash_result.kind == SlashDispatchKind.SIDE_QUESTION:
            side_run_context = Conversation.from_dict(preview.to_dict())
            side_run_context.switch_to_node(requested_parent_node_id)
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
        user_msg: Optional[Message] = None
        if not suppress_user_message:
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
            if hidden_user_message:
                user_msg["is_hidden_from_transcript"] = True
            if slash_result.kind in {SlashDispatchKind.MAIN_PROMPT, SlashDispatchKind.REFER_PROMPT}:
                user_msg["slash_command"] = self._slash_command_metadata(slash_result)
        normalized_import_files = self._normalize_import_file_refs(import_files)
        if user_msg is not None and normalized_import_files:
            user_msg["import_files"] = normalized_import_files
        normalized_image_refs = self._normalize_image_refs(image_refs)
        if user_msg is not None and normalized_image_refs:
            user_msg["image_refs"] = normalized_image_refs

        active_plan_permission_mode = await self._active_plan_permission_mode(conversation_id)
        refer_context_messages = self._refer_context_messages(refer_bundle)
        requested_tool_permission_mode = (
            normalize_permission_mode(tool_permission_mode)
            if tool_permission_mode not in (None, "")
            else None
        )
        if (
            requested_tool_permission_mode == "plan"
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
        turn_state, error = await self._create_turn_node(
            conversation_id=conversation_id,
            requested_parent_node_id=requested_parent_node_id,
            target_model=target_model,
            target_provider=target_provider,
            meta=meta,
            model_content=model_content,
            user_msg=user_msg,
            focus_new_node=focus_new_node,
            append_to_existing_node=append_to_existing_node,
            requested_tool_permission_mode=requested_tool_permission_mode,
            active_plan_permission_mode=active_plan_permission_mode,
            task_context_mode=task_context_mode,
            hidden_user_message=hidden_user_message,
            suppress_user_message=suppress_user_message,
            run_id=run_id,
        )
        if error is not None:
            yield StreamChunk(
                status=StreamStatus.ERROR,
                content="",
                node_id=None,
                conversation_id=conversation_id,
                run_id=run_id,
                error=error,
                tokens_used=0,
            )
            return
        conversation = turn_state["conversation"]
        new_node = turn_state["new_node"]
        current_node_id = turn_state["current_node_id"]
        skill_names = turn_state["skill_names"]

        assistant_message_id = str(uuid.uuid4())

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
            assistant_message_id=assistant_message_id,
            tokens_used=0,
            tool_permission_mode=new_node.get("tool_permission_mode"),
            task_context_mode=new_node.get("task_context_mode"),
        )

        # 准备消息链。即使调用方要求不切换 UI 焦点，模型也必须基于刚创建的
        # new_node 回复，否则后台通知这类 focus_new_node=False 的消息不会进入上下文。
        prompt_conversation = conversation
        if conversation.current_node_id != new_node["id"]:
            prompt_conversation = Conversation.from_dict(conversation.to_dict())
            prompt_conversation.switch_to_node(new_node["id"])
        task_turn_context = self._start_task_turn_context(prompt_conversation)
        with profiler.span("chat.build_prompt", conversation_id=conversation_id, run_id=run_id, node_id=new_node["id"]):
            messages = self._build_prompt_messages(
                prompt_conversation,
                skill_names,
                task_turn_context=task_turn_context,
            )
        self._insert_context_before_history(messages, refer_context_messages)
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
        exposure_context = self._tool_exposure_context(
            slash_result=slash_result,
            multi_agent_mode=multi_agent_mode,
            permission_mode=new_node.get("tool_permission_mode") or "default",
        )
        available_tools = self._get_openai_tools_for_workspace(workspace_context, exposure_context)
        tools = self._filter_tools_for_runtime(
            available_tools,
            multi_agent_mode=multi_agent_mode,
            permission_mode=new_node.get("tool_permission_mode") or "default",
            task_context_mode=(
                new_node.get("task_context_mode") or TaskContextMode.ATTACHED.value
            ),
        )
        tools = tools or None
        if slash_result.tool_policy == SlashToolPolicy.DISABLED:
            tools = None
        max_tool_rounds = int(cfg.data.get("tools", {}).get("max_rounds", 5)) if isinstance(cfg.data, dict) else 5
        tool_run_context: Dict[str, Any] = {
            "run_id": run_id,
            "run_kind": "chat",
            "root_run_id": run_id,
            "conversation_id": conversation_id,
            "anchor_node_id": current_node_id,
            "node_id": new_node["id"],
            "task_summary": model_content[:160],
            "task_context_mode": new_node.get("task_context_mode") or TaskContextMode.ATTACHED.value,
            "task_generation_id": task_turn_context.generation_id,
            "task_revision": task_turn_context.revision,
            "workspace": workspace_context,
        }
        chat_repository = getattr(self, "chat_repository", None)
        if chat_repository is not None:
            tool_run_context["chat_repository"] = chat_repository
            tool_run_context["persistence"] = chat_repository.persistence

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
        process_content_parts: list[str] = []
        process_parts: list[Dict[str, Any]] = []
        process_order = 0
        pending_terminal_chunks: list[StreamChunk] = []

        try:
            all_tool_calls: List[Dict[str, Any]] = []
            all_tool_messages: List[Message] = []
            all_approval_events: List[Dict[str, Any]] = []
            tool_round = 0
            plan_guard_nudge_count = 0
            max_plan_guard_nudges = 3

            while True:
                if await controller.is_stopped():
                    generation_status = "stopped"
                    pending_terminal_chunks.append(StreamChunk(
                        status=StreamStatus.STOPPED,
                        content="",
                        node_id=new_node["id"],
                        target_node_id=new_node["id"],
                        conversation_id=conversation_id,
                        run_id=run_id,
                        error=None,
                        tokens_used=tokens_used,
                    ))
                    break

                round_content = ""
                round_reasoning = ""
                round_status = "completed"
                complete_chunk = None
                round_tool_calls: List[Dict[str, Any]] = []
                defer_round_content = await self._needs_plan_mode_nudge(
                    conversation_id,
                    new_node.get("tool_permission_mode"),
                )
                deferred_content_chunks: List[Dict[str, Any]] = []

                self._replace_main_runtime_context_message(
                    messages,
                    runtime_prompt_context(
                        "main",
                        prompt_conversation,
                        latest_user_content=self._latest_user_content(prompt_conversation),
                        task_turn_context=task_turn_context,
                        multi_agent_mode=self._resolve_multi_agent_mode(
                            self._multi_agent_intent_text(
                                prompt_conversation,
                                self._latest_user_content(prompt_conversation),
                            ),
                            prompt_conversation.metadata if prompt_conversation is not None else {},
                        ),
                        permission_mode=self._current_node_permission_mode(prompt_conversation),
                        task_context_mode=self._current_node_task_context_mode(prompt_conversation),
                        plan_ledger=getattr(self, "plan_ledger", None),
                    ),
                )
                # provider 引用已在循环前捕获（见上方 get_model）。即便此刻 config 变更
                # 重建了 model_manager，在途流仍用这个局部 provider，不受影响。
                # 不要在循环内重新读取 self.model_manager。
                provider_chunk_count = 0
                provider_content_chars = 0
                provider_reasoning_chars = 0
                provider_tool_call_chunks = 0
                first_provider_chunk = True
                provider_started = perf_counter()
                first_chunk_latency_ms: float | None = None
                first_token_latency_ms: float | None = None
                first_reasoning_latency_ms: float | None = None
                first_content_latency_ms: float | None = None
                with profiler.span(
                    "chat.provider_round",
                    conversation_id=conversation_id,
                    run_id=run_id,
                    node_id=new_node["id"],
                    provider_id=target_provider,
                    model_id=target_model,
                    tool_round=tool_round,
                    tool_count=len(tools or []),
                ):
                    async for chunk in provider.generate_response_stream(
                        model=target_model,
                        messages=messages,
                        stream_controller=controller,
                        tools=tools,
                        tool_choice="auto" if tools else None,
                        reasoning_effort=eff_effort,
                        thinking_enabled=eff_thinking,
                    ): # type: ignore
                        provider_chunk_count += 1
                        provider_tool_event = str(chunk.get("event_type") or "") in {
                            "tool_call_start",
                            "tool_call",
                        }
                        if first_provider_chunk:
                            first_chunk_latency_ms = (perf_counter() - provider_started) * 1000.0
                            profiler.mark(
                                "chat.provider_first_chunk",
                                conversation_id=conversation_id,
                                run_id=run_id,
                                node_id=new_node["id"],
                                provider_id=target_provider,
                                model_id=target_model,
                                latency_ms=round(first_chunk_latency_ms, 3),
                            )
                            first_provider_chunk = False
                        if r := chunk.get("reasoning"):
                            if first_token_latency_ms is None:
                                first_token_latency_ms = (perf_counter() - provider_started) * 1000.0
                                profiler.record({
                                    "type": "span",
                                    "name": "chat.provider_first_token_latency",
                                    "duration_ms": first_token_latency_ms,
                                    "attrs": {
                                        "conversation_id": conversation_id,
                                        "run_id": run_id,
                                        "node_id": new_node["id"],
                                        "provider_id": target_provider,
                                        "model_id": target_model,
                                        "token_kind": "reasoning",
                                    },
                                })
                            if first_reasoning_latency_ms is None:
                                first_reasoning_latency_ms = (perf_counter() - provider_started) * 1000.0
                                profiler.record({
                                    "type": "span",
                                    "name": "chat.provider_first_reasoning_latency",
                                    "duration_ms": first_reasoning_latency_ms,
                                    "attrs": {
                                        "conversation_id": conversation_id,
                                        "run_id": run_id,
                                        "node_id": new_node["id"],
                                        "provider_id": target_provider,
                                        "model_id": target_model,
                                    },
                                })
                            provider_reasoning_chars += len(str(r))
                            total_reasoning += r
                            round_reasoning += r
                        if data := chunk.get("content"):
                            if first_token_latency_ms is None:
                                first_token_latency_ms = (perf_counter() - provider_started) * 1000.0
                                profiler.record({
                                    "type": "span",
                                    "name": "chat.provider_first_token_latency",
                                    "duration_ms": first_token_latency_ms,
                                    "attrs": {
                                        "conversation_id": conversation_id,
                                        "run_id": run_id,
                                        "node_id": new_node["id"],
                                        "provider_id": target_provider,
                                        "model_id": target_model,
                                        "token_kind": "content",
                                    },
                                })
                            if first_content_latency_ms is None:
                                first_content_latency_ms = (perf_counter() - provider_started) * 1000.0
                                profiler.record({
                                    "type": "span",
                                    "name": "chat.provider_first_content_latency",
                                    "duration_ms": first_content_latency_ms,
                                    "attrs": {
                                        "conversation_id": conversation_id,
                                        "run_id": run_id,
                                        "node_id": new_node["id"],
                                        "provider_id": target_provider,
                                        "model_id": target_model,
                                    },
                                })
                            provider_content_chars += len(str(data))
                            total_content += data
                            round_content += data
                        if chunk.get("tool_calls"):
                            provider_tool_call_chunks += 1
                            provider_tool_event = True
                            round_tool_calls = self._merge_tool_call_lists(round_tool_calls, chunk.get("tool_calls") or [])
                        elif chunk.get("tool_call"):
                            provider_tool_call_chunks += 1
                            provider_tool_event = True
                            embedded = chunk.get("tool_call") or {}
                            if embedded.get("tool_calls"):
                                round_tool_calls = self._merge_tool_call_lists(round_tool_calls, embedded.get("tool_calls") or [])

                        chunk_status = chunk.get("status")
                        if chunk_status == StreamStatus.START:
                            continue
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
                        if provider_tool_event:
                            continue
                        if defer_round_content and chunk.get("content"):
                            deferred_content_chunks.append(dict(chunk))
                        else:
                            yield chunk
                provider_duration_ms = (perf_counter() - provider_started) * 1000.0
                provider_content_tokens = _estimate_stream_tokens(round_content)
                provider_reasoning_tokens = _estimate_stream_tokens(round_reasoning)
                provider_estimated_output_tokens = provider_content_tokens + provider_reasoning_tokens
                provider_usage_output_tokens = _usage_output_tokens(usage_info)
                provider_output_tokens = provider_usage_output_tokens or provider_estimated_output_tokens
                provider_tpm = (
                    (provider_output_tokens * 60000.0) / provider_duration_ms
                    if provider_duration_ms > 0 and provider_output_tokens > 0
                    else 0.0
                )
                profiler.mark(
                    "chat.provider_round.done",
                    conversation_id=conversation_id,
                    run_id=run_id,
                    node_id=new_node["id"],
                    provider_id=target_provider,
                    model_id=target_model,
                    chunks=provider_chunk_count,
                    duration_ms=round(provider_duration_ms, 3),
                    first_chunk_latency_ms=round(first_chunk_latency_ms, 3) if first_chunk_latency_ms is not None else None,
                    first_token_latency_ms=round(first_token_latency_ms, 3) if first_token_latency_ms is not None else None,
                    first_reasoning_latency_ms=round(first_reasoning_latency_ms, 3) if first_reasoning_latency_ms is not None else None,
                    first_content_latency_ms=round(first_content_latency_ms, 3) if first_content_latency_ms is not None else None,
                    content_chars=provider_content_chars,
                    reasoning_chars=provider_reasoning_chars,
                    content_tokens_est=provider_content_tokens,
                    reasoning_tokens_est=provider_reasoning_tokens,
                    output_tokens_est=provider_estimated_output_tokens,
                    output_tokens_usage=provider_usage_output_tokens or None,
                    output_tokens_for_tpm=provider_output_tokens,
                    tokens_per_minute_source="usage" if provider_usage_output_tokens else "estimate",
                    tokens_per_minute_est=round(provider_tpm, 3),
                    tool_call_chunks=provider_tool_call_chunks,
                )

                if round_status != "completed":
                    final_content = round_content
                    final_reasoning = round_reasoning
                    if complete_chunk:
                        complete_chunk["conversation_id"] = conversation_id
                        complete_chunk["run_id"] = run_id
                        complete_chunk["target_node_id"] = new_node["id"]
                        pending_terminal_chunks.append(complete_chunk)
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
                            pending_terminal_chunks.append(complete_chunk)
                        break
                    if round_reasoning and (all_tool_calls or all_tool_messages):
                        process_parts.append({
                            "type": "reasoning",
                            "content": round_reasoning,
                            "order": process_order,
                        })
                        process_order += 1
                    for deferred_chunk in deferred_content_chunks:
                        yield deferred_chunk
                    final_content = round_content
                    persisted_final_content = round_content
                    final_reasoning = round_reasoning
                    if complete_chunk:
                        complete_chunk["conversation_id"] = conversation_id
                        complete_chunk["run_id"] = run_id
                        complete_chunk["target_node_id"] = new_node["id"]
                        pending_terminal_chunks.append(complete_chunk)
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
                        pending_terminal_chunks.append(complete_chunk)
                    break

                if tool_round >= max_tool_rounds:
                    error_message = f"工具调用轮数超过上限 {max_tool_rounds}"
                    generation_status = "error"
                    pending_terminal_chunks.append(StreamChunk(
                        status=StreamStatus.ERROR,
                        content="",
                        node_id=new_node["id"],
                        target_node_id=new_node["id"],
                        conversation_id=conversation_id,
                        run_id=run_id,
                        error=error_message,
                        tokens_used=tokens_used,
                    ))
                    break

                round_has_tool_calls = bool(round_tool_calls)
                round_text_is_intermediate = should_emit_as_intermediate_text(
                    has_tool_calls=round_has_tool_calls,
                    plan_guard_active=defer_round_content,
                )

                if not round_text_is_intermediate:
                    for deferred_chunk in deferred_content_chunks:
                        yield deferred_chunk
                tool_round += 1
                tool_round_id = f"{run_id or new_node['id']}:tool-round-{tool_round}"
                assistant_tool_message = {
                    "role": "assistant",
                    "content": round_content,
                    "tool_calls": round_tool_calls,
                    "tool_round": tool_round,
                    "tool_round_id": tool_round_id,
                }
                messages.append(assistant_tool_message)
                if round_reasoning:
                    process_parts.append({
                        "type": "reasoning",
                        "content": round_reasoning,
                        "order": process_order,
                    })
                    process_order += 1
                if round_text_is_intermediate and round_content:
                    process_content_parts.append(round_content)
                    process_parts.append({
                        "type": "content",
                        "content": round_content,
                        "order": process_order,
                    })
                    process_order += 1
                if round_text_is_intermediate:
                    for deferred_chunk in deferred_content_chunks:
                        deferred_chunk.setdefault("event_type", "process_content")
                        yield deferred_chunk
                for call in round_tool_calls:
                    call["call_index"] = process_order
                    process_order += 1
                all_tool_calls.extend(round_tool_calls)
                yield StreamChunk(
                    status=StreamStatus.CONTENT,
                    content=None,
                    node_id=new_node["id"],
                    target_node_id=new_node["id"],
                    conversation_id=conversation_id,
                    run_id=run_id,
                    error=None,
                    tokens_used=0,
                    event_type="tool_calls_committed",
                    tool_calls=round_tool_calls,
                    tool_round=tool_round,
                    tool_round_id=tool_round_id,
                )
                approval_events: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
                round_run_context = {
                    **tool_run_context,
                    "tool_round": tool_round,
                    "tool_round_id": tool_round_id,
                }

                async def emit_tool_event(event: Dict[str, Any]):
                    event = dict(event)
                    event.setdefault("tool_round", tool_round)
                    event.setdefault("tool_round_id", tool_round_id)
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

                async def execute_tool_calls_with_perf():
                    with profiler.span(
                        "chat.tool_round",
                        conversation_id=conversation_id,
                        run_id=run_id,
                        node_id=new_node["id"],
                        tool_round=tool_round,
                        tool_call_count=len(round_tool_calls),
                    ):
                        return await self._execute_tool_calls(
                        round_tool_calls,
                        node_id=new_node["id"],
                        conversation_id=conversation_id,
                        emit_event=emit_tool_event,
                        workspace=workspace_context,
                        permission_mode=new_node.get("tool_permission_mode") or "default",
                        run_context=round_run_context,
                        task_turn_context=task_turn_context,
                    )

                execute_task = asyncio.create_task(
                    execute_tool_calls_with_perf()
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
                            await asyncio.sleep(0)
                            if event_get_task.done():
                                event = event_get_task.result()
                                if str(event.get("event_type", "")).startswith("tool_approval_"):
                                    all_approval_events.append(deepcopy(event))
                                yield self._tool_event_stream_chunk(
                                    event,
                                    node_id=new_node["id"],
                                    conversation_id=conversation_id,
                                )
                            while not approval_events.empty():
                                event = approval_events.get_nowait()
                                if str(event.get("event_type", "")).startswith("tool_approval_"):
                                    all_approval_events.append(deepcopy(event))
                                yield self._tool_event_stream_chunk(
                                    event,
                                    node_id=new_node["id"],
                                    conversation_id=conversation_id,
                                )
                            if not execute_task.done():
                                execute_task.cancel()
                                with suppress(asyncio.CancelledError):
                                    await execute_task
                            pending_terminal_chunks.append(StreamChunk(
                                status=StreamStatus.STOPPED,
                                content="",
                                node_id=new_node["id"],
                                target_node_id=new_node["id"],
                                conversation_id=conversation_id,
                                run_id=run_id,
                                error=None,
                                tokens_used=tokens_used,
                            ))
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
                                if self.chat_repository is not None:
                                    self.chat_repository.ensure_node(
                                        conversation_id,
                                        new_node["id"],
                                        parent_id=new_node.get("parent_id") or "",
                                        tool_permission_mode=next_permission_mode,
                                        focus=False,
                                    )
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
                                    exposure_context = self._tool_exposure_context(
                                        slash_result=slash_result,
                                        multi_agent_mode=multi_agent_mode,
                                        permission_mode=next_permission_mode,
                                    )
                                    available_tools = self._get_openai_tools_for_workspace(
                                        workspace_context,
                                        exposure_context,
                                    )
                                    tools = self._filter_tools_for_runtime(
                                        available_tools,
                                        multi_agent_mode=multi_agent_mode,
                                        permission_mode=next_permission_mode,
                                        task_context_mode=(
                                            new_node.get("task_context_mode")
                                            or TaskContextMode.ATTACHED.value
                                        ),
                                    ) or None
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
                model_tool_messages = apply_round_tool_result_budget(tool_messages)
                messages.extend(model_tool_messages)
                all_tool_messages.extend(tool_messages)
                if has_blocking_plan_participation_result(tool_messages):
                    final_content = ""
                    persisted_final_content = ""
                    final_reasoning = total_reasoning
                    if complete_chunk:
                        complete_chunk["conversation_id"] = conversation_id
                        complete_chunk["run_id"] = run_id
                        complete_chunk["target_node_id"] = new_node["id"]
                        complete_chunk["metadata"] = {
                            **(complete_chunk.get("metadata") or {}),
                            "followup_expected": False,
                            "terminal_reason": "awaiting_plan_approval",
                        }
                        pending_terminal_chunks.append(complete_chunk)
                    break

            # 检查是否被手动停止
            if await controller.is_stopped():
                generation_status = "stopped"

        except Exception as e:
            generation_status = "error"
            error_message = str(e) or e.__class__.__name__
            logger.exception(f"流式生成出错: {error_message}")
            pending_terminal_chunks.append(StreamChunk(
                status=StreamStatus.ERROR,
                content="",
                node_id=new_node["id"],
                target_node_id=new_node["id"],
                conversation_id=conversation_id,
                run_id=run_id,
                error=error_message,
                tokens_used=tokens_used,
            ))
        finally:
            # ── 临界区 2（锁内）+ 转写持久化：见 _finalize_assistant_turn ──
            await self._finalize_assistant_turn(
                conversation_id=conversation_id,
                run_id=run_id,
                conversation=conversation,
                new_node=new_node,
                target_provider=target_provider,
                target_model=target_model,
                meta=meta,
                assistant_message_id=assistant_message_id,
                start_time=start_time,
                tokens_used=tokens_used,
                usage_info=usage_info,
                generation_status=generation_status,
                error_message=error_message,
                total_content=total_content,
                total_reasoning=total_reasoning,
                final_content=final_content,
                persisted_final_content=persisted_final_content,
                process_content_parts=process_content_parts,
                process_parts=process_parts,
                all_tool_calls=all_tool_calls,
                all_tool_messages=all_tool_messages,
                all_approval_events=all_approval_events,
                append_to_existing_node=append_to_existing_node,
            )

        for terminal_chunk in pending_terminal_chunks:
            yield terminal_chunk

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

    def _start_task_turn_context(
        self,
        conversation: Optional[Conversation],
    ) -> TaskTurnContext:
        mode = normalize_context_mode(self._current_node_task_context_mode(conversation))
        task = None
        conversation_id = str(((conversation.metadata if conversation is not None else {}) or {}).get("id") or "")
        task_service = getattr(self, "task_service", None)
        if mode == TaskContextMode.ATTACHED and conversation_id and task_service is not None:
            task = task_service.get_active_task_snapshot(conversation_id)
        return TaskTurnContext.start(mode, task)

    @staticmethod
    def _replace_main_runtime_context_message(
        messages: List[Message],
        runtime_context: RuntimePromptContext,
    ) -> None:
        replacement = Message(runtime_context.as_section(priority=15).as_message())
        for index, message in enumerate(messages):
            metadata = message.get("metadata")
            if isinstance(metadata, dict) and metadata.get("runtime_context") == "main":
                messages[index] = replacement
                return
        raise RuntimeError("main runtime context message is missing")

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

    def _refer_context_messages(self, refer_bundle: Optional[Dict[str, Any]]) -> list[Message]:
        if not refer_bundle:
            return []
        content, truncated = format_refer_context_message(refer_bundle)
        refer_bundle["truncated"] = bool(refer_bundle.get("truncated") or truncated)
        return [Message({
            "role": Role.SYSTEM,
            "content": content,
        })]

    @staticmethod
    def _insert_context_before_history(messages: list[Message], context_messages: list[Message]) -> None:
        if not context_messages:
            return
        index = 0
        while index < len(messages):
            role = messages[index].get("role")
            role_value = role.value if hasattr(role, "value") else str(role or "")
            if role_value != "system":
                break
            index += 1
        messages[index:index] = context_messages

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

    def _merge_existing_node_assistant_continuation(
        self,
        existing_assistant: Optional[Message],
        continuation_assistant: Message,
    ) -> Message:
        if not existing_assistant:
            return Message(deepcopy(continuation_assistant))

        merged = Message(deepcopy(continuation_assistant))
        merged["id"] = existing_assistant.get("id") or continuation_assistant.get("id")
        for key in ("content", "reasoning", "process_content"):
            existing_text = str(existing_assistant.get(key) or "")
            continuation_text = str(continuation_assistant.get(key) or "")
            merged[key] = (existing_text + continuation_text) or ("" if key == "content" else None)
        existing_process_parts = [
            dict(part)
            for part in (existing_assistant.get("process_parts") or [])
            if isinstance(part, dict)
        ]
        continuation_process_parts = [
            dict(part)
            for part in (continuation_assistant.get("process_parts") or [])
            if isinstance(part, dict)
        ]
        if existing_process_parts or continuation_process_parts:
            ordered_process_parts: list[Dict[str, Any]] = []
            current_process_parts: list[Dict[str, Any]] = []
            next_order = 0
            for is_continuation, parts in (
                (False, existing_process_parts),
                (True, continuation_process_parts),
            ):
                for part in sorted(parts, key=lambda item: self._numeric_order(item.get("order"))):
                    part["order"] = next_order
                    ordered_process_parts.append(part)
                    if is_continuation:
                        current_process_parts.append(part)
                    next_order += 1
            if ordered_process_parts:
                merged["process_parts"] = ordered_process_parts
            continuation_assistant["process_parts"] = current_process_parts or None
        merged.pop("tool_calls", None)
        merged.pop("tool_results", None)
        return merged

    @staticmethod
    def _numeric_order(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _permission_mode_after_plan_tools(self, tool_messages: list[Message], current_mode: str) -> str:
        mode = normalize_permission_mode(current_mode)
        for message in tool_messages:
            name = str(message.get("name") or "")
            payload = _tool_result_payload(message.get("raw_content") or message.get("content"))
            if name == "enter_plan_mode" and payload.get("permission_mode") == "plan":
                mode = "plan"
            elif name == "ask_user_question" and payload.get("status") == "awaiting_question":
                mode = "plan"
            elif name == "exit_plan_mode" and payload.get("status") == "awaiting_approval":
                mode = "plan"
        return mode

    def _plan_tool_paused_turn(self, tool_messages: list[Message]) -> bool:
        return has_blocking_plan_participation_result(tool_messages)

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
            "Plan mode final response was discarded because plan mode requires explicit plan-mode tool use.",
            "You are already in plan mode. Continue read-only planning.",
            "Call `ask_user_question` only when a genuine user decision is required to continue planning.",
            "Call `exit_plan_mode` with the final plan when user approval is required before implementation.",
            "Do not edit files, run implementation commands, or claim implementation may start.",
            f"Attempt: {attempt}",
            "</system-reminder>",
        ])

    def _plan_guard_blocked_message(self) -> str:
        return "\n".join([
            "计划模式仍在等待明确的计划模式工具调用。",
            "已丢弃普通最终回复，避免绕过计划模式流程。",
        ])

    def _latest_user_content(self, conversation: Conversation) -> str:
        node_id = conversation.current_node_id
        if not node_id:
            return ""
        for message in reversed(
            _canonical_messages_by_node(self.chat_repository,conversation.metadata["id"], [node_id]).get(node_id, [])
        ):
            if message.get("role") == Role.USER and not message.get("is_hidden_from_transcript"):
                content = message.get("content")
                return content if isinstance(content, str) else str(content or "")
        return ""

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

        node_ids = [str(node.get("id")) for node in chain if node.get("id")]
        messages_by_node = _canonical_messages_by_node(self.chat_repository,conversation.metadata["id"], node_ids)
        for node in reversed(chain):
            node_id = str((node or {}).get("id") or "")
            user_messages = [
                message for message in messages_by_node.get(node_id, [])
                if message.get("role") == Role.USER and not message.get("is_hidden_from_transcript")
            ]
            if not user_messages:
                continue
            content = user_messages[-1].get("content")
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
            if (name in AGENT_TOOL_NAMES or name == "agent") and mode == "none":
                continue
            filtered.append(tool)
        return filtered

    def _filter_plan_tools_for_mode(
        self,
        tools: List[Dict[str, Any]],
        mode: str,
    ) -> List[Dict[str, Any]]:
        normalized_mode = normalize_permission_mode(mode)
        plan_only = {"ask_user_question", "exit_plan_mode"}
        filtered: List[Dict[str, Any]] = []
        for tool in tools:
            name = str((tool.get("function") or {}).get("name") or "")
            if normalized_mode == "plan" and name in {"agent", "edit", "shell"}:
                continue
            if name in plan_only and normalized_mode != "plan":
                continue
            if name == "enter_plan_mode" and normalized_mode == "plan":
                continue
            filtered.append(tool)
        return filtered

    def _filter_tools_for_runtime(
        self,
        tools: List[Dict[str, Any]],
        *,
        multi_agent_mode: str,
        permission_mode: str,
        task_context_mode: str,
    ) -> List[Dict[str, Any]]:
        filtered = self._filter_agent_tools_for_mode(tools, multi_agent_mode)
        filtered = self._filter_plan_tools_for_mode(filtered, permission_mode)
        return filter_task_tools_for_context(filtered, task_context_mode)

    def _refresh_task_turn_context(
        self,
        *,
        run_context: Optional[Dict[str, Any]],
        turn_context: Optional[TaskTurnContext],
        conversation_id: Optional[str],
        tool_call: Dict[str, Any],
        tool_message: Message,
    ) -> None:
        if (
            run_context is None
            or turn_context is None
            or turn_context.mode != TaskContextMode.ATTACHED
            or not conversation_id
            or self.task_service is None
        ):
            return
        outcome = self._task_outcome_from_tool_execution(tool_call, tool_message)
        task = self.task_service.get_active_task_snapshot(conversation_id)
        turn_context.refresh(task, outcome)
        run_context["task_generation_id"] = turn_context.generation_id
        run_context["task_revision"] = turn_context.revision

    def _task_outcome_from_tool_execution(
        self,
        tool_call: Dict[str, Any],
        tool_message: Message,
    ) -> Optional[TaskOutcome]:
        payload = self._parse_structured_tool_result(
            tool_message.get("raw_content", tool_message.get("content"))
        )
        if isinstance(payload, dict):
            outcome = TaskOutcome.from_dict(payload.get("task_outcome"))
            if outcome is not None:
                return outcome
        return None

    @staticmethod
    def _parse_structured_tool_result(content: Any) -> Any:
        if isinstance(content, (dict, list)):
            return content
        if not isinstance(content, str):
            return None
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return None

    def _current_node_permission_mode(self, conversation: Optional[Conversation]) -> str:
        if conversation is None:
            return "default"
        node = conversation.nodes.get(conversation.current_node_id or "") or {}
        return normalize_permission_mode(node.get("tool_permission_mode") or "default")

    def _current_node_task_context_mode(self, conversation: Optional[Conversation]) -> str:
        if conversation is None:
            return TaskContextMode.ATTACHED.value
        node = conversation.nodes.get(conversation.current_node_id or "") or {}
        return normalize_context_mode(node.get("task_context_mode")).value

    def _model_node_chain(
        self,
        conversation: Conversation,
        *,
        include_messages_to_keep: bool = True,
    ) -> List[Dict[str, Any]]:
        """返回发给模型的节点链：root + 最新 canonical compact 后的有效上下文。"""
        chain = conversation.get_node_chain(conversation.current_node_id)
        if not chain:
            return []
        compact_metadata = compact_metadata_by_node(self.chat_repository,
            conversation.metadata["id"],
            [str(node.get("id")) for node in chain if node.get("id")],
        )

        latest_boundary_index = None
        for index, node in enumerate(chain):
            if str(node.get("id") or "") in compact_metadata:
                latest_boundary_index = index

        if latest_boundary_index is None:
            return chain

        root = chain[0]
        compact_node = chain[latest_boundary_index]
        compact_meta = compact_metadata.get(str(compact_node.get("id") or ""), {})
        keep_count = max(int(compact_meta.get("messages_to_keep") or 0), 0) if include_messages_to_keep else 0
        kept_nodes = [
            node for node in chain[1:latest_boundary_index]
            if str(node.get("id") or "") not in compact_metadata
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
        chain = self._model_node_chain(
            conversation,
            include_messages_to_keep=include_messages_to_keep,
        )
        messages_by_node = _canonical_messages_by_node(self.chat_repository,
            conversation.metadata["id"],
            [str(node.get("id")) for node in chain if node.get("id")],
        )
        for node in chain:
            for msg in messages_by_node.get(str(node.get("id") or ""), []):
                if msg.get("role") == Role.USER:
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

    def _latest_prune_summary_for_context(
        self,
        conversation_id: str,
        node_id: str,
        target_chain_ids: set[str],
    ) -> Optional[Dict[str, Any]]:
        summaries = prune_summaries_by_node(self.chat_repository,conversation_id, [node_id]).get(node_id, [])
        if not summaries:
            return None
        for summary in summaries:
            covered = set(str(node_id) for node_id in (summary.get("covered_node_ids") or []))
            if covered.intersection(target_chain_ids):
                continue
            return summary
        return None

    def _format_prune_summary_context_message(
        self,
        conversation_id: str,
        node_id: str,
        target_chain_ids: set[str],
    ) -> Optional[Message]:
        summary = self._latest_prune_summary_for_context(conversation_id, node_id, target_chain_ids)
        if not summary:
            return None
        return Message({
            "id": f"{node_id}:prune_summary:{summary.get('id')}",
            "role": Role.USER,
            "content": build_prune_context_message(summary),
            "timestamp": int(summary.get("created_at") or time()),
            "is_visible_in_transcript_only": True,
            "subtype": "prune_summary_context",
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
        parent_node_id: str,
        target_model: str,
        target_provider: str,
        model_context_window: Optional[int],
    ) -> Dict[str, Any]:
        if not model_context_window:
            return {"was_compacted": False}
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            return {"was_compacted": False}
        if parent_node_id not in conversation.nodes:
            return {"was_compacted": False}
        conversation.switch_to_node(parent_node_id)
        current_node = conversation.nodes.get(parent_node_id)
        if current_node and parent_node_id in compact_metadata_by_node(self.chat_repository,
            conversation_id,
            [parent_node_id],
        ):
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
            parent_node_id=parent_node_id,
            focus_new_node=False,
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
        parent_node_id: Optional[str] = None,
        focus_new_node: bool = True,
    ) -> Dict[str, Any]:
        """手动执行 Claude Code 风格上下文压缩，并把结果追加为当前分支节点。"""
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            raise ValueError("对话不存在")
        requested_parent_node_id = parent_node_id or conversation.current_node_id
        if not requested_parent_node_id or requested_parent_node_id not in conversation.nodes:
            raise ValueError("父节点不存在")
        conversation.switch_to_node(requested_parent_node_id)

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
        summary = format_compact_summary(str(summary or ""))
        pre_tokens = self._rough_token_count_for_messages(messages_to_summarize)

        async with self._lock_for(conversation_id):
            latest = self.get_conversation(conversation_id)
            if latest is None:
                raise ValueError("对话不存在")
            parent_id = requested_parent_node_id
            if parent_id not in latest.nodes:
                raise ValueError("父节点不存在")
            parent_task_context_mode = str(
                (latest.nodes.get(parent_id or "") or {}).get("task_context_mode")
                or TaskContextMode.ATTACHED.value
            )
            compact_node = NodeManager.create_compact_node(
                parent_id=parent_id,
                model_id=target_model,
                task_context_mode=parent_task_context_mode,
            )
            compact_metadata: Dict[str, Any] = {
                "trigger": "auto" if trigger == "auto" else "manual",
                "pre_tokens": int(pre_tokens or 0),
                "messages_to_keep": max(int(messages_to_keep or 0), 0),
                "last_pre_compact_message_id": parent_id,
            }
            if restored_files:
                compact_metadata["restored_files"] = restored_files
            boundary_message_id = str(uuid.uuid4())
            summary_message_id = str(uuid.uuid4())
            compact_node["usage"] = self._node_usage_snapshot(
                turn_usage=estimated_usage(tokens_used),
                branch_usage=estimated_usage(0),
                model_context_window=self._model_context_window(target_provider, target_model),
            )
            latest.add_node(compact_node, parent_id=parent_id, focus=focus_new_node)
            latest.metadata["updated_at"] = max(
                int(latest.metadata.get("updated_at") or 0),
                int(time()),
            )
            self.chat_repository.save(latest)
            if self.chat_repository is not None:
                self.chat_repository.ensure_branch(
                    latest,
                    compact_node["id"],
                    provider_id=target_provider,
                    model_id=target_model,
                    focus_node_id=latest.current_node_id,
                )
                self.chat_repository.add_message(
                    conversation_id,
                    compact_node["id"],
                    role=Role.SYSTEM.value,
                    content="Conversation compacted",
                    subtype="compact_boundary",
                    hidden=True,
                    metadata=compact_metadata,
                    message_id=boundary_message_id,
                )
                self.chat_repository.add_message(
                    conversation_id,
                    compact_node["id"],
                    role=Role.ASSISTANT.value,
                    content=str(summary or ""),
                    subtype="compact_summary",
                    hidden=True,
                    transcript_only=True,
                    metadata=compact_metadata,
                    message_id=summary_message_id,
                )

        return {
            "conversation_id": conversation_id,
            "node_id": compact_node["id"],
            "pre_tokens": pre_tokens,
            "tokens_used": tokens_used,
            "trigger": compact_metadata["trigger"],
        }

    async def prune_summary(
        self,
        conversation_id: str,
        parent_node_id: str,
        custom_instructions: Optional[str] = None,
        model_id: Optional[str] = None,
        provider_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """总结父节点下的子树，并把摘要保存为父节点上下文附件。"""
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            raise ValueError("对话不存在")
        if not parent_node_id or parent_node_id not in conversation.nodes:
            raise ValueError("父节点不存在")

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

        # provider.generate_response 是同步网络调用。剪枝摘要可能耗时较长，
        # 必须放到线程里，避免阻塞 FastAPI 主事件循环和其它 SSE/HTTP 请求。
        summary_record = await asyncio.to_thread(
            self._generate_prune_summary_record_sync,
            conversation,
            parent_node_id,
            custom_instructions,
            target_model,
            target_provider,
            provider,
        )

        async with self._lock_for(conversation_id):
            latest = self.get_conversation(conversation_id)
            if latest is None:
                raise ValueError("对话不存在")
            parent = latest.nodes.get(parent_node_id)
            if parent is None:
                raise ValueError("父节点不存在")
            latest.metadata["updated_at"] = max(
                int(latest.metadata.get("updated_at") or 0),
                int(time()),
            )
            self.chat_repository.save(latest)
            if self.chat_repository is not None:
                metadata = {
                    key: value
                    for key, value in summary_record.items()
                    if key not in {"id", "summary", "type", "status", "created_at"}
                }
                self.chat_repository.add_message(
                    conversation_id,
                    parent_node_id,
                    role=Role.SYSTEM.value,
                    content=str(summary_record.get("summary") or ""),
                    subtype="prune_summary",
                    hidden=True,
                    transcript_only=True,
                    metadata=metadata,
                    message_id=str(summary_record["id"]),
                )

        preview = str(summary_record.get("summary") or "").strip()
        if len(preview) > 800:
            preview = preview[:800] + "..."
        return {
            "conversation_id": conversation_id,
            "parent_node_id": parent_node_id,
            "summary_id": summary_record["id"],
            "covered_node_count": len(summary_record.get("covered_node_ids") or []),
            "covered_direct_child_count": len(summary_record.get("covered_direct_child_ids") or []),
            "covered_node_ids": summary_record.get("covered_node_ids") or [],
            "covered_direct_child_ids": summary_record.get("covered_direct_child_ids") or [],
            "compact_node_ids": summary_record.get("compact_node_ids") or [],
            "truncated_node_ids": summary_record.get("truncated_node_ids") or [],
            "coverage_notes": summary_record.get("coverage_notes") or [],
            "summary_preview": preview,
            "summary": summary_record.get("summary") or "",
        }

    def _generate_prune_summary_record_sync(
        self,
        conversation: Conversation,
        parent_node_id: str,
        custom_instructions: Optional[str],
        target_model: str,
        target_provider: str,
        provider: Any,
    ) -> Dict[str, Any]:
        tool_history = {
            node_id: [
                {
                    "tool_call": dict((tool_call_message.get("tool_calls") or [{}])[0]),
                    "tool_result": dict(tool_result_message),
                }
                for tool_call_message, tool_result_message in pairs
            ]
            for node_id, pairs in tool_history_by_node(self.chat_repository,
                conversation.metadata["id"],
                [str(node_id) for node_id in conversation.nodes.keys()],
            ).items()
        }
        node_ids = [str(node_id) for node_id in conversation.nodes.keys()]
        messages_by_node = {
            node_id: [dict(message) for message in messages]
            for node_id, messages in _canonical_messages_by_node(self.chat_repository,
                conversation.metadata["id"],
                node_ids,
            ).items()
        }
        compact_metadata = compact_metadata_by_node(self.chat_repository,
            conversation.metadata["id"],
            node_ids,
        )
        packet_bundle = build_prune_packets(
            conversation,
            parent_node_id,
            messages_by_node,
            tool_history,
            compact_metadata,
        )
        branch_digests: List[Dict[str, Any]] = []
        summary_packet_bundle = packet_bundle
        tokens_used_total = 0
        if len(json_dumps(packet_bundle)) > PRUNE_PACKET_BUDGET_CHARS:
            for branch_packet in packet_bundle.get("branch_packets") or []:
                digest_messages = build_branch_digest_messages(
                    packet_bundle.get("parent") or {},
                    branch_packet,
                    custom_instructions,
                )
                digest, digest_tokens = provider.generate_response(
                    target_model,
                    digest_messages,
                    max_tokens=PRUNE_BRANCH_DIGEST_MAX_OUTPUT_TOKENS,
                    temperature=0,
                    tools=None,
                    tool_choice=None,
                )
                tokens_used_total += int(digest_tokens or 0)
                branch_digests.append({
                    "direct_child_node_id": branch_packet.get("direct_child_node_id"),
                    "branch_order": branch_packet.get("branch_order"),
                    "is_current_branch": branch_packet.get("is_current_branch"),
                    "digest": str(digest or "").strip(),
                    "coverage": branch_packet.get("coverage") or {},
                })
            summary_packet_bundle = {
                "parent": packet_bundle.get("parent") or {},
                "branch_digests": branch_digests,
                "coverage": deepcopy(packet_bundle.get("coverage") or {}),
            }
            coverage_notes = summary_packet_bundle["coverage"].setdefault("coverage_notes", [])
            coverage_notes.append("子树 packet 超过全局预算，已先生成分支摘要后再合成剪枝摘要。")

        summary_messages = build_prune_summary_messages(summary_packet_bundle, custom_instructions)
        summary, tokens_used = provider.generate_response(
            target_model,
            summary_messages,
            max_tokens=PRUNE_SUMMARY_MAX_OUTPUT_TOKENS,
            temperature=0,
            tools=None,
            tool_choice=None,
        )
        tokens_used_total += int(tokens_used or 0)
        summary_record = create_prune_summary_record(
            parent_node_id=parent_node_id,
            summary=summary,
            packet_bundle=packet_bundle,
            model_id=target_model,
            provider_id=target_provider,
            custom_instructions=custom_instructions,
            tokens_used=tokens_used_total,
            branch_digests=branch_digests,
        )
        if branch_digests:
            summary_record["coverage_notes"] = list(summary_packet_bundle.get("coverage", {}).get("coverage_notes") or [])
        return summary_record

    def _prepare_messages_for_api_with_conversation(
        self,
        conversation: Conversation,
        *,
        include_messages_to_keep: bool = True,
    ) -> List[Message]:
        """准备API调用的消息列表。历史事实只读 canonical SQLite。"""
        msg_dict = []

        def append_message(msg: Optional[Message]):
            if not msg:
                return
            if msg.get("subtype") == "compact_boundary":
                return
            role = getattr(msg["role"], "value", msg["role"])
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
        node_ids = [str(node.get("id")) for node in node_chain if node.get("id")]
        target_chain_ids = set(node_ids)
        canonical_messages = _canonical_messages_by_node(self.chat_repository,conversation.metadata["id"], node_ids)
        canonical_tool_history = tool_history_by_node(self.chat_repository,
            conversation.metadata["id"],
            node_ids,
        )
        compact_metadata = compact_metadata_by_node(self.chat_repository,
            conversation.metadata["id"],
            node_ids,
        )
        for node in node_chain:
            node_id = str(node.get("id") or "")
            messages_for_node = canonical_messages.get(node_id, [])
            is_compact_node = node_id in compact_metadata
            final_assistant_messages: List[Message] = []
            for message in messages_for_node:
                role = getattr(message.get("role"), "value", message.get("role"))
                subtype = str(message.get("subtype") or "")
                if subtype == "compact_boundary":
                    continue
                if subtype == "compact_summary":
                    compact_meta = compact_metadata.get(node_id, {})
                    append_message(Message({
                        "id": message.get("id") or f"{node_id}:compact_summary",
                        "role": Role.USER,
                        "content": get_compact_user_summary_message(
                            str(message.get("content") or ""),
                            suppress_follow_up_questions=bool(
                                compact_meta.get("suppress_follow_up_questions", True)
                            ),
                        ),
                        "timestamp": int(message.get("timestamp") or time()),
                        "subtype": "compact_summary",
                    }))
                    continue
                if subtype in {"assistant_process_reasoning", "assistant_process_content", "prune_summary"}:
                    continue
                if message.get("is_hidden_from_transcript") and not message.get("is_visible_in_transcript_only"):
                    continue
                if role == Role.USER.value:
                    append_message(message)
                    import_files = message.get("import_files") or []
                    if import_files:
                        append_message(
                            self._format_import_file_context_message(
                                conversation.metadata["id"],
                                import_files,
                            )
                        )
                elif role == Role.ASSISTANT.value:
                    final_assistant = Message(dict(message))
                    final_assistant.pop("tool_calls", None)
                    final_assistant.pop("tool_results", None)
                    final_assistant_messages.append(final_assistant)
                elif role == Role.SYSTEM.value:
                    append_message(message)
            if is_compact_node:
                restored_files = compact_metadata.get(node_id, {}).get("restored_files") or []
                if restored_files:
                    append_message(Message({
                        "id": f"{node_id}:restored_files",
                        "role": Role.SYSTEM,
                        "content": format_restored_file_context(restored_files),
                        "timestamp": int(node.get("timestamp") or time()),
                    }))
            for tool_call_message, tool_result_message in canonical_tool_history.get(str(node.get("id")), []):
                append_message(tool_call_message)
                for tool_msg in apply_round_tool_result_budget([tool_result_message]):
                    append_message(tool_msg)
            for final_assistant in final_assistant_messages:
                append_message(final_assistant)
            append_message(self._format_prune_summary_context_message(
                conversation.metadata["id"],
                node_id,
                target_chain_ids,
            ))

        return microcompact_messages(msg_dict)

    def _merge_tool_call_lists(
        self,
        current: List[Dict[str, Any]],
        incoming: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        merged = [deepcopy(call) for call in current]
        index_by_id = {
            str(call.get("id")): index
            for index, call in enumerate(merged)
            if call.get("id")
        }
        for call in incoming:
            call_copy = deepcopy(call)
            call_id = str(call_copy.get("id") or "")
            if call_id and call_id in index_by_id:
                merged[index_by_id[call_id]] = call_copy
            else:
                if call_id:
                    index_by_id[call_id] = len(merged)
                merged.append(call_copy)
        return merged

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
            run_id=event.get("run_id"),
            error=None,
            tokens_used=0,
            event_type=event.get("event_type"),
            approval=event.get("approval"),
            tool_call=event.get("tool_call"),
            tool_calls=event.get("tool_calls"),
            tool_round=event.get("tool_round"),
            tool_round_id=event.get("tool_round_id"),
        )

    def _tool_observation_event(
        self,
        event: Dict[str, Any],
        *,
        tool_call: Dict[str, Any],
        name: str,
    ) -> Dict[str, Any]:
        fn = tool_call.get("function") or {}
        tool_call_id = str(tool_call.get("id") or event.get("tool_call_id") or "")
        incoming = event.get("tool_call") if isinstance(event.get("tool_call"), dict) else {}
        payload = dict(incoming)
        payload.setdefault("id", tool_call_id)
        payload.setdefault("tool_call_id", tool_call_id)
        payload.setdefault("name", name)
        payload.setdefault("type", tool_call.get("type") or "function")
        payload.setdefault(
            "function",
            {
                "name": name,
                "arguments": fn.get("arguments") or "",
            },
        )
        for key in ("status", "progress", "content_delta", "error"):
            if key in event:
                payload[key] = event[key]
        return {
            "event_type": event.get("event_type") or "tool_progress",
            "run_id": event.get("run_id"),
            "tool_round": event.get("tool_round"),
            "tool_round_id": event.get("tool_round_id"),
            "tool_call": payload,
        }

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
            persisted = persist_model_visible_tool_result(
                self.chat_repository,
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
        task_turn_context: Optional[TaskTurnContext] = None,
    ) -> List[Message]:
        results: list[Optional[Message]] = [None] * len(tool_calls)
        current_permission_mode = permission_mode

        if not self.tool_manager:
            raise RuntimeError("Tool manager is not configured")

        waves = plan_tool_call_waves(
            tool_calls,
            lambda name: self._tool_capabilities_for_runtime(name, workspace),
        )

        async def execute_one(tool_call: Dict[str, Any], mode: PermissionMode) -> Message:
            return await self._execute_single_tool_call(
                tool_call,
                node_id=node_id,
                conversation_id=conversation_id,
                emit_event=emit_event,
                workspace=workspace,
                permission_mode=mode,
                run_context=run_context,
            )

        for wave in waves:
            if wave.parallel:
                wave_permission_mode = current_permission_mode
                wave_messages = await asyncio.gather(*[
                    execute_one(item.call, wave_permission_mode)
                    for item in wave.calls
                ])
            else:
                item = wave.calls[0]
                wave_messages = [await execute_one(item.call, current_permission_mode)]

            for item, message in zip(wave.calls, wave_messages):
                results[item.index] = message
                self._refresh_task_context_after_relevant_tool(
                    tool_call=item.call,
                    tool_message=message,
                    run_context=run_context,
                    task_turn_context=task_turn_context,
                    conversation_id=conversation_id,
                )
                current_permission_mode = self._permission_mode_after_plan_tools(
                    [message],
                    current_permission_mode,
                )

        return [message for message in results if message is not None]

    def _refresh_task_context_after_relevant_tool(
        self,
        *,
        tool_call: Dict[str, Any],
        tool_message: Message,
        run_context: Optional[Dict[str, Any]],
        task_turn_context: Optional[TaskTurnContext],
        conversation_id: Optional[str],
    ) -> None:
        name = tool_call_function_name(tool_call)
        arguments = self._parse_tool_arguments((tool_call.get("function") or {}).get("arguments"))
        if not (
            name in TASK_TOOL_NAMES
            or name in TASK_OBSERVATION_TOOL_NAMES
            or (name in TASK_BOUND_RUN_TOOL_NAMES and arguments.get("step") is not None)
        ):
            return
        self._refresh_task_turn_context(
            run_context=run_context,
            turn_context=task_turn_context,
            conversation_id=conversation_id,
            tool_call=tool_call,
            tool_message=tool_message,
        )

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
        chat_repository = getattr(self, "chat_repository", None)
        if chat_repository is not None:
            call_run_context["chat_repository"] = chat_repository
            call_run_context["persistence"] = chat_repository.persistence
        loop = asyncio.get_running_loop()
        pending_observation_futures: list[Any] = []

        async def emit_tool_observation(event: Dict[str, Any]) -> None:
            if emit_event is None:
                return
            event = dict(event)
            event.setdefault("run_id", call_run_context.get("run_id"))
            event.setdefault("tool_round", call_run_context.get("tool_round"))
            event.setdefault("tool_round_id", call_run_context.get("tool_round_id"))
            payload = self._tool_observation_event(
                event,
                tool_call=tool_call,
                name=name,
            )
            result = emit_event(payload)
            if isawaitable(result):
                await result

        def schedule_tool_observation(event: Dict[str, Any]) -> None:
            if emit_event is None or loop.is_closed():
                return
            future = asyncio.run_coroutine_threadsafe(
                emit_tool_observation(event),
                loop,
            )
            pending_observation_futures.append(future)

        async def flush_tool_observations() -> None:
            while pending_observation_futures:
                futures = list(pending_observation_futures)
                pending_observation_futures.clear()
                await asyncio.gather(
                    *(asyncio.wrap_future(future) for future in futures),
                    return_exceptions=True,
                )

        call_run_context["tool_event_sink"] = schedule_tool_observation

        await emit_tool_observation({
            "event_type": "tool_call_start",
            "status": "running",
        })
        await emit_tool_observation({
            "event_type": "tool_progress",
            "status": "running",
            "progress": {
                "phase": "started",
                "elapsed_ms": 0,
            },
        })

        heartbeat_stop = asyncio.Event()

        async def progress_heartbeat() -> None:
            started_at = perf_counter()
            try:
                try:
                    await asyncio.wait_for(heartbeat_stop.wait(), timeout=1.5)
                    return
                except asyncio.TimeoutError:
                    pass
                while not heartbeat_stop.is_set():
                    await emit_tool_observation({
                        "event_type": "tool_progress",
                        "status": "running",
                        "progress": {
                            "phase": "running",
                            "elapsed_ms": int((perf_counter() - started_at) * 1000),
                        },
                    })
                    try:
                        await asyncio.wait_for(heartbeat_stop.wait(), timeout=1.0)
                        break
                    except asyncio.TimeoutError:
                        continue
            except asyncio.CancelledError:
                raise

        heartbeat_task = asyncio.create_task(progress_heartbeat())
        profiler = get_profiler()
        tool_call_attrs = {
            "conversation_id": conversation_id,
            "node_id": node_id,
            "run_id": call_run_context.get("run_id"),
            "tool_name": name,
            "tool_call_id": tool_call.get("id"),
            "permission_mode": permission_mode,
            **summarize_tool_arguments(name, arguments),
        }
        tool_call_started = perf_counter()
        profiler.mark("chat.tool_call.start", **tool_call_attrs)
        try:
            if tool_orchestrator:
                message = await tool_orchestrator.execute_tool_call(
                    tool_call,
                    conversation_id or "",
                    node_id,
                    emit_event=emit_event,
                    workspace=workspace,
                    permission_mode=permission_mode,
                    run_context=call_run_context,
                )
                model_message = self._model_visible_tool_message(
                    message,
                    name=name,
                    conversation_id=conversation_id,
                    node_id=node_id,
                    tool_call_id=tool_call.get("id"),
                )
            else:
                if not self.tool_manager:
                    raw_result = json.dumps({"error": "Tool manager is not configured"}, ensure_ascii=False)
                else:
                    raw_result = await self.tool_manager.execute_tool(
                        name,
                        arguments,
                        workspace=workspace,
                        runtime_context=call_run_context,
                    )
                model_message = self._model_visible_tool_message(
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
        except Exception as exc:
            profiler.record({
                "type": "span",
                "name": "chat.tool_call",
                "duration_ms": (perf_counter() - tool_call_started) * 1000.0,
                "attrs": {**tool_call_attrs, "error_type": type(exc).__name__},
            })
            await flush_tool_observations()
            await emit_tool_observation({
                "event_type": "tool_call_error",
                "status": "error",
                "error": str(exc),
            })
            raise
        finally:
            heartbeat_stop.set()
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

        profiler.record({
            "type": "span",
            "name": "chat.tool_call",
            "duration_ms": (perf_counter() - tool_call_started) * 1000.0,
            "attrs": {**tool_call_attrs, **summarize_tool_result(model_message.get("raw_content"))},
        })
        await flush_tool_observations()
        parsed_result = self._parse_structured_tool_result(model_message.get("raw_content"))
        result_status = "error" if isinstance(parsed_result, dict) and parsed_result.get("error") else "done"
        await emit_tool_observation({
            "event_type": "tool_result",
            "status": result_status,
            "tool_call": {
                "tool_call_id": model_message.get("tool_call_id"),
                "name": model_message.get("name"),
                "content": model_message.get("content"),
                "raw_content": model_message.get("raw_content"),
                "model_visible_content": model_message.get("model_visible_content"),
                "tool_result_id": model_message.get("tool_result_id"),
            },
        })
        return model_message

    def _tool_capabilities_for_runtime(
        self,
        tool_name: str,
        workspace: Optional[Dict[str, Any]] = None,
    ) -> set[Any]:
        capabilities_for = getattr(self.tool_manager, "capabilities_for", None)
        if callable(capabilities_for):
            return set(capabilities_for(tool_name, workspace=workspace))
        return set()

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
            usage = add_usage(
                usage,
                turn_usage_for_node(self.chat_repository,conversation.metadata["id"], str(node.get("id") or "")),
            )
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
        turn_usage = turn_usage_for_node(self.chat_repository,conversation.metadata["id"], node_id) or estimated_usage(0)
        branch_usage = self._branch_usage_for_node(conversation, node_id)
        node["branch_usage_info"] = branch_usage
        node["total_tokens"] = usage_total(branch_usage)
        node["usage"] = self._node_usage_snapshot(
            turn_usage=turn_usage,
            branch_usage=branch_usage,
            model_context_window=model_context_window,
        )

    def get_conversation_history(self) -> List[Message]:
        """获取当前分支 canonical 历史，供旧调试入口使用。"""
        if not self.current_conversation or not self.current_conversation.current_node_id:
            return []
        node_ids = [
            str(node.get("id"))
            for node in self.current_conversation.get_node_chain(self.current_conversation.current_node_id)
            if node.get("id")
        ]
        messages_by_node = _canonical_messages_by_node(self.chat_repository,self.current_conversation.metadata["id"], node_ids)
        history: List[Message] = []
        for node_id in node_ids:
            history.extend(messages_by_node.get(node_id, []))
        return history
