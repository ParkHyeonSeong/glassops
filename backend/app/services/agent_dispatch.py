"""Helpers for routing agent-scoped REST calls to either the local backend or a remote agent via RPC."""

from fastapi import HTTPException

from app.config import settings
from app.services import agent_rpc


def is_local(agent_id: str) -> bool:
    return agent_id == settings.local_agent_id


async def call_remote(agent_id: str, method: str, params: dict | None = None) -> dict:
    """Invoke an RPC on a remote agent, mapping protocol errors to HTTPExceptions."""
    try:
        return await agent_rpc.call(agent_id, method, params or {})
    except agent_rpc.AgentNotConnected as e:
        raise HTTPException(503, str(e))
    except agent_rpc.RpcTimeout as e:
        raise HTTPException(504, str(e))
    except agent_rpc.RpcError as e:
        raise HTTPException(400, str(e))
