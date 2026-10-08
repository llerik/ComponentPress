"""Guards for changes requested from the visual editor."""

from componentpress.domain.component import ComponentDefinition
from componentpress.domain.diagnostics import Diagnostic, ProjectError
from componentpress.domain.tree import effectively_locked_ids, locations


def ensure_visual_edit_respects_locks(before: ComponentDefinition, after: ComponentDefinition) -> None:
    """Keep every previously protected node intact during a visual edit.

    Changing the explicit lock flag is permitted as a dedicated visual action.
    Moving an unrelated sibling across a protected node is also permitted.
    YAML edits have a separate preparation path and intentionally skip this guard.
    """
    old_index = locations(before)
    new_index = locations(after)
    for node_id in effectively_locked_ids(before):
        old = old_index[node_id]
        new = new_index.get(node_id)
        if new is None:
            raise ProjectError(Diagnostic(
                "NODE_LOCKED", "визуальную операцию нельзя применять к заблокированному элементу", None, f"elements.{node_id}"
            ))
        old_values = old.node.model_dump(mode="python")
        new_values = new.node.model_dump(mode="python")
        old_values.pop("locked", None)
        new_values.pop("locked", None)
        if old_values != new_values or old.parent_id != new.parent_id or (old.global_x, old.global_y) != (new.global_x, new.global_y):
            raise ProjectError(Diagnostic(
                "NODE_LOCKED", "визуальную операцию нельзя применять к заблокированному элементу", None, f"elements.{node_id}"
            ))
