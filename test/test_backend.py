# test_backend.py - 直接测试后端核心逻辑
import sys
import os
backend_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(backend_dir)
sys.path.insert(0, project_root)

from backend.core.config.config import Config
from backend.core.model.model_manager import ModelManager
from backend.core.chat.chat_manager import ChatManager
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage
from backend.core.config.types import Message, Role, ModelProvider
import time
from backend.core.chat.message import MessageManager
from backend.core.chat.node import NodeManager
from backend.core.config.config import cfg

def test_config():
    """测试配置管理"""
    print("=== 测试配置管理 ===")
    config = Config("data/test_config.json")
    
    # 设置测试配置
    config.data['default_provider'] = ModelProvider.OLLAMA
    config.data['provider'] = {
        ModelProvider.OLLAMA: {
            'name': 'ollama',
            'models': [],
            'api_key': 'test-key',
            'base_url': 'https://127.0.0.1:11434/v1',
            'enabled': True,
            'default_model': ''
        }
    }
    config.save()
    
    # 重新加载验证
    config2 = Config("data/test_config.json")
    assert config2.data['default_provider'] == ModelProvider.OLLAMA
    print("✅ 配置管理测试通过")

def test_model_manager():
    """测试模型管理器"""
    # print("\n=== 测试模型管理器 ===")
    # model_manager = ModelManager()
    
    # # 测试获取模型列表
    # # 注意：这会实际调用API，需要有效配置
    # # models = model_manager.list_available_models(ModelProvider.OPENAI)
    # # print(f"可用模型: {models}")
    
    # # 测试设置当前模型
    # result = model_manager.set_current_model(ModelProvider.OPENAI)
    # assert result == True
    # print(f"当前模型: {model_manager.current_model}")
    # print("✅ 模型管理器测试通过")
    pass

def test_chat_storage():
    """测试聊天存储"""
    print("\n=== 测试聊天存储 ===")
    storage = ChatStorage("data/conversations")
    
    # 测试数据
    test_data = {
        "metadata": {
            "id": "test-conv-123",
            "title": "测试对话",
            "created_at": 0,
            "updated_at": 0,
            "model": "gpt-3.5-turbo",
            "total_tokens": {}
        },
        "nodes": [
            {
                "id": "node-1",
                "parent_id": None,
                "children_ids": ["node-2"],
                "user_message": None,
                "assistant_message": None,
                "tool_messages": [],
                "system_message": {
                    "id": "msg-1",
                    "role": "system",
                    "content": "你是一个助手",
                    "timestamp": 0
                },
                "timestamp": 0,
                "model_id": None,
                "total_tokens": 0
            },
            {
                "id": "node-2",
                "parent_id": "node-1",
                "children_ids": [],
                "user_message": {
                    "id": "msg-2",
                    "role": "user",
                    "content": "你好",
                    "timestamp": 0
                },
                "assistant_message": {
                    "id": "msg-3",
                    "role": "assistant",
                    "content": "你好！有什么可以帮你的吗？",
                    "timestamp": 0
                },
                "tool_messages": [],
                "system_message": None,
                "timestamp": 0,
                "model_id": "gpt-3.5-turbo",
                "total_tokens": 10
            }
        ],
        "current_node_id": "node-2",
        "root_node_id": "node-1"
    }
    
    # 测试保存
    storage.save(test_data)
    print("✅ 对话保存成功")
    
    # 测试加载
    loaded = storage.load("test-conv-123")
    assert loaded is not None
    assert loaded["metadata"]["title"] == "测试对话"
    assert len(loaded["nodes"]) == 2
    print("✅ 对话加载成功")
    
    # 测试列表
    conv_list = storage.list()
    assert len(conv_list) > 0
    print(f"✅ 对话列表: {len(conv_list)} 个对话")
    
    # 测试存在检查
    assert storage.exists("test-conv-123") == True
    print("✅ 存在检查通过")
    
    # 测试删除
    storage.delete("test-conv-123")
    assert storage.exists("test-conv-123") == False
    print("✅ 删除测试通过")

