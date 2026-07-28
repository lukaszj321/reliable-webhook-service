from typing import Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = (
        "postgresql+psycopg://reliable_webhook:reliable_webhook@127.0.0.1:5432/reliable_webhook"
    )
    webhook_delivery_timeout_seconds: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    webhook_delivery_max_attempts: int = Field(default=5, ge=1)
    webhook_delivery_retry_base_seconds: float = Field(
        default=5.0,
        gt=0,
        allow_inf_nan=False,
    )
    webhook_delivery_retry_max_seconds: float = Field(
        default=300.0,
        gt=0,
        allow_inf_nan=False,
    )
    webhook_worker_poll_interval_seconds: float = Field(
        default=1.0,
        gt=0,
        allow_inf_nan=False,
    )
    webhook_worker_stale_processing_timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        allow_inf_nan=False,
    )
    webhook_worker_recovery_limit: int = Field(default=100, ge=1)
    webhook_worker_processing_limit: int = Field(default=100, ge=1)

    @field_validator(
        "webhook_worker_poll_interval_seconds",
        "webhook_worker_stale_processing_timeout_seconds",
        "webhook_worker_recovery_limit",
        "webhook_worker_processing_limit",
        mode="before",
    )
    @classmethod
    def reject_boolean_worker_settings(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("worker settings must not be booleans")
        return value

    @model_validator(mode="after")
    def validate_webhook_delivery_retry_delays(self) -> Self:
        if self.webhook_delivery_retry_base_seconds > self.webhook_delivery_retry_max_seconds:
            raise ValueError(
                "WEBHOOK_DELIVERY_RETRY_BASE_SECONDS must be less than or equal to "
                "WEBHOOK_DELIVERY_RETRY_MAX_SECONDS"
            )
        return self
