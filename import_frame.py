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

# Replace the addon's AUTO texturing with the correct albedo per mesh. The .nr
# records every bound texture at bindSlot 0 (no diffuse/normal/mask distinction),
# so AUTO often picks a normal/flow-mask (e.g. green hair) or nothing at all. We
# gather all textures bound to each mesh's geometry across every render pass and
# choose the albedo by content (skip normal/blue/packed/pure-mask/black + shared
# LUTs), then wire it into Base Color. See fix_materials().
FIX_MATERIALS = globals().get("FIX_MATERIALS", True)

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

    # Snapshot vertex counts now, while every draw still exists as an object
    # (bridges may reference draws that get removed as duplicates below).
    vc_of = {n: len(bpy.data.objects[n].data.vertices) for n in Ms}

    def _vc(name):
        return vc_of.get(name, -1)

    def _npmat(a):
        return Matrix([[float(a[i][j]) for j in range(4)] for i in range(4)])

    # Pass 1: keep the meshes that land near the reference (ref's own render pass).
    kept, removed, deferred = [], 0, []
    kept_by_vc = {}
    for ob in list(objs):
        if ob.type != "MESH" or ob.name not in Ms:
            continue
        rel = M_ref_inv @ Ms[ob.name]
        rel = rel / rel[3, 3]
        placed = (rel @ np.append(pre_c[ob.name], 1.0))[:3]
        if float(np.linalg.norm(placed - ref_c)) <= keep_radius:
            ob.matrix_world = _npmat(rel) @ ob.matrix_world
            kept.append(ob)
            kept_by_vc.setdefault(_vc(ob.name), ob)
        else:
            deferred.append(ob)

    # Pass 2: a deferred draw is either a duplicate of a kept part (same vertex
    # count -> another render pass of the same mesh; drop it) or a UNIQUE part that
    # only exists in another pass (e.g. the eyeballs). The latter can't be placed by
    # M_ref because its View differs, but it CAN be anchored through a "bridge": a
    # draw sharing its pass whose geometry WAS kept. Then, with K/bridge being the
    # same mesh in two passes (World cancels):
    #     ob_world = K.matrix_world @ (M_bridge^-1 @ M_ob)  ==  World_ref^-1 @ World_ob
    recovered = 0
    for ob in deferred:
        if _vc(ob.name) in kept_by_vc:
            bpy.data.objects.remove(ob, do_unlink=True)
            removed += 1
            continue
        anchor = None
        for bn in Ms:
            if bn == ob.name or _vc(bn) not in kept_by_vc:
                continue
            relb = np.linalg.inv(Ms[bn]) @ Ms[ob.name]
            relb = relb / relb[3, 3]
            # same pass  <=>  projection cancels  <=>  affine, no perspective row
            if (abs(relb[3, 0]) + abs(relb[3, 1]) + abs(relb[3, 2]) < 1e-4
                    and 0.5 < abs(np.linalg.det(relb[:3, :3])) < 2.0):
                anchor = (kept_by_vc[_vc(bn)], relb)
                break
        if anchor:
            K, relb = anchor
            ob.matrix_world = K.matrix_world @ _npmat(relb)
            kept.append(ob)
            recovered += 1
        else:
            bpy.data.objects.remove(ob, do_unlink=True)
            removed += 1
    print("reconstruct: ref=%s  kept %d same-pass + %d recovered cross-pass, removed %d copies"
          % (ref, len(kept) - recovered, recovered, removed))
    return kept


# --------------------------------------------------------------------------- #
# Correct-albedo material fix                                                  #
# --------------------------------------------------------------------------- #
def _tex_by_vcount(frame_dir, nrfile):
    """vertex-count -> set of texture filenames bound to that geometry anywhere.

    The same mesh is drawn several times (color/depth/shadow passes); only some
    passes bind the full texture set. Keying by PreVS vertex count groups all
    passes of one part, so a kept mesh whose own draw bound no texture still finds
    its textures from a sibling pass.
    """
    table = {}
    for f in sorted(glob.glob(os.path.join(frame_dir, "mesh_*.nr")), key=_natural_key):
        nr = nrfile.NRFile()
        if not nr.parse(f):
            continue
        for i in range(nr.getMeshCount()):
            g = nr.getMesh(i)
            v = g.getVertexes(0)
            if not v:
                continue
            txs = g.getTextures()
            if not txs:
                continue
            vc = v.getVertexCount()
            for t in range(txs.getTexturesCount()):
                table.setdefault(vc, set()).add(txs.getTexture(t).fileName)
    return table


