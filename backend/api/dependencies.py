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


def get_tool_manager(request: Request):
    """获取工具管理器"""
    try:
        if not hasattr(request.app.state, 'tool_manager'):
            logger.error("❌ tool_manager 未在 app.state 中初始化")
            raise HTTPException(status_code=500, detail="工具管理器未初始化")
        return request.app.state.tool_manager
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 获取 tool_manager 失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"依赖注入错误: {str(e)}")


def get_approval_manager(request: Request):
    """获取工具审批管理器"""
    try:
        if not hasattr(request.app.state, 'approval_manager'):
            logger.error("❌ approval_manager 未在 app.state 中初始化")
            raise HTTPException(status_code=500, detail="工具审批管理器未初始化")
        return request.app.state.approval_manager
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 获取 approval_manager 失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"依赖注入错误: {str(e)}")


def get_capability_registry(request: Request):
    """获取能力注册表"""
    try:
        if not hasattr(request.app.state, 'capability_registry'):
            logger.error("❌ capability_registry 未在 app.state 中初始化")
            raise HTTPException(status_code=500, detail="能力注册表未初始化")
        return request.app.state.capability_registry
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 获取 capability_registry 失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"依赖注入错误: {str(e)}")


def get_run_manager(request: Request):
    """获取运行管理器"""
    try:
        if not hasattr(request.app.state, 'run_manager'):
            logger.error("❌ run_manager 未在 app.state 中初始化")
            raise HTTPException(status_code=500, detail="运行管理器未初始化")
        return request.app.state.run_manager
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 获取 run_manager 失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"依赖注入错误: {str(e)}")


def get_task_ledger(request: Request):
    """获取 TaskLedger"""
    try:
        if not hasattr(request.app.state, 'task_ledger'):
            logger.error("❌ task_ledger 未在 app.state 中初始化")
            raise HTTPException(status_code=500, detail="TaskLedger 未初始化")
        return request.app.state.task_ledger
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 获取 task_ledger 失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"依赖注入错误: {str(e)}")


def get_plan_ledger(request: Request):
    """获取 PlanLedger"""
    try:
        if not hasattr(request.app.state, 'plan_ledger'):
            logger.error("❌ plan_ledger 未在 app.state 中初始化")
            raise HTTPException(status_code=500, detail="PlanLedger 未初始化")
        return request.app.state.plan_ledger
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 获取 plan_ledger 失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"依赖注入错误: {str(e)}")


def get_persistence(request: Request):
    """获取 SQLite persistence。"""
    try:
        if not hasattr(request.app.state, 'persistence'):
            logger.error("❌ persistence 未在 app.state 中初始化")
            raise HTTPException(status_code=500, detail="Persistence 未初始化")
        return request.app.state.persistence
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 获取 persistence 失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"依赖注入错误: {str(e)}")


def get_transcript_projection(request: Request):
    """获取 TranscriptProjection。"""
    try:
        if not hasattr(request.app.state, 'transcript_projection'):
            logger.error("❌ transcript_projection 未在 app.state 中初始化")
            raise HTTPException(status_code=500, detail="TranscriptProjection 未初始化")
        return request.app.state.transcript_projection
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 获取 transcript_projection 失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"依赖注入错误: {str(e)}")


def get_subagent_executor(request: Request):
    """获取 Subagent 执行器"""
    try:
        if not hasattr(request.app.state, 'subagent_executor'):
            logger.error("❌ subagent_executor 未在 app.state 中初始化")
            raise HTTPException(status_code=500, detail="Subagent 执行器未初始化")
        return request.app.state.subagent_executor
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 获取 subagent_executor 失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"依赖注入错误: {str(e)}")


def get_agent_runtime(request: Request):
    """获取 Agent runtime"""
    try:
        if not hasattr(request.app.state, 'agent_runtime'):
            logger.error("❌ agent_runtime 未在 app.state 中初始化")
            raise HTTPException(status_code=500, detail="Agent runtime 未初始化")
        return request.app.state.agent_runtime
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 获取 agent_runtime 失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"依赖注入错误: {str(e)}")


def get_agent_mailbox(request: Request):
    """获取 Agent mailbox"""
    try:
        if not hasattr(request.app.state, 'agent_mailbox'):
            logger.error("❌ agent_mailbox 未在 app.state 中初始化")
            raise HTTPException(status_code=500, detail="Agent mailbox 未初始化")
        return request.app.state.agent_mailbox
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 获取 agent_mailbox 失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"依赖注入错误: {str(e)}")


def get_workflow_manager(request: Request):
    """获取 Workflow 管理器"""
    try:
        if not hasattr(request.app.state, 'workflow_manager'):
            logger.error("❌ workflow_manager 未在 app.state 中初始化")
            raise HTTPException(status_code=500, detail="Workflow 管理器未初始化")
        return request.app.state.workflow_manager
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ 获取 workflow_manager 失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"依赖注入错误: {str(e)}")


def get_command_executor(request: Request):
    """获取 Command 执行器；未初始化时返回 None 以兼容旧测试/轻量路由。"""
    return getattr(request.app.state, 'command_executor', None)


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
