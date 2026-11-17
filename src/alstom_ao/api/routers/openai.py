"""OpenAI-compatible surface: the orchestration layer as a *virtual model*.

The UI can register ``alstom-ao`` as any other chat model and keep its existing
streaming/usage handling. Orchestration metadata (run id, trace id, selected
agents, approvals) is returned alongside the standard OpenAI fields.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from alstom_ao.api.deps import (
    AppContainer,
    get_container,
    get_run_service,
    resolve_identity,
)
from alstom_ao.api.schemas import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ModelInfo,
    ModelListResponse,
)
from alstom_ao.errors import InputValidationError
from alstom_ao.orchestrator.service import RunService

router = APIRouter(prefix="/v1", tags=["openai-compat"])

SSE_CHUNK_SIZE = 320


def _usage(run) -> dict[str, int]:
    usage = run.usage or {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens", 0)),
        "completion_tokens": int(usage.get("completion_tokens", 0)),
        "total_tokens": int(usage.get("total_tokens", 0)),
    }


def _metadata(run) -> dict:
    return {
        "run_id": run.run_id,
        "workflow_id": run.workflow_id,
        "trace_id": run.trace_id,
        "status": str(run.status),
        "selected_agents": [agent.agent_id for agent in (run.plan.selected_agents if run.plan else [])],
        "plan_source": run.plan.source if run.plan else None,
        "confidence": run.plan.confidence if run.plan else None,
        "approval": (
            {
                "approval_id": run.approval.approval_id,
                "status": str(run.approval.status),
                "risk_level": str(run.approval.risk_level),
                "reasons": run.approval.reasons,
                "decision_endpoint": f"/v1/approvals/{run.approval.approval_id}/decision",
            }
            if run.approval
            else None
        ),
        "langfuse_url": run.langfuse_url,
        "duration_ms": run.duration_ms,
        "policy_denials": [
            {"agent_id": decision.agent_id, "reasons": decision.reasons}
            for decision in run.policy_decisions
            if not decision.allowed or decision.reasons
        ],
    }


async def _sse_stream(
    *,
    completion_id: str,
    created: int,
    model: str,
    content: str,
    metadata: dict,
    usage: dict[str, int],
) -> AsyncIterator[str]:
    def frame(payload: dict) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield frame(
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    )
    for start in range(0, len(content), SSE_CHUNK_SIZE):
        yield frame(
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": content[start : start + SSE_CHUNK_SIZE]},
                        "finish_reason": None,
                    }
                ],
            }
        )
    yield frame(
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": usage,
            "alstom_ao": metadata,
        }
    )
    yield "data: [DONE]\n\n"


@router.get("/models", response_model=ModelListResponse)
async def list_models(
    container: AppContainer = Depends(get_container),
) -> ModelListResponse:
    now = int(time.time())
    model_id = container.settings.virtual_model_id
    return ModelListResponse(
        data=[
            ModelInfo(id=model_id, created=now),
            ModelInfo(id=f"{model_id}:{container.settings.environment}", created=now),
        ]
    )


@router.post("/chat/completions")
async def chat_completions(
    payload: ChatCompletionRequest,
    request: Request,
    container: AppContainer = Depends(get_container),
    run_service: RunService = Depends(get_run_service),
):
    identity = resolve_identity(request, container.settings, fallback_user_id=payload.user)
    query = payload.last_user_message.strip()
    if not query:
        raise InputValidationError("A user message with content is required.")

    run = await run_service.start(
        query=query,
        identity=identity,
        use_case=(payload.metadata or {}).get("use_case")
        or request.headers.get("x-use-case")
        or "virtual_model",
        conversation=payload.conversation,
        trace_id=request.headers.get("x-trace-id"),
        workflow_id=request.headers.get("x-workflow-id"),
    )

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:20]}"
    created = int(time.time())
    model = payload.model or container.settings.virtual_model_id
    metadata = _metadata(run)
    usage = _usage(run)

    if payload.stream:
        return StreamingResponse(
            _sse_stream(
                completion_id=completion_id,
                created=created,
                model=model,
                content=run.response,
                metadata=metadata,
                usage=usage,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return ChatCompletionResponse(
        id=completion_id,
        created=created,
        model=model,
        choices=[
            ChatCompletionChoice(message={"role": "assistant", "content": run.response}, finish_reason="stop")
        ],
        usage=usage,
        alstom_ao=metadata,
    )
