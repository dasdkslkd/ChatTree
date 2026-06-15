import asyncio, json, sys
sys.path.insert(0, ".")
from backend.core.model.model_manager import ModelManager
from backend.core.config.config import Config
from backend.core.tools.tool_manager import ToolManager
from backend.core.config.types import StreamController

cfg = Config()
mm = ModelManager()
provider = mm.get_model("openai", is_async=True)
tm = ToolManager(cfg.data)
tools = tm.get_openai_tools()

async def test():
    messages = [{"role": "user", "content": "Search for today weather in Shanghai"}]
    controller = StreamController(node_id="test-node", conversation_id="test-conv")
    
    print("=== Streaming with tools ===")
    chunk_count = 0
    tool_calls_found = None
    async for chunk in provider.generate_response_stream(
        model="gpt-5.4-mini",
        messages=messages,
        stream_controller=controller,
        tools=tools,
    ):
        status = chunk.get("status")
        if status == "content":
            chunk_count += 1
        if status == "complete":
            tool_calls_found = chunk.get("tool_calls")
            print("COMPLETE: %d content chunks" % chunk_count)
            print("tool_calls: %s" % json.dumps(tool_calls_found, ensure_ascii=False, indent=2))
        if status == "error":
            print("ERROR: %s" % chunk.get("error"))

asyncio.run(test())