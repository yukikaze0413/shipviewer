"""
主窗口控制器
管理菜单、工具栏、停靠面板和3D视口
"""
import csv
import os
import sys
from collections import defaultdict
import vtk

from PySide6.QtWidgets import (
    QMainWindow, QDockWidget, QTreeWidget, QTreeWidgetItem,
    QTableWidget, QTableWidgetItem,
    QToolBar, QStatusBar, QFileDialog, QMessageBox,
    QProgressBar, QLabel, QVBoxLayout, QHBoxLayout,
    QWidget, QMenu, QMenuBar, QSplitter, QTabWidget,
    QGroupBox, QCheckBox, QComboBox, QPushButton, QAbstractItemView,
    QFrame, QSizePolicy, QApplication
)
from PySide6.QtCore import Qt, QSize, QTimer
from PySide6.QtGui import QAction, QIcon, QKeySequence, QFont, QColor

from core.vtk_widget import VTKWidget
from core.model_loader import ModelLoadThread, get_supported_formats, get_file_info


class MainWindow(QMainWindow):
    DAMAGE_NODE_ID_ROLE = Qt.UserRole + 100
    DAMAGE_PARENT_ID_ROLE = Qt.UserRole + 101
    DAMAGE_LEVEL_ROLE = Qt.UserRole + 102

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
        self._damage_tree_csv_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "damage-tree-nodes.csv",
        )

        # 构建界面
        self._create_vtk_viewport()
        self._create_menus()
        self._create_toolbar()
        self._create_model_tree_dock()
        self._create_properties_dock()
        self._create_status_bar()
        self._load_damage_tree_from_csv()

        # 信号连接
        self.vtk_widget.model_clicked.connect(self._on_vtk_model_clicked)

        # 初始化VTK（延迟到窗口显示后）
        QTimer.singleShot(100, self._init_vtk)

    # ============================================================
    # UI 构建
    # ============================================================

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

        self.action_grid = QAction("显示网格(&G)", self)
        self.action_grid.setShortcut(QKeySequence("G"))
        self.action_grid.setCheckable(True)
        self.action_grid.setChecked(True)
        self.action_grid.toggled.connect(self.vtk_widget.toggle_grid)
        view_menu.addAction(self.action_grid)

        # === 窗口菜单 ===
        window_menu = menubar.addMenu("窗口(&W)")
        self.action_show_tree = QAction("损伤树", self)
        self.action_show_tree.setCheckable(True)
        self.action_show_tree.setChecked(True)
        window_menu.addAction(self.action_show_tree)

        self.action_show_props = QAction("属性面板", self)
        self.action_show_props.setCheckable(True)
        self.action_show_props.setChecked(True)
        window_menu.addAction(self.action_show_props)

        # === 帮助菜单 ===
        help_menu = menubar.addMenu("帮助(&H)")
        action_about = QAction("关于(&A)...", self)
        action_about.triggered.connect(self._show_about)
        help_menu.addAction(action_about)

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
        """创建属性面板停靠窗口"""
        self.props_dock = QDockWidget("属性", self)
        self.props_dock.setMinimumWidth(260)
        self.props_dock.setFeatures(
            QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetClosable
        )

        props_container = QWidget()
        props_layout = QVBoxLayout(props_container)
        props_layout.setContentsMargins(8, 8, 8, 8)
        props_layout.setSpacing(8)

        # 文件信息组
        file_group = QGroupBox("文件信息")
        file_layout = QVBoxLayout(file_group)
        self.file_info_table = QTableWidget()
        self.file_info_table.setColumnCount(2)
        self.file_info_table.setHorizontalHeaderLabels(["属性", "值"])
        self.file_info_table.horizontalHeader().setStretchLastSection(True)
        self.file_info_table.verticalHeader().setVisible(False)
        self.file_info_table.setAlternatingRowColors(True)
        self.file_info_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.file_info_table.setMaximumHeight(160)
        file_layout.addWidget(self.file_info_table)
        props_layout.addWidget(file_group)

        # 几何信息组
        geo_group = QGroupBox("几何信息")
        geo_layout = QVBoxLayout(geo_group)
        self.geo_info_table = QTableWidget()
        self.geo_info_table.setColumnCount(2)
        self.geo_info_table.setHorizontalHeaderLabels(["属性", "值"])
        self.geo_info_table.horizontalHeader().setStretchLastSection(True)
        self.geo_info_table.verticalHeader().setVisible(False)
        self.geo_info_table.setAlternatingRowColors(True)
        self.geo_info_table.setEditTriggers(QTableWidget.NoEditTriggers)
        geo_layout.addWidget(self.geo_info_table)
        props_layout.addWidget(geo_group)

        # 显示选项组
        display_group = QGroupBox("显示选项")
        display_layout = QVBoxLayout(display_group)

        self.chk_wireframe = QCheckBox("线框模式")
        self.chk_wireframe.toggled.connect(self._toggle_wireframe)
        display_layout.addWidget(self.chk_wireframe)

        self.chk_grid = QCheckBox("显示网格")
        self.chk_grid.setChecked(True)
        self.chk_grid.toggled.connect(self.vtk_widget.toggle_grid)
        display_layout.addWidget(self.chk_grid)

        props_layout.addWidget(display_group)

        props_layout.addStretch()
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

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(200)
        self.progress_bar.setVisible(False)
        self.status_bar.addPermanentWidget(self.progress_bar)

        self.vertex_label = QLabel("")
        self.status_bar.addPermanentWidget(self.vertex_label)

    def _init_vtk(self):
        """延迟初始化VTK"""
        self.vtk_widget.initialize()

    def _reload_damage_tree(self):
        self._load_damage_tree_from_csv()

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
            node_id = row["node_id"]
            if node_id in nodes_by_id:
                duplicate_ids.append(node_id)
                continue
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

    def _create_damage_tree_item(self, node, parent_item, nodes_by_id, children_map, level, lineage):
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
            self,
            "打开模型文件",
            "",
            get_supported_formats()
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

        # 启动加载线程
        self._load_thread = ModelLoadThread(filepath)
        self._load_thread.progress.connect(self._on_load_progress)
        self._load_thread.finished.connect(self._on_load_finished)
        self._load_thread.error.connect(self._on_load_error)
        self._load_thread.start()

    def _load_gltf_with_importer(self, filepath):
        """用 vtkGLTFImporter 加载，保留完整材质/贴图。"""
        try:
            self.progress_bar.setValue(20)
            self.status_label.setText("正在导入 glTF 场景（保留材质）...")

            self.vtk_widget.clear_scene()
            self._loaded_items = []

            importer = vtk.vtkGLTFImporter()
            importer.SetRenderWindow(self.vtk_widget.render_window)
            importer.SetFileName(filepath)
            importer.Update()
            self._gltf_importer = importer

            self.progress_bar.setValue(60)
            self.status_label.setText("正在优化贴图质量...")

            imported = importer.GetImportedActors()
            imported.InitTraversal()
            imported_count = imported.GetNumberOfItems()
            texture_max_size = 512 if imported_count > 1500 else 1024
            total_verts = 0
            total_faces = 0
            object_count = 0
            textured_count = 0
            actor_index = 0

            while True:
                actor = imported.GetNextActor()
                if actor is None:
                    break

                if self._actor_has_any_texture(actor):
                    textured_count += 1
                self._optimize_actor_texture(actor, max_texture_size=texture_max_size)
                self._optimize_actor_material_for_speed(actor)

                actor_name = f"actor_{actor_index}"
                self.vtk_widget.add_actor(actor, actor_name, add_to_renderer=False)

                points, cells = self._get_actor_geometry_stats(actor)
                total_verts += points
                total_faces += cells
                object_count += 1
                self._loaded_items.append({
                    "actor": actor,
                    "name": f"Object_{actor_index}",
                    "type": "Mesh",
                    "points": points,
                    "cells": cells,
                })
                actor_index += 1

            self.vtk_widget.reset_camera()
            self.progress_bar.setVisible(False)

            if object_count == 0:
                self.status_label.setText("加载失败")
                QMessageBox.warning(self, "提示", "未从 glTF 中导入到可渲染对象。")
                return

            if textured_count == 0:
                self.status_label.setText(f"✓ 成功加载 {object_count} 个对象（该模型不含贴图）")
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

    def _on_load_progress(self, percent, description):
        """加载进度回调"""
        self.progress_bar.setValue(percent)
        self.status_label.setText(description)

    def _on_load_finished(self, actors, message):
        """加载完成回调"""
        self.progress_bar.setVisible(False)

        # 清空旧场景
        self.vtk_widget.clear_scene()
        self._loaded_items = actors

        total_verts = 0
        total_faces = 0
        object_count = 0

        for i, item_data in enumerate(actors):
            actor = item_data["actor"]
            actor_name = f"actor_{i}"
            self.vtk_widget.add_actor(actor, actor_name)

            sub_items = item_data.get("sub_items") or []
            if sub_items:
                object_count += len(sub_items)
                for sub in sub_items:
                    sub_name = f"{actor_name}_block_{sub['flat_index']}"
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
            self.vtk_widget.highlight_actor("")
            self._update_geo_info_all(0, 0, self._get_loaded_object_count())
            return

        # 高亮
        self.vtk_widget.highlight_actor(actor_name)
        info = self.vtk_widget.get_actor_info(actor_name)
        if info:
            self._update_geo_info(info)

    def _on_tree_item_clicked(self, item, column):
        """树节点点击事件"""
        node_id = item.data(0, self.DAMAGE_NODE_ID_ROLE)
        if node_id:
            if item.childCount() > 0:
                item.setExpanded(not item.isExpanded())

            parent_id = item.data(0, self.DAMAGE_PARENT_ID_ROLE) or "无"
            self.status_label.setText(
                f"损伤节点: {item.text(0)}  |  节点ID: {node_id}  |  上级ID: {parent_id}  |  下级: {item.childCount()}"
            )
            return

        actor_name = item.data(0, Qt.UserRole)
        if actor_name:
            self.vtk_widget.highlight_actor(actor_name)
            info = self.vtk_widget.get_actor_info(actor_name)
            if info:
                self._update_geo_info(info)

    def clear_scene(self):
        """清空场景"""
        self.vtk_widget.clear_scene()
        self._gltf_importer = None
        self._loaded_items = []
        self._current_file = None
        self.file_info_table.setRowCount(0)
        self.geo_info_table.setRowCount(0)
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
        self.chk_wireframe.blockSignals(True)
        self.action_wireframe.setChecked(checked)
        self.btn_wireframe.setChecked(checked)
        self.chk_wireframe.setChecked(checked)
        self.action_wireframe.blockSignals(False)
        self.btn_wireframe.blockSignals(False)
        self.chk_wireframe.blockSignals(False)

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

    def _optimize_actor_material_for_speed(self, actor):
        """保持贴图可见，降低材质计算复杂度。"""
        prop = actor.GetProperty()
        if prop is None:
            return
        # Keep glTF/PBR textured materials intact; forcing Gouraud can drop PBR textures.
        if self._actor_has_any_texture(actor):
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
                points, cells = self._count_data_object_geometry(iterator.GetCurrentDataObject())
                total_points += points
                total_cells += cells
                iterator.GoToNextItem()
            return total_points, total_cells
        return 0, 0

    # ============================================================
    # 信息面板更新
    # ============================================================

    def _update_file_info(self, filepath):
        """更新文件信息表"""
        info = get_file_info(filepath)
        self.file_info_table.setRowCount(len(info))
        for row, (key, value) in enumerate(info.items()):
            self.file_info_table.setItem(row, 0, QTableWidgetItem(key))
            self.file_info_table.setItem(row, 1, QTableWidgetItem(str(value)))

    def _update_geo_info(self, info):
        """更新几何信息表（单个对象）"""
        self.geo_info_table.setRowCount(len(info))
        for row, (key, value) in enumerate(info.items()):
            self.geo_info_table.setItem(row, 0, QTableWidgetItem(key))
            self.geo_info_table.setItem(row, 1, QTableWidgetItem(str(value)))

    def _update_geo_info_all(self, total_verts, total_faces, obj_count):
        """更新几何信息表（汇总）"""
        info = {
            "对象数量": str(obj_count),
            "总顶点数": f"{total_verts:,}",
            "总面片数": f"{total_faces:,}",
        }
        self.geo_info_table.setRowCount(len(info))
        for row, (key, value) in enumerate(info.items()):
            self.geo_info_table.setItem(row, 0, QTableWidgetItem(key))
            self.geo_info_table.setItem(row, 1, QTableWidgetItem(value))

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
            "<li>G - 显示/隐藏网格</li>"
            "</ul>"
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
