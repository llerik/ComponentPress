"""Component properties shown for an active document."""

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from componentpress.domain.component import ComponentDefinition
from componentpress.domain.nodes import GroupNode, HtmlNode, ImageNode, Node


class ComponentProperties(QWidget):
    nameChanged = Signal(str)
    sizeChanged = Signal(float, float)
    backgroundChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("componentProperties")
        layout = QFormLayout(self)
        self.identity = QLabel()
        self.identity.setObjectName("componentIdentity")
        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("componentName")
        self.width_spin = self._size_spin("componentWidth")
        self.height_spin = self._size_spin("componentHeight")
        self.background_edit = QLineEdit()
        self.background_edit.setObjectName("componentBackground")
        self.background_edit.setPlaceholderText("#FFFFFF")
        layout.addRow("ID", self.identity)
        layout.addRow("Название", self.name_edit)
        layout.addRow("Ширина, мм", self.width_spin)
        layout.addRow("Высота, мм", self.height_spin)
        layout.addRow("Фон", self.background_edit)
        self.name_edit.editingFinished.connect(lambda: self.nameChanged.emit(self.name_edit.text()))
        self.width_spin.editingFinished.connect(self._emit_size)
        self.height_spin.editingFinished.connect(self._emit_size)
        self.background_edit.editingFinished.connect(
            lambda: self.backgroundChanged.emit(self.background_edit.text())
        )
        self.setEnabled(False)

    @staticmethod
    def _size_spin(name: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setObjectName(name)
        spin.setRange(0.001, 10000.0)
        spin.setDecimals(3)
        spin.setSingleStep(1.0)
        return spin

    def _emit_size(self) -> None:
        self.sizeChanged.emit(self.width_spin.value(), self.height_spin.value())

    def show_component(self, component: ComponentDefinition | None) -> None:
        self.setEnabled(component is not None)
        if component is None:
            return
        blockers = [
            QSignalBlocker(self.name_edit),
            QSignalBlocker(self.width_spin),
            QSignalBlocker(self.height_spin),
            QSignalBlocker(self.background_edit),
        ]
        self.identity.setText(component.id)
        self.name_edit.setText(component.name)
        self.width_spin.setValue(component.size_mm.width)
        self.height_spin.setValue(component.size_mm.height)
        self.background_edit.setText(component.background)
        del blockers


class ElementProperties(QWidget):
    """Exact geometry and type-specific values for one selected node."""

    nodeChanged = Signal(dict)
    insertImageRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("elementProperties")
        outer = QVBoxLayout(self)
        form = QFormLayout()
        self.form = form
        self.identity = QLabel()
        self.identity.setObjectName("elementIdentity")
        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("elementName")
        self.x_spin = self._number("elementX", -10000.0, 10000.0)
        self.y_spin = self._number("elementY", -10000.0, 10000.0)
        self.width_spin = self._number("elementWidth", 0.001, 10000.0)
        self.height_spin = self._number("elementHeight", 0.001, 10000.0)
        self.source_edit = QLineEdit()
        self.source_edit.setObjectName("imageSource")
        self.fit_combo = QComboBox()
        self.fit_combo.setObjectName("imageFit")
        self.fit_combo.addItems(["contain", "cover", "stretch"])
        self.font_edit = QLineEdit()
        self.font_edit.setObjectName("htmlFont")
        self.font_spin = self._number("htmlFontSize", 0.001, 1000.0)
        self.color_edit = QLineEdit()
        self.color_edit.setObjectName("htmlColor")
        form.addRow("Элемент", self.identity)
        form.addRow("Название", self.name_edit)
        form.addRow("X, мм", self.x_spin)
        form.addRow("Y, мм", self.y_spin)
        form.addRow("Ширина, мм", self.width_spin)
        form.addRow("Высота, мм", self.height_spin)
        form.addRow("Изображение", self.source_edit)
        form.addRow("Режим", self.fit_combo)
        form.addRow("Шрифт", self.font_edit)
        form.addRow("Размер, pt", self.font_spin)
        form.addRow("Цвет", self.color_edit)
        outer.addLayout(form)
        self.html_edit = QPlainTextEdit()
        self.html_edit.setObjectName("htmlSource")
        self.html_edit.setMinimumHeight(100)
        outer.addWidget(self.html_edit)
        buttons = QHBoxLayout()
        self.insert_image = QPushButton("Вставить изображение в текст…")
        self.insert_image.setObjectName("insertHtmlImage")
        self.apply_html = QPushButton("Применить HTML")
        self.apply_html.setObjectName("applyHtml")
        buttons.addWidget(self.insert_image)
        buttons.addWidget(self.apply_html)
        outer.addLayout(buttons)
        for spin in (self.x_spin, self.y_spin, self.width_spin, self.height_spin):
            spin.editingFinished.connect(self._geometry_changed)
        self.name_edit.editingFinished.connect(lambda: self.nodeChanged.emit({"name": self.name_edit.text()}))
        self.source_edit.editingFinished.connect(lambda: self.nodeChanged.emit({"source": self.source_edit.text()}))
        self.fit_combo.currentTextChanged.connect(lambda value: self.nodeChanged.emit({"fit": value}))
        self.font_edit.editingFinished.connect(lambda: self.nodeChanged.emit({"font_family": self.font_edit.text()}))
        self.font_spin.editingFinished.connect(lambda: self.nodeChanged.emit({"font_size_pt": self.font_spin.value()}))
        self.color_edit.editingFinished.connect(lambda: self.nodeChanged.emit({"color": self.color_edit.text()}))
        self.apply_html.clicked.connect(lambda: self.nodeChanged.emit({"html": self.html_edit.toPlainText()}))
        self.insert_image.clicked.connect(self.insertImageRequested)
        self.setEnabled(False)

    @staticmethod
    def _number(name: str, minimum: float, maximum: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setObjectName(name)
        spin.setRange(minimum, maximum)
        spin.setDecimals(3)
        spin.setSingleStep(1.0)
        return spin

    def _geometry_changed(self) -> None:
        changes: dict[str, float] = {"x_mm": self.x_spin.value(), "y_mm": self.y_spin.value()}
        if self.width_spin.isVisible():
            changes.update(width_mm=self.width_spin.value(), height_mm=self.height_spin.value())
        self.nodeChanged.emit(changes)

    def show_node(self, node: Node | None) -> None:
        self.setEnabled(node is not None)
        if node is None:
            return
        widgets = (
            self.x_spin, self.y_spin, self.width_spin, self.height_spin, self.name_edit,
            self.source_edit, self.fit_combo, self.font_edit, self.font_spin, self.color_edit,
            self.html_edit,
        )
        blockers = [QSignalBlocker(widget) for widget in widgets]
        self.identity.setText(f"{node.id} ({node.type})")
        self.x_spin.setValue(node.x_mm)
        self.y_spin.setValue(node.y_mm)
        sized = isinstance(node, (ImageNode, HtmlNode))
        self.form.setRowVisible(self.width_spin, sized)
        self.form.setRowVisible(self.height_spin, sized)
        if sized:
            self.width_spin.setValue(node.width_mm)
            self.height_spin.setValue(node.height_mm)
        image = isinstance(node, ImageNode)
        html = isinstance(node, HtmlNode)
        self.form.setRowVisible(self.name_edit, True)
        self.name_edit.setText(node.name)
        self.form.setRowVisible(self.source_edit, image)
        self.form.setRowVisible(self.fit_combo, image)
        if image:
            self.source_edit.setText(node.source)
            self.fit_combo.setCurrentText(node.fit)
        for widget in (self.font_edit, self.font_spin, self.color_edit):
            self.form.setRowVisible(widget, html)
        for widget in (self.html_edit, self.insert_image, self.apply_html):
            widget.setVisible(html)
        if html:
            self.font_edit.setText(node.font_family)
            self.font_spin.setValue(node.font_size_pt)
            self.color_edit.setText(node.color)
            self.html_edit.setPlainText(node.html)
        del blockers

    def insert_html_image(self, source: str, width: int, height: int, align: str) -> None:
        cursor = self.html_edit.textCursor()
        alignment = f' align="{align}"' if align else ""
        cursor.insertText(f'<img src="{source}" width="{width}" height="{height}"{alignment} />')
        self.html_edit.setTextCursor(cursor)
