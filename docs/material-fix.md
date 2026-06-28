# Ninja Ripper 2 → Blender：几何重建与材质修复说明

本文记录 `import_frame.py` 如何把 Ninja Ripper 2 抓取的一帧（`frame_N` 目录）
还原成「站立、贴图正确」的角色，以及修复过程中踩到的两个材质坑和它们的根因 /
解法。校准用的真实参考是游戏内截图（`frame_N/!screenshot.dds`，以及外部参考图）。

---

## 1. 整体流水线（`main()`）

```
ensure_addon()                # 启用官方 io_import_nr 插件
purge_empty_addon_groups()    # 清掉插件上次残留的空 grp_* 集合
import_mesh_prevs.nr(...)     # 以 PreVS(本地空间) 导入全部 mesh_*.nr
  └─ 把新对象从 grp_* 收拢进 frame_N_prevs 集合
reconstruct()                 # 投影无关的 clip-space 重建（拼装身体 + 去重复 pass）
STAND_UPRIGHT                 # 最长包围盒轴 → +Z，立正
fix_materials()               # 每个网格挑正确 albedo + 正确 UV 层
purge_empty_addon_groups()    # 再清一次本次导入产生的空 grp_*
summarize()                   # 打印逐网格 + 汇总报告
```

全部逻辑都在 `import_frame.py` 一个文件里。换一帧/换模型**只需改 `FRAME_DIR`**，
逻辑里没有任何硬编码的网格名 / 贴图名 / UV 层号（那些只出现在注释举例中）。

---

## 2. 几何：投影无关的 clip-space 重建（`reconstruct()`）

**为什么需要**：PreVS（本地空间）里每个 draw 都在各自的骨骼/本地原点，头、发、眼
会散落（头的绑定原点在骨盆而非脖子）。正确摆放只存在于 PostVS（裁剪空间 =
`Proj · View · World · local`），而游戏真正的投影矩阵不在 ripper 日志里。

**技巧**：每个 draw 同时存了 PreVS(local) 与 PostVS(clip) 顶点，一一对应。对
`PostVS = M · PreVS` 解出 `M_i = Proj · View · World_i`。取一个参考网格后

```
rel_i = M_ref⁻¹ · M_i = World_ref⁻¹ · World_i
```

未知的 `Proj`、`View` 精确抵消，`rel_i` 就是网格 i 在参考空间里的刚体摆放。应用它
即可用**精确几何 + 正确位置**拼出角色，全程不需要投影矩阵。

Ninja Ripper 把每个 draw 录了多次（主相机 / 阴影 / 深度，view 矩阵不同），其它 pass
的副本会被 `rel` 摆到很远处；按距参考网格 `RECON_KEEP_FACTOR × ref_diag` 的半径
只保留同一 pass 的网格，丢掉重复副本。

> 这一步是纯数学，与具体模型无关：任何带 PreVS+PostVS 的 NR2 抓帧都适用。

### 2.1 头的位置是怎么修正的（演进史）

"头飘在身体外 / 接不到脖子上"是这套流程最难啃的一块，前后换过几代做法（见 git
历史）：

1. **自动判朝向**（`dc89809`）：用最长包围盒轴当 up 轴做 STAND_UPRIGHT。
2. **质心判正负 + 手动 flip**（`5309ab9`）：靠质心猜上下，再加 `UPRIGHT_FLIP` 兜底。
3. **丢弃质心翻转，固定 head-up 基准旋转**（`a7e5a63`）：质心启发式不稳，去掉。
4. **专门的"头复位"启发式**（`3e4ff48`）：把蒙皮的头部组**硬贴到身体颈部**——靠几何
   位置猜，换姿势/换角色就失准。
5. **最终：用投影无关的 clip-space 重建替换所有 head-fix 启发式**（`5b1f58b`）：
   头不再"猜位置"，而是和其它部件一样用 `rel = M_ref⁻¹ · M_i` **精确摆放**。

**结论**：头的位置不该用启发式去"凑"，而是和全身一起由第 2 节的 clip-space 重建
**精确解算**——这才彻底稳定。头之所以最早出问题，是因为它的绑定原点在骨盆而非颈部，
PreVS 本地坐标完全摆错；只有 PostVS(clip) 里才有正确位置，正是重建利用的信息。

### 2.2 跨 pass 丢件恢复（眼球）——`reconstruct()` 第 2 步

