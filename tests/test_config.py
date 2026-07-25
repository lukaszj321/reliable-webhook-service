import pytest
from pydantic import ValidationError

from reliable_webhook_service.config import Settings


def test_webhook_delivery_timeout_uses_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WEBHOOK_DELIVERY_TIMEOUT_SECONDS", raising=False)

    settings = Settings(_env_file=None)

    assert settings.webhook_delivery_timeout_seconds == 10.0


def test_webhook_delivery_timeout_reads_environment_as_float(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEBHOOK_DELIVERY_TIMEOUT_SECONDS", "7")

    settings = Settings(_env_file=None)

    assert settings.webhook_delivery_timeout_seconds == 7.0
    assert isinstance(settings.webhook_delivery_timeout_seconds, float)


def test_webhook_delivery_timeout_accepts_positive_fraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEBHOOK_DELIVERY_TIMEOUT_SECONDS", "2.5")

    settings = Settings(_env_file=None)

    assert settings.webhook_delivery_timeout_seconds == 2.5


@pytest.mark.parametrize(
    "value",
    [
        "0",
        "-1",
        "nan",
        "inf",
        "-inf",
    ],
)
def test_webhook_delivery_timeout_rejects_invalid_value(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("WEBHOOK_DELIVERY_TIMEOUT_SECONDS", value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
