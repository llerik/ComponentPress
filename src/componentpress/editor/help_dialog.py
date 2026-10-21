"""Small offline help window shared for the lifetime of the main window."""

from html import escape

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLineEdit, QListWidget, QPushButton, QTextBrowser, QVBoxLayout

from componentpress.i18n import tr


class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("helpDialog")
        self.setWindowTitle(tr("help.title"))
        self.resize(900, 620)
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.back_button = QPushButton("←")
        self.back_button.setAccessibleName(tr("help.back"))
        self.back_button.setToolTip(tr("help.back"))
        self.forward_button = QPushButton("→")
        self.forward_button.setAccessibleName(tr("help.forward"))
        self.forward_button.setToolTip(tr("help.forward"))
        self.back_button.clicked.connect(lambda: self._go_history(-1))
        self.forward_button.clicked.connect(lambda: self._go_history(1))
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("help.search"))
        self.search.setClearButtonEnabled(True)
        toolbar.addWidget(self.back_button)
        toolbar.addWidget(self.forward_button)
        toolbar.addWidget(self.search, 1)
        layout.addLayout(toolbar)
        body = QHBoxLayout()
        self.sections = QListWidget()
        self.sections.setMinimumWidth(190)
        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)
        self.browser.anchorClicked.connect(self._open_link)
        body.addWidget(self.sections)
        body.addWidget(self.browser, 1)
        layout.addLayout(body, 1)
        self._keys = ("help.start", "help.project", "help.canvas", "help.data", "help.build", "help.recovery", "help.troubleshoot", "help.hotkeys")
        self._titles = [tr(f"{key}.title") for key in self._keys]
        self._content = [tr(key) for key in self._keys]
        self._selected_title = ""
        self._history: list[int] = []
        self._history_index = -1
        self._navigating_history = False
        self.search.textChanged.connect(self._filter)
        self.sections.currentRowChanged.connect(self._show_row)
        self._filter("")
        self.sections.setCurrentRow(0)

    def _filter(self, text: str) -> None:
        query = text.casefold().strip()
        current = self._selected_title
        self.sections.blockSignals(True)
        self.sections.clear()
        visible = []
        for title, content in zip(self._titles, self._content):
            if not query or query in title.casefold() or query in content.casefold():
                self.sections.addItem(title)
                visible.append(title)
        self.sections.blockSignals(False)
        if visible:
            self.sections.setCurrentRow(visible.index(current) if current in visible else 0)

    def _show_row(self, row: int) -> None:
        if row < 0:
            self.browser.clear()
            return
        title = self.sections.item(row).text()
        index = self._titles.index(title)
        self._selected_title = title
        related = " · ".join(
            f'<a href="help://{key.removeprefix("help.")}">{escape(title)}</a>'
            for key, title in zip(self._keys, self._titles) if key != self._keys[index]
        )
        self.browser.setHtml(self._content[index] + f"<hr><p>{tr('help.related')}: {related}</p>")
        if not self._navigating_history and (self._history_index < 0 or self._history[self._history_index] != index):
            del self._history[self._history_index + 1:]
            self._history.append(index)
            self._history_index = len(self._history) - 1
        self._update_history_actions()

    def _go_history(self, direction: int) -> None:
        destination = self._history_index + direction
        if not 0 <= destination < len(self._history):
            return
        index = self._history[destination]
        title = self._titles[index]
        if not self.sections.findItems(title, Qt.MatchFlag.MatchExactly):
            self.search.clear()
        row = next((i for i in range(self.sections.count()) if self.sections.item(i).text() == title), -1)
        if row < 0:
            return
        self._history_index = destination
        self._navigating_history = True
        self.sections.setCurrentRow(row)
        self._navigating_history = False
        self._update_history_actions()

    def _update_history_actions(self) -> None:
        self.back_button.setEnabled(self._history_index > 0)
        self.forward_button.setEnabled(self._history_index + 1 < len(self._history))

    def _open_link(self, url) -> None:
        if url.scheme().lower() == "https":
            QDesktopServices.openUrl(url)
        elif url.scheme().lower() == "help" and url.host():
            title = tr(f"help.{url.host()}.title")
            matches = self.sections.findItems(title, Qt.MatchFlag.MatchExactly)
            if not matches:
                self.search.clear()
                matches = self.sections.findItems(title, Qt.MatchFlag.MatchExactly)
            if matches:
                self.sections.setCurrentItem(matches[0])

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)
