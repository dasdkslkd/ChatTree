"""导入文件存储方法 + 路径穿越防护 验证。
从仓库根运行：  python test/test_import_files.py
"""
import os
import shutil
import sys
import tempfile
import asyncio
import io
from time import time

sys.path.insert(0, ".")

from starlette.datastructures import Headers, UploadFile

from backend.api.routes.conversations import read_import_file, upload_import_file
from backend.core.chat.chat_manager import ChatManager
from backend.core.chat.canonical_reader import messages_by_node
from backend.core.config.types import Message, Role
from backend.core.model.providers.anthropic_provider import AnthropicProvider
from backend.core.model.providers.gemini_provider import GeminiProvider
from backend.core.model.providers.openai_compatible import OpenAICompatibleProvider
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage


class _DummyModelManager:
    def get_provider_for_model(self, model):
        raise AssertionError("provider should not be needed")


def main():
    tmp = tempfile.mkdtemp(prefix="chattree_imp_")
    try:
        storage = ChatStorage(storage_dir=os.path.join(tmp, "conversations"))
        cid = "conv-1"

        # save + read
        storage.save_import_file(cid, "notes.txt", b"hello world")
        assert storage.read_import_file(cid, "notes.txt") == "hello world", "读回内容不符"

        # list
        listed = storage.list_import_files(cid)
        assert any(f["filename"] == "notes.txt" and f["size"] == 11 for f in listed), f"list 不正确: {listed}"

        # 路径穿越：read 越界返回 None
        assert storage.read_import_file(cid, "../../../main.py") is None, "路径穿越未被拦截 (read)"
        assert storage.read_import_file(cid, "..\\..\\..\\main.py") is None, "路径穿越未被拦截 (read win)"

        # 路径穿越：save 越界抛 ValueError
        try:
            storage.save_import_file(cid, "../escape.txt", b"x")
            raise AssertionError("路径穿越未被拦截 (save)")
        except ValueError:
            pass

        # delete：存在 -> True，再次 -> False
        assert storage.delete_import_file(cid, "notes.txt") is True, "首次删除应为 True"
        assert storage.delete_import_file(cid, "notes.txt") is False, "重复删除应为 False"
        assert storage.read_import_file(cid, "notes.txt") is None, "删除后应读不到"

        # 越界 delete 返回 False（不抛）
        assert storage.delete_import_file(cid, "../../../main.py") is False, "越界删除应为 False"

        print("PASS: 导入文件 CRUD + 路径穿越防护正确")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_image_references_are_injected_as_multimodal_user_content():
    tmp = tempfile.mkdtemp(prefix="chattree_image_context_")
    try:
        storage = ChatStorage(storage_dir=os.path.join(tmp, "conversations"))
        prompts = PromptStorage(storage_dir=os.path.join(tmp, "prompts"))
        manager = ChatManager(_DummyModelManager(), storage, prompts)
        conv = manager.create_conversation("image references")
        storage.save_import_file(conv.metadata["id"], "diagram.png", b"\x89PNG\r\n\x1a\n")

        current_node_id = asyncio.run(
            manager.create_visible_user_anchor_node(
                conversation_id=conv.metadata["id"],
                content="看看这张图",
                parent_node_id=conv.current_node_id,
                model_id="fake-model",
                slash_metadata=None,
            )
        )
        user_messages = messages_by_node(manager.chat_repository, conv.metadata["id"], [current_node_id])[current_node_id]
        user_message_id = next(message["id"] for message in user_messages if message.get("role") == "user")
        manager.chat_repository.add_message(
            conv.metadata["id"],
            current_node_id,
            role="user",
            content="看看这张图",
            metadata={"image_refs": [{"filename": "diagram.png", "mime_type": "image/png"}]},
            message_id=user_message_id,
        )

        messages = manager._prepare_messages_for_api_with_conversation(manager.get_conversation(conv.metadata["id"]))

        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"][0] == {"type": "text", "text": "看看这张图"}
        assert messages[-1]["content"][1]["type"] == "image_url"
        assert messages[-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_upload_import_accepts_images_as_image_references():
    tmp = tempfile.mkdtemp(prefix="chattree_image_upload_")
    try:
        storage = ChatStorage(storage_dir=os.path.join(tmp, "conversations"))
        prompts = PromptStorage(storage_dir=os.path.join(tmp, "prompts"))
        manager = ChatManager(_DummyModelManager(), storage, prompts)
        conv = manager.create_conversation("image upload")
        upload = UploadFile(
            filename="diagram.png",
            file=io.BytesIO(b"\x89PNG\r\n\x1a\n"),
            headers=Headers({"content-type": "image/png"}),
        )

        result = asyncio.run(upload_import_file(conv.metadata["id"], upload, manager))

        assert result["filename"] == "diagram.png"
        assert result["kind"] == "image"
        assert result["mime_type"] == "image/png"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_read_import_returns_binary_image_response():
    tmp = tempfile.mkdtemp(prefix="chattree_image_read_")
    try:
        storage = ChatStorage(storage_dir=os.path.join(tmp, "conversations"))
        prompts = PromptStorage(storage_dir=os.path.join(tmp, "prompts"))
        manager = ChatManager(_DummyModelManager(), storage, prompts)
        conv = manager.create_conversation("image read")
        storage.save_import_file(conv.metadata["id"], "diagram.png", b"\x89PNG\r\n\x1a\n")

        response = asyncio.run(read_import_file(conv.metadata["id"], "diagram.png", manager))

        assert response.media_type == "image/png"
        assert response.body == b"\x89PNG\r\n\x1a\n"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_multimodal_image_blocks_convert_for_provider_formats():
    image_url = "data:image/png;base64,aGVsbG8="
    messages = [Message({
        "id": "m",
        "role": Role.USER,
        "content": [
            {"type": "text", "text": "看看这张图"},
            {"type": "image_url", "image_url": {"url": image_url}},
        ],
        "timestamp": int(time()),
    })]

    openai = OpenAICompatibleProvider({"api_key": "test", "api_format": "responses"})
    _, responses_input = openai._convert_messages_to_responses_input(messages)
    assert responses_input[0]["content"] == [
        {"type": "input_text", "text": "看看这张图"},
        {"type": "input_image", "image_url": image_url},
    ]

    anthropic = AnthropicProvider({"api_key": "test"})
    _, anthropic_messages = anthropic._convert_messages(messages)
    assert anthropic_messages[0]["content"] == [
        {"type": "text", "text": "看看这张图"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="}},
    ]

    gemini = GeminiProvider({"api_key": "test"})
    _, gemini_messages = gemini._convert_messages(messages)
    assert gemini_messages[0]["parts"] == [
        {"text": "看看这张图"},
        {"inline_data": {"mime_type": "image/png", "data": "aGVsbG8="}},
    ]


if __name__ == "__main__":
    main()
