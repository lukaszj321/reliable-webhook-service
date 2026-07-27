import ast
import importlib
import logging
import signal
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, Mock, call

import pytest
from sqlalchemy.orm import Session

from reliable_webhook_service.worker_loop_service import WebhookWorkerRunResult


def _worker_module() -> ModuleType:
    return importlib.import_module("reliable_webhook_service.worker")


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        database_url="postgresql+psycopg://worker:password@database:5432/worker",
        webhook_worker_poll_interval_seconds=1.25,
        webhook_worker_stale_processing_timeout_seconds=45.5,
        webhook_worker_recovery_limit=7,
        webhook_worker_processing_limit=11,
        webhook_delivery_timeout_seconds=4.5,
        webhook_delivery_max_attempts=5,
        webhook_delivery_retry_base_seconds=2.0,
        webhook_delivery_retry_max_seconds=30.0,
    )


def _result() -> WebhookWorkerRunResult:
    return WebhookWorkerRunResult(
        iterations_started=3,
        iterations_completed=3,
        total_recovered_count=2,
        total_claimed_count=4,
        total_completed_count=4,
        shutdown_requested=True,
        final_iteration=None,
    )


def test_import_has_no_runtime_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    httpx2 = importlib.import_module("httpx2")
    sqlalchemy = importlib.import_module("sqlalchemy")
    config_module = importlib.import_module("reliable_webhook_service.config")
    delivery_http_module = importlib.import_module("reliable_webhook_service.delivery_http")
    worker_loop_module = importlib.import_module("reliable_webhook_service.worker_loop_service")

    module_name = "reliable_webhook_service.worker"
    previous_module = sys.modules.pop(module_name, None)
    settings_mock = Mock()
    engine_mock = Mock()
    client_mock = Mock()
    signal_mock = Mock()
    logging_mock = Mock()
    wrapper_mock = Mock()
    loop_mock = Mock()
    monkeypatch.setattr(config_module, "Settings", settings_mock)
    monkeypatch.setattr(sqlalchemy, "create_engine", engine_mock)
    monkeypatch.setattr(httpx2, "Client", client_mock)
    monkeypatch.setattr(signal, "signal", signal_mock)
    monkeypatch.setattr(logging, "basicConfig", logging_mock)
    monkeypatch.setattr(delivery_http_module, "Httpx2WebhookHttpClient", wrapper_mock)
    monkeypatch.setattr(worker_loop_module, "run_webhook_worker", loop_mock)

    try:
        imported_module = importlib.import_module(module_name)
    finally:
        sys.modules.pop(module_name, None)
        if previous_module is not None:
            sys.modules[module_name] = previous_module

    assert imported_module.__all__ == ["main", "run_worker_process"]
    settings_mock.assert_not_called()
    engine_mock.assert_not_called()
    client_mock.assert_not_called()
    signal_mock.assert_not_called()
    logging_mock.assert_not_called()
    wrapper_mock.assert_not_called()
    loop_mock.assert_not_called()


