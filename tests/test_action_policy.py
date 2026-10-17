from componentpress.editor.action_policy import (
    BuildContext, EditingContext, HistoryContext, HistoryTarget, build_actions, editing_actions, history_target,
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
