"""Qt item models for component and reverse-layer element trees."""

from PySide6.QtCore import QPoint, QItemSelectionModel, QTimer, QSize, Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QStyle
from PySide6.QtWidgets import QAbstractItemView, QStyleOptionViewItem, QStyledItemDelegate, QTreeView

from componentpress.i18n import tr
from componentpress.application.sessions import ProjectSession
from componentpress.domain.component import ComponentDefinition
from componentpress.domain.nodes import GroupNode, HtmlNode, ImageNode, Node
from componentpress.app_assets import set_themed_icon


ID_ROLE = int(Qt.ItemDataRole.UserRole) + 1
TYPE_ROLE = int(Qt.ItemDataRole.UserRole) + 2


class LayerTreeView(QTreeView):
    """Layer tree whose drag is interpreted by the document model."""

    layerDropRequested = Signal(object, object, object, object)
    lockToggleRequested = Signal(str)
    nodeContextMenuRequested = Signal(str, QPoint)

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
        self.setItemDelegate(LockIconDelegate(self))

    def lock_icon_rect(self, index):
        option = QStyleOptionViewItem()
        option.initFrom(self)
        option.rect = self.visualRect(index)
        option.widget = self
        self.itemDelegateForIndex(index).initStyleOption(option, index)
        return self.style().subElementRect(QStyle.SubElement.SE_ItemViewItemDecoration, option, self)

    def mousePressEvent(self, event) -> None:
        index = self.indexAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.RightButton:
            if index.isValid():
                if not self.selectionModel().isSelected(index):
                    self.selectionModel().clearSelection()
                    self.selectionModel().select(
                        index, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
                    )
                self.nodeContextMenuRequested.emit(str(index.data(ID_ROLE)), event.position().toPoint())
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and index.isValid() and index.column() == 0:
            if self.lock_icon_rect(index).contains(event.position().toPoint()):
                self.lockToggleRequested.emit(str(index.data(ID_ROLE)))
                event.accept()
                return
        super().mousePressEvent(event)
    def startDrag(self, _supported_actions) -> None:
        super().startDrag(Qt.DropAction.CopyAction)

    def dropEvent(self, event) -> None:
        target = self.indexAt(event.position().toPoint())
        target_id = target.data(ID_ROLE) if target.isValid() else None
        ids = tuple(index.data(ID_ROLE) for index in self.selectionModel().selectedRows(0))
        position = self.dropIndicatorPosition()
        if ids and (target_id or position == QAbstractItemView.DropIndicatorPosition.OnViewport):
            model = self.model()
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            # Rebuilding the model inside QAbstractItemView.dropEvent leaves
            # its internal source indexes dangling.  Apply after the gesture
            # has fully unwound instead.
            QTimer.singleShot(
                0,
                lambda: self.layerDropRequested.emit(model, ids, target_id, position)
                if self.model() is model else None,
            )
        else:
            event.ignore()


class LockIconDelegate(QStyledItemDelegate):
    """Standard item delegate used to locate the clickable lock decoration."""

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        option.decorationSize = QSize(22, 22)


def component_model(session: ProjectSession) -> QStandardItemModel:
    model = QStandardItemModel()
    model.setHorizontalHeaderLabels([tr("ui.component_header")])
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
        return tr("ui.component_type_group")
    if isinstance(node, ImageNode):
        return tr("ui.image")
    if isinstance(node, HtmlNode):
        return tr("ui.html_text")
    return type(node).__name__


def _node_row(node: Node, locked_ids: frozenset[str]) -> list[QStandardItem]:
    name = QStandardItem(_node_label(node))
    kind = QStandardItem(_node_type(node))
    for item in (name, kind):
        item.setEditable(False)
        item.setData(node.id, ID_ROLE)
        item.setData(node.type, TYPE_ROLE)
    if isinstance(node, GroupNode):
        for child in reversed(node.children):
            name.appendRow(_node_row(child, locked_ids))
    locked = node.id in locked_ids
    set_themed_icon(name, "lock.png" if locked else "lock_open.png")
    name.setToolTip((tr("ui.item_locked_group") if isinstance(node, GroupNode) else tr("ui.item_locked")) if locked else tr("ui.item_lock_hint"))
    name.setData(locked, LOCKED_ROLE)
    return [name, kind]


LOCKED_ROLE = ID_ROLE + 2


def element_model(component: ComponentDefinition) -> QStandardItemModel:
    model = QStandardItemModel()
    model.setHorizontalHeaderLabels([tr("ui.layer_header"), tr("ui.layer_type_header")])
    from componentpress.domain.tree import effectively_locked_ids

    locked_ids = effectively_locked_ids(component)
    for node in reversed(component.elements):
        model.appendRow(_node_row(node, locked_ids))
    return model
