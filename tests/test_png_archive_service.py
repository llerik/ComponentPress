from __future__ import annotations

from pathlib import Path

import pytest

from componentpress.application.contracts import PngArchiveResult
from componentpress.application.png_archive import export_png_archive
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.execution.cancellation import CancellationToken


class FakeRuntime:
    def __init__(self, fail_at: str | None = None, cleanup=()):
        self.calls = []
        self.fail_at = fail_at
        self.cleanup = cleanup
        self.error = None

    def _call(self, name):
        self.calls.append(name)
        if name == self.fail_at:
            raise ProjectError(Diagnostic("PNG_TEST_FAILURE", "test failure"))

    def begin(self, *_args):
        self._call("begin")
        return Path("job")

    def prepare(self, *_args):
        self._call("prepare")
        return object()

    def render(self, *_args):
        self._call("render")

    def publish(self, *_args):
        self._call("publish")
        return PngArchiveResult(Path("result.zip"), "prod", 1, 1)

    def finish(self, _job_id, published, error):
        self.calls.append("finish")
        self.error = error
        self.published = published
        return self.cleanup


def test_png_archive_scenario_orders_phases_and_keeps_post_publish_warning():
    runtime = FakeRuntime(cleanup=(Diagnostic("CLEANUP_PENDING", "pending", severity="warning"),))
    result = export_png_archive(object(), Path("result.zip"), runtime=runtime, cancellation=CancellationToken())
    assert runtime.calls == ["begin", "prepare", "render", "publish", "finish"]
    assert runtime.published
    assert result.path == Path("result.zip")
    assert [item.code for item in result.diagnostics] == ["CLEANUP_PENDING"]


def test_png_archive_scenario_preserves_primary_error_when_finalizing():
    runtime = FakeRuntime(fail_at="prepare", cleanup=(Diagnostic("CLEANUP_PENDING", "pending", severity="warning"),))
    with pytest.raises(ProjectError, match="PNG_TEST_FAILURE"):
        export_png_archive(object(), Path("result.zip"), runtime=runtime, cancellation=CancellationToken())
    assert runtime.calls == ["begin", "prepare", "finish"]
    assert not runtime.published
    assert runtime.error.diagnostic.code == "PNG_TEST_FAILURE"