def _classify_tex(path, np):
    """Load a .dds and decide if it's an albedo; return (is_albedo, score, has_alpha).

    Albedo = natural skin/cloth tone (R>=G>=B-ish, moderate saturation) or a light
    grey with strand-alpha (silver hair). Rejected: normal maps (blue-dominant or
    the R~1,G~B~0.5 pack), pure-hue masks, black/undecodable LUTs.
    """
    pre = set(bpy.data.images.keys())
    try:
        img = bpy.data.images.load(path, check_existing=True)
    except Exception:
        return (False, 0.0, False)
    new = img.name not in pre
    w, h = img.size
    out = (False, 0.0, False)
    try:
        if w * h > 0 and img.has_data:
            a = np.empty(w * h * 4, np.float32)
            img.pixels.foreach_get(a)
            a = a.reshape(-1, 4)
            if len(a) > 40000:
                a = a[np.linspace(0, len(a) - 1, 40000).astype(np.int64)]
            rgb = a[:, :3]
            m = rgb.mean(0)
            sat = float(((rgb.max(1) - rgb.min(1)) / (rgb.max(1) + 1e-6)).mean())
            amin = float(a[:, 3].min())
            R, G, B = (float(m[0]), float(m[1]), float(m[2]))
            packed = (R > 0.85 and 0.40 < G < 0.62 and 0.40 < B < 0.62)
            blue = (B > R + 0.15 and B > 0.55)
            puremask = ((G > 0.55 and R < 0.20) or (R > 0.55 and G < 0.20 and B < 0.20))
            black = (R < 0.05 and G < 0.05 and B < 0.05)
            graylo = sat < 0.10
            natural = (R + 0.03 >= G >= B - 0.06) and 0.10 <= sat <= 0.65
            is_albedo = (not (packed or blue or puremask or black)
                         and (natural or (graylo and amin < 0.6 and float(m.mean()) > 0.4)))
            score = (2.0 if natural else (1.0 if graylo else 0.0)) + min(w, 2048) / 2048.0
            out = (bool(is_albedo), float(score), bool(amin < 0.6))
    except Exception:
        pass
    if new and img.users == 0:
        bpy.data.images.remove(img)
    return out


def _coherent_uv(me, np):
    """Name of the UV layer whose islands are spatially continuous.

    The PreVS import dumps every texcoord set as uv_0..uv_N, but only one is the
    real diffuse UV; the rest are mis-unpacked and come out as per-vertex noise
    (huge per-edge jumps in UV space), which paints any texture as speckle. The
    correct layer has, by far, the smallest mean per-edge UV jump (~0.01 vs ~0.5).
    This is why the head needs uv_5 while the body/armor need uv_7.
    """
    if not me.uv_layers:
        return None
    nloops = len(me.loops)
    pairs = []
    for p in me.polygons:
        ls = list(p.loop_indices)
        for k in range(len(ls)):
            pairs.append((ls[k], ls[(k + 1) % len(ls)]))
    if not pairs:
        return me.uv_layers[0].name
    pairs = np.asarray(pairs)
    best = None
    for L in me.uv_layers:
        uv = np.empty(nloops * 2, np.float32)
        L.data.foreach_get("uv", uv)
        uv = uv.reshape(-1, 2)
        d = float(np.linalg.norm(uv[pairs[:, 0]] - uv[pairs[:, 1]], axis=1).mean())
        if best is None or d < best[1]:
            best = (L.name, d)
    return best[0]


def _set_albedo_material(ob, img_path, with_alpha, uv_name):
    img = bpy.data.images.load(img_path, check_existing=True)
    img.colorspace_settings.name = "sRGB"
    mat = bpy.data.materials.new("mat_%s" % _file_token(ob.name))
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial"); out.location = (300, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location = (0, 0)
    tex = nt.nodes.new("ShaderNodeTexImage"); tex.location = (-400, 0); tex.image = img
    nt.links.new(bsdf.inputs["Base Color"], tex.outputs["Color"])
    if with_alpha:
        nt.links.new(bsdf.inputs["Alpha"], tex.outputs["Alpha"])
        mat.blend_method = "HASHED"
        mat.shadow_method = "HASHED"
    nt.links.new(out.inputs["Surface"], bsdf.outputs["BSDF"])
    me = ob.data
    # The Image Texture node has no UV Map node, so it samples the *render*-active
    # UV; point that (and the edit-active) at the continuous diffuse layer.
    if uv_name and uv_name in me.uv_layers:
        for L in me.uv_layers:
            L.active_render = (L.name == uv_name)
        me.uv_layers.active = me.uv_layers[uv_name]
    me.materials.clear()
    me.materials.append(mat)


def fix_materials(objs, frame_dir):
    """Give each kept mesh its correct albedo (Base Color), replacing AUTO guesses."""
    try:
        import numpy as np
        import nrfile  # noqa: F401  (import side effect: ensure available)
    except Exception as e:
        print("fix_materials: numpy/nrfile unavailable (%s); skipping" % e)
        return
    import nrfile as _nr
    from collections import Counter

    meshes = [o for o in objs if o.type == "MESH"]
    if not meshes:
        return
    table = _tex_by_vcount(frame_dir, _nr)
    cand = {o.name: set(table.get(len(o.data.vertices), set())) for o in meshes}
    # Textures bound to many parts are shared LUTs/ramps, never a part's albedo.
    freq = Counter(t for s in cand.values() for t in s)
    shared = {t for t, c in freq.items() if c >= max(3, len(cand) // 2)}

    fixed = 0
    for o in meshes:
        best = None  # (score, filename, has_alpha)
        for tex in sorted(cand[o.name] - shared):
            p = os.path.join(frame_dir, tex)
            ok, score, has_alpha = _classify_tex(p, np)
            if ok and (best is None or score > best[0]):
                best = (score, tex, has_alpha)
        if best:
            uv_name = _coherent_uv(o.data, np)
            _set_albedo_material(o, os.path.join(frame_dir, best[1]), best[2], uv_name)
            fixed += 1
            print("fix_materials: %-12s -> %s  uv=%s%s"
                  % (o.name, best[1], uv_name, "  (+alpha)" if best[2] else ""))
        else:
            print("fix_materials: %-12s -> no albedo candidate found" % o.name)
    print("fix_materials: set albedo on %d/%d meshes" % (fixed, len(meshes)))


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

    # Replace the addon's AUTO texturing with the correct albedo per mesh.
    if FIX_MATERIALS and MODE == "prevs":
        fix_materials(new_objs, FRAME_DIR)

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
