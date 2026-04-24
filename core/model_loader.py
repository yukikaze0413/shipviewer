"""
模型加载器
支持 .3dm (Rhino) 和 .glb (glTF Binary) 格式
"""
import os
import vtk
from PySide6.QtCore import QThread, Signal

GLB_COMPOSITE_MAPPER_THRESHOLD = 300


class ModelLoadThread(QThread):
    """模型加载线程，避免阻塞UI"""
    finished = Signal(object, str)    # (actors_list, message)
    progress = Signal(int, str)       # (percent, description)
    error = Signal(str)               # error message

    def __init__(self, filepath, parent=None):
        super().__init__(parent)
        self.filepath = filepath

    def run(self):
        try:
            ext = os.path.splitext(self.filepath)[1].lower()
            if ext == ".3dm":
                self.progress.emit(10, "正在解析 Rhino 3DM 文件...")
                actors = self._load_3dm(self.filepath)
            elif ext == ".glb" or ext == ".gltf":
                self.progress.emit(10, "正在解析 glTF 文件...")
                actors = self._load_glb(self.filepath)
            else:
                self.error.emit(f"不支持的文件格式: {ext}")
                return

            if actors:
                self.finished.emit(actors, f"成功加载 {len(actors)} 个对象")
            else:
                self.error.emit("未能从文件中提取任何几何体")
        except Exception as e:
            self.error.emit(f"加载失败: {str(e)}")

    def _load_3dm(self, filepath):
        """加载 Rhino .3dm 文件"""
        import rhino3dm

        self.progress.emit(20, "正在读取 3DM 文件...")
        model = rhino3dm.File3dm.Read(filepath)
        if model is None:
            raise RuntimeError("无法读取 3DM 文件")

        actors = []
        total = len(model.Objects)
        if total == 0:
            raise RuntimeError("3DM 文件中没有几何对象")

        # 预定义工业风配色
        colors = [
            (0.60, 0.72, 0.78),  # 钢蓝色
            (0.75, 0.75, 0.75),  # 银灰色
            (0.55, 0.63, 0.68),  # 石板灰
            (0.70, 0.78, 0.82),  # 浅钢色
            (0.50, 0.55, 0.60),  # 深灰色
            (0.82, 0.68, 0.35),  # 黄铜色
            (0.72, 0.45, 0.20),  # 铜色
        ]

        for i, obj in enumerate(model.Objects):
            percent = 20 + int(70 * (i + 1) / total)
            self.progress.emit(percent, f"正在处理对象 {i + 1}/{total}...")

            geo = obj.Geometry
            attr = obj.Attributes

            # 获取对象名称
            name = attr.Name if attr.Name else f"对象_{i}"

            mesh = None
            # 尝试从不同类型的几何体获取网格
            geo_type = type(geo).__name__

            if geo_type == "Mesh":
                mesh = geo
            elif geo_type == "Brep":
                # 对Brep进行网格化
                try:
                    meshes = rhino3dm.Mesh.CreateFromBrep(geo)
                    if meshes and len(meshes) > 0:
                        mesh = meshes[0]
                        for m in meshes[1:]:
                            mesh.Append(m)
                except Exception:
                    continue
            elif geo_type == "Extrusion":
                try:
                    brep = geo.ToBrep()
                    if brep:
                        meshes = rhino3dm.Mesh.CreateFromBrep(brep)
                        if meshes and len(meshes) > 0:
                            mesh = meshes[0]
                            for m in meshes[1:]:
                                mesh.Append(m)
                except Exception:
                    continue
            elif hasattr(geo, "GetMesh"):
                try:
                    mesh = geo.GetMesh(rhino3dm.MeshType.Default)
                except Exception:
                    continue

            if mesh is None:
                continue

            # 转换为VTK
            vtk_actor = self._rhino_mesh_to_vtk(mesh, name)
            if vtk_actor is None:
                continue

            # 设置颜色
            if attr.ColorSource == rhino3dm.ObjectColorSource.ColorFromObject:
                color = attr.ObjectColor
                r, g, b = color.R / 255.0, color.G / 255.0, color.B / 255.0
            else:
                color_idx = i % len(colors)
                r, g, b = colors[color_idx]

            self._configure_actor_material(vtk_actor, (r, g, b))
            points, cells = self._get_actor_geometry_stats(vtk_actor)
            actors.append({
                "actor": vtk_actor,
                "name": name,
                "type": geo_type,
                "points": points,
                "cells": cells,
            })

        self.progress.emit(100, "加载完成")
        return actors

    def _rhino_mesh_to_vtk(self, mesh, name="mesh"):
        """将 Rhino 网格转换为 VTK Actor"""
        vertices = mesh.Vertices
        faces = mesh.Faces

        if vertices.Count == 0:
            return None

        # 创建VTK点集
        vtk_points = vtk.vtkPoints()
        for i in range(vertices.Count):
            v = vertices[i]
            vtk_points.InsertNextPoint(v.X, v.Y, v.Z)

        # 创建VTK多边形
        vtk_cells = vtk.vtkCellArray()
        for i in range(faces.Count):
            face_verts = faces[i]
            if len(face_verts) == 4:
                # 四边面 -> 两个三角面
                tri1 = vtk.vtkTriangle()
                tri1.GetPointIds().SetId(0, face_verts[0])
                tri1.GetPointIds().SetId(1, face_verts[1])
                tri1.GetPointIds().SetId(2, face_verts[2])
                vtk_cells.InsertNextCell(tri1)

                tri2 = vtk.vtkTriangle()
                tri2.GetPointIds().SetId(0, face_verts[0])
                tri2.GetPointIds().SetId(1, face_verts[2])
                tri2.GetPointIds().SetId(2, face_verts[3])
                vtk_cells.InsertNextCell(tri2)
            elif len(face_verts) == 3:
                tri = vtk.vtkTriangle()
                tri.GetPointIds().SetId(0, face_verts[0])
                tri.GetPointIds().SetId(1, face_verts[1])
                tri.GetPointIds().SetId(2, face_verts[2])
                vtk_cells.InsertNextCell(tri)

        # 创建 PolyData
        poly_data = vtk.vtkPolyData()
        poly_data.SetPoints(vtk_points)
        poly_data.SetPolys(vtk_cells)

        # 计算法线
        normals = vtk.vtkPolyDataNormals()
        normals.SetInputData(poly_data)
        normals.ComputePointNormalsOn()
        normals.ComputeCellNormalsOn()
        normals.SplittingOff()
        normals.Update()

        # 创建Mapper和Actor
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(normals.GetOutput())
        self._configure_mapper_for_static_scene(mapper)

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)

        return actor

    def _load_glb(self, filepath):
        """加载 glTF/GLB 文件"""
        self.progress.emit(20, "正在读取 glTF 文件...")

        reader = vtk.vtkGLTFReader()
        reader.SetFileName(filepath)
        reader.Update()

        self.progress.emit(50, "正在解析场景...")

        mb = reader.GetOutput()
        if mb is None:
            raise RuntimeError("无法读取 glTF 文件")

        actors = []
        poly_blocks = self._collect_polydata_blocks(mb)
        poly_count = len(poly_blocks)

        # 超大 GLB 场景直接使用复合 mapper，减少 actor 数量和 draw call。
        if poly_count >= GLB_COMPOSITE_MAPPER_THRESHOLD:
            self.progress.emit(65, f"网格数量 {poly_count}，启用合并渲染加速...")
            use_vertex_color = any(self._has_vertex_color(info["poly"]) for info in poly_blocks)
            mapper = vtk.vtkCompositePolyDataMapper()
            mapper.SetInputDataObject(mb)
            self._configure_mapper_for_static_scene(mapper, enable_vertex_color=use_vertex_color)

            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            self._configure_actor_material(actor, (1.0, 1.0, 1.0))

            sub_items = []
            for idx, block_info in enumerate(poly_blocks):
                color = self._extract_poly_base_color(block_info["poly"])
                flat_index = block_info["flat_index"]
                if not self._has_vertex_color(block_info["poly"]):
                    mapper.SetBlockColor(flat_index, *color)
                sub_items.append({
                    "name": block_info["name"],
                    "type": "Mesh",
                    "flat_index": flat_index,
                    "dataset_ptr": block_info["dataset_ptr"],
                    "color": color,
                    "points": block_info["poly"].GetNumberOfPoints(),
                    "cells": block_info["poly"].GetNumberOfCells(),
                })

            points, cells = self._count_data_object_geometry(mb)
            actors.append({
                "actor": actor,
                "name": f"GLB_Scene_{poly_count}_Meshes",
                "type": "CompositeMesh",
                "points": points,
                "cells": cells,
                "sub_items": sub_items,
            })
        else:
            for idx, block_info in enumerate(poly_blocks):
                poly = block_info["poly"]
                self.progress.emit(
                    50 + int(40 * (idx + 1) / max(poly_count, 1)),
                    f"正在处理网格 {idx + 1}/{max(poly_count, 1)}..."
                )

                if poly.GetNumberOfPoints() == 0:
                    continue

                if not poly.GetPointData().GetNormals():
                    normals = vtk.vtkPolyDataNormals()
                    normals.SetInputData(poly)
                    normals.ComputePointNormalsOn()
                    normals.Update()
                    poly = normals.GetOutput()

                mapper = vtk.vtkPolyDataMapper()
                mapper.SetInputData(poly)
                has_vertex_color = self._has_vertex_color(poly)
                self._configure_mapper_for_static_scene(mapper, enable_vertex_color=has_vertex_color)

                actor = vtk.vtkActor()
                actor.SetMapper(mapper)
                if has_vertex_color:
                    self._configure_actor_material(actor, (1.0, 1.0, 1.0))
                else:
                    self._configure_actor_material(actor, self._extract_poly_base_color(poly))
                actors.append({
                    "actor": actor,
                    "name": block_info["name"],
                    "type": "Mesh",
                    "points": poly.GetNumberOfPoints(),
                    "cells": poly.GetNumberOfCells(),
                })

        # 如果没有找到 PolyData 块，尝试用 GeometryFilter 转换
        if not actors:
            self.progress.emit(70, "正在转换几何数据...")
            geo_filter = vtk.vtkCompositeDataGeometryFilter()
            geo_filter.SetInputData(mb)
            geo_filter.Update()
            poly = geo_filter.GetOutput()

            if poly and poly.GetNumberOfPoints() > 0:
                normals = vtk.vtkPolyDataNormals()
                normals.SetInputData(poly)
                normals.ComputePointNormalsOn()
                normals.Update()
                poly = normals.GetOutput()

                mapper = vtk.vtkPolyDataMapper()
                mapper.SetInputData(poly)
                self._configure_mapper_for_static_scene(
                    mapper,
                    enable_vertex_color=self._has_vertex_color(poly)
                )

                actor = vtk.vtkActor()
                actor.SetMapper(mapper)
                self._configure_actor_material(actor, (0.60, 0.72, 0.78))

                actors.append({
                    "actor": actor,
                    "name": "Model",
                    "type": "Mesh",
                    "points": poly.GetNumberOfPoints(),
                    "cells": poly.GetNumberOfCells(),
                })

        self.progress.emit(100, "加载完成")
        return actors

    def _collect_polydata_blocks(self, composite):
        blocks = []
        iterator = composite.NewIterator()
        iterator.InitTraversal()
        index = 0
        while not iterator.IsDoneWithTraversal():
            block = iterator.GetCurrentDataObject()
            if block and block.IsA("vtkPolyData"):
                flat_index = iterator.GetCurrentFlatIndex()
                blocks.append({
                    "poly": block,
                    "flat_index": flat_index,
                    "dataset_ptr": getattr(block, "__this__", ""),
                    "name": f"Mesh_{index}",
                })
                index += 1
            iterator.GoToNextItem()
        return blocks

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

    def _get_actor_geometry_stats(self, actor):
        mapper = actor.GetMapper()
        if not mapper:
            return 0, 0
        data = mapper.GetInputDataObject(0, 0)
        return self._count_data_object_geometry(data)

    def _configure_mapper_for_static_scene(self, mapper, enable_vertex_color=False):
        if hasattr(mapper, "SetScalarVisibility"):
            mapper.SetScalarVisibility(enable_vertex_color)
        if enable_vertex_color:
            if hasattr(mapper, "SetColorModeToDirectScalars"):
                mapper.SetColorModeToDirectScalars()
            if hasattr(mapper, "SetScalarModeToUsePointFieldData"):
                mapper.SetScalarModeToUsePointFieldData()
            if hasattr(mapper, "SelectColorArray"):
                mapper.SelectColorArray("COLOR_0")
        if hasattr(mapper, "SetStatic"):
            mapper.SetStatic(True)

    def _configure_actor_material(self, actor, color):
        prop = actor.GetProperty()
        prop.SetColor(*color)
        prop.SetSpecular(0.08)
        prop.SetSpecularPower(12)
        prop.SetInterpolationToGouraud()

    def _has_vertex_color(self, poly):
        if poly is None:
            return False
        pd = poly.GetPointData()
        if pd is None:
            return False
        arr = pd.GetArray("COLOR_0")
        return arr is not None and arr.GetNumberOfComponents() >= 3

    def _extract_poly_base_color(self, poly, default=(0.72, 0.74, 0.78)):
        if poly is None:
            return default
        fd = poly.GetFieldData()
        if fd is None:
            return default
        arr = fd.GetArray("BaseColorMultiplier")
        if arr is None or arr.GetNumberOfTuples() == 0:
            return default
        t = arr.GetTuple(0)
        if len(t) < 3:
            return default
        r, g, b = t[0], t[1], t[2]
        if max(r, g, b) > 1.0:
            r, g, b = r / 255.0, g / 255.0, b / 255.0
        return (
            max(0.0, min(1.0, float(r))),
            max(0.0, min(1.0, float(g))),
            max(0.0, min(1.0, float(b))),
        )


def get_supported_formats():
    """获取支持的文件格式过滤器字符串"""
    return "所有支持格式 (*.3dm *.glb *.gltf);;Rhino 文件 (*.3dm);;glTF 文件 (*.glb *.gltf)"


def get_file_info(filepath):
    """获取文件基本信息"""
    stat = os.stat(filepath)
    size = stat.st_size
    if size < 1024:
        size_str = f"{size} B"
    elif size < 1024 * 1024:
        size_str = f"{size / 1024:.1f} KB"
    else:
        size_str = f"{size / (1024 * 1024):.1f} MB"

    return {
        "文件名": os.path.basename(filepath),
        "文件路径": filepath,
        "文件大小": size_str,
        "文件格式": os.path.splitext(filepath)[1].upper(),
    }
