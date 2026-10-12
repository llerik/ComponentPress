"""Component properties shown for an active document."""

from PySide6.QtCore import QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QButtonGroup,
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
        self.background_button = QPushButton("Выбрать цвет…")
        self.background_button.setObjectName("componentBackgroundPicker")
        self.sheet = QComboBox()
        self.sheet.setObjectName("componentPropertySheet")
        self.sheet.addItem("Без привязки к Excel", None)
        self.sheet.addItems(list(sheets))
        background_row = QHBoxLayout()
        background_row.addWidget(self.background_edit, 1)
        background_row.addWidget(self.background_button)
        layout.addRow("ID", self.identity)
        layout.addRow("Название", self.name_edit)
        layout.addRow("Ширина, мм", self.width_spin)
        layout.addRow("Высота, мм", self.height_spin)
        layout.addRow("Фон", background_row)
        layout.addRow("Лист Excel", self.sheet)
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
        self.sheet.addItem("Без привязки к Excel", None)
        for sheet in self._available_sheets:
            self.sheet.addItem(sheet, sheet)
        if current_sheet and self.sheet.findData(current_sheet) < 0:
            self.sheet.addItem(f"{current_sheet} (не найден)", current_sheet)
        self.sheet.setCurrentIndex(max(0, self.sheet.findData(current_sheet)))
        del blockers


