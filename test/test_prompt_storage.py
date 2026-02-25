import sys
import os
from pathlib import Path
import pdb

# 将backend目录添加到Python路径
backend_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(backend_dir)
sys.path.insert(0, project_root)

from backend.core.storage.prompt_storage import PromptStorage


storage = PromptStorage()

prompt1 = {
    "id": "1",
    "title": "Test Prompt 1",
    "content": "This is the content of test prompt 1.",
}
prompt2 = {
    "id": "2",
    "title": "Test Prompt 2",
    "content": "This is the content of test prompt 2.",
}

storage.save(prompt1)
print(storage.list())
storage.save(prompt2)
print(storage.list())
print(storage.load("1"))
print(storage.load("2"))