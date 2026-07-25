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


def test_webhook_delivery_retry_settings_use_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for environment_variable in (
        "WEBHOOK_DELIVERY_MAX_ATTEMPTS",
        "WEBHOOK_DELIVERY_RETRY_BASE_SECONDS",
        "WEBHOOK_DELIVERY_RETRY_MAX_SECONDS",
    ):
        monkeypatch.delenv(environment_variable, raising=False)

    settings = Settings(_env_file=None)

    assert settings.webhook_delivery_max_attempts == 5
    assert settings.webhook_delivery_retry_base_seconds == 5.0
    assert isinstance(settings.webhook_delivery_retry_base_seconds, float)
    assert settings.webhook_delivery_retry_max_seconds == 300.0
    assert isinstance(settings.webhook_delivery_retry_max_seconds, float)


def test_webhook_delivery_retry_settings_read_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEBHOOK_DELIVERY_MAX_ATTEMPTS", "7")
    monkeypatch.setenv("WEBHOOK_DELIVERY_RETRY_BASE_SECONDS", "2.5")
    monkeypatch.setenv("WEBHOOK_DELIVERY_RETRY_MAX_SECONDS", "120")

    settings = Settings(_env_file=None)

    assert settings.webhook_delivery_max_attempts == 7
    assert settings.webhook_delivery_retry_base_seconds == 2.5
    assert isinstance(settings.webhook_delivery_retry_base_seconds, float)
    assert settings.webhook_delivery_retry_max_seconds == 120.0
    assert isinstance(settings.webhook_delivery_retry_max_seconds, float)


@pytest.mark.parametrize("value", ["0", "-1"])
def test_webhook_delivery_max_attempts_rejects_invalid_value(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("WEBHOOK_DELIVERY_MAX_ATTEMPTS", value)
    monkeypatch.delenv("WEBHOOK_DELIVERY_RETRY_BASE_SECONDS", raising=False)
    monkeypatch.delenv("WEBHOOK_DELIVERY_RETRY_MAX_SECONDS", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "environment_variable",
    [
        "WEBHOOK_DELIVERY_RETRY_BASE_SECONDS",
        "WEBHOOK_DELIVERY_RETRY_MAX_SECONDS",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_webhook_delivery_retry_delay_rejects_invalid_value(
    monkeypatch: pytest.MonkeyPatch,
    environment_variable: str,
    value: str,
) -> None:
    monkeypatch.delenv("WEBHOOK_DELIVERY_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("WEBHOOK_DELIVERY_RETRY_BASE_SECONDS", raising=False)
    monkeypatch.delenv("WEBHOOK_DELIVERY_RETRY_MAX_SECONDS", raising=False)
    monkeypatch.setenv(environment_variable, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_webhook_delivery_retry_base_rejects_value_above_maximum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WEBHOOK_DELIVERY_MAX_ATTEMPTS", raising=False)
    monkeypatch.setenv("WEBHOOK_DELIVERY_RETRY_BASE_SECONDS", "10.0")
    monkeypatch.setenv("WEBHOOK_DELIVERY_RETRY_MAX_SECONDS", "5.0")

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    assert (
        "WEBHOOK_DELIVERY_RETRY_BASE_SECONDS must be less than or equal to "
        "WEBHOOK_DELIVERY_RETRY_MAX_SECONDS"
    ) in str(exc_info.value)


def test_webhook_delivery_retry_base_accepts_value_equal_to_maximum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WEBHOOK_DELIVERY_MAX_ATTEMPTS", raising=False)
    monkeypatch.setenv("WEBHOOK_DELIVERY_RETRY_BASE_SECONDS", "5.0")
    monkeypatch.setenv("WEBHOOK_DELIVERY_RETRY_MAX_SECONDS", "5.0")

    settings = Settings(_env_file=None)

    assert settings.webhook_delivery_retry_base_seconds == 5.0
    assert settings.webhook_delivery_retry_max_seconds == 5.0
