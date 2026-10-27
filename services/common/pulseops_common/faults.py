"""Fault injection: the "soft" half of chaos engineering. The control plane
calls POST /admin/faults on every replica of a service to add latency
and/or a failure rate for a limited time; the middleware applies it to
all non-probe, non-admin requests. Faults always expire on their own, so a
crashed control plane can never leave a service permanently degraded."""

import asyncio
import hmac
import random
import time

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from pulseops_common.metrics import FAULTS_ACTIVE

MAX_FAULT_SECONDS = 600


class FaultSpec(BaseModel):
    latency_ms: int = Field(default=0, ge=0, le=10_000)
    error_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    duration_sec: int = Field(default=60, ge=1, le=MAX_FAULT_SECONDS)


class FaultStatus(BaseModel):
    active: bool
    latency_ms: int
    error_rate: float
    expires_in_sec: float


class FaultState:
    def __init__(self, service: str):
        self.service = service
        self.latency_ms = 0
        self.error_rate = 0.0
        self.expires_at = 0.0

    @property
    def active(self) -> bool:
        if self.expires_at and time.monotonic() >= self.expires_at:
            self.clear()
        return self.expires_at > 0

    def set(self, spec: FaultSpec) -> None:
        self.latency_ms = spec.latency_ms
        self.error_rate = spec.error_rate
        self.expires_at = time.monotonic() + spec.duration_sec
        FAULTS_ACTIVE.labels(self.service, "latency").set(1 if spec.latency_ms else 0)
        FAULTS_ACTIVE.labels(self.service, "errors").set(1 if spec.error_rate else 0)

    def clear(self) -> None:
        self.latency_ms = 0
        self.error_rate = 0.0
        self.expires_at = 0.0
        FAULTS_ACTIVE.labels(self.service, "latency").set(0)
        FAULTS_ACTIVE.labels(self.service, "errors").set(0)

    def status(self) -> FaultStatus:
        active = self.active
        return FaultStatus(
            active=active,
            latency_ms=self.latency_ms,
            error_rate=self.error_rate,
            expires_in_sec=max(self.expires_at - time.monotonic(), 0.0) if active else 0.0,
        )


_EXEMPT_PREFIXES = ("/metrics", "/healthz", "/readyz", "/admin")


class FaultMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, state: FaultState):
        super().__init__(app)
        self.state = state

    async def dispatch(self, request: Request, call_next):
        # Probes stay healthy on purpose: an injected fault simulates a
        # degraded dependency, not a dead pod — so Kubernetes must not
        # "heal" it by restarting; the callers' retries and circuit
        # breakers are what's under test.
        if self.state.active and not request.url.path.startswith(_EXEMPT_PREFIXES):
            if self.state.latency_ms:
                await asyncio.sleep(self.state.latency_ms / 1000)
            if self.state.error_rate and random.random() < self.state.error_rate:
                return JSONResponse(
                    status_code=503,
                    content={"error": "injected fault", "service": self.state.service},
                )
        return await call_next(request)


def fault_router(state: FaultState, admin_token: str) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["chaos"], include_in_schema=False)

    def _check(token: str | None) -> None:
        if not admin_token:
            raise HTTPException(status_code=404, detail="fault injection disabled")
        if not token or not hmac.compare_digest(token, admin_token):
            raise HTTPException(status_code=401, detail="invalid admin token")

    @router.get("/faults", response_model=FaultStatus)
    async def get_faults(x_admin_token: str | None = Header(default=None)) -> FaultStatus:
        _check(x_admin_token)
        return state.status()

    @router.post("/faults", response_model=FaultStatus)
    async def set_faults(spec: FaultSpec, x_admin_token: str | None = Header(default=None)) -> FaultStatus:
        _check(x_admin_token)
        state.set(spec)
        return state.status()

    @router.delete("/faults", response_model=FaultStatus)
    async def clear_faults(x_admin_token: str | None = Header(default=None)) -> FaultStatus:
        _check(x_admin_token)
        state.clear()
        return state.status()

    return router