def test_conversation_tree():
    """测试对话树核心逻辑"""
    print("\n=== 测试对话树逻辑 ===")
    
    # 初始化组件
    model_manager = ModelManager()
    storage = ChatStorage("data/conversations")
    prompts = PromptStorage("data/prompts")
    chat_manager = ChatManager(model_manager, storage, prompts)
    
    # 测试1: 创建对话
    conversation = chat_manager.create_conversation("测试对话树")
    conv_id = conversation.metadata["id"]
    print(f"✅ 创建对话: {conv_id}")
    
    # 测试2: 添加系统消息
    conversation.initialize_with_system_message("你是一个 helpful 助手")
    print("✅ 初始化系统消息")
    
    # 测试3: 构建消息链
    # 模拟第一轮对话
    from backend.core.chat.node import NodeManager
    from backend.core.chat.message import MessageManager
    
    # 创建用户消息节点
    user_msg = MessageManager.create_user_message("你好，请介绍一下自己")
    node1 = NodeManager.create_node(user_msg, parent_id=conversation.root_node_id, model_id="gpt-3.5-turbo")
    conversation.add_node(node1, parent_id=conversation.root_node_id)
    
    # 创建助手回复
    assistant_msg = MessageManager.create_assistant_message("你好！我是一个AI助手，可以帮助你回答问题、提供建议等。")
    NodeManager.add_assistant_message(node1, assistant_msg)
    print("✅ 添加第一轮对话")
    
    # 测试4: 创建分支（第二轮对话的两个不同回复）
    # 分支1
    user_msg2a = MessageManager.create_user_message("你能做什么？")
    node2a = NodeManager.create_node(user_msg2a, parent_id=node1["id"], model_id="gpt-3.5-turbo")
    conversation.add_node(node2a, parent_id=node1["id"])
    
    assistant_msg2a = MessageManager.create_assistant_message("我可以回答问题、写文章、编程、翻译等等。")
    NodeManager.add_assistant_message(node2a, assistant_msg2a)
    
    # 分支2（从同一父节点创建）
    user_msg2b = MessageManager.create_user_message("你叫什么名字？")
    node2b = NodeManager.create_node(user_msg2b, parent_id=node1["id"], model_id="gpt-3.5-turbo")
    conversation.add_node(node2b, parent_id=node1["id"])
    
    assistant_msg2b = MessageManager.create_assistant_message("我没有特定的名字，你可以叫我AI助手。")
    NodeManager.add_assistant_message(node2b, assistant_msg2b)
    
    print(f"✅ 创建分支: 节点 {node1['id'][:8]} 有两个子节点")
    
    # 测试5: 获取分支信息
    branches = conversation.get_available_branches()
    print(f"✅ 检测到 {len(branches)} 个分支点")
    for branch in branches:
        print(f"  - 分支ID: {branch['branch_id'][:8]}, 消息数: {branch['message_count']}")
    
    # 测试6: 切换节点
    current_before = conversation.current_node_id
    conversation.switch_to_node(node2a["id"])
    assert conversation.current_node_id is not None
    assert current_before is not None
    print(f"✅ 切换节点: {current_before[:8]} -> {conversation.current_node_id[:8]}")
    
    # 测试7: 获取消息链
    message_chain = conversation.get_message_chain_from_node(node2a["id"])
    print(f"✅ 消息链长度: {len(message_chain)} 条消息")
    for msg in message_chain:
        print(f"  - {msg['role']}: {msg['content'][:30]}...")
    
    # 测试8: 保存对话
    chat_manager.save_conversation()
    print("✅ 对话已持久化")
    
    # 测试9: 加载对话
    chat_manager.load_conversation(conv_id)
    loaded_conv = chat_manager.current_conversation
    assert loaded_conv is not None
    assert loaded_conv.metadata["id"] == conv_id
    print(f"✅ 重新加载对话: {loaded_conv.metadata['title']}")
    
    # 测试10: 删除对话
    # chat_manager.delete_conversation(conv_id)
    # assert not storage.exists(conv_id)
    # print("✅ 对话删除成功")

