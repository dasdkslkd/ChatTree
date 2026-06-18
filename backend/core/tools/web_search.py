import json
import html
import re
from urllib.parse import urljoin
from typing import Any, Dict, List
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

    def _headers(self, accept: str) -> Dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36 ChatTree/0.1"
            ),
            "Accept": accept,
            "Accept-Language": f"{self.default_language},zh;q=0.9,en;q=0.8",
        }

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

        try:
            data = await self._search_json(url, params)
            output = self._format_json_results(data, query, page, num_results)
            return json.dumps(output, ensure_ascii=False, indent=2)
        except Exception as json_error:
            logger.warning(f"SearXNG JSON search failed, falling back to HTML: {json_error}")
            try:
                html_params = dict(params)
                html_params.pop("format", None)
                data = await self._search_html(url, html_params, query, page, num_results)
                return json.dumps(data, ensure_ascii=False, indent=2)
            except Exception as html_error:
                logger.error(f"SearXNG search failed: json={json_error}; html={html_error}")
                return json.dumps({
                    "error": str(html_error),
                    "json_error": str(json_error),
                    "query": query,
                    "hint": (
                        "SearXNG JSON API may be disabled or forbidden. "
                        "HTML fallback also failed; check the local SearXNG instance."
                    ),
                }, ensure_ascii=False)

    async def _search_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        client = await self._get_client()
        resp = await client.get(
            url,
            params=params,
            headers=self._headers("application/json,text/html;q=0.8,*/*;q=0.5"),
        )
        resp.raise_for_status()
        return resp.json()

    async def _search_html(
        self,
        url: str,
        params: Dict[str, Any],
        query: str,
        page: int,
        num_results: int,
    ) -> Dict[str, Any]:
        client = await self._get_client()
        resp = await client.get(
            url,
            params=params,
            headers=self._headers("text/html,application/xhtml+xml"),
        )
        resp.raise_for_status()
        results = self._parse_searxng_html(resp.text, num_results)
        return {
            "query": query,
            "page": page,
            "num_results": len(results),
            "total_results": len(results),
            "results": results,
            "source": "searxng_html_fallback",
        }

    def _format_json_results(
        self,
        data: Dict[str, Any],
        query: str,
        page: int,
        num_results: int,
    ) -> Dict[str, Any]:
        results = []
        for item in data.get("results", [])[:num_results]:
            result = {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
                "engine": item.get("engine", ""),
            }
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
            "source": "searxng_json",
        }
        if data.get("suggestions"):
            output["suggestions"] = data["suggestions"][:5]
        return output

    def _parse_searxng_html(self, content: str, limit: int) -> List[Dict[str, Any]]:
        articles = re.findall(
            r'<article\b[^>]*class="[^"]*\bresult\b[^"]*"[^>]*>(.*?)</article>',
            content,
            flags=re.IGNORECASE | re.DOTALL,
        )
        results: List[Dict[str, Any]] = []
        for article in articles:
            title_match = re.search(r"<h3[^>]*>\s*<a\b[^>]*>(.*?)</a>\s*</h3>", article, flags=re.IGNORECASE | re.DOTALL)
            href_match = re.search(r'<a\b[^>]*class="[^"]*\burl_header\b[^"]*"[^>]*href="([^"]+)"', article, flags=re.IGNORECASE)
            if not href_match:
                href_match = re.search(r"<h3[^>]*>\s*<a\b[^>]*href=\"([^\"]+)\"", article, flags=re.IGNORECASE | re.DOTALL)
            snippet_match = re.search(r'<p\b[^>]*class="[^"]*\bcontent\b[^"]*"[^>]*>(.*?)</p>', article, flags=re.IGNORECASE | re.DOTALL)
            engine_block = re.search(r'<div\b[^>]*class="[^"]*\bengines\b[^"]*"[^>]*>(.*?)</div>', article, flags=re.IGNORECASE | re.DOTALL)

            title = self._strip_html(title_match.group(1)) if title_match else ""
            result_url = html.unescape(href_match.group(1)) if href_match else ""
            snippet = self._strip_html(snippet_match.group(1)) if snippet_match else ""
            engine = ""
            if engine_block:
                engines = [self._strip_html(match) for match in re.findall(r"<span[^>]*>(.*?)</span>", engine_block.group(1), flags=re.IGNORECASE | re.DOTALL)]
                engine = ",".join([item for item in engines if item])

            if not title and not result_url:
                continue
            results.append({
                "title": title,
                "url": urljoin(self.searxng_url, result_url),
                "snippet": snippet,
                "engine": engine,
            })
            if len(results) >= limit:
                break
        return results

    def _strip_html(self, value: str) -> str:
        text = re.sub(r"<[^>]+>", " ", value)
        text = html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()


    async def close(self):
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None


