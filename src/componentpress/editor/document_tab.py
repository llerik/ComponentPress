"""One component tab with synchronized layout and YAML views."""

from dataclasses import dataclass
from uuid import uuid4

from PySide6.QtCore import QEvent, QRegularExpression, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QSyntaxHighlighter, QTextCharFormat, QUndoStack
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from componentpress.i18n import tr
from componentpress.application.sessions import ProjectSession
from componentpress.application.contracts import ProjectSnapshot
from componentpress.domain.diagnostics import ProjectError
from componentpress.rendering import render_component_image
from componentpress.rendering.image_quality import ImageQuality
from componentpress.rendering.geometry import MM_PER_INCH
from componentpress.domain.component import ComponentDefinition
from componentpress.app_assets import set_themed_icon

from .canvas import ComponentCanvas
from componentpress.preview_coordinator import PreviewIdentity, PreviewRequest


@dataclass(frozen=True)
class PreviewArguments:
    snapshot: ProjectSnapshot
    component: ComponentDefinition
    revision: int
    dpi: int
    fit: bool
    selected_ids: tuple[str, ...]
    bindings_resolved: bool
    row_number: int | None
    source_label: str


@dataclass(frozen=True)
class DeferredPreview:
    image: QImage
    quality: tuple[ImageQuality, ...]
    generation: int
    fit: bool


