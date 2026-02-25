# test_stream_async.py
import asyncio
import sys
import os
from pathlib import Path
import pdb

# 将backend目录添加到Python路径
backend_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(backend_dir)
sys.path.insert(0, project_root)

from backend.core.chat.chat_manager import ChatManager
from backend.core.model.model_manager import ModelManager
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage
from backend.core.config.config import cfg
from backend.core.config.types import StreamStatus, ModelProvider

# 日志配置
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

async def wait_for_q_key(chat_manager: ChatManager, node_id: str):
    """
    在后台监听键盘输入，按下q时终止流
    在独立线程中运行，不阻塞事件循环
    """
    def _read_key():
        try:
            # 非阻塞读取单个字符
            if os.name == 'nt':  # Windows
                import msvcrt
                while True:
                    if msvcrt.kbhit():
                        key = msvcrt.getch().decode('utf-8', errors='ignore').lower()
                        if key == 'q':
                            return True
        except:
            pass
        return False
    
    # 在独立线程中运行阻塞的键盘监听
    should_stop = await asyncio.to_thread(_read_key)
    
    if should_stop:
        print("\n[⏹️ 检测到按键 'q'，正在终止生成...]")
        chat_manager.stop_stream(node_id)

async def test_stream_generation():
    """测试正常流式生成"""
    print("=" * 60)
    print("测试场景1: 正常流式生成")
    print("=" * 60)
    cfg.set_default_model(ModelProvider.OLLAMA)
    # 检查配置
    provider = cfg.data.get('default_provider', ModelProvider.OLLAMA)
    provider_config = cfg.data.get('provider', {}).get(provider)
    
    if not provider_config or not provider_config.get('api_key'):
        print("⚠️  警告: 未找到有效的API配置，跳过模型对话测试")
        print(f"请先在 data/test_config.json 中配置 {provider} 的 API key")
        return
    try:
        # 初始化核心组件
        print("1. 初始化组件...")
        model_manager = ModelManager()
        storage = ChatStorage()
        prompts = PromptStorage("data/prompts")
        chat_manager = ChatManager(model_manager, storage, prompts)

        print(model_manager.list_available_models(provider))
        
        # 创建对话
        print("\n2. 创建测试对话...")
        conversation = chat_manager.create_conversation("异步流式测试")
        print(f"   对话ID: {conversation.metadata['id']}")
        conversation.set_current_model(ModelProvider.OLLAMA, 'deepseek-v3.1:671b-cloud')
        
        # 发送流式消息
        print("\n3. 发送流式消息...")
        user_input = "请介绍一下人工智能的发展历史，详细说明关键里程碑。"

        current_node_id = conversation.current_node_id
        key_task = asyncio.create_task(
            wait_for_q_key(chat_manager, current_node_id)
        )
        
        full_content = ""
        chunk_count = 0
        
        async for chunk in chat_manager.send_message_stream(user_input, 'deepseek-v3.1:671b-cloud'):
            chunk_count += 1
            content = chunk.get("content", "") or ""
            full_content += content
            
            # 实时显示(chunk大小>5才显示，避免过多小片段)
            if content:
                sys.stdout.write(content)
                sys.stdout.flush()
            
            # 检查状态
            if chunk["status"] == StreamStatus.COMPLETE:
                print(f"\n   ✅ 生成完成！总长度: {len(full_content)} 字符")
                print(f"   总片段数: {chunk_count}")
            elif chunk["status"] == StreamStatus.ERROR:
                print(f"\n   ❌ 错误: {chunk['error']}")
                return False
        
        # 验证保存
        print("\n4. 验证对话保存...")
        chat_manager.save_conversation()
        loaded = chat_manager.load_conversation(conversation.metadata["id"])
        print(f"   保存并重新加载: {'成功' if loaded else '失败'}")
        
        # 显示对话树
        tree = conversation.get_node_tree()
        print(f"\n5. 对话树节点数: {len(tree)}")
        for node_info in tree:
            level = node_info["level"]
            user_content = node_info.get("user_content", "")[:30]
            assistant_content = node_info.get("assistant_content", "")[:30]
            print(f"   {'  ' * level}└─ Node {node_info['id'][:8]}: {user_content}... → {assistant_content}...")
        
        return True
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_stream_termination():
    """测试流式终止功能"""
    print("\n" + "=" * 60)
    print("测试场景2: 流式终止功能")
    print("=" * 60)
    
    try:
        # 初始化
        print("1. 初始化组件...")
        model_manager = ModelManager()
        storage = ChatStorage()
        prompts = PromptStorage("data/prompts")
        chat_manager = ChatManager(model_manager, storage, prompts)
        
        # 创建对话
        print("\n2. 创建测试对话...")
        conversation = chat_manager.create_conversation("终止测试")
        # conversation.metadata["model"] = "qwen2.5:7b"
        print(f"   对话ID: {conversation.metadata['id']}")
        
        # 发送流式消息，但2秒后终止
        print("\n3. 发送流式消息（将在2秒后终止）...")
        user_input = "请详细解释机器学习的各个分支，包括监督学习、无监督学习和强化学习，每个分支提供具体算法示例和应用场景。"
        
        full_content = ""
        chunk_count = 0
        
        # 创建异步任务
        stream_task = asyncio.create_task(
            _collect_stream(chat_manager, user_input)
        )
        
        # 等待2秒后终止
        await asyncio.sleep(2)
        current_node_id = conversation.current_node_id
        print(f"\n   ⏹️  终止节点: {current_node_id[:8]}...")
        
        success = chat_manager.stop_stream(current_node_id)
        print(f"   终止请求发送: {'成功' if success else '失败'}")
        
        # 等待任务完成
        try:
            full_content, chunk_count = await asyncio.wait_for(stream_task, timeout=5)
        except asyncio.TimeoutError:
            print("   ⚠️  等待流完成超时")
            stream_task.cancel()
            return False
        
        print(f"\n4. 终止结果:")
        print(f"   已生成内容长度: {len(full_content)} 字符")
        print(f"   接收到的片段数: {chunk_count}")
        print(f"   终止后内容: {full_content[:100]}...")
        
        # 验证节点已保存部分结果
        current_node = conversation.nodes.get(current_node_id)
        if current_node and current_node.get("assistant_message"):
            saved_content = current_node["assistant_message"]["content"]
            print(f"   节点保存的内容长度: {len(saved_content)} 字符")
            print(f"   内容一致性: {'✅ 一致' if saved_content == full_content else '❌ 不一致'}")
        
        return True
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False


