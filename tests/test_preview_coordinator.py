from componentpress.preview_coordinator import (
    PreviewCoordinator,
    PreviewIdentity,
    PreviewJob,
    PreviewPhase,
    PreviewRequest,
)


def job(request_id: str, *, tab: str = "tab", generation: int = 1) -> PreviewJob:
    identity = PreviewIdentity("project", "card", tab, 1, generation, request_id)
    request = PreviewRequest(identity, None, None, 96, False, None, (), False)
    return PreviewJob(request, object(), object())


def test_active_preview_keeps_only_the_latest_pending_request() -> None:
    queue = PreviewCoordinator()
    first, second, latest = job("a"), job("b"), job("c", generation=2)

    assert queue.submit(first) is first
    assert queue.submit(second) is None
    assert queue.submit(latest) is None
    transition = queue.finish("a", PreviewPhase.FONTS)

    assert transition.finished is first
    assert transition.start is latest
    assert queue.active is latest
    assert queue.pending is None


def test_each_completion_must_match_active_request_and_phase() -> None:
    queue = PreviewCoordinator()
    first, second = job("a"), job("b")
    assert queue.submit(first) is first
    assert queue.begin_render("a") is first
    assert queue.begin_render("a") is None
    assert queue.finish("a", PreviewPhase.FONTS).finished is None
    assert queue.finish("a", PreviewPhase.RENDER).finished is first
    assert queue.submit(second) is second

    assert queue.finish("a", PreviewPhase.RENDER).finished is None
    assert queue.active is second
    assert queue.phase is PreviewPhase.FONTS


def test_discard_and_shutdown_preserve_only_running_work() -> None:
    queue = PreviewCoordinator()
    first, pending = job("a"), job("b", tab="closing-tab")
    assert queue.submit(first) is first
    queue.submit(pending)

    queue.discard_pending("closing-tab")
    queue.shutdown()

    assert queue.submit(job("c")) is None
    transition = queue.finish("a", PreviewPhase.FONTS)
    assert transition.finished is first
    assert transition.start is None
    assert queue.active is None


def test_identity_distinguishes_reopened_session_and_tab_with_same_component_id() -> None:
    original = job("a").request.identity
    reopened = PreviewIdentity("new-session", "card", "new-tab", 1, 1, "b")
    edited = PreviewIdentity("project", "card", "tab", 2, 2, "c")

    assert original.component_id == reopened.component_id
    assert original.project != reopened.project
    assert original.tab != reopened.tab
    assert original.request != reopened.request
    assert original.revision != edited.revision
    assert original.generation != edited.generation
