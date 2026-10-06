"""Qt item models for component and reverse-layer element trees."""

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QAbstractItemView, QTreeView

from componentpress.application.sessions import ProjectSession
from componentpress.domain.component import ComponentDefinition
from componentpress.domain.nodes import GroupNode, HtmlNode, ImageNode, Node


ID_ROLE = int(Qt.ItemDataRole.UserRole) + 1
TYPE_ROLE = int(Qt.ItemDataRole.UserRole) + 2


class LayerTreeView(QTreeView):
    """Layer tree whose drag is interpreted by the document model."""

    layerDropRequested = Signal(object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QTreeView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        # The persistent document model performs the move.  Qt must treat the
        # gesture as a copy, otherwise InternalMove removes a row from the
        # freshly rebuilt presentation model after our synchronous drop.
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)

    def startDrag(self, _supported_actions) -> None:
        super().startDrag(Qt.DropAction.CopyAction)

    def dropEvent(self, event) -> None:
        target = self.indexAt(event.position().toPoint())
        target_id = target.data(ID_ROLE) if target.isValid() else None
        ids = tuple(index.data(ID_ROLE) for index in self.selectionModel().selectedRows(0))
        if target_id and ids:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            # Rebuilding the model inside QAbstractItemView.dropEvent leaves
            # its internal source indexes dangling.  Apply after the gesture
            # has fully unwound instead.
            QTimer.singleShot(0, lambda: self.layerDropRequested.emit(ids, target_id))
        else:
            event.ignore()


def component_model(session: ProjectSession) -> QStandardItemModel:
    model = QStandardItemModel()
    model.setHorizontalHeaderLabels(["Компоненты"])
    for ref in session.snapshot.model.components:
        document = session.documents[ref.id]
        item = QStandardItem(document.model.name)
        item.setEditable(False)
        item.setData(ref.id, ID_ROLE)
        item.setToolTip(ref.path)
        model.appendRow(item)
    return model


def _node_label(node: Node) -> str:
    return node.name


def _node_type(node: Node) -> str:
    if isinstance(node, GroupNode):
        return "Группа"
    if isinstance(node, ImageNode):
        return "Изображение"
    if isinstance(node, HtmlNode):
        return "HTML-текст"
    return type(node).__name__


def _node_row(node: Node) -> list[QStandardItem]:
    name = QStandardItem(_node_label(node))
    kind = QStandardItem(_node_type(node))
    for item in (name, kind):
        item.setEditable(False)
        item.setData(node.id, ID_ROLE)
        item.setData(node.type, TYPE_ROLE)
    if isinstance(node, GroupNode):
        for child in reversed(node.children):
            name.appendRow(_node_row(child))
    return [name, kind]


def element_model(component: ComponentDefinition) -> QStandardItemModel:
    model = QStandardItemModel()
    model.setHorizontalHeaderLabels(["Элемент", "Тип"])
    for node in reversed(component.elements):
        model.appendRow(_node_row(node))
    return model
