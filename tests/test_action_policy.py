from componentpress.editor.action_policy import (
    BuildContext, EditingContext, HistoryContext, HistoryTarget, OperationContext, build_actions,
    editing_actions, history_target, operation_allowed,
)


def test_history_target_matches_text_focus_and_layout_rules() -> None:
    assert history_target(HistoryContext(True, False, True, True, True)) is HistoryTarget.TEXT
    assert history_target(HistoryContext(True, False, False, True, True)) is HistoryTarget.PROJECT
    assert history_target(HistoryContext(True, False, False, True, False)) is HistoryTarget.NONE
    assert history_target(HistoryContext(False, False, False, True, True)) is HistoryTarget.DOCUMENT
    assert history_target(HistoryContext(False, True, False, True, True)) is HistoryTarget.PROJECT


def test_editing_actions_respect_reservation_locks_and_operation_scope() -> None:
    selected = EditingContext(True, False, False, False, 2, True, True, False, False)
    actions = editing_actions(selected)
    assert actions.duplicate and actions.delete
    assert not actions.group and not actions.align

    frozen = editing_actions(EditingContext(True, True, True, False, 1, True, True, True, True))
    assert not frozen.layout and not frozen.text and not frozen.delete

    rendering = editing_actions(EditingContext(True, True, False, False, 1, True, True, True, True))
    assert rendering.text


def test_build_actions_require_project_tab_and_idle_state() -> None:
    project = build_actions(BuildContext(True, True, False, False, False))
    assert project.build_all and project.build_active and project.validate_all and project.validate_active

    reserved = build_actions(BuildContext(True, True, False, False, True))
    assert not reserved.idle and not reserved.build_all and not reserved.validate_active

    no_project = build_actions(BuildContext(False, False, False, False, False))
    assert no_project.idle and not no_project.build_all and not no_project.build_active


def test_operation_admission_allows_independent_work_and_blocks_conflicts() -> None:
    idle = OperationContext(False, False, False, False, False)
    preview = OperationContext(True, False, False, False, False)
    pending_build = OperationContext(False, True, False, False, False)
    build = OperationContext(False, False, True, False, False)
    png = OperationContext(False, False, False, True, False)
    version = OperationContext(False, False, False, False, True)

    assert all(operation_allowed(idle, name) for name in ("project_switch", "png_export", "build", "version", "resource_write", "close_tab"))
    assert operation_allowed(preview, "png_export") and operation_allowed(preview, "build")
    assert operation_allowed(preview, "version") and not operation_allowed(preview, "resource_write")
    assert not operation_allowed(preview, "project_switch")
    for context in (pending_build, build, png, version):
        assert not operation_allowed(context, "project_switch")
        assert not operation_allowed(context, "png_export")
        assert not operation_allowed(context, "close_tab")
    assert not operation_allowed(build, "version")
    assert operation_allowed(png, "view")
    assert not operation_allowed(png, "unlisted_operation")
