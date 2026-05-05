"""
主窗口控制器
管理菜单、工具栏、停靠面板和3D视口
"""

import csv
import glob
import json
import os
import re
import struct
import unicodedata
from collections import defaultdict
import vtk

from PySide6.QtWidgets import (
    QMainWindow,
    QDockWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QStatusBar,
    QFileDialog,
    QMessageBox,
    QProgressBar,
    QLabel,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QWidget,
    QMenu,
    QMenuBar,
    QStackedWidget,
    QGroupBox,
    QPushButton,
    QAbstractItemView,
    QScrollArea,
    QFrame,
)
from PySide6.QtCore import Qt, QSize, QTimer, QUrl
from PySide6.QtGui import QAction, QKeySequence, QDesktopServices, QPixmap, QFont

try:
    from PySide6.QtWebEngineCore import QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView
except ImportError:
    QWebEngineSettings = None
    QWebEngineView = None

try:
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtPdfWidgets import QPdfView
except ImportError:
    QPdfDocument = None
    QPdfView = None

from core.vtk_widget import VTKWidget
from core.model_loader import ModelLoadThread, get_supported_formats
from core.paths import app_base_dir, app_path


class MainWindow(QMainWindow):
    DAMAGE_NODE_ID_ROLE = Qt.UserRole + 100
    DAMAGE_PARENT_ID_ROLE = Qt.UserRole + 101
    DAMAGE_LEVEL_ROLE = Qt.UserRole + 102
    GLTF_THREADED_LOAD_THRESHOLD_BYTES = 64 * 1024 * 1024

    """主窗口"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("工业模型查看器 - ShipViewer v1.0")
        self.resize(1400, 850)

        # 状态
        self._current_file = None
        self._load_thread = None
        self._gltf_importer = None
        self._loaded_items = []
        self._is_wireframe = False
        self._project_root = app_base_dir()
        self._assets_root = app_path("assets")
        self._damage_tree_csv_path = self._resolve_data_file(
            ("assets", "csv", "damage-tree-nodes.csv"),
            "damage-tree-nodes.csv",
        )
        self._pdf_catalog_csv_path = self._resolve_data_file(
            ("assets", "csv", "part-pdf-catalog.csv"),
            "part-pdf-catalog.csv",
        )
        self._device_catalog_json_path = self._resolve_data_file(
            ("assets", "json", "device-catalog.json"),
            "device-catalog.json",
        )
        self._viewport_background_path = self._resolve_background_hdr_path()
        self._water_texture_path = self._resolve_data_file(
            ("assets", "water", "water_diffuse.jpg"),
            ("assets", "water", "water_diffuse.png"),
            ("assets", "water", "material.png"),
            ("assets", "water", "water.jpg"),
            ("assets", "water", "water.png"),
        )
        self._pdf_catalog_rows = []
        self._pdf_catalog_by_model_name = defaultdict(list)
        self._pdf_catalog_by_damage_leaf_id = defaultdict(list)
        self._device_catalog_rows = []
        self._device_catalog_by_model_name = defaultdict(list)
        self._device_catalog_by_damage_leaf_id = defaultdict(list)
        self._damage_nodes_by_id = {}
        self._damage_children_by_id = defaultdict(list)
        self._damage_node_ids = set()
        self._current_catalog_matches = []
        self._current_document_path = None
        self._actor_catalog_names = {}
        self._actor_display_names = {}

        # 构建界面
        self._create_vtk_viewport()
        self._create_menus()
        self._create_toolbar()
        self._create_model_tree_dock()
        self._create_properties_dock()
        self._create_status_bar()
        self._load_pdf_catalog_from_csv()
        self._load_device_catalog_from_json()
        self._load_damage_tree_from_csv()

        # 信号连接
        self.vtk_widget.model_clicked.connect(self._on_vtk_model_clicked)

        # 初始化VTK（延迟到窗口显示后）
        QTimer.singleShot(100, self._init_vtk)

    # ============================================================
    # UI 构建
    # ============================================================

    def _resolve_data_file(self, *relative_candidates):
        for candidate in relative_candidates:
            if isinstance(candidate, (tuple, list)):
                path = os.path.join(self._project_root, *candidate)
            else:
                path = os.path.join(self._project_root, candidate)
            path = os.path.normpath(path)
            if os.path.exists(path):
                return path

        first = relative_candidates[0]
        if isinstance(first, (tuple, list)):
            return os.path.normpath(os.path.join(self._project_root, *first))
        return os.path.normpath(os.path.join(self._project_root, first))

    def _resolve_background_hdr_path(self):
        backgrounds_dir = os.path.join(self._assets_root, "backgrounds")
        preferred_names = (
            "background.hdr",
            "environment.hdr",
            "water.hdr",
            "citrus_orchard_puresky_4k.hdr",
        )

        for filename in preferred_names:
            path = os.path.join(backgrounds_dir, filename)
            if os.path.exists(path):
                return os.path.normpath(path)

        hdr_paths = sorted(glob.glob(os.path.join(backgrounds_dir, "*.hdr")))
        if hdr_paths:
            return os.path.normpath(hdr_paths[0])

        return os.path.normpath(os.path.join(backgrounds_dir, preferred_names[0]))

    def _create_vtk_viewport(self):
        """创建VTK 3D视口"""
        self.vtk_widget = VTKWidget(self)
        self.setCentralWidget(self.vtk_widget)

    def _create_menus(self):
        """创建菜单栏"""
        menubar = self.menuBar()

        # === 文件菜单 ===
        file_menu = menubar.addMenu("文件(&F)")

        self.action_open = QAction("打开模型(&O)...", self)
        self.action_open.setShortcut(QKeySequence("Ctrl+O"))
        self.action_open.setToolTip("打开 3DM 或 GLB 模型文件")
        self.action_open.triggered.connect(self.open_file)
        file_menu.addAction(self.action_open)

        file_menu.addSeparator()

        self.action_screenshot = QAction("截图保存(&S)...", self)
        self.action_screenshot.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.action_screenshot.triggered.connect(self.save_screenshot)
        file_menu.addAction(self.action_screenshot)

        file_menu.addSeparator()

        self.action_clear = QAction("清空场景(&C)", self)
        self.action_clear.triggered.connect(self.clear_scene)
        file_menu.addAction(self.action_clear)

        self.action_reload_damage_tree = QAction("刷新损伤树(&D)", self)
        self.action_reload_damage_tree.setShortcut(QKeySequence("Ctrl+Shift+D"))
        self.action_reload_damage_tree.triggered.connect(self._reload_damage_tree)
        file_menu.addAction(self.action_reload_damage_tree)

        file_menu.addSeparator()

        action_exit = QAction("退出(&X)", self)
        action_exit.setShortcut(QKeySequence("Alt+F4"))
        action_exit.triggered.connect(self.close)
        file_menu.addAction(action_exit)

        # === 视图菜单 ===
        view_menu = menubar.addMenu("视图(&V)")

        self.action_front = QAction("前视图", self)
        self.action_front.setShortcut(QKeySequence("1"))
        self.action_front.triggered.connect(self.vtk_widget.set_view_front)
        view_menu.addAction(self.action_front)

        self.action_top = QAction("俯视图", self)
        self.action_top.setShortcut(QKeySequence("2"))
        self.action_top.triggered.connect(self.vtk_widget.set_view_top)
        view_menu.addAction(self.action_top)

        self.action_right = QAction("右视图", self)
        self.action_right.setShortcut(QKeySequence("3"))
        self.action_right.triggered.connect(self.vtk_widget.set_view_right)
        view_menu.addAction(self.action_right)

        self.action_iso = QAction("等轴测视图", self)
        self.action_iso.setShortcut(QKeySequence("0"))
        self.action_iso.triggered.connect(self.vtk_widget.set_view_iso)
        view_menu.addAction(self.action_iso)

        view_menu.addSeparator()

        self.action_reset = QAction("重置视角(&R)", self)
        self.action_reset.setShortcut(QKeySequence("R"))
        self.action_reset.triggered.connect(self.vtk_widget.reset_camera)
        view_menu.addAction(self.action_reset)

        view_menu.addSeparator()

        self.action_wireframe = QAction("线框模式(&W)", self)
        self.action_wireframe.setShortcut(QKeySequence("W"))
        self.action_wireframe.setCheckable(True)
        self.action_wireframe.toggled.connect(self._toggle_wireframe)
        view_menu.addAction(self.action_wireframe)

        self.action_water = QAction("显示水面", self)
        self.action_water.setCheckable(True)
        self.action_water.setChecked(True)
        self.action_water.toggled.connect(self.vtk_widget.set_water_surface_visible)
        view_menu.addAction(self.action_water)

        self.action_camera_debug = QAction("显示相机调试框", self)
        self.action_camera_debug.setCheckable(True)
        self.action_camera_debug.setChecked(False)
        self.action_camera_debug.toggled.connect(self._set_camera_debug_overlay_visible)
        view_menu.addAction(self.action_camera_debug)

        # === 窗口菜单 ===
        window_menu = menubar.addMenu("窗口(&W)")
        self.action_show_tree = QAction("损伤树", self)
        self.action_show_tree.setCheckable(True)
        self.action_show_tree.setChecked(True)
        window_menu.addAction(self.action_show_tree)

        self.action_show_props = QAction("信息展示", self)
        self.action_show_props.setCheckable(True)
        self.action_show_props.setChecked(True)
        window_menu.addAction(self.action_show_props)

        # === 帮助菜单 ===
        help_menu = menubar.addMenu("帮助(&H)")
        action_about = QAction("关于(&A)...", self)
        action_about.triggered.connect(self._show_about)
        help_menu.addAction(action_about)

        menubar.hide()

    def _create_toolbar(self):
        """创建工具栏"""
        toolbar = QToolBar("主工具栏")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(20, 20))
        self.addToolBar(toolbar)

        # 打开文件
        btn_open = QAction("📂 打开", self)
        btn_open.setToolTip("打开模型文件 (Ctrl+O)")
        btn_open.triggered.connect(self.open_file)
        toolbar.addAction(btn_open)

        toolbar.addSeparator()

        # 视图按钮
        btn_front = QAction("⬜ 前视", self)
        btn_front.setToolTip("前视图 (1)")
        btn_front.triggered.connect(self.vtk_widget.set_view_front)
        toolbar.addAction(btn_front)

        btn_top = QAction("⬆ 俯视", self)
        btn_top.setToolTip("俯视图 (2)")
        btn_top.triggered.connect(self.vtk_widget.set_view_top)
        toolbar.addAction(btn_top)

        btn_right = QAction("➡ 右视", self)
        btn_right.setToolTip("右视图 (3)")
        btn_right.triggered.connect(self.vtk_widget.set_view_right)
        toolbar.addAction(btn_right)

        btn_iso = QAction("🔲 等轴测", self)
        btn_iso.setToolTip("等轴测视图 (0)")
        btn_iso.triggered.connect(self.vtk_widget.set_view_iso)
        toolbar.addAction(btn_iso)

        toolbar.addSeparator()

        btn_reset = QAction("🔄 重置", self)
        btn_reset.setToolTip("重置视角 (R)")
        btn_reset.triggered.connect(self.vtk_widget.reset_camera)
        toolbar.addAction(btn_reset)

        # 线框模式
        self.btn_wireframe = QAction("🔳 线框", self)
        self.btn_wireframe.setCheckable(True)
        self.btn_wireframe.setToolTip("线框模式 (W)")
        self.btn_wireframe.toggled.connect(self._toggle_wireframe)
        toolbar.addAction(self.btn_wireframe)

        self.btn_camera_debug = QAction("🧭 调试", self)
        self.btn_camera_debug.setCheckable(True)
        self.btn_camera_debug.setToolTip("显示相机 direction / view_up 调试框")
        self.btn_camera_debug.toggled.connect(self._set_camera_debug_overlay_visible)
        toolbar.addAction(self.btn_camera_debug)

        toolbar.addSeparator()

        # 截图
        btn_screenshot = QAction("📷 截图", self)
        btn_screenshot.setToolTip("截图保存 (Ctrl+Shift+S)")
        btn_screenshot.triggered.connect(self.save_screenshot)
        toolbar.addAction(btn_screenshot)

        # 清空
        btn_clear = QAction("🗑 清空", self)
        btn_clear.setToolTip("清空场景")
        btn_clear.triggered.connect(self.clear_scene)
        toolbar.addAction(btn_clear)

    def _create_model_tree_dock(self):
        """创建损伤树停靠面板"""
        self.tree_dock = QDockWidget("损伤树", self)
        self.tree_dock.setMinimumWidth(220)
        self.tree_dock.setFeatures(
            QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetClosable
        )

        # 树控件
        tree_container = QWidget()
        tree_layout = QVBoxLayout(tree_container)
        tree_layout.setContentsMargins(4, 4, 4, 4)
        tree_layout.setSpacing(4)

        self.model_tree = QTreeWidget()
        self.model_tree.setHeaderLabels(["损伤节点"])
        self.model_tree.setAlternatingRowColors(False)
        self.model_tree.setAnimated(True)
        self.model_tree.setIndentation(16)
        self.model_tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.model_tree.setAllColumnsShowFocus(True)
        # Keep selection color continuous across branch/text areas and hide dark branch lines.
        self.model_tree.setStyleSheet(
            """
            QTreeView { show-decoration-selected: 1; }
            QTreeView::branch:has-siblings:!adjoins-item { border-image: none; }
            QTreeView::branch:has-siblings:adjoins-item { border-image: none; }
            QTreeView::branch:!has-children:!has-siblings:adjoins-item { border-image: none; }
            QTreeWidget::item { margin: 0px; border-radius: 0px; }
            """
        )
        self.model_tree.itemClicked.connect(self._on_tree_item_clicked)
        self.model_tree.setColumnWidth(0, 220)
        tree_layout.addWidget(self.model_tree)

        self.tree_dock.setWidget(tree_container)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.tree_dock)

        # 绑定窗口菜单
        self.action_show_tree.toggled.connect(self.tree_dock.setVisible)
        self.tree_dock.visibilityChanged.connect(self.action_show_tree.setChecked)

    def _create_properties_dock(self):
        """创建信息展示停靠窗口"""
        self.props_dock = QDockWidget("信息展示", self)
        self.props_dock.setMinimumWidth(220)
        self.props_dock.setFeatures(
            QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetClosable
        )

        props_container = QWidget()
        props_container.setMinimumWidth(0)
        props_layout = QVBoxLayout(props_container)
        props_layout.setContentsMargins(0, 0, 0, 0)
        props_layout.setSpacing(0)

        # These widgets keep the selection state for existing update logic, but are not
        # shown because the right dock is dedicated to the device detail view.
        self.selection_title_label = QLabel("未选择对象或损伤节点")
        self.selection_title_label.setObjectName("accentLabel")
        self.selection_title_label.setWordWrap(True)
        self.selection_meta_label = QLabel(
            "请选择模型中的 object，或点击左侧损伤树节点。"
        )
        self.selection_meta_label.setWordWrap(True)

        self.catalog_table = QTableWidget()
        self.catalog_table.setColumnCount(3)
        self.catalog_table.setHorizontalHeaderLabels(["名称", "匹配ID", "详情"])
        self.catalog_table.horizontalHeader().setStretchLastSection(True)
        self.catalog_table.verticalHeader().setVisible(False)
        self.catalog_table.setAlternatingRowColors(True)
        self.catalog_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.catalog_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.catalog_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.catalog_table.setMaximumHeight(140)
        self.catalog_table.cellClicked.connect(self._on_catalog_row_clicked)

        self.document_status_label = QLabel("未选择设备")
        self.document_status_label.setWordWrap(True)
        self.open_document_button = QPushButton("打开文档")
        self.open_document_button.setEnabled(False)
        self.open_document_button.clicked.connect(self._open_current_document)

        self.document_view = QStackedWidget()
        self.document_view.setMinimumWidth(0)
        self.document_message_view = QLabel("未选择设备")
        self.document_message_view.setMinimumWidth(0)
        self.document_message_view.setAlignment(Qt.AlignCenter)
        self.document_message_view.setWordWrap(True)
        self.document_view.addWidget(self.document_message_view)

        self.pdf_document = None
        self.pdf_view = None
        self.web_document_view = None
        self._create_device_detail_view()

        self.document_view.setMinimumHeight(260)
        props_layout.addWidget(self.document_view, 1)

        self.props_dock.setWidget(props_container)
        self.addDockWidget(Qt.RightDockWidgetArea, self.props_dock)

        # 绑定窗口菜单
        self.action_show_props.toggled.connect(self.props_dock.setVisible)
        self.props_dock.visibilityChanged.connect(self.action_show_props.setChecked)

    def _create_status_bar(self):
        """创建状态栏"""
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

        self.status_label = QLabel("就绪")
        self.status_bar.addWidget(self.status_label, 1)

        self.highlight_debug_label = QLabel("实际高亮: 无")
        self.status_bar.addPermanentWidget(self.highlight_debug_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(200)
        self.progress_bar.setVisible(False)
        self.status_bar.addPermanentWidget(self.progress_bar)

        self.vertex_label = QLabel("")
        self.status_bar.addPermanentWidget(self.vertex_label)

    def _init_vtk(self):
        """延迟初始化VTK"""
        self.vtk_widget.initialize()
        if os.path.exists(self._viewport_background_path):
            self.vtk_widget.set_background_image(self._viewport_background_path)
        self.vtk_widget.set_water_surface(
            self._water_texture_path
            if os.path.exists(self._water_texture_path)
            else ""
        )

    def _reload_damage_tree(self):
        self._load_pdf_catalog_from_csv()
        self._load_device_catalog_from_json()
        self._load_damage_tree_from_csv()

    def _create_device_detail_view(self):
        detail_scroll = QScrollArea()
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QFrame.NoFrame)
        detail_scroll.setMinimumWidth(0)

        detail_widget = QWidget()
        detail_layout = QVBoxLayout(detail_widget)
        detail_layout.setContentsMargins(18, 18, 18, 18)
        detail_layout.setSpacing(14)

        self.device_title_label = QLabel("未选择设备")
        self.device_title_label.setObjectName("accentLabel")
        self.device_title_label.setWordWrap(True)
        detail_layout.addWidget(self.device_title_label)

        self.device_image_label = QLabel("未配置设备图片")
        self.device_image_label.setAlignment(Qt.AlignCenter)
        self.device_image_label.setMinimumHeight(180)
        self.device_image_label.setFrameShape(QFrame.StyledPanel)
        self.device_image_label.setWordWrap(True)
        detail_layout.addWidget(self.device_image_label)

        detail_form = QFormLayout()
        detail_form.setLabelAlignment(Qt.AlignRight | Qt.AlignTop)
        detail_form.setFormAlignment(Qt.AlignTop)
        detail_form.setHorizontalSpacing(12)
        detail_form.setVerticalSpacing(10)

        self.device_field_labels = {}
        for field_key, field_name in (
            ("material", "材质"),
            ("thickness", "厚度"),
            ("size", "尺寸"),
            ("function", "功能"),
        ):
            value_label = QLabel("待补充")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.device_field_labels[field_key] = value_label
            detail_form.addRow(f"{field_name}:", value_label)

        detail_layout.addLayout(detail_form)
        detail_layout.addStretch(1)

        detail_scroll.setWidget(detail_widget)
        self.device_detail_view = detail_scroll
        self.document_view.addWidget(self.device_detail_view)

    def _configure_document_view(self, document_view):
        if QWebEngineSettings is None:
            return

        settings = document_view.settings()
        web_attribute = getattr(QWebEngineSettings, "WebAttribute", QWebEngineSettings)
        for setting_name in (
            "PdfViewerEnabled",
            "LocalContentCanAccessFileUrls",
            "LocalContentCanAccessRemoteUrls",
        ):
            attr = getattr(web_attribute, setting_name, None)
            if attr is None:
                attr = getattr(QWebEngineSettings, setting_name, None)
            if attr is not None:
                settings.setAttribute(attr, True)

    def _load_pdf_catalog_from_csv(self, csv_path=None):
        csv_path = csv_path or self._pdf_catalog_csv_path
        self._pdf_catalog_rows = []
        self._pdf_catalog_by_model_name.clear()
        self._pdf_catalog_by_damage_leaf_id.clear()

        if not os.path.exists(csv_path):
            self._set_document_message(
                f"未找到文档目录文件: {os.path.basename(csv_path)}"
            )
            return

        try:
            rows = self._read_pdf_catalog_rows(csv_path)
        except Exception as exc:
            self._set_document_message(f"读取文档目录失败: {exc}")
            return

        self._pdf_catalog_rows = rows
        for row in rows:
            model_name = row["model_name"]
            damage_leaf_id = self._normalize_damage_id(row["damage_leaf_id"])
            if model_name:
                self._pdf_catalog_by_model_name[model_name].append(row)
            if damage_leaf_id:
                self._pdf_catalog_by_damage_leaf_id[damage_leaf_id].append(row)

        if not rows:
            self._set_document_message("文档目录为空。")

    def _read_pdf_catalog_rows(self, csv_path):
        last_error = None
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                with open(csv_path, "r", encoding=encoding, newline="") as f:
                    reader = csv.reader(f)
                    next(reader, None)  # header
                    rows = []
                    for idx, row in enumerate(reader, start=2):
                        if not row or not any(col.strip() for col in row):
                            continue

                        cols = list(row) + [""] * (4 - len(row))
                        model_name = cols[0].strip()
                        display_name = cols[1].strip()
                        document_path = cols[2].strip()
                        damage_leaf_id = cols[3].strip()
                        if not any(
                            (model_name, display_name, document_path, damage_leaf_id)
                        ):
                            continue
                        rows.append(
                            {
                                "model_name": model_name,
                                "display_name": display_name
                                or model_name
                                or damage_leaf_id,
                                "document_path": document_path,
                                "damage_leaf_id": damage_leaf_id,
                                "row_number": idx,
                            }
                        )
                    return rows
            except UnicodeDecodeError as exc:
                last_error = exc
                continue

        if last_error:
            raise last_error
        return []

    def _load_device_catalog_from_json(self, json_path=None):
        json_path = json_path or self._device_catalog_json_path
        self._device_catalog_rows = []
        self._device_catalog_by_model_name.clear()
        self._device_catalog_by_damage_leaf_id.clear()

        if not os.path.exists(json_path):
            return

        try:
            rows = self._read_device_catalog_rows(json_path)
        except Exception as exc:
            self._set_document_message(f"读取设备详情失败: {exc}")
            return

        self._device_catalog_rows = rows
        for row in rows:
            model_name = row["model_name"]
            damage_leaf_id = self._normalize_damage_id(row["damage_leaf_id"])
            if model_name:
                self._device_catalog_by_model_name[model_name].append(row)
            if damage_leaf_id:
                self._device_catalog_by_damage_leaf_id[damage_leaf_id].append(row)

    def _read_device_catalog_rows(self, json_path):
        with open(json_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)

        if isinstance(data, dict):
            devices = data.get("devices", [])
        else:
            devices = data
        if not isinstance(devices, list):
            raise ValueError("设备详情 JSON 必须是数组，或包含 devices 数组。")

        rows = []
        for idx, item in enumerate(devices, start=1):
            if not isinstance(item, dict):
                continue
            model_name = str(item.get("model_name", "")).strip()
            display_name = str(item.get("display_name", "")).strip()
            damage_leaf_id = str(item.get("damage_leaf_id", "")).strip()
            if not any((model_name, display_name, damage_leaf_id)):
                continue

            rows.append(
                {
                    "model_name": model_name,
                    "display_name": display_name or model_name or damage_leaf_id,
                    "damage_leaf_id": damage_leaf_id,
                    "image_path": str(item.get("image_path", "")).strip(),
                    "material": str(item.get("material", "")).strip(),
                    "thickness": str(item.get("thickness", "")).strip(),
                    "size": str(item.get("size", "")).strip(),
                    "function": str(item.get("function", "")).strip(),
                    "row_number": idx,
                }
            )
        return rows

    def _resolve_document_path(self, document_path):
        raw_path = (document_path or "").strip().strip('"')
        if not raw_path:
            return ""

        if re.match(r"^[A-Za-z]:[\\/]", raw_path):
            return os.path.normpath(raw_path)

        if raw_path.startswith(("/", "\\")):
            raw_path = raw_path.lstrip("/\\")

        if os.path.isabs(raw_path):
            return os.path.normpath(raw_path)

        candidates = []
        if not os.path.dirname(raw_path):
            candidates.append(os.path.join(self._assets_root, "pdfs", raw_path))

        candidates.append(os.path.join(self._project_root, raw_path))
        candidates.append(
            os.path.join(self._assets_root, "pdfs", os.path.basename(raw_path))
        )
        candidates.append(
            os.path.join(self._project_root, "pdfs", os.path.basename(raw_path))
        )

        for candidate in candidates:
            candidate = os.path.normpath(candidate)
            if os.path.exists(candidate):
                return candidate

        return os.path.normpath(candidates[0] if candidates else raw_path)

    def _resolve_device_image_path(self, image_path):
        raw_path = (image_path or "").strip().strip('"')
        if not raw_path:
            return ""

        if re.match(r"^[A-Za-z]:[\\/]", raw_path):
            return os.path.normpath(raw_path)

        if raw_path.startswith(("/", "\\")):
            raw_path = raw_path.lstrip("/\\")

        if os.path.isabs(raw_path):
            return os.path.normpath(raw_path)

        candidates = [
            os.path.join(self._assets_root, raw_path),
            os.path.join(self._project_root, raw_path),
            os.path.join(self._assets_root, "images", "devices", os.path.basename(raw_path)),
        ]
        for candidate in candidates:
            candidate = os.path.normpath(candidate)
            if os.path.exists(candidate):
                return candidate
        return os.path.normpath(candidates[0])

    def _load_damage_tree_from_csv(self, csv_path=None):
        csv_path = csv_path or self._damage_tree_csv_path
        if not os.path.exists(csv_path):
            self.model_tree.clear()
            self.status_label.setText(f"未找到损伤树文件: {os.path.basename(csv_path)}")
            return

        try:
            rows = self._read_damage_tree_rows(csv_path)
        except Exception as exc:
            QMessageBox.critical(self, "损伤树错误", f"读取损伤树失败: {exc}")
            return

        nodes_by_id = {}
        duplicate_ids = []
        for row in rows:
            node_id = self._normalize_damage_id(row["node_id"])
            if node_id in nodes_by_id:
                duplicate_ids.append(node_id)
                continue
            row["node_id"] = node_id
            row["parent_id"] = self._normalize_damage_id(row["parent_id"])
            nodes_by_id[node_id] = row

        children_map = defaultdict(list)
        root_nodes = []
        for node in nodes_by_id.values():
            parent_id = node["parent_id"]
            if parent_id and parent_id in nodes_by_id:
                children_map[parent_id].append(node)
            else:
                root_nodes.append(node)

        def sort_key(node):
            return (node["order"], node["name"], node["node_id"])

        for child_list in children_map.values():
            child_list.sort(key=sort_key)
        root_nodes.sort(key=sort_key)
        self._damage_nodes_by_id = dict(nodes_by_id)
        self._damage_children_by_id = defaultdict(
            list, {key: list(value) for key, value in children_map.items()}
        )
        self._damage_node_ids = set(nodes_by_id)

        self.model_tree.blockSignals(True)
        self.model_tree.clear()
        for root in root_nodes:
            self._create_damage_tree_item(
                node=root,
                parent_item=None,
                nodes_by_id=nodes_by_id,
                children_map=children_map,
                level=0,
                lineage=(),
            )
        self._expand_damage_tree_to_level(max_expand_level=0)
        self.model_tree.blockSignals(False)

        msg = f"损伤树已加载: {len(nodes_by_id)} 个节点"
        if duplicate_ids:
            msg += f"（忽略重复ID: {len(duplicate_ids)}）"
        self.status_label.setText(msg)

    def _load_damage_camera_poses(self, json_path=None):
        json_path = json_path or self._damage_camera_poses_path
        self._damage_camera_poses_by_id = {}
        if not os.path.exists(json_path):
            return

        try:
            with open(json_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception as exc:
            self.status_label.setText(f"读取损伤相机配置失败: {exc}")
            return

        for pose in data.get("poses") or []:
            node_id = self._normalize_damage_id(pose.get("node_id"))
            if not node_id:
                continue
            if self._is_valid_damage_camera_pose(pose):
                self._damage_camera_poses_by_id[node_id] = pose

    def _is_valid_damage_camera_pose(self, pose):
        if not isinstance(pose, dict):
            return False
        focal_point = pose.get("focal_point")
        position = pose.get("position")
        direction = pose.get("direction")
        if not self._is_numeric_vector(focal_point):
            return False
        return self._is_numeric_vector(position) or self._is_numeric_vector(direction)

    def _is_numeric_vector(self, vector, size=3):
        if not isinstance(vector, (list, tuple)) or len(vector) != size:
            return False
        try:
            for value in vector:
                float(value)
        except (TypeError, ValueError):
            return False
        return True

    def _read_damage_tree_rows(self, csv_path):
        last_error = None
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                with open(csv_path, "r", encoding=encoding, newline="") as f:
                    reader = csv.reader(f)
                    next(reader, None)  # header
                    rows = []
                    for idx, row in enumerate(reader, start=2):
                        if not row or not any(col.strip() for col in row):
                            continue

                        cols = list(row) + [""] * (4 - len(row))
                        node_id = cols[0].strip()
                        parent_id = cols[1].strip()
                        name = cols[2].strip() or node_id
                        order_raw = cols[3].strip()

                        if not node_id:
                            continue

                        try:
                            order = int(order_raw)
                        except (TypeError, ValueError):
                            order = idx

                        rows.append(
                            {
                                "node_id": node_id,
                                "parent_id": parent_id,
                                "name": name,
                                "order": order,
                            }
                        )
                    return rows
            except UnicodeDecodeError as exc:
                last_error = exc
                continue

        if last_error:
            raise last_error
        return []

    def _create_damage_tree_item(
        self, node, parent_item, nodes_by_id, children_map, level, lineage
    ):
        item = QTreeWidgetItem(parent_item or self.model_tree)
        node_id = node["node_id"]
        parent_id = node["parent_id"]
        name = node["name"]

        children = children_map.get(node_id, [])
        has_parent = bool(parent_id and parent_id in nodes_by_id)
        has_children = bool(children)

        item.setText(0, name)
        item.setData(0, self.DAMAGE_NODE_ID_ROLE, node_id)
        item.setData(0, self.DAMAGE_PARENT_ID_ROLE, parent_id)
        item.setData(0, self.DAMAGE_LEVEL_ROLE, level)
        if has_children:
            item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
            font = QFont()
            font.setBold(True)
            font.setPointSize(11 if level == 0 else 10)
            item.setFont(0, font)

        parent_name = nodes_by_id[parent_id]["name"] if has_parent else "无"
        path_text = " / ".join((*lineage, name))
        tip = (
            f"节点ID: {node_id}\n"
            f"上级: {parent_name}\n"
            f"下级数量: {len(children)}\n"
            f"路径: {path_text}"
        )
        item.setToolTip(0, tip)

        for child in children:
            self._create_damage_tree_item(
                node=child,
                parent_item=item,
                nodes_by_id=nodes_by_id,
                children_map=children_map,
                level=level + 1,
                lineage=(*lineage, name),
            )

    def _expand_damage_tree_to_level(self, max_expand_level=1):
        for i in range(self.model_tree.topLevelItemCount()):
            root = self.model_tree.topLevelItem(i)
            self._set_tree_expand_state(root, 0, max_expand_level)

    def _set_tree_expand_state(self, item, level, max_expand_level):
        item.setExpanded(level <= max_expand_level and item.childCount() > 0)
        for idx in range(item.childCount()):
            self._set_tree_expand_state(item.child(idx), level + 1, max_expand_level)

    # ============================================================
    # 操作方法
    # ============================================================

    def open_file(self):
        """打开文件对话框"""
        filepath, _ = QFileDialog.getOpenFileName(
            self, "打开模型文件", "", get_supported_formats()
        )
        if filepath:
            self._load_model(filepath)

    def _load_model(self, filepath):
        """加载模型文件"""
        if self._load_thread and self._load_thread.isRunning():
            QMessageBox.warning(self, "提示", "正在加载模型，请等待完成。")
            return

        self._current_file = filepath
        self.status_label.setText(f"正在加载: {os.path.basename(filepath)}")
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)

        # 更新文件信息
        self._update_file_info(filepath)

        ext = os.path.splitext(filepath)[1].lower()
        if ext in (".glb", ".gltf"):
            self._load_gltf_with_importer(filepath)
            return

        self._start_model_load_thread(filepath)

    def _start_model_load_thread(self, filepath, status_text=None):
        """启动后台加载线程，避免模型解析阻塞 UI 事件循环。"""
        self._gltf_importer = None
        if status_text:
            self.status_label.setText(status_text)
        self._load_thread = ModelLoadThread(filepath)
        self._load_thread.progress.connect(self._on_load_progress)
        self._load_thread.finished.connect(self._on_load_finished)
        self._load_thread.error.connect(self._on_load_error)
        self._load_thread.start()

    def _should_load_gltf_in_thread(self, filepath):
        """大 glTF/GLB 使用 reader 线程路径，避免 importer 在主线程长时间阻塞。"""
        return (
            self._gltf_asset_size_bytes(filepath)
            >= self.GLTF_THREADED_LOAD_THRESHOLD_BYTES
        )

    def _gltf_asset_size_bytes(self, filepath):
        try:
            total = os.path.getsize(filepath)
        except OSError:
            return 0

        if os.path.splitext(filepath)[1].lower() != ".gltf":
            return total

        try:
            gltf = self._read_gltf_json(filepath)
        except Exception:
            return total

        base_dir = os.path.dirname(filepath)
        for buffer_info in gltf.get("buffers") or []:
            uri = str(buffer_info.get("uri") or "")
            if not uri or uri.startswith("data:") or "://" in uri:
                continue
            buffer_path = os.path.normpath(os.path.join(base_dir, uri))
            try:
                total += os.path.getsize(buffer_path)
            except OSError:
                continue
        return total

    def _load_gltf_with_importer(self, filepath):
        """用 vtkGLTFImporter 加载，保留完整材质/贴图。"""
        try:
            self.progress_bar.setValue(20)
            self.status_label.setText("正在导入 glTF 场景（保留材质）...")

            self.vtk_widget.clear_scene()
            self._loaded_items = []
            self._actor_catalog_names.clear()
            self._actor_display_names.clear()

            importer = vtk.vtkGLTFImporter()
            importer.SetRenderWindow(self.vtk_widget.render_window)
            importer.SetFileName(filepath)
            importer.Update()
            self._gltf_importer = importer

            self.progress_bar.setValue(60)
            self.status_label.setText("正在优化贴图质量...")

            gltf_name_entries = self._read_gltf_renderable_name_entries(
                filepath,
                attach_reader_bounds=False,
            )
            imported = importer.GetImportedActors()
            imported.InitTraversal()
            imported_count = imported.GetNumberOfItems()
            texture_max_size = 512 if imported_count > 1500 else 1024
            total_verts = 0
            total_faces = 0
            object_count = 0
            textured_count = 0
            actor_index = 0
            imported_actors = []

            while True:
                actor = imported.GetNextActor()
                if actor is None:
                    break

                has_texture = self._actor_has_any_texture(actor)
                if has_texture:
                    textured_count += 1
                self._optimize_actor_texture(actor, max_texture_size=texture_max_size)
                self._optimize_actor_material_for_speed(actor, has_texture=has_texture)
                points, cells = self._get_actor_geometry_stats(actor)
                imported_actors.append(
                    (
                        actor,
                        points,
                        cells,
                        self._vtk_object_name(actor),
                        actor.GetBounds(),
                    )
                )

            display_names = self._match_gltf_display_names(
                gltf_name_entries,
                [
                    (points, cells, bounds)
                    for _actor, points, cells, _actor_object_name, bounds in imported_actors
                ],
            )

            for actor, points, cells, actor_object_name, _bounds in imported_actors:
                actor_name = f"actor_{actor_index}"
                matched_name = (
                    display_names[actor_index]
                    if actor_index < len(display_names)
                    else ""
                )
                original_name = self._original_display_name(
                    actor_object_name or matched_name,
                    actor_name,
                )
                catalog_name = self._best_catalog_name(
                    original_name,
                    matched_name,
                )
                self.vtk_widget.add_actor(actor, actor_name, add_to_renderer=False)
                self._actor_catalog_names[actor_name] = catalog_name
                self._actor_display_names[actor_name] = original_name

                total_verts += points
                total_faces += cells
                object_count += 1
                self._loaded_items.append(
                    {
                        "actor": actor,
                        "name": original_name,
                        "type": "Mesh",
                        "points": points,
                        "cells": cells,
                    }
                )
                actor_index += 1

            scene_bounds = self.vtk_widget.get_scene_model_bounds()
            self.vtk_widget.update_water_surface_to_scene(
                render=False, scene_bounds=scene_bounds
            )
            self.vtk_widget.reset_camera(scene_bounds=scene_bounds)
            self.progress_bar.setVisible(False)

            if object_count == 0:
                self.status_label.setText("加载失败")
                QMessageBox.warning(self, "提示", "未从 glTF 中导入到可渲染对象。")
                return

            if textured_count == 0:
                self.status_label.setText(
                    f"✓ 成功加载 {object_count} 个对象（该模型不含贴图）"
                )
            else:
                self.status_label.setText(
                    f"✓ 成功加载 {object_count} 个对象（贴图对象: {textured_count}，贴图上限: {texture_max_size}px）"
                )
            self.vertex_label.setText(
                f"顶点: {total_verts:,}  |  面片: {total_faces:,}  |  对象: {object_count}"
            )
            self._update_geo_info_all(total_verts, total_faces, object_count)
        except Exception as e:
            self.progress_bar.setVisible(False)
            self.status_label.setText("加载失败")
            QMessageBox.critical(self, "加载错误", f"贴图加载失败: {str(e)}")

    def _read_gltf_renderable_name_entries(self, filepath, attach_reader_bounds=True):
        try:
            gltf = self._read_gltf_json(filepath)
        except Exception:
            return {"primitive": [], "node": []}

        nodes = gltf.get("nodes") or []
        meshes = gltf.get("meshes") or []
        accessors = gltf.get("accessors") or []
        if not nodes:
            return {"primitive": [], "node": []}

        primitive_entries = []
        node_entries = []
        visited = set()

        def node_display_name(node, node_idx):
            node_name = str(node.get("name") or "").strip()
            mesh_idx = node.get("mesh")
            mesh_name = ""
            if isinstance(mesh_idx, int) and 0 <= mesh_idx < len(meshes):
                mesh_name = str(meshes[mesh_idx].get("name") or "").strip()
            return node_name or mesh_name or f"Node_{node_idx}"

        def node_primitives(node):
            mesh_idx = node.get("mesh")
            if not isinstance(mesh_idx, int) or mesh_idx < 0 or mesh_idx >= len(meshes):
                return []
            return meshes[mesh_idx].get("primitives") or []

        def accessor_count(accessor_idx):
            if (
                not isinstance(accessor_idx, int)
                or accessor_idx < 0
                or accessor_idx >= len(accessors)
            ):
                return 0
            return int(accessors[accessor_idx].get("count") or 0)

        def primitive_geometry(primitive):
            attributes = primitive.get("attributes") or {}
            points = accessor_count(attributes.get("POSITION"))
            index_count = accessor_count(primitive.get("indices"))
            draw_count = index_count or points
            mode = int(primitive.get("mode", 4))
            if mode == 4:  # TRIANGLES
                cells = draw_count // 3
            elif mode in (5, 6):  # TRIANGLE_STRIP / TRIANGLE_FAN
                cells = max(0, draw_count - 2)
            elif mode == 1:  # LINES
                cells = draw_count // 2
            elif mode == 3:  # LINE_STRIP
                cells = max(0, draw_count - 1)
            else:
                cells = draw_count
            return points, cells

        def visit(node_idx):
            if not isinstance(node_idx, int) or node_idx < 0 or node_idx >= len(nodes):
                return
            if node_idx in visited:
                return
            visited.add(node_idx)

            node = nodes[node_idx]
            primitives = node_primitives(node)
            if primitives:
                display_name = node_display_name(node, node_idx)
                node_points = 0
                node_cells = 0
                for primitive in primitives:
                    points, cells = primitive_geometry(primitive)
                    node_points += points
                    node_cells += cells
                    primitive_entries.append(
                        {
                            "name": display_name,
                            "points": points,
                            "cells": cells,
                        }
                    )
                node_entries.append(
                    {
                        "name": display_name,
                        "points": node_points,
                        "cells": node_cells,
                    }
                )

            for child_idx in node.get("children") or []:
                visit(child_idx)

        scene_indices = []
        scenes = gltf.get("scenes") or []
        scene_idx = gltf.get("scene")
        if isinstance(scene_idx, int) and 0 <= scene_idx < len(scenes):
            scene_indices = scenes[scene_idx].get("nodes") or []
        elif scenes:
            for scene in scenes:
                scene_indices.extend(scene.get("nodes") or [])

        if scene_indices:
            for node_idx in scene_indices:
                visit(node_idx)

        for node_idx, node in enumerate(nodes):
            if node_idx not in visited and node_primitives(node):
                visit(node_idx)

        name_entries = {"primitive": primitive_entries, "node": node_entries}
        if attach_reader_bounds:
            self._attach_gltf_reader_bounds(filepath, name_entries)
        return name_entries

    def _apply_gltf_names_to_threaded_items(self, actors):
        """Restore glTF node names for the threaded reader path used by large files."""
        if not self._current_file:
            return
        if os.path.splitext(self._current_file)[1].lower() not in (".glb", ".gltf"):
            return

        name_entries = self._read_gltf_renderable_name_entries(
            self._current_file,
            attach_reader_bounds=False,
        )
        if not name_entries.get("node") and not name_entries.get("primitive"):
            return

        sub_items = [
            sub_item
            for item_data in actors
            for sub_item in (item_data.get("sub_items") or [])
        ]
        if sub_items:
            display_names = self._match_gltf_display_names(
                name_entries,
                [
                    (sub.get("points", 0), sub.get("cells", 0))
                    for sub in sub_items
                ],
            )
            for sub_item, display_name in zip(sub_items, display_names):
                if display_name:
                    sub_item["name"] = display_name
            return

        display_names = self._match_gltf_display_names(
            name_entries,
            [
                (item_data.get("points", 0), item_data.get("cells", 0))
                for item_data in actors
            ],
        )
        for item_data, display_name in zip(actors, display_names):
            if display_name:
                item_data["name"] = display_name

    def _attach_gltf_reader_bounds(self, filepath, name_entries):
        reader_entries = self._read_gltf_reader_geometry_entries(filepath)
        if not reader_entries:
            return

        for entry_key in ("node", "primitive"):
            entries = name_entries.get(entry_key) or []
            if len(entries) != len(reader_entries):
                continue
            for entry, reader_entry in zip(entries, reader_entries):
                if entry.get("points") == reader_entry.get("points") and entry.get(
                    "cells"
                ) == reader_entry.get("cells"):
                    entry["bounds"] = reader_entry.get("bounds")
            return

    def _read_gltf_reader_geometry_entries(self, filepath):
        try:
            reader = vtk.vtkGLTFReader()
            reader.SetFileName(filepath)
            reader.Update()
            data_obj = reader.GetOutput()
        except Exception:
            return []

        if data_obj is None or not data_obj.IsA("vtkCompositeDataSet"):
            return []

        entries = []
        iterator = data_obj.NewIterator()
        iterator.InitTraversal()
        while not iterator.IsDoneWithTraversal():
            current = iterator.GetCurrentDataObject()
            if current is not None and current.IsA("vtkPolyData"):
                entries.append(
                    {
                        "points": current.GetNumberOfPoints(),
                        "cells": current.GetNumberOfCells(),
                        "bounds": tuple(current.GetBounds()),
                    }
                )
            iterator.GoToNextItem()
        return entries

    def _match_gltf_display_names(self, name_entries, actor_stats):
        primitive_entries = list(name_entries.get("primitive") or [])
        node_entries = list(name_entries.get("node") or [])
        if not actor_stats:
            return []

        if len(node_entries) == len(actor_stats):
            ordered_entries = node_entries
        elif len(primitive_entries) == len(actor_stats):
            ordered_entries = primitive_entries
        else:
            ordered_entries = node_entries or primitive_entries

        match_entries = self._dedupe_gltf_match_entries(
            [*node_entries, *primitive_entries]
        )
        display_names = [None] * len(actor_stats)
        used_entry_indexes = set()

        for actor_idx, actor_stat in enumerate(actor_stats):
            points, cells, bounds = self._normalize_actor_match_stat(actor_stat)
            exact_indexes = [
                entry_idx
                for entry_idx, entry in enumerate(match_entries)
                if entry_idx not in used_entry_indexes
                and entry.get("points") == points
                and entry.get("cells") == cells
            ]
            entry_idx = self._best_gltf_match_entry_index(
                exact_indexes, match_entries, bounds
            )
            if entry_idx is not None:
                used_entry_indexes.add(entry_idx)
                display_names[actor_idx] = match_entries[entry_idx]["name"]

        for actor_idx, actor_stat in enumerate(actor_stats):
            if display_names[actor_idx]:
                continue
            points, cells, bounds = self._normalize_actor_match_stat(actor_stat)
            exact_indexes = [
                entry_idx
                for entry_idx, entry in enumerate(match_entries)
                if entry_idx not in used_entry_indexes
                and entry.get("points") == points
                and entry.get("cells") == cells
            ]
            entry_idx = self._best_gltf_match_entry_index(
                exact_indexes, match_entries, bounds
            )
            if entry_idx is not None:
                used_entry_indexes.add(entry_idx)
                display_names[actor_idx] = match_entries[entry_idx]["name"]

        for actor_idx, name in enumerate(display_names):
            if name:
                continue
            if actor_idx < len(ordered_entries):
                display_names[actor_idx] = ordered_entries[actor_idx]["name"]

        return [name or "" for name in display_names]

    def _normalize_actor_match_stat(self, actor_stat):
        if len(actor_stat) >= 3:
            return actor_stat[0], actor_stat[1], actor_stat[2]
        return actor_stat[0], actor_stat[1], None

    def _best_gltf_match_entry_index(self, exact_indexes, match_entries, actor_bounds):
        if not exact_indexes:
            return None
        if len(exact_indexes) == 1:
            return exact_indexes[0]
        if actor_bounds is None:
            return exact_indexes[0]

        scored = []
        for entry_idx in exact_indexes:
            entry_bounds = match_entries[entry_idx].get("bounds")
            if entry_bounds is None:
                continue
            scored.append(
                (self._bounds_center_distance(actor_bounds, entry_bounds), entry_idx)
            )
        if not scored:
            return exact_indexes[0]
        scored.sort(key=lambda item: item[0])
        return scored[0][1]

    def _bounds_center_distance(self, bounds_a, bounds_b):
        center_a = self._bounds_center(bounds_a)
        center_b = self._bounds_center(bounds_b)
        if center_a is None or center_b is None:
            return float("inf")
        return sum((a - b) ** 2 for a, b in zip(center_a, center_b))

    def _bounds_center(self, bounds):
        if bounds is None or len(bounds) < 6:
            return None
        return (
            0.5 * (float(bounds[0]) + float(bounds[1])),
            0.5 * (float(bounds[2]) + float(bounds[3])),
            0.5 * (float(bounds[4]) + float(bounds[5])),
        )

    def _dedupe_gltf_match_entries(self, entries):
        unique_entries = []
        seen = set()
        for entry in entries:
            key = (entry.get("name"), entry.get("points"), entry.get("cells"))
            if key in seen:
                continue
            unique_entries.append(entry)
            seen.add(key)
        return unique_entries

    def _vtk_object_name(self, actor):
        getter = getattr(actor, "GetObjectName", None)
        if not callable(getter):
            return ""
        try:
            return str(getter() or "").strip()
        except Exception:
            return ""

    def _read_gltf_json(self, filepath):
        ext = os.path.splitext(filepath)[1].lower()
        if ext == ".gltf":
            with open(filepath, "r", encoding="utf-8-sig") as f:
                return json.load(f)

        with open(filepath, "rb") as f:
            header = f.read(12)
            if len(header) != 12:
                raise ValueError("GLB header is incomplete")
            magic, _version, length = struct.unpack("<4sII", header)
            if magic != b"glTF":
                raise ValueError("Invalid GLB file")

            bytes_read = 12
            while bytes_read + 8 <= length:
                chunk_header = f.read(8)
                if len(chunk_header) != 8:
                    break
                chunk_length, chunk_type = struct.unpack("<II", chunk_header)
                chunk_data = f.read(chunk_length)
                bytes_read += 8 + chunk_length
                if chunk_type == 0x4E4F534A:
                    return json.loads(chunk_data.decode("utf-8").rstrip("\x00 \t\r\n"))

        raise ValueError("GLB JSON chunk not found")

    def _on_load_progress(self, percent, description):
        """加载进度回调"""
        self.progress_bar.setValue(percent)
        self.status_label.setText(description)

    def _on_load_finished(self, actors, message):
        """加载完成回调"""
        self.progress_bar.setVisible(False)

        # 清空旧场景
        self.vtk_widget.clear_scene()
        self._apply_gltf_names_to_threaded_items(actors)
        self._loaded_items = actors
        self._actor_catalog_names.clear()
        self._actor_display_names.clear()

        total_verts = 0
        total_faces = 0
        object_count = 0

        for i, item_data in enumerate(actors):
            actor = item_data["actor"]
            actor_name = f"actor_{i}"
            self.vtk_widget.add_actor(actor, actor_name)
            self._actor_catalog_names[actor_name] = self._best_catalog_name(
                item_data.get("name"),
            )
            self._actor_display_names[actor_name] = self._original_display_name(
                item_data.get("name"),
                actor_name,
            )

            sub_items = item_data.get("sub_items") or []
            if sub_items:
                object_count += len(sub_items)
                for sub_idx, sub in enumerate(sub_items):
                    sub_name = f"{actor_name}_block_{sub['flat_index']}"
                    self._actor_catalog_names[sub_name] = self._best_catalog_name(
                        sub.get("name"),
                    )
                    self._actor_display_names[sub_name] = self._original_display_name(
                        sub.get("name"),
                        sub_name,
                    )
                    self.vtk_widget.register_actor_alias(
                        alias_name=sub_name,
                        base_name=actor_name,
                        flat_block_index=sub["flat_index"],
                        points=sub.get("points", 0),
                        cells=sub.get("cells", 0),
                        base_color=sub.get("color"),
                        dataset_ptr=sub.get("dataset_ptr", ""),
                    )
            else:
                object_count += 1

            # 统计
            points = item_data.get("points")
            cells = item_data.get("cells")
            if points is None or cells is None:
                points, cells = self._get_actor_geometry_stats(actor)
            total_verts += points
            total_faces += cells

        self.vtk_widget.update_water_surface_to_scene(render=False)

        # 重置相机
        self.vtk_widget.reset_camera()

        # 更新状态
        self.status_label.setText(f"✓ {message}")
        self.vertex_label.setText(
            f"顶点: {total_verts:,}  |  面片: {total_faces:,}  |  对象: {object_count}"
        )

        # 更新几何信息表
        self._update_geo_info_all(total_verts, total_faces, object_count)

    def _on_load_error(self, error_msg):
        """加载错误回调"""
        self.progress_bar.setVisible(False)
        self.status_label.setText("加载失败")
        QMessageBox.critical(self, "加载错误", error_msg)

    def _on_vtk_model_clicked(self, actor_name):
        """VTK视口模型点击事件"""
        if not actor_name:
            # 点击空白处，清除高亮和选中
            self.vtk_widget.clear_selection_focus()
            self.vtk_widget.highlight_actor("")
            self._set_highlight_debug(self.vtk_widget.highlighted_names())
            self._update_geo_info_all(0, 0, self._get_loaded_object_count())
            self.selection_title_label.setText("未选择对象或损伤节点")
            self.selection_meta_label.setText(
                "请选择模型中的 object，或点击左侧损伤树节点。"
            )
            self.catalog_table.setRowCount(0)
            self._current_catalog_matches = []
            self._set_document_message("未选择设备")
            return

        # 高亮
        self.vtk_widget.clear_selection_focus()
        self.vtk_widget.highlight_actor(actor_name)
        self._set_highlight_debug(self.vtk_widget.highlighted_names())
        info = self.vtk_widget.get_actor_info(actor_name)
        if info:
            self._update_geo_info(info)

        catalog_key = self._catalog_key_for_actor(actor_name)
        display_name = self._display_name_for_actor(actor_name)
        meta_parts = [f"选择名称: {display_name}"]
        if info:
            meta_parts.extend(f"{key}: {value}" for key, value in info.items())
        matches = self._pdf_catalog_by_model_name.get(catalog_key, [])
        if catalog_key != display_name:
            meta_parts.append(f"匹配模型名称: {catalog_key}")
        self._show_catalog_matches(
            source_type="model",
            source_key=catalog_key,
            display_name=f"模型对象: {display_name}",
            matches=matches,
            meta="  |  ".join(meta_parts),
        )

    def _on_tree_item_clicked(self, item, column):
        """树节点点击事件"""
        node_id = self._normalize_damage_id(item.data(0, self.DAMAGE_NODE_ID_ROLE))
        if node_id:
            if item.childCount() > 0:
                item.setExpanded(not item.isExpanded())

            parent_id = item.data(0, self.DAMAGE_PARENT_ID_ROLE) or "无"
            is_hierarchy = item.childCount() > 0
            if is_hierarchy:
                damage_ids = self._descendant_leaf_node_ids(item)
                node_matches = self._node_catalog_rows(node_id)
                object_matches = self._object_catalog_rows_for_damage_ids(damage_ids)
                matches = self._damage_display_matches(node_matches, object_matches)
            else:
                object_matches = self._object_catalog_rows_for_damage_ids([node_id])
                matches = self._damage_display_matches([], object_matches)
            highlighted_names = self._selection_names_for_catalog_rows(object_matches)
            self.vtk_widget.highlight_actors(highlighted_names)
            self.vtk_widget.focus_selection(highlighted_names)
            self._set_highlight_debug(self.vtk_widget.highlighted_names())

            highlight_text = (
                f"  |  高亮对象: {len(highlighted_names)}" if highlighted_names else ""
            )
            self.status_label.setText(
                f"损伤节点: {item.text(0)}  |  节点ID: {node_id}  |  上级ID: {parent_id}  |  下级: {item.childCount()}{highlight_text}"
            )
            self._show_catalog_matches(
                source_type="damage",
                source_key=node_id,
                display_name=f"损伤节点: {item.text(0)}",
                matches=matches,
                meta=(
                    f"节点ID: {node_id}  |  上级ID: {parent_id}  |  下级: {item.childCount()}"
                    f"  |  类型: {'层级' if is_hierarchy else '毁伤叶子'}"
                ),
            )
            return

        actor_name = item.data(0, Qt.UserRole)
        if actor_name:
            self.vtk_widget.highlight_actor(actor_name)
            self.vtk_widget.focus_selection([actor_name])
            self._set_highlight_debug(self.vtk_widget.highlighted_names())
            info = self.vtk_widget.get_actor_info(actor_name)
            if info:
                self._update_geo_info(info)
            catalog_key = self._catalog_key_for_actor(actor_name)
            display_name = self._display_name_for_actor(actor_name)
            meta_parts = [f"选择名称: {display_name}"]
            if info:
                meta_parts.extend(f"{key}: {value}" for key, value in info.items())
            if catalog_key != display_name:
                meta_parts.append(f"匹配模型名称: {catalog_key}")
            self._show_catalog_matches(
                source_type="model",
                source_key=catalog_key,
                display_name=f"模型对象: {display_name}",
                matches=self._pdf_catalog_by_model_name.get(catalog_key, []),
                meta="  |  ".join(meta_parts),
            )

    def _descendant_leaf_node_ids(self, item):
        if item.childCount() == 0:
            node_id = item.data(0, self.DAMAGE_NODE_ID_ROLE)
            return [node_id] if node_id else []

        leaf_ids = []
        for idx in range(item.childCount()):
            leaf_ids.extend(self._descendant_leaf_node_ids(item.child(idx)))
        return leaf_ids

    def _node_catalog_rows(self, node_id):
        normalized_node_id = self._normalize_damage_id(node_id)
        return [
            row
            for row in self._pdf_catalog_rows
            if self._normalize_damage_id(row.get("model_name", "")) == normalized_node_id
            if self._is_damage_node_catalog_row(row)
        ]

    def _object_catalog_rows_for_damage_ids(self, damage_ids):
        rows = []
        for damage_id in damage_ids:
            normalized_damage_id = self._normalize_damage_id(damage_id)
            for row in self._pdf_catalog_by_damage_leaf_id.get(normalized_damage_id, []):
                if self._is_damage_node_catalog_row(row):
                    continue
                rows.append(row)
        return rows

    def _is_damage_node_catalog_row(self, row):
        model_name = self._normalize_damage_id(row.get("model_name", ""))
        if model_name not in self._damage_node_ids:
            return False
        damage_leaf_id = self._normalize_damage_id(row.get("damage_leaf_id", ""))
        return not damage_leaf_id or damage_leaf_id == model_name

    def _set_highlight_debug(self, object_names):
        names = [self._highlight_debug_text(name) for name in object_names if name]
        text = " ; ".join(names) if names else "无"
        if len(text) > 160:
            text = f"{text[:157]}..."
        self.highlight_debug_label.setText(f"实际高亮: {text}")

    def _highlight_debug_text(self, selection_name):
        catalog_name = self._catalog_key_for_actor(selection_name)
        display_name = self._display_name_for_actor(selection_name)
        blender_name = display_name or catalog_name or selection_name
        if catalog_name and catalog_name != blender_name:
            return f"{blender_name} | CSV:{catalog_name} | VTK:{selection_name}"
        if blender_name != selection_name:
            return f"{blender_name} | VTK:{selection_name}"
        return str(selection_name)

    def _selection_names_for_catalog_rows(self, rows):
        selection_names = []
        seen = set()
        for row in rows:
            for selection_name in self._selection_names_for_catalog_key(
                row.get("model_name", "")
            ):
                if selection_name in seen:
                    continue
                selection_names.append(selection_name)
                seen.add(selection_name)
        return selection_names

    def _damage_display_matches(self, node_matches, object_matches):
        ordered_matches = []
        seen = set()
        for row in (*node_matches, *object_matches):
            key = (
                row.get("model_name"),
                row.get("damage_leaf_id"),
                row.get("document_path"),
            )
            if key in seen:
                continue
            ordered_matches.append(row)
            seen.add(key)
        return ordered_matches

    def _selection_names_for_catalog_key(self, catalog_key):
        if not catalog_key:
            return []

        matches = []
        fuzzy_matches = []
        lookup_key = self._normalize_catalog_match_text(catalog_key)
        for selection_name in self._actor_catalog_names:
            actor_key = self._catalog_key_for_actor(selection_name)
            if actor_key == catalog_key:
                matches.append(selection_name)
                continue

            candidate_texts = (
                actor_key,
                self._actor_catalog_names.get(selection_name, ""),
                self._display_name_for_actor(selection_name),
                selection_name,
            )
            if any(
                lookup_key
                and lookup_key in self._normalize_catalog_match_text(candidate)
                for candidate in candidate_texts
            ):
                fuzzy_matches.append(selection_name)
        matches.extend(
            selection_name
            for selection_name in fuzzy_matches
            if selection_name not in matches
        )
        return matches

    def _normalize_catalog_match_text(self, text):
        return str(text or "").strip().casefold()

    def _original_display_name(self, name, fallback):
        name = str(name or "").strip()
        return name or fallback

    def _display_name_for_actor(self, actor_name):
        return self._actor_display_names.get(actor_name) or actor_name

    def _best_catalog_name(self, *candidates):
        fallback = ""
        for candidate in candidates:
            if not candidate:
                continue
            candidate = str(candidate).strip()
            if not fallback:
                fallback = candidate
            if candidate in self._pdf_catalog_by_model_name:
                return candidate
        return fallback

    def _catalog_key_for_actor(self, actor_name):
        if not actor_name:
            return ""

        mapped_name = self._actor_catalog_names.get(actor_name)
        if mapped_name and mapped_name in self._pdf_catalog_by_model_name:
            return mapped_name

        return mapped_name or actor_name

    def _show_catalog_matches(
        self, source_type, source_key, display_name, matches, meta=""
    ):
        self.selection_title_label.setText(display_name)
        lookup_label = "模型名称" if source_type == "model" else "毁伤叶子ID"
        self.selection_meta_label.setText(meta or f"{lookup_label}: {source_key}")

        self._current_catalog_matches = list(matches)
        self.catalog_table.blockSignals(True)
        self.catalog_table.setRowCount(len(self._current_catalog_matches))
        for row_idx, row in enumerate(self._current_catalog_matches):
            match_id = (
                row["model_name"] if source_type == "model" else row["damage_leaf_id"]
            )
            detail_status = (
                "已配置" if self._device_detail_for_catalog_row(row) else "未配置"
            )
            values = (
                row["display_name"],
                match_id or source_key,
                detail_status,
            )
            for col_idx, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.UserRole, row_idx)
                self.catalog_table.setItem(row_idx, col_idx, item)
        self.catalog_table.blockSignals(False)

        if self._current_catalog_matches:
            self.catalog_table.selectRow(0)
            self._display_catalog_row(0)
            return

        device_row = self._device_detail_for_key(source_type, source_key)
        if device_row:
            self._show_device_detail(device_row, display_name)
            return

        self._set_document_message(
            f"未找到设备详情: {lookup_label} = {source_key}"
        )

    def _on_catalog_row_clicked(self, row, column):
        self._display_catalog_row(row)

    def _display_catalog_row(self, row_idx):
        if row_idx < 0 or row_idx >= len(self._current_catalog_matches):
            self._set_document_message("未选择设备")
            return

        row = self._current_catalog_matches[row_idx]
        title = row["display_name"] or row["model_name"] or row["damage_leaf_id"]
        device_row = self._device_detail_for_catalog_row(row)

        if device_row is None:
            self._set_document_message(f"未找到设备详情: {title}")
            return

        self._show_device_detail(device_row, title)

    def _device_detail_for_key(self, source_type, source_key):
        if source_type == "model":
            rows = self._device_catalog_by_model_name.get(source_key, [])
        else:
            rows = self._device_catalog_by_damage_leaf_id.get(source_key, [])
            if not rows:
                rows = self._device_catalog_by_model_name.get(source_key, [])
        return rows[0] if rows else None

    def _device_detail_for_catalog_row(self, catalog_row):
        model_name = catalog_row.get("model_name", "")
        damage_leaf_id = self._normalize_damage_id(catalog_row.get("damage_leaf_id", ""))

        rows = self._device_catalog_by_model_name.get(model_name, [])
        if rows:
            return rows[0]

        rows = self._device_catalog_by_damage_leaf_id.get(damage_leaf_id, [])
        if rows:
            return rows[0]

        return None

    def _normalize_damage_id(self, value):
        text = unicodedata.normalize("NFKC", str(value or ""))
        return text.strip()

    def _show_device_detail(self, device_row, fallback_title=""):
        title = (
            device_row.get("display_name")
            or device_row.get("model_name")
            or device_row.get("damage_leaf_id")
            or fallback_title
        )
        self._current_document_path = None
        self.open_document_button.setEnabled(False)
        self.device_title_label.setText(title or "未命名设备")

        for field_key in ("material", "thickness", "size", "function"):
            value = device_row.get(field_key) or "待补充"
            self.device_field_labels[field_key].setText(value)

        self._show_device_image(device_row.get("image_path", ""))
        self.document_status_label.setText(f"正在显示设备详情: {title}")
        self.document_view.setCurrentWidget(self.device_detail_view)

    def _show_device_image(self, image_path):
        resolved_path = self._resolve_device_image_path(image_path)
        if not image_path:
            self.device_image_label.setPixmap(QPixmap())
            self.device_image_label.setText("未配置设备图片")
            return
        if not os.path.exists(resolved_path):
            self.device_image_label.setPixmap(QPixmap())
            self.device_image_label.setText(f"设备图片不存在:\n{image_path}")
            return

        pixmap = QPixmap(resolved_path)
        if pixmap.isNull():
            self.device_image_label.setPixmap(QPixmap())
            self.device_image_label.setText(f"设备图片无法读取:\n{image_path}")
            return

        target_size = self.device_image_label.size()
        scaled = pixmap.scaled(
            max(target_size.width(), 240),
            260,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.device_image_label.setText("")
        self.device_image_label.setPixmap(scaled)

    def _load_document_preview(self, resolved_path, title):
        filename = os.path.basename(resolved_path)
        ext = os.path.splitext(resolved_path)[1].lower()

        if ext == ".pdf":
            if self.pdf_document is None or self.pdf_view is None:
                self._show_document_message(
                    f"已匹配 PDF，但当前 PySide6 环境未启用 QtPdf。\n{resolved_path}"
                )
                self.document_status_label.setText(f"无法预览: {title} ({filename})")
                return

            self.pdf_document.close()
            error = self.pdf_document.load(resolved_path)
            if error != QPdfDocument.Error.None_:
                self._show_document_message(
                    f"PDF 加载失败: {error.name}\n{resolved_path}"
                )
                self.document_status_label.setText(
                    f"PDF 加载失败: {title} ({filename})"
                )
                return

            self.document_view.setCurrentWidget(self.pdf_view)
            self.document_status_label.setText(f"正在显示: {title} ({filename})")
            return

        if self.web_document_view is None:
            self._show_document_message(
                f"已匹配文档，但当前 PySide6 环境未启用 QtWebEngine。\n{resolved_path}"
            )
            self.document_status_label.setText(f"无法预览: {title} ({filename})")
            return

        self.web_document_view.load(QUrl.fromLocalFile(resolved_path))
        self.document_view.setCurrentWidget(self.web_document_view)
        self.document_status_label.setText(f"正在显示: {title} ({filename})")

    def _set_document_message(self, message):
        self._current_document_path = None
        if hasattr(self, "open_document_button"):
            self.open_document_button.setEnabled(False)
        if hasattr(self, "document_status_label"):
            self.document_status_label.setText(message)
        if not hasattr(self, "document_message_view"):
            return

        self._show_document_message(message)

    def _show_document_message(self, message):
        self.document_message_view.setText(message)
        self.document_view.setCurrentWidget(self.document_message_view)

    def _open_current_document(self):
        if not self._current_document_path:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(self._current_document_path))

    def clear_scene(self):
        """清空场景"""
        self.vtk_widget.clear_scene()
        self._gltf_importer = None
        self._loaded_items = []
        self._actor_catalog_names.clear()
        self._actor_display_names.clear()
        self._current_file = None
        self.catalog_table.setRowCount(0)
        self._current_catalog_matches = []
        self._set_highlight_debug(self.vtk_widget.highlighted_names())
        self.selection_title_label.setText("未选择对象或损伤节点")
        self.selection_meta_label.setText(
            "请选择模型中的 object，或点击左侧损伤树节点。"
        )
        self._set_document_message("未选择设备")
        self.vertex_label.setText("")
        self.status_label.setText("场景已清空")
        self.vtk_widget.refresh()

    def save_screenshot(self):
        """保存截图"""
        filepath, _ = QFileDialog.getSaveFileName(
            self, "保存截图", "screenshot.png", "PNG 图片 (*.png)"
        )
        if filepath:
            self.vtk_widget.screenshot(filepath)
            self.status_label.setText(f"截图已保存: {filepath}")

    def _toggle_wireframe(self, checked):
        """切换线框模式"""
        self._is_wireframe = checked
        self.vtk_widget.toggle_wireframe(checked)

        # 同步所有关联控件
        self.action_wireframe.blockSignals(True)
        self.btn_wireframe.blockSignals(True)
        if hasattr(self, "chk_wireframe"):
            self.chk_wireframe.blockSignals(True)
        self.action_wireframe.setChecked(checked)
        self.btn_wireframe.setChecked(checked)
        if hasattr(self, "chk_wireframe"):
            self.chk_wireframe.setChecked(checked)
        self.action_wireframe.blockSignals(False)
        self.btn_wireframe.blockSignals(False)
        if hasattr(self, "chk_wireframe"):
            self.chk_wireframe.blockSignals(False)

    def _set_camera_debug_overlay_visible(self, checked):
        """统一同步菜单、工具栏与 VTK 调试框状态。"""
        self.vtk_widget.set_camera_debug_overlay_visible(checked)

        self.action_camera_debug.blockSignals(True)
        self.btn_camera_debug.blockSignals(True)
        self.action_camera_debug.setChecked(checked)
        self.btn_camera_debug.setChecked(checked)
        self.action_camera_debug.blockSignals(False)
        self.btn_camera_debug.blockSignals(False)

    def _actor_has_any_texture(self, actor):
        if actor is None:
            return False

        if actor.GetTexture() is not None:
            return True

        prop = actor.GetProperty()
        if prop is None:
            return False

        # VTK glTF importer may store PBR textures in vtkProperty texture map
        # (for example key "albedoTex") instead of actor.GetTexture().
        if hasattr(prop, "GetNumberOfTextures") and prop.GetNumberOfTextures() > 0:
            return True

        pbr_texture_getters = (
            "GetBaseColorTexture",
            "GetORMTexture",
            "GetNormalTexture",
            "GetEmissiveTexture",
            "GetOcclusionTexture",
            "GetMetallicTexture",
            "GetRoughnessTexture",
        )
        for getter_name in pbr_texture_getters:
            getter = getattr(prop, getter_name, None)
            if callable(getter):
                try:
                    if getter() is not None:
                        return True
                except TypeError:
                    continue

        return False

    def _optimize_actor_texture(self, actor, max_texture_size=1024):
        """保留贴图但下采样，降低显存与带宽压力。"""
        texture = actor.GetTexture()
        if texture is None:
            return

        texture.InterpolateOff()
        texture.MipmapOff()
        texture.SetQualityTo16Bit()

        image = texture.GetInput()
        if image is None:
            return
        dims = image.GetDimensions()
        width, height = dims[0], dims[1]
        if width <= 0 or height <= 0:
            return
        max_dim = max(width, height)
        if max_dim <= max_texture_size:
            return

        scale = max_texture_size / float(max_dim)
        out_w = max(1, int(width * scale))
        out_h = max(1, int(height * scale))

        resize = vtk.vtkImageResize()
        resize.SetInputData(image)
        resize.SetResizeMethodToOutputDimensions()
        resize.SetOutputDimensions(out_w, out_h, max(1, dims[2]))
        resize.Update()
        texture.SetInputData(resize.GetOutput())

    def _optimize_actor_material_for_speed(self, actor, has_texture=None):
        """保持贴图可见，降低材质计算复杂度。"""
        prop = actor.GetProperty()
        if prop is None:
            return
        # Keep glTF/PBR textured materials intact; forcing Gouraud can drop PBR textures.
        if has_texture is None:
            has_texture = self._actor_has_any_texture(actor)
        if has_texture:
            if hasattr(prop, "SetInterpolationToPBR"):
                prop.SetInterpolationToPBR()
            return

        prop.SetSpecular(0.02)
        prop.SetSpecularPower(8.0)
        prop.SetInterpolationToGouraud()

    def _get_loaded_object_count(self):
        count = 0
        for item_data in self._loaded_items:
            sub_items = item_data.get("sub_items") or []
            count += len(sub_items) if sub_items else 1
        return count

    def _get_actor_geometry_stats(self, actor):
        mapper = actor.GetMapper()
        if not mapper:
            return 0, 0
        data_obj = mapper.GetInputDataObject(0, 0)
        return self._count_data_object_geometry(data_obj)

    def _count_data_object_geometry(self, data_obj):
        if data_obj is None:
            return 0, 0
        if data_obj.IsA("vtkPolyData"):
            return data_obj.GetNumberOfPoints(), data_obj.GetNumberOfCells()
        if data_obj.IsA("vtkCompositeDataSet"):
            total_points = 0
            total_cells = 0
            iterator = data_obj.NewIterator()
            iterator.InitTraversal()
            while not iterator.IsDoneWithTraversal():
                points, cells = self._count_data_object_geometry(
                    iterator.GetCurrentDataObject()
                )
                total_points += points
                total_cells += cells
                iterator.GoToNextItem()
            return total_points, total_cells
        return 0, 0

    # ============================================================
    # 信息面板更新
    # ============================================================

    def _update_file_info(self, filepath):
        """右侧栏不再显示文件属性；保留方法避免加载流程分叉。"""
        return

    def _update_geo_info(self, info):
        """几何信息合并到当前选择摘要，不再单独显示表格。"""
        return

    def _update_geo_info_all(self, total_verts, total_faces, obj_count):
        """右侧栏只显示当前选择和设备详情，不显示场景汇总。"""
        return

    def _show_about(self):
        """显示关于对话框"""
        QMessageBox.about(
            self,
            "关于 工业模型查看器",
            "<h2 style='color:#4fc3f7'>工业模型查看器</h2>"
            "<p>ShipViewer v1.0</p>"
            "<p>基于 PySide6 + VTK 的工业级3D模型查看器</p>"
            "<hr>"
            "<p><b>支持格式：</b></p>"
            "<ul>"
            "<li>.3dm - Rhinoceros 3D 模型</li>"
            "<li>.glb / .gltf - glTF 二进制格式</li>"
            "</ul>"
            "<p><b>快捷键：</b></p>"
            "<ul>"
            "<li>Ctrl+O - 打开文件</li>"
            "<li>1/2/3 - 前视/俯视/右视</li>"
            "<li>0 - 等轴测视图</li>"
            "<li>R - 重置视角</li>"
            "<li>W - 线框模式</li>"
            "</ul>",
        )

    # ============================================================
    # 拖拽支持
    # ============================================================

    def dragEnterEvent(self, event):
        """拖拽进入事件"""
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                filepath = url.toLocalFile()
                ext = os.path.splitext(filepath)[1].lower()
                if ext in (".3dm", ".glb", ".gltf"):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event):
        """拖拽放下事件"""
        for url in event.mimeData().urls():
            filepath = url.toLocalFile()
            ext = os.path.splitext(filepath)[1].lower()
            if ext in (".3dm", ".glb", ".gltf"):
                self._load_model(filepath)
                return