async def _collect_stream(chat_manager, user_input: str):
    """辅助函数：收集流式输出"""
    full_content = ""
    chunk_count = 0
    
    async for chunk in chat_manager.send_message_stream(user_input, 'deepseek-v3.1:671b-cloud'):
        chunk_count += 1
        content = chunk.get("content", "") or ""
        full_content += content
        
        status = chunk["status"]
        if status == StreamStatus.CONTENT and len(content) > 10:
            print(f"   [接收 {chunk_count}] {content[:30]}...")
        elif status == StreamStatus.STOPPED:
            print(f"   ⏹️  流被终止: {chunk.get('error')}")
            break
        elif status == StreamStatus.ERROR:
            print(f"   ❌ 流错误: {chunk.get('error')}")
            break
        elif status == StreamStatus.COMPLETE:
            print(f"   ✅ 流正常完成")
            break
    
    return full_content, chunk_count


async def test_ollama_connection():
    """测试Ollama连接"""
    print("测试Ollama连接...")
    try:
        model_manager = ModelManager()
        provider = model_manager.get_model("ollama")
        if not provider:
            print("❌ 无法初始化Ollama提供商")
            return False
        
        models = provider.list_models()
        print(f"✅ Ollama连接成功，可用模型: {models}")
        return True
    except Exception as e:
        print(f"❌ Ollama连接失败: {e}")
        print("请确保Ollama已安装并运行: ollama serve")
        return False


async def main():
    """主测试函数"""
    print("开始异步流式功能测试...")
    print(f"配置目录: {cfg.config_path}")
    
    # 检查Ollama
    if not await test_ollama_connection():
        print("\n⚠️  Ollama未连接，测试将使用mock数据（如果需要）")
        print("   建议先安装并运行Ollama以获得真实测试结果")
        response = input("是否继续测试？(y/n): ")
        if response.lower() != 'y':
            return
    
    # 运行测试
    results = []
    
    # 测试1: 正常生成
    result1 = await test_stream_generation()
    results.append(("正常流式生成", result1))
    
    # 测试2: 终止功能
    # result2 = await test_stream_termination()
    # results.append(("流式终止", result2))
    
    # 总结
    print("\n" + "=" * 60)
    print("测试总结")
    print("=" * 60)
    
    for test_name, passed in results:
        status = "✅ 通过" if passed else "❌ 失败"
        print(f"   {test_name}: {status}")
    
    all_passed = all(r[1] for r in results)
    print(f"\n总体结果: {'✅ 所有测试通过' if all_passed else '❌ 部分测试失败'}")
    return


if __name__ == "__main__":
    asyncio.run(main())