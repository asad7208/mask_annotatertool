"""Mask Annotator - a small desktop editor for binary segmentation masks.

Run with: python app.py
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QImage,
    QImageReader,
    QKeySequence,
    QPainter,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


IMAGE_EXTENSIONS = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
MAX_UNDO_STEPS = 30
CONFIG_PATH = Path(__file__).resolve().with_name("config.json")
PROGRESS_FILENAME = "annotation_progress.json"
COMPLETED_DIRECTORY = "completed"
COMPLETED_MANIFEST_FILENAME = "completed.json"
OUTPUT_DIRECTORY = "output"


def read_image(path: Path) -> QImage:
    """Read an image while respecting orientation metadata."""
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    return reader.read()


def empty_mask(size: QSize) -> QImage:
    # ARGB32 gives QPainter reliable, crisp brush behaviour across Qt platforms.
    # The save path converts this to an 8-bit grayscale PNG.
    mask = QImage(size, QImage.Format.Format_ARGB32)
    mask.fill(QColor(0, 0, 0))
    return mask


def normalise_mask(source: QImage, target_size: QSize) -> QImage:
    """Return a strictly black-and-white mask at the requested size."""
    if source.isNull():
        return empty_mask(target_size)
    if source.size() != target_size:
        source = source.scaled(target_size, Qt.AspectRatioMode.IgnoreAspectRatio,
                               Qt.TransformationMode.FastTransformation)

    mask = QImage(target_size, QImage.Format.Format_ARGB32)
    for y in range(target_size.height()):
        for x in range(target_size.width()):
            value = 255 if source.pixelColor(x, y).value() >= 128 else 0
            mask.setPixelColor(x, y, QColor(value, value, value))
    return mask


class ImageCanvas(QWidget):
    """A fitted image canvas. In mask mode it supports a binary round brush."""

    mask_changed = Signal()
    history_changed = Signal(bool, bool)
    brush_stroke = Signal(QPointF, QPointF, bool)

    def __init__(self, editable: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._editable = editable
        self._editing_allowed = editable
        self._brush_input_enabled = False
        self._image = QImage()
        self._brush_size = 24
        self._paint_value = 255
        self._drawing = False
        self._last_image_pos: QPointF | None = None
        self._cursor_image_pos: QPointF | None = None
        self._undo_stack: list[QImage] = []
        self._redo_stack: list[QImage] = []
        self.setMinimumSize(280, 240)
        self.setMouseTracking(True)
        if editable:
            self.setCursor(Qt.CursorShape.CrossCursor)

    def sizeHint(self) -> QSize:
        return QSize(640, 480)

    @property
    def image(self) -> QImage:
        return self._image

    def set_image(self, image: QImage) -> None:
        self._image = image.copy()
        self._drawing = False
        self._last_image_pos = None
        self._cursor_image_pos = None
        self.update()

    def set_mask(self, image: QImage) -> None:
        self.set_image(image)
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._emit_history_state()

    def set_brush_size(self, size: int) -> None:
        self._brush_size = max(1, size)
        self.update()

    def set_editing_allowed(self, allowed: bool) -> None:
        self._editing_allowed = allowed
        self._update_brush_cursor()

    def set_brush_input_enabled(self, enabled: bool) -> None:
        """Enable context painting: forward strokes without altering this displayed image."""
        self._brush_input_enabled = enabled
        self._update_brush_cursor()

    def _update_brush_cursor(self) -> None:
        self._drawing = False
        self._last_image_pos = None
        self._cursor_image_pos = None
        self.setCursor(
            Qt.CursorShape.CrossCursor
            if (self._editable and self._editing_allowed) or self._brush_input_enabled
            else Qt.CursorShape.ArrowCursor
        )
        self.update()

    def set_paint_value(self, value: int) -> None:
        self._paint_value = 255 if value else 0
        self._update_brush_cursor()
        self.update()

    def apply_external_stroke(self, start: QPointF, end: QPointF, new_stroke: bool) -> None:
        """Apply a stroke forwarded from the original-image context view."""
        if not self._editable or not self._editing_allowed or self._image.isNull():
            return
        if new_stroke:
            self._push_undo()
        self._paint_segment(start, end)
        self.mask_changed.emit()
        self.update()

    def clear_mask(self) -> None:
        if self._image.isNull():
            return
        self._push_undo()
        self._image.fill(0)
        self.mask_changed.emit()
        self.update()

    def invert_mask(self) -> None:
        if self._image.isNull():
            return
        self._push_undo()
        self._image.invertPixels()
        self.mask_changed.emit()
        self.update()

    def undo(self) -> None:
        if not self._undo_stack:
            return
        self._redo_stack.append(self._image.copy())
        self._image = self._undo_stack.pop()
        self._emit_history_state()
        self.mask_changed.emit()
        self.update()

    def redo(self) -> None:
        if not self._redo_stack:
            return
        self._undo_stack.append(self._image.copy())
        self._image = self._redo_stack.pop()
        self._emit_history_state()
        self.mask_changed.emit()
        self.update()

    def _push_undo(self) -> None:
        self._undo_stack.append(self._image.copy())
        if len(self._undo_stack) > MAX_UNDO_STEPS:
            self._undo_stack.pop(0)
        self._redo_stack.clear()
        self._emit_history_state()

    def _emit_history_state(self) -> None:
        self.history_changed.emit(bool(self._undo_stack), bool(self._redo_stack))

    def _image_rect(self) -> QRectF:
        if self._image.isNull() or self.width() <= 0 or self.height() <= 0:
            return QRectF()
        scale = min(self.width() / self._image.width(), self.height() / self._image.height())
        width = self._image.width() * scale
        height = self._image.height() * scale
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)

    def _to_image_pos(self, widget_pos: QPointF) -> QPointF | None:
        image_rect = self._image_rect()
        if image_rect.isEmpty() or not image_rect.contains(widget_pos):
            return None
        x = (widget_pos.x() - image_rect.x()) * self._image.width() / image_rect.width()
        y = (widget_pos.y() - image_rect.y()) * self._image.height() / image_rect.height()
        return QPointF(max(0, min(x, self._image.width() - 1)),
                       max(0, min(y, self._image.height() - 1)))

    def _paint_segment(self, start: QPointF, end: QPointF) -> None:
        painter = QPainter(self._image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        colour = QColor(255, 255, 255) if self._paint_value else QColor(0, 0, 0)
        pen = QPen(colour, self._brush_size, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        if start == end:
            # A zero-length line is not painted consistently by all Qt backends.
            painter.setBrush(colour)
            painter.drawEllipse(start, self._brush_size / 2, self._brush_size / 2)
        else:
            painter.drawLine(start, end)
        painter.end()

    def paintEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#f1f5f9"))
        image_rect = self._image_rect()
        if self._image.isNull():
            painter.setPen(QColor("#64748b"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "Open an image to begin")
            return

        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(image_rect, self._image)

        if (self._editable or self._brush_input_enabled) and self._cursor_image_pos is not None:
            cursor = self._cursor_image_pos
            if 0 <= cursor.x() < self._image.width() and 0 <= cursor.y() < self._image.height():
                scale = image_rect.width() / self._image.width()
                centre = QPointF(image_rect.x() + cursor.x() * scale,
                                 image_rect.y() + cursor.y() * scale)
                radius = self._brush_size * scale / 2
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor("#2563eb"), 1.5))
                painter.drawEllipse(centre, radius, radius)

    def mousePressEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt naming
        can_accept_stroke = (self._editable and self._editing_allowed) or self._brush_input_enabled
        if not can_accept_stroke or event.button() != Qt.MouseButton.LeftButton:
            return
        position = self._to_image_pos(event.position())
        if position is None:
            return
        self._drawing = True
        self._last_image_pos = position
        self._cursor_image_pos = position
        if self._editable:
            self._push_undo()
            self._paint_segment(position, position)
            self.mask_changed.emit()
        else:
            self.brush_stroke.emit(position, position, True)
        self.update()

    def mouseMoveEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt naming
        can_accept_stroke = (self._editable and self._editing_allowed) or self._brush_input_enabled
        if not can_accept_stroke:
            return
        position = self._to_image_pos(event.position())
        self._cursor_image_pos = position
        if self._drawing and position is not None and self._last_image_pos is not None:
            if self._editable:
                self._paint_segment(self._last_image_pos, position)
                self.mask_changed.emit()
            else:
                self.brush_stroke.emit(self._last_image_pos, position, False)
            self._last_image_pos = position
        self.update()

    def mouseReleaseEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.LeftButton and self._drawing:
            self._drawing = False
            self._last_image_pos = None
            self.mask_changed.emit()
            self.update()

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt naming
        self._cursor_image_pos = None
        self.update()
        super().leaveEvent(event)


class CanvasPanel(QFrame):
    def __init__(self, title: str, canvas: ImageCanvas, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("canvasPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(7)
        label = QLabel(title)
        label.setObjectName("canvasTitle")
        layout.addWidget(label)
        layout.addWidget(canvas, 1)


class MaskAnnotator(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Mask Annotator")
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(
                min(1420, max(840, available.width() - 40)),
                min(850, max(600, available.height() - 60)),
            )
        else:
            self.resize(1200, 760)

        self._config = self._read_config()
        configured_users = self._config.get("users", [])
        if not isinstance(configured_users, list):
            configured_users = []
        self._users = [name.strip() for name in configured_users if isinstance(name, str) and name.strip()]
        configured_user = self._config.get("selected_user", "")
        self._selected_user = configured_user if configured_user in self._users else ""
        self._image_paths: list[Path] = []
        self._current_index = -1
        self._project_folder: Path | None = None
        self._mask_folder: Path | None = None
        self._output_folder: Path | None = None
        self._source_path: Path | None = None
        self._is_dirty = False
        self._progress: dict[str, object] = {"images": {}}
        self._completed_images: dict[str, dict[str, object]] = {}
        self._legacy_completion_markers: list[Path] = []

        self.original_canvas = ImageCanvas(editable=False)
        self.mask_canvas = ImageCanvas(editable=True)
        self.original_canvas.brush_stroke.connect(self.mask_canvas.apply_external_stroke)
        self.mask_canvas.mask_changed.connect(self._queue_autosave)
        self.mask_canvas.history_changed.connect(self._set_history_actions)

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(450)
        self._autosave_timer.timeout.connect(self._save_current)

        self._build_actions()
        self._build_ui()
        self._populate_user_selector()
        self._set_history_actions(False, False)
        self._update_controls()
        if not self._restore_saved_project():
            self.statusBar().showMessage("Open a project folder containing images/ and masks/ to start.")

    def _build_actions(self) -> None:
        self.open_folder_action = QAction(
            "Project…", self, shortcut=QKeySequence.StandardKey.Open
        )
        self.open_folder_action.triggered.connect(self._open_project_folder)
        self.manage_users_action = QAction("Users…", self)
        self.manage_users_action.triggered.connect(self._edit_users)
        self.save_action = QAction("Save + next", self, shortcut=QKeySequence.StandardKey.Save)
        self.save_action.triggered.connect(self._save_complete_and_next)
        self.complete_action = QAction("Mark complete", self, shortcut="Ctrl+Return")
        self.complete_action.triggered.connect(self._mark_current_complete)
        self.reopen_action = QAction("Mark incomplete", self)
        self.reopen_action.triggered.connect(self._mark_current_incomplete)
        self.delete_action = QAction("Delete saved output", self, shortcut=QKeySequence.StandardKey.Delete)
        self.delete_action.triggered.connect(self._delete_current_output)
        self.undo_action = QAction("Undo", self, shortcut=QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self.mask_canvas.undo)
        self.redo_action = QAction("Redo", self, shortcut=QKeySequence.StandardKey.Redo)
        self.redo_action.triggered.connect(self.mask_canvas.redo)
        self.clear_action = QAction("Clear mask", self)
        self.clear_action.triggered.connect(self._clear_mask)
        self.invert_action = QAction("Invert mask", self)
        self.invert_action.triggered.connect(self._invert_mask)
        self.previous_action = QAction("Previous", self, shortcut="Left")
        self.previous_action.triggered.connect(lambda: self._move_to_image(-1))
        self.next_action = QAction("Next", self, shortcut="Right")
        self.next_action.triggered.connect(lambda: self._move_to_image(1))

        self.paint_action = QAction("Paint", self, checkable=True, shortcut="B")
        self.erase_action = QAction("Erase", self, checkable=True, shortcut="E")
        brush_group = QActionGroup(self)
        brush_group.setExclusive(True)
        brush_group.addAction(self.paint_action)
        brush_group.addAction(self.erase_action)
        self.paint_action.setChecked(True)
        self.paint_action.triggered.connect(lambda: self._set_paint_value(255))
        self.erase_action.triggered.connect(lambda: self._set_paint_value(0))

    def _build_ui(self) -> None:
        toolbar = QToolBar("Tools", self)
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.addToolBar(toolbar)
        user_label = QLabel("User")
        user_label.setObjectName("toolbarLabel")
        toolbar.addWidget(user_label)
        self.user_selector = QComboBox()
        self.user_selector.setMinimumWidth(135)
        self.user_selector.currentTextChanged.connect(self._set_active_user)
        toolbar.addWidget(self.user_selector)
        toolbar.addSeparator()
        toolbar.addAction(self.open_folder_action)
        toolbar.addSeparator()
        toolbar.addAction(self.paint_action)
        toolbar.addAction(self.erase_action)
        toolbar.addSeparator()

        brush_label = QLabel("Brush")
        brush_label.setObjectName("toolbarLabel")
        toolbar.addWidget(brush_label)
        self.brush_slider = QSlider(Qt.Orientation.Horizontal)
        self.brush_slider.setRange(1, 300)
        self.brush_slider.setValue(24)
        self.brush_slider.setFixedWidth(90)
        self.brush_slider.valueChanged.connect(self._set_brush_size)
        toolbar.addWidget(self.brush_slider)
        self.brush_spinbox = QSpinBox()
        self.brush_spinbox.setRange(1, 300)
        self.brush_spinbox.setSuffix(" px")
        self.brush_spinbox.setValue(24)
        self.brush_spinbox.valueChanged.connect(self._set_brush_size)
        toolbar.addSeparator()
        toolbar.addAction(self.save_action)
        more_menu = QMenu("More actions", self)
        more_menu.addAction(self.manage_users_action)
        more_menu.addSeparator()
        more_menu.addAction(self.undo_action)
        more_menu.addAction(self.redo_action)
        more_menu.addSeparator()
        more_menu.addAction(self.clear_action)
        more_menu.addAction(self.invert_action)
        more_menu.addSeparator()
        more_menu.addAction(self.complete_action)
        more_menu.addAction(self.reopen_action)
        more_menu.addAction(self.delete_action)
        more_button = QToolButton(toolbar)
        more_button.setText("More")
        more_button.setToolTip("More editing and completion actions")
        more_button.setMenu(more_menu)
        more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        toolbar.addWidget(more_button)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(CanvasPanel("Original image  •  Paint here for context", self.original_canvas))
        splitter.addWidget(CanvasPanel("Binary mask  •  Updates while you paint", self.mask_canvas))
        splitter.setSizes([700, 700])
        self.setCentralWidget(splitter)

        queue_panel = QWidget()
        queue_layout = QVBoxLayout(queue_panel)
        queue_layout.setContentsMargins(9, 9, 9, 9)
        queue_layout.setSpacing(8)
        self.incomplete_label = QLabel("Incomplete (0)")
        self.incomplete_label.setObjectName("queueTitle")
        queue_layout.addWidget(self.incomplete_label)
        self.incomplete_list = QListWidget()
        self.incomplete_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.incomplete_list.currentItemChanged.connect(self._select_list_item)
        queue_layout.addWidget(self.incomplete_list, 1)
        self.completed_label = QLabel("Completed (0)")
        self.completed_label.setObjectName("queueTitle")
        queue_layout.addWidget(self.completed_label)
        self.completed_list = QListWidget()
        self.completed_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.completed_list.currentItemChanged.connect(self._select_list_item)
        queue_layout.addWidget(self.completed_list, 1)
        self.progress_label = QLabel()
        self.progress_label.setObjectName("progressLabel")
        self.progress_label.setWordWrap(True)
        queue_layout.addWidget(self.progress_label)
        navigation = QHBoxLayout()
        previous_button = QPushButton("← Previous")
        previous_button.clicked.connect(lambda: self._move_to_image(-1))
        next_button = QPushButton("Next →")
        next_button.clicked.connect(lambda: self._move_to_image(1))
        navigation.addWidget(previous_button)
        navigation.addWidget(next_button)
        queue_layout.addLayout(navigation)
        queue_panel.setMinimumWidth(250)
        queue_panel.setMaximumWidth(340)

        queue_dock = QFrame()
        queue_dock.setObjectName("queuePanel")
        dock_layout = QVBoxLayout(queue_dock)
        dock_layout.setContentsMargins(0, 0, 0, 0)
        dock_layout.addWidget(queue_panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self._make_dock(queue_dock))

        status = QStatusBar(self)
        self.setStatusBar(status)

        file_menu = self.menuBar().addMenu("File")
        file_menu.addAction(self.open_folder_action)
        file_menu.addAction(self.save_action)
        file_menu.addSeparator()
        file_menu.addAction(self.complete_action)
        file_menu.addAction(self.reopen_action)
        file_menu.addAction(self.delete_action)
        people_menu = self.menuBar().addMenu("People")
        people_menu.addAction(self.manage_users_action)
        edit_menu = self.menuBar().addMenu("Edit")
        edit_menu.addAction(self.undo_action)
        edit_menu.addAction(self.redo_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.paint_action)
        edit_menu.addAction(self.erase_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.clear_action)
        edit_menu.addAction(self.invert_action)

        self.setStyleSheet("""
            QMainWindow { background: #f8fafc; color: #172033; }
            QToolBar { background: #ffffff; border: 0; border-bottom: 1px solid #dbe3ef; padding: 7px; spacing: 5px; }
            QToolButton { border: 1px solid transparent; border-radius: 6px; padding: 6px 8px; }
            QToolButton:hover { background: #eef4ff; border-color: #c8d9f7; }
            QToolButton:checked { background: #dbeafe; border-color: #93c5fd; color: #1d4ed8; font-weight: 600; }
            QFrame#canvasPanel { background: #ffffff; border: 1px solid #dbe3ef; border-radius: 9px; }
            QLabel#canvasTitle, QLabel#queueTitle { font-weight: 600; color: #334155; }
            QLabel#toolbarLabel { color: #475569; margin-left: 4px; }
            QLabel#progressLabel { background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 6px; color: #334155; padding: 8px; }
            QComboBox { background: #ffffff; border: 1px solid #cbd5e1; border-radius: 6px; padding: 5px 8px; }
            QListWidget { background: #ffffff; border: 1px solid #dbe3ef; border-radius: 7px; padding: 4px; }
            QListWidget::item { padding: 7px; border-radius: 5px; }
            QListWidget::item:selected { background: #dbeafe; color: #1e3a8a; }
            QPushButton { background: #ffffff; border: 1px solid #cbd5e1; border-radius: 6px; padding: 7px; }
            QPushButton:hover { background: #f1f5f9; }
            QStatusBar { background: #ffffff; border-top: 1px solid #dbe3ef; color: #475569; }
        """)

    def _make_dock(self, widget: QWidget):
        from PySide6.QtWidgets import QDockWidget

        dock = QDockWidget("", self)
        dock.setObjectName("imageQueueDock")
        dock.setTitleBarWidget(QWidget())
        dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        dock.setWidget(widget)
        return dock

    @staticmethod
    def _read_config() -> dict[str, object]:
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return config if isinstance(config, dict) else {}

    def _populate_user_selector(self) -> None:
        self.user_selector.blockSignals(True)
        self.user_selector.clear()
        self.user_selector.addItem("Select username…")
        self.user_selector.addItems(self._users)
        selected_index = self.user_selector.findText(self._selected_user)
        self.user_selector.setCurrentIndex(selected_index if selected_index > 0 else 0)
        self.user_selector.blockSignals(False)
        self._refresh_progress()

    def _edit_users(self) -> None:
        names, accepted = QInputDialog.getText(
            self,
            "Edit users",
            "Enter usernames separated by commas:",
            text=", ".join(self._users),
        )
        if not accepted:
            return
        users: list[str] = []
        for name in names.split(","):
            clean_name = name.strip()
            if clean_name and clean_name not in users:
                users.append(clean_name)
        self._users = users
        if self._selected_user not in self._users:
            self._selected_user = ""
        self._populate_user_selector()
        self._write_config()
        self._update_controls()

    def _set_active_user(self, name: str) -> None:
        self._selected_user = name if name in self._users else ""
        self._write_config()
        self._update_controls()
        if self._selected_user:
            self.statusBar().showMessage(f"Editing as {self._selected_user}", 3000)

    def _load_progress(self) -> None:
        self._progress = {"images": {}}
        if self._project_folder is None:
            self._refresh_progress()
            return
        progress_path = self._project_folder / PROGRESS_FILENAME
        try:
            loaded = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = {}
        if isinstance(loaded, dict) and isinstance(loaded.get("images"), dict):
            self._progress = loaded
        self._refresh_progress()

    def _load_completed_images(self) -> None:
        self._completed_images = {}
        self._legacy_completion_markers = []
        if self._project_folder is None:
            return
        completed_folder = self._project_folder / COMPLETED_DIRECTORY
        if not completed_folder.is_dir():
            return
        manifest_path = completed_folder / COMPLETED_MANIFEST_FILENAME
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
        records = manifest.get("images", {}) if isinstance(manifest, dict) else {}
        if isinstance(records, dict):
            for image_name, record in records.items():
                if isinstance(image_name, str) and isinstance(record, dict):
                    self._completed_images[image_name] = record

        # Read older per-image marker files so they can be consolidated on the next save.
        for marker_path in completed_folder.glob("*.json"):
            if marker_path == manifest_path:
                continue
            try:
                record = json.loads(marker_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            image_name = record.get("image_filename")
            if not isinstance(image_name, str) or not image_name:
                continue
            self._completed_images.setdefault(image_name, record)
            self._legacy_completion_markers.append(marker_path)

    def _completed_folder(self) -> Path | None:
        return self._project_folder / COMPLETED_DIRECTORY if self._project_folder else None

    def _completed_manifest_path(self) -> Path | None:
        completed_folder = self._completed_folder()
        return completed_folder / COMPLETED_MANIFEST_FILENAME if completed_folder else None

    def _write_completed_manifest(self) -> bool:
        manifest_path = self._completed_manifest_path()
        if manifest_path is None:
            return False
        records = {
            image_name: {key: value for key, value in record.items() if not key.startswith("_")}
            for image_name, record in self._completed_images.items()
        }
        manifest = {
            "project_folder": str(self._project_folder),
            "images": records,
        }
        try:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            for legacy_marker in self._legacy_completion_markers:
                if legacy_marker.exists():
                    legacy_marker.unlink()
        except OSError as error:
            QMessageBox.warning(
                self, "Could not save completion list", f"Could not update {COMPLETED_MANIFEST_FILENAME}:\n{error}"
            )
            return False
        self._legacy_completion_markers = []
        return True

    def _is_current_complete(self) -> bool:
        return self._source_path is not None and self._source_path.name in self._completed_images

    def _rebuild_image_lists(self) -> None:
        if not hasattr(self, "incomplete_list"):
            return
        selected_path = self._source_path
        self.incomplete_list.blockSignals(True)
        self.completed_list.blockSignals(True)
        self.incomplete_list.clear()
        self.completed_list.clear()
        selected_item: QListWidgetItem | None = None
        incomplete_count = 0
        completed_count = 0
        for index, image_path in enumerate(self._image_paths):
            completed_record = self._completed_images.get(image_path.name)
            if completed_record is None:
                item = QListWidgetItem(image_path.name)
                self.incomplete_list.addItem(item)
                incomplete_count += 1
            else:
                editor = completed_record.get("completed_by", "Unknown")
                item = QListWidgetItem(f"{image_path.name}  •  {editor}")
                self.completed_list.addItem(item)
                completed_count += 1
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setToolTip(str(image_path))
            if image_path == selected_path:
                selected_item = item
        if selected_item is not None:
            target_list = self.completed_list if self._is_current_complete() else self.incomplete_list
            target_list.setCurrentItem(selected_item)
        self.incomplete_list.blockSignals(False)
        self.completed_list.blockSignals(False)
        self.incomplete_label.setText(f"Incomplete ({incomplete_count})")
        self.completed_label.setText(f"Completed ({completed_count})")

    def _select_list_item(
        self, current: QListWidgetItem | None, previous: QListWidgetItem | None
    ) -> None:
        if current is None:
            return
        index = current.data(Qt.ItemDataRole.UserRole)
        if isinstance(index, int):
            self._select_image(index)

    def _write_progress(self) -> None:
        if self._project_folder is None:
            return
        progress_path = self._project_folder / PROGRESS_FILENAME
        self._progress["project_folder"] = str(self._project_folder)
        try:
            progress_path.write_text(json.dumps(self._progress, indent=2) + "\n", encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(
                self, "Could not save progress", f"Could not update {PROGRESS_FILENAME}:\n{error}"
            )

    def _record_edit(self, mask_path: Path) -> None:
        if self._source_path is None or not self._selected_user:
            return
        images = self._progress.setdefault("images", {})
        if not isinstance(images, dict):
            images = {}
            self._progress["images"] = images
        image_key = self._source_path.name
        record = images.get(image_key, {})
        if not isinstance(record, dict):
            record = {}
        contributors = record.get("contributors", [])
        if not isinstance(contributors, list):
            contributors = []
        if self._selected_user not in contributors:
            contributors.append(self._selected_user)
        previous_edits = record.get("edit_count", 0)
        edit_count = previous_edits if isinstance(previous_edits, int) else 0
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        images[image_key] = {
            "image_path": str(self._source_path),
            "mask_path": str(mask_path),
            "last_edited_by": self._selected_user,
            "last_edited_at": timestamp,
            "contributors": contributors,
            "edit_count": edit_count + 1,
        }
        self._write_progress()
        self._refresh_progress()

    def _refresh_progress(self) -> None:
        if not hasattr(self, "progress_label"):
            return
        images = self._progress.get("images", {})
        if not isinstance(images, dict) or not images:
            self.progress_label.setText("Progress\nNo edited images yet")
            return
        counts: dict[str, int] = {}
        for record in images.values():
            if not isinstance(record, dict):
                continue
            contributors = record.get("contributors", [])
            if isinstance(contributors, list):
                for user in contributors:
                    if isinstance(user, str):
                        counts[user] = counts.get(user, 0) + 1
        lines = [f"Progress  •  {len(images)} image(s) edited"]
        displayed_users = list(self._users)
        displayed_users.extend(user for user in counts if user not in displayed_users)
        for user in displayed_users:
            lines.append(f"{user}: {counts.get(user, 0)} image(s)")
        self.progress_label.setText("\n".join(lines))

    def _set_brush_size(self, size: int) -> None:
        if self.brush_slider.value() != size:
            self.brush_slider.setValue(size)
        if self.brush_spinbox.value() != size:
            self.brush_spinbox.setValue(size)
        self.original_canvas.set_brush_size(size)
        self.mask_canvas.set_brush_size(size)

    def _set_paint_value(self, value: int) -> None:
        self.original_canvas.set_paint_value(value)
        self.mask_canvas.set_paint_value(value)

    def _set_history_actions(self, can_undo: bool, can_redo: bool) -> None:
        can_edit = self._source_path is not None and bool(self._selected_user)
        self.undo_action.setEnabled(can_edit and can_undo)
        self.redo_action.setEnabled(can_edit and can_redo)

    def _update_controls(self) -> None:
        active = self._source_path is not None
        can_edit = active and bool(self._selected_user)
        self.original_canvas.set_brush_input_enabled(can_edit)
        self.mask_canvas.set_editing_allowed(can_edit)
        for action in (self.save_action, self.clear_action, self.invert_action,
                       self.paint_action, self.erase_action):
            action.setEnabled(can_edit)
        self.brush_slider.setEnabled(can_edit)
        self.brush_spinbox.setEnabled(can_edit)
        self.previous_action.setEnabled(active)
        self.next_action.setEnabled(active)
        self.complete_action.setEnabled(can_edit and active and not self._is_current_complete())
        self.reopen_action.setEnabled(can_edit and active and self._is_current_complete())
        self.delete_action.setEnabled(active)
        self._set_history_actions(
            bool(self.mask_canvas._undo_stack), bool(self.mask_canvas._redo_stack)
        )

    def _open_project_folder(self) -> None:
        start_directory = self._project_folder or Path.home()
        folder = QFileDialog.getExistingDirectory(
            self, "Choose project folder", str(start_directory)
        )
        if not folder:
            return
        self._load_project(Path(folder), save_to_config=True)

    def _restore_saved_project(self) -> bool:
        saved_path = self._config.get("project_folder", "")
        if not isinstance(saved_path, str) or not saved_path:
            return False
        return self._load_project(Path(saved_path), save_to_config=False, show_errors=False)

    def _write_config(self) -> None:
        self._config = {
            "project_folder": str(self._project_folder) if self._project_folder else "",
            "users": self._users,
            "selected_user": self._selected_user,
        }
        try:
            CONFIG_PATH.write_text(json.dumps(self._config, indent=2) + "\n", encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(self, "Could not update config", f"Could not save config.json:\n{error}")

    def _load_project(
        self, folder: Path, *, save_to_config: bool, show_errors: bool = True
    ) -> bool:
        images_folder = folder / "images"
        masks_folder = folder / "masks"
        if not images_folder.is_dir() or not masks_folder.is_dir():
            if show_errors:
                QMessageBox.warning(
                    self,
                    "Invalid project folder",
                    "Choose a folder containing both an images/ folder and a masks/ folder.",
                )
            return False
        paths = sorted(
            (path for path in images_folder.iterdir()
             if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS),
            key=lambda path: path.name.lower(),
        )
        if not paths:
            if show_errors:
                QMessageBox.information(
                    self, "No images found", "The images/ folder has no supported image files."
                )
            return False

        self._save_current()
        self._project_folder = folder
        self._mask_folder = masks_folder
        self._output_folder = folder / OUTPUT_DIRECTORY
        self._source_path = None
        self._is_dirty = False
        self._load_progress()
        self._load_completed_images()
        self._set_images(paths)
        if save_to_config:
            self._write_config()
        return True

    def _set_images(self, paths: list[Path]) -> None:
        valid_paths = [path for path in paths if path.suffix.lower() in IMAGE_EXTENSIONS]
        if not valid_paths:
            return
        self._save_current()
        self._image_paths = valid_paths
        self._current_index = -1
        self._rebuild_image_lists()
        self._select_image(0)

    def _select_image(self, row: int) -> None:
        if row < 0 or row >= len(self._image_paths) or row == self._current_index:
            return
        self._save_current()
        self._current_index = row
        self._load_current()

    def _move_to_image(self, offset: int) -> None:
        new_row = self._current_index + offset
        if 0 <= new_row < len(self._image_paths):
            self._select_image(new_row)

    def _matching_mask_path(self, image_path: Path) -> Path | None:
        if self._output_folder is not None:
            working_mask = self._output_folder / "masks" / f"{image_path.stem}.png"
            if working_mask.is_file():
                return working_mask
        if self._mask_folder is None or not self._mask_folder.is_dir():
            return None
        # Original masks are used only when there is no saved working version.
        png_mask = self._mask_folder / f"{image_path.stem}.png"
        if png_mask.is_file():
            return png_mask
        exact = self._mask_folder / image_path.name
        if exact.is_file():
            return exact
        stem = image_path.stem.casefold()
        matches = [path for path in self._mask_folder.iterdir()
                   if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
                   and path.stem.casefold() == stem]
        return sorted(matches, key=lambda path: path.suffix.lower())[0] if matches else None

    def _load_current(self) -> None:
        source_path = self._image_paths[self._current_index]
        source = read_image(source_path)
        if source.isNull():
            QMessageBox.warning(self, "Could not load image", f"Could not open:\n{source_path}")
            return
        self._source_path = source_path
        self.original_canvas.set_image(source)
        mask_path = self._matching_mask_path(source_path)
        if mask_path is not None:
            loaded_mask = read_image(mask_path)
            mask = normalise_mask(loaded_mask, source.size())
            mask_message = f"Loaded matching mask: {mask_path.name}"
        else:
            mask = empty_mask(source.size())
            mask_message = "New blank binary mask"
        self.mask_canvas.set_mask(mask)
        self._is_dirty = False
        self._rebuild_image_lists()
        self._update_controls()
        self.statusBar().showMessage(f"{source_path.name}  •  {mask_message}", 5000)

    def _mark_current_complete(self) -> None:
        if self._source_path is None or self._output_folder is None or not self._selected_user:
            return
        mask_path = self._output_folder / "masks" / f"{self._source_path.stem}.png"
        if not mask_path.exists():
            self._is_dirty = True
        self._save_current()
        if self._is_dirty:
            self.statusBar().showMessage("Could not mark complete until the mask is saved.", 5000)
            return
        image_destination = self._output_folder / "images" / self._source_path.name
        record = {
            "image_filename": self._source_path.name,
            "image_path": str(self._source_path),
            "output_image_path": str(image_destination),
            "mask_path": str(mask_path),
            "completed_by": self._selected_user,
            "completed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        try:
            image_destination.parent.mkdir(parents=True, exist_ok=True)
            if self._source_path.resolve() != image_destination.resolve():
                shutil.copy2(self._source_path, image_destination)
        except (OSError, shutil.Error) as error:
            QMessageBox.warning(self, "Could not complete image", f"Could not save image copy:\n{error}")
            return
        self._completed_images[self._source_path.name] = record
        if not self._write_completed_manifest():
            self._completed_images.pop(self._source_path.name, None)
            return
        self._rebuild_image_lists()
        self._refresh_progress()
        self._update_controls()
        self.statusBar().showMessage(f"Marked {self._source_path.name} complete.", 3500)

    def _save_complete_and_next(self) -> None:
        """Save the current work, place it in Completed, then open the next image."""
        if self._source_path is None:
            return
        current_index = self._current_index
        self._mark_current_complete()
        if not self._is_current_complete():
            return
        next_index = current_index + 1
        if next_index < len(self._image_paths):
            self._select_image(next_index)
        else:
            self.statusBar().showMessage("Saved and completed the last image in the queue.", 3500)

    def _mark_current_incomplete(self) -> None:
        if self._source_path is None:
            return
        record = self._completed_images.pop(self._source_path.name, None)
        if not self._write_completed_manifest():
            if record is not None:
                self._completed_images[self._source_path.name] = record
            return
        self._rebuild_image_lists()
        self._refresh_progress()
        self._update_controls()
        self.statusBar().showMessage(f"Returned {self._source_path.name} to incomplete.", 3500)

    def _delete_current_output(self) -> None:
        if self._source_path is None or self._output_folder is None:
            return
        image_path = self._source_path
        output_image = self._output_folder / "images" / image_path.name
        output_mask = self._output_folder / "masks" / f"{image_path.stem}.png"
        targets = [target for target in (output_image, output_mask) if target.exists()]
        target_lines = "\n".join(f"• {path}" for path in targets)
        answer = QMessageBox.question(
            self,
            "Delete saved output",
            "Delete this image's saved output and completion status? The original image and "
            f"original mask will stay unchanged.\n\n{target_lines or '• Completion status only'}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._autosave_timer.stop()
        try:
            for target in targets:
                if target.exists():
                    target.unlink()
        except OSError as error:
            QMessageBox.warning(self, "Delete failed", f"Could not delete saved output:\n{error}")
            return
        self._is_dirty = False
        self._completed_images.pop(image_path.name, None)
        if not self._write_completed_manifest():
            return
        images = self._progress.get("images", {})
        if isinstance(images, dict):
            images.pop(image_path.name, None)
            self._write_progress()
        self._load_current()
        self._refresh_progress()
        self.statusBar().showMessage(f"Deleted saved output for {image_path.name}; originals remain unchanged.", 4000)

    def _clear_mask(self) -> None:
        answer = QMessageBox.question(
            self, "Clear mask", "Clear this mask to black? You can undo this action.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.mask_canvas.clear_mask()

    def _invert_mask(self) -> None:
        self.mask_canvas.invert_mask()

    def _queue_autosave(self) -> None:
        if (self._source_path is None or self.mask_canvas.image.isNull()
                or not self._selected_user):
            return
        self._is_dirty = True
        self._autosave_timer.start()
        self.statusBar().showMessage("Autosave pending…")

    def _save_current(self) -> None:
        self._autosave_timer.stop()
        if (self._source_path is None or self.mask_canvas.image.isNull()
                or not self._is_dirty or not self._selected_user or self._output_folder is None):
            return
        try:
            masks_dir = self._output_folder / "masks"
            masks_dir.mkdir(parents=True, exist_ok=True)
            mask_destination = masks_dir / f"{self._source_path.stem}.png"
            binary_mask = self.mask_canvas.image.convertToFormat(QImage.Format.Format_Grayscale8)
            if not binary_mask.save(str(mask_destination), "PNG"):
                raise OSError("Qt could not write the PNG mask")
        except OSError as error:
            QMessageBox.warning(self, "Autosave failed", f"Could not save mask:\n{error}")
            self.statusBar().showMessage("Autosave failed", 5000)
            return
        self._is_dirty = False
        self._record_edit(mask_destination)
        self.statusBar().showMessage(
            f"Saved working mask to output/masks/  •  edited by {self._selected_user}", 3500
        )

    def closeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt naming
        self._save_current()
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Mask Annotator")
    app.setOrganizationName("Mask Annotator")
    window = MaskAnnotator()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