def test_run_worker_process_owns_resources_and_forwards_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker_module()
    settings = _settings()
    settings_factory = Mock(return_value=settings)
    engine = Mock(name="local_engine")
    engine_factory = Mock(return_value=engine)
    local_session_factory = Mock(name="local_session_factory")
    sessionmaker_factory = Mock(return_value=local_session_factory)
    shutdown_event = Mock(name="shutdown_event")
    event_factory = Mock(return_value=shutdown_event)
    raw_http_client = MagicMock(name="raw_http_client")
    raw_http_client.__enter__.return_value = raw_http_client
    raw_http_client_factory = Mock(return_value=raw_http_client)
    wrapped_http_client = Mock(name="wrapped_http_client")
    wrapper_factory = Mock(return_value=wrapped_http_client)
    expected_result = _result()
    worker_loop = Mock(return_value=expected_result)
    signal_context = Mock(name="signal_context")
    lifecycle_events: list[str] = []

    @contextmanager
    def installed_handlers(event: object) -> object:
        signal_context(event)
        lifecycle_events.append("signals:enter")
        try:
            yield
        finally:
            lifecycle_events.append("signals:exit")

    raw_http_client.__enter__.side_effect = lambda: (
        lifecycle_events.append("http:enter") or raw_http_client
    )
    raw_http_client.__exit__.side_effect = lambda *args: (
        lifecycle_events.append("http:exit") or False
    )
    worker_loop.side_effect = lambda **kwargs: lifecycle_events.append("worker") or expected_result
    engine.dispose.side_effect = lambda: lifecycle_events.append("engine:dispose")

    monkeypatch.setattr(worker, "Settings", settings_factory)
    monkeypatch.setattr(worker, "create_engine", engine_factory)
    monkeypatch.setattr(worker, "sessionmaker", sessionmaker_factory)
    monkeypatch.setattr(worker, "Event", event_factory)
    monkeypatch.setattr(worker, "_installed_shutdown_handlers", installed_handlers)
    monkeypatch.setattr(worker.httpx2, "Client", raw_http_client_factory)
    monkeypatch.setattr(worker, "Httpx2WebhookHttpClient", wrapper_factory)
    monkeypatch.setattr(worker, "run_webhook_worker", worker_loop)

    result = worker.run_worker_process()

    assert result is expected_result
    settings_factory.assert_called_once_with()
    engine_factory.assert_called_once_with(settings.database_url, pool_pre_ping=True)
    sessionmaker_factory.assert_called_once_with(
        bind=engine,
        class_=Session,
        expire_on_commit=False,
    )
    event_factory.assert_called_once_with()
    signal_context.assert_called_once_with(shutdown_event)
    raw_http_client_factory.assert_called_once_with()
    raw_http_client.__enter__.assert_called_once_with()
    raw_http_client.__exit__.assert_called_once_with(None, None, None)
    wrapper_factory.assert_called_once_with(raw_http_client)
    worker_loop.assert_called_once_with(
        session_factory=local_session_factory,
        http_client=wrapped_http_client,
        poll_interval_seconds=settings.webhook_worker_poll_interval_seconds,
        stale_processing_timeout_seconds=settings.webhook_worker_stale_processing_timeout_seconds,
        recovery_limit=settings.webhook_worker_recovery_limit,
        processing_limit=settings.webhook_worker_processing_limit,
        timeout_seconds=settings.webhook_delivery_timeout_seconds,
        max_attempts=settings.webhook_delivery_max_attempts,
        base_delay_seconds=settings.webhook_delivery_retry_base_seconds,
        max_delay_seconds=settings.webhook_delivery_retry_max_seconds,
        stop_requested=shutdown_event.is_set,
        wait=shutdown_event.wait,
    )
    assert "iteration_now" not in worker_loop.call_args.kwargs
    assert "utc_now" not in worker_loop.call_args.kwargs
    assert "decision_now" not in worker_loop.call_args.kwargs
    assert "monotonic_ns" not in worker_loop.call_args.kwargs
    engine.dispose.assert_called_once_with()
    assert lifecycle_events == [
        "signals:enter",
        "http:enter",
        "worker",
        "http:exit",
        "signals:exit",
        "engine:dispose",
    ]


