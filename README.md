# ninja_ripper → Blender

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
- `MODE` — `"prevs"` or `"world"`.
- `CLEAR_COLLECTION` — empty a previous import of the same name before re-importing.

The script prints a per-object report (verts / faces / has-UV / has-material /
texture name) plus how many textures loaded vs. failed, so correctness is easy
to confirm.
