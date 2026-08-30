from pydantic import BaseModel


class ServiceStatus(BaseModel):
    provider: str
    model: str
    dimensions: int | None = None


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    services: dict[str, ServiceStatus]