class YamlHighlighter(QSyntaxHighlighter):
    """Small YAML-oriented highlighter; validation remains the codec's job."""

    def __init__(self, document) -> None:
        super().__init__(document)
        key = QTextCharFormat()
        key.setFontWeight(QFont.Weight.Bold)
        comment = QTextCharFormat()
        string = QTextCharFormat()
        self._formats = (key, comment, string)
        self._rules = (
            (QRegularExpression(r"^\s*[A-Za-z_][A-Za-z0-9_-]*(?=\s*:)") , key),
            (QRegularExpression(r"#.*$"), comment),
            (QRegularExpression(r"(?:\"[^\"]*\"|'[^']*')"), string),
        )
        self.update_theme()

    def update_theme(self) -> None:
        from .theme import is_dark_theme

        dark = is_dark_theme()
        for style, color in zip(self._formats, ("#8AB4F8" if dark else "#235A97", "#9AA0A6" if dark else "#6A737D", "#F9AB79" if dark else "#8A3B12")):
            style.setForeground(QColor(color))
        self.rehighlight()

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
    previewResolutionRequested = Signal()

    def __init__(self, component_id: str, parent=None):
        super().__init__(parent)
        self.component_id = component_id
        self.identity, self.preview_generation = uuid4().hex, 0
        self.preview_row_number: int | None = None
        self.resolved_component: ComponentDefinition | None = None
        self.preview_deferred = False
        self.displayed_generation = -1
        self.image_quality = ()
        self.quality_warning_fingerprint = ()
        self._preview_arguments: PreviewArguments | None = None; self._deferred_preview: DeferredPreview | None = None
        self._applying_image = False
        self.undo_stack = QUndoStack(self)
        self.setObjectName(f"documentTab-{component_id}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        controls = QHBoxLayout()
        self.layout_mode = QPushButton(tr("ui.layout"))
        self.layout_mode.setObjectName("layoutMode")
        self.text_mode = QPushButton(tr("ui.text"))
        self.text_mode.setObjectName("textMode")
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for button in (self.layout_mode, self.text_mode):
            button.setCheckable(True)
            self.mode_group.addButton(button)
            button.setToolTip(tr("ui.drag") if button is self.layout_mode else tr("ui.text"))
            button.setAccessibleName(button.toolTip())
            button.setText("")
            set_themed_icon(button, "brick.png" if button is self.layout_mode else "code.png")
            button.setIconSize(QSize(22, 22))
            controls.addWidget(button)
        self.layout_mode.setChecked(True)
        self.preview_first = QPushButton()
        self.preview_previous = QPushButton()
        self.preview_next = QPushButton()
        self.preview_last = QPushButton()
        for button, icon, label in (
            (self.preview_first, "begin.png", tr("ui.preview_first")),
            (self.preview_previous, "left.png", tr("ui.preview_previous")),
            (self.preview_next, "right.png", tr("ui.preview_next")),
            (self.preview_last, "end.png", tr("ui.preview_last")),
        ):
            set_themed_icon(button, icon)
            button.setIconSize(QSize(22, 22))
            button.setFixedSize(34, 34)
            button.setToolTip(label)
            button.setAccessibleName(label)
            button.setEnabled(False)
            controls.addWidget(button)
        self.instance_position = QLabel("—")
        self.instance_position.setObjectName("instancePosition")
        self.instance_position.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.instance_position.setMinimumWidth(48)
        self.instance_position.setAccessibleName(tr("ui.instance_position_empty"))
        controls.insertWidget(4, self.instance_position)
        self.instance_controls = (
            self.preview_first, self.preview_previous, self.instance_position,
            self.preview_next, self.preview_last,
        )
        controls.addStretch()
        minus = QPushButton()
        minus.setObjectName("zoomOut")
        set_themed_icon(minus, "zoom_out.png")
        minus.setIconSize(QSize(22, 22))
        minus.setToolTip(tr("ui.preview_zoom_out"))
        minus.setAccessibleName(minus.toolTip())
        minus.setFixedSize(34, 34)
        plus = QPushButton()
        plus.setObjectName("zoomIn")
        set_themed_icon(plus, "zoom_in.png")
        plus.setIconSize(QSize(22, 22))
        plus.setToolTip(tr("ui.preview_zoom_in"))
        plus.setAccessibleName(plus.toolTip())
        plus.setFixedSize(34, 34)
        fit = QPushButton(tr("ui.fit"))
        fit.setObjectName("zoomFit")
        set_themed_icon(fit, "by_screen.png")
        fit.setIconSize(QSize(22, 22))
        fit.setText("")
        fit.setToolTip(tr("ui.fit"))
        fit.setAccessibleName(fit.toolTip())
        fit.setFixedSize(34, 34)
        actual = QPushButton()
        actual.setObjectName("zoomActual")
        set_themed_icon(actual, "real_size.png")
        actual.setIconSize(QSize(22, 22))
        actual.setToolTip(tr("ui.zoom_actual"))
        actual.setAccessibleName(actual.toolTip())
        actual.setFixedSize(34, 34)
        self.zoom_label = QLabel("100%")
        self.zoom_label.setObjectName("zoomLabel")
        for widget in (minus, plus, fit, actual, self.zoom_label):
            controls.addWidget(widget)
        self.zoom_controls = (minus, plus, fit, actual, self.zoom_label)
        layout.addLayout(controls)
        source_row = QHBoxLayout()
        self.preview_source = QLabel()
        self.preview_source.setObjectName("previewSource")
        self.preview_source.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.preview_status = QLabel()
        self.preview_status.setObjectName("previewSourceStatus")
        source_row.addWidget(self.preview_source, 1)
        source_row.addWidget(self.preview_status)
        layout.addLayout(source_row)
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
        self.diagnostic.setStyleSheet("padding: 4px")
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
        self.canvas.visualGestureFinished.connect(self._apply_deferred_preview)
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
        for widget in (*self.instance_controls, *self.zoom_controls):
            widget.setVisible(not text)

    def set_text(self, text: str) -> None:
        if self.text_editor.toPlainText() == text:
            return
        blocked = self.text_editor.blockSignals(True)
        self.text_editor.setPlainText(text)
        self.text_editor.blockSignals(blocked)

    def show_diagnostic(self, message: str = "") -> None:
        self.diagnostic.setText(message)
        from .theme import style_warning

        style_warning(self.diagnostic, bool(message))
        self.diagnostic.setVisible(bool(message))

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if hasattr(self, "highlighter") and event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            blocked = self.text_editor.blockSignals(True)
            try:
                self.highlighter.update_theme()
            finally:
                self.text_editor.blockSignals(blocked)

    def _zoom_changed(self, percent: int) -> None:
        self.zoom_label.setText(f"{percent}%")
        self.zoomChanged.emit(percent)
        if not self._applying_image and self._preview_arguments is not None:
            self.previewResolutionRequested.emit()

    @staticmethod
    def _preview_dpi(component: ComponentDefinition, zoom: float, device_ratio: float) -> int:
        requested = max(1, round(96 * zoom * device_ratio))
        by_edge = min(4096 * MM_PER_INCH / component.size_mm.width, 4096 * MM_PER_INCH / component.size_mm.height)
        by_area = (8_000_000 * MM_PER_INCH * MM_PER_INCH / (component.size_mm.width * component.size_mm.height)) ** 0.5
        return max(1, min(requested, int(by_edge), int(by_area)))

    def preview_request_data(self, project: str) -> PreviewRequest | None:
        arguments = self._preview_arguments
        if arguments is None:
            return None
        identity = PreviewIdentity(
            project, self.component_id, self.identity, arguments.revision,
            self.preview_generation, uuid4().hex,
        )
        return PreviewRequest(
            identity, arguments.snapshot, arguments.component, arguments.dpi,
            arguments.bindings_resolved, arguments.row_number,
            arguments.selected_ids, arguments.fit,
        )

    @property
    def preview_arguments(self) -> PreviewArguments | None:
        return self._preview_arguments

    def invalidate_preview(self) -> None:
        self.preview_timer.stop()
        self.preview_generation += 1
        self.preview_deferred = False
        self._preview_arguments = None
        self._deferred_preview = None

    def set_preview_source(self, label: str = "", status: str = "") -> None:
        self.preview_source.setText(label)
        self.preview_source.setToolTip(label)
        self.preview_status.setText(status)

    def fail_preview_source(self, source_label: str) -> None:
        self.invalidate_preview()
        status = tr("ui.preview_source_error_previous") if self.preview_source.text() else tr("ui.preview_source_error")
        if not self.preview_source.text():
            self.set_preview_source(source_label, status)
        else:
            self.preview_status.setText(status)
        self.canvas.set_preview_pending(False)

    def update_preview_resolution(self, dpi: int) -> bool:
        arguments = self._preview_arguments
        if arguments is None or arguments.dpi == dpi:
            return False
        self._preview_arguments = PreviewArguments(
            arguments.snapshot, arguments.component, arguments.revision, dpi,
            False, arguments.selected_ids, arguments.bindings_resolved, arguments.row_number,
            arguments.source_label,
        )
        self.preview_generation += 1
        self.preview_timer.start()
        return True

    def queue_preview(self, session: ProjectSession, *, component: ComponentDefinition | None = None, dpi: int = 96, fit: bool = False, selected: tuple[str, ...] | None = None, bindings_resolved: bool | None = None, source_label: str = "") -> int:
        document = session.documents[self.component_id]
        active_component = component or document.model
        if selected is None:
            selected = self.canvas.selected_ids
        self.canvas.set_component_model(active_component, selected)
        self.canvas.set_preview_pending(True)
        self.preview_generation += 1
        self._preview_arguments = PreviewArguments(
            session.snapshot, active_component, document.revision, dpi, fit, selected,
            component is not None if bindings_resolved is None else bindings_resolved,
            self.preview_row_number, source_label,
        )
        self.preview_status.setText(
            tr("ui.preview_source_updating") if self.preview_source.text()
            else tr("ui.preview_source_waiting")
        )
        self.preview_timer.start()
        return self.preview_generation

    def render_queued_preview(self, *, register_fonts: bool = False):
        arguments = self._preview_arguments
        if arguments is None:
            return None
        return render_component_image(
            arguments.snapshot,
            self.component_id,
            component=arguments.component,
            dpi=arguments.dpi,
            bindings_resolved=arguments.bindings_resolved,
            register_fonts=register_fonts,
        )

    def apply_preview(self, image, quality=(), *, generation: int, fit: bool = False) -> bool:
        if self._preview_arguments is None or generation != self.preview_generation:
            return False
        arguments = self._preview_arguments
        if self.canvas.gesture_active:
            requested_fit = arguments.fit
            self._deferred_preview = DeferredPreview(image, quality, generation, fit or requested_fit)
            return True
        self._present_preview(image, arguments.component, arguments.selected_ids, quality, generation, fit or arguments.fit)
        return True

    def _present_preview(self, image, component, selected, quality, generation: int, fit: bool) -> None:
        selected = tuple(node_id for node_id in self.canvas.selected_ids if node_id in self.canvas.available_node_ids) or selected
        self._applying_image = True
        try:
            self.canvas.set_document(image, component, fit=fit, selected=selected)
        finally:
            self._applying_image = False
        self.displayed_generation = generation
        self.image_quality = tuple(quality)
        self.set_preview_source(self._preview_arguments.source_label)

    def _apply_deferred_preview(self) -> None:
        deferred = getattr(self, "_deferred_preview", None)
        if deferred is None:
            return
        self._deferred_preview = None
        image, quality, generation, fit = deferred.image, deferred.quality, deferred.generation, deferred.fit
        if self._preview_arguments is None or generation != self.preview_generation:
            return
        arguments = self._preview_arguments
        self._present_preview(image, arguments.component, arguments.selected_ids, quality, generation, fit)

    def fail_preview(self, message: str, *, generation: int) -> bool:
        if self._preview_arguments is None or generation != self.preview_generation:
            return False
        arguments = self._preview_arguments
        self.canvas.set_component_model(arguments.component, arguments.selected_ids)
        if not self.preview_source.text():
            self.set_preview_source(arguments.source_label, tr("ui.preview_source_error"))
        else:
            self.preview_status.setText(tr("ui.preview_source_error_previous"))
        self.previewError.emit(message)
        return True

    def refresh(self, session: ProjectSession, *, component: ComponentDefinition | None = None, fit: bool = False, selected: tuple[str, ...] | None = None) -> bool:
        self.queue_preview(session, component=component, fit=fit, selected=selected)
        return True