重建按"距参考网格的半径"只保留**与参考同一渲染 pass** 的网格。但有些部件（**眼球**、
睫毛）**只在另一个 pass 里出现**，它们的 `M` 含不同的 View，`rel = M_ref⁻¹·M_i` 会把
它们摆飞 → 早期被当垃圾删掉，于是"眼睛丢了"。

**修法（桥接重锚定）**：对一个顶点数独一无二、被延后的件 `X`，在它**自己的 pass** 里找
一个"桥" `B`——`B` 是某个**已保留件 `K` 的同几何**（同顶点数、不同 pass）。因为同一
mesh 在两个 pass 里世界坐标相同（World 相消）：

```
X_world = K.matrix_world @ (M_B⁻¹ · M_X)  ==  World_ref⁻¹ · World_X   （正确刚体位置）
```

判定同 pass：`M_B⁻¹·M_X` 归一化后**无透视行**（底行≈[0,0,0,1]）且行列式≈±1。
实测眼球(mesh_15)的两颗球精确落进眼眶（rel det=1.0）。**重复 pass 的副本**（顶点数与
已保留件相同）仍照常丢弃。

> 同样是模型无关的：靠"顶点数 + 同 pass 桥"自动恢复，不认具体网格。

---

## 3. 材质：两个坑

导入后角色"投影/几何对了，但材质不对"。逐项排查发现**两个独立的根因**。

### 坑 1 — albedo 选错（插件 AUTO 不可靠）

`.nr` 的 TXTR 块里**每张贴图的 `bindSlot` 都记成 0**（格式没区分 diffuse / 法线 /
遮罩），所以插件的 AUTO 贴图：

- 经常把法线 / 流向遮罩当颜色（典型：头发被贴成绿色，实际用的是绿+品红的**发丝流
  向 ID 遮罩** `30D170`，而真正银发 albedo 是 `A9E770`）；
- 或者根本不贴（reconstruct 保留的那个 draw 的 TXTR 是空的，颜色 pass 的贴图在另一
  个同几何的 draw 上）。

**关键观察**：在「多图 pass」里，贴图顺序是 `[环境图, albedo, 法线, 遮罩…]`——
第 0 张是**跨部位共享的环境/反射图**（如 `6BA070`，Blender 还解不了），**第 1 张才是
albedo**。

**解法（`fix_materials` + `_tex_by_vcount` + `_classify_tex`）**：

1. `_tex_by_vcount()`：按 **PreVS 顶点数**把"同一几何在所有渲染 pass 里绑过的全部
   贴图"聚到一起（顶点数是同一部件不同 pass 的天然主键），这样即使保留的 draw 没绑
   图，也能从兄弟 pass 找到它的贴图。
2. 去掉**跨部位共享**的贴图（出现在 ≥ 半数网格候选里的 = LUT/ramp/环境图，非某部件
   的 albedo）。
3. `_classify_tex()` 按**内容**判定 albedo：读图采样，算均值 / 饱和度 / alpha，排除
   - 法线 / 打包图：蓝色主导，或 `R≈1, G≈B≈0.5` 的图案；
   - 纯色遮罩：某通道接近 0、另一通道很高；
   - 全黑 / 解码失败的图；
   保留**自然肤/布色调**（`R≳G≳B`、中等饱和、不透明）或**浅灰带 alpha**（银发）。
4. `_set_albedo_material()` 把选中的 albedo 接到 Principled `Base Color`；有透明
   （发丝 / 镂空护甲图集）时再接 `Alpha` 并设 `HASHED` 混合。

### 坑 2 — UV 层选错（**真正让贴图发花的元凶**）

修好 albedo 后**脸/身仍满是噪点斑**。排查链：

- 给网格贴**纯白材质** → 头部干净光滑 ⇒ **几何、法线都没问题**，斑点来自贴图采样。
- 把 **UV 当颜色画出来**（U→红、V→绿）→ 头和身体的 `uv_0` 都是**一片噪点**，不是平滑
  渐变 ⇒ UV 本身是逐顶点乱码。
- 原因：PreVS 导入把多套 texcoord **全 dump 成 `uv_0..uv_7`**，其中**只有一层是真正
  的 diffuse UV**，其余是错误解包的噪声。而 Image Texture 节点（无 UV Map 节点）采样
  的是 **render-active** 那层，默认恰好是噪声层 `uv_0`。
  （注意：`uv_layers.active` 是"编辑活动层"，`active_render` 才是"渲染活动层"——只改
  前者不影响贴图采样，这也是早期"试了 8 层结果全一样"的原因。）

