"""Qt event-loop waiting for synchronous command-line build entry points."""

from PySide6.QtCore import QEventLoop

from componentpress.application.contracts import BuildRequest, BuildResult


def run_build(service, session, request: BuildRequest, *, on_progress=None) -> BuildResult:
    loop = QEventLoop()
    result: BuildResult | None = None

    def finish(value: BuildResult) -> None:
        nonlocal result
        result = value
        loop.quit()

    service.start(session, request, on_progress=on_progress, on_finished=finish)
    if result is None:
        loop.exec()
    assert result is not None
    return result
