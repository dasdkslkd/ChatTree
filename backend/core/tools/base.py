# tools/base.py - 工具基类
from abc import ABC, abstractmethod
from typing import Any, Dict


class BaseTool(ABC):
    """工具基类，所有工具必须继承"""

    @property
    @abstractmethod
    def name(self) -> str:
        """工具名称"""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """工具描述"""
        pass

    @abstractmethod
    def parameters_schema(self) -> Dict[str, Any]:
        """返回 JSON Schema 格式的参数定义"""
        pass

    @abstractmethod
    async def execute(self, **kwargs) -> str:
        """执行工具，返回文本结果"""
        pass

    def to_openai_tool(self) -> Dict[str, Any]:
        """转换为 OpenAI function calling 格式"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema(),
            }
        }
