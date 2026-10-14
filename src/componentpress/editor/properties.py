"""Component properties shown for an active document."""

import math
import re

from PySide6.QtCore import QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontDatabase, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
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

from componentpress.i18n import tr
from componentpress.domain.component import ComponentDefinition
from componentpress.domain.nodes import ConditionalGroupNode, GroupNode, HtmlNode, ImageNode, LineNode, Node, ShapeNode


class ComponentProperties(QWidget):
    nameChanged = Signal(str)
    sizeChanged = Signal(float, float)
    backgroundChanged = Signal(str)
    backgroundColorRequested = Signal()
    sheetChanged = Signal(object)

    def __init__(self, parent=None, *, sheets: tuple[str, ...] = ()):
        super().__init__(parent)
        self._available_sheets = sheets
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
        self.background_button = QPushButton(tr("ui.select_color"))
        self.background_button.setObjectName("componentBackgroundPicker")
        self.sheet = QComboBox()
        self.sheet.setObjectName("componentPropertySheet")
        self.sheet.addItem(tr("ui.no_sheet"), None)
        self.sheet.addItems(list(sheets))
        background_row = QHBoxLayout()
        background_row.addWidget(self.background_edit, 1)
        background_row.addWidget(self.background_button)
        layout.addRow(tr("ui.component_id"), self.identity)
        layout.addRow(tr("ui.name"), self.name_edit)
        layout.addRow(tr("ui.width_mm"), self.width_spin)
        layout.addRow(tr("ui.height_mm"), self.height_spin)
        layout.addRow(tr("ui.component_background"), background_row)
        layout.addRow(tr("ui.component_sheet"), self.sheet)
        self.name_edit.editingFinished.connect(lambda: self.nameChanged.emit(self.name_edit.text()))
        self.background_edit.editingFinished.connect(lambda: self.backgroundChanged.emit(self.background_edit.text()))
        self.width_spin.editingFinished.connect(self._emit_size)
        self.height_spin.editingFinished.connect(self._emit_size)
        self.background_button.clicked.connect(self.backgroundColorRequested)
        self.sheet.currentIndexChanged.connect(lambda _index: self.sheetChanged.emit(self.sheet.currentData()))
        self.setEnabled(False)

    def set_sheets(self, sheets: tuple[str, ...]) -> None:
        self._available_sheets = sheets

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
            QSignalBlocker(self.sheet),
        ]
        self.identity.setText(component.id)
        self.name_edit.setText(component.name)
        self.width_spin.setValue(component.size_mm.width)
        self.height_spin.setValue(component.size_mm.height)
        self.background_edit.setText(component.background)
        current_sheet = component.data.sheet if component.data else None
        self.sheet.clear()
        self.sheet.addItem(tr("ui.no_sheet"), None)
        for sheet in self._available_sheets:
            self.sheet.addItem(sheet, sheet)
        if current_sheet and self.sheet.findData(current_sheet) < 0:
            self.sheet.addItem(tr("ui.sheet_missing", sheet=current_sheet), current_sheet)
        self.sheet.setCurrentIndex(max(0, self.sheet.findData(current_sheet)))
        del blockers


