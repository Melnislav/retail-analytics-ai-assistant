"""FastAPI HTTP inbound adapter with RBAC header extraction and domain exception mapping."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.application.chat_orchestrator import ChatOrchestratorService
from app.application.ports import ObservabilityPort, PersonaStorePort, UserPreferencePort
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
    PersonaConfig,
    SavedReport,
    UserContext,
    UserPreferenceProfile,
    UserRole,
)

router = APIRouter(prefix="/api/v1", tags=["Retail Data Analysis Assistant"])


def get_current_user(
    x_user_id: str = Header(default="exec_alice", alias="X-User-Id"),
    x_user_name: str = Header(default="Alice (VP Retail)", alias="X-User-Name"),
    x_user_role: str = Header(default="executive", alias="X-User-Role"),
    x_allowed_categories: str = Header(default="", alias="X-Allowed-Categories"),
    x_allowed_departments: str = Header(default="", alias="X-Allowed-Departments"),
) -> UserContext:
    """Extracts user identity and product RBAC permissions from request headers."""
    try:
        role = UserRole(x_user_role.lower())
    except ValueError:
        role = UserRole.EXECUTIVE

    categories = [c.strip() for c in x_allowed_categories.split(",") if c.strip()]
    departments = [d.strip() for d in x_allowed_departments.split(",") if d.strip()]
    return UserContext(
        user_id=x_user_id,
        display_name=x_user_name,
        role=role,
        allowed_categories=categories,
        allowed_departments=departments,
    )


def get_orchestrator(request: Request) -> ChatOrchestratorService:
    return request.app.state.container.chat_orchestrator


def get_report_service(request: Request) -> ReportService:
    return request.app.state.container.report_service


def get_persona_store(request: Request) -> PersonaStorePort:
    return request.app.state.container.persona_store


def get_preference_store(request: Request) -> UserPreferencePort:
    return request.app.state.container.preference_store


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


class PersonaUpdateRequest(BaseModel):
    tone_instructions: str = Field(..., min_length=5, max_length=2000)


@router.get("/persona", response_model=PersonaConfig)
async def get_persona(
    persona_store: PersonaStorePort = Depends(get_persona_store),
) -> PersonaConfig:
    return await persona_store.get_active_persona()


@router.put("/persona", response_model=PersonaConfig)
async def update_persona(
    payload: PersonaUpdateRequest,
    user: UserContext = Depends(get_current_user),
    persona_store: PersonaStorePort = Depends(get_persona_store),
) -> PersonaConfig:
    """Allows CEO/non-developers to update report tone dynamically without redeployment."""
    updated = PersonaConfig(
        tone_instructions=payload.tone_instructions,
        updated_by=user.user_id,
    )
    return await persona_store.save_persona(updated)


@router.get("/preferences", response_model=UserPreferenceProfile)
async def get_preferences(
    user: UserContext = Depends(get_current_user),
    pref_store: UserPreferencePort = Depends(get_preference_store),
) -> UserPreferenceProfile:
    return await pref_store.get_preferences(user.user_id)


@router.put("/preferences", response_model=UserPreferenceProfile)
async def update_preferences(
    profile: UserPreferenceProfile,
    user: UserContext = Depends(get_current_user),
    pref_store: UserPreferencePort = Depends(get_preference_store),
) -> UserPreferenceProfile:
    profile.user_id = user.user_id
    return await pref_store.upsert_preferences(profile)


@router.get("/metrics")
async def get_metrics(
    obs: ObservabilityPort = Depends(get_observability),
) -> dict:
    return obs.get_metrics_snapshot()
