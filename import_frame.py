"""
import_frame.py
================
Import a Ninja Ripper 2 capture frame folder into a running Blender, using the
official `io_import_nr` addon.

Default mode is LOCAL / T-pose ("PreVs"):
  * Geometry is read straight from the model-space vertex buffers, so it needs
    NO projection matrix and is geometrically exact.
  * UVs, split normals and DDS textures are bound automatically
    (texturingTab=AUTO, normalsTab=AUTO).
  * Every imported object is placed in a dedicated collection named after the
    frame folder, so re-running is idempotent and the rest of the scene is left
    untouched.

World-space scene reconstruction is also available (MODE = "world") but it is
only approximate here because Rise of Eros' projection matrix is not recoverable
from the ripper log (it lives in undecoded D3D constant buffers).

Run it inside the already-open Blender (via Blender MCP):
    exec(open(r'E:\\code\\othercode\\ninja_ripper\\import_frame.py').read())

Or headless:
    blender --background --python import_frame.py
"""

import bpy
import os
import re
import glob
import time
import math
import addon_utils
from mathutils import Matrix

# --------------------------------------------------------------------------- #
# Config — edit these (or set the matching globals before exec()'ing the file) #
# --------------------------------------------------------------------------- #
FRAME_DIR = globals().get(
    "FRAME_DIR",
    r"C:\Users\haoni\AppData\Roaming\Ninja Ripper"
    r"\2026.06.28_11.49.25_RiseOfEros.exe_24956\frame_1",
)
MODE = globals().get("MODE", "prevs")          # "prevs" = Local/T-pose | "world" = world-space
CLEAR_COLLECTION = globals().get("CLEAR_COLLECTION", True)
# Game models are usually Y-up; Blender is Z-up. Rotate +90deg about X so the
# character stands upright. Geometry is unchanged, only the orientation.
STAND_UPRIGHT = globals().get("STAND_UPRIGHT", True)
# Only used when MODE == "world" (capture was 2560x1600; FOV is a guess):
WORLD_SCR_W, WORLD_SCR_H, WORLD_FOV = 2560.0, 1600.0, 45.0

ADDON = "io_import_nr"


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _natural_key(path):
    """Sort mesh_2.nr before mesh_10.nr."""
    m = re.search(r"(\d+)", os.path.basename(path))
    return int(m.group(1)) if m else -1


def ensure_addon():
    """Make sure the Ninja Ripper 2 importer is enabled; return its operators."""
    enabled = ADDON in {m.__name__ for m in addon_utils.modules() if
                         addon_utils.check(m.__name__)[1]}
    if not enabled:
        addon_utils.enable(ADDON, default_set=True, persistent=True)
    if not hasattr(bpy.ops.import_mesh_prevs, "nr"):
        raise RuntimeError(
            "Addon '%s' did not register its operators. Is it installed in "
            "Blender's addons folder?" % ADDON
        )


def get_clean_collection(name):
    """Return a collection `name`, emptied first if CLEAR_COLLECTION is set."""
    coll = bpy.data.collections.get(name)
    if coll is not None:
        if CLEAR_COLLECTION:
            for ob in list(coll.objects):
                bpy.data.objects.remove(ob, do_unlink=True)
    else:
        coll = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(coll)
    return coll


def set_active_collection(coll):
    """Point bpy.context.collection at `coll` so the importer links objects there."""
    vl = bpy.context.view_layer

    def find(layer_coll):
        if layer_coll.collection is coll:
            return layer_coll
        for child in layer_coll.children:
            hit = find(child)
            if hit:
                return hit
        return None

    lc = find(vl.layer_collection)
    if lc:
        vl.active_layer_collection = lc


def mesh_files(frame_dir):
    files = sorted(glob.glob(os.path.join(frame_dir, "mesh_*.nr")), key=_natural_key)
    return files


