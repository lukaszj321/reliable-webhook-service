import json
import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent


def test_worker_module_entry_point_completes_with_controlled_boundaries(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "worker-smoke-state.json"
    sitecustomize_path = tmp_path / "sitecustomize.py"
    secret_marker = "worker-smoke-secret-marker"
    database_url = f"postgresql+psycopg://smoke-user:{secret_marker}@127.0.0.1:6543/smoke-database"
    payload_marker = "worker-smoke-payload-marker"
    source_root = Path(__file__).resolve().parents[1] / "src"
    sitecustomize_path.write_text(
        dedent(
            f"""
            import atexit
            import json
            import os
            import signal

            import httpx2
            import sqlalchemy
            import sqlalchemy.orm
            import reliable_webhook_service.config as config_module

            state = {{
                "settings_calls": 0,
                "engine_calls": 0,
                "engine_url_matches": False,
                "engine_pool_pre_ping": None,
                "sessionmaker_calls": 0,
                "sessionmaker_bind_matches": False,
                "sessionmaker_class_matches": False,
                "sessionmaker_expire_on_commit": None,
                "raw_http_calls": 0,
                "raw_http_enters": 0,
                "raw_http_exits": 0,
                "wrapped_http_calls": 0,
                "worker_calls": 0,
                "worker_arguments": {{}},
                "signal_get_calls": [],
                "signal_install_calls": [],
                "signal_restore_calls": [],
                "engine_dispose_calls": 0,
            }}


            class FakeSettings:
                def __init__(self):
                    state["settings_calls"] += 1
                    self.database_url = {database_url!r}
                    self.webhook_worker_poll_interval_seconds = 1.25
                    self.webhook_worker_stale_processing_timeout_seconds = 45.5
                    self.webhook_worker_recovery_limit = 7
                    self.webhook_worker_processing_limit = 11
                    self.webhook_delivery_timeout_seconds = 4.5
                    self.webhook_delivery_max_attempts = 5
                    self.webhook_delivery_retry_base_seconds = 2.0
                    self.webhook_delivery_retry_max_seconds = 30.0


            class FakeEngine:
                def dispose(self):
                    state["engine_dispose_calls"] += 1


            class FakeSessionFactory:
                pass


            class FakeRawHttpClient:
                def __enter__(self):
                    state["raw_http_enters"] += 1
                    return self

                def __exit__(self, exception_type, exception, traceback):
                    state["raw_http_exits"] += 1
                    return False

                def close(self):
                    raise AssertionError("Unexpected fallback close")


            class FakeWrappedHttpClient:
                def __init__(self, raw_http_client):
                    state["wrapped_http_calls"] += 1
                    self.raw_http_client = raw_http_client


            fake_engine = FakeEngine()
            fake_session_factory = FakeSessionFactory()


            def fake_create_engine(url, *, pool_pre_ping):
                state["engine_calls"] += 1
                state["engine_url_matches"] = url == {database_url!r}
                state["engine_pool_pre_ping"] = pool_pre_ping
                return fake_engine


            def fake_sessionmaker(*, bind, class_, expire_on_commit):
                state["sessionmaker_calls"] += 1
                state["sessionmaker_bind_matches"] = bind is fake_engine
                state["sessionmaker_class_matches"] = class_ is sqlalchemy.orm.Session
                state["sessionmaker_expire_on_commit"] = expire_on_commit
                return fake_session_factory


            class FakeSessionmakerBoundary:
                @classmethod
                def __class_getitem__(cls, _item):
                    return cls

                def __new__(cls, *, bind, class_, expire_on_commit):
                    return fake_sessionmaker(
                        bind=bind,
                        class_=class_,
                        expire_on_commit=expire_on_commit,
                    )


            def fake_raw_http_client():
                state["raw_http_calls"] += 1
                return FakeRawHttpClient()


            def fake_getsignal(signal_number):
                state["signal_get_calls"].append(int(signal_number))
                return signal.SIG_DFL


            def fake_signal(signal_number, handler):
                if callable(handler):
                    state["signal_install_calls"].append(int(signal_number))
                else:
                    state["signal_restore_calls"].append(int(signal_number))
                return signal.SIG_DFL


            config_module.Settings = FakeSettings
            sqlalchemy.create_engine = fake_create_engine
            sqlalchemy.orm.sessionmaker = FakeSessionmakerBoundary
            httpx2.Client = fake_raw_http_client
            signal.getsignal = fake_getsignal
            signal.signal = fake_signal

            import reliable_webhook_service.delivery_http as delivery_http_module
            import reliable_webhook_service.worker_loop_service as worker_loop_module


            def fake_wrapped_http_client(raw_http_client):
                return FakeWrappedHttpClient(raw_http_client)


            def fake_run_webhook_worker(**arguments):
                state["worker_calls"] += 1
                state["worker_arguments"] = {{
                    "session_factory_matches": (
                        arguments["session_factory"] is fake_session_factory
                    ),
                    "http_client_type": type(arguments["http_client"]).__name__,
                    "poll_interval_seconds": arguments["poll_interval_seconds"],
                    "stale_processing_timeout_seconds": (
                        arguments["stale_processing_timeout_seconds"]
                    ),
                    "recovery_limit": arguments["recovery_limit"],
                    "processing_limit": arguments["processing_limit"],
                    "timeout_seconds": arguments["timeout_seconds"],
                    "max_attempts": arguments["max_attempts"],
                    "base_delay_seconds": arguments["base_delay_seconds"],
                    "max_delay_seconds": arguments["max_delay_seconds"],
                    "stop_requested_callable": callable(arguments["stop_requested"]),
                    "wait_callable": callable(arguments["wait"]),
                    "explicit_clock_count": sum(
                        name in arguments
                        for name in (
                            "iteration_now",
                            "utc_now",
                            "decision_now",
                            "monotonic_ns",
                        )
                    ),
                }}
                return worker_loop_module.WebhookWorkerRunResult(
                    iterations_started=0,
                    iterations_completed=0,
                    total_recovered_count=0,
                    total_claimed_count=0,
                    total_completed_count=0,
                    shutdown_requested=True,
                    final_iteration=None,
                )


            delivery_http_module.Httpx2WebhookHttpClient = fake_wrapped_http_client
            worker_loop_module.run_webhook_worker = fake_run_webhook_worker

            state.update(
                settings_calls=0,
                engine_calls=0,
                engine_url_matches=False,
                engine_pool_pre_ping=None,
                sessionmaker_calls=0,
                sessionmaker_bind_matches=False,
                sessionmaker_class_matches=False,
                sessionmaker_expire_on_commit=None,
            )


            @atexit.register
            def write_state():
                with open(
                    os.environ["WORKER_SMOKE_STATE_PATH"],
                    "w",
                    encoding="utf-8",
                ) as state_file:
                    json.dump(state, state_file, sort_keys=True)
            """
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    pythonpath_entries = [str(tmp_path), str(source_root)]
    if existing_pythonpath:
        pythonpath_entries.append(existing_pythonpath)
    environment["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    environment["WORKER_SMOKE_STATE_PATH"] = str(state_path)
    environment["WORKER_SMOKE_SECRET_MARKER"] = secret_marker
    environment["WORKER_SMOKE_PAYLOAD_MARKER"] = payload_marker

    command = [sys.executable, "-m", "reliable_webhook_service.worker"]
    completed = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert command[1:] == ["-m", "reliable_webhook_service.worker"]
    assert completed.returncode == 0, completed.stderr
    assert state_path.is_file()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["settings_calls"] == 1
    assert state["engine_calls"] == 1
    assert state["engine_url_matches"] is True
    assert state["engine_pool_pre_ping"] is True
    assert state["sessionmaker_calls"] == 1
    assert state["sessionmaker_bind_matches"] is True
    assert state["sessionmaker_class_matches"] is True
    assert state["sessionmaker_expire_on_commit"] is False
    assert state["raw_http_calls"] == 1
    assert state["raw_http_enters"] == 1
    assert state["raw_http_exits"] == 1
    assert state["wrapped_http_calls"] == 1
    assert state["worker_calls"] == 1
    assert state["worker_arguments"] == {
        "session_factory_matches": True,
        "http_client_type": "FakeWrappedHttpClient",
        "poll_interval_seconds": 1.25,
        "stale_processing_timeout_seconds": 45.5,
        "recovery_limit": 7,
        "processing_limit": 11,
        "timeout_seconds": 4.5,
        "max_attempts": 5,
        "base_delay_seconds": 2.0,
        "max_delay_seconds": 30.0,
        "stop_requested_callable": True,
        "wait_callable": True,
        "explicit_clock_count": 0,
    }
    assert len(state["signal_get_calls"]) == 2
    assert len(state["signal_install_calls"]) == 2
    assert state["signal_restore_calls"] == list(reversed(state["signal_get_calls"]))
    assert state["engine_dispose_calls"] == 1
    combined_output = completed.stdout + completed.stderr
    assert secret_marker not in combined_output
    assert database_url not in combined_output
    assert payload_marker not in combined_output
    assert "postgresql+psycopg://" not in combined_output