class FetchUrlTool(BaseTool):

    def __init__(self, config: Dict[str, Any]):
        self.timeout = config.get('timeout', 30)
        self.max_content_length = config.get('max_content_length', 8000)
        self.use_crawl4ai = config.get('use_crawl4ai', False)
        self._http_client = None

    async def _get_client(self):
        if self._http_client is None:
            import httpx
            self._http_client = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0 Safari/537.36 ChatTree/0.1"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/json,text/plain,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            )
        return self._http_client

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
            logger.info(f"Fetching URL: {url}")
            output = await self._fetch_with_http(url)
            return json.dumps(output, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"HTTP fetch failed for {url}: {e}")
            if self.use_crawl4ai:
                try:
                    output = await self._fetch_with_crawl4ai(url)
                    return json.dumps(output, ensure_ascii=False, indent=2)
                except Exception as fallback_error:
                    logger.error(f"Failed to fetch {url}: http={e}; crawl4ai={fallback_error}")
                    return json.dumps({
                        "error": str(fallback_error) or fallback_error.__class__.__name__,
                        "http_error": str(e) or e.__class__.__name__,
                        "url": url,
                    }, ensure_ascii=False)
            logger.error(f"Failed to fetch {url}: {e}")
            return json.dumps({
                "error": str(e) or e.__class__.__name__,
                "url": url,
            }, ensure_ascii=False)

    async def _fetch_with_http(self, url: str) -> Dict[str, Any]:
        client = await self._get_client()
        resp = await client.get(url)
        resp.raise_for_status()

        content_type = (resp.headers.get("content-type") or "").lower()
        raw_text = resp.text
        if "text/html" in content_type or "<html" in raw_text[:500].lower():
            title = self._extract_title(raw_text)
            content = self._html_to_text(raw_text)
        else:
            title = ""
            content = raw_text

        content = self._truncate(content)
        return {
            "url": str(resp.url),
            "title": title,
            "content": content,
            "success": True,
            "status_code": resp.status_code,
            "content_type": content_type,
            "source": "http",
        }

    async def _fetch_with_crawl4ai(self, url: str) -> Dict[str, Any]:
        from crawl4ai import AsyncWebCrawler, CrawlerRunConfig

        config = CrawlerRunConfig()
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=url, config=config)

        content = result.markdown if result.markdown else (result.text or "")
        return {
            "url": url,
            "title": getattr(result, "title", "") or "",
            "content": self._truncate(content),
            "success": result.success if hasattr(result, "success") else True,
            "source": "crawl4ai",
        }

    def _extract_title(self, content: str) -> str:
        match = re.search(r"<title[^>]*>(.*?)</title>", content, flags=re.IGNORECASE | re.DOTALL)
        return self._strip_html(match.group(1)) if match else ""

    def _html_to_text(self, content: str) -> str:
        body_match = re.search(r"<body[^>]*>(.*?)</body>", content, flags=re.IGNORECASE | re.DOTALL)
        text = body_match.group(1) if body_match else content
        text = re.sub(r"(?is)<(script|style|noscript|svg|canvas|iframe)\b.*?</\1>", " ", text)
        text = re.sub(r"(?is)<!--.*?-->", " ", text)
        text = re.sub(r"(?i)<\s*br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</\s*(p|div|section|article|header|footer|li|h[1-6]|tr)\s*>", "\n", text)
        text = self._strip_html(text)
        return text

    def _strip_html(self, value: str) -> str:
        text = re.sub(r"<[^>]+>", " ", value)
        text = html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    def _truncate(self, content: str) -> str:
        if len(content) <= self.max_content_length:
            return content
        return content[:self.max_content_length] + f"\n\n[Content truncated, total {len(content)} characters]"

    async def close(self):
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None