def summarize(objects):
    """Print a per-object + aggregate report so correctness is easy to verify."""
    total_v = total_f = 0
    n_with_uv = n_with_mat = n_with_img = 0
    images, missing = set(), set()

    print("-" * 72)
    print("%-16s %9s %9s  %-3s %-3s %s" % ("object", "verts", "faces",
                                           "uv", "mat", "image"))
    print("-" * 72)
    for ob in objects:
        if ob.type != "MESH":
            continue
        me = ob.data
        nv, nf = len(me.vertices), len(me.polygons)
        total_v += nv
        total_f += nf
        has_uv = bool(me.uv_layers)
        mat = me.materials[0] if me.materials else None
        img_name = ""
        if has_uv:
            n_with_uv += 1
        if mat:
            n_with_mat += 1
            if mat.use_nodes:
                for node in mat.node_tree.nodes:
                    if node.type == "TEX_IMAGE" and node.image:
                        img = node.image
                        img_name = img.name
                        if tuple(img.size) == (0, 0):
                            missing.add(img.filepath)
                        else:
                            images.add(img.filepath)
                        break
            if img_name:
                n_with_img += 1
        print("%-16s %9d %9d  %-3s %-3s %s" % (
            ob.name[:16], nv, nf, "Y" if has_uv else "-",
            "Y" if mat else "-", os.path.basename(img_name)))

    print("-" * 72)
    print("objects: %d  |  verts: %d  faces: %d" % (len(objects), total_v, total_f))
    print("with UV: %d  with material: %d  with image: %d"
          % (n_with_uv, n_with_mat, n_with_img))
    print("textures loaded: %d  |  textures FAILED: %d"
          % (len(images), len(missing)))
    for f in sorted(missing):
        print("   FAILED image:", f)
    print("-" * 72)


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def main():
    t0 = time.time()
    if not os.path.isdir(FRAME_DIR):
        raise RuntimeError("Frame folder not found: %s" % FRAME_DIR)

    ensure_addon()

    files = mesh_files(FRAME_DIR)
    if not files:
        raise RuntimeError("No mesh_*.nr files in %s" % FRAME_DIR)
    file_elems = [{"name": os.path.basename(f)} for f in files]

    coll_name = "%s_%s" % (os.path.basename(FRAME_DIR.rstrip("\\/")), MODE)
    coll = get_clean_collection(coll_name)
    set_active_collection(coll)

    before = set(bpy.data.objects)
    directory = FRAME_DIR.rstrip("\\/") + os.sep

    print("Importing %d .nr files from %s  (mode=%s)" % (len(files), FRAME_DIR, MODE))
    if MODE == "prevs":
        bpy.ops.import_mesh_prevs.nr(
            directory=directory,
            files=file_elems,
            vertexLayoutTab="AUTO",
            texturingTab="AUTO",
            normalsTab="AUTO",
        )
    elif MODE == "world":
        bpy.ops.import_mesh.nr(
            directory=directory,
            files=file_elems,
            projTab="MANUAL",
            scrWidth=WORLD_SCR_W,
            scrHeight=WORLD_SCR_H,
            fov=WORLD_FOV,
            texturingTab="AUTO",
            normalsTab="AUTO",
        )
    else:
        raise RuntimeError("Unknown MODE %r (use 'prevs' or 'world')" % MODE)

    new_objs = [o for o in bpy.data.objects if o not in before]

    # The importer links new objects to the active collection; if anything
    # slipped into the scene root collection, move it into ours too.
    for ob in new_objs:
        if coll.name not in [c.name for c in ob.users_collection]:
            for c in ob.users_collection:
                c.objects.unlink(ob)
            coll.objects.link(ob)

    # Y-up (game) -> Z-up (Blender): stand the models upright about world origin.
    if STAND_UPRIGHT:
        rot = Matrix.Rotation(math.radians(90.0), 4, "X")
        for ob in new_objs:
            ob.matrix_world = rot @ ob.matrix_world

    summarize(new_objs)
    print("Done in %.1fs -> collection '%s'" % (time.time() - t0, coll.name))
    return new_objs


if __name__ == "__main__" or True:
    # `or True` so `exec(open(...).read())` (no __main__) still runs it.
    main()
