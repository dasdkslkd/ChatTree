from typing import Any, Dict, List, Optional, Tuple


def parse_data_image_url(url: str) -> Optional[Tuple[str, str]]:
    prefix = "data:"
    marker = ";base64,"
    if not isinstance(url, str) or not url.startswith(prefix) or marker not in url:
        return None
    header, data = url[len(prefix):].split(marker, 1)
    if not header.startswith("image/") or not data:
        return None
    return header, data


def to_openai_responses_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    converted: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            converted.append({"type": "input_text", "text": str(block)})
            continue
        block_type = block.get("type")
        if block_type == "text":
            converted.append({"type": "input_text", "text": block.get("text", "")})
        elif block_type == "image_url":
            image_url = (block.get("image_url") or {}).get("url")
            if image_url:
                converted.append({"type": "input_image", "image_url": image_url})
        else:
            converted.append(block)
    return converted


def to_anthropic_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    converted: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            converted.append({"type": "text", "text": str(block)})
            continue
        block_type = block.get("type")
        if block_type == "text":
            converted.append({"type": "text", "text": block.get("text", "")})
        elif block_type == "image_url":
            parsed = parse_data_image_url((block.get("image_url") or {}).get("url"))
            if parsed:
                media_type, data = parsed
                converted.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": data,
                    },
                })
        else:
            converted.append(block)
    return converted


def to_gemini_parts(content: Any) -> List[Dict[str, Any]]:
    if not isinstance(content, list):
        return [{"text": str(content)}]
    parts: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append({"text": str(block)})
            continue
        block_type = block.get("type")
        if block_type == "text":
            parts.append({"text": block.get("text", "")})
        elif block_type == "image_url":
            parsed = parse_data_image_url((block.get("image_url") or {}).get("url"))
            if parsed:
                mime_type, data = parsed
                parts.append({"inline_data": {"mime_type": mime_type, "data": data}})
        else:
            text = block.get("text") or str(block)
            parts.append({"text": text})
    return parts
