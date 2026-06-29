# ninja_ripper → Blender（中文说明）

> English: [README.md](README.md) ｜ 几何重建与材质修复的深度文档见 [docs/material-fix.md](docs/material-fix.md)

把 **Ninja Ripper 2** 抓取的一帧（`.nr` 网格 + 同目录 `.dds` 贴图）导入正在运行的
Blender，并自动完成**几何、UV、拆边法线、贴图**的正确绑定，最终得到一个
**站立、落地、贴图正确**的角色。

脚本不自己解析私有 `.nr` 二进制，而是**驱动官方 `io_import_nr` 插件**（目标 Blender 3.6
里已安装），因此导入结果与插件本身完全一致；在此基础上再做投影无关的重建与材质修复。

---

## 导入模式

| 模式 | 调用算子 | 几何 | 适用 |
|------|----------|------|------|
| `prevs`（默认） | `import_mesh_prevs.nr` | **精确**的模型空间顶点，无需投影矩阵 | 提取模型（T-pose / 本地空间）|
| `world` | `import_mesh.nr` | 反投影出的世界空间，**近似** | 还原场景布局；真正的投影矩阵不在 ripper 日志里，故不精确 |

两种模式都会自动绑定 DDS 贴图（`texturingTab=AUTO`）和拆边法线（`normalsTab=AUTO`），
并把对象收进一个独立集合 `<frame>_<mode>`，重复运行幂等。

推荐走 `prevs`：**精确几何 + 正确位置**（位置由下面的重建解算）。

---

## 流水线（`main()`）

```
ensure_addon()              # 启用官方 io_import_nr 插件
purge_empty_addon_groups()  # 清掉插件上次残留的空 grp_* 集合
import_mesh_prevs.nr(...)   # 以 PreVS(本地空间) 导入全部 mesh_*.nr
reconstruct()               # 投影无关的 clip-space 重建（拼身体 + 去重复 pass + 跨 pass 恢复眼球）
STAND_UPRIGHT               # 最长包围盒轴 → +Z，立正
GROUND_SNAP                 # 落地：脚→Z=0、X/Y 居中
fix_materials()             # 每个网格挑正确 albedo + 正确 UV 层（眼球等特例走 ALBEDO_OVERRIDES）
purge_empty_addon_groups()  # 再清一次本次导入产生的空 grp_*
summarize()                 # 打印逐网格 + 汇总报告
```

逻辑全在 `import_frame.py` 一个文件里，**没有硬编码的网格名 / 贴图名 / UV 层号**，
换帧/换模型通常只需改 `FRAME_DIR`。

---

## 投影无关的重建（`prevs` 模式核心）

PreVS 里每个 draw 都在各自的骨骼/本地原点，头、发、眼会散落（头的绑定原点在骨盆而非
脖子）。正确摆放只存在于 PostVS（裁剪空间 = `Proj · View · World · local`），而真正的
投影矩阵**不在** ripper 日志里。

**技巧**：每个 draw 同时存了 PreVS(local) 与 PostVS(clip) 顶点，一一对应。对
`PostVS = M · PreVS` 解出 `M_i = Proj · View · World_i`，取一个参考网格后

```
rel_i = M_ref⁻¹ · M_i = World_ref⁻¹ · World_i
```

未知的 `Proj`、`View` **精确抵消**，`rel_i` 即网格 i 在参考空间里的精确刚体摆放。应用它
就能用精确几何拼出角色，全程不需要投影矩阵。

Ninja Ripper 把每个 draw 录多次（主相机 / 阴影 / 深度，view 不同），其它 pass 的副本会被
摆到很远；`reconstruct()` 只保留参考网格附近（同一 pass）的件，丢掉重复副本。眼球/睫毛这种
**只在另一 pass 出现**的件，再用「顶点数 + 同 pass 桥」自动跨 pass 找回。

> 这一步是纯数学，模型无关。详细推导（含跨 pass 桥接、眼球恢复）见
> [docs/material-fix.md](docs/material-fix.md)。

---

## 材质修复要点

`.nr` 的贴图块里**每张图的 bindSlot 都是 0**（不区分 diffuse/法线/遮罩），插件 AUTO 经常
贴错（典型：头发被贴成绿色流向遮罩）。修复思路：

