"""Component properties shown for an active document."""

import math
import re

from PySide6.QtCore import QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from componentpress.i18n import tr
from componentpress.app_assets import set_themed_icon
from componentpress.domain.component import ComponentDefinition
from componentpress.domain.nodes import ConditionalGroupNode, GroupNode, HtmlNode, ImageNode, LineNode, Node, ShapeNode
from componentpress.editor.numeric import NumericSpinBox

_POSITION_ROWS = (
    (("north_west", "left", "top"), ("north", "center", "top"), ("north_east", "right", "top")),
    (("west", "left", "center"), ("center", "center", "center"), ("east", "right", "center")),
    (("south_west", "left", "bottom"), ("south", "center", "bottom"), ("south_east", "right", "bottom")),
)


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
        self._create_component_identity_fields()
        self._create_component_dimension_fields()
        self._create_component_background_fields()
        self._create_component_sheet_field(sheets)
        self._add_component_rows(layout)
        self._connect_component_fields()
        self.setEnabled(False)

    def _create_component_identity_fields(self) -> None:
        self.identity = QLabel()
        self.identity.setObjectName("componentIdentity")
        self.name_edit = QLineEdit()
        self.name_edit.setObjectName("componentName")

    def _create_component_dimension_fields(self) -> None:
        self.width_spin = self._size_spin("componentWidth")
        self.height_spin = self._size_spin("componentHeight")

    def _create_component_background_fields(self) -> None:
        self.background_edit = QLineEdit()
        self.background_edit.setObjectName("componentBackground")
        self.background_edit.setPlaceholderText("#FFFFFF")
        self.background_edit.setMaxLength(7)
        self.background_edit.setFixedWidth(self.background_edit.fontMetrics().horizontalAdvance("#FFFFFF") + 18)
        self.background_button = QPushButton()
        self.background_button.setObjectName("componentBackgroundPicker")
        self.background_button.setFixedSize(28, 28)
        self.background_button.setAccessibleName(tr("ui.select_background_color"))
        self.background_button.setToolTip(self.background_button.accessibleName())

    def _create_component_sheet_field(self, sheets: tuple[str, ...]) -> None:
        self.sheet = QComboBox()
        self.sheet.setObjectName("componentPropertySheet")
        self.sheet.addItem(tr("ui.no_sheet"), None)
        self.sheet.addItems(list(sheets))

    def _add_component_rows(self, layout: QFormLayout) -> None:
        background_row = QHBoxLayout()
        background_row.addWidget(self.background_edit, 1)
        background_row.addWidget(self.background_button)
        layout.addRow(tr("ui.component_id"), self.identity)
        layout.addRow(tr("ui.name"), self.name_edit)
        layout.addRow(tr("ui.width_mm"), self.width_spin)
        layout.addRow(tr("ui.height_mm"), self.height_spin)
        layout.addRow(tr("ui.component_background"), background_row)
        layout.addRow(tr("ui.component_sheet"), self.sheet)

    def _connect_component_fields(self) -> None:
        self.name_edit.editingFinished.connect(lambda: self.nameChanged.emit(self.name_edit.text()))
        self.background_edit.editingFinished.connect(self._commit_background)
        self.width_spin.editingFinished.connect(self._emit_size)
        self.height_spin.editingFinished.connect(self._emit_size)
        self.background_button.clicked.connect(self.backgroundColorRequested)
        self.sheet.currentIndexChanged.connect(lambda _index: self.sheetChanged.emit(self.sheet.currentData()))

    def set_sheets(self, sheets: tuple[str, ...]) -> None:
        self._available_sheets = sheets

    @staticmethod
    def _size_spin(name: str) -> NumericSpinBox:
        spin = NumericSpinBox()
        spin.setObjectName(name)
        spin.setRange(0.001, 10000.0)
        spin.setDecimals(3)
        spin.setSingleStep(1.0)
        return spin

    def _emit_size(self) -> None:
        self.sizeChanged.emit(self.width_spin.value(), self.height_spin.value())

    def _commit_background(self) -> None:
        value = self.background_edit.text().upper()
        if not re.fullmatch(r"#[0-9A-F]{6}", value):
            value = getattr(self, "_background", "#FFFFFF")
        self._background = value
        self.background_edit.setText(value)
        ElementProperties.update_color_swatch(self.background_button, value)
        if value != getattr(self, "_background_model", value):
            self.backgroundChanged.emit(value)

    def show_component(self, component: ComponentDefinition | None) -> None:
        self.setEnabled(component is not None)
        if component is None:
            return
        blockers = self._block_component_signals()
        self.identity.setText(component.id)
        self.name_edit.setText(component.name)
        self.width_spin.setValue(component.size_mm.width)
        self.height_spin.setValue(component.size_mm.height)
        self.background_edit.setText(component.background)
        self._background = self._background_model = component.background.upper()
        ElementProperties.update_color_swatch(self.background_button, self._background)
        self._show_component_sheet(component)
        del blockers

    def _block_component_signals(self) -> list[QSignalBlocker]:
        return [QSignalBlocker(widget) for widget in (
            self.name_edit, self.width_spin, self.height_spin, self.background_edit, self.sheet,
        )]

    def _show_component_sheet(self, component: ComponentDefinition) -> None:
        current_sheet = component.data.sheet if component.data else None
        self.sheet.clear()
        self.sheet.addItem(tr("ui.no_sheet"), None)
        for sheet in self._available_sheets:
            self.sheet.addItem(sheet, sheet)
        if current_sheet and self.sheet.findData(current_sheet) < 0:
            self.sheet.addItem(tr("ui.sheet_missing", sheet=current_sheet), current_sheet)
        self.sheet.setCurrentIndex(max(0, self.sheet.findData(current_sheet)))


