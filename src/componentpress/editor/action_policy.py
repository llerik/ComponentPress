"""Pure editor decisions shared by action controls and command handlers."""

from dataclasses import dataclass
from enum import Enum, auto


class HistoryTarget(Enum):
    NONE = auto()
    TEXT = auto()
    DOCUMENT = auto()
    PROJECT = auto()


@dataclass(frozen=True)
class HistoryContext:
    text_mode: bool
    project_focus: bool
    text_available: bool
    document_available: bool
    project_available: bool


def history_target(context: HistoryContext) -> HistoryTarget:
    if context.text_mode and not context.project_focus and context.text_available:
        return HistoryTarget.TEXT
    if context.project_focus and context.project_available:
        return HistoryTarget.PROJECT
    if not context.text_mode and context.document_available:
        return HistoryTarget.DOCUMENT
    return HistoryTarget.PROJECT if context.project_available else HistoryTarget.NONE


@dataclass(frozen=True)
class BuildContext:
    has_project: bool
    has_tab: bool
    build_active: bool
    version_busy: bool
    reserved: bool


@dataclass(frozen=True)
class BuildActions:
    idle: bool
    build_all: bool
    build_active: bool
    validate_all: bool
    validate_active: bool


def build_actions(context: BuildContext) -> BuildActions:
    idle = not context.build_active and not context.version_busy and not context.reserved
    return BuildActions(
        idle,
        context.has_project and idle,
        context.has_tab and idle,
        context.has_project and idle,
        context.has_tab and idle,
    )


@dataclass(frozen=True)
class EditingContext:
    has_tab: bool
    text_mode: bool
    reserved: bool
    version_busy: bool
    selection_count: int
    selection_unlocked: bool
    subtrees_unlocked: bool
    same_parent: bool
    selected_is_group: bool


@dataclass(frozen=True)
class EditingActions:
    layout: bool
    text: bool
    selection: bool
    layer_order: bool
    delete: bool
    duplicate: bool
    group: bool
    ungroup: bool
    align: bool


def editing_actions(context: EditingContext) -> EditingActions:
    editable = not context.reserved and not context.version_busy
    layout = context.has_tab and not context.text_mode and editable
    text = context.has_tab and context.text_mode and editable
    selection = layout and context.selection_count > 0
    unlocked = context.selection_unlocked and context.subtrees_unlocked
    return EditingActions(
        layout, text, selection and unlocked, selection and context.selection_unlocked,
        selection and unlocked, selection and unlocked,
        selection and context.selection_count > 1 and unlocked and context.same_parent,
        selection and context.selection_count == 1 and context.selected_is_group and unlocked,
        selection and context.selection_count > 1 and context.selection_unlocked and context.same_parent,
    )