class ElementProperties(QWidget):
    """Exact geometry and type-specific values for one selected node."""

    nodeChanged = Signal(dict)
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
        self.source_edit = QLineEdit()
        self.source_edit.setObjectName("imageSource")
        self.fit_combo = QComboBox()
        self.fit_combo.setObjectName("imageFit")
        self.fit_combo.addItems(["contain", "cover", "stretch"])
        self.content_mode = QComboBox()
        self.content_mode.setObjectName("contentMode")
        self.content_mode.addItem("Ручной ввод", "manual")
        self.content_mode.addItem("Столбец", "column")
        self.content_column = QComboBox()
        self.content_column.setObjectName("contentColumn")
        self.condition_column = QComboBox()
        self.condition_column.setObjectName("conditionColumn")
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
        self.stroke_style.addItem("Сплошная", "solid")
        self.stroke_style.addItem("Штриховая", "dash")
        self.stroke_style.addItem("Точечная", "dot")
        self.line_dx = self._number("lineDX", -10000.0, 10000.0)
        self.line_dy = self._number("lineDY", -10000.0, 10000.0)
        self.font_combo = QComboBox()
        self.font_combo.setObjectName("htmlFont")
        self.font_spin = self._number("htmlFontSize", 0.1, 1000.0)
        self.font_spin.setDecimals(1)
        self.font_spin.setSingleStep(0.1)
        self.color_edit = QLineEdit()
        self.color_edit.setObjectName("htmlColor")
        self.color_edit.setReadOnly(True)
        self.color_button = QPushButton("Выбрать цвет…")
        self.color_button.setObjectName("htmlColorPicker")
        self.color_widget = QWidget()
        color_row = QHBoxLayout(self.color_widget)
        color_row.setContentsMargins(0, 0, 0, 0)
        color_row.addWidget(self.color_edit, 1)
        color_row.addWidget(self.color_button)
        self.alignment_group = QButtonGroup(self)
        self.alignment_group.setExclusive(True)
        self.alignment_buttons: dict[str, QPushButton] = {}
        alignment_widget = QWidget()
        alignment_row = QHBoxLayout(alignment_widget)
        alignment_row.setContentsMargins(0, 0, 0, 0)
        for value, label in (("left", "Слева"), ("center", "По центру"), ("right", "Справа")):
            button = QPushButton(label)
            button.setObjectName(f"htmlAlign{value.title()}")
            button.setCheckable(True)
            button.setProperty("alignment", value)
            self.alignment_group.addButton(button)
            self.alignment_buttons[value] = button
            alignment_row.addWidget(button)
        self.lock_button = QPushButton()
        self.lock_button.setObjectName("elementLocked")
        self.lock_button.setCheckable(True)
        self.lock_button.setFixedSize(34, 34)
        self.fill_button = QPushButton("Выбрать цвет…")
        self.fill_button.setObjectName("shapeFillPicker")
        self.stroke_button = QPushButton("Выбрать цвет…")
        self.stroke_button.setObjectName("shapeStrokePicker")
        self.no_fill_button = QPushButton("Без заливки")
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
        form.addRow("Элемент", self.identity)
        form.addRow("Защита", self.lock_button)
        form.addRow("Габариты группы, мм", self.group_bounds)
        form.addRow("Разрешение рисунков", self.quality_label)
        form.addRow("Название", self.name_edit)
        form.addRow("X, мм", self.x_spin)
        form.addRow("Y, мм", self.y_spin)
        form.addRow("Ширина, мм", self.width_spin)
        form.addRow("Высота, мм", self.height_spin)
        form.addRow("Изображение", self.source_edit)
        form.addRow("Источник содержимого", self.content_mode)
        form.addRow("Столбец Excel", self.content_column)
        form.addRow("Столбец условия", self.condition_column)
        form.addRow("Значение условия", self.condition_value)
        form.addRow("Состояние", self.condition_state)
        form.addRow("Заливка", fill_row)
        form.addRow("Контур", self.stroke_button)
        form.addRow("Толщина контура, мм", self.stroke_width)
        form.addRow("Стиль контура", self.stroke_style)
        form.addRow("Конец линии ΔX, мм", self.line_dx)
        form.addRow("Конец линии ΔY, мм", self.line_dy)
        form.addRow("Режим", self.fit_combo)
        form.addRow("Шрифт", self.font_combo)
        form.addRow("Размер, pt", self.font_spin)
        form.addRow("Цвет", self.color_widget)
        self.alignment_widget = alignment_widget
        form.addRow("Выравнивание текста", self.alignment_widget)
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
        for spin in (self.x_spin, self.y_spin, self.width_spin, self.height_spin, self.stroke_width, self.line_dx, self.line_dy):
            spin.editingFinished.connect(self._geometry_changed)
        self.name_edit.editingFinished.connect(lambda: self.nodeChanged.emit({"name": self.name_edit.text()}))
        self.source_edit.editingFinished.connect(lambda: self.nodeChanged.emit({"source": self.source_edit.text()}))
        self.content_mode.currentIndexChanged.connect(self._content_mode_changed)
        self.content_column.activated.connect(lambda _index: self._content_column_changed())
        self.condition_column.activated.connect(lambda _index: self.nodeChanged.emit({"condition_column": self.condition_column.currentText()}))
        self.condition_value.editingFinished.connect(lambda: self.nodeChanged.emit({"condition_value": self.condition_value.text()}))
        self.stroke_color.editingFinished.connect(lambda: self.nodeChanged.emit({"stroke": self.stroke_color.text()}))
        self.fill_color.editingFinished.connect(lambda: self.nodeChanged.emit({"fill": self.fill_color.text() or None}))
        self.stroke_width.editingFinished.connect(lambda: self.nodeChanged.emit({"stroke_width_mm": self.stroke_width.value()}))
        self.stroke_style.currentIndexChanged.connect(lambda _index: self.nodeChanged.emit({"stroke_style": self.stroke_style.currentData()}))
        self.fit_combo.currentTextChanged.connect(lambda value: self.nodeChanged.emit({"fit": value}))
        self.font_combo.currentTextChanged.connect(lambda value: self.nodeChanged.emit({"font_family": value}))
        self.font_spin.editingFinished.connect(lambda: self.nodeChanged.emit({"font_size_pt": self.font_spin.value()}))
        self.color_edit.editingFinished.connect(lambda: self.nodeChanged.emit({"color": self.color_edit.text()}))
        self.color_button.clicked.connect(lambda: self.colorRequested.emit(self.color_edit.text()))
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
        self.lock_button.setAccessibleName("Разблокировать элемент" if locked else "Заблокировать элемент")

    def _geometry_changed(self) -> None:
        changes: dict[str, float] = {"x_mm": self.x_spin.value(), "y_mm": self.y_spin.value()}
        if self.width_spin.isVisible():
            changes.update(width_mm=self.width_spin.value(), height_mm=self.height_spin.value())
        if self.line_dx.isVisible():
            changes.update(dx_mm=self.line_dx.value(), dy_mm=self.line_dy.value())
        if self.stroke_width.isVisible():
            changes["stroke_width_mm"] = self.stroke_width.value()
        self.nodeChanged.emit(changes)

    def show_node(self, node: Node | None, columns: tuple[str, ...] = (), component: ComponentDefinition | None = None, condition_matches: bool | None = None, font_families: tuple[str, ...] = ()) -> None:
        self.setEnabled(node is not None)
        if node is None:
            return
        widgets = (
            self.x_spin, self.y_spin, self.width_spin, self.height_spin, self.name_edit,
            self.source_edit, self.fit_combo, self.font_combo, self.font_spin, self.color_edit,
            self.html_edit, self.content_mode, self.content_column, self.font_combo,
            self.lock_button, self.condition_column, self.condition_value, self.stroke_color,
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
        self.lock_button.setToolTip("Разблокируйте родительскую группу" if inherited_lock else ("Разблокировать элемент" if effectively_locked else "Заблокировать элемент"))
        self.form.setRowVisible(self.lock_button, True)
        self.x_spin.setValue(node.x_mm)
        self.y_spin.setValue(node.y_mm)
        sized = isinstance(node, (ImageNode, HtmlNode))
        self.form.setRowVisible(self.width_spin, sized)
        self.form.setRowVisible(self.height_spin, sized)
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
        self.form.setRowVisible(self.condition_value, conditional)
        self.form.setRowVisible(self.condition_state, conditional)
        if conditional:
            self.condition_column.clear()
            self.condition_column.addItems(list(columns))
            if self.condition_column.findText(node.condition_column) < 0:
                self.condition_column.addItem(node.condition_column)
            self.condition_column.setCurrentText(node.condition_column)
            self.condition_value.setText(node.condition_value)
            self.condition_state.setText("Показывается" if condition_matches is True else "Скрыта" if condition_matches is False else "Нет строки")
        if is_group and component is not None:
            from componentpress.domain.tree import node_bounds

            _, _, bounds_width, bounds_height = node_bounds(component, node.id)
            self.group_bounds.setText(f"{bounds_width:.3f} × {bounds_height:.3f}")
        if sized:
            self.width_spin.setValue(node.width_mm)
            self.height_spin.setValue(node.height_mm)
        image = isinstance(node, ImageNode)
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
            self.font_combo.addItems(list(font_families or sorted(QFontDatabase.families(), key=str.casefold)))
            if self.font_combo.findText(node.font_family, Qt.MatchFlag.MatchFixedString) < 0:
                self.font_combo.insertItem(0, f"{node.font_family} (недоступен)", node.font_family)
            self.font_combo.setCurrentText(node.font_family if self.font_combo.findText(node.font_family) >= 0 else f"{node.font_family} (недоступен)")
            self.font_combo.setToolTip("Семейство будет сохранено без автоматической замены" if self.font_combo.currentIndex() == 0 and "(недоступен)" in self.font_combo.currentText() else "")
            self.font_spin.setValue(node.font_size_pt)
            self.color_edit.setText(node.color)
            self.html_edit.setPlainText(node.html)
            for value, button in self.alignment_buttons.items():
                button.setChecked(node.text_align == value)
        else:
            self.form.setRowVisible(self.group_bounds, False)
        del blockers

    def set_edit_locked(self, locked: bool) -> None:
        for widget in (
            self.name_edit, self.x_spin, self.y_spin, self.width_spin, self.height_spin,
            self.condition_column, self.condition_value, self.stroke_button, self.fill_button, self.no_fill_button,
            self.stroke_width, self.stroke_style, self.line_dx, self.line_dy,
            self.source_edit, self.fit_combo, self.content_mode, self.content_column,
            self.font_combo, self.font_spin, self.color_edit, self.color_button,
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
            status = " — ниже 300 dpi" if item.warning else ""
            has_warning = has_warning or item.warning
            lines.append(
                f"{item.source}: {item.pixel_width}×{item.pixel_height} px → "
                f"{item.width_mm:.2f}×{item.height_mm:.2f} мм; "
                f"{item.dpi_x:.1f}×{item.dpi_y:.1f} dpi{status}"
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