class ElementProperties(QWidget):
    """Exact geometry and type-specific values for one selected node."""

    nodeChanged = Signal(dict)
    rotationRequested = Signal(float)
    positionRequested = Signal(object)
    insertImageRequested = Signal()
    selectImageRequested = Signal()
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
        self.source_edit.setReadOnly(True)
        self.source_button = QPushButton(tr("ui.choose"))
        self.source_button.setObjectName("chooseImageSource")
        self.source_button.clicked.connect(self.selectImageRequested)
        source_row = QWidget()
        self.source_row = source_row
        source_layout = QHBoxLayout(source_row)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.addWidget(self.source_edit, 1)
        source_layout.addWidget(self.source_button)
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
        self.stroke_color = self._color_edit("shapeStroke")
        self.fill_color = self._color_edit("shapeFill")
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
        self.font_spin.setSingleStep(1.0)
        self.color_edit = self._color_edit("htmlColor")
        self.color_swatch = self._color_swatch("htmlColorPicker", tr("ui.select_text_color"))
        self.color_widget = QWidget()
        color_row = QHBoxLayout(self.color_widget)
        color_row.setContentsMargins(0, 0, 0, 0)
        color_row.setSpacing(6)
        color_row.addWidget(self.color_swatch)
        color_row.addWidget(self.color_edit)
        self.alignment_group = QButtonGroup(self)
        self.alignment_buttons: dict[str, QPushButton] = {}
        alignment_widget = QWidget()
        alignment_row = QHBoxLayout(alignment_widget)
        alignment_row.setContentsMargins(0, 0, 0, 0)
        for value, label, icon in (("left", tr("ui.text_align_left"), "format_align_left.png"), ("center", tr("ui.text_align_center"), "format_align_center.png"), ("right", tr("ui.text_align_right"), "format_align_right.png")):
            button = QPushButton()
            button.setObjectName(f"htmlAlign{value.title()}")
            button.setCheckable(True)
            button.setProperty("alignment", value)
            set_themed_icon(button, icon)
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
        self.fill_button = self._color_swatch("shapeFillPicker", tr("ui.select_fill_color"))
        self.stroke_button = self._color_swatch("shapeStrokePicker", tr("ui.select_stroke_color"))
        self.no_fill_button = QPushButton(tr("ui.no_fill"))
        self.no_fill_button.setObjectName("shapeNoFill")
        fill_row = QWidget()
        self.fill_widget = fill_row
        fill_layout = QHBoxLayout(fill_row)
        fill_layout.setContentsMargins(0, 0, 0, 0)
        fill_layout.addWidget(self.fill_button)
        fill_layout.addWidget(self.fill_color)
        fill_layout.addWidget(self.no_fill_button)
        self.stroke_widget = QWidget()
        stroke_layout = QHBoxLayout(self.stroke_widget)
        stroke_layout.setContentsMargins(0, 0, 0, 0)
        stroke_layout.addWidget(self.stroke_button)
        stroke_layout.addWidget(self.stroke_color)
        self.group_bounds = QLabel()
        self.group_bounds.setObjectName("groupBounds")
        self.quality_label = QLabel()
        self.quality_label.setObjectName("imageQuality")
        self.quality_label.setWordWrap(True)
        form.addRow(tr("ui.name"), self.name_edit)
        form.addRow(tr("ui.x_mm"), self.x_spin)
        form.addRow(tr("ui.y_mm"), self.y_spin)
        form.addRow(tr("ui.width_mm"), self.width_spin)
        form.addRow(tr("ui.height_mm"), self.height_spin)
        form.addRow(tr("ui.rotation"), self.rotation_spin)
        form.addRow(tr("ui.element"), self.identity)
        form.addRow(tr("ui.lock"), self.lock_button)
        form.addRow(tr("ui.group_bounds"), self.group_bounds)
        form.addRow(tr("ui.image_resolution"), self.quality_label)
        form.addRow(tr("ui.optional_image"), self.optional_image)
        form.addRow(tr("ui.image"), source_row)
        form.addRow(tr("ui.content_source"), self.content_mode)
        form.addRow(tr("ui.excel_column"), self.content_column)
        form.addRow(tr("ui.condition_column"), self.condition_column)
        form.addRow(tr("ui.condition_operator"), self.condition_operator)
        form.addRow(tr("ui.condition_value"), self.condition_value)
        form.addRow(tr("ui.condition_state"), self.condition_state)
        form.addRow(tr("ui.fill"), fill_row)
        form.addRow(tr("ui.stroke"), self.stroke_widget)
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
        self._create_position_controls(form)
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
        self.stroke_color.editingFinished.connect(lambda: self._commit_node_color(self.stroke_color, "stroke"))
        self.fill_color.editingFinished.connect(lambda: self._commit_node_color(self.fill_color, "fill"))
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

    def _create_position_controls(self, form: QFormLayout) -> None:
        position = QWidget()
        layout = QVBoxLayout(position)
        layout.setContentsMargins(0, 0, 0, 0)
        self.position_buttons = {}
        self._add_gravity_grid(layout)
        self._add_axis_centering(layout)
        form.addRow(tr("ui.element_position"), position)

    def _add_gravity_grid(self, layout):
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)
        for row, values in enumerate(_POSITION_ROWS):
            for column, (key, horizontal, vertical) in enumerate(values):
                value = (horizontal, vertical)
                button = self._position_button(f"gravity_{key}", f"gravity_{key}.png", value)
                self.position_buttons[key] = button
                grid.addWidget(button, row, column)
        layout.addLayout(grid)

    def _add_axis_centering(self, layout):
        axes = QHBoxLayout()
        axes.setContentsMargins(0, 0, 0, 0)
        self.center_horizontal_button = self._position_button("horizontal_center", "align_horizontal_center.png", ("center", None))
        self.center_vertical_button = self._position_button("vertical_center", "align_vertical_center.png", (None, "center"))
        axes.addWidget(self.center_horizontal_button)
        axes.addWidget(self.center_vertical_button)
        layout.addLayout(axes)

    def _position_button(self, name: str, icon: str, position: tuple[str | None, str | None]) -> QPushButton:
        button = QPushButton()
        prefix = "gravity" if name.startswith("gravity_") else "position"
        button.setObjectName(f"{prefix}{name.removeprefix('gravity_').title().replace('_', '')}")
        button.setFixedSize(32, 32)
        button.setIconSize(QSize(20, 20))
        label = tr(f"ui.{name}")
        button.setToolTip(label)
        button.setAccessibleName(label)
        set_themed_icon(button, icon)
        button.clicked.connect(lambda _checked=False: self.positionRequested.emit(position))
        return button

    @staticmethod
    def _number(name: str, minimum: float, maximum: float) -> NumericSpinBox:
        spin = NumericSpinBox()
        spin.setObjectName(name)
        spin.setRange(minimum, maximum)
        spin.setDecimals(3)
        spin.setSingleStep(1.0)
        return spin

    @staticmethod
    def _color_edit(name: str) -> QLineEdit:
        edit = QLineEdit()
        edit.setObjectName(name)
        edit.setMaxLength(9)
        edit.setFixedWidth(edit.fontMetrics().horizontalAdvance("#AARRGGBB") + 18)
        return edit

    @staticmethod
    def _color_swatch(name: str, label: str) -> QPushButton:
        button = QPushButton()
        button.setObjectName(name)
        button.setFixedSize(28, 28)
        button.setAccessibleName(label)
        button.setToolTip(label)
        return button

    def _set_lock_icon(self, locked: bool) -> None:
        set_themed_icon(self.lock_button, "lock.png" if locked else "lock_open.png")
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

    @staticmethod
    def update_color_swatch(button: QPushButton, value: str | None) -> None:
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        for y in range(0, 24, 6):
            for x in range(0, 24, 6):
                color = Qt.GlobalColor.white if (x // 6 + y // 6) % 2 else Qt.GlobalColor.lightGray
                painter.fillRect(x, y, 6, 6, color)
        if value:
            painter.fillRect(0, 0, 24, 24, QColor(value))
        painter.end()
        button.setIcon(QIcon(pixmap))
        button.setIconSize(QSize(24, 24))

    def _update_html_color_swatch(self, value: str) -> None:
        self.update_color_swatch(self.color_swatch, value)

    def _commit_node_color(self, edit: QLineEdit, field: str) -> None:
        value = edit.text().upper()
        if not re.fullmatch(r"#[0-9A-F]{6}(?:[0-9A-F]{2})?", value):
            value = getattr(self, f"_{field}_color", "#000000")
        setattr(self, f"_{field}_color", value)
        edit.setText(value)
        self.update_color_swatch(self.fill_button if field == "fill" else self.stroke_button, value)
        if value != getattr(self, f"_{field}_node_color", value):
            setattr(self, f"_{field}_node_color", value)
            self.nodeChanged.emit({field: value})

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
        blockers = [QSignalBlocker(widget) for widget in self._node_widgets()]
        self.identity.setText(f"{node.id} ({node.type})")
        self._node_type = node.type
        self._show_node_lock(node, component)
        self._show_node_geometry(node)
        self._show_node_shape(node)
        self._show_node_condition(node, columns, condition_matches, component)
        self._show_node_content(node, columns)
        self._show_node_html(node, font_families)
        del blockers
        self._sync_content_mode_visibility()

    def _node_widgets(self) -> tuple[QWidget, ...]:
        return (
            self.x_spin, self.y_spin, self.width_spin, self.height_spin, self.rotation_spin, self.name_edit,
            self.source_edit, self.fit_combo, self.font_combo, self.font_spin, self.color_edit, self.html_edit,
            self.content_mode, self.content_column, self.lock_button, self.condition_column,
            self.condition_operator, self.condition_value, self.optional_image, self.stroke_color,
            self.fill_color, self.stroke_width, self.stroke_style, self.line_dx, self.line_dy,
            *self.alignment_buttons.values(),
        )

    def _show_node_lock(self, node: Node, component: ComponentDefinition | None) -> None:
        inherited_lock = self._has_locked_parent(node, component)
        effectively_locked = node.locked or inherited_lock
        self.lock_button.setChecked(effectively_locked)
        self._set_lock_icon(effectively_locked)
        self.lock_button.setEnabled(not inherited_lock)
        label = "ui.unlock_parent" if inherited_lock else "ui.unlock_element" if effectively_locked else "ui.lock_element"
        self.lock_button.setToolTip(tr(label))
        self.form.setRowVisible(self.lock_button, True)

    @staticmethod
    def _has_locked_parent(node: Node, component: ComponentDefinition | None) -> bool:
        if component is None:
            return False
        from componentpress.domain.tree import locations

        index = locations(component)
        parent_id = index[node.id].parent_id
        while parent_id is not None:
            if index[parent_id].node.locked:
                return True
            parent_id = index[parent_id].parent_id
        return False

    def _show_node_geometry(self, node: Node) -> None:
        self.x_spin.setValue(node.x_mm)
        self.y_spin.setValue(node.y_mm)
        sized = isinstance(node, (ImageNode, HtmlNode, GroupNode))
        self.form.setRowVisible(self.width_spin, sized)
        self.form.setRowVisible(self.height_spin, sized)
        self.rotation_spin.setValue(math.degrees(math.atan2(node.transform.m21, node.transform.m11)) % 360)
        self._rotation_initial_value = self.rotation_spin.value()
        self._rotation_text_edited = False
        self.form.setRowVisible(self.rotation_spin, True)
        if sized:
            scale_x = math.hypot(node.transform.m11, node.transform.m21)
            scale_y = math.hypot(node.transform.m12, node.transform.m22)
            width, height = (node.frame.width_mm, node.frame.height_mm) if isinstance(node, GroupNode) else (node.width_mm, node.height_mm)
            self.width_spin.setValue(width * scale_x)
            self.height_spin.setValue(height * scale_y)

    def _show_node_shape(self, node: Node) -> None:
        line, shape = isinstance(node, LineNode), isinstance(node, ShapeNode)
        self._show_shape_visibility(line, shape)
        self._show_shape_values(node, line, shape)

    def _show_shape_visibility(self, line: bool, shape: bool) -> None:
        self.form.setRowVisible(self.line_dx, line)
        self.form.setRowVisible(self.line_dy, line)
        self.form.setRowVisible(self.fill_widget, shape)
        self.form.setRowVisible(self.stroke_widget, shape or line)
        self.form.setRowVisible(self.stroke_width, shape or line)
        self.form.setRowVisible(self.stroke_style, shape or line)

    def _show_shape_values(self, node: Node, line: bool, shape: bool) -> None:
        if line:
            self.line_dx.setValue(node.dx_mm)
            self.line_dy.setValue(node.dy_mm)
        if shape or line:
            self.stroke_color.setText(node.stroke.upper())
            self._stroke_color = self._stroke_node_color = node.stroke.upper()
            self.update_color_swatch(self.stroke_button, node.stroke)
            self.stroke_width.setValue(node.stroke_width_mm)
            self.stroke_style.setCurrentIndex(max(0, self.stroke_style.findData(node.stroke_style)))
        if shape:
            self.fill_color.setText(node.fill or "")
            self._fill_color = self._fill_node_color = (node.fill or "").upper()
            self.update_color_swatch(self.fill_button, node.fill)

    def _show_node_condition(self, node: Node, columns: tuple[str, ...], condition_matches: bool | None, component: ComponentDefinition | None) -> None:
        is_group = isinstance(node, GroupNode)
        self.form.setRowVisible(self.group_bounds, is_group)
        conditional = isinstance(node, ConditionalGroupNode)
        self._show_condition_fields(node, columns, condition_matches, conditional)
        self._show_group_bounds(node, component, is_group)

    def _show_condition_fields(self, node: Node, columns: tuple[str, ...], condition_matches: bool | None, conditional: bool) -> None:
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

    def _show_group_bounds(self, node: Node, component: ComponentDefinition | None, is_group: bool) -> None:
        if is_group and component is not None:
            from componentpress.domain.tree import node_bounds

            _, _, bounds_width, bounds_height = node_bounds(component, node.id)
            self.group_bounds.setText(f"{bounds_width:.3f} × {bounds_height:.3f}")

    def _show_node_content(self, node: Node, columns: tuple[str, ...]) -> None:
        image = isinstance(node, ImageNode)
        self.optional_image.setChecked(image and node.optional)
        self.form.setRowVisible(self.optional_image, image)
        html = isinstance(node, HtmlNode)
        self._show_content_source(node, columns, image, html)

    def _show_content_source(self, node: Node, columns: tuple[str, ...], image: bool, html: bool) -> None:
        self.form.setRowVisible(self.name_edit, True)
        self.name_edit.setText(node.name)
        self.form.setRowVisible(self.fit_combo, image)
        self.form.setRowVisible(self.content_mode, image or html)
        mode, column = self._content_source(node, image, html)
        self.form.setRowVisible(self.content_column, (image or html) and mode == "column")
        self.content_mode.setCurrentIndex(max(0, self.content_mode.findData(mode)))
        self._populate_content_columns(columns, column)
        self.content_column.setEnabled(mode == "column")
        self._content_mode = mode
        if getattr(self, "_node_type", "") == "html":
            self.html_edit.setReadOnly(mode != "manual")
            self.apply_html.setEnabled(mode == "manual")
            self.insert_image.setEnabled(mode == "manual")

    def _content_source(self, node: Node, image: bool, html: bool) -> tuple[str, str | None]:
        if image:
            self.source_edit.setText(node.source)
            self.fit_combo.setCurrentText(node.fit)
            return node.source_mode, node.source_column
        return (node.content_mode, node.content_column) if html else ("manual", None)

    def _populate_content_columns(self, columns: tuple[str, ...], column: str | None) -> None:
        self.content_column.clear()
        self.content_column.addItems(list(columns))
        if column and self.content_column.findText(column) < 0:
            self.content_column.addItem(column)
        if column:
            self.content_column.setCurrentText(column)

    def _show_node_html(self, node: Node, font_families: tuple[str, ...]) -> None:
        html = isinstance(node, HtmlNode)
        mode = getattr(self, "_content_mode", "manual")
        self._show_html_visibility(html, mode)
        if html:
            self._show_html_values(node, font_families)
        else:
            self.form.setRowVisible(self.group_bounds, False)

    def _show_html_visibility(self, html: bool, mode: str) -> None:
        for widget in (self.font_combo, self.font_spin, self.color_widget):
            self.form.setRowVisible(widget, html)
        for button in self.alignment_buttons.values():
            button.setVisible(html)
        self.form.setRowVisible(self.alignment_widget, html)
        for widget in (self.html_edit, self.insert_image, self.apply_html):
            widget.setVisible(html and mode == "manual")

    def _show_html_values(self, node: HtmlNode, font_families: tuple[str, ...]) -> None:
        self.font_combo.clear()
        families = list(font_families or sorted(QFontDatabase.families(), key=str.casefold))
        for family in families:
            self.font_combo.addItem(family, family)
            self.font_combo.setItemData(self.font_combo.count() - 1, QFont(family), Qt.ItemDataRole.FontRole)
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

    def set_edit_locked(self, locked: bool) -> None:
        for widget in (
            self.name_edit, self.x_spin, self.y_spin, self.width_spin, self.height_spin, self.rotation_spin,
            self.condition_column, self.condition_operator, self.condition_value, self.optional_image, self.stroke_button, self.fill_button, self.stroke_color, self.fill_color, self.no_fill_button,
            self.stroke_width, self.stroke_style, self.line_dx, self.line_dy,
            self.source_edit, self.source_button, self.fit_combo, self.content_mode, self.content_column,
            self.font_combo, self.font_spin, self.color_edit, self.color_swatch,
            self.html_edit, self.insert_image, self.apply_html,
            *self.alignment_buttons.values(),
            *self.position_buttons.values(), self.center_horizontal_button, self.center_vertical_button,
        ):
            widget.setEnabled(not locked)
        if not locked:
            self.content_column.setEnabled(self.content_mode.currentData() == "column")
            manual = self.content_mode.currentData() == "manual"
            self.html_edit.setReadOnly(not manual)
            self.apply_html.setEnabled(manual)
            self.insert_image.setEnabled(manual)
        self._sync_content_mode_visibility()

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
        self.quality_label.setProperty("componentpressWarning", has_warning)
        from .theme import style_warning

        style_warning(self.quality_label, has_warning)
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
        self._sync_content_mode_visibility()
        self.nodeChanged.emit(changes)

    def _sync_content_mode_visibility(self) -> None:
        node_type = getattr(self, "_node_type", "")
        html = node_type == "html"
        image = node_type == "image"
        manual = self.content_mode.currentData() == "manual"
        self.form.setRowVisible(self.content_column, (html or image) and not manual)
        self.form.setRowVisible(self.source_row, image and manual)
        for widget in (self.html_edit, self.insert_image, self.apply_html):
            widget.setVisible(html and manual)

    def _content_column_changed(self) -> None:
        field = "source_column" if getattr(self, "_node_type", "") == "image" else "content_column"
        self.nodeChanged.emit({field: self.content_column.currentText()})

    def insert_html_image(self, source: str, width: int, height: int, align: str) -> None:
        cursor = self.html_edit.textCursor()
        alignment = f' align="{align}"' if align else ""
        cursor.insertText(f'<img src="{source}" width="{width}" height="{height}"{alignment} />')
        self.html_edit.setTextCursor(cursor)
