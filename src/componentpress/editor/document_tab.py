"""One component tab with synchronized layout and YAML views."""

from PySide6.QtCore import QRegularExpression, Signal
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
)

from componentpress.application.sessions import ProjectSession
from componentpress.domain.diagnostics import ProjectError
from componentpress.rendering import render_component_image
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

    def __init__(self, component_id: str, parent=None):
        super().__init__(parent)
        self.component_id = component_id
        self.preview_row_number: int | None = None
        self.resolved_component: ComponentDefinition | None = None
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
        layout.addLayout(controls)
        self.canvas = ComponentCanvas()
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

    def refresh(self, session: ProjectSession, *, component: ComponentDefinition | None = None, fit: bool = False, selected: tuple[str, ...] | None = None) -> bool:
        document = session.documents[self.component_id]
        try:
            image = render_component_image(
                session.snapshot,
                self.component_id,
                component=component or document.model,
                dpi=96,
                bindings_resolved=component is not None,
            )
        except ProjectError as exc:
            self.previewError.emit(str(exc.diagnostic))
            return False
        if selected is None:
            selected = self.canvas.selected_ids
        self.canvas.set_document(image, component or document.model, fit=fit, selected=selected)
        return True
