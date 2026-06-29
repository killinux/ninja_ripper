# ninja_ripper → Blender

> 中文说明见 [README.zh-CN.md](README.zh-CN.md)

Import a **Ninja Ripper 2** capture frame (`.nr` meshes + `.dds` textures) into a
running Blender, with geometry, UVs, split normals and textures all bound
correctly.

It drives the official `io_import_nr` addon (already installed in the target
Blender 3.6) rather than re-parsing the private `.nr` binary format, so the
import is guaranteed to match the addon's own results.

## Modes

| Mode | Operator | Geometry | Use for |
|------|----------|----------|---------|
| `prevs` (default) | `import_mesh_prevs.nr` | **Exact** model-space verts, no projection matrix needed | extracting models (T-pose / local space) |
| `world` | `import_mesh.nr` | World-space reconstruction via reverse projection | scene layout — *approximate*, the real projection matrix is not in the ripper log |

Either mode auto-binds DDS textures (`texturingTab=AUTO`) and split normals
(`normalsTab=AUTO`), and drops every object into a dedicated collection named
`<frame>_<mode>` so re-runs are idempotent.

In `prevs` mode a **projection-free reconstruction** then assembles the parts
correctly (see below) — this is the recommended path: exact geometry *and*
correct placement.

## Workflow (Mac writes, remote Windows runs)

```
Mac: edit import_frame.py  ->  git push
Win: cd E:\code\othercode\ninja_ripper && git pull
Win Blender: exec(open(r'E:\code\othercode\ninja_ripper\import_frame.py').read())
```

Blender must be open with the BlenderMCP addon started (so code can be sent in),
or run headless:

```
blender --background --python import_frame.py
```

## Config

Edit the constants at the top of `import_frame.py`:

- `FRAME_DIR` — the `...\frame_N` folder to import (the `.dds` files must sit
  next to the `.nr` files, which is how Ninja Ripper exports them).
- `MODE` — `"prevs"` (recommended) or `"world"`.
- `CLEAR_COLLECTION` — empty a previous import of the same name before re-importing.
- `RECONSTRUCT` / `RECON_REF` / `RECON_KEEP_FACTOR` — see below.

The script prints a per-object report (verts / faces / has-UV / has-material /
texture name) plus how many textures loaded vs. failed, so correctness is easy
to confirm.

## Projection-free reconstruction (`prevs` mode)

In PreVS each draw comes in at its own bone/local origin, so the head, hair and
eyes land away from the body (the head's bind origin is the pelvis, not the
neck). The correct placement only exists in the PostVS data, which is clip space
`Proj · View · World · local` — and the real projection matrix is **not** in the
ripper log.

The key: each draw stores **both** PreVS (local) and PostVS (clip) vertices, 1:1.
Solving the 4×4 `M` with `PostVS = M · PreVS` gives `M_i = Proj · View · World_i`,
so for a reference mesh

```
rel_i = M_ref⁻¹ · M_i = World_ref⁻¹ · World_i
```

and **`Proj` and `View` cancel exactly**. `rel_i` is the exact rigid placement of
mesh `i` in the reference's space, so applying it assembles the character with
exact geometry and correct placement — no projection matrix needed.

Ninja Ripper records each draw several times (main camera, shadow, depth) with
different view matrices, so other-pass copies land far away. `reconstruct()`
keeps only the meshes near the reference (its own pass) and removes the rest, so
e.g. this frame's 29 imported objects collapse to the **8** that form one clean
character.

- `RECONSTRUCT` — enable (default `True`, `prevs` mode only).
- `RECON_REF` — reference object name; `None` auto-picks the mesh with the most
  vertices (the main body).
- `RECON_KEEP_FACTOR` — keep meshes whose placed centroid is within
  `FACTOR × reference_diagonal` (default `2.5`); separates the reference's pass
  from the far-away duplicate passes.
