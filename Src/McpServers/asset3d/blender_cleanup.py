"""Blender-side cleanup for generated static meshes."""

from __future__ import annotations

import json
import sys
from math import radians
from pathlib import Path

import bmesh
import bpy
from mathutils import Color, Vector
from mathutils.bvhtree import BVHTree


def _triangles(obj: bpy.types.Object) -> int:
    return sum(max(0, len(polygon.vertices) - 2) for polygon in obj.data.polygons)


def _activate(obj: bpy.types.Object) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj


def _cleanup(obj: bpy.types.Object, preserve_uv: bool = False) -> None:
    _activate(obj)
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    if preserve_uv:
        # Merging vertices collapses the duplicated UV-seam vertices a textured mesh
        # depends on, so a textured pass only re-applies the transform.
        return
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.00001)
    bmesh.ops.dissolve_degenerate(bm, dist=0.00001, edges=bm.edges)
    seen = set()
    duplicates = []
    for face in bm.faces:
        key = tuple(sorted(vertex.index for vertex in face.verts))
        if key in seen:
            duplicates.append(face)
        seen.add(key)
    if duplicates:
        bmesh.ops.delete(bm, geom=duplicates, context="FACES_ONLY")
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(obj.data)
    bm.free()
    obj.data.validate(clean_customdata=False)
    obj.data.update()


def _inspection(
    objects: list[bpy.types.Object], target: int, material_required: bool = False
) -> dict:
    result = {
        "objects": len(objects),
        "vertices": 0,
        "triangles": 0,
        "boundaryEdges": 0,
        "nonManifoldEdges": 0,
        "looseVertices": 0,
        "zeroAreaFaces": 0,
        "connectedComponents": 0,
        "selfIntersectionPairs": 0,
        "materialSlots": 0,
        "materialRequired": material_required,
    }
    trees = []
    for obj in objects:
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        result["vertices"] += len(bm.verts)
        result["triangles"] += sum(max(0, len(face.verts) - 2) for face in bm.faces)
        result["boundaryEdges"] += sum(edge.is_boundary for edge in bm.edges)
        result["nonManifoldEdges"] += sum(not edge.is_manifold for edge in bm.edges)
        result["looseVertices"] += sum(not vertex.link_faces for vertex in bm.verts)
        result["zeroAreaFaces"] += sum(face.calc_area() <= 1e-12 for face in bm.faces)
        result["materialSlots"] += len(obj.data.materials)
        remaining = set(bm.verts)
        while remaining:
            result["connectedComponents"] += 1
            stack = [remaining.pop()]
            while stack:
                vertex = stack.pop()
                for edge in vertex.link_edges:
                    neighbor = edge.other_vert(vertex)
                    if neighbor in remaining:
                        remaining.remove(neighbor)
                        stack.append(neighbor)
        tree = BVHTree.FromBMesh(bm, epsilon=1e-7)
        result["selfIntersectionPairs"] += sum(
            first < second
            and set(bm.faces[first].verts).isdisjoint(bm.faces[second].verts)
            for first, second in tree.overlap(tree)
        )
        trees.append(BVHTree.FromObject(obj, bpy.context.evaluated_depsgraph_get()))
        bm.free()
    for index, tree in enumerate(trees):
        for other in trees[index + 1 :]:
            result["selfIntersectionPairs"] += len(tree.overlap(other))
    result["triangleBudgetPassed"] = result["triangles"] <= target
    result["materialPassed"] = not material_required or result["materialSlots"] >= len(objects)
    result["topologyStrictPassed"] = (
        result["nonManifoldEdges"] == 0
        and result["looseVertices"] == 0
        and result["zeroAreaFaces"] == 0
        and result["selfIntersectionPairs"] == 0
    )
    result["gameReadyPassed"] = (
        result["triangleBudgetPassed"]
        and result["zeroAreaFaces"] == 0
        and result["looseVertices"] == 0
        and result["materialPassed"]
    )
    return result