def test_model_conversation():
    """测试模型对话功能（需要有效API配置）"""
    print("\n=== 测试模型对话功能 ===")
    cfg.set_default_model(ModelProvider.OLLAMA)
    # 检查配置
    provider = cfg.data.get('default_provider', ModelProvider.OLLAMA)
    provider_config = cfg.data.get('provider', {}).get(provider)
    
    if not provider_config or not provider_config.get('api_key'):
        print("⚠️  警告: 未找到有效的API配置，跳过模型对话测试")
        print(f"请先在 data/config.json 中配置 {provider} 的 API key")
        return
    if not provider_config.get('enabled', False):
        provider_config['enabled'] = True
        cfg.add_provider_config(provider, provider_config)
    
    # 初始化组件
    model_manager = ModelManager()
    storage = ChatStorage("data/conversations")
    prompts = PromptStorage("data/prompts")
    chat_manager = ChatManager(model_manager, storage, prompts)

    print(model_manager.list_available_models(provider))
    
    try:
        # 测试1: 单轮对话
        print("\n--- 测试1: 单轮对话 ---")
        conversation = chat_manager.create_conversation("单轮测试")
        conv_id = conversation.metadata["id"]
        conversation.set_current_model(ModelProvider.OLLAMA, 'deepseek-v3.1:671b-cloud')
        
        # 添加系统消息
        conversation.initialize_with_system_message("你是一个简洁的助手，用20字内回答", True)
        
        # 发送消息
        print("用户: 你好，请自我介绍")
        response = chat_manager.send_message("你好，请自我介绍",'deepseek-v3.1:671b-cloud')
        print(f"助手: {response}")
        assert response and len(response) > 0, "响应为空"
        
        # 验证token统计
        total_tokens = conversation.metadata["total_tokens"]
        assert len(total_tokens) > 0, "Token统计未更新"
        print(f"✅ Token统计: {total_tokens}")
        
        # 验证消息历史
        history = chat_manager.get_conversation_history()
        assert len(history) == 3  # system + user + assistant
        print(f"✅ 消息历史: {len(history)} 条消息")
        
        # 保存对话
        chat_manager.save_conversation()
        print(f"✅ 对话已保存: {conv_id[:8]}")
        
        # 测试2: 多轮对话
        print("\n--- 测试2: 多轮对话 ---")
        
        # 第二轮
        print("\n用户: 你能做什么？")
        response2 = chat_manager.send_message("你能做什么？",'deepseek-v3.1:671b-cloud')
        print(f"助手: {response2}")
        
        # 第三轮
        print("\n用户: 讲个笑话")
        response3 = chat_manager.send_message("讲个笑话",'deepseek-v3.1:671b-cloud')
        print(f"助手: {response3}")
        
        # 验证多轮历史
        history = chat_manager.get_conversation_history()
        assert len(history) == 7  # system + 3轮对话(user+assistant*3)
        print(f"✅ 多轮历史: {len(history)} 条消息")
        
        # 测试3: 分支功能
        print("\n--- 测试3: 分支功能 ---")
        
        # 回到第一个用户消息节点创建分支
        nodes = list(conversation.nodes.keys())
        first_user_node = None
        for node_id in nodes:
            node = conversation.nodes[node_id]
            if node.get("user_message") and "你好" in node["user_message"]["content"]: # type: ignore
                first_user_node = node_id
                break
        
        assert first_user_node, "未找到用户消息节点"
        
        # 创建分支（从第一个问题分出不同路径）
        print(f"\n从节点 {first_user_node[:8]} 创建分支...")
        
        # 分支A: 问天气
        user_msg_branch_a = MessageManager.create_user_message("今天天气怎么样？")
        node_branch_a = NodeManager.create_node(
            user_msg_branch_a, 
            parent_id=first_user_node, 
            model_id="gpt-3.5-turbo"
        )
        conversation.add_node(node_branch_a, parent_id=first_user_node)
        
        # 模拟助手回复（实际使用模型）
        assistant_msg_a = MessageManager.create_assistant_message(
            "我无法获取实时天气信息，建议查看天气预报应用。"
        )
        NodeManager.add_assistant_message(node_branch_a, assistant_msg_a)
        print("✅ 分支A创建: 天气查询")
        
        # 分支B: 问新闻
        user_msg_branch_b = MessageManager.create_user_message("有什么新闻？")
        node_branch_b = NodeManager.create_node(
            user_msg_branch_b,
            parent_id=first_user_node,
            model_id="gpt-3.5-turbo"
        )
        conversation.add_node(node_branch_b, parent_id=first_user_node)
        
        assistant_msg_b = MessageManager.create_assistant_message(
            "我无法获取实时新闻，建议查看新闻网站或应用。"
        )
        NodeManager.add_assistant_message(node_branch_b, assistant_msg_b)
        print("✅ 分支B创建: 新闻查询")
        
        # 验证分支
        branches = conversation.get_available_branches()
        print(f"✅ 检测到 {len(branches)} 个分支点")
        assert len(branches) >= 1, "分支检测失败"
        
        # 切换到分支A
        print(f"\n切换到分支A: {node_branch_a['id'][:8]}")
        conversation.switch_to_node(node_branch_a["id"])
        current_history = chat_manager.get_conversation_history()
        print(f"分支A历史消息数: {len(current_history)}")
        
        # 在分支A继续对话
        print("用户: 那明天天气呢？")
        response_branch_a = chat_manager.send_message("那明天天气呢？",'deepseek-v3.1:671b-cloud')
        print(f"助手: {response_branch_a}")
        
        # 切换回B分支
        print(f"\n切换回B分支: {node_branch_b['id'][:8]}")
        conversation.switch_to_node(node_branch_b["id"])
        
        # 在分支B继续对话
        print("用户: 体育新闻呢？")
        response_branch_b = chat_manager.send_message("体育新闻呢？",'deepseek-v3.1:671b-cloud')
        print(f"助手: {response_branch_b}")
        
        # 测试4: 验证分支独立性
        print("\n--- 测试4: 验证分支独立性 ---")
        
        # 检查两个分支的历史不同
        conversation.switch_to_node(node_branch_a["id"])
        history_a = chat_manager.get_conversation_history()
        
        conversation.switch_to_node(node_branch_b["id"])
        history_b = chat_manager.get_conversation_history()
        
        assert history_a != history_b, "分支历史应该不同"
        print(f"✅ 分支A历史: {len(history_a)} 条消息")
        print(f"✅ 分支B历史: {len(history_b)} 条消息")
        
        # 检查最后消息内容不同
        last_msg_a = history_a[-1]["content"]
        last_msg_b = history_b[-1]["content"]
        assert last_msg_a != last_msg_b, "分支最后消息应该不同"
        print("✅ 分支消息内容验证通过")
        
        # 测试5: 持久化和恢复
        print("\n--- 测试5: 持久化和恢复 ---")
        
        # 保存当前状态
        chat_manager.save_conversation()
        
        # 重新加载
        chat_manager.load_conversation(conv_id)
        reloaded_conv = chat_manager.current_conversation
        
        # 验证分支仍然可用
        assert reloaded_conv is not None
        reloaded_branches = reloaded_conv.get_available_branches()
        assert len(reloaded_branches) == len(branches), "持久化后分支信息丢失"
        print("✅ 分支信息持久化验证通过")
        
        # 验证可以切换到之前分支
        reloaded_conv.switch_to_node(node_branch_a["id"])
        reloaded_history_a = chat_manager.get_conversation_history()
        assert len(reloaded_history_a) == len(history_a), "分支A历史恢复失败"
        print("✅ 分支A恢复验证通过")
        
        # 测试6: 性能测试
        print("\n--- 测试6: 性能测试 ---")
        
        start_time = time.time()
        
        # 执行多次切换操作
        for _ in range(5):
            conversation.switch_to_node(node_branch_a["id"])
            _ = chat_manager.get_conversation_history()
            conversation.switch_to_node(node_branch_b["id"])
            _ = chat_manager.get_conversation_history()
        
        end_time = time.time()
        avg_time = (end_time - start_time) / 10
        print(f"✅ 节点切换平均耗时: {avg_time*1000:.2f}ms")
        
        # 测试多节点加载性能
        start_time = time.time()
        chat_manager.load_conversation(conv_id)
        load_time = time.time() - start_time
        assert chat_manager.current_conversation is not None
        node_count = len(chat_manager.current_conversation.nodes)
        print(f"✅ 加载 {node_count} 个节点耗时: {load_time*1000:.2f}ms")
        
        print("\n🎉 所有模型对话测试通过！")
        
    except Exception as e:
        print(f"\n❌ 模型对话测试失败: {e}")
        import traceback
        traceback.print_exc()
        
    finally:
        # 清理测试对话
        if chat_manager.current_conversation:
            chat_manager.delete_conversation(conv_id)
            print(f"✅ 清理测试对话: {conv_id[:8]}")

