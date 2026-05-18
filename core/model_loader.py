"""
模型加载器
支持 .3dm (Rhino) 和 .glb (glTF Binary) 格式
"""
import hashlib
import json
import os
import sys
import vtk
from PySide6.QtCore import QThread, Signal

GLB_COMPOSITE_MAPPER_THRESHOLD = 300
MODEL_CACHE_VERSION = 1


def app_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def model_cache_root():
    return os.path.normpath(os.path.join(app_base_dir(), "assets", "cache", "models"))


def model_cache_key(filepath):
    abs_path = os.path.normcase(os.path.abspath(filepath))
    digest = hashlib.sha256(abs_path.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]
    stem = os.path.splitext(os.path.basename(filepath))[0]
    safe_stem = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in stem)
    return f"{safe_stem}-{digest}"


class ModelLoadThread(QThread):
    """模型加载线程，避免阻塞UI"""
    finished = Signal(object, str)    # (actors_list, message)
    progress = Signal(int, str)       # (percent, description)
    error = Signal(str)               # error message
    completed = Signal()              # emitted whenever run() exits

    def __init__(self, filepath, parent=None):
        super().__init__(parent)
        self.filepath = filepath
        self._current_model_dir = ""
        self._object_index_by_id = {}
        self.force_rebuild_cache = False

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

            if self.isInterruptionRequested():
                return

            if actors:
                self.finished.emit(actors, f"成功加载 {len(actors)} 个对象")
            else:
                diagnostics = getattr(self, "_last_3dm_diagnostics", [])
                if self.isInterruptionRequested():
                    return
                if ext == ".3dm" and diagnostics:
                    preview = "\n".join(diagnostics[:12])
                    extra = "" if len(diagnostics) <= 12 else f"\n... 另有 {len(diagnostics) - 12} 项"
                    self.error.emit(
                        "未能从文件中提取任何可显示网格。\n"
                        "可能原因：文件里只有曲线/点/未保存渲染网格的 NURBS 曲面，"
                        "或对象类型暂不支持。\n\n"
                        f"对象诊断:\n{preview}{extra}"
                    )
                    return
                self.error.emit("未能从文件中提取任何几何体")
        except Exception as e:
            if not self.isInterruptionRequested():
                self.error.emit(f"加载失败: {str(e)}")
        finally:
            self.completed.emit()

    def _load_3dm_cache(self, filepath):
        self.progress.emit(12, "正在检查 3DM 模型缓存...")
        for cache_dir in self._cache_candidate_dirs(filepath):
            try:
                metadata_path = os.path.join(cache_dir, "metadata.json")
                geometry_path = os.path.join(cache_dir, "geometry.vtm")
                if not os.path.exists(metadata_path) or not os.path.exists(geometry_path):
                    continue
                with open(metadata_path, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
                if not self._cache_metadata_matches(metadata, filepath):
                    continue
                self.progress.emit(18, "正在读取 3DM 缓存几何...")
                actors = self._actors_from_3dm_cache(metadata, geometry_path)
                if actors:
                    return actors
            except Exception:
                continue
        return []

    def _write_3dm_cache(self, filepath, actors):
        try:
            cache_dir = self._cache_dir_for_path(filepath)
            os.makedirs(cache_dir, exist_ok=True)
            geometry_path = os.path.join(cache_dir, "geometry.vtm")
            metadata_path = os.path.join(cache_dir, "metadata.json")

            multiblock = vtk.vtkMultiBlockDataSet()
            multiblock.SetNumberOfBlocks(len(actors))
            entries = []
            for block_index, item in enumerate(actors):
                actor = item.get("actor")
                mapper = actor.GetMapper() if actor is not None else None
                data_obj = mapper.GetInputDataObject(0, 0) if mapper is not None else None
                if data_obj is None:
                    continue
                multiblock.SetBlock(block_index, data_obj)
                entries.append(self._cache_entry_for_actor_item(item, block_index))

            if not entries:
                return False

            writer = vtk.vtkXMLMultiBlockDataWriter()
            writer.SetFileName(geometry_path)
            writer.SetInputData(multiblock)
            writer.SetDataModeToBinary()
            if not writer.Write():
                return False

            metadata = self._source_file_metadata(filepath, include_hash=True)
            metadata.update(
                {
                    "cache_version": MODEL_CACHE_VERSION,
                    "entry_count": len(entries),
                    "geometry_file": "geometry.vtm",
                    "entries": entries,
                }
            )
            with open(metadata_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    def _actors_from_3dm_cache(self, metadata, geometry_path):
        reader = vtk.vtkXMLMultiBlockDataReader()
        reader.SetFileName(geometry_path)
        reader.Update()
        multiblock = reader.GetOutput()
        if multiblock is None:
            return []

        self.progress.emit(35, "正在从缓存重建场景对象...")
        actors = []
        for entry in metadata.get("entries", []):
            block_index = int(entry.get("block_index", len(actors)))
            data_obj = multiblock.GetBlock(block_index)
            if data_obj is None:
                continue

            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputData(data_obj)
            self._configure_mapper_for_static_scene(mapper)

            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            texture = self._texture_for_path(entry.get("texture_path", ""))
            self._configure_actor_material(
                actor,
                self._metadata_color(entry.get("color")),
                texture,
            )

            actors.append(
                {
                    "actor": actor,
                    "name": str(entry.get("name", "")),
                    "base_name": str(entry.get("base_name", "")),
                    "is_split": bool(entry.get("is_split", False)),
                    "type": str(entry.get("type", "Mesh")),
                    "points": int(entry.get("points", data_obj.GetNumberOfPoints())),
                    "cells": int(entry.get("cells", data_obj.GetNumberOfCells())),
                    "texture_path": str(entry.get("texture_path", "")),
                }
            )
        return actors

    def _cache_entry_for_actor_item(self, item, block_index):
        actor = item.get("actor")
        prop = actor.GetProperty() if actor is not None else None
        color = prop.GetColor() if prop is not None else (0.72, 0.74, 0.78)
        return {
            "block_index": block_index,
            "name": str(item.get("name", "")),
            "base_name": str(item.get("base_name", "")),
            "is_split": bool(item.get("is_split", False)),
            "type": str(item.get("type", "Mesh")),
            "points": int(item.get("points", 0)),
            "cells": int(item.get("cells", 0)),
            "color": [float(color[0]), float(color[1]), float(color[2])],
            "texture_path": str(item.get("texture_path", "")),
        }

    def _cache_metadata_matches(self, metadata, filepath):
        if int(metadata.get("cache_version", -1)) != MODEL_CACHE_VERSION:
            return False

        current = self._source_file_metadata(filepath)
        if int(metadata.get("source_size", -1)) != current["source_size"]:
            return False

        cached_norm = os.path.normcase(os.path.abspath(str(metadata.get("source_path", ""))))
        current_norm = os.path.normcase(os.path.abspath(filepath))
        path_matches = cached_norm == current_norm or str(
            metadata.get("source_basename", "")
        ).casefold() == current["source_basename"].casefold()
        if not path_matches:
            return False

        if int(metadata.get("source_mtime_ns", -1)) == current["source_mtime_ns"]:
            return True

        cached_hash = str(metadata.get("source_sha256", ""))
        return bool(cached_hash) and cached_hash == self._file_sha256(filepath)

    def _source_file_metadata(self, filepath, include_hash=False):
        stat = os.stat(filepath)
        abs_path = os.path.normpath(os.path.abspath(filepath))
        metadata = {
            "source_path": abs_path,
            "source_norm_path": os.path.normcase(abs_path),
            "source_basename": os.path.basename(filepath),
            "source_size": int(stat.st_size),
            "source_mtime_ns": int(stat.st_mtime_ns),
        }
        if include_hash:
            metadata["source_sha256"] = self._file_sha256(filepath)
        return metadata

    def _file_sha256(self, filepath):
        digest = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _cache_dir_for_path(self, filepath):
        return os.path.join(model_cache_root(), model_cache_key(filepath))

    def _cache_candidate_dirs(self, filepath):
        exact_dir = self._cache_dir_for_path(filepath)
        candidates = [exact_dir]
        root = model_cache_root()
        if os.path.isdir(root):
            for name in os.listdir(root):
                candidate = os.path.join(root, name)
                if os.path.isdir(candidate) and candidate not in candidates:
                    candidates.append(candidate)
        return candidates

    def _metadata_color(self, value, default=(0.72, 0.74, 0.78)):
        if not isinstance(value, (list, tuple)) or len(value) < 3:
            return default
        try:
            return (
                self._clamp_color_channel(value[0]),
                self._clamp_color_channel(value[1]),
                self._clamp_color_channel(value[2]),
            )
        except (TypeError, ValueError):
            return default

    def _load_3dm(self, filepath):
        """加载 Rhino .3dm 文件"""
        if not self.force_rebuild_cache:
            cached_actors = self._load_3dm_cache(filepath)
            if cached_actors:
                self.progress.emit(100, "已从 3DM 模型缓存加载")
                return cached_actors

        try:
            import rhino3dm
        except ImportError as exc:
            raise RuntimeError(
                "当前 Python 环境缺少 rhino3dm，无法加载 3DM 文件。\n"
                f"解释器: {sys.executable}\n"
                "请使用项目的 启动查看器.bat 启动，或在当前环境执行: "
                "python -m pip install -r requirements.txt"
            ) from exc

        self.progress.emit(20, "正在读取 3DM 文件...")
        model = rhino3dm.File3dm.Read(filepath)
        if model is None:
            raise RuntimeError("无法读取 3DM 文件")

        self._current_model_dir = os.path.dirname(os.path.abspath(filepath))
        actors = []
        self._last_3dm_diagnostics = []
        total = len(model.Objects)
        if total == 0:
            raise RuntimeError("3DM 文件中没有几何对象")
        self._object_index_by_id = {}
        for index, model_obj in enumerate(model.Objects):
            object_id = getattr(model_obj.Attributes, "Id", None)
            if object_id:
                self._object_index_by_id[str(object_id)] = index

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
            if self.isInterruptionRequested():
                return actors

            percent = 20 + int(70 * (i + 1) / total)
            self.progress.emit(percent, f"正在处理对象 {i + 1}/{total}...")

            entries = self._rhino_object_to_actor_entries(
                rhino3dm,
                model,
                obj,
                object_index=i,
                fallback_colors=colors,
            )
            actors.extend(entries)

        if not self.isInterruptionRequested() and actors:
            self._write_3dm_cache(filepath, actors)

        self.progress.emit(100, "加载完成")
        return actors

    def _rhino_object_to_actor_entries(
        self,
        rhino3dm,
        model,
        obj,
        object_index=0,
        fallback_colors=None,
        transform=None,
        visited_instance_ids=None,
    ):
        attr = obj.Attributes
        geo = obj.Geometry
        geo_type = type(geo).__name__
        name = attr.Name if attr.Name else f"对象_{object_index}"
        fallback_colors = fallback_colors or [(0.60, 0.72, 0.78)]
        visited_instance_ids = visited_instance_ids or set()

        if geo_type == "InstanceReference":
            return self._rhino_instance_to_actor_entries(
                rhino3dm,
                model,
                geo,
                object_index,
                fallback_colors,
                transform,
                visited_instance_ids,
            )

        material = self._rhino_object_material(rhino3dm, model, attr)
        color = self._rhino_object_color(
            rhino3dm, model, attr, material, object_index, fallback_colors
        )
        texture_path = self._rhino_material_texture_path(material) if material else ""
        texture = self._texture_for_path(texture_path)
        entries = []
        meshes = self._rhino_geometry_to_meshes(rhino3dm, geo, transform)
        is_split = len(meshes) > 1
        for mesh_index, mesh in enumerate(meshes):
            entry_name = name if not is_split else f"{name}_{mesh_index + 1}"
            vtk_actor = self._rhino_mesh_to_vtk(mesh, entry_name)
            if vtk_actor is None:
                continue
            self._configure_actor_material(vtk_actor, color, texture)
            points, cells = self._get_actor_geometry_stats(vtk_actor)
            entries.append(
                {
                    "actor": vtk_actor,
                    "name": entry_name,
                    "base_name": name,
                    "is_split": is_split,
                    "type": geo_type,
                    "points": points,
                    "cells": cells,
                    "texture_path": texture_path,
                }
            )

        if not entries:
            self._last_3dm_diagnostics.append(f"{name}: {geo_type} 未找到可显示网格")
        return entries

    def _rhino_instance_to_actor_entries(
        self,
        rhino3dm,
        model,
        instance_ref,
        object_index,
        fallback_colors,
        transform,
        visited_instance_ids,
    ):
        idef_id = getattr(instance_ref, "ParentIdefId", None)
        if not idef_id:
            return []
        idef_key = str(idef_id)
        if idef_key in visited_instance_ids:
            self._last_3dm_diagnostics.append(f"块实例循环引用: {idef_key}")
            return []

        idef = model.InstanceDefinitions.FindId(idef_id)
        if idef is None:
            self._last_3dm_diagnostics.append(f"未找到块定义: {idef_key}")
            return []

        instance_xform = getattr(instance_ref, "Xform", None)
        composed = self._compose_rhino_transform(rhino3dm, transform, instance_xform)
        entries = []
        next_visited = set(visited_instance_ids)
        next_visited.add(idef_key)
        for child_id in idef.GetObjectIds() or []:
            child_obj = model.Objects.FindId(child_id)
            if child_obj is None:
                continue
            child_index = self._rhino_object_index(child_obj, object_index)
            entries.extend(
                self._rhino_object_to_actor_entries(
                    rhino3dm,
                    model,
                    child_obj,
                    object_index=child_index,
                    fallback_colors=fallback_colors,
                    transform=composed,
                    visited_instance_ids=next_visited,
                )
            )
        return entries

    def _rhino_geometry_to_meshes(self, rhino3dm, geo, transform=None):
        geo_type = type(geo).__name__
        meshes = []

        if geo_type == "Mesh":
            meshes.append(geo)
        elif geo_type == "Brep":
            meshes.extend(self._rhino_brep_to_meshes(rhino3dm, geo))
        elif geo_type == "Extrusion":
            meshes.extend(self._rhino_extrusion_to_meshes(rhino3dm, geo))
        elif geo_type == "Surface":
            try:
                brep = rhino3dm.Brep.CreateFromSurface(geo)
                if brep:
                    meshes.extend(self._rhino_brep_to_meshes(rhino3dm, brep))
            except Exception:
                pass
        elif geo_type == "SubD" and hasattr(rhino3dm.Mesh, "CreateFromSubDControlNet"):
            try:
                mesh = rhino3dm.Mesh.CreateFromSubDControlNet(geo)
                if mesh:
                    meshes.append(mesh)
            except Exception:
                pass
        elif hasattr(geo, "GetMesh"):
            meshes.extend(self._meshes_from_get_mesh(rhino3dm, geo))

        return [self._rhino_mesh_with_transform(mesh, transform) for mesh in meshes if mesh]

    def _rhino_brep_to_meshes(self, rhino3dm, brep):
        meshes = []
        faces = getattr(brep, "Faces", None)
        if faces is not None:
            for face in faces:
                meshes.extend(self._meshes_from_get_mesh(rhino3dm, face))
        if not meshes:
            meshes.extend(self._meshes_from_create_from_brep(rhino3dm, brep))
        return meshes

    def _rhino_extrusion_to_meshes(self, rhino3dm, extrusion):
        meshes = self._meshes_from_get_mesh(rhino3dm, extrusion)
        if meshes:
            return meshes
        try:
            brep = extrusion.ToBrep()
            if brep:
                return self._rhino_brep_to_meshes(rhino3dm, brep)
        except Exception:
            pass
        return []

    def _meshes_from_get_mesh(self, rhino3dm, geo):
        meshes = []
        getter = getattr(geo, "GetMesh", None)
        if not callable(getter):
            return meshes
        for mesh_type in (
            rhino3dm.MeshType.Render,
            rhino3dm.MeshType.Default,
            rhino3dm.MeshType.Preview,
            rhino3dm.MeshType.Any,
        ):
            try:
                mesh = getter(mesh_type)
            except Exception:
                continue
            if (
                mesh
                and self._rhino_collection_count(mesh.Vertices) > 0
                and self._rhino_collection_count(mesh.Faces) > 0
            ):
                meshes.append(mesh)
                break
        return meshes

    def _meshes_from_create_from_brep(self, rhino3dm, brep):
        creator = getattr(rhino3dm.Mesh, "CreateFromBrep", None)
        if not callable(creator):
            return []
        try:
            meshes = creator(brep)
        except Exception:
            return []
        if not meshes:
            return []
        return [
            mesh
            for mesh in meshes
            if mesh
            and self._rhino_collection_count(mesh.Vertices) > 0
            and self._rhino_collection_count(mesh.Faces) > 0
        ]

    def _rhino_mesh_with_transform(self, mesh, transform):
        if transform is None:
            return mesh
        try:
            mesh = mesh.Duplicate()
            mesh.Transform(transform)
        except Exception:
            pass
        return mesh

    def _compose_rhino_transform(self, rhino3dm, parent, child):
        if parent is None:
            return child
        if child is None:
            return parent
        try:
            return rhino3dm.Transform.Multiply(parent, child)
        except Exception:
            return child

    def _rhino_object_index(self, obj, fallback):
        object_id = getattr(obj.Attributes, "Id", None)
        if not object_id:
            return fallback
        return self._object_index_by_id.get(str(object_id), fallback)

    def _rhino_object_material(self, rhino3dm, model, attr):
        if attr.MaterialSource == rhino3dm.ObjectMaterialSource.MaterialFromObject:
            return self._rhino_material_at(model, getattr(attr, "MaterialIndex", -1))
        if attr.MaterialSource == rhino3dm.ObjectMaterialSource.MaterialFromLayer:
            layer = self._rhino_layer_at(model, getattr(attr, "LayerIndex", -1))
            material_index = getattr(layer, "RenderMaterialIndex", -1) if layer else -1
            return self._rhino_material_at(model, material_index)
        return None

    def _rhino_object_color(
        self, rhino3dm, model, attr, material, object_index, fallback_colors
    ):
        if attr.ColorSource == rhino3dm.ObjectColorSource.ColorFromObject:
            color = self._rhino_color_tuple(getattr(attr, "ObjectColor", None))
            if color:
                return color
        if material:
            material_color = self._rhino_material_diffuse_color(material)
            if material_color:
                return material_color
        if attr.ColorSource == rhino3dm.ObjectColorSource.ColorFromLayer:
            layer = self._rhino_layer_at(model, getattr(attr, "LayerIndex", -1))
            layer_color = self._rhino_color_tuple(getattr(layer, "Color", None))
            if layer_color:
                return layer_color
        return fallback_colors[object_index % len(fallback_colors)]

    def _rhino_material_at(self, model, material_index):
        try:
            material_index = int(material_index)
        except (TypeError, ValueError):
            return None
        if material_index < 0 or material_index >= len(model.Materials):
            return None
        try:
            return model.Materials[material_index]
        except Exception:
            return None

    def _rhino_layer_at(self, model, layer_index):
        try:
            layer_index = int(layer_index)
        except (TypeError, ValueError):
            return None
        if layer_index < 0 or layer_index >= len(model.Layers):
            return None
        try:
            return model.Layers[layer_index]
        except Exception:
            return None

    def _rhino_material_diffuse_color(self, material):
        try:
            return self._rhino_color_tuple(material.DiffuseColor)
        except Exception:
            return None

    def _rhino_color_tuple(self, color):
        if color is None:
            return None
        if isinstance(color, tuple) and len(color) >= 3:
            r, g, b = color[:3]
        else:
            try:
                r, g, b = color.R, color.G, color.B
            except AttributeError:
                return None
        return self._clamp_color_channel(r), self._clamp_color_channel(g), self._clamp_color_channel(b)

    def _clamp_color_channel(self, value):
        value = float(value)
        if value > 1.0:
            value /= 255.0
        return max(0.0, min(1.0, value))

    def _rhino_material_texture(self, material):
        if material is None:
            return None
        texture_path = self._rhino_material_texture_path(material)
        return self._texture_for_path(texture_path)

    def _texture_for_path(self, texture_path):
        if not texture_path:
            return None
        texture_path = os.path.normpath(str(texture_path))
        if not os.path.exists(texture_path):
            return None
        reader = self._texture_reader_for_path(texture_path)
        if reader is None:
            return None
        try:
            reader.SetFileName(texture_path)
            reader.Update()
            texture = vtk.vtkTexture()
            texture.SetInputConnection(reader.GetOutputPort())
            texture.InterpolateOn()
            return texture
        except Exception:
            return None

    def _rhino_material_texture_path(self, material):
        for getter_name in ("GetBitmapTexture", "GetTexture"):
            getter = getattr(material, getter_name, None)
            if not callable(getter):
                continue
            try:
                texture_info = getter()
            except TypeError:
                continue
            path = self._rhino_texture_filename(texture_info)
            if path:
                return path
        return ""

    def _rhino_texture_filename(self, texture_info):
        if texture_info is None:
            return ""
        for attr_name in ("FileName", "Filename", "Path"):
            filename = getattr(texture_info, attr_name, "")
            if filename:
                return self._resolve_texture_path(str(filename))
        return ""

    def _resolve_texture_path(self, filename):
        filename = filename.strip().strip('"')
        if not filename:
            return ""
        candidates = []
        if os.path.isabs(filename):
            candidates.append(filename)
        else:
            candidates.append(os.path.join(self._current_model_dir, filename))
            candidates.append(os.path.join(self._current_model_dir, os.path.basename(filename)))
        for candidate in candidates:
            candidate = os.path.normpath(candidate)
            if os.path.exists(candidate):
                return candidate
        return ""

    def _texture_reader_for_path(self, texture_path):
        ext = os.path.splitext(texture_path)[1].lower()
        if ext in (".jpg", ".jpeg"):
            return vtk.vtkJPEGReader()
        if ext == ".png":
            return vtk.vtkPNGReader()
        if ext == ".bmp":
            return vtk.vtkBMPReader()
        if ext in (".tif", ".tiff"):
            return vtk.vtkTIFFReader()
        return None

    def _rhino_collection_count(self, collection):
        try:
            count = getattr(collection, "Count", None)
        except AttributeError:
            count = None
        if count is not None:
            try:
                return int(count() if callable(count) else count)
            except (TypeError, ValueError, AttributeError):
                pass
        try:
            return len(collection)
        except (TypeError, AttributeError):
            return sum(1 for _ in collection)

    def _rhino_mesh_to_vtk(self, mesh, name="mesh"):
        """将 Rhino 网格转换为 VTK Actor"""
        vertices = mesh.Vertices
        faces = mesh.Faces

        vertex_count = self._rhino_collection_count(vertices)
        face_count = self._rhino_collection_count(faces)
        if vertex_count == 0:
            return None

        # 创建VTK点集
        vtk_points = vtk.vtkPoints()
        for i in range(vertex_count):
            v = vertices[i]
            vtk_points.InsertNextPoint(v.X, v.Z, -v.Y)

        # 创建VTK多边形
        vtk_cells = vtk.vtkCellArray()
        for i in range(face_count):
            face_verts = faces[i]
            is_tri = len(face_verts) == 3 or (
                len(face_verts) == 4 and face_verts[2] == face_verts[3]
            )
            if len(face_verts) == 4 and not is_tri:
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
            elif is_tri:
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

    def _configure_actor_material(self, actor, color, texture=None):
        prop = actor.GetProperty()
        prop.SetColor(*color)
        prop.SetSpecular(0.08)
        prop.SetSpecularPower(12)
        prop.SetInterpolationToGouraud()
        if texture is not None:
            actor.SetTexture(texture)

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