def main() -> None:
    args = sys.argv[sys.argv.index("--") + 1 :]
    source, destination, target = Path(args[0]), Path(args[1]), int(args[2])
    material_values = json.loads(args[3]) if len(args) > 3 else None
    normal_policy = args[4] if len(args) > 4 else "mixed"
    preserve_uv = args[5] == "preserve_uv" if len(args) > 5 else False
    bpy.ops.wm.read_factory_settings(use_empty=True)
    if source.suffix.lower() == ".glb":
        bpy.ops.import_scene.gltf(filepath=str(source))
    else:
        bpy.ops.import_scene.fbx(filepath=str(source))

    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    if not meshes:
        raise RuntimeError("no mesh objects found")
    for obj in meshes:
        _cleanup(obj, preserve_uv)

    if normal_policy == "explicit_hard" and not preserve_uv:
        for obj in meshes:
            _activate(obj)
            modifier = obj.modifiers.new("PlanarCleanup", "DECIMATE")
            modifier.decimate_type = "DISSOLVE"
            modifier.angle_limit = radians(0.5)
            modifier.delimit = {"NORMAL", "MATERIAL", "SEAM"}
            bpy.ops.object.modifier_apply(modifier=modifier.name)

    if material_values:
        material = bpy.data.materials.new("GeneratedMaterial")
        material.use_nodes = True
        shader = material.node_tree.nodes.get("Principled BSDF")
        color = material_values["baseColor"].lstrip("#")
        srgb = Color(tuple(int(color[index : index + 2], 16) / 255 for index in (0, 2, 4)))
        shader.inputs["Base Color"].default_value = tuple(
            srgb.from_srgb_to_scene_linear()
        ) + (1.0,)
        shader.inputs["Metallic"].default_value = material_values["metallic"]
        shader.inputs["Roughness"].default_value = material_values["roughness"]
        for obj in meshes:
            obj.data.materials.clear()
            obj.data.materials.append(material)

    count = sum(_triangles(obj) for obj in meshes)
    if count > target and not preserve_uv:
        ratio = target / count
        for obj in meshes:
            _activate(obj)
            modifier = obj.modifiers.new("TriangleBudget", "DECIMATE")
            modifier.ratio = ratio
            modifier.use_collapse_triangulate = True
            bpy.ops.object.modifier_apply(modifier=modifier.name)

    for index, obj in enumerate(meshes, 1):
        _activate(obj)
        modifier = obj.modifiers.new("Triangulate", "TRIANGULATE")
        modifier.keep_custom_normals = True
        bpy.ops.object.modifier_apply(modifier=modifier.name)
        obj.name = f"AssetPart_{index:03d}"
        obj.data.name = f"AssetPart_{index:03d}_Mesh"

    corners = [obj.matrix_world @ Vector(corner) for obj in meshes for corner in obj.bound_box]
    center_x = (min(v.x for v in corners) + max(v.x for v in corners)) / 2
    center_y = (min(v.y for v in corners) + max(v.y for v in corners)) / 2
    minimum_z = min(v.z for v in corners)
    offset = Vector((-center_x, -center_y, -minimum_z))
    for obj in meshes:
        _activate(obj)
        obj.location += offset
        bpy.ops.object.transform_apply(location=True, rotation=False, scale=False)

    destination.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.object.select_all(action="DESELECT")
    for obj in meshes:
        obj.select_set(True)
    if destination.suffix.lower() == ".glb":
        bpy.ops.export_scene.gltf(
            filepath=str(destination), export_format="GLB", export_apply=True, use_selection=True
        )
    else:
        # Provider FBX media arrives packed with a filepath that no longer exists, so the
        # exporter cannot copy it. Write each map into the sidecar folder Unity reads.
        textures = destination.with_name(f"{destination.stem}.fbm")
        for image in bpy.data.images:
            if not image.packed_file or not image.size[0]:
                continue
            textures.mkdir(parents=True, exist_ok=True)
            image.filepath_raw = str(textures / Path(image.filepath).name)
            image.save()
            image.unpack(method="REMOVE")
        bpy.ops.export_scene.fbx(
            filepath=str(destination),
            use_selection=True,
            apply_unit_scale=True,
            axis_forward="-Z",
            axis_up="Y",
            use_triangles=True,
            add_leaf_bones=False,
            path_mode="COPY",
        )
    Path(f"{destination}.report.json").write_text(
        json.dumps(_inspection(meshes, target, bool(material_values)), indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