class ElementProperties(QWidget):
    """Exact geometry and type-specific values for one selected node."""

    nodeChanged = Signal(dict)
    rotationRequested = Signal(float)
    insertImageRequested = Signal()
    colorRequested = Signal(str)

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
        self.rotation_spin = self._number("elementRotation", -360000.0, 360000.0)
        self.rotation_spin.setDecimals(1)
        self.rotation_spin.setSingleStep(0.1)
        self.rotation_spin.setSuffix("°")
        self._rotation_initial_value = 0.0
        self._rotation_text_edited = False
        self.optional_image = QCheckBox(tr("ui.image_optional"))
        self.optional_image.setObjectName("optionalImage")
        self.source_edit = QLineEdit()
        self.source_edit.setObjectName("imageSource")
        self.fit_combo = QComboBox()
        self.fit_combo.setObjectName("imageFit")
        self.fit_combo.addItems(["contain", "cover", "stretch"])
        self.content_mode = QComboBox()
        self.content_mode.setObjectName("contentMode")
        self.content_mode.addItem(tr("ui.content_manual"), "manual")
        self.content_mode.addItem(tr("ui.column"), "column")
        self.content_column = QComboBox()
        self.content_column.setObjectName("contentColumn")
        self.condition_column = QComboBox()
        self.condition_column.setObjectName("conditionColumn")
        self.condition_operator = QComboBox()
        self.condition_operator.setObjectName("conditionOperator")
        for operator, label in (("equals", tr("ui.condition_equals")), ("not_equals", tr("ui.condition_not_equals")), ("is_empty", tr("ui.condition_is_empty")), ("is_not_empty", tr("ui.condition_is_not_empty"))):
            self.condition_operator.addItem(label, operator)
        self.condition_value = QLineEdit()
        self.condition_value.setObjectName("conditionValue")
        self.condition_state = QLabel()
        self.condition_state.setObjectName("conditionState")
        self.stroke_color = QLineEdit()
        self.stroke_color.setObjectName("shapeStroke")
        self.stroke_color.setReadOnly(True)
        self.fill_color = QLineEdit()
        self.fill_color.setObjectName("shapeFill")
        self.fill_color.setReadOnly(True)
        self.stroke_width = self._number("shapeStrokeWidth", 0.001, 1000.0)
        self.stroke_style = QComboBox()
        self.stroke_style.setObjectName("shapeStrokeStyle")
        self.stroke_style.addItem(tr("ui.stroke_solid"), "solid")
        self.stroke_style.addItem(tr("ui.stroke_dash"), "dash")
        self.stroke_style.addItem(tr("ui.stroke_dot"), "dot")
        self.line_dx = self._number("lineDX", -10000.0, 10000.0)
        self.line_dy = self._number("lineDY", -10000.0, 10000.0)
        self.font_combo = QComboBox()
        self.font_combo.setObjectName("htmlFont")
        self.font_spin = self._number("htmlFontSize", 0.1, 1000.0)
        self.font_spin.setDecimals(1)
        self.font_spin.setSingleStep(0.1)
        self.color_edit = QLineEdit()
        self.color_edit.setObjectName("htmlColor")
        self.color_edit.setMaxLength(9)
        self.color_edit.setFixedWidth(self.color_edit.fontMetrics().horizontalAdvance("#AARRGGBB") + 18)
        self.color_swatch = QPushButton()
        self.color_swatch.setObjectName("htmlColorPicker")
        self.color_swatch.setFixedSize(28, 28)
        self.color_swatch.setAccessibleName(tr("ui.select_text_color"))
        self.color_swatch.setToolTip(tr("ui.select_text_color"))
        self.color_widget = QWidget()
        color_row = QHBoxLayout(self.color_widget)
        color_row.setContentsMargins(0, 0, 0, 0)
        color_row.setSpacing(6)
        color_row.addWidget(self.color_swatch)
        color_row.addWidget(self.color_edit)
        self.alignment_group = QButtonGroup(self)
        self.alignment_group.setExclusive(True)
        self.alignment_buttons: dict[str, QPushButton] = {}
        alignment_widget = QWidget()
        alignment_row = QHBoxLayout(alignment_widget)
        alignment_row.setContentsMargins(0, 0, 0, 0)
        from componentpress.app_assets import load_icon

        for value, label, icon in (("left", tr("ui.text_align_left"), "format_align_left.png"), ("center", tr("ui.text_align_center"), "format_align_center.png"), ("right", tr("ui.text_align_right"), "format_align_right.png")):
            button = QPushButton()
            button.setObjectName(f"htmlAlign{value.title()}")
            button.setCheckable(True)
            button.setProperty("alignment", value)
            button.setIcon(load_icon(icon))
            button.setIconSize(QSize(20, 20))
            button.setFixedSize(32, 32)
            button.setToolTip(label)
            button.setAccessibleName(label)
            self.alignment_group.addButton(button)
            self.alignment_buttons[value] = button
            alignment_row.addWidget(button)
        self.lock_button = QPushButton()
        self.lock_button.setObjectName("elementLocked")
        self.lock_button.setCheckable(True)
        self.lock_button.setFixedSize(34, 34)
        self.fill_button = QPushButton(tr("ui.select_color"))
        self.fill_button.setObjectName("shapeFillPicker")
        self.stroke_button = QPushButton(tr("ui.select_color"))
        self.stroke_button.setObjectName("shapeStrokePicker")
        self.no_fill_button = QPushButton(tr("ui.no_fill"))
        self.no_fill_button.setObjectName("shapeNoFill")
        fill_row = QWidget()
        self.fill_widget = fill_row
        fill_layout = QHBoxLayout(fill_row)
        fill_layout.setContentsMargins(0, 0, 0, 0)
        fill_layout.addWidget(self.fill_button)
        fill_layout.addWidget(self.no_fill_button)
        self.group_bounds = QLabel()
        self.group_bounds.setObjectName("groupBounds")
        self.quality_label = QLabel()
        self.quality_label.setObjectName("imageQuality")
        self.quality_label.setWordWrap(True)
        form.addRow(tr("ui.element"), self.identity)
        form.addRow(tr("ui.lock"), self.lock_button)
        form.addRow(tr("ui.group_bounds"), self.group_bounds)
        form.addRow(tr("ui.image_resolution"), self.quality_label)
        form.addRow(tr("ui.name"), self.name_edit)
        form.addRow(tr("ui.x_mm"), self.x_spin)
        form.addRow(tr("ui.y_mm"), self.y_spin)
        form.addRow(tr("ui.width_mm"), self.width_spin)
        form.addRow(tr("ui.height_mm"), self.height_spin)
        form.addRow(tr("ui.rotation"), self.rotation_spin)
        form.addRow(tr("ui.optional_image"), self.optional_image)
        form.addRow(tr("ui.image"), self.source_edit)
        form.addRow(tr("ui.content_source"), self.content_mode)
        form.addRow(tr("ui.excel_column"), self.content_column)
        form.addRow(tr("ui.condition_column"), self.condition_column)
        form.addRow(tr("ui.condition_operator"), self.condition_operator)
        form.addRow(tr("ui.condition_value"), self.condition_value)
        form.addRow(tr("ui.condition_state"), self.condition_state)
        form.addRow(tr("ui.fill"), fill_row)
        form.addRow(tr("ui.stroke"), self.stroke_button)
        form.addRow(tr("ui.stroke_width"), self.stroke_width)
        form.addRow(tr("ui.stroke_style"), self.stroke_style)
        form.addRow(tr("ui.line_end_dx"), self.line_dx)
        form.addRow(tr("ui.line_end_dy"), self.line_dy)
        form.addRow(tr("ui.mode"), self.fit_combo)
        form.addRow(tr("ui.font"), self.font_combo)
        form.addRow(tr("ui.size_pt"), self.font_spin)
        form.addRow(tr("ui.color"), self.color_widget)
        self.alignment_widget = alignment_widget
        form.addRow(tr("ui.text_alignment"), self.alignment_widget)
        outer.addLayout(form)
        self.html_edit = QPlainTextEdit()
        self.html_edit.setObjectName("htmlSource")
        self.html_edit.setMinimumHeight(100)
        outer.addWidget(self.html_edit)
        buttons = QHBoxLayout()
        self.insert_image = QPushButton(tr("ui.insert_image_html"))
        self.insert_image.setObjectName("insertHtmlImage")
        self.apply_html = QPushButton(tr("ui.apply_html"))
        self.apply_html.setObjectName("applyHtml")
        buttons.addWidget(self.insert_image)
        buttons.addWidget(self.apply_html)
        outer.addLayout(buttons)
        for spin in (self.x_spin, self.y_spin, self.width_spin, self.height_spin, self.stroke_width, self.line_dx, self.line_dy):
            spin.editingFinished.connect(self._geometry_changed)
        self.rotation_spin.editingFinished.connect(self._commit_rotation)
        self.rotation_spin.lineEdit().textEdited.connect(self._rotation_text_was_edited)
        self.optional_image.toggled.connect(lambda value: self.nodeChanged.emit({"optional": value}))
        self.name_edit.editingFinished.connect(lambda: self.nodeChanged.emit({"name": self.name_edit.text()}))
        self.source_edit.editingFinished.connect(lambda: self.nodeChanged.emit({"source": self.source_edit.text()}))
        self.content_mode.currentIndexChanged.connect(self._content_mode_changed)
        self.content_column.activated.connect(lambda _index: self._content_column_changed())
        self.condition_column.activated.connect(lambda _index: self.nodeChanged.emit({"condition_column": self.condition_column.currentText()}))
        self.condition_value.editingFinished.connect(lambda: self.nodeChanged.emit({"condition_value": self.condition_value.text()}))
        self.condition_operator.currentIndexChanged.connect(self._condition_operator_changed)
        self.stroke_color.editingFinished.connect(lambda: self.nodeChanged.emit({"stroke": self.stroke_color.text()}))
        self.fill_color.editingFinished.connect(lambda: self.nodeChanged.emit({"fill": self.fill_color.text() or None}))
        self.stroke_width.editingFinished.connect(lambda: self.nodeChanged.emit({"stroke_width_mm": self.stroke_width.value()}))
        self.stroke_style.currentIndexChanged.connect(lambda _index: self.nodeChanged.emit({"stroke_style": self.stroke_style.currentData()}))
        self.fit_combo.currentTextChanged.connect(lambda value: self.nodeChanged.emit({"fit": value}))
        self.font_combo.currentIndexChanged.connect(lambda _index: self.nodeChanged.emit({"font_family": self.font_combo.currentData()}))
        self.font_spin.editingFinished.connect(lambda: self.nodeChanged.emit({"font_size_pt": self.font_spin.value()}))
        self.color_edit.editingFinished.connect(self._commit_html_color)
        self.color_swatch.clicked.connect(lambda: self.colorRequested.emit(self.color_edit.text()))
        self.lock_button.toggled.connect(lambda checked: self.nodeChanged.emit({"locked": checked}))
        self.fill_button.clicked.connect(lambda: self.colorRequested.emit("shapeFill"))
        self.stroke_button.clicked.connect(lambda: self.colorRequested.emit("shapeStroke"))
        self.no_fill_button.clicked.connect(lambda: self.nodeChanged.emit({"fill": None}))
        self.alignment_group.buttonClicked.connect(
            lambda button: self.nodeChanged.emit({"text_align": button.property("alignment")})
        )
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

    def _set_lock_icon(self, locked: bool) -> None:
        from componentpress.app_assets import load_icon

        self.lock_button.setIcon(load_icon("lock.png" if locked else "lock_open.png"))
        self.lock_button.setIconSize(QSize(22, 22))
        self.lock_button.setAccessibleName(tr("ui.unlock_element") if locked else tr("ui.lock_element"))

    def _geometry_changed(self) -> None:
        changes: dict[str, float] = {"x_mm": self.x_spin.value(), "y_mm": self.y_spin.value()}
        if self.width_spin.isVisible():
            changes.update(width_mm=self.width_spin.value(), height_mm=self.height_spin.value())
        if self.line_dx.isVisible():
            changes.update(dx_mm=self.line_dx.value(), dy_mm=self.line_dy.value())
        if self.stroke_width.isVisible():
            changes["stroke_width_mm"] = self.stroke_width.value()
        self.nodeChanged.emit(changes)

    def _rotation_text_was_edited(self, _text: str) -> None:
        self._rotation_text_edited = True

    def _commit_rotation(self) -> None:
        if self._rotation_text_edited or self.rotation_spin.value() != self._rotation_initial_value:
            self.rotationRequested.emit(self.rotation_spin.value())
        self._rotation_text_edited = False
        self._rotation_initial_value = self.rotation_spin.value()

    def _commit_html_color(self) -> None:
        value = self.color_edit.text().upper()
        if not re.fullmatch(r"#[0-9A-F]{6}(?:[0-9A-F]{2})?", value):
            value = getattr(self, "_html_color", "#111111")
        self._html_color = value
        self.color_edit.setText(value)
        self._update_html_color_swatch(value)
        if value != getattr(self, "_node_color", value):
            self.nodeChanged.emit({"color": value})

    def _update_html_color_swatch(self, value: str) -> None:
        pixmap = QPixmap(24, 24)
        painter = QPainter(pixmap)
        for y in range(0, 24, 6):
            for x in range(0, 24, 6):
                color = Qt.GlobalColor.white if (x // 6 + y // 6) % 2 else Qt.GlobalColor.lightGray
                painter.fillRect(x, y, 6, 6, color)
        painter.fillRect(0, 0, 24, 24, QColor(value))
        painter.end()
        self.color_swatch.setIcon(QIcon(pixmap))
        self.color_swatch.setIconSize(QSize(24, 24))

    def _condition_operator_changed(self, _index: int) -> None:
        operator = self.condition_operator.currentData()
        changes = {"condition_operator": operator}
        if operator in {"is_empty", "is_not_empty"}:
            changes["condition_value"] = None
            self.form.setRowVisible(self.condition_value, False)
        else:
            changes["condition_value"] = self.condition_value.text()
            self.form.setRowVisible(self.condition_value, True)
        self.nodeChanged.emit(changes)

    def show_node(self, node: Node | None, columns: tuple[str, ...] = (), component: ComponentDefinition | None = None, condition_matches: bool | None = None, font_families: tuple[str, ...] = ()) -> None:
        self.setEnabled(node is not None)
        if node is None:
            return
        widgets = (
            self.x_spin, self.y_spin, self.width_spin, self.height_spin, self.rotation_spin, self.name_edit,
            self.source_edit, self.fit_combo, self.font_combo, self.font_spin, self.color_edit,
            self.html_edit, self.content_mode, self.content_column, self.font_combo,
            self.lock_button, self.condition_column, self.condition_operator, self.condition_value, self.optional_image, self.stroke_color,
            self.fill_color, self.stroke_width, self.stroke_style, self.line_dx, self.line_dy,
            *self.alignment_buttons.values(),
        )
        blockers = [QSignalBlocker(widget) for widget in widgets]
        self.identity.setText(f"{node.id} ({node.type})")
        self._node_type = node.type
        inherited_lock = False
        if component is not None:
            from componentpress.domain.tree import locations

            index = locations(component)
            parent_id = index[node.id].parent_id
            while parent_id is not None:
                if index[parent_id].node.locked:
                    inherited_lock = True
                    break
                parent_id = index[parent_id].parent_id
        effectively_locked = node.locked or inherited_lock
        self.lock_button.setChecked(effectively_locked)
        self._set_lock_icon(effectively_locked)
        self.lock_button.setEnabled(not inherited_lock)
        self.lock_button.setToolTip(tr("ui.unlock_parent") if inherited_lock else (tr("ui.unlock_element") if effectively_locked else tr("ui.lock_element")))
        self.form.setRowVisible(self.lock_button, True)
        self.x_spin.setValue(node.x_mm)
        self.y_spin.setValue(node.y_mm)
        sized = isinstance(node, (ImageNode, HtmlNode, GroupNode))
        self.form.setRowVisible(self.width_spin, sized)
        self.form.setRowVisible(self.height_spin, sized)
        self.rotation_spin.setValue(math.degrees(math.atan2(node.transform.m21, node.transform.m11)) % 360)
        self._rotation_initial_value = self.rotation_spin.value()
        self._rotation_text_edited = False
        self.form.setRowVisible(self.rotation_spin, True)
        line = isinstance(node, LineNode)
        shape = isinstance(node, ShapeNode)
        self.form.setRowVisible(self.line_dx, line)
        self.form.setRowVisible(self.line_dy, line)
        self.form.setRowVisible(self.fill_widget, shape)
        self.form.setRowVisible(self.stroke_button, shape or line)
        self.form.setRowVisible(self.stroke_width, shape or line)
        self.form.setRowVisible(self.stroke_style, shape or line)
        if line:
            self.line_dx.setValue(node.dx_mm)
            self.line_dy.setValue(node.dy_mm)
        if shape or line:
            self.stroke_color.setText(node.stroke)
            self.stroke_button.setStyleSheet(f"background-color: {node.stroke}")
            self.stroke_width.setValue(node.stroke_width_mm)
            self.stroke_style.setCurrentIndex(max(0, self.stroke_style.findData(node.stroke_style)))
        if shape:
            self.fill_color.setText(node.fill or "")
            self.fill_button.setStyleSheet(f"background-color: {node.fill or 'transparent'}")
        is_group = isinstance(node, GroupNode)
        self.form.setRowVisible(self.group_bounds, is_group)
        conditional = isinstance(node, ConditionalGroupNode)
        self.form.setRowVisible(self.condition_column, conditional)
        self.form.setRowVisible(self.condition_operator, conditional)
        self.form.setRowVisible(self.condition_value, conditional)
        self.form.setRowVisible(self.condition_state, conditional)
        if conditional:
            self.condition_column.clear()
            self.condition_column.addItems(list(columns))
            if self.condition_column.findText(node.condition_column) < 0:
                self.condition_column.addItem(node.condition_column)
            self.condition_column.setCurrentText(node.condition_column)
            self.condition_value.setText(node.condition_value or "")
            self.condition_operator.setCurrentIndex(max(0, self.condition_operator.findData(node.condition_operator)))
            self.condition_value.setEnabled(node.condition_operator in {"equals", "not_equals"})
            self.form.setRowVisible(self.condition_value, node.condition_operator in {"equals", "not_equals"})
            self.condition_state.setText(tr("ui.condition_visible") if condition_matches is True else tr("ui.condition_hidden") if condition_matches is False else tr("ui.condition_no_row"))
        if is_group and component is not None:
            from componentpress.domain.tree import node_bounds

            _, _, bounds_width, bounds_height = node_bounds(component, node.id)
            self.group_bounds.setText(f"{bounds_width:.3f} × {bounds_height:.3f}")
        if sized:
            scale_x = math.hypot(node.transform.m11, node.transform.m21)
            scale_y = math.hypot(node.transform.m12, node.transform.m22)
            if isinstance(node, GroupNode):
                self.width_spin.setValue(node.frame.width_mm * scale_x)
                self.height_spin.setValue(node.frame.height_mm * scale_y)
            else:
                self.width_spin.setValue(node.width_mm * scale_x)
                self.height_spin.setValue(node.height_mm * scale_y)
        image = isinstance(node, ImageNode)
        self.optional_image.setChecked(image and node.optional)
        self.form.setRowVisible(self.optional_image, image)
        html = isinstance(node, HtmlNode)
        self.form.setRowVisible(self.name_edit, True)
        self.name_edit.setText(node.name)
        self.form.setRowVisible(self.source_edit, image)
        self.form.setRowVisible(self.fit_combo, image)
        self.form.setRowVisible(self.content_mode, image or html)
        self.form.setRowVisible(self.content_column, image or html)
        if image:
            self.source_edit.setText(node.source)
            self.fit_combo.setCurrentText(node.fit)
            mode, column = node.source_mode, node.source_column
        elif html:
            mode, column = node.content_mode, node.content_column
        else:
            mode, column = "manual", None
        self.content_mode.setCurrentIndex(max(0, self.content_mode.findData(mode)))
        self.content_column.clear()
        self.content_column.addItems(list(columns))
        if column and self.content_column.findText(column) < 0:
            self.content_column.addItem(column)
        if column:
            self.content_column.setCurrentText(column)
        self.content_column.setEnabled(mode == "column")
        if getattr(self, "_node_type", "") == "html":
            self.html_edit.setReadOnly(mode != "manual")
            self.apply_html.setEnabled(mode == "manual")
            self.insert_image.setEnabled(mode == "manual")
        for widget in (self.font_combo, self.font_spin, self.color_widget):
            self.form.setRowVisible(widget, html)
        for button in self.alignment_buttons.values():
            button.setVisible(html)
        self.form.setRowVisible(self.alignment_widget, html)
        for widget in (self.html_edit, self.insert_image, self.apply_html):
            widget.setVisible(html)
        if html:
            self.font_combo.clear()
            self.html_edit.setReadOnly(mode != "manual")
            self.apply_html.setEnabled(mode == "manual")
            self.insert_image.setEnabled(mode == "manual")
            families = list(font_families or sorted(QFontDatabase.families(), key=str.casefold))
            for family in families:
                self.font_combo.addItem(family, family)
            if node.font_family not in families:
                self.font_combo.insertItem(0, tr("ui.font_unavailable", font=node.font_family), node.font_family)
            self.font_combo.setCurrentIndex(max(0, self.font_combo.findData(node.font_family)))
            self.font_combo.setToolTip(tr("ui.font_preserved") if node.font_family not in families else "")
            self.font_spin.setValue(node.font_size_pt)
            self._node_color = node.color.upper()
            self._html_color = self._node_color
            self.color_edit.setText(self._node_color)
            self._update_html_color_swatch(self._node_color)
            self.html_edit.setPlainText(node.html)
            for value, button in self.alignment_buttons.items():
                button.setChecked(node.text_align == value)
        else:
            self.form.setRowVisible(self.group_bounds, False)
        del blockers

    def set_edit_locked(self, locked: bool) -> None:
        for widget in (
            self.name_edit, self.x_spin, self.y_spin, self.width_spin, self.height_spin, self.rotation_spin,
            self.condition_column, self.condition_operator, self.condition_value, self.optional_image, self.stroke_button, self.fill_button, self.no_fill_button,
            self.stroke_width, self.stroke_style, self.line_dx, self.line_dy,
            self.source_edit, self.fit_combo, self.content_mode, self.content_column,
            self.font_combo, self.font_spin, self.color_edit, self.color_swatch,
            self.html_edit, self.insert_image, self.apply_html,
            *self.alignment_buttons.values(),
        ):
            widget.setEnabled(not locked)
        if not locked:
            self.content_column.setEnabled(self.content_mode.currentData() == "column")
            manual = self.content_mode.currentData() == "manual"
            self.html_edit.setReadOnly(not manual)
            self.apply_html.setEnabled(manual)
            self.insert_image.setEnabled(manual)

    def show_image_quality(self, quality) -> None:
        items = tuple(quality)
        if not items:
            self.quality_label.clear()
            self.form.setRowVisible(self.quality_label, False)
            return
        lines = []
        has_warning = False
        for item in items:
            status = tr("ui.image_quality_status") if item.warning else ""
            has_warning = has_warning or item.warning
            lines.append(
                tr("ui.image_quality", source=item.source, width_px=item.pixel_width, height_px=item.pixel_height,
                   width_mm=item.width_mm, height_mm=item.height_mm, dpi_x=item.dpi_x, dpi_y=item.dpi_y, warning=status)
            )
        self.quality_label.setText("\n".join(lines))
        self.quality_label.setStyleSheet("color: #A00000" if has_warning else "")
        self.form.setRowVisible(self.quality_label, True)

    def _content_mode_changed(self, _index: int) -> None:
        mode = self.content_mode.currentData()
        field = "source_mode" if getattr(self, "_node_type", "") == "image" else "content_mode"
        changes = {field: mode}
        if mode == "column" and self.content_column.currentText():
            changes["source_column" if field == "source_mode" else "content_column"] = self.content_column.currentText()
        self.content_column.setEnabled(mode == "column")
        if self._node_type == "html":
            self.html_edit.setReadOnly(mode != "manual")
            self.apply_html.setEnabled(mode == "manual")
            self.insert_image.setEnabled(mode == "manual")
        self.nodeChanged.emit(changes)

    def _content_column_changed(self) -> None:
        field = "source_column" if getattr(self, "_node_type", "") == "image" else "content_column"
        self.nodeChanged.emit({field: self.content_column.currentText()})

    def insert_html_image(self, source: str, width: int, height: int, align: str) -> None:
        cursor = self.html_edit.textCursor()
        alignment = f' align="{align}"' if align else ""
        cursor.insertText(f'<img src="{source}" width="{width}" height="{height}"{alignment} />')
        self.html_edit.setTextCursor(cursor)
