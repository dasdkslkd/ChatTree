import json
import asyncio
from typing import Any, Dict, List, Optional
from .base import BaseTool
from ..utils.logger import setup_logger

logger = setup_logger('WebSearch')


class WebSearchTool(BaseTool):

    def __init__(self, config: Dict[str, Any]):
        self.searxng_url = config.get('searxng_url', 'http://localhost:8888')
        self.default_engines = config.get('engines', '')
        self.default_language = config.get('language', 'zh-CN')
        self.max_results = config.get('max_results', 10)
        self.timeout = config.get('timeout', 15)
        self._http_client = None

    async def _get_client(self):
        if self._http_client is None:
            import httpx
            self._http_client = httpx.AsyncClient(timeout=self.timeout)
        return self._http_client

    @property
    def name(self) -> str:
        return 'web_search'

    @property
    def description(self) -> str:
        return (
            'Search the web for up-to-date information. '
            'Returns titles, URLs, and snippets. '
            'Only the query parameter is required; all others have sensible defaults.'
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'query': {
                    'type': 'string',
                    'description': 'Search keywords. Be concise and specific.',
                },
                'num_results': {
                    'type': 'integer',
                    'description': 'Max number of results (1-10). Default is 5.',
                    'default': 5,
                },
                'page': {
                    'type': 'integer',
                    'description': 'Page number for pagination, starting from 1.',
                    'default': 1,
                },
                'language': {
                    'type': 'string',
                    'description': 'Language code such as zh-CN or en-US. Default is zh-CN.',
                    'default': 'zh-CN',
                },
                'time_range': {
                    'type': 'string',
                    'description': 'Limit results to a recent time period.',
                    'enum': ['day', 'week', 'month', 'year'],
                },
            },
            'required': ['query'],
        }
    async def execute(self, **kwargs) -> str:
        query = kwargs.get("query", "")
        num_results = min(kwargs.get("num_results", 5), self.max_results)
        page = max(kwargs.get("page", 1), 1)
        language = kwargs.get("language", self.default_language)
        time_range = kwargs.get("time_range", "")

        if not query:
            return json.dumps({"error": "query is required"}, ensure_ascii=False)

        try:
            client = await self._get_client()
            params = {
                "q": query,
                "format": "json",
                "language": language,
                "pageno": page,
            }
            if self.default_engines:
                params["engines"] = self.default_engines
            if time_range:
                params["time_range"] = time_range

            url = f"{self.searxng_url.rstrip('/')}/search"
            logger.info(f"SearXNG search: query='{query}' page={page}, url={url}")
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

            results = []
            for item in data.get("results", [])[:num_results]:
                result = {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": item.get("content", ""),
                    "engine": item.get("engine", ""),
                }
                # 可选元数据字段
                if item.get("score"):
                    result["score"] = item["score"]
                if item.get("category"):
                    result["category"] = item["category"]
                if item.get("publishedDate"):
                    result["publishedDate"] = item["publishedDate"]
                if item.get("thumbnail"):
                    result["thumbnail"] = item["thumbnail"]
                if item.get("author"):
                    result["author"] = item["author"]
                results.append(result)

            output = {
                "query": query,
                "page": page,
                "num_results": len(results),
                "total_results": data.get("number_of_results", 0),
                "results": results,
            }
            # 搜索建议
            if data.get("suggestions"):
                output["suggestions"] = data["suggestions"][:5]

            return json.dumps(output, ensure_ascii=False, indent=2)

        except Exception as e:
            logger.error(f"SearXNG search failed: {e}")
            return json.dumps({"error": str(e), "query": query}, ensure_ascii=False)


    async def close(self):
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None


class FetchUrlTool(BaseTool):

    def __init__(self, config: Dict[str, Any]):
        self.timeout = config.get('timeout', 30)
        self.max_content_length = config.get('max_content_length', 8000)

    @property
    def name(self) -> str:
        return 'fetch_url'

    @property
    def description(self) -> str:
        return (
            'Fetch and extract the text content of a web page. '
            'Use this after web_search to read a relevant page in detail.'
        )

    def parameters_schema(self) -> Dict[str, Any]:
        return {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'url': {
                    'type': 'string',
                    'description': 'The full URL of the page to fetch.',
                },
            },
            'required': ['url'],
        }
    async def execute(self, **kwargs) -> str:
        url = kwargs.get("url", "")
        if not url:
            return json.dumps({"error": "url is required"}, ensure_ascii=False)

        try:
            from crawl4ai import AsyncWebCrawler, CrawlerRunConfig

            config = CrawlerRunConfig()

            logger.info(f"Fetching URL: {url}")
            async with AsyncWebCrawler() as crawler:
                result = await crawler.arun(url=url, config=config)

            content = result.markdown if result.markdown else (result.text or "")

            # 截断过长内容
            if len(content) > self.max_content_length:
                content = content[:self.max_content_length] + "\n\n[Content truncated...]"

            output = {
                "url": url,
                "title": getattr(result, "title", "") or "",
                "content": content,
                "success": result.success if hasattr(result, "success") else True,
            }
            return json.dumps(output, ensure_ascii=False, indent=2)

        except ImportError:
            logger.error("crawl4ai not installed. Run: pip install crawl4ai")
            return json.dumps({
                "error": "crawl4ai not installed. Run: pip install crawl4ai",
                "url": url,
            }, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to fetch {url}: {e}")
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)



