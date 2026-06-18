# chat/chat_manager.py - 适配延迟加载
from typing import List, Optional, Dict, AsyncIterator
import uuid
import asyncio  
from time import time
from .conversation import Conversation
from .node import NodeManager
from ..config.types import Message, Role, StreamChunk, StreamStatus, StreamController, GenerationInfo
from ..storage.chat_storage import ChatStorage
from ..storage.prompt_storage import PromptStorage
from ..model.model_manager import ModelManager
from ..model.usage import add_usage, estimated_usage, usage_total
from ..utils.logger import setup_logger
from ..config.config import cfg

logger = setup_logger('ChatManager')

class ChatManager:
    """延迟加载模型的聊天管理器"""
    
    def __init__(self, model_manager: ModelManager, storage: ChatStorage, prompts: PromptStorage):
        self.model_manager = model_manager
        self.storage = storage
        self.prompts = prompts
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
    
    def create_conversation(self, title: str = '', prompt_id: Optional[str] = None) -> Conversation:
        """
        创建新对话（不实例化模型，只保存配置ID）
        """
        # 创建对话，只保存model_id字符串引用
        conversation = Conversation(title=title)
        
        # 初始化系统消息
        system_prompt = None if not prompt_id else self.prompts.load(prompt_id)
        conversation.initialize_with_system_message(system_prompt)

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
    
    def list_conversations(self) -> List[Dict[str, str]]:
        """列出所有对话"""
        return self.storage.list()
    
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

    async def send_message_stream(
        self,
        conversation_id: str,
        content: str,
        model_id: Optional[str] = None,
        node_id: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        thinking_enabled: Optional[bool] = None,
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
                content="",
                error="对话不存在",
                tokens_used=0
            )
            return

        preview = Conversation.from_dict(conversation_data)
        # 确定模型：优先使用传入的model_id，其次使用对话的current_model，最后使用第一个可用模型
        target_model = model_id or preview.current_model
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
                content="",
                error="未指定模型ID",
                tokens_used=0
            )
            return
        
        # 获取提供商
        target_provider = None
        for provider, models in self.model_manager.model_list.items():
            if target_model in models:
                target_provider = provider
                break

        logger.info(f"Stream: model={target_model}, provider={target_provider}, model_list_keys={list(self.model_manager.model_list.keys())}")

        if not target_provider:
            yield StreamChunk(
                status=StreamStatus.ERROR,
                content="",
                node_id=None,
                conversation_id=conversation_id,
                error=f"无法找到模型 {target_model} 对应的提供商",
                tokens_used=0
            )
            return
        
        provider = self.model_manager.get_model(target_provider, True)
        if not provider:
            logger.error(f"无法初始化提供商 {target_provider} (is_async=True)")
            yield StreamChunk(
                status=StreamStatus.ERROR,
                content="",
                node_id=None,
                conversation_id=conversation_id,
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

        # 创建用户消息
        user_msg = Message({
            "id": str(uuid.uuid4()),
            "role": Role.USER,
            "content": content,
            "name": None,
            "tool_calls": None,
            "tool_call_id": None,
            "timestamp": int(time())
        })

        # ── 临界区 1（锁内）：重新加载最新快照 + 建节点 + 立即保存 user 消息 ──
        # 锁内重载确保看到其他并发流刚提交的兄弟节点，add_node 不会丢失 root 引用。
        # 立即落盘是为了让前端的 userMsgLanded 判定能尽快看到真实 user 消息。
        async with self._lock_for(conversation_id):
            conversation = self.get_conversation(conversation_id)
            if conversation is None:
                yield StreamChunk(
                    status=StreamStatus.ERROR, content="", node_id=None,
                    conversation_id=conversation_id, error="对话不存在", tokens_used=0)
                return
            if node_id:
                conversation.switch_to_node(node_id)
            current_node_id = conversation.current_node_id
            new_node = NodeManager.create_node(
                user_message=user_msg,
                parent_id=current_node_id,
                model_id=target_model
            )
            conversation.add_node(new_node, parent_id=current_node_id)
            self._save(conversation)

        # 创建流控制器（在锁外，避免把网络流式包进锁里阻塞同对话其他分支）
        controller = StreamController(
            node_id=new_node["id"],
            conversation_id=conversation.metadata["id"]
        )
        self._active_controllers[new_node["id"]] = controller

        # 准备消息链（使用锁内加载的最新 conversation）
        messages = self._prepare_messages_for_api_with_conversation(conversation)

        total_content = ""
        total_reasoning = ""
        tokens_used = 0
        usage_info = None
        start_time = time()  # 记录开始时间
        generation_status = "completed"  # 默认状态
        error_message = None

        try:
            # provider 引用已在循环前捕获（见上方 get_model）。即便此刻 config 变更
            # 重建了 model_manager，在途流仍用这个局部 provider，不受影响。
            # 不要在循环内重新读取 self.model_manager。
            async for chunk in provider.generate_response_stream(
                model=target_model,
                messages=messages,
                stream_controller=controller,
                reasoning_effort=eff_effort,
                thinking_enabled=eff_thinking,
            ): # type: ignore
                # 思考增量：累积到 total_reasoning（event_type=="reasoning" 时 content 为空）
                if r := chunk.get("reasoning"):
                    total_reasoning += r
                if data := chunk.get("content"):
                    total_content += data
                # Track generation status from stream chunks
                chunk_status = chunk.get("status")
                if chunk_status == StreamStatus.ERROR:
                    generation_status = "error"
                    error_message = chunk.get("error")
                elif chunk_status == StreamStatus.STOPPED:
                    generation_status = "stopped"
                # 在 COMPLETE chunk 上捕获最终 token 总量（三家 provider 都在此带最终值）
                if chunk_status == StreamStatus.COMPLETE:
                    tokens_used = chunk.get("tokens_used", tokens_used) or tokens_used
                    usage_info = chunk.get("usage_info") or usage_info
                    tokens_used = usage_total(usage_info, tokens_used)
                # 更新 conversation_id 在 chunk 中
                chunk["conversation_id"] = conversation_id
                yield chunk

            # 检查是否被手动停止
            if await controller.is_stopped():
                generation_status = "stopped"

        except Exception as e:
            generation_status = "error"
            error_message = str(e)
            logger.error(f"流式生成出错: {e}")
            raise
        finally:
            # 计算用时
            duration_ms = int((time() - start_time) * 1000)
            if usage_info is None:
                usage_info = estimated_usage(tokens_used)
            tokens_used = usage_total(usage_info, tokens_used)

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
                "content": total_content,
                "name": None,
                "tool_calls": None,
                "tool_call_id": None,
                "reasoning": total_reasoning or None,
                "timestamp": int(time()),
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
                    self._update_token_stats_for_conversation(latest, target_provider, tokens_used)
                    self._update_branch_usage_for_node(latest, new_node["id"])
                    self._save(latest)
                else:
                    # 极端情况：节点已被并发删除——退回到只保存本节点，避免丢消息
                    NodeManager.add_assistant_message(new_node, assistant_msg)
                    new_node["total_tokens"] = usage_total(usage_info, tokens_used)
                    new_node["branch_usage_info"] = usage_info
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
            logger.info(f"已请求终止节点 {node_id} 的流")
            return True
        return False

    async def stop_all_streams(self):
        """终止所有活跃流"""
        for node_id in list(self._active_controllers.keys()):
            await self.stop_stream(node_id)

    def _prepare_messages_for_api_with_conversation(self, conversation: Conversation) -> List[Message]:
        """准备API调用的消息列表（使用指定的 conversation）。

        工具轮次接缝：除 role/content 外，**存在即保留** tool_calls / tool_call_id /
        name，使未来的工具调用消息能完整流到 provider，而当前纯文本路径形状不变。
        """
        messages = conversation.get_message_chain_from_node(conversation.current_node_id)

        msg_dict = []
        for msg in messages:
            role = msg["role"]
            # Role 枚举转为小写字符串值（"user" 而非 "Role.USER"）
            if not isinstance(role, str) or role.startswith("Role."):
                role = role.value if hasattr(role, 'value') else str(role).split(".")[-1].lower()
            out: Dict[str, Any] = {
                "role": role,
                "content": msg["content"],
            }
            if msg.get("tool_calls"):
                out["tool_calls"] = msg["tool_calls"]
            if msg.get("tool_call_id"):
                out["tool_call_id"] = msg["tool_call_id"]
            if msg.get("name"):
                out["name"] = msg["name"]
            msg_dict.append(out)

        return msg_dict

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

    def _branch_usage_for_node(self, conversation: Conversation, node_id: str):
        usage = None
        for node in conversation.get_node_chain(node_id):
            usage = add_usage(usage, self._usage_from_message(node.get("assistant_message")))
        return usage or estimated_usage(0)

    def _update_branch_usage_for_node(self, conversation: Conversation, node_id: str):
        node = conversation.nodes.get(node_id)
        if not node:
            return
        branch_usage = self._branch_usage_for_node(conversation, node_id)
        node["branch_usage_info"] = branch_usage
        node["total_tokens"] = usage_total(branch_usage)

    def get_conversation_history(self) -> List[Message]:
        """获取对话历史"""
        if self.current_conversation:
            return self.current_conversation.get_message_chain_from_node(self.current_conversation.current_node_id)
        return []
