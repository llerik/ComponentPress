"""One component tab with synchronized layout and YAML views."""

from PySide6.QtCore import QRegularExpression, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QUndoStack
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QStyle,
)

from componentpress.application.sessions import ProjectSession
from componentpress.domain.diagnostics import ProjectError
from componentpress.rendering import render_component_image
from componentpress.rendering.geometry import MM_PER_INCH
from componentpress.domain.component import ComponentDefinition

from .canvas import ComponentCanvas


class YamlHighlighter(QSyntaxHighlighter):
    """Small YAML-oriented highlighter; validation remains the codec's job."""

    def __init__(self, document) -> None:
        super().__init__(document)
        key = QTextCharFormat()
        key.setForeground(QColor("#235A97"))
        key.setFontWeight(QFont.Weight.Bold)
        comment = QTextCharFormat()
        comment.setForeground(QColor("#6A737D"))
        string = QTextCharFormat()
        string.setForeground(QColor("#8A3B12"))
        self._rules = (
            (QRegularExpression(r"^\s*[A-Za-z_][A-Za-z0-9_-]*(?=\s*:)") , key),
            (QRegularExpression(r"#.*$"), comment),
            (QRegularExpression(r"(?:\"[^\"]*\"|'[^']*')"), string),
        )

    def highlightBlock(self, text: str) -> None:
        for expression, style in self._rules:
            match = expression.globalMatch(text)
            while match.hasNext():
                found = match.next()
                self.setFormat(found.capturedStart(), found.capturedLength(), style)


