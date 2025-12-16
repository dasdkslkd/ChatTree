# backend/api/dependencies.py
from fastapi import Request, HTTPException
import logging

logger = logging.getLogger(__name__)

def get_chat_manager(request: Request):
    """获取聊天管理器"""
    try:
        if not hasattr(request.app.state, 'chat_manager'):
            logger.error("❌ chat_manager 未在 app.state 中初始化")
            raise HTTPException(status_code=500, detail="聊天管理器未初始化")
        manager = request.app.state.chat_manager
        logger.info(f"✅ 获取 chat_manager 成功: {type(manager)}")
        return manager
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 获取 chat_manager 失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"依赖注入错误: {str(e)}")

def get_model_manager(request: Request):
    """获取模型管理器"""
    try:
        if not hasattr(request.app.state, 'model_manager'):
            logger.error("❌ model_manager 未在 app.state 中初始化")
            raise HTTPException(status_code=500, detail="模型管理器未初始化")
        return request.app.state.model_manager
    except Exception as e:
        logger.error(f"❌ 获取 model_manager 失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"依赖注入错误: {str(e)}")

def get_config_manager(request: Request):
    """获取配置管理器"""
    try:
        if not hasattr(request.app.state, 'config_manager'):
            logger.error("❌ config_manager 未在 app.state 中初始化")
            raise HTTPException(status_code=500, detail="配置管理器未初始化")
        return request.app.state.config_manager
    except Exception as e:
        logger.error(f"❌ 获取 config_manager 失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"依赖注入错误: {str(e)}")