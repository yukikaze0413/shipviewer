"""
VTK渲染窗口组件
将VTK渲染器集成到PySide6 QWidget中
"""
import math

import vtk
from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor
from PySide6.QtWidgets import QWidget, QVBoxLayout
from PySide6.QtCore import Signal


class VTKWidget(QWidget):
    """基于VTK的3D视口组件 - 极致性能优化版 (针对百万面级模型)"""
    LOD_POINT_THRESHOLD = 200_000
    DYNAMIC_HIDE_MIN_ACTOR_COUNT = 800
    DYNAMIC_HIDE_MAX_ANGULAR_SIZE_RAD = 0.01
    DYNAMIC_HIDE_MAX_CELLS = 40_000

    # 信号
    model_clicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._actors = {}  # base_name -> vtkActor/vtkLODActor
        self._actor_geometry = {}  # name -> (points, cells)
        self._actor_base_color = {}  # base_name -> (r, g, b)
        self._selection_targets = {}  # sel_name -> (base_name, flat_block_index or None)
        self._selection_geometry = {}  # sel_name -> (points, cells)
        self._selection_base_color = {}  # sel_name -> (r, g, b)
        self._base_to_flat_alias = {}  # base_name -> {flat_block_index: sel_name}
        self._base_to_dataset_alias = {}  # base_name -> {dataset_ptr: sel_name}
        self._actor_sphere = {}  # base_name -> (cx, cy, cz, radius)
        self._selected_actor_name = None
        self._dynamic_hidden_actor_names = set()
        self._dynamic_hide_active = False
        self._setup_ui()
        self._setup_vtk()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.vtk_widget = QVTKRenderWindowInteractor(self)
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
        self.interactor.SetStillUpdateRate(1.0)     # 静止时维持低频更新

        style = vtk.vtkInteractorStyleTrackballCamera()
        self.interactor.SetInteractorStyle(style)

        self.picker = vtk.vtkCellPicker()
        self.picker.SetTolerance(0.0005)
        self.interactor.AddObserver("LeftButtonPressEvent", self._on_left_button_press, -1.0)
        self.interactor.AddObserver("StartInteractionEvent", self._on_start_interaction, 1.0)
        self.interactor.AddObserver("EndInteractionEvent", self._on_end_interaction, 1.0)

        self._add_axes_widget()
        self._add_grid_floor()
        self._setup_lighting()

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

    def _on_left_button_press(self, obj, event):
        click_pos = self.interactor.GetEventPosition()
        self.picker.Pick(click_pos[0], click_pos[1], 0, self.renderer)
        actor = self.picker.GetActor()
        clicked_name = ""
        if actor:
            for name, a in self._actors.items():
                if a == actor:
                    clicked_name = name
                    break
        self.model_clicked.emit(clicked_name)

    def _on_start_interaction(self, obj, event):
        if self._dynamic_hide_active:
            return
        if len(self._actors) < self.DYNAMIC_HIDE_MIN_ACTOR_COUNT:
            return
        camera = self.renderer.GetActiveCamera()
        if camera is None:
            return

        selected_base = ""
        if self._selected_actor_name in self._selection_targets:
            selected_base, _ = self._selection_targets[self._selected_actor_name]

        self._dynamic_hidden_actor_names.clear()
        for name, actor in self._actors.items():
            if name == selected_base or not actor.GetVisibility():
                continue
            if self._should_temporarily_hide_actor(name, camera):
                actor.SetVisibility(False)
                self._dynamic_hidden_actor_names.add(name)

        self._dynamic_hide_active = bool(self._dynamic_hidden_actor_names)

    def _on_end_interaction(self, obj, event):
        if not self._dynamic_hide_active:
            return
        for name in list(self._dynamic_hidden_actor_names):
            actor = self._actors.get(name)
            if actor is not None:
                actor.SetVisibility(True)
        self._dynamic_hidden_actor_names.clear()
        self._dynamic_hide_active = False
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
            add_to_renderer
            and
            mapper is not None
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
            lod_actor.AutomaticLODSelectionOn()
            render_actor = lod_actor

        render_actor.GetProperty().BackfaceCullingOn()
        self._actors[name] = render_actor
        self._actor_geometry[name] = self._count_data_object_geometry(data_obj)
        self._actor_sphere[name] = self._compute_actor_bounding_sphere(render_actor)
        self._actor_base_color[name] = tuple(render_actor.GetProperty().GetColor())
        self._selection_targets[name] = (name, None)
        self._selection_geometry[name] = self._actor_geometry[name]
        self._selection_base_color[name] = self._actor_base_color[name]
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
        dataset_ptr="",
    ):
        if base_name not in self._actors:
            return
        self._selection_targets[alias_name] = (base_name, flat_block_index)
        self._selection_geometry[alias_name] = (points, cells)
        if base_color is not None:
            self._selection_base_color[alias_name] = tuple(base_color)
        self._base_to_flat_alias.setdefault(base_name, {})[flat_block_index] = alias_name
        if dataset_ptr:
            self._base_to_dataset_alias.setdefault(base_name, {})[dataset_ptr] = alias_name

    def remove_actor(self, name):
        if name in self._actors:
            if self._selected_actor_name:
                selected_base, _ = self._selection_targets.get(self._selected_actor_name, ("", None))
                if selected_base == name:
                    self._clear_highlight(self._selected_actor_name)
                    self._selected_actor_name = None
            self.renderer.RemoveActor(self._actors[name])
            del self._actors[name]
            self._actor_geometry.pop(name, None)
            self._actor_sphere.pop(name, None)
            self._actor_base_color.pop(name, None)
            self._dynamic_hidden_actor_names.discard(name)
            self._remove_selection_entries_for_base(name)

    def clear_scene(self):
        if self._selected_actor_name:
            self._clear_highlight(self._selected_actor_name)
        for actor in self._actors.values():
            self.renderer.RemoveActor(actor)
        self._actors.clear()
        self._actor_geometry.clear()
        self._actor_sphere.clear()
        self._actor_base_color.clear()
        self._selection_targets.clear()
        self._selection_geometry.clear()
        self._selection_base_color.clear()
        self._base_to_flat_alias.clear()
        self._base_to_dataset_alias.clear()
        self._dynamic_hidden_actor_names.clear()
        self._dynamic_hide_active = False
        self._selected_actor_name = None

    def highlight_actor(self, name):
        if self._selected_actor_name:
            self._clear_highlight(self._selected_actor_name)
        self._selected_actor_name = name
        if name:
            self._apply_highlight(name)
        self.render_window.Render()

    def reset_camera(self):
        self.renderer.ResetCamera()
        self.render_window.Render()

    def set_view_front(self):
        camera = self.renderer.GetActiveCamera()
        camera.SetPosition(0, 0, 1)
        camera.SetViewUp(0, 1, 0)
        camera.SetFocalPoint(0, 0, 0)
        self.renderer.ResetCamera()
        self.render_window.Render()

    def set_view_top(self):
        camera = self.renderer.GetActiveCamera()
        camera.SetPosition(0, 1, 0)
        camera.SetViewUp(0, 0, -1)
        camera.SetFocalPoint(0, 0, 0)
        self.renderer.ResetCamera()
        self.render_window.Render()

    def set_view_right(self):
        camera = self.renderer.GetActiveCamera()
        camera.SetPosition(1, 0, 0)
        camera.SetViewUp(0, 1, 0)
        camera.SetFocalPoint(0, 0, 0)
        self.renderer.ResetCamera()
        self.render_window.Render()

    def set_view_iso(self):
        self.reset_camera()

    def toggle_wireframe(self, wireframe=True):
        for actor in self._actors.values():
            if wireframe:
                actor.GetProperty().SetRepresentationToWireframe()
            else:
                actor.GetProperty().SetRepresentationToSurface()
        self.render_window.Render()

    def toggle_grid(self, visible=True):
        self.grid_actor.SetVisibility(visible)
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
            points, cells = self._selection_geometry.get(name, self._actor_geometry.get(base_name, (0, 0)))
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
        keys_to_remove = [key for key, val in self._selection_targets.items() if val[0] == base_name]
        for key in keys_to_remove:
            self._selection_targets.pop(key, None)
            self._selection_geometry.pop(key, None)
            self._selection_base_color.pop(key, None)
        self._base_to_flat_alias.pop(base_name, None)
        self._base_to_dataset_alias.pop(base_name, None)

    def _set_block_color(self, actor, flat_index, color):
        mapper = actor.GetMapper()
        if mapper and hasattr(mapper, "SetBlockColor"):
            mapper.SetBlockColor(flat_index, *color)
            mapper.Modified()

    def _apply_highlight(self, selection_name):
        target = self._selection_targets.get(selection_name)
        if not target:
            return
        base_name, flat_index = target
        actor = self._actors.get(base_name)
        if actor is None:
            return
        if flat_index is None:
            actor.GetProperty().SetEdgeVisibility(True)
            actor.GetProperty().SetEdgeColor(1.0, 0.6, 0.0)
            actor.GetProperty().SetLineWidth(1.5)
            return
        self._set_block_color(actor, flat_index, (1.0, 0.60, 0.0))

    def _clear_highlight(self, selection_name):
        target = self._selection_targets.get(selection_name)
        if not target:
            return
        base_name, flat_index = target
        actor = self._actors.get(base_name)
        if actor is None:
            return
        if flat_index is None:
            actor.GetProperty().SetEdgeVisibility(False)
            base_color = self._actor_base_color.get(base_name)
            if base_color:
                actor.GetProperty().SetColor(*base_color)
            return
        base_color = self._selection_base_color.get(selection_name)
        if base_color:
            self._set_block_color(actor, flat_index, base_color)

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
                points, cells = self._count_data_object_geometry(iterator.GetCurrentDataObject())
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