class DocumentTab(QWidget):
    zoomChanged = Signal(int)
    previewError = Signal(str)
    modeRequested = Signal(str)
    draftChanged = Signal(str)
    previewRequested = Signal()

    def __init__(self, component_id: str, parent=None):
        super().__init__(parent)
        self.component_id = component_id
        self.preview_row_number: int | None = None
        self.resolved_component: ComponentDefinition | None = None
        self.preview_generation = 0
        self.preview_deferred = False
        self.displayed_generation = -1
        self.image_quality = ()
        self.quality_warning_fingerprint = ()
        self._preview_arguments = None
        self._applying_image = False
        self.undo_stack = QUndoStack(self)
        self.setObjectName(f"documentTab-{component_id}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        self.layout_mode = QPushButton("Макет")
        self.layout_mode.setObjectName("layoutMode")
        self.text_mode = QPushButton("Текст")
        self.text_mode.setObjectName("textMode")
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for button in (self.layout_mode, self.text_mode):
            button.setCheckable(True)
            self.mode_group.addButton(button)
            button.setToolTip("Перетаскивание" if button is self.layout_mode else "Текст")
            button.setAccessibleName(button.toolTip())
            button.setText("")
            button.setIcon(self.style().standardIcon(
                QStyle.StandardPixmap.SP_FileDialogDetailedView if button is self.layout_mode
                else QStyle.StandardPixmap.SP_FileDialogContentsView
            ))
            controls.addWidget(button)
        self.layout_mode.setChecked(True)
        controls.addStretch()
        minus = QPushButton("−")
        minus.setObjectName("zoomOut")
        plus = QPushButton("+")
        plus.setObjectName("zoomIn")
        fit = QPushButton("По размеру")
        fit.setObjectName("zoomFit")
        actual = QPushButton("100%")
        actual.setObjectName("zoomActual")
        self.zoom_label = QLabel("100%")
        self.zoom_label.setObjectName("zoomLabel")
        for widget in (minus, plus, fit, actual, self.zoom_label):
            controls.addWidget(widget)
        self.zoom_controls = (minus, plus, fit, actual, self.zoom_label)
        layout.addLayout(controls)
        self.canvas = ComponentCanvas()
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(150)
        self.preview_timer.timeout.connect(self.previewRequested.emit)
        self.text_editor = QPlainTextEdit()
        self.text_editor.setObjectName("yamlEditor")
        self.text_editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        fixed = QFont("Consolas")
        fixed.setStyleHint(QFont.StyleHint.Monospace)
        self.text_editor.setFont(fixed)
        self.highlighter = YamlHighlighter(self.text_editor.document())
        self.diagnostic = QLabel()
        self.diagnostic.setObjectName("yamlDiagnostic")
        self.diagnostic.setWordWrap(True)
        self.diagnostic.setStyleSheet("color: #A00000; padding: 4px")
        text_page = QWidget()
        text_layout = QVBoxLayout(text_page)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.addWidget(self.text_editor, 1)
        text_layout.addWidget(self.diagnostic)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.canvas)
        self.pages.addWidget(text_page)
        layout.addWidget(self.pages, 1)
        minus.clicked.connect(lambda: self.canvas.zoom_by(1 / 1.2))
        plus.clicked.connect(lambda: self.canvas.zoom_by(1.2))
        fit.clicked.connect(self.canvas.fit_page)
        actual.clicked.connect(self.canvas.actual_size)
        self.canvas.zoomChanged.connect(self._zoom_changed)
        self.layout_mode.clicked.connect(lambda: self.modeRequested.emit("layout"))
        self.text_mode.clicked.connect(lambda: self.modeRequested.emit("text"))
        self.text_editor.textChanged.connect(lambda: self.draftChanged.emit(self.text_editor.toPlainText()))

    @property
    def mode(self) -> str:
        return "text" if self.pages.currentIndex() == 1 else "layout"

    def set_mode(self, mode: str) -> None:
        text = mode == "text"
        self.pages.setCurrentIndex(1 if text else 0)
        self.text_mode.setChecked(text)
        self.layout_mode.setChecked(not text)
        for widget in self.zoom_controls:
            widget.setVisible(not text)

    def set_text(self, text: str) -> None:
        if self.text_editor.toPlainText() == text:
            return
        blocked = self.text_editor.blockSignals(True)
        self.text_editor.setPlainText(text)
        self.text_editor.blockSignals(blocked)

    def show_diagnostic(self, message: str = "") -> None:
        self.diagnostic.setText(message)
        self.diagnostic.setVisible(bool(message))

    def _zoom_changed(self, percent: int) -> None:
        self.zoom_label.setText(f"{percent}%")
        self.zoomChanged.emit(percent)
        if not self._applying_image and self._preview_arguments is not None:
            snapshot, component, _dpi, fit, selected, bindings_resolved = self._preview_arguments
            dpi = self._preview_dpi(component, self.canvas.zoom_percent / 100, self.canvas.viewport().devicePixelRatioF())
            self.preview_generation += 1
            self._preview_arguments = (snapshot, component, dpi, False, selected, bindings_resolved)
            self.preview_timer.start()

    @staticmethod
    def _preview_dpi(component: ComponentDefinition, zoom: float, device_ratio: float) -> int:
        requested = max(1, round(96 * zoom * device_ratio))
        by_edge = min(4096 * MM_PER_INCH / component.size_mm.width, 4096 * MM_PER_INCH / component.size_mm.height)
        by_area = (8_000_000 * MM_PER_INCH * MM_PER_INCH / (component.size_mm.width * component.size_mm.height)) ** 0.5
        return max(1, min(requested, int(by_edge), int(by_area)))

    def preview_request_data(self):
        if self._preview_arguments is None:
            return None
        snapshot, component, dpi, _fit, _selected, bindings_resolved = self._preview_arguments
        return snapshot, component, dpi, bindings_resolved, self.preview_generation

    def queue_preview(self, session: ProjectSession, *, component: ComponentDefinition | None = None, dpi: int = 96, fit: bool = False, selected: tuple[str, ...] | None = None, bindings_resolved: bool | None = None) -> int:
        document = session.documents[self.component_id]
        active_component = component or document.model
        if selected is None:
            selected = self.canvas.selected_ids
        self.canvas.set_component_model(active_component, selected)
        self.preview_generation += 1
        self._preview_arguments = (
            session.snapshot, active_component, dpi, fit, selected,
            component is not None if bindings_resolved is None else bindings_resolved,
        )
        self.preview_timer.start()
        return self.preview_generation

    def render_queued_preview(self, *, register_fonts: bool = False):
        if self._preview_arguments is None:
            return None
        snapshot, component, dpi, _fit, _selected, bindings_resolved = self._preview_arguments
        return render_component_image(
            snapshot,
            self.component_id,
            component=component,
            dpi=dpi,
            bindings_resolved=bindings_resolved,
            register_fonts=register_fonts,
        )

    def apply_preview(self, image, quality=(), *, generation: int, fit: bool = False) -> bool:
        if self._preview_arguments is None or generation != self.preview_generation:
            return False
        _snapshot, active_component, _dpi, requested_fit, selected, _bindings_resolved = self._preview_arguments
        self._applying_image = True
        try:
            self.canvas.set_document(image, active_component, fit=fit or requested_fit, selected=selected)
        finally:
            self._applying_image = False
        self.displayed_generation = generation
        self.image_quality = tuple(quality)
        return True

    def fail_preview(self, message: str, *, generation: int) -> bool:
        if self._preview_arguments is None or generation != self.preview_generation:
            return False
        _snapshot, active_component, _dpi, _fit, selected, _bindings_resolved = self._preview_arguments
        self.canvas.set_component_model(active_component, selected)
        self.previewError.emit(message)
        return True

    def refresh(self, session: ProjectSession, *, component: ComponentDefinition | None = None, fit: bool = False, selected: tuple[str, ...] | None = None) -> bool:
        self.queue_preview(session, component=component, fit=fit, selected=selected)
        return True