1. **按 PreVS 顶点数聚类**同一部件在所有 pass 里绑过的全部贴图（顶点数是天然主键）；
2. 去掉跨部位共享的环境/LUT 图（出现在 ≥ 半数网格候选里的）；
3. **按内容分类**挑出真正的 albedo（排除法线/打包图/纯色遮罩/全黑图）；
4. **按 UV 连续性**选对 UV 层（正确层空间连续 ≈ 0.01，噪声层 ≈ 0.5），设为 `active_render`；
5. 接到 Principled `Base Color`，有透明（发丝/镂空护甲）再接 `Alpha` 并设 `HASHED`。

眼球这种"内容/UV 都猜不准"的特例，用 `ALBEDO_OVERRIDES`（按顶点数钉死贴图+UV）显式覆盖。

---

## 工作流（Mac 写、远端 Windows 跑）

```
Mac:        编辑 import_frame.py  ->  git push
Win:        cd E:\code\othercode\ninja_ripper && git pull
Win Blender: exec(open(r'E:\code\othercode\ninja_ripper\import_frame.py').read())
```

Blender 需开启并启动 BlenderMCP 插件（以便发送代码），或无头运行：

```
blender --background --python import_frame.py
```

---

## 配置开关

在 `import_frame.py` 顶部改常量，或在 `exec` 前设同名全局变量覆盖默认值：

| 变量 | 默认 | 作用 / 何时调 |
|------|------|---------------|
| `FRAME_DIR` | 本帧路径 | **换模型必改**，指向新的 `frame_N` 目录（`.dds` 必须与 `.nr` 同目录）|
| `MODE` | `"prevs"` | `prevs`=精确本地几何（推荐）；`world`=插件反投影（近似）|
| `CLEAR_COLLECTION` | — | 重导入前清空同名集合 |
| `RECONSTRUCT` | `True` | 关掉则只导入不拼装（仅 `prevs`）|
| `RECON_REF` | `None` | 指定参考网格名；默认取顶点最多的（主身体）|
| `RECON_KEEP_FACTOR` | `2.5` | 去重半径系数；有合法部件被误删就调大 |
| `FIX_MATERIALS` | `True` | 关掉则保留插件 AUTO 材质 |
| `STAND_UPRIGHT` | `True` | 立正（最长轴 → +Z）|
| `UPRIGHT_FLIP` | `False` | 模型上下颠倒时设 `True` |
| `GROUND_SNAP` | `True` | 落地：最低点 → Z=0、X/Y 居中到原点（脚踩地平线）|
| `ALBEDO_OVERRIDES` | `{458:(30D730,uv_0)}` | 按顶点数钉死贴图+UV（眼球等特例）；换模型设 `{}` |

脚本会打印逐对象报告（顶点/面/有无 UV/有无材质/贴图名）和贴图加载成功/失败数，便于核对。

---

## 换模型如何适配

整条流水线是**算法化**的、不含本角色专属常量，理论上换 NR2 抓帧可直接复用：

1. 把 `FRAME_DIR` 指向新的 `frame_N` 目录；
2. （多数情况下）`ALBEDO_OVERRIDES` 设为 `{}`；
3. 跑 `exec(open(r'...import_frame.py').read())`；
4. **强烈建议**用 `frame_N/!screenshot.dds`（游戏内截图）做一次目检校准。

注意：内容分类阈值是对照本角色（写实人形）调出来的合理默认，换**美术风格差异很大**的模型
（机甲、鲜艳卡通、异色生物）时 albedo 判定可能需要微调；`STAND_UPRIGHT` 的"最长轴"启发式
对个别姿势可能判错，用 `UPRIGHT_FLIP` 或手动旋转兜底。

---

## 已知限制（非阻塞）

- 当前只重建到正确的 **base color（颜色层）**；法线贴图、卡通 ramp、环境反射尚未接。
- **眼球贴图仍不完美**：单张 base-color 无法还原游戏"眼白 + 正面虹膜 + 瞳孔 + 高光"的
  分层 shader，当前虹膜偏淡、整体略一色。
- 个别极小薄片（如本例 `mesh_29`）无 albedo 候选，影响可忽略。
- `!screenshot.dds` 是无压缩 DDS，Blender 解不了，需按位掩码手动解码。