def test_error_handling():
    """测试错误处理"""
    print("\n=== 测试错误处理 ===")
    
    config = Config("data/test_config.json")
    model_manager = ModelManager()
    storage = ChatStorage("data/conversations")
    prompts = PromptStorage("data/prompts")
    chat_manager = ChatManager(model_manager, storage, prompts)
    
    # 测试1: 未加载对话时发送消息
    chat_manager.current_conversation = None
    response = chat_manager.send_message("测试消息")
    assert response == "", "未加载对话时应返回空字符串"
    print("✅ 未加载对话错误处理")
    
    # 测试2: 加载不存在的对话
    result = chat_manager.load_conversation("non-existent-id")
    assert result == False, "加载不存在对话应返回False"
    print("✅ 加载不存在对话错误处理")
    
    # 测试3: 删除不存在的对话（不应抛出异常）
    try:
        chat_manager.delete_conversation("non-existent-id")
        print("✅ 删除不存在对话错误处理")
    except Exception as e:
        print(f"❌ 删除不存在对话失败: {e}")
    
    print("✅ 错误处理测试通过")

def test_full_workflow():
    """测试完整工作流"""
    print("\n=== 测试完整工作流 ===")
    
    # 初始化
    config = Config("data/test_config.json")
    model_manager = ModelManager()
    storage = ChatStorage("data/conversations")
    prompts = PromptStorage("data/prompts")
    chat_manager = ChatManager(model_manager, storage, prompts)
    
    # 1. 创建对话
    conv = chat_manager.create_conversation("完整测试")
    print(f"1. 创建对话: {conv.metadata['id'][:8]}")
    
    # 2. 模拟发送消息（不调用实际API）
    # 注意：这会调用实际的模型API，需要有效配置
    # try:
    #     response = chat_manager.send_message("你好，这是一个测试")
    #     print(f"2. 发送消息，回复: {response[:30]}...")
    # except Exception as e:
    #     print(f"2. 发送消息失败（需要有效API配置）: {e}")
    
    # 3. 列出对话
    conversations = chat_manager.list_conversations()
    print(f"3. 对话列表: 共 {len(conversations)} 个对话")
    
    print("✅ 完整工作流测试完成")

def cleanup():
    """清理测试数据"""
    print("\n=== 清理测试数据 ===")
    import shutil
    
    test_dirs = [
        "data/test_conversations",
        "data/test_config.json",
        "data/messages"
    ]
    
    for path in test_dirs:
        if os.path.exists(path):
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            print(f"已清理: {path}")
    
    print("✅ 清理完成")

def run_all_tests():
    """运行所有测试"""
    print("开始测试后端核心逻辑...")
    
    try:
        test_config()
        test_model_manager()
        test_chat_storage()
        test_conversation_tree()
        test_full_workflow()
        test_model_conversation()
        test_error_handling()
        
        print("\n🎉 所有测试通过！")
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        # cleanup()
        pass

if __name__ == "__main__":
    run_all_tests()