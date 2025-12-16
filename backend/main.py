# backend/main.py
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from datetime import datetime
import os
import sys

# 将backend目录添加到Python路径
backend_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(backend_dir)
sys.path.insert(0, project_root)

from backend.api.routes import conversations, messages, models, config
from backend.core.chat.chat_manager import ChatManager
from backend.core.model.model_manager import ModelManager
from backend.core.storage.json_storage import ChatStorage
from backend.core.config.config import Config
from backend.core.config.types import ChatConfig
from backend.core.utils.logger import setup_logger

logger = setup_logger('FastAPI')

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    try:
        logger.info("=" * 50)
        logger.info("开始初始化应用...")
        
        # 确保data目录存在
        os.makedirs("data/conversations", exist_ok=True)
        
        # 初始化配置管理器
        logger.info("加载配置...")
        config_manager = Config("data/config.json")
        logger.info(f"配置加载完成: {config_manager.data}")
        
        # 初始化存储
        logger.info("初始化存储...")
        storage = ChatStorage("data/conversations")
        logger.info("存储初始化完成")
        
        # 初始化模型管理器
        logger.info("初始化模型管理器...")
        model_manager = ModelManager()
        
        # 加载默认模型配置
        default_provider = config_manager.data.get('default_provider')
        if default_provider:
            model_manager.set_current_model(default_provider)
            logger.info(f"已加载默认模型提供商: {default_provider}")
        
        # 初始化聊天管理器
        logger.info("初始化聊天管理器...")
        chat_config = ChatConfig(
            save_history=True,
            max_history_messages=50,
            system_prompt=config_manager.data.get('system_prompt', '')
        )
        logger.info(f"聊天配置: {chat_config}")
        
        chat_manager = ChatManager(model_manager, storage, chat_config)
        logger.info("聊天管理器初始化完成")
        
        # 将实例挂载到 app.state
        app.state.chat_manager = chat_manager
        app.state.config_manager = config_manager
        app.state.model_manager = model_manager
        
        logger.info("应用状态挂载完成")
        logger.info("=" * 50)
        logger.info("FastAPI应用启动成功！")
        
    except Exception as e:
        logger.error(f"应用初始化失败: {e}", exc_info=True)
        raise
    
    yield  # 应用运行中
    
    # 关闭时清理
    logger.info("FastAPI应用关闭")

app = FastAPI(
    title="AI对话管理API",
    description="支持多模型、树形对话管理的API服务",
    version="1.0.0",
    lifespan=lifespan
)

# CORS配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(conversations.router, prefix="/api/conversations", tags=["conversations"])
app.include_router(messages.router, prefix="/api/conversations", tags=["messages"])
app.include_router(models.router, prefix="/api/models", tags=["models"])
app.include_router(config.router, prefix="/api/config", tags=["config"])

# 挂载静态文件
frontend_path = os.path.join(project_root, "frontend")
if os.path.exists(frontend_path):
    app.mount("/frontend", StaticFiles(directory=frontend_path, html=True), name="frontend")
    logger.info(f"前端文件已挂载到 /frontend")

@app.get("/")
async def root():
    return {
        "message": "AI对话管理API",
        "version": "1.0.0",
        "docs": "/docs",
        "frontend": "访问 /frontend/index.html 查看前端页面"
    }

@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.middleware("http")
async def log_requests(request: Request, call_next):
    """请求日志中间件"""
    logger.info(f"收到请求: {request.method} {request.url}")
    try:
        response = await call_next(request)
        logger.info(f"响应状态: {response.status_code}")
        return response
    except Exception as e:
        logger.error(f"请求处理异常: {e}", exc_info=True)
        raise