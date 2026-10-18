"""ProjectService operation logging stays outside the application boundary."""

from pathlib import Path
import zipfile

import pytest

from componentpress.application.project_service import ProjectService
from componentpress.domain.diagnostics import Diagnostic, ProjectError


class Projects:
    def __init__(self, events, result=None, error=None):
        self.events = events
        self.result = result if result is not None else object()
        self.error = error

    def create(self, root, name):
        self.events.append(("repository", root, name))
        if self.error:
            raise self.error
        return self.result

    def open(self, root):
        self.events.append(("repository", root))
        if self.error:
            raise self.error
        return self.result

    def add_component(self, snapshot, component_id, name, **values):
        self.events.append(("repository.add_component", component_id, name, values))
        if self.error:
            raise self.error
        return self.result


def make_service(projects, event_logger, exception_logger):
    from contextlib import nullcontext

    return ProjectService(
        projects, object(), lambda _root: nullcontext(), object(),
        diagnostic_formatter=str, event_logger=event_logger,
        exception_logger=exception_logger,
    )


def test_operation_logs_start_before_repository_and_returns_same_result():
    result, calls = object(), []
    projects = Projects(calls, result)
    service = make_service(
        projects,
        lambda event, **context: calls.append((event, context)),
        lambda *_args, **_kwargs: "incident",
    )

    assert service.create(Path("game"), "Game") is result
    assert [item[0] for item in calls] == [
        "project.operation", "repository", "project.operation",
    ]
    assert [item[1]["status"] for item in (calls[0], calls[2])] == ["started", "succeeded"]


def test_project_error_keeps_identity_and_logs_only_its_code():
    error, calls = ProjectError(Diagnostic("PROJECT_OPEN_FAILED", "private")), []
    service = make_service(
        Projects(calls, error=error),
        lambda event, **context: calls.append((event, context)),
        lambda *_args, **_kwargs: "incident",
    )

    with pytest.raises(ProjectError) as caught:
        service.open(Path("game"))

    assert caught.value is error
    assert calls[-1] == (
        "project.operation",
        {"operation": "open", "status": "failed", "code": "PROJECT_OPEN_FAILED"},
    )


def test_unexpected_error_is_reported_and_rethrown_unchanged():
    error, calls = RuntimeError("private detail"), []
    service = make_service(
        Projects(calls, error=error),
        lambda event, **context: calls.append((event, context)),
        lambda event, caught, **context: calls.append((event, caught, context)) or "incident",
    )

    with pytest.raises(RuntimeError) as caught:
        service.open(Path("game"))

    assert caught.value is error
    assert calls[-1] == (
        "project.operation", error, {"operation": "open", "status": "failed"},
    )


@pytest.mark.parametrize("failed_status", ("started", "succeeded", "failed"))
def test_event_logger_failure_does_not_change_operation_result(failed_status):
    result, events = object(), []

    def event_logger(event, **context):
        events.append(context["status"])
        if context["status"] == failed_status:
            raise OSError("log sink")

    service = make_service(Projects([], result), event_logger, lambda *_args, **_kwargs: "incident")

    if failed_status == "failed":
        error = ProjectError(Diagnostic("PROJECT_OPEN_FAILED", "private"))
        service = make_service(Projects([], error=error), event_logger, lambda *_args, **_kwargs: "incident")
        with pytest.raises(ProjectError) as caught:
            service.open(Path("game"))
        assert caught.value is error
    else:
        assert service.create(Path("game"), "Game") is result
    assert events[0] == "started"


def test_exception_logger_failure_does_not_replace_primary_error():
    error = RuntimeError("primary")
    service = make_service(
        Projects([], error=error), lambda *_args, **_kwargs: None,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("log sink")),
    )

    with pytest.raises(RuntimeError) as caught:
        service.open(Path("game"))

    assert caught.value is error


def test_event_logger_failure_does_not_prevent_session_update():
    published, calls = object(), []

    class Session:
        def __init__(self):
            self.snapshot = object()
            self.documents = {}

        def replace_snapshot(self, snapshot):
            calls.append(snapshot)
            self.snapshot = snapshot

    session = Session()
    service = make_service(
        Projects(calls, result=published),
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("log sink")),
        lambda *_args, **_kwargs: "incident",
    )

    assert service.add_to_session(session, "Card", "card") == "card"
    assert session.snapshot is published
    assert calls.count(published) == 1


def test_service_instances_use_only_their_configured_loggers():
    first, second = [], []
    make_service(Projects([]), lambda *_args, **kw: first.append(kw), lambda *_a, **_k: "one").create(
        Path("one"), "One"
    )
    make_service(Projects([]), lambda *_args, **kw: second.append(kw), lambda *_a, **_k: "two").open(Path("two"))

    assert [call["operation"] for call in first] == ["create", "create"]
    assert [call["operation"] for call in second] == ["open", "open"]


def test_bootstrap_callbacks_write_safe_real_log_and_report(monkeypatch, tmp_path):
    from componentpress import bootstrap
    from componentpress.diagnostics import logging as diagnostics

    diagnostics.shutdown()
    monkeypatch.setenv("COMPONENTPRESS_LOG_DIR", str(tmp_path / "logs"))
    projects = Projects([])
    monkeypatch.setattr(bootstrap, "project_repository", lambda: projects)
    monkeypatch.setattr(bootstrap, "resource_repository", object)
    diagnostics.initialize()
    try:
        service = bootstrap.project_service()
        service.create(Path("game"), "Game")
        secret = "SECRET_PROJECT_PATH_AND_EXCEPTION"
        projects.error = RuntimeError(secret)
        with pytest.raises(RuntimeError):
            service.open(Path(secret))

        report = diagnostics.create_report(tmp_path / "support.zip")
        with zipfile.ZipFile(report) as archive:
            members = [archive.read(name) for name in archive.namelist()]
            records = b"\n".join(
                content for name, content in zip(archive.namelist(), members)
                if name.endswith(".log")
            )
        assert secret.encode() not in b"\n".join(members)
        assert b'"status":"started"' in records
        assert b'"status":"succeeded"' in records
        assert b'"status":"failed"' in records
        assert b'"incident_id"' in records
        assert secret.encode() not in records
    finally:
        diagnostics.shutdown()
