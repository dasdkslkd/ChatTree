import asyncio, json, sys
sys.path.insert(0, ".")

from backend.core.chat.chat_manager import ChatManager
from backend.core.model.model_manager import ModelManager
from backend.core.config.config import Config
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage
from backend.core.tools.tool_manager import ToolManager

cfg = Config()
mm = ModelManager()
storage = ChatStorage()
prompts = PromptStorage()
tm = ToolManager(cfg.data)
cm = ChatManager(mm, storage, prompts, tm)

# Create conversation
conv = cm.create_conversation("debug test")
conv_id = conv.metadata["id"]
print("Conv ID:", conv_id)

# Check tools
tools = cm.tool_manager.get_openai_tools()
print("Tools:", [t["function"]["name"] for t in tools])

# Prepare messages like the API would
provider = mm.get_model("openai", is_async=True)
print("Provider:", type(provider).__name__)

# Simulate what send_message_stream does
async def test():
    from backend.core.chat.node import NodeManager
    from backend.core.config.types import Message, Role, StreamController
    from time import time
    import uuid
    
    user_msg = Message({
        "id": str(uuid.uuid4()),
        "role": Role.USER,
        "content": "Search for today weather in Shanghai",
        "name": None, "tool_calls": None, "tool_call_id": None,
        "timestamp": int(time())
    })
    
    current_node_id = conv.current_node_id
    new_node = NodeManager.create_node(user_message=user_msg, parent_id=current_node_id, model_id="gpt-5.4-mini")
    conv.add_node(new_node, parent_id=current_node_id)
    
    # Prepare messages
    messages = cm._prepare_messages_for_api_with_conversation(conv)
    print("Messages count:", len(messages))
    for m in messages:
        print("  role=%s content=%s" % (m["role"], m["content"][:50]))
    
    # Prepare API format
    api_messages = cm._format_messages_for_api(messages)
    print("API messages count:", len(api_messages))
    for m in api_messages:
        print("  role=%s content=%s" % (m["role"], str(m.get("content",""))[:50]))
    
    # Stream with tools
    controller = StreamController(node_id=new_node["id"], conversation_id=conv_id)
    
    print("\n=== Streaming ===")
    async for chunk in provider.generate_response_stream(
        model="gpt-5.4-mini",
        messages=api_messages,
        stream_controller=controller,
        tools=tools,
    ):
        status = chunk.get("status")
        if status == "complete":
            tc = chunk.get("tool_calls")
            print("COMPLETE - tool_calls:", json.dumps(tc, ensure_ascii=False) if tc else "None")
        elif status == "error":
            print("ERROR:", chunk.get("error"))
        elif status == "content":
            pass  # skip content output

asyncio.run(test())