"""
import_frame.py
================
Import a Ninja Ripper 2 capture frame folder into a running Blender, using the
official `io_import_nr` addon for geometry/UV/textures, then assemble the
character correctly with a projection-free clip-space reconstruction.

Why reconstruction is needed
----------------------------
In PreVS (local space) every draw comes in at its own bone/local origin, so the
head, hair and eyes land away from the body (the head's bind origin is the
pelvis, not the neck). The correct placement lives only in the PostVS data,
which is clip space = Proj * View * World * local — and Rise of Eros' real
projection matrix is not in the ripper log.

The trick: each draw stores BOTH PreVS (local) and PostVS (clip) vertices, 1:1.
Solving the 4x4 M with  PostVS = M @ PreVS  gives  M_i = Proj * View * World_i.
For a reference mesh,
        rel_i = M_ref^-1 @ M_i = World_ref^-1 @ World_i
and the unknown Proj and View cancel exactly. rel_i is the exact rigid placement
of mesh i in the reference's space, so applying it assembles the character with
EXACT geometry and CORRECT placement, with no projection matrix at all.

Ninja Ripper records each draw several times (main camera, shadow, depth) with
different view matrices, so other-pass copies land far away; we keep only the
meshes near the reference (its own pass) and drop the duplicates.

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
import sys
import addon_utils
from mathutils import Matrix, Vector

# --------------------------------------------------------------------------- #
# Config — edit these (or set the matching globals before exec()'ing the file) #
# --------------------------------------------------------------------------- #
FRAME_DIR = globals().get(
    "FRAME_DIR",
    r"C:\Users\haoni\AppData\Roaming\Ninja Ripper"
    r"\2026.06.28_11.49.25_RiseOfEros.exe_24956\frame_1",
)
MODE = globals().get("MODE", "prevs")          # "prevs" = exact local geometry | "world" = addon's reverse-projection
CLEAR_COLLECTION = globals().get("CLEAR_COLLECTION", True)

# Projection-free clip-space reconstruction (prevs mode only). Places head/hair/
# eyes/body correctly and removes duplicate render-pass copies. See reconstruct().
RECONSTRUCT = globals().get("RECONSTRUCT", True)
RECON_REF = globals().get("RECON_REF", None)        # reference object name; None = mesh with most verts
RECON_KEEP_FACTOR = globals().get("RECON_KEEP_FACTOR", 2.5)  # keep meshes placed within FACTOR*ref_diag (same pass)

# Game models are usually not Z-up. Rotate the longest bbox axis onto +Z so the
# character stands upright. Geometry is unchanged, only the orientation.
STAND_UPRIGHT = globals().get("STAND_UPRIGHT", True)
UPRIGHT_FLIP = globals().get("UPRIGHT_FLIP", False)   # flip if a model lands upside down

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


def _file_token(obj_name):
    """'mesh_3.nr' / 'mesh_3.nr.001' -> 'mesh_3.nr' (the backing file name)."""
    return re.sub(r"\.\d+$", "", obj_name)


def ensure_addon():
    """Make sure the Ninja Ripper 2 importer is enabled and importable."""
    enabled = ADDON in {m.__name__ for m in addon_utils.modules() if
                        addon_utils.check(m.__name__)[1]}
    if not enabled:
        addon_utils.enable(ADDON, default_set=True, persistent=True)
    if not hasattr(bpy.ops.import_mesh_prevs, "nr"):
        raise RuntimeError(
            "Addon '%s' did not register its operators. Is it installed in "
            "Blender's addons folder?" % ADDON
        )
    # The addon appends its dir to sys.path on enable; make sure nrfile/nrtools
    # are importable for the reconstruction step.
    for mod in addon_utils.modules():
        if mod.__name__ == ADDON:
            d = os.path.dirname(mod.__file__)
            if d not in sys.path:
                sys.path.append(d)
            break


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


def purge_empty_addon_groups():
    """Remove empty 'grp_<n>.nr' collections the importer leaves behind.

    io_import_nr creates one per-file group collection (``grp_0.nr`` ...) on every
    import and links it under the scene root. main() then moves the imported
    meshes into the mode collection, and reconstruct() deletes the duplicate
    passes, so these group collections end up empty. Without this they pile up on
    every re-run (``grp_0.nr.001``, ``.002`` ...) and clutter the outliner.
    Only collections that are genuinely empty (no objects, no children) are
    removed, so real data is never touched.
    """
    removed = 0
    for c in list(bpy.data.collections):
        if (re.match(r"grp_\d+\.nr(\.\d+)?$", c.name)
                and not c.objects and not c.children):
            bpy.data.collections.remove(c)
            removed += 1
    if removed:
        print("cleanup: removed %d empty addon group collection(s)" % removed)
    return removed


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


def _combined_bbox(objs):
    mn = [1e18] * 3
    mx = [-1e18] * 3
    for ob in objs:
        if ob.type != "MESH":
            continue
        for corner in ob.bound_box:
            w = ob.matrix_world @ Vector(corner)
            for i in range(3):
                mn[i] = min(mn[i], w[i])
                mx[i] = max(mx[i], w[i])
    return mn, mx, [mx[i] - mn[i] for i in range(3)]


def _upright_rotation(objs):
    """Rotate the longest bbox axis onto +Z so a character stands up.

    The base mapping is chosen so this game's models land head-up; if a model
    from another game comes in upside down, set UPRIGHT_FLIP=True to add 180deg.
    """
    _, _, dims = _combined_bbox(objs)
    up = dims.index(max(dims))
    if up == 2:                                     # already tall along Z
        base = Matrix.Identity(4)
    elif up == 0:                                   # tall along X
        base = Matrix.Rotation(math.radians(90.0), 4, "Y")
    else:                                           # tall along Y
        base = Matrix.Rotation(math.radians(-90.0), 4, "X")
    if UPRIGHT_FLIP:
        base = Matrix.Rotation(math.radians(180.0), 4, "X") @ base
    return base


# --------------------------------------------------------------------------- #
# Projection-free clip-space reconstruction                                   #
# --------------------------------------------------------------------------- #
def _parse_pre_post(path, nrfile, nrtools, np):
    """Return (pre Nx3 local, post Nx4 clip) for the draw in `path`, or (None,None)."""
    if not os.path.isfile(path):
        return None, None
    nr = nrfile.NRFile()
    if not nr.parse(path):
        return None, None
    pre = post = None
    for i in range(nr.getMeshCount()):
        g = nr.getMesh(i)
        vert = g.getVertexes(0)
        vatrs = g.getVertexAttributes(0)
        if not vert or not vatrs:
            continue
        vd = vert.read()
        pv = vatrs.getAttr(0)
        if g.getShaderStage() == nrfile.ShaderStage.PreVs:
            pos = nrtools.unpackVertexComponentVaAsList(
                vert, vd, vatrs, [[0, 0], [0, 1], [0, 2]])
            if pos:
                pre = np.asarray(pos, dtype=float)
        elif pv.compCount == 4:
            pos = nrtools.unpackVertexComponentAsList(vert, vd, pv)
            if pos:
                post = np.asarray(pos, dtype=float)
    return pre, post


def _solve_M(pre, post, np):
    """Least-squares 4x4 M with  post = M @ [pre,1].  Exact for a rigid draw."""
    A = np.hstack([pre, np.ones((len(pre), 1))])
    MT, _, _, _ = np.linalg.lstsq(A, post, rcond=None)
    return MT.T


def reconstruct(objs, frame_dir):
    """Assemble the character via rel = M_ref^-1 @ M_mesh (projection cancels).

    Places each kept mesh in the reference's space (exact geometry, correct
    placement) and removes other-render-pass duplicates. Returns the kept objects.
    """
    try:
        import numpy as np
        import nrfile
        import nrtools
    except Exception as e:
        print("reconstruct: numpy/nrfile unavailable (%s); skipping" % e)
        return objs

    Ms, pre_c = {}, {}
    for ob in objs:
        if ob.type != "MESH":
            continue
        pre, post = _parse_pre_post(
            os.path.join(frame_dir, _file_token(ob.name)), nrfile, nrtools, np)
        if pre is None or post is None or len(pre) != len(post):
            continue
        Ms[ob.name] = _solve_M(pre, post, np)
        pre_c[ob.name] = pre.mean(0)

    if not Ms:
        print("reconstruct: no PreVS/PostVS pairs found; skipping")
        return objs

    ref = RECON_REF if (RECON_REF in Ms) else max(
        Ms, key=lambda n: len(bpy.data.objects[n].data.vertices))
    M_ref_inv = np.linalg.inv(Ms[ref])
    ref_c = pre_c[ref]
    ref_diag = Vector(bpy.data.objects[ref].dimensions).length or 1.0
    keep_radius = RECON_KEEP_FACTOR * ref_diag

    kept, removed = [], 0
    for ob in list(objs):
        if ob.type != "MESH" or ob.name not in Ms:
            continue
        rel = M_ref_inv @ Ms[ob.name]
        rel = rel / rel[3, 3]
        placed = (rel @ np.append(pre_c[ob.name], 1.0))[:3]
        if float(np.linalg.norm(placed - ref_c)) <= keep_radius:
            relM = Matrix([[float(rel[i][j]) for j in range(4)] for i in range(4)])
            ob.matrix_world = relM @ ob.matrix_world
            kept.append(ob)
        else:
            bpy.data.objects.remove(ob, do_unlink=True)
            removed += 1
    print("reconstruct: ref=%s  kept %d same-pass meshes, removed %d other-pass copies"
          % (ref, len(kept), removed))
    return kept


def mesh_files(frame_dir):
    return sorted(glob.glob(os.path.join(frame_dir, "mesh_*.nr")), key=_natural_key)


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
    purge_empty_addon_groups()      # clear stale empty grp_* from earlier runs

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
            directory=directory, files=file_elems,
            vertexLayoutTab="AUTO", texturingTab="AUTO", normalsTab="AUTO")
    elif MODE == "world":
        bpy.ops.import_mesh.nr(
            directory=directory, files=file_elems,
            projTab="MANUAL", scrWidth=WORLD_SCR_W, scrHeight=WORLD_SCR_H,
            fov=WORLD_FOV, texturingTab="AUTO", normalsTab="AUTO")
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

    # Projection-free assembly: place each mesh correctly and drop pass duplicates.
    if RECONSTRUCT and MODE == "prevs":
        new_objs = reconstruct(new_objs, FRAME_DIR)

    # Stand upright (Z-up).
    if STAND_UPRIGHT:
        rot = _upright_rotation(new_objs)
        if rot is not None:
            for ob in new_objs:
                ob.matrix_world = rot @ ob.matrix_world

    # The importer's per-file group collections are now empty (meshes were moved
    # into our collection and pass-duplicates deleted); drop them so the outliner
    # holds only the assembled character.
    purge_empty_addon_groups()

    summarize(new_objs)
    print("Done in %.1fs -> collection '%s'" % (time.time() - t0, coll.name))
    return new_objs


if __name__ == "__main__" or True:
    # `or True` so `exec(open(...).read())` (no __main__) still runs it.
    main()
