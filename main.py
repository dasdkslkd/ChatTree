#!/usr/bin/env python3
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ---------- 导入路由 ----------
from backend.api.routes import config, conversations, messages, models, prompts

# ---------- 导入核心 ----------
from backend.core.chat.chat_manager import ChatManager
from backend.core.model.model_manager import ModelManager
from backend.core.config.config import Config
from backend.core.storage.chat_storage import ChatStorage
from backend.core.storage.prompt_storage import PromptStorage

app = FastAPI(
    title="AI 对话树后端",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- 挂载管理器 ----------
@app.on_event("startup")
async def startup_event():
    config_manager = Config()
    model_manager = ModelManager()
    chat_storage = ChatStorage()
    prompt_storage = PromptStorage()
    chat_manager = ChatManager(model_manager,chat_storage,prompt_storage)

    app.state.config_manager = config_manager
    app.state.model_manager = model_manager
    app.state.chat_manager = chat_manager

# ---------- 注册路由 ----------
app.include_router(config.router,        prefix="",        tags=["配置"])
app.include_router(conversations.router, prefix="", tags=["对话"])
app.include_router(messages.router,      prefix="", tags=["消息"])
app.include_router(models.router,        prefix="",               tags=["模型"])
app.include_router(prompts.router,        prefix="",               tags=["提示词"])

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)