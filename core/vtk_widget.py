"""
VTK渲染窗口组件
将VTK渲染器集成到PySide6 QWidget中
"""

import ctypes
import math
import os
import sys
import time
from collections import deque

import vtk
from vtkmodules.qt.QVTKRenderWindowInteractor import (
    QVTKRenderWindowInteractor,
    _get_event_pos,
)
from PySide6.QtWidgets import QWidget, QVBoxLayout
from PySide6.QtCore import Signal, QEvent, Qt, QTimer


class YUpInteractorStyle(vtk.vtkInteractorStyleTerrain):
    """旧方案：尝试在交互风格层重写按键语义，保留但不再调用。"""

    def OnLeftButtonDown(self):
        # 左键仅保留给拾取，不进入相机交互
        return

    def OnLeftButtonUp(self):
        return

    def OnRightButtonDown(self):
        # 复用 Terrain 风格的旋转逻辑，但绑定到右键
        super().OnLeftButtonDown()

    def OnRightButtonUp(self):
        super().OnLeftButtonUp()


class RemappedQVTKRenderWindowInteractor(QVTKRenderWindowInteractor):
    """新方案：在 Qt 事件桥接层直接重映射鼠标语义。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._wheel_delta_remainder = 0

    def _viewport_parent(self):
        parent = self.parent()
        return parent if parent is not None else None

    def _set_interactor_event_info(self, ev, repeat=0):
        ctrl, shift = self._GetCtrlShift(ev)
        x, y = _get_event_pos(ev)
        self._setEventInformation(x, y, ctrl, shift, chr(0), repeat, None)
        return x, y

    def _dispatch_left_click_pick(self):
        parent = self._viewport_parent()
        if parent is not None and hasattr(parent, "_handle_viewport_left_click"):
            parent._handle_viewport_left_click()

    def _dispatch_hover_pick(self, ev):
        parent = self._viewport_parent()
        if parent is not None and hasattr(parent, "_handle_viewport_hover"):
            parent._handle_viewport_hover(self._event_global_pos(ev))

    def _dispatch_hover_leave(self):
        parent = self._viewport_parent()
        if parent is not None and hasattr(parent, "_handle_viewport_hover_leave"):
            parent._handle_viewport_hover_leave()

    def _event_global_pos(self, ev):
        if hasattr(ev, "globalPosition"):
            return ev.globalPosition().toPoint()
        if hasattr(ev, "globalPos"):
            return ev.globalPos()
        return None

    def _dispatch_wheel_step(self, direction):
        parent = self._viewport_parent()
        if parent is not None and hasattr(parent, "_handle_viewport_wheel"):
            parent._handle_viewport_wheel(direction)
        elif direction > 0:
            self._Iren.MouseWheelForwardEvent()
        else:
            self._Iren.MouseWheelBackwardEvent()

    def _wheel_event_delta(self, ev):
        angle_delta = ev.angleDelta().y() if hasattr(ev, "angleDelta") else 0
        pixel_delta = ev.pixelDelta().y() if hasattr(ev, "pixelDelta") else 0
        legacy_delta = ev.delta() if hasattr(ev, "delta") else 0
        return angle_delta, pixel_delta, legacy_delta

    def _consume_wheel_angle_delta(self):
        while self._wheel_delta_remainder >= 120:
            self._dispatch_wheel_step(1)
            self._wheel_delta_remainder -= 120

        while self._wheel_delta_remainder <= -120:
            self._dispatch_wheel_step(-1)
            self._wheel_delta_remainder += 120

    def mousePressEvent(self, ev):
        repeat = 1 if ev.type() == QEvent.Type.MouseButtonDblClick else 0
        self._set_interactor_event_info(ev, repeat=repeat)

        self._ActiveButton = ev.button()

        if self._ActiveButton == Qt.MouseButton.LeftButton:
            self._dispatch_left_click_pick()
            ev.accept()
            return

        if self._ActiveButton == Qt.MouseButton.RightButton:
            self._Iren.LeftButtonPressEvent()
            return

        if self._ActiveButton == Qt.MouseButton.MiddleButton:
            self._Iren.MiddleButtonPressEvent()
            return

        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._set_interactor_event_info(ev)

        if self._ActiveButton == Qt.MouseButton.LeftButton:
            self._ActiveButton = Qt.MouseButton.NoButton
            ev.accept()
            return

        if self._ActiveButton == Qt.MouseButton.RightButton:
            self._Iren.LeftButtonReleaseEvent()
            self._ActiveButton = Qt.MouseButton.NoButton
            return

        if self._ActiveButton == Qt.MouseButton.MiddleButton:
            self._Iren.MiddleButtonReleaseEvent()
            self._ActiveButton = Qt.MouseButton.NoButton
            return

        super().mouseReleaseEvent(ev)

    def mouseMoveEvent(self, ev):
        self._set_interactor_event_info(ev)
        if ev.buttons() == Qt.MouseButton.NoButton:
            self._dispatch_hover_pick(ev)
            super().mouseMoveEvent(ev)
        else:
            self._dispatch_hover_leave()
            self._Iren.MouseMoveEvent()
        ev.accept()

    def leaveEvent(self, ev):
        self._dispatch_hover_leave()
        super().leaveEvent(ev)

    def wheelEvent(self, ev):
        self._set_interactor_event_info(ev)
        angle_delta, pixel_delta, legacy_delta = self._wheel_event_delta(ev)

        # 优先使用标准滚轮角度增量；若当前输入设备只提供像素级滚动，则直接按方向触发缩放。
        if angle_delta:
            self._wheel_delta_remainder += angle_delta
            self._consume_wheel_angle_delta()
        elif pixel_delta:
            self._dispatch_wheel_step(1 if pixel_delta > 0 else -1)
        elif legacy_delta:
            self._dispatch_wheel_step(1 if legacy_delta > 0 else -1)

        ev.accept()


class VTKWidget(QWidget):
    """基于VTK的3D视口组件 - 极致性能优化版 (针对百万面级模型)"""

    LOD_POINT_THRESHOLD = 200_000
    ENABLE_LOD_ACTOR = False
    DYNAMIC_HIDE_MIN_ACTOR_COUNT = 800
    DYNAMIC_HIDE_MAX_ANGULAR_SIZE_RAD = 0.01
    DYNAMIC_HIDE_MAX_CELLS = 40_000
    CAMERA_VIEW_ANGLE_DEG = 28.0
    CAMERA_MAX_DISTANCE = 750.0
    CAMERA_FRAME_PADDING = 1.08
    FOCUS_FRAME_PADDING = 1.28
    FOCUS_DIM_OPACITY = 0.15
    WATER_DEFAULT_SIZE = 180.0
    WATER_TILE_SIZE = 24.0

    # 信号
    model_clicked = Signal(str)
    model_hovered = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._actors = {}  # base_name -> vtkActor/vtkLODActor
        self._actor_name_by_ptr = {}  # vtk object pointer -> base_name
        self._actor_geometry = {}  # name -> (points, cells)
        self._actor_base_color = {}  # base_name -> (r, g, b)
        self._selection_targets = (
            {}
        )  # sel_name -> (base_name, flat_block_index or None)
        self._selection_geometry = {}  # sel_name -> (points, cells)
        self._selection_base_color = {}  # sel_name -> (r, g, b)
        self._selection_world_bounds = {}  # sel_name -> world-space bounds
        self._base_to_flat_alias = {}  # base_name -> {flat_block_index: sel_name}
        self._base_to_dataset_alias = {}  # base_name -> {dataset_ptr: sel_name}
        self._actor_sphere = {}  # base_name -> (cx, cy, cz, radius)
        self._selected_actor_name = None
        self._selected_actor_names = []
        self._hovered_highlight_name = ""
        self._highlight_overlay_actors = {}
        self._highlight_actor_prop_state = {}
        self._background_image_reader = None
        self._background_texture = None
        self._skybox_actor = None
        self._water_actor = None
        self._water_polydata = None
        self._water_points = None
        self._water_tcoords = None
        self._water_texture = None
        self._water_texture_reader = None
        self._dynamic_hidden_actor_names = set()
        self._dynamic_hide_active = False
        self._dynamic_hide_enabled = True
        self._invisible_actor_count = 0
        self._focus_dimmed_actor_state = {}
        self._focus_dimmed_base_names = set()
        self._focus_selected_base_names = set()
        self._camera_debug_annotation = None
        self._camera_debug_overlay_visible = False
        self._performance_annotation = None
        self._performance_overlay_visible = False
        self._last_render_timestamp = 0.0
        self._frame_durations = deque(maxlen=200)
        self._frame_duration_sum = 0.0
        self._performance_snapshot = {}
        self._performance_snapshot_time = 0.0
        self._system_stats_cache = {}
        self._system_stats_timer = QTimer(self)
        self._system_stats_timer.setInterval(1500)
        self._system_stats_timer.timeout.connect(self._refresh_system_stats_cache)
        self._performance_overlay_update_interval = 2.0
        self._performance_overlay_last_text_time = 0.0
        self._current_fps = 0.0
        self._avg_fps_200 = 0.0
        self._frame_time_ms = 0.0
        self._avg_frame_time_ms = 0.0
        self._interaction_active = False
        self._render_count = 0
        self._scene_metadata = {
            "file_path": "",
            "file_format": "",
            "file_size_bytes": 0,
            "logical_object_count": 0,
        }
        self._camera_max_distance = float(self.CAMERA_MAX_DISTANCE)
        self._setup_ui()
        self._setup_vtk()

    def _setup_ui(self):
        self.setMouseTracking(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.vtk_widget = RemappedQVTKRenderWindowInteractor(self)
        self.vtk_widget.setMouseTracking(True)
        layout.addWidget(self.vtk_widget)

    def _setup_vtk(self):
        self.render_window = self.vtk_widget.GetRenderWindow()

        # 优化1：对于超大模型，关闭抗锯齿或保持极低
        self.render_window.SetMultiSamples(0)

        self.renderer = vtk.vtkRenderer()
        self.renderer.SetBackground(0.12, 0.13, 0.16)
        self.renderer.SetBackground2(0.18, 0.20, 0.24)
        self.renderer.GradientBackgroundOn()
        self.render_window.AddRenderer(self.renderer)

        # 优化2：设置交互时的目标帧率 (FPS)
        # 当进行旋转等交互时，VTK会尝试通过 LOD 切换来维持这个更新率
        self.interactor = self.render_window.GetInteractor()
        self.interactor.SetDesiredUpdateRate(30.0)  # 交互时尽量提高流畅度
        self.interactor.SetStillUpdateRate(1.0)  # 静止时维持低频更新

        # 新方案在 Qt 事件桥接层改按键语义，因此这里使用 VTK 原生 Terrain 风格即可。
        # 旧的 YUpInteractorStyle 方案保留在文件中，但不再启用。
        style = vtk.vtkInteractorStyleTerrain()
        self.interactor.SetInteractorStyle(style)

        self.picker = vtk.vtkCellPicker()
        self.picker.SetTolerance(0.0005)
        self.interactor.AddObserver(
            vtk.vtkCommand.StartInteractionEvent, self._on_start_interaction, 1.0
        )
        self.interactor.AddObserver(
            vtk.vtkCommand.EndInteractionEvent, self._on_end_interaction, 1.0
        )
        self.render_window.AddObserver(
            vtk.vtkCommand.StartEvent, self._on_render_start, 1.0
        )

        self._add_axes_widget()
        self._add_water_surface()
        self._setup_lighting()
        self._setup_camera_debug_overlay()
        self._setup_performance_overlay()

    def set_background_image(self, filepath):
        if filepath.lower().endswith((".hdr", ".pic")):
            return self.set_environment_texture(filepath)

        reader_factory = vtk.vtkImageReader2Factory()
        reader = reader_factory.CreateImageReader2(filepath)
        if reader is None:
            return False

        reader.SetFileName(filepath)
        reader.Update()

        texture = vtk.vtkTexture()
        texture.SetInputConnection(reader.GetOutputPort())
        texture.InterpolateOn()
        texture.RepeatOff()

        self._background_image_reader = reader
        self._background_texture = texture
        self.renderer.SetBackgroundTexture(texture)
        self.renderer.TexturedBackgroundOn()
        self.renderer.GradientBackgroundOff()
        self._performance_snapshot_time = 0.0
        self.render_window.Render()
        return True

    def set_environment_texture(self, filepath):
        reader = vtk.vtkHDRReader()
        if not reader.CanReadFile(filepath):
            return False

        reader.SetFileName(filepath)
        reader.Update()

        texture = vtk.vtkTexture()
        texture.SetInputConnection(reader.GetOutputPort())
        texture.SetColorModeToDirectScalars()
        texture.MipmapOn()
        texture.InterpolateOn()

        if self._skybox_actor is not None:
            self.renderer.RemoveActor(self._skybox_actor)

        skybox = vtk.vtkSkybox()
        skybox.SetTexture(texture)
        skybox.SetProjectionToSphere()
        skybox.SetFloorRight(0, 0, 1)

        self._background_image_reader = reader
        self._background_texture = texture
        self._skybox_actor = skybox
        self.renderer.AddActor(skybox)
        self.renderer.SetEnvironmentTexture(texture, False)
        self.renderer.UseImageBasedLightingOn()
        use_spherical_harmonics = getattr(
            self.renderer, "UseSphericalHarmonicsOn", None
        )
        if callable(use_spherical_harmonics):
            use_spherical_harmonics()
        self.renderer.TexturedBackgroundOff()
        self.renderer.GradientBackgroundOff()
        self._performance_snapshot_time = 0.0
        self.render_window.Render()
        return True

    def _setup_lighting(self):
        self.renderer.RemoveAllLights()
        key_light = vtk.vtkLight()
        key_light.SetLightTypeToSceneLight()
        key_light.SetPosition(5, 8, 5)
        key_light.SetIntensity(0.8)
        self.renderer.AddLight(key_light)

        fill_light = vtk.vtkLight()
        fill_light.SetLightTypeToSceneLight()
        fill_light.SetPosition(-5, 0, -5)
        fill_light.SetIntensity(0.3)
        self.renderer.AddLight(fill_light)

    def _setup_camera_debug_overlay(self):
        annotation = vtk.vtkCornerAnnotation()
        annotation.SetText(vtk.vtkCornerAnnotation.UpperRight, "")
        annotation.SetMaximumFontSize(18)
        annotation.SetLinearFontScaleFactor(2.0)
        annotation.SetNonlinearFontScaleFactor(1.0)

        text_prop = annotation.GetTextProperty()
        text_prop.SetColor(0.95, 0.97, 0.99)
        text_prop.SetBackgroundColor(0.07, 0.08, 0.10)
        text_prop.SetBackgroundOpacity(0.78)
        text_prop.FrameOn()
        text_prop.SetFrameColor(0.32, 0.36, 0.42)
        text_prop.SetFrameWidth(1)
        text_prop.BoldOff()
        text_prop.ShadowOff()

        annotation.VisibilityOff()
        self._camera_debug_annotation = annotation
        self.renderer.AddViewProp(annotation)

    def _on_render_start(self, obj, event):
        self._capture_render_timing()
        self._update_camera_debug_overlay()
        self._update_performance_overlay()

    def _format_vector(self, vector):
        return f"({vector[0]:+.4f}, {vector[1]:+.4f}, {vector[2]:+.4f})"

    def _camera_debug_values(self, camera):
        position = camera.GetPosition()
        focal_point = camera.GetFocalPoint()
        center_to_camera = self._normalize_vector(
            (
                position[0] - focal_point[0],
                position[1] - focal_point[1],
                position[2] - focal_point[2],
            )
        )
        projection_direction = self._normalize_vector(camera.GetDirectionOfProjection())
        view_up = self._normalize_vector(camera.GetViewUp())
        distance = camera.GetDistance()
        roll = camera.GetRoll()

        return {
            "position": position,
            "focal_point": focal_point,
            "center_to_camera": center_to_camera,
            "projection_direction": projection_direction,
            "view_up": view_up,
            "distance": distance,
            "roll": roll,
        }

    def _camera_debug_text(self, camera):
        values = self._camera_debug_values(camera)
        return (
            "相机调试\n"
            f"position: {self._format_vector(values['position'])}\n"
            f"focal_point: {self._format_vector(values['focal_point'])}\n"
            f"direction (center→camera): {self._format_vector(values['center_to_camera'])}\n"
            f"dop (camera→center): {self._format_vector(values['projection_direction'])}\n"
            f"view_up (screen↑): {self._format_vector(values['view_up'])}\n"
            f"distance: {values['distance']:.4f}\n"
            f"roll: {values['roll']:.4f}°"
        )

    def _update_camera_debug_overlay(self):
        if (
            not self._camera_debug_overlay_visible
            or self._camera_debug_annotation is None
        ):
            return

        camera = self.renderer.GetActiveCamera()
        if camera is None:
            self._camera_debug_annotation.SetText(
                vtk.vtkCornerAnnotation.UpperRight,
                "相机调试\n无活动相机",
            )
            return

        self._camera_debug_annotation.SetText(
            vtk.vtkCornerAnnotation.UpperRight,
            self._camera_debug_text(camera),
        )

    def set_camera_debug_overlay_visible(self, visible=True):
        self._camera_debug_overlay_visible = bool(visible)
        if self._camera_debug_annotation is None:
            return

        if self._camera_debug_overlay_visible:
            self._update_camera_debug_overlay()
            self._camera_debug_annotation.VisibilityOn()
        else:
            self._camera_debug_annotation.VisibilityOff()

        self.render_window.Render()

    def toggle_camera_debug_overlay(self, visible=None):
        if visible is None:
            visible = not self._camera_debug_overlay_visible
        self.set_camera_debug_overlay_visible(visible)

    def _setup_performance_overlay(self):
        annotation = vtk.vtkCornerAnnotation()
        annotation.SetText(vtk.vtkCornerAnnotation.UpperLeft, "")
        annotation.SetMaximumFontSize(16)
        annotation.SetLinearFontScaleFactor(2.0)
        annotation.SetNonlinearFontScaleFactor(1.0)

        text_prop = annotation.GetTextProperty()
        text_prop.SetColor(0.90, 0.98, 0.92)
        text_prop.SetBackgroundColor(0.05, 0.08, 0.06)
        text_prop.SetBackgroundOpacity(0.78)
        text_prop.FrameOn()
        text_prop.SetFrameColor(0.18, 0.34, 0.22)
        text_prop.SetFrameWidth(1)
        text_prop.BoldOff()
        text_prop.ShadowOff()

        annotation.VisibilityOff()
        self._performance_annotation = annotation
        self.renderer.AddViewProp(annotation)

    def _capture_render_timing(self):
        now = time.perf_counter()
        if self._last_render_timestamp > 0.0:
            delta = now - self._last_render_timestamp
            if delta > 0.0:
                self._frame_time_ms = delta * 1000.0
                self._current_fps = 1.0 / delta
                if len(self._frame_durations) == self._frame_durations.maxlen:
                    self._frame_duration_sum -= self._frame_durations[0]
                self._frame_durations.append(delta)
                self._frame_duration_sum += delta
                frame_count = len(self._frame_durations)
                if self._frame_duration_sum > 0.0 and frame_count > 0:
                    self._avg_fps_200 = frame_count / self._frame_duration_sum
                    self._avg_frame_time_ms = (
                        self._frame_duration_sum * 1000.0 / frame_count
                    )
        self._last_render_timestamp = now
        self._render_count += 1

    def _performance_overlay_text(self, stats):
        file_name = stats.get("file_name") or "No file"
        return (
            "Performance\n"
            f"FPS: {stats['fps']:.1f}  |  Avg200 FPS: {stats['avg_fps_200']:.1f}  |  Frame: {stats['frame_time_ms']:.2f} ms  |  Avg200 Frame: {stats['avg_frame_time_ms']:.2f} ms\n"
            f"File: {file_name}  |  Format: {stats['file_format_text']}  |  Size: {self._format_bytes(stats['file_size_bytes'])}\n"
            f"Objects: {stats['logical_object_count']:,}  |  Render Actors: {stats['render_actor_count']:,}  |  Invisible: {stats['invisible_actor_count']:,}\n"
            f"Vertices: {stats['point_count']:,}  |  Cells: {stats['cell_count']:,}  |  LOD: {stats['lod_actor_count']:,}\n"
            f"Textured Actors: {stats['textured_actor_count']:,}  |  Textures: {stats['texture_count']:,}  |  Images: {stats['image_count']:,}\n"
            f"Texture Pixels: {stats['texture_pixel_count'] / 1_000_000.0:.2f} MP  |  Max Texture: {stats['max_texture_size_text']}\n"
            f"Texture Memory ~= {self._format_bytes(stats['texture_memory_bytes'])}  |  Geometry Memory ~= {self._format_bytes(stats['geometry_memory_bytes'])}\n"
            f"Process Memory: WS {self._format_bytes(stats['process_working_set_bytes'])}  |  Private {self._format_bytes(stats['process_private_bytes'])}\n"
            f"Interaction: {'Active' if stats['interaction_active'] else 'Idle'}  |  Background: {stats['background_mode']}  |  Water: {'On' if stats['water_visible'] else 'Off'}"
        )

    def _update_performance_overlay(self, force=False):
        if (
            not self._performance_overlay_visible
            or self._performance_annotation is None
        ):
            return

        now = time.perf_counter()
        if (
            not force
            and self._performance_overlay_last_text_time > 0.0
            and (now - self._performance_overlay_last_text_time)
            < self._performance_overlay_update_interval
        ):
            return

        stats = self.get_performance_stats(force=force)
        self._performance_annotation.SetText(
            vtk.vtkCornerAnnotation.UpperLeft,
            self._performance_overlay_text(stats),
        )
        self._performance_overlay_last_text_time = now

    def set_performance_overlay_visible(self, visible=True):
        self._performance_overlay_visible = bool(visible)
        if self._performance_annotation is None:
            return

        if self._performance_overlay_visible:
            self._refresh_system_stats_cache()
            self._system_stats_timer.start()
            self._update_performance_overlay(force=True)
            self._performance_annotation.VisibilityOn()
        else:
            self._system_stats_timer.stop()
            self._performance_annotation.VisibilityOff()

        self.render_window.Render()

    def toggle_performance_overlay(self, visible=None):
        if visible is None:
            visible = not self._performance_overlay_visible
        self.set_performance_overlay_visible(visible)

    def _format_bytes(self, size_bytes):
        size = float(size_bytes or 0)
        if size <= 0.0:
            return "0 B"
        units = ("B", "KB", "MB", "GB", "TB")
        unit_index = 0
        while size >= 1024.0 and unit_index < len(units) - 1:
            size /= 1024.0
            unit_index += 1
        return f"{size:.2f} {units[unit_index]}"

    def _texture_object_id(self, vtk_obj):
        return getattr(vtk_obj, "__this__", "") if vtk_obj is not None else ""

    def _vtk_memory_size_bytes(self, vtk_obj):
        if vtk_obj is None:
            return 0
        getter = getattr(vtk_obj, "GetActualMemorySize", None)
        if not callable(getter):
            return 0
        try:
            value = getter()
            numeric_value = int(str(value)) if value is not None else 0
            return max(0, numeric_value) * 1024
        except Exception:
            return 0

    def _texture_image_stats(self, texture):
        image = texture.GetInput() if texture is not None else None
        if image is None:
            return {
                "image_id": "",
                "width": 0,
                "height": 0,
                "depth": 0,
                "pixel_count": 0,
                "memory_bytes": 0,
            }

        dims = image.GetDimensions()
        width = max(0, int(dims[0]))
        height = max(0, int(dims[1]))
        depth = max(1, int(dims[2]))
        pixel_count = width * height * depth
        memory_bytes = self._vtk_memory_size_bytes(image)
        if memory_bytes <= 0:
            scalar_components = max(1, int(image.GetNumberOfScalarComponents() or 1))
            scalar_size = max(1, int(image.GetScalarSize() or 1))
            memory_bytes = pixel_count * scalar_components * scalar_size

        return {
            "image_id": self._texture_object_id(image),
            "width": width,
            "height": height,
            "depth": depth,
            "pixel_count": pixel_count,
            "memory_bytes": memory_bytes,
        }

    def _actor_textures(self, actor):
        textures = []
        seen = set()

        def add_texture(texture):
            texture_id = self._texture_object_id(texture)
            if texture is None or texture_id in seen:
                return
            seen.add(texture_id)
            textures.append(texture)

        if actor is None:
            return textures

        add_texture(actor.GetTexture())
        prop = actor.GetProperty()
        if prop is None:
            return textures

        for getter_name in (
            "GetBaseColorTexture",
            "GetORMTexture",
            "GetNormalTexture",
            "GetEmissiveTexture",
            "GetOcclusionTexture",
            "GetMetallicTexture",
            "GetRoughnessTexture",
        ):
            getter = getattr(prop, getter_name, None)
            if not callable(getter):
                continue
            try:
                add_texture(getter())
            except TypeError:
                continue

        return textures

    def _process_memory_info(self):
        if sys.platform.startswith("win"):

            class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS_EX()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
            process = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                process,
                ctypes.byref(counters),
                counters.cb,
            )
            if ok:
                return {
                    "working_set_bytes": int(counters.WorkingSetSize),
                    "private_bytes": int(counters.PrivateUsage),
                }

        return {"working_set_bytes": 0, "private_bytes": 0}

    def _build_performance_snapshot(self):
        total_points = sum(points for points, _cells in self._actor_geometry.values())
        total_cells = sum(cells for _points, cells in self._actor_geometry.values())
        render_actor_count = len(self._actors)
        lod_actor_count = sum(
            1 for actor in self._actors.values() if actor.IsA("vtkLODActor")
        )
        textured_actor_count = 0
        geometry_memory_bytes = 0
        unique_textures = {}
        unique_images = {}

        for actor in self._actors.values():
            if self._actor_has_any_texture(actor):
                textured_actor_count += 1
            mapper = actor.GetMapper()
            data_obj = mapper.GetInputDataObject(0, 0) if mapper else None
            geometry_memory_bytes += self._vtk_memory_size_bytes(data_obj)
            for texture in self._actor_textures(actor):
                texture_id = self._texture_object_id(texture)
                if not texture_id or texture_id in unique_textures:
                    continue
                image_stats = self._texture_image_stats(texture)
                unique_textures[texture_id] = image_stats
                image_id = image_stats["image_id"]
                if image_id and image_id not in unique_images:
                    unique_images[image_id] = image_stats

        for texture in (self._background_texture, self._water_texture):
            texture_id = self._texture_object_id(texture)
            if texture is None or not texture_id or texture_id in unique_textures:
                continue
            image_stats = self._texture_image_stats(texture)
            unique_textures[texture_id] = image_stats
            image_id = image_stats["image_id"]
            if image_id and image_id not in unique_images:
                unique_images[image_id] = image_stats

        texture_memory_bytes = sum(
            item["memory_bytes"] for item in unique_textures.values()
        )
        texture_pixel_count = sum(
            item["pixel_count"] for item in unique_textures.values()
        )
        max_texture_width = max(
            (item["width"] for item in unique_textures.values()),
            default=0,
        )
        max_texture_height = max(
            (item["height"] for item in unique_textures.values()),
            default=0,
        )
        file_path = self._scene_metadata.get("file_path", "")
        file_format = self._scene_metadata.get("file_format", "")
        file_size_bytes = int(self._scene_metadata.get("file_size_bytes") or 0)
        logical_object_count = int(
            self._scene_metadata.get("logical_object_count") or 0
        )

        return {
            "file_name": os.path.basename(file_path) if file_path else "",
            "file_path": file_path,
            "file_format_text": file_format.upper() if file_format else "-",
            "file_size_bytes": file_size_bytes,
            "logical_object_count": logical_object_count,
            "render_actor_count": render_actor_count,
            "lod_actor_count": lod_actor_count,
            "point_count": total_points,
            "cell_count": total_cells,
            "textured_actor_count": textured_actor_count,
            "texture_count": len(unique_textures),
            "image_count": len(unique_images),
            "texture_pixel_count": texture_pixel_count,
            "texture_memory_bytes": texture_memory_bytes,
            "geometry_memory_bytes": geometry_memory_bytes,
            "max_texture_width": max_texture_width,
            "max_texture_height": max_texture_height,
            "max_texture_size_text": (
                f"{max_texture_width}×{max_texture_height}"
                if max_texture_width and max_texture_height
                else "-"
            ),
        }

    def _refresh_system_stats_cache(self):
        process_memory = self._process_memory_info()
        self._system_stats_cache = {
            "process_working_set_bytes": process_memory["working_set_bytes"],
            "process_private_bytes": process_memory["private_bytes"],
            "background_mode": (
                "HDR Environment"
                if self._skybox_actor is not None
                else (
                    "2D Background"
                    if self._background_texture is not None
                    else "Solid Color"
                )
            ),
            "water_visible": bool(
                self._water_actor is not None and self._water_actor.GetVisibility()
            ),
        }

    def rebuild_performance_stats(self):
        self._performance_snapshot = self._build_performance_snapshot()
        self._performance_snapshot_time = time.perf_counter()
        self._performance_overlay_last_text_time = 0.0
        self._refresh_system_stats_cache()

    def set_dynamic_hide_enabled(self, enabled=True):
        enabled = bool(enabled)
        if self._dynamic_hide_enabled == enabled:
            return

        self._dynamic_hide_enabled = enabled
        self._performance_overlay_last_text_time = 0.0
        if enabled:
            return

        if self._dynamic_hidden_actor_names:
            for name in list(self._dynamic_hidden_actor_names):
                actor = self._actors.get(name)
                if actor is not None:
                    actor.SetVisibility(True)
            self._dynamic_hidden_actor_names.clear()
            self._dynamic_hide_active = False
            self._invisible_actor_count = 0
            self.render_window.Render()

    def get_performance_stats(self, force=False):
        if not self._performance_snapshot:
            self.rebuild_performance_stats()
        elif force and not self._system_stats_cache:
            self._refresh_system_stats_cache()

        stats = dict(self._performance_snapshot)
        stats.update(self._system_stats_cache)
        stats.update(
            {
                "fps": self._current_fps,
                "avg_fps_200": self._avg_fps_200,
                "frame_time_ms": self._frame_time_ms,
                "avg_frame_time_ms": self._avg_frame_time_ms,
                "interaction_active": self._interaction_active,
                "selected_count": len(self._selected_actor_names),
                "render_count": self._render_count,
                "invisible_actor_count": self._invisible_actor_count,
            }
        )
        return stats

    def camera_max_distance(self):
        return self._camera_max_distance

    def set_camera_max_distance(self, distance):
        distance = float(distance)
        if distance <= 0:
            return False

        self._camera_max_distance = distance
        camera = self.renderer.GetActiveCamera()
        if camera is not None:
            self._clamp_camera_distance(camera)
            self.renderer.ResetCameraClippingRange()
            self.render_window.Render()
        return True

    def _add_axes_widget(self):
        axes = vtk.vtkAxesActor()
        axes.SetTotalLength(1.0, 1.0, 1.0)
        self.axes_widget = vtk.vtkOrientationMarkerWidget()
        self.axes_widget.SetOrientationMarker(axes)
        self.axes_widget.SetInteractor(self.interactor)
        self.axes_widget.SetViewport(0.0, 0.0, 0.15, 0.15)
        self.axes_widget.EnabledOn()
        self.axes_widget.InteractiveOff()

    def _add_grid_floor(self):
        grid_size = 20
        grid_step = 5.0  # 再次调大网格，减少几何体
        points = vtk.vtkPoints()
        lines = vtk.vtkCellArray()
        idx = 0
        half = grid_size * grid_step / 2.0
        for i in range(grid_size + 1):
            val = -half + i * grid_step
            points.InsertNextPoint(val, 0, -half)
            points.InsertNextPoint(val, 0, half)
            points.InsertNextPoint(-half, 0, val)
            points.InsertNextPoint(half, 0, val)
            for j in range(2):
                line = vtk.vtkLine()
                line.GetPointIds().SetId(0, idx)
                line.GetPointIds().SetId(1, idx + 1)
                lines.InsertNextCell(line)
                idx += 2
        grid_data = vtk.vtkPolyData()
        grid_data.SetPoints(points)
        grid_data.SetLines(lines)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(grid_data)
        self.grid_actor = vtk.vtkActor()
        self.grid_actor.SetMapper(mapper)
        self.grid_actor.GetProperty().SetColor(0.25, 0.27, 0.3)
        self.grid_actor.GetProperty().SetOpacity(0.2)
        self.renderer.AddActor(self.grid_actor)

    def _add_water_surface(self):
        self._water_points = vtk.vtkPoints()
        self._water_tcoords = vtk.vtkFloatArray()
        self._water_tcoords.SetNumberOfComponents(2)
        self._water_tcoords.SetName("TextureCoordinates")

        quad = vtk.vtkQuad()
        for idx in range(4):
            quad.GetPointIds().SetId(idx, idx)

        polys = vtk.vtkCellArray()
        polys.InsertNextCell(quad)

        self._water_polydata = vtk.vtkPolyData()
        self._water_polydata.SetPoints(self._water_points)
        self._water_polydata.SetPolys(polys)
        self._water_polydata.GetPointData().SetTCoords(self._water_tcoords)
        self._update_water_surface_geometry()

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(self._water_polydata)

        self._water_actor = vtk.vtkActor()
        self._water_actor.SetMapper(mapper)
        self._water_actor.PickableOff()
        prop = self._water_actor.GetProperty()
        prop.SetColor(0.34, 0.62, 0.82)
        prop.SetOpacity(0.58)
        prop.SetSpecular(0.45)
        prop.SetSpecularPower(32.0)
        prop.LightingOn()
        self.renderer.AddActor(self._water_actor)

    def _update_water_surface_geometry(self, bounds=None):
        if self._water_points is None or self._water_tcoords is None:
            return

        if bounds is None:
            center_x = 0.0
            center_z = 0.0
            water_y = 0.0
            size = self.WATER_DEFAULT_SIZE
        else:
            center_x = 0.5 * (bounds[0] + bounds[1])
            center_z = 0.5 * (bounds[4] + bounds[5])
            span_x = bounds[1] - bounds[0]
            span_z = bounds[5] - bounds[4]
            horizontal_span = max(span_x, span_z, self.WATER_DEFAULT_SIZE)
            water_y = bounds[2] - max(horizontal_span * 0.015, 0.1)
            size = horizontal_span * 2.6

        half = size * 0.5
        points = (
            (center_x - half, water_y, center_z - half),
            (center_x + half, water_y, center_z - half),
            (center_x + half, water_y, center_z + half),
            (center_x - half, water_y, center_z + half),
        )
        repeat = max(size / self.WATER_TILE_SIZE, 1.0)
        tcoords = ((0.0, 0.0), (repeat, 0.0), (repeat, repeat), (0.0, repeat))

        self._water_points.SetNumberOfPoints(4)
        self._water_tcoords.SetNumberOfTuples(4)
        for idx, point in enumerate(points):
            self._water_points.SetPoint(idx, *point)
            self._water_tcoords.SetTuple2(idx, *tcoords[idx])

        if self._water_polydata is not None:
            self._water_points.Modified()
            self._water_tcoords.Modified()
            self._water_polydata.Modified()

    def set_water_surface(self, texture_path="", visible=True):
        if self._water_actor is None:
            return False

        texture_loaded = self._set_water_texture(texture_path)
        self.set_water_surface_visible(visible, render=False)
        self.update_water_surface_to_scene(render=False)
        self.render_window.Render()
        return texture_loaded

    def set_water_surface_visible(self, visible=True, render=True):
        if self._water_actor is None:
            return
        self._water_actor.SetVisibility(bool(visible))
        if render:
            self.render_window.Render()

    def update_water_surface_to_scene(self, render=True, scene_bounds=None):
        if scene_bounds is None:
            scene_bounds = self._get_scene_model_bounds()
        self._update_water_surface_geometry(scene_bounds)
        if render:
            self.render_window.Render()

    def get_scene_model_bounds(self):
        return self._get_scene_model_bounds()

    def _set_water_texture(self, texture_path):
        water_actor = self._water_actor
        if water_actor is None:
            return False

        if not texture_path or not os.path.exists(texture_path):
            self._water_texture = None
            self._water_texture_reader = None
            water_actor.SetTexture(vtk.vtkTexture())
            self._performance_snapshot_time = 0.0
            return False

        reader_factory = vtk.vtkImageReader2Factory()
        reader = reader_factory.CreateImageReader2(texture_path)
        if reader is None:
            water_actor.SetTexture(vtk.vtkTexture())
            self._performance_snapshot_time = 0.0
            return False

        reader.SetFileName(texture_path)
        reader.Update()

        texture = vtk.vtkTexture()
        texture.SetInputConnection(reader.GetOutputPort())
        texture.InterpolateOn()
        texture.RepeatOn()

        self._water_texture_reader = reader
        self._water_texture = texture
        water_actor.SetTexture(texture)
        self._performance_snapshot_time = 0.0
        return True

    def _current_interactor_event_position(self):
        return self.interactor.GetEventPosition()

    def _pick_actor_name_at_event_position(self):
        # 取 VTK 交互器中的显示坐标，而不是直接使用 Qt 原始鼠标坐标。
        # 新方案在桥接层已经通过 _setEventInformation() 做过像素比缩放与 Y 轴翻转；
        # 若这里继续拿 Qt 坐标去 Pick，会导致拾取射线起点落在错误的屏幕位置。
        click_pos = self._current_interactor_event_position()
        self.picker.Pick(click_pos[0], click_pos[1], 0, self.renderer)
        actor = self.picker.GetActor()
        if actor:
            actor_ptr = getattr(actor, "__this__", "")
            base_name = self._actor_name_by_ptr.get(actor_ptr, "")
            if base_name:
                return self._selection_name_for_pick(base_name)
        return ""

    def _pick_highlighted_actor_name_at_event_position(self):
        if not self._selected_actor_names:
            return ""

        highlighted_base_names = []
        seen = set()
        for selected_name in self._selected_actor_names:
            target = self._selection_targets.get(selected_name)
            if not target:
                continue
            base_name = target[0]
            if base_name in seen or base_name not in self._actors:
                continue
            highlighted_base_names.append(base_name)
            seen.add(base_name)

        if not highlighted_base_names:
            return ""

        click_pos = self._current_interactor_event_position()
        self.picker.InitializePickList()
        for base_name in highlighted_base_names:
            self.picker.AddPickList(self._actors[base_name])
        self.picker.PickFromListOn()
        try:
            self.picker.Pick(click_pos[0], click_pos[1], 0, self.renderer)
            actor = self.picker.GetActor()
            if actor:
                actor_ptr = getattr(actor, "__this__", "")
                base_name = self._actor_name_by_ptr.get(actor_ptr, "")
                if base_name:
                    return self._selection_name_for_pick(base_name)
            return ""
        finally:
            self.picker.PickFromListOff()
            self.picker.InitializePickList()

    def _emit_model_clicked(self, clicked_name):
        self.model_clicked.emit(clicked_name)

    def _emit_model_hovered(self, hovered_name, global_pos=None):
        self.model_hovered.emit(hovered_name, global_pos)

    def _clear_highlight_hover(self):
        if not self._hovered_highlight_name:
            return
        self._hovered_highlight_name = ""
        self._emit_model_hovered("", None)

    def _handle_viewport_left_click(self):
        self._emit_model_clicked(self._pick_actor_name_at_event_position())

    def _handle_viewport_hover(self, global_pos=None):
        if not self._selected_actor_names:
            self._clear_highlight_hover()
            return

        hovered_name = self._pick_highlighted_actor_name_at_event_position()
        if hovered_name not in self._selected_actor_names:
            self._clear_highlight_hover()
            return

        self._hovered_highlight_name = hovered_name
        self._emit_model_hovered(hovered_name, global_pos)

    def _handle_viewport_hover_leave(self):
        self._clear_highlight_hover()

    def _zoom_factor(self):
        return 1.2

    def _set_camera_distance(self, camera, distance):
        focal_point = camera.GetFocalPoint()
        position = camera.GetPosition()
        direction = self._normalize_vector(
            (
                position[0] - focal_point[0],
                position[1] - focal_point[1],
                position[2] - focal_point[2],
            )
        )
        camera.SetPosition(
            focal_point[0] + direction[0] * distance,
            focal_point[1] + direction[1] * distance,
            focal_point[2] + direction[2] * distance,
        )

    def _clamp_camera_distance(self, camera):
        if camera.GetDistance() > self._camera_max_distance:
            self._set_camera_distance(camera, self._camera_max_distance)

    def _zoom_parallel_camera(self, camera, direction, zoom_factor):
        scale = camera.GetParallelScale()
        if direction > 0:
            camera.SetParallelScale(max(scale / zoom_factor, 1e-6))
        else:
            camera.SetParallelScale(scale * zoom_factor)

    def _zoom_perspective_camera(self, camera, direction, zoom_factor):
        if direction > 0:
            camera.Dolly(zoom_factor)
        else:
            camera.Dolly(1.0 / zoom_factor)
            self._clamp_camera_distance(camera)

    def _finalize_camera_zoom(self):
        self.renderer.ResetCameraClippingRange()
        self.render_window.Render()

    def _handle_viewport_wheel(self, direction):
        camera = self.renderer.GetActiveCamera()
        if camera is None:
            return

        zoom_factor = self._zoom_factor()
        if camera.GetParallelProjection():
            self._zoom_parallel_camera(camera, direction, zoom_factor)
        else:
            self._zoom_perspective_camera(camera, direction, zoom_factor)

        self._finalize_camera_zoom()

    def _on_left_button_press(self, obj, event):
        # 旧方案保留：如果未来重新启用 VTK 左键事件拾取，可继续复用该入口。
        self._handle_viewport_left_click()

    def _selection_name_for_pick(self, base_name):
        dataset = (
            self.picker.GetDataSet() if hasattr(self.picker, "GetDataSet") else None
        )
        dataset_ptr = getattr(dataset, "__this__", "") if dataset is not None else ""
        if dataset_ptr:
            alias_name = self._base_to_dataset_alias.get(base_name, {}).get(dataset_ptr)
            if alias_name:
                return alias_name

        if hasattr(self.picker, "GetFlatBlockIndex"):
            try:
                flat_index = int(self.picker.GetFlatBlockIndex())
            except (TypeError, ValueError):
                flat_index = -1
            alias_name = self._base_to_flat_alias.get(base_name, {}).get(flat_index)
            if alias_name:
                return alias_name

        return base_name

    def _on_start_interaction(self, obj, event):
        self._interaction_active = True
        if not self._dynamic_hide_enabled or self._dynamic_hide_active:
            return
        if len(self._actors) < self.DYNAMIC_HIDE_MIN_ACTOR_COUNT:
            return
        camera = self.renderer.GetActiveCamera()
        if camera is None:
            return

        selected_bases = {
            target[0]
            for selected_name in self._selected_actor_names
            for target in [self._selection_targets.get(selected_name)]
            if target
        }

        self._dynamic_hidden_actor_names.clear()
        for name, actor in self._actors.items():
            if name in selected_bases or not actor.GetVisibility():
                continue
            if self._should_temporarily_hide_actor(name, camera):
                actor.SetVisibility(False)
                self._dynamic_hidden_actor_names.add(name)

        self._dynamic_hide_active = bool(self._dynamic_hidden_actor_names)
        self._invisible_actor_count = len(self._dynamic_hidden_actor_names)

    def _on_end_interaction(self, obj, event):
        self._interaction_active = False
        if not self._dynamic_hide_active:
            self._invisible_actor_count = len(self._dynamic_hidden_actor_names)
            return
        for name in list(self._dynamic_hidden_actor_names):
            actor = self._actors.get(name)
            if actor is not None:
                actor.SetVisibility(True)
        self._dynamic_hidden_actor_names.clear()
        self._dynamic_hide_active = False
        self._invisible_actor_count = 0
        self.render_window.Render()

    def _should_temporarily_hide_actor(self, base_name, camera):
        sphere = self._actor_sphere.get(base_name)
        if sphere is None:
            return False
        cx, cy, cz, radius = sphere
        if radius <= 0.0:
            return False
        cam_pos = camera.GetPosition()
        dx = cx - cam_pos[0]
        dy = cy - cam_pos[1]
        dz = cz - cam_pos[2]
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        if dist <= radius * 1.05:
            return False

        angular = 2.0 * math.atan2(radius, dist)
        if angular >= self.DYNAMIC_HIDE_MAX_ANGULAR_SIZE_RAD:
            return False
        _, cells = self._actor_geometry.get(base_name, (0, 0))
        return cells <= self.DYNAMIC_HIDE_MAX_CELLS

    def initialize(self):
        self.interactor.Initialize()

    def set_scene_metadata(self, file_path="", logical_object_count=0):
        normalized_path = os.path.normpath(file_path) if file_path else ""
        try:
            file_size_bytes = os.path.getsize(normalized_path) if normalized_path else 0
        except OSError:
            file_size_bytes = 0

        self._scene_metadata = {
            "file_path": normalized_path,
            "file_format": os.path.splitext(normalized_path)[1].lstrip(".").lower(),
            "file_size_bytes": file_size_bytes,
            "logical_object_count": max(0, int(logical_object_count or 0)),
        }
        self._invisible_actor_count = 0
        self._performance_snapshot = {}
        self._performance_snapshot_time = 0.0

    def _actor_has_any_texture(self, actor):
        if actor is None:
            return False

        if actor.GetTexture() is not None:
            return True

        prop = actor.GetProperty()
        if prop is None:
            return False

        # VTK glTF importer may store textures on vtkProperty texture map.
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

    def add_actor(self, actor, name="model", add_to_renderer=True):
        """根据数据规模选择普通 Actor 或 LODActor。"""
        mapper = actor.GetMapper()
        data_obj = mapper.GetInputDataObject(0, 0) if mapper else None
        has_texture = self._actor_has_any_texture(actor)
        use_lod = (
            self.ENABLE_LOD_ACTOR
            and add_to_renderer
            and mapper is not None
            and data_obj is not None
            and not has_texture
            and data_obj.IsA("vtkPolyData")
            and data_obj.GetNumberOfPoints() >= self.LOD_POINT_THRESHOLD
        )

        if mapper and hasattr(mapper, "SetStatic"):
            mapper.SetStatic(True)

        render_actor = actor
        if use_lod:
            lod_actor = vtk.vtkLODActor()
            lod_actor.SetMapper(mapper)
            lod_actor.SetProperty(actor.GetProperty())
            lod_actor.SetNumberOfCloudPoints(12000)
            enable_auto_lod = getattr(lod_actor, "AutomaticLODSelectionOn", None)
            if callable(enable_auto_lod):
                enable_auto_lod()
            render_actor = lod_actor

        render_actor.GetProperty().BackfaceCullingOn()
        self._actors[name] = render_actor
        actor_ptr = getattr(render_actor, "__this__", "")
        if actor_ptr:
            self._actor_name_by_ptr[actor_ptr] = name
        self._actor_geometry[name] = self._count_data_object_geometry(data_obj)
        self._actor_sphere[name] = self._compute_actor_bounding_sphere(render_actor)
        self._actor_base_color[name] = tuple(render_actor.GetProperty().GetColor())
        self._selection_targets[name] = (name, None)
        self._selection_geometry[name] = self._actor_geometry[name]
        self._selection_base_color[name] = self._actor_base_color[name]
        bounds = render_actor.GetBounds()
        if self._valid_bounds(bounds):
            self._selection_world_bounds[name] = tuple(float(value) for value in bounds)
        self._performance_snapshot = {}
        self._performance_snapshot_time = 0.0
        if add_to_renderer:
            self.renderer.AddActor(render_actor)

    def register_actor_alias(
        self,
        alias_name,
        base_name,
        flat_block_index,
        points=0,
        cells=0,
        base_color=None,
        world_bounds=None,
        dataset_ptr="",
    ):
        if base_name not in self._actors:
            return
        self._selection_targets[alias_name] = (base_name, flat_block_index)
        self._selection_geometry[alias_name] = (points, cells)
        self._selection_base_color[alias_name] = (
            tuple(base_color) if base_color is not None else None
        )
        if self._valid_bounds(world_bounds):
            self._selection_world_bounds[alias_name] = tuple(
                float(value) for value in world_bounds
            )
        self._base_to_flat_alias.setdefault(base_name, {})[
            flat_block_index
        ] = alias_name
        if dataset_ptr:
            self._base_to_dataset_alias.setdefault(base_name, {})[
                dataset_ptr
            ] = alias_name

    def remove_actor(self, name):
        if name in self._actors:
            if (
                name in self._focus_selected_base_names
                or name in self._focus_dimmed_actor_state
            ):
                self.clear_selection_focus()
            selected_names = [
                selected_name
                for selected_name in self._selected_actor_names
                if self._selection_targets.get(selected_name, ("", None))[0] == name
            ]
            for selected_name in selected_names:
                self._clear_highlight(selected_name)
            if selected_names:
                self._selected_actor_names = [
                    selected_name
                    for selected_name in self._selected_actor_names
                    if selected_name not in selected_names
                ]
                self._selected_actor_name = (
                    self._selected_actor_names[0]
                    if self._selected_actor_names
                    else None
                )
            render_actor = self._actors[name]
            actor_ptr = getattr(render_actor, "__this__", "")
            if actor_ptr:
                self._actor_name_by_ptr.pop(actor_ptr, None)
            self.renderer.RemoveActor(render_actor)
            del self._actors[name]
            self._actor_geometry.pop(name, None)
            self._actor_sphere.pop(name, None)
            self._actor_base_color.pop(name, None)
            self._highlight_actor_prop_state.pop(name, None)
            self._dynamic_hidden_actor_names.discard(name)
            self._invisible_actor_count = len(self._dynamic_hidden_actor_names)
            self._performance_snapshot = {}
            self._performance_snapshot_time = 0.0
            self._remove_selection_entries_for_base(name)

    def clear_scene(self):
        self._clear_highlight_hover()
        self.clear_selection_focus()
        for selected_name in self._selected_actor_names:
            self._clear_highlight(selected_name)
        self._remove_all_highlight_overlays()
        for actor in self._actors.values():
            self.renderer.RemoveActor(actor)
        self._actors.clear()
        self._actor_name_by_ptr.clear()
        self._actor_geometry.clear()
        self._actor_sphere.clear()
        self._actor_base_color.clear()
        self._selection_targets.clear()
        self._selection_geometry.clear()
        self._selection_base_color.clear()
        self._selection_world_bounds.clear()
        self._base_to_flat_alias.clear()
        self._base_to_dataset_alias.clear()
        self._dynamic_hidden_actor_names.clear()
        self._dynamic_hide_active = False
        self._invisible_actor_count = 0
        self._focus_dimmed_actor_state.clear()
        self._focus_dimmed_base_names.clear()
        self._focus_selected_base_names.clear()
        self._selected_actor_name = None
        self._selected_actor_names = []
        self._highlight_overlay_actors.clear()
        self._highlight_actor_prop_state.clear()
        self._performance_snapshot = {}
        self._performance_snapshot_time = 0.0
        self.update_water_surface_to_scene(render=False)

    def highlight_actor(self, name):
        self.highlight_actors([name] if name else [])

    def highlight_actors(self, names):
        self._clear_highlight_hover()
        for selected_name in self._selected_actor_names:
            self._clear_highlight(selected_name)

        unique_names = []
        seen = set()
        for name in names:
            if not name or name in seen or name not in self._selection_targets:
                continue
            unique_names.append(name)
            seen.add(name)

        self._selected_actor_names = unique_names
        self._selected_actor_name = unique_names[0] if unique_names else None
        for name in unique_names:
            self._apply_highlight(name)
        self.render_window.Render()

    def highlighted_names(self):
        return list(self._selected_actor_names)

    def selection_center(self, name):
        bounds = self._bounds_for_selection([name])
        if bounds is None:
            return None

        center, _ = self._bounds_center_radius(bounds)
        return center

    def focus_selection(self, names):
        unique_names = self._valid_selection_names(names)
        if not unique_names:
            self.clear_selection_focus(render=True)
            return False

        selected_bases = self._base_names_for_selection(unique_names)
        bounds = self._bounds_for_selection(unique_names)
        if bounds is None:
            self.clear_selection_focus(render=True)
            return False

        self._apply_selection_focus_dim(selected_bases)
        self._frame_bounds_from_best_view(bounds, padding=self.FOCUS_FRAME_PADDING)
        self.render_window.Render()
        return True

    def set_selection_focus_dim(self, names, render=False):
        unique_names = self._valid_selection_names(names)
        if not unique_names:
            self.clear_selection_focus(render=render)
            return False

        selected_bases = self._base_names_for_selection(unique_names)
        if not selected_bases:
            self.clear_selection_focus(render=render)
            return False

        self._apply_selection_focus_dim(selected_bases)
        if render:
            self.render_window.Render()
        return True

    def set_rotation_focus_to_selection(self, names):
        unique_names = self._valid_selection_names(names)
        if not unique_names:
            return False

        bounds = self._bounds_for_selection(unique_names)
        if bounds is None:
            return False

        center, _ = self._bounds_center_radius(bounds)
        return self.set_rotation_focus(center)

    def set_rotation_focus(self, focal_point):
        if focal_point is None:
            return False

        camera = self.renderer.GetActiveCamera()
        if camera is None:
            return False

        focal_point = tuple(float(value) for value in focal_point)
        current_focal_point = camera.GetFocalPoint()
        current_position = camera.GetPosition()
        delta = (
            focal_point[0] - current_focal_point[0],
            focal_point[1] - current_focal_point[1],
            focal_point[2] - current_focal_point[2],
        )

        camera.SetFocalPoint(*focal_point)
        camera.SetPosition(
            current_position[0] + delta[0],
            current_position[1] + delta[1],
            current_position[2] + delta[2],
        )
        scene_bounds = self._get_scene_model_bounds()
        if scene_bounds is None:
            self.renderer.ResetCameraClippingRange()
        else:
            self.renderer.ResetCameraClippingRange(scene_bounds)
        self.render_window.Render()
        return True

    def clear_selection_focus(self, render=False):
        if not self._focus_dimmed_actor_state and not self._focus_selected_base_names:
            return
        self._restore_focus_dimmed_actors()
        self._focus_selected_base_names.clear()
        if render:
            self.render_window.Render()

    def _valid_selection_names(self, names):
        unique_names = []
        seen = set()
        for name in names:
            if not name or name in seen or name not in self._selection_targets:
                continue
            unique_names.append(name)
            seen.add(name)
        return unique_names

    def _base_names_for_selection(self, names):
        return {
            target[0]
            for name in names
            for target in [self._selection_targets.get(name)]
            if target and target[0] in self._actors
        }

    def _apply_selection_focus_dim(self, selected_bases):
        selected_bases = set(selected_bases)
        current_dimmed = set(self._focus_dimmed_base_names)
        target_dimmed = {
            base_name
            for base_name in self._actors.keys()
            if base_name not in selected_bases
        }

        names_to_restore = current_dimmed - target_dimmed
        names_to_dim = target_dimmed - current_dimmed

        self._restore_focus_dimmed_actors(names_to_restore)
        self._focus_selected_base_names = set(selected_bases)
        if not names_to_dim:
            return

        for base_name in names_to_dim:
            actor = self._actors.get(base_name)
            if actor is None:
                continue
            prop = actor.GetProperty()
            self._focus_dimmed_actor_state[base_name] = prop.GetOpacity()
            prop.SetOpacity(self.FOCUS_DIM_OPACITY)
            self._focus_dimmed_base_names.add(base_name)

    def _restore_focus_dimmed_actors(self, base_names=None):
        restore_names = (
            set(base_names)
            if base_names is not None
            else set(self._focus_dimmed_base_names)
        )
        for base_name in restore_names:
            opacity = self._focus_dimmed_actor_state.pop(base_name, None)
            actor = self._actors.get(base_name)
            if actor is not None and opacity is not None:
                actor.GetProperty().SetOpacity(opacity)
            self._focus_dimmed_base_names.discard(base_name)

    def _camera_pose_iso(self):
        return (1.0, 1.0, 1.0), (0.0, 1.0, 0.0)

    def _camera_pose_front(self):
        return (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)

    def _camera_pose_top(self):
        # 近俯视：主方向沿世界 Y 轴，并保留极小 Z 偏移以维持 Y-up 的可观察投影。
        # 这不是严格 0 度正俯视，而是为避免 Y 轴与视线完全共线而采用的稳定近似值。
        return (0.0, 0.9998, 0.0209), (0.0, 1.0, 0.0)

    def _camera_pose_right(self):
        return (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)

    def _apply_camera_pose(self, direction, view_up, scene_bounds=None):
        self._reset_camera_to_direction(direction, view_up, scene_bounds=scene_bounds)
        self.render_window.Render()

    def reset_camera(self, scene_bounds=None):
        self.clear_selection_focus()
        self._apply_camera_pose(*self._camera_pose_iso(), scene_bounds=scene_bounds)

    def set_view_front(self):
        self._apply_camera_pose(*self._camera_pose_front())

    def set_view_top(self):
        self._apply_camera_pose(*self._camera_pose_top())

    def set_view_right(self):
        self._apply_camera_pose(*self._camera_pose_right())

    def set_view_iso(self):
        self.reset_camera()

    def set_camera_pose(
        self,
        focal_point,
        position=None,
        direction=None,
        distance=None,
        view_up=(0.0, 1.0, 0.0),
        clipping_bounds=None,
    ):
        if focal_point is None:
            return False

        camera = self.renderer.GetActiveCamera()
        if camera is None:
            return False

        focal_point = tuple(float(value) for value in focal_point)
        position = tuple(float(value) for value in position) if position else None
        direction = tuple(float(value) for value in direction) if direction else None
        view_up = tuple(float(value) for value in (view_up or (0.0, 1.0, 0.0)))

        use_direction = position is None or self._points_are_close(
            position, focal_point
        )
        if use_direction:
            if direction is None:
                return False
            direction = self._normalize_vector(direction)
            distance = (
                float(distance)
                if distance and distance > 0
                else self._camera_max_distance
            )
            position = (
                focal_point[0] + direction[0] * distance,
                focal_point[1] + direction[1] * distance,
                focal_point[2] + direction[2] * distance,
            )

        if position is None:
            return False

        camera.SetViewAngle(self.CAMERA_VIEW_ANGLE_DEG)
        camera.SetFocalPoint(*focal_point)
        camera.SetPosition(*position)
        camera.SetViewUp(*view_up)
        if clipping_bounds is None:
            self.renderer.ResetCameraClippingRange()
        else:
            self.renderer.ResetCameraClippingRange(clipping_bounds)
        self.render_window.Render()
        return True

    def _points_are_close(self, point_a, point_b, epsilon=1e-6):
        return all(abs(a - b) <= epsilon for a, b in zip(point_a, point_b))

    def _reset_camera_to_direction(self, direction, view_up, scene_bounds=None):
        bounds = (
            scene_bounds if scene_bounds is not None else self._get_scene_model_bounds()
        )
        if bounds is None:
            self.renderer.ResetCamera()
            return
        self._reset_camera_to_bounds(bounds, direction, view_up)

    def _reset_camera_to_bounds(
        self, bounds, direction, view_up, padding=None, clipping_bounds=None
    ):
        if padding is None:
            padding = self.CAMERA_FRAME_PADDING
        center, radius = self._bounds_center_radius(bounds)
        direction = self._normalize_vector(direction)
        view_angle_rad = math.radians(self.CAMERA_VIEW_ANGLE_DEG)
        distance = min(
            max(radius * padding / math.sin(view_angle_rad / 2.0), 1.0),
            self._camera_max_distance,
        )

        camera = self.renderer.GetActiveCamera()
        camera.SetViewAngle(self.CAMERA_VIEW_ANGLE_DEG)
        camera.SetFocalPoint(*center)
        camera.SetPosition(
            center[0] + direction[0] * distance,
            center[1] + direction[1] * distance,
            center[2] + direction[2] * distance,
        )
        camera.SetViewUp(*view_up)
        # 注意：这里暂时不调用 camera.OrthogonalizeViewUp()。
        # 原因是当前俯视调参阶段需要保留传入的原始 view_up 语义，便于通过调试框观察输入效果；
        # 若启用正交化，VTK 会把 view_up 投影到与视线垂直的平面上，导致观测值被几何修正。
        camera.SetRoll(0.0)
        self.renderer.ResetCameraClippingRange(clipping_bounds or bounds)
        if camera.GetParallelProjection():
            camera.SetParallelScale(radius * padding)

    def _frame_bounds_from_current_view(self, bounds, padding=None):
        camera = self.renderer.GetActiveCamera()
        if camera is None:
            self._reset_camera_to_bounds(
                bounds,
                *self._camera_pose_iso(),
                padding=padding,
                clipping_bounds=self._get_scene_model_bounds() or bounds,
            )
            return

        direction = self._normalize_vector(
            (
                camera.GetPosition()[0] - camera.GetFocalPoint()[0],
                camera.GetPosition()[1] - camera.GetFocalPoint()[1],
                camera.GetPosition()[2] - camera.GetFocalPoint()[2],
            )
        )
        view_up = self._normalize_vector(camera.GetViewUp())
        self._reset_camera_to_bounds(
            bounds,
            direction,
            view_up,
            padding=padding,
            clipping_bounds=self._get_scene_model_bounds() or bounds,
        )

    def _frame_bounds_from_best_view(self, bounds, padding=None):
        scene_bounds = self._get_scene_model_bounds()
        direction, view_up = self._best_focus_camera_pose(bounds, scene_bounds)
        self._reset_camera_to_bounds(
            bounds,
            direction,
            view_up,
            padding=padding,
            clipping_bounds=scene_bounds or bounds,
        )

    def _best_focus_camera_pose(self, focus_bounds, scene_bounds):
        focus_center, _ = self._bounds_center_radius(focus_bounds)
        if scene_bounds is None:
            return self._camera_pose_iso()

        scene_center, _ = self._bounds_center_radius(scene_bounds)
        offset_x = focus_center[0] - scene_center[0]
        offset_z = focus_center[2] - scene_center[2]
        horizontal_length = math.sqrt(offset_x * offset_x + offset_z * offset_z)
        scene_width = max(
            scene_bounds[1] - scene_bounds[0],
            scene_bounds[5] - scene_bounds[4],
        )

        if horizontal_length <= max(scene_width * 0.06, 1e-6):
            return self._camera_pose_iso()

        outward_x = offset_x / horizontal_length
        outward_z = offset_z / horizontal_length
        return self._normalize_vector((outward_x, 0.42, outward_z)), (0.0, 1.0, 0.0)

    def _get_scene_model_bounds(self):
        visible_bounds = []
        for actor in self._actors.values():
            if not actor.GetVisibility():
                continue
            bounds = actor.GetBounds()
            if (
                bounds
                and bounds[0] <= bounds[1]
                and bounds[2] <= bounds[3]
                and bounds[4] <= bounds[5]
            ):
                visible_bounds.append(bounds)

        if not visible_bounds:
            return None

        return (
            min(bounds[0] for bounds in visible_bounds),
            max(bounds[1] for bounds in visible_bounds),
            min(bounds[2] for bounds in visible_bounds),
            max(bounds[3] for bounds in visible_bounds),
            min(bounds[4] for bounds in visible_bounds),
            max(bounds[5] for bounds in visible_bounds),
        )

    def _bounds_for_selection(self, names):
        selection_bounds = []
        for name in names:
            target = self._selection_targets.get(name)
            if not target:
                continue
            base_name, flat_index = target
            actor = self._actors.get(base_name)
            if actor is None:
                continue

            bounds = self._bounds_for_selection_target(name, actor, flat_index)
            if self._valid_bounds(bounds):
                selection_bounds.append(bounds)

        return self._union_bounds(selection_bounds)

    def _bounds_for_selection_target(self, selection_name, actor, flat_index):
        cached_bounds = self._selection_world_bounds.get(selection_name)
        if self._valid_bounds(cached_bounds):
            return cached_bounds

        data_obj = self._highlight_data_object(actor, flat_index)
        if data_obj is not None:
            try:
                local_bounds = data_obj.GetBounds()
            except TypeError:
                local_bounds = None
            transformed_bounds = self._transform_bounds(local_bounds, actor.GetMatrix())
            if self._valid_bounds(transformed_bounds):
                return transformed_bounds

        return actor.GetBounds()

    def _valid_bounds(self, bounds):
        return (
            bounds
            and len(bounds) == 6
            and bounds[0] <= bounds[1]
            and bounds[2] <= bounds[3]
            and bounds[4] <= bounds[5]
        )

    def _union_bounds(self, bounds_list):
        bounds_list = [bounds for bounds in bounds_list if self._valid_bounds(bounds)]
        if not bounds_list:
            return None
        return (
            min(bounds[0] for bounds in bounds_list),
            max(bounds[1] for bounds in bounds_list),
            min(bounds[2] for bounds in bounds_list),
            max(bounds[3] for bounds in bounds_list),
            min(bounds[4] for bounds in bounds_list),
            max(bounds[5] for bounds in bounds_list),
        )

    def _transform_bounds(self, bounds, matrix):
        if not self._valid_bounds(bounds) or matrix is None:
            return bounds

        points = []
        for x in (bounds[0], bounds[1]):
            for y in (bounds[2], bounds[3]):
                for z in (bounds[4], bounds[5]):
                    points.append(matrix.MultiplyPoint((x, y, z, 1.0)))

        return (
            min(point[0] for point in points),
            max(point[0] for point in points),
            min(point[1] for point in points),
            max(point[1] for point in points),
            min(point[2] for point in points),
            max(point[2] for point in points),
        )

    def _bounds_center_radius(self, bounds):
        center = (
            0.5 * (bounds[0] + bounds[1]),
            0.5 * (bounds[2] + bounds[3]),
            0.5 * (bounds[4] + bounds[5]),
        )
        radius = 0.5 * math.sqrt(
            (bounds[1] - bounds[0]) ** 2
            + (bounds[3] - bounds[2]) ** 2
            + (bounds[5] - bounds[4]) ** 2
        )
        return center, max(radius, 1.0)

    def _normalize_vector(self, vector):
        length = math.sqrt(sum(component * component for component in vector))
        if length <= 0.0:
            return (0.0, 1.0, 0.0)
        return tuple(component / length for component in vector)

    def toggle_wireframe(self, wireframe=True):
        for actor in self._actors.values():
            if wireframe:
                actor.GetProperty().SetRepresentationToWireframe()
            else:
                actor.GetProperty().SetRepresentationToSurface()
        self.render_window.Render()

    def toggle_grid(self, visible=True):
        self.render_window.Render()

    def refresh(self):
        self.render_window.Render()

    def get_actor_info(self, name):
        if name not in self._selection_targets:
            return None
        base_name, _ = self._selection_targets[name]
        actor = self._actors.get(base_name)
        if actor is None:
            return None
        mapper = actor.GetMapper()
        if mapper:
            points, cells = self._selection_geometry.get(
                name, self._actor_geometry.get(base_name, (0, 0))
            )
            bounds = actor.GetBounds()
            info = {
                "顶点数": points,
                "面片数": cells,
                "X尺寸": f"{bounds[1] - bounds[0]:.2f}",
                "Y尺寸": f"{bounds[3] - bounds[2]:.2f}",
                "Z尺寸": f"{bounds[5] - bounds[4]:.2f}",
            }
            return info
        return None

    def _remove_selection_entries_for_base(self, base_name):
        keys_to_remove = [
            key for key, val in self._selection_targets.items() if val[0] == base_name
        ]
        for key in keys_to_remove:
            self._selection_targets.pop(key, None)
            self._selection_geometry.pop(key, None)
            self._selection_base_color.pop(key, None)
            self._selection_world_bounds.pop(key, None)
        self._base_to_flat_alias.pop(base_name, None)
        self._base_to_dataset_alias.pop(base_name, None)

    def _apply_highlight(self, selection_name):
        target = self._selection_targets.get(selection_name)
        if not target:
            return
        base_name, flat_index = target
        actor = self._actors.get(base_name)
        if actor is None:
            return
        self._restore_selection_block_color(selection_name)
        overlay_actor = self._create_highlight_overlay_actor(actor, flat_index)
        if overlay_actor is not None:
            self._highlight_overlay_actors[selection_name] = overlay_actor
            self.renderer.AddActor(overlay_actor)
            return
        if flat_index is None:
            self._save_actor_highlight_prop_state(base_name, actor)
            actor.GetProperty().SetEdgeVisibility(True)
            actor.GetProperty().SetEdgeColor(1.0, 0.6, 0.0)
            actor.GetProperty().SetEdgeOpacity(1.0)
            actor.GetProperty().SetLineWidth(1.5)
            return
        # Do not recolor composite sub-blocks as a fallback: SetBlockColor can
        # override the original material/texture and make the highlighted device
        # look like it lost its material.

    def _clear_highlight(self, selection_name):
        overlay_actor = self._highlight_overlay_actors.pop(selection_name, None)
        if overlay_actor is not None:
            self.renderer.RemoveActor(overlay_actor)
            return

        target = self._selection_targets.get(selection_name)
        if not target:
            return
        base_name, flat_index = target
        actor = self._actors.get(base_name)
        if actor is None:
            return
        if flat_index is None:
            self._restore_actor_highlight_prop_state(base_name, actor)
            return
        self._restore_selection_block_color(selection_name)

    def _remove_all_highlight_overlays(self):
        for overlay_actor in self._highlight_overlay_actors.values():
            self.renderer.RemoveActor(overlay_actor)
        self._highlight_overlay_actors.clear()

    def _save_actor_highlight_prop_state(self, base_name, actor):
        if base_name in self._highlight_actor_prop_state:
            return
        prop = actor.GetProperty()
        self._highlight_actor_prop_state[base_name] = {
            "edge_visibility": prop.GetEdgeVisibility(),
            "edge_color": tuple(prop.GetEdgeColor()),
            "edge_opacity": prop.GetEdgeOpacity(),
            "line_width": prop.GetLineWidth(),
        }

    def _restore_actor_highlight_prop_state(self, base_name, actor):
        state = self._highlight_actor_prop_state.pop(base_name, None)
        if state is None:
            return
        prop = actor.GetProperty()
        prop.SetEdgeVisibility(state["edge_visibility"])
        prop.SetEdgeColor(*state["edge_color"])
        prop.SetEdgeOpacity(state["edge_opacity"])
        prop.SetLineWidth(state["line_width"])

    def _restore_selection_block_color(self, selection_name):
        target = self._selection_targets.get(selection_name)
        if not target:
            return
        base_name, flat_index = target
        if flat_index is None:
            return
        actor = self._actors.get(base_name)
        mapper = actor.GetMapper() if actor is not None else None
        color = self._selection_base_color.get(selection_name)
        if mapper is None:
            return
        if color is None:
            remove_block_color = getattr(mapper, "RemoveBlockColor", None)
            if callable(remove_block_color):
                remove_block_color(flat_index)
            return
        if not hasattr(mapper, "SetBlockColor"):
            return
        mapper.SetBlockColor(flat_index, *color)

    def _create_highlight_overlay_actor(self, actor, flat_index):
        data_obj = self._highlight_data_object(actor, flat_index)
        if data_obj is None:
            return None

        mapper = self._highlight_outline_mapper_for_data(data_obj)
        if mapper is None:
            return None

        overlay_actor = vtk.vtkActor()
        overlay_actor.SetMapper(mapper)
        overlay_actor.PickableOff()

        matrix = vtk.vtkMatrix4x4()
        matrix.DeepCopy(actor.GetMatrix())
        overlay_actor.SetUserMatrix(matrix)

        prop = overlay_actor.GetProperty()
        prop.SetColor(1.0, 0.45, 0.45)
        prop.SetOpacity(1.0)
        prop.SetLineWidth(2.0)
        prop.LightingOff()
        if hasattr(prop, "SetRenderLinesAsTubes"):
            prop.SetRenderLinesAsTubes(True)
        return overlay_actor

    def _highlight_data_object(self, actor, flat_index):
        mapper = actor.GetMapper()
        if mapper is None:
            return None
        data_obj = mapper.GetInputDataObject(0, 0)
        if (
            flat_index is None
            or data_obj is None
            or not data_obj.IsA("vtkCompositeDataSet")
        ):
            return data_obj

        iterator = data_obj.NewIterator()
        iterator.InitTraversal()
        while not iterator.IsDoneWithTraversal():
            if iterator.GetCurrentFlatIndex() == flat_index:
                return iterator.GetCurrentDataObject()
            iterator.GoToNextItem()
        return None

    def _highlight_outline_mapper_for_data(self, data_obj):
        if data_obj is None:
            return None

        silhouette = vtk.vtkPolyDataSilhouette()
        silhouette.SetCamera(self.renderer.GetActiveCamera())
        silhouette.SetEnableFeatureAngle(False)
        silhouette.BorderEdgesOn()

        if data_obj.IsA("vtkPolyData"):
            silhouette.SetInputData(data_obj)
            pipeline_refs = (silhouette,)
        elif data_obj.IsA("vtkCompositeDataSet"):
            geometry_filter = vtk.vtkCompositeDataGeometryFilter()
            geometry_filter.SetInputDataObject(data_obj)
            silhouette.SetInputConnection(geometry_filter.GetOutputPort())
            pipeline_refs = (geometry_filter, silhouette)
        elif data_obj.IsA("vtkDataSet"):
            geometry_filter = vtk.vtkGeometryFilter()
            geometry_filter.SetInputData(data_obj)
            silhouette.SetInputConnection(geometry_filter.GetOutputPort())
            pipeline_refs = (geometry_filter, silhouette)
        else:
            return None

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(silhouette.GetOutputPort())
        mapper.ScalarVisibilityOff()
        setattr(mapper, "_highlight_pipeline_refs", pipeline_refs)
        if hasattr(mapper, "SetStatic"):
            mapper.SetStatic(False)
        return mapper

    def _compute_actor_bounding_sphere(self, actor):
        bounds = actor.GetBounds()
        if bounds is None:
            return None
        if bounds[0] > bounds[1] or bounds[2] > bounds[3] or bounds[4] > bounds[5]:
            return None
        cx = 0.5 * (bounds[0] + bounds[1])
        cy = 0.5 * (bounds[2] + bounds[3])
        cz = 0.5 * (bounds[4] + bounds[5])
        rx = 0.5 * (bounds[1] - bounds[0])
        ry = 0.5 * (bounds[3] - bounds[2])
        rz = 0.5 * (bounds[5] - bounds[4])
        radius = math.sqrt(rx * rx + ry * ry + rz * rz)
        return (cx, cy, cz, radius)

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

    def screenshot(self, filepath):
        w2i = vtk.vtkWindowToImageFilter()
        w2i.SetInput(self.render_window)
        w2i.Update()
        writer = vtk.vtkPNGWriter()
        writer.SetFileName(filepath)
        writer.SetInputConnection(w2i.GetOutputPort())
        writer.Write()