**解法（`_coherent_uv()`）**：正确的 UV 层**空间连续**（相邻顶点的 UV 也相邻），错的
是乱码（UV 跳变大）。对每层算**网格边的平均 UV 跳变**，取最小的那层设为
`active_render`：

```
正确层 ≈ 0.01   噪声层 ≈ 0.5     （差约 50 倍，赢家非常明确）
```

逐网格不同——本例**头部 = uv_5，身体/护甲 = uv_7**——但判据自动选对，无需硬编码。

---

## 4. 修复前后

| 部位 | 修复前 | 修复后（与参考图一致） |
|---|---|---|
| 头发 | 绿色（贴成流向遮罩 `30D170`） | 银白（albedo `A9E770` + alpha） |
| 脸 / 身体 | 满脸噪点斑（UV 用了噪声层 uv_0） | 干净肤色（脸 uv_5、身 uv_7） |
| 护甲 | 发白 / 缺失 | 深色金边图集（`4D2A30` + alpha 镂空透肤） |
| mesh_3/4/5 | 完全没材质（灰白） | 各自正确 albedo |

---

## 5. 换模型如何适配

整条流水线是**算法化**的，不含本角色专属常量，理论上换 NR2 抓帧可直接复用：

- **几何/身体**：`reconstruct()` 纯数学，完全通用。
- **材质**：`fix_materials` 用「顶点数聚类 + 内容分类 + UV 连续性」推导，无硬编码贴图
  名 / 网格名 / UV 层号。

**换模型的标准操作**：把 `FRAME_DIR` 指向新的 `frame_N` 目录，跑：

```python
exec(open(r'E:\code\othercode\ninja_ripper\import_frame.py').read())
```

**可调开关**（exec 前设同名全局变量即可覆盖默认值）：

| 变量 | 默认 | 作用 / 何时调 |
|---|---|---|
| `FRAME_DIR` | 本帧路径 | **换模型必改**，指向新 frame 目录 |
| `MODE` | `"prevs"` | `prevs`=精确本地几何（推荐）；`world`=插件反投影 |
| `RECONSTRUCT` | `True` | 关掉则只导入不拼装 |
| `RECON_REF` | `None` | 指定参考网格名；默认取顶点最多的 |
| `RECON_KEEP_FACTOR` | `2.5` | 去重半径系数；有合法部件被误删就调大 |
| `FIX_MATERIALS` | `True` | 关掉则保留插件 AUTO 材质 |
| `STAND_UPRIGHT` | `True` | 立正（最长轴→+Z） |
| `UPRIGHT_FLIP` | `False` | 模型上下颠倒时设 `True` |

### 需要注意的（诚实说明）
- 内容分类的阈值（"自然肤色"、法线 `1,.5,.5` 图案、银发"灰+alpha"特例）是**对照本
  角色（写实人形）调出来的合理默认**。换**美术风格差异很大**的模型（机甲、鲜艳卡通、
  异色生物）时，albedo 判定可能需要微调阈值。
- `STAND_UPRIGHT` 的"最长轴"启发式对个别姿势可能判错，用 `UPRIGHT_FLIP` /
  手动旋转兜底。
- 强烈建议**换模型后用 `frame_N/!screenshot.dds` 做一次目检校准**。

---

## 6. 已知限制（非阻塞）
当前只重建到**正确的 base color（颜色层）**。要更接近游戏精度还可继续接：
- **法线贴图**（各部件 7 图 pass 里的法线张）；
- **卡通 ramp**（共享 LUT，如 `FAED70`）；
- **环境反射**（`6BA070` 等，Blender 解不了该 DDS，需外部转 BC7）。

`!screenshot.dds` 是 **无压缩 DDS**，Blender 解不了，可按位掩码手动解码（见会话脚本）。

---

## 7. 验证
按文档 `exec(...import_frame.py)` 全新重导入：无 crash，`reconstruct` 报
`kept 8 same-pass + 3 recovered cross-pass`（共 11 网格，眼球/睫毛归位），
`fix_materials` 自动为 10/11 网格配出正确 `albedo + UV 层`（日志含 `uv=uv_5/uv_7`），
渲染与参考图 `inase.png` 一致：银发、有眼睛、白皙皮肤、黑金护甲。
（`mesh_29` 这枚极小薄片暂无 albedo 候选，影响可忽略。）
