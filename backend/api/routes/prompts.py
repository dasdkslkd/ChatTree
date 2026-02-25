# backend/api/routes/prompts.py
from fastapi import APIRouter, Depends, HTTPException
from typing import List, Dict
from pydantic import BaseModel
import logging
from ...core.chat.chat_manager import ChatManager
from ..dependencies import get_chat_manager

logger = logging.getLogger(__name__)
router = APIRouter()

class PromptResponse(BaseModel):
    id: str
    title: str
    updated_at: int

class Prompt(BaseModel):
    id: str
    title: str
    content: str

class ListPromptsResponse(BaseModel):
    prompts: List[PromptResponse]

@router.post("/prompts", response_model=Dict[str, str])
async def save_prompt(
    request: Prompt,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """保存提示词"""
    try:
        data = {"id": request.id, "title": request.title, "content": request.content}
        chat_manager.prompts.save(data)
        return {"message": "提示词保存成功"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@router.get("/prompts", response_model=ListPromptsResponse)
async def list_prompts(chat_manager: ChatManager = Depends(get_chat_manager)):
    """列出所有提示词"""
    try:
        prompts = chat_manager.prompts.list()
        return {"prompts": prompts}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@router.get("/prompts/{prompt_id}", response_model=Prompt)
async def load_prompt(
    prompt_id: str,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """加载提示词"""
    try:
        content = chat_manager.prompts.load(prompt_id)
        if content is None:
            raise HTTPException(status_code=404, detail="提示词未找到")
        prompt_info = chat_manager.prompts.index.get(prompt_id, {})
        return {
            "id": prompt_id,
            "title": prompt_info.get("title", ""),
            "content": content
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/prompts/{prompt_id}", response_model=Dict[str, str])
async def delete_prompt(
    prompt_id: str,
    chat_manager: ChatManager = Depends(get_chat_manager)
):
    """删除提示词"""
    try:
        if not chat_manager.prompts.exists(prompt_id):
            raise HTTPException(status_code=404, detail="提示词未找到")
        chat_manager.prompts.delete(prompt_id)
        return {"message": "提示词删除成功"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))