import asyncio, json
from backend.core.tools.tool_manager import ToolManager
from backend.core.config.config import Config

cfg = Config()
tm = ToolManager(cfg.data)
print("Tools:", tm.list_tools())

async def test():
    # Test web_search
    result = await tm.execute_tool("web_search", {"query": "Python programming", "num_results": 3})
    data = json.loads(result)
    print("\n=== web_search ===")
    if "error" in data:
        print("ERROR:", data["error"])
    else:
        print(f"Found {data['num_results']} results")
        for r in data.get("results", []):
            print(f"  - {r['title'][:60]}")
            print(f"    {r['url']}")

asyncio.run(test())