@pytest.mark.parametrize("failure_stage", ["settings", "engine"])
def test_run_worker_process_propagates_early_failures_without_later_resources(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    worker = _worker_module()
    error = RuntimeError(f"{failure_stage} failed")
    settings_factory = Mock(return_value=_settings())
    engine_factory = Mock()
    sessionmaker_factory = Mock()
    event_factory = Mock()
    client_factory = Mock()
    worker_loop = Mock()
    if failure_stage == "settings":
        settings_factory.side_effect = error
    else:
        engine_factory.side_effect = error
    monkeypatch.setattr(worker, "Settings", settings_factory)
    monkeypatch.setattr(worker, "create_engine", engine_factory)
    monkeypatch.setattr(worker, "sessionmaker", sessionmaker_factory)
    monkeypatch.setattr(worker, "Event", event_factory)
    monkeypatch.setattr(worker.httpx2, "Client", client_factory)
    monkeypatch.setattr(worker, "run_webhook_worker", worker_loop)

    with pytest.raises(RuntimeError) as error_info:
        worker.run_worker_process()

    assert error_info.value is error
    settings_factory.assert_called_once_with()
    assert engine_factory.call_count == (0 if failure_stage == "settings" else 1)
    sessionmaker_factory.assert_not_called()
    event_factory.assert_not_called()
    client_factory.assert_not_called()
    worker_loop.assert_not_called()


def test_sessionmaker_failure_disposes_engine_without_other_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker_module()
    error = RuntimeError("sessionmaker failed")
    engine = Mock()
    event_factory = Mock()
    client_factory = Mock()
    worker_loop = Mock()
    monkeypatch.setattr(worker, "Settings", Mock(return_value=_settings()))
    monkeypatch.setattr(worker, "create_engine", Mock(return_value=engine))
    monkeypatch.setattr(worker, "sessionmaker", Mock(side_effect=error))
    monkeypatch.setattr(worker, "Event", event_factory)
    monkeypatch.setattr(worker.httpx2, "Client", client_factory)
    monkeypatch.setattr(worker, "run_webhook_worker", worker_loop)

    with pytest.raises(RuntimeError) as error_info:
        worker.run_worker_process()

    assert error_info.value is error
    engine.dispose.assert_called_once_with()
    event_factory.assert_not_called()
    client_factory.assert_not_called()
    worker_loop.assert_not_called()


def test_signal_installation_failure_disposes_engine_before_http_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker_module()
    error = RuntimeError("signal installation failed")
    engine = Mock()
    client_factory = Mock()
    worker_loop = Mock()
    monkeypatch.setattr(worker, "Settings", Mock(return_value=_settings()))
    monkeypatch.setattr(worker, "create_engine", Mock(return_value=engine))
    monkeypatch.setattr(worker, "sessionmaker", Mock(return_value=Mock()))
    monkeypatch.setattr(worker, "Event", Mock(return_value=Mock()))
    monkeypatch.setattr(worker, "_installed_shutdown_handlers", Mock(side_effect=error))
    monkeypatch.setattr(worker.httpx2, "Client", client_factory)
    monkeypatch.setattr(worker, "run_webhook_worker", worker_loop)

    with pytest.raises(RuntimeError) as error_info:
        worker.run_worker_process()

    assert error_info.value is error
    engine.dispose.assert_called_once_with()
    client_factory.assert_not_called()
    worker_loop.assert_not_called()


def test_http_construction_failure_restores_signals_and_disposes_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker_module()
    error = RuntimeError("HTTP construction failed")
    engine = Mock()
    worker_loop = Mock()
    lifecycle_events: list[str] = []

    @contextmanager
    def installed_handlers(_event: object) -> object:
        lifecycle_events.append("signals:enter")
        try:
            yield
        finally:
            lifecycle_events.append("signals:exit")

    def dispose() -> None:
        lifecycle_events.append("engine:dispose")

    engine.dispose.side_effect = dispose
    monkeypatch.setattr(worker, "Settings", Mock(return_value=_settings()))
    monkeypatch.setattr(worker, "create_engine", Mock(return_value=engine))
    monkeypatch.setattr(worker, "sessionmaker", Mock(return_value=Mock()))
    monkeypatch.setattr(worker, "Event", Mock(return_value=Mock()))
    monkeypatch.setattr(worker, "_installed_shutdown_handlers", installed_handlers)
    monkeypatch.setattr(worker.httpx2, "Client", Mock(side_effect=error))
    monkeypatch.setattr(worker, "run_webhook_worker", worker_loop)

    with pytest.raises(RuntimeError) as error_info:
        worker.run_worker_process()

    assert error_info.value is error
    worker_loop.assert_not_called()
    engine.dispose.assert_called_once_with()
    assert lifecycle_events == ["signals:enter", "signals:exit", "engine:dispose"]


def test_http_enter_failure_closes_client_restores_signals_and_disposes_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker_module()
    error = RuntimeError("HTTP enter failed")
    engine = Mock()
    raw_http_client = MagicMock()
    raw_http_client.__enter__.side_effect = error
    worker_loop = Mock()
    lifecycle_events: list[str] = []

    @contextmanager
    def installed_handlers(_event: object) -> object:
        lifecycle_events.append("signals:enter")
        try:
            yield
        finally:
            lifecycle_events.append("signals:exit")

    raw_http_client.close.side_effect = lambda: lifecycle_events.append("http:close")
    engine.dispose.side_effect = lambda: lifecycle_events.append("engine:dispose")
    monkeypatch.setattr(worker, "Settings", Mock(return_value=_settings()))
    monkeypatch.setattr(worker, "create_engine", Mock(return_value=engine))
    monkeypatch.setattr(worker, "sessionmaker", Mock(return_value=Mock()))
    monkeypatch.setattr(worker, "Event", Mock(return_value=Mock()))
    monkeypatch.setattr(worker, "_installed_shutdown_handlers", installed_handlers)
    monkeypatch.setattr(worker.httpx2, "Client", Mock(return_value=raw_http_client))
    monkeypatch.setattr(worker, "run_webhook_worker", worker_loop)

    with pytest.raises(RuntimeError) as error_info:
        worker.run_worker_process()

    assert error_info.value is error
    raw_http_client.__enter__.assert_called_once_with()
    raw_http_client.__exit__.assert_not_called()
    raw_http_client.close.assert_called_once_with()
    worker_loop.assert_not_called()
    engine.dispose.assert_called_once_with()
    assert lifecycle_events == [
        "signals:enter",
        "http:close",
        "signals:exit",
        "engine:dispose",
    ]


def test_worker_failure_closes_http_restores_signals_and_disposes_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker_module()
    error = RuntimeError("worker failed")
    engine = Mock()
    raw_http_client = MagicMock()
    raw_http_client.__enter__.return_value = raw_http_client
    wrapped_http_client = Mock()
    worker_loop = Mock(side_effect=error)
    lifecycle_events: list[str] = []

    @contextmanager
    def installed_handlers(_event: object) -> object:
        lifecycle_events.append("signals:enter")
        try:
            yield
        finally:
            lifecycle_events.append("signals:exit")

    raw_http_client.__enter__.side_effect = lambda: (
        lifecycle_events.append("http:enter") or raw_http_client
    )
    raw_http_client.__exit__.side_effect = lambda *args: (
        lifecycle_events.append("http:exit") or False
    )
    engine.dispose.side_effect = lambda: lifecycle_events.append("engine:dispose")
    monkeypatch.setattr(worker, "Settings", Mock(return_value=_settings()))
    monkeypatch.setattr(worker, "create_engine", Mock(return_value=engine))
    monkeypatch.setattr(worker, "sessionmaker", Mock(return_value=Mock()))
    monkeypatch.setattr(worker, "Event", Mock(return_value=Mock()))
    monkeypatch.setattr(worker, "_installed_shutdown_handlers", installed_handlers)
    monkeypatch.setattr(worker.httpx2, "Client", Mock(return_value=raw_http_client))
    monkeypatch.setattr(
        worker,
        "Httpx2WebhookHttpClient",
        Mock(return_value=wrapped_http_client),
    )
    monkeypatch.setattr(worker, "run_webhook_worker", worker_loop)

    with pytest.raises(RuntimeError) as error_info:
        worker.run_worker_process()

    assert error_info.value is error
    worker_loop.assert_called_once()
    raw_http_client.__enter__.assert_called_once_with()
    raw_http_client.__exit__.assert_called_once()
    exit_exception_type, exit_exception, exit_traceback = raw_http_client.__exit__.call_args.args
    assert exit_exception_type is RuntimeError
    assert exit_exception is error
    assert exit_traceback is not None
    engine.dispose.assert_called_once_with()
    assert lifecycle_events == [
        "signals:enter",
        "http:enter",
        "http:exit",
        "signals:exit",
        "engine:dispose",
    ]


@pytest.mark.parametrize("raise_inside_context", [False, True])
def test_signal_handlers_set_event_and_are_restored(
    monkeypatch: pytest.MonkeyPatch,
    raise_inside_context: bool,
) -> None:
    worker = _worker_module()
    shutdown_event = Mock()
    previous_sigint = Mock(name="previous_sigint")
    previous_sigterm = Mock(name="previous_sigterm")
    getsignal_mock = Mock(side_effect=[previous_sigint, previous_sigterm])
    signal_mock = Mock()
    monkeypatch.setattr(worker.signal, "getsignal", getsignal_mock)
    monkeypatch.setattr(worker.signal, "signal", signal_mock)

    error = RuntimeError("context failed")
    if raise_inside_context:
        with pytest.raises(RuntimeError) as error_info:
            with worker._installed_shutdown_handlers(shutdown_event):
                raise error
        assert error_info.value is error
    else:
        with worker._installed_shutdown_handlers(shutdown_event):
            pass

    installed_handler = signal_mock.call_args_list[0].args[1]
    installed_handler(signal.SIGINT, None)
    shutdown_event.set.assert_called_once_with()
    getsignal_mock.assert_has_calls([call(signal.SIGINT), call(signal.SIGTERM)])
    assert signal_mock.call_args_list == [
        call(signal.SIGINT, installed_handler),
        call(signal.SIGTERM, installed_handler),
        call(signal.SIGTERM, previous_sigterm),
        call(signal.SIGINT, previous_sigint),
    ]


def test_partial_signal_installation_failure_restores_first_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker_module()
    shutdown_event = Mock()
    previous_sigint = Mock(name="previous_sigint")
    previous_sigterm = Mock(name="previous_sigterm")
    error = RuntimeError("SIGTERM installation failed")
    calls: list[tuple[object, object]] = []

    monkeypatch.setattr(
        worker.signal,
        "getsignal",
        Mock(side_effect=[previous_sigint, previous_sigterm]),
    )

    def install(signal_number: object, handler: object) -> None:
        calls.append((signal_number, handler))
        if signal_number == signal.SIGTERM and handler is not previous_sigterm:
            raise error

    monkeypatch.setattr(worker.signal, "signal", install)

    with pytest.raises(RuntimeError) as error_info:
        with worker._installed_shutdown_handlers(shutdown_event):
            raise AssertionError("context must not be entered")

    assert error_info.value is error
    assert calls[0][0] == signal.SIGINT
    assert calls[1][0] == signal.SIGTERM
    assert calls[2] == (signal.SIGINT, previous_sigint)
    shutdown_event.set.assert_not_called()


def test_main_success_configures_logging_once_and_logs_safe_summary(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = _worker_module()
    result = _result()
    basic_config = Mock()
    process = Mock(return_value=result)
    monkeypatch.setattr(worker.logging, "basicConfig", basic_config)
    monkeypatch.setattr(worker, "run_worker_process", process)
    monkeypatch.setattr(worker.logger, "disabled", False)

    with caplog.at_level(logging.INFO, logger=worker.__name__):
        exit_code = worker.main()

    assert exit_code == 0
    basic_config.assert_called_once_with(level=logging.INFO)
    process.assert_called_once_with()
    assert caplog.messages == [
        (
            "Webhook worker process completed: iterations_completed=3 "
            "total_recovered=2 total_claimed=4 total_completed=4 "
            "shutdown_requested=True"
        )
    ]
    assert "final_iteration" not in caplog.text


def test_main_failure_returns_one_without_logging_exception_message(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    worker = _worker_module()
    secret_marker = "SECRET database-url payload target-url response-body"
    error = RuntimeError(secret_marker)
    basic_config = Mock()
    process = Mock(side_effect=error)
    monkeypatch.setattr(worker.logging, "basicConfig", basic_config)
    monkeypatch.setattr(worker, "run_worker_process", process)
    monkeypatch.setattr(worker.logger, "disabled", False)

    with caplog.at_level(logging.INFO, logger=worker.__name__):
        exit_code = worker.main()

    assert exit_code == 1
    basic_config.assert_called_once_with(level=logging.INFO)
    process.assert_called_once_with()
    assert "Webhook worker process failed: RuntimeError" in caplog.text
    assert secret_marker not in caplog.text


def test_module_contract_guard_and_forbidden_imports() -> None:
    worker = _worker_module()
    source_path = Path(worker.__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    forbidden_modules = {
        "argparse",
        "asyncio",
        "fastapi",
        "multiprocessing",
        "reliable_webhook_service.api",
        "reliable_webhook_service.database",
        "reliable_webhook_service.delivery_job_recovery_service",
        "reliable_webhook_service.delivery_processing_service",
        "reliable_webhook_service.main",
        "reliable_webhook_service.models",
        "reliable_webhook_service.worker_iteration_service",
        "subprocess",
    }

    assert worker.__all__ == ["main", "run_worker_process"]
    assert imported_modules.isdisjoint(forbidden_modules)
    assert all(token not in source for token in ("Celery", "Redis", "Kafka"))
    assert any(
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        and any(isinstance(statement, ast.Raise) for statement in node.body)
        for node in tree.body
    )
    assert not hasattr(worker, "SessionFactory")
    assert not hasattr(worker, "engine")
    assert not hasattr(worker, "settings")
