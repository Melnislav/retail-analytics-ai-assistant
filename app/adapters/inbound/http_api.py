"""FastAPI HTTP inbound adapter with RBAC header extraction and domain exception mapping."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.application.chat_orchestrator import ChatOrchestratorService
from app.application.ports import ObservabilityPort
from app.application.report_service import ReportService
from app.domain.exceptions import (
    DomainError,
    ExternalServiceUnavailableError,
    QueryExecutionError,
    SecurityViolationError,
    SQLSafetyError,
    UnauthorizedDataAccessError,
)
from app.domain.models import (
    ChatRequest,
    ChatResponse,
    ConfirmationRequest,
    SavedReport,
    TraceRecord,
    UserContext,
    UserRole,
)

router = APIRouter(prefix="/api/v1", tags=["Retail Data Analysis Assistant"])


def get_current_user(request: Request) -> UserContext:
    """Return the server-controlled prototype identity, never caller-supplied authority."""
    return request.app.state.container.prototype_user


def get_orchestrator(request: Request) -> ChatOrchestratorService:
    return request.app.state.container.chat_orchestrator


def get_report_service(request: Request) -> ReportService:
    return request.app.state.container.report_service


def get_observability(request: Request) -> ObservabilityPort:
    return request.app.state.container.observability


@router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(
    payload: ChatRequest,
    user: UserContext = Depends(get_current_user),
    orchestrator: ChatOrchestratorService = Depends(get_orchestrator),
) -> ChatResponse:
    try:
        return await orchestrator.handle_message(payload, user)
    except (SecurityViolationError, SQLSafetyError, UnauthorizedDataAccessError) as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.message) from exc
    except ExternalServiceUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.message
        ) from exc
    except QueryExecutionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.message
        ) from exc
    except DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc


@router.post("/chat/confirm", response_model=ChatResponse)
async def confirm_destructive_action(
    payload: ConfirmationRequest,
    user: UserContext = Depends(get_current_user),
    orchestrator: ChatOrchestratorService = Depends(get_orchestrator),
) -> ChatResponse:
    return await orchestrator.confirm_action(payload, user)


@router.get("/reports", response_model=list[SavedReport])
async def list_reports(
    user: UserContext = Depends(get_current_user),
    report_service: ReportService = Depends(get_report_service),
) -> list[SavedReport]:
    return await report_service.list_user_reports(user)


@router.get("/reports/{report_id}", response_model=SavedReport)
async def get_report(
    report_id: str,
    user: UserContext = Depends(get_current_user),
    report_service: ReportService = Depends(get_report_service),
) -> SavedReport:
    report = await report_service.get_user_report(user, report_id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report not found.",
        )
    return report


@router.get("/metrics")
async def get_metrics(
    obs: ObservabilityPort = Depends(get_observability),
) -> dict:
    return obs.get_metrics_snapshot()


@router.get("/traces", response_model=list[TraceRecord])
async def list_traces(
    limit: int = 10,
    user: UserContext = Depends(get_current_user),
    obs: ObservabilityPort = Depends(get_observability),
) -> list[TraceRecord]:
    scoped_user = None if user.role in {UserRole.CEO, UserRole.ADMIN} else user.user_id
    return obs.get_recent_traces(limit=limit, user_id=scoped_user)


@router.get("/traces/{trace_id}", response_model=TraceRecord)
async def get_trace_detail(
    trace_id: str,
    user: UserContext = Depends(get_current_user),
    obs: ObservabilityPort = Depends(get_observability),
) -> TraceRecord:
    scoped_user = None if user.role in {UserRole.CEO, UserRole.ADMIN} else user.user_id
    record = obs.get_trace(trace_id, user_id=scoped_user)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Trace not found.",
        )
    return record
