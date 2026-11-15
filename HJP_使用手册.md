# HJP 使用手册

> 版本：v1（2026-11-15）
> 适用：HJP 1.0 草案 + 参考实现 `hjp.py` / `hjpbundle.py`
> 读者：要打包素材的人、要写读取器的人、要接引擎的人

---

## 0. 一句话

**HJP 是「一个角色 = 一个文件」的立绘容器。** 里面装这个角色的全部表情，每个表情含颜色、透明度、法线、深度、材质蒙版。透明分区不存，由 `颜色 × 轮廓 × 蒙版` 现场算。

---

## 1. 什么时候该用

| 场景 | 适合 |
|---|---|
| 一个角色有多个表情/差分 | ✅ |
| 每个表情还带法线/深度/材质分区 | ✅ |
| 内容要分层发布（本体 / 材质包 / 社区改版） | ✅ |
| 只有几千张互不相关的 UI 图 | ❌ 用普通图集 |
| 需要动画、骨骼、粒子 | ❌ 不是这个格式的事 |

---

## 2. 五分钟上手

### 2.1 打包

```powershell
$py = "C:\Users\Administrator\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"

# 按「立绘大合集」目录布局打包前 20 个角色
& $py pack_collection.py --limit 20

# 全量（202 个）
& $py pack_collection.py

# 无损档
& $py pack_collection.py --lossless
```

输出目录：

| 档位 | 目录 |
|---|---|
| 默认（有损） | `D:\Probably Stolen\成品素材` |
| 无损 | `D:\Probably Stolen\成品素材_无损` |

每个角色产出两个文件：

```
femaleUpper1.base.hjp      颜色 + 轮廓        （上游，可单独用）
femaleUpper1.material.hjp  法线 + 深度 + 蒙版  （下游，依赖上游）
```

### 2.2 读一个包

```python
import sys; sys.path.insert(0, r"D:\Document\bim\portrait-pack")
from hjpbundle import merge_all
from hjp import L_COLOR, L_NORMAL, L_DEPTH, L_MASK

bundles = merge_all([
    r"D:\Probably Stolen\成品素材\femaleUpper1.base.hjp",
    r"D:\Probably Stolen\成品素材\femaleUpper1.material.hjp",
])
b = bundles["femaleUpper1"]

color = b.image(0, L_COLOR)[0]          # (H, W, 3) uint8
alpha = b.silhouette(0)                  # (H, W)    uint8
normal = b.image(0, L_NORMAL, 0)[0]      # 见 §5.2，两种布局
depth  = b.gray(0, L_DEPTH)              # (H, W)
skin   = b.image(0, L_MASK, 0)[0][..., 0]  # 第 0 类材质覆盖率
```

### 2.3 校验

```powershell
& $py verify_collection.py --limit 20      # 新批次，逐像素比对源图
& $py run_checks.py                       # 格式/合并/一致性/门禁 全跑
```

**看退出码，不要只看输出。** 0 才是通过。

---

## 3. 文件命名与目录

推荐布局（`pack_collection.py` 的输出）：

```
成品素材/
├─ <角色id>.base.hjp
├─ <角色id>.material.hjp
├─ _build.json              逐角色体积与尺寸清单
└─ expr_ids.snapshot.json   表情 ID 快照
```

- `<角色id>` 用 ASCII，来自 `角色索引与映射.json` 的 `id`（如 `femaleUpper1`）。
- **不要**把角色 id 改成中文或带空格。
- 同一角色的 `base` 与 `material` 必须**同一批**生成；`pari` 配对表要对得上。

---

## 4. 包里有什么

### 4.1 层（layer）

| layer | 名称 | 通道 | 说明 |
|---|---|---:|---|
| 0 | 颜色 | 3 | RGB，不含透明度 |
| 1 | 法线 | 3 或 1+1 | 见 §5.2 |
| 2 | 深度 | 1 | 逐图归一化，近白远黑 |
| 3 | 蒙版 | 4 / 2 / 1 | 按 `materials` 顺序切槽 |
| 4 | 轮廓 | 1 | 颜色 alpha；slot 1 是法线/深度有效域 |

### 4.2 槽（slot）

| layer | slot | 通道 | 含义 |
|---|---:|---:|---|
| 0 | 0 | 3 | RGB |
| 1 | 0 | 3 | 无损档：RGB 法线 |
| 1 | 0 / 1 | 1 / 1 | 有损档：x / y 两块灰度 |
| 2 | 0 | 1 | 深度 |
| 3 | 0 | 4 | `materials[0..3]` |
| 3 | 1 | 2 | `materials[4..5]` |
| 3 | 2 | 1 | `materials[6]` |
| 4 | 0 | 1 | 颜色 alpha |
| 4 | 1 | 1 | 法线/深度 alpha（仅无损档） |

### 4.3 材质顺序

**必须读元数据 `materials`，不要写死。** 现有两套：

```
7 类（新）: 皮肤 毛发 服装 金属饰品 布料饰品 眼白 虹膜
6 类（旧）: 皮肤 毛发_含眉毛 服装 金属饰品 布料饰品 眼睛_尚未拆分眼白虹膜
```

合并不同批次时，旧列表必须是新列表的**逐项相同前缀**，否则材质通道会错位，读取器必须报错。

---

## 5. 像素语义（最容易错的地方）

### 5.1 透明分区 = `min`，不是相乘

```
分区.RGB = 颜色.RGB
分区.A   = min(颜色.A, 蒙版)
```

**不是** `颜色.A × 蒙版 / 255`。当两者都是 128 时，`min` 得 128，相乘只得 64。写 shader 时不要归一化后再乘。

### 5.2 法线有两种布局，必须按结构判别

| 判定 | 布局 | 读法 |
|---|---|---|
| 有 `layer 1 slot 1` | 两块灰度 | `x=slot0, y=slot1, z=sqrt(1-x²-y²)` |
| 无 `slot 1` | RGB | 直接用三通道 |

不要用「通道数是 3 就一定是 RGB」判断——要看 slot。

### 5.3 颜色空间

- 颜色：**sRGB**
- 法线 / 深度 / 蒙版 / 轮廓：**线性数据**，上传纹理时不要当 sRGB 处理

Unity 里对应 `Texture2D` 的 `linear` 参数；其它引擎对应 sRGB/linear 采样设置。**弄错会让打光整体偏亮或偏暗。**

### 5.4 尺寸不一定相同

同一角色的不同表情、以及蒙版与颜色，**可能有不同尺寸**（源素材如此）。详见 §7。

---

## 6. 上下游与合并

### 6.1 两种包

| 类型 | 判定 | 能否单独用 |
|---|---|---|
| 上游 | 元数据 `requires` 为 null | ✅ |
| 下游 | `requires: {"stage": 0}` | ❌ 必须凑齐上游 |

### 6.2 合并会做什么

```python
bundles = merge_all([base_path, material_path, submod_path])
```

1. 同一文件重复给 → 按内容哈希去重（幂等）
2. `requires` 不满足的包整包丢弃，依赖它的更高层也一起丢
3. 同键冲突：内容相同去重；否则必须 `replace=true` 且优先级**严格更高**
4. 逐表情配对：`pari` 的 `baseId` 不一致 → **只禁用那个表情的下游块**，其余正常
5. 结果与文件传入顺序无关

```python
b.degraded   # 哪些表情因配对失败降级了
b.dropped    # 哪些包因缺依赖被丢弃
b.provenance # 每个块实际来自哪个文件
```

### 6.3 做 submod（社区改版）

```jsonc
{
  "character": "femaleUpper1",
  "stage": 0,
  "priority": 500,        // 必须严格高于被覆盖者
  "replace": true,        // 覆盖授权
  "requires": null
}
```

⚠️ **只替换颜色会让所有下游材质降级**——因为像素变了，`baseId` 变了。要避免降级，submod 必须把法线/深度/蒙版一起提供。

---

## 7. 尺寸：`dimn` 扩展块

块尺寸默认由 `画布 >> scale_shift` 推出。但真实素材常见「颜色 1024×1536、蒙版 832×1248」，比值不是 2 的幂，推不出来。

此时包里会带一个 `dimn` 扩展块，逐块写明 `(expr, layer, slot, width, height)`。

**读取器必须优先用 `dimn` 给出的尺寸**，不能一律按画布推。参考实现：

```python
w, h = bundle.expected_size(expr, layer, slot)   # 已处理 dimn
```

渲染端按 **UV 采样**，尺寸差异由显示几何消化，不需要预缩放。

> 当前批次（2026-11-15）**全部 20 个角色都存在同角色内尺寸不一致**：基础原图 1024×1536 / 1122×1402，差分为 832×1248 / 928×1152；蒙版尺寸跟随各自的表情（基础原图的蒙版与其颜色同尺寸，差分的蒙版是 832×1248）。这是源数据问题，格式只是忠实记录。

---

## 8. 编码器

| id | 名称 | 已实现 | 说明 |
|---:|---|---|---|
| 0 | raw | ✅ | 裸像素，零依赖 |
| 1 | png | ✅ | 无损，Unity 原生 |
| 2 | webp | ✅ | 有损/无损，体积最优 |
| 3 | bc7 | ❌ | PC GPU 格式 |
| 4 | bc4 | ❌ | 单通道 GPU |
| 5 | zlib | ❌ | |
| 6 | lz4 | ❌ | |
| 7 | astc4x4 | ❌ | 移动端 |
| 8 | astc6x6 | ❌ | 移动端 |

**能力协商**：实现必须公开自己支持的 codec 集合；遇到缺失的**非必需** codec 应降级并记录，遇到缺失的**必需** codec（颜色/轮廓）则该表情不可显示，其它表情不受影响。

### 8.1 为什么法线不能整张 RGB 走有损

WebP 有损固定 YUV 4:2:0，会把色度降采样。法线是纯高频色度数据，实测角误差 p99.9 会从 4.8° 爆炸到 **33.9°**，而且**提高质量参数救不回来**。

所以有损档把法线拆成 x、y 两块**灰度**（无色素可抽），z 在 shader 里开方重建。这也是 BC5 的标准做法。

---

## 9. 命令行参考

### 9.1 `hjp.py`

```
python hjp.py build [前缀|all] [--lossless] [--out DIR]
python hjp.py dump  <file.hjp> [--json out.json]
python hjp.py check <file.hjp>
```

- `build`：走旧的「成品」目录布局
- `dump`：输出规范化索引（跨语言一致性比对用）
- `check`：只校验结构，不解码像素

### 9.2 `pack_collection.py`

```
python pack_collection.py [--limit N] [--lossless]
```

走「立绘大合集_按角色」布局，7 类材质。

### 9.3 `verify_collection.py`

```
python verify_collection.py [--limit N] [--lossless]
```

解码每个块并与源图逐像素比对。**退出码 0 才算通过。**

### 9.4 `run_checks.py`

```
python run_checks.py          # 快速
python run_checks.py --full   # 全库
```

串联：坏包探针 → 合并规则 → 一致性样本 → 像素门禁 → 全黑注入反例。

---

## 10. 接引擎（C# / 任意语言）

### 10.1 最小读取流程

```
读文件 → 校验头/表/元数据 → 建块索引
      → 需要某表情时：读该块 → 按 codec 解码 → 建纹理
```

### 10.2 必须实现的校验

- magic、主版本
- `header_size` 对齐与范围
- 块表：重复键、越界、未对齐、未知 codec、通道与 `(layer, slot)` 不匹配
- 头 CRC
- 扩展块：critical 未知必须拒绝，ancillary 未知跳过
- 解码尺寸必须等于 `dimn`（若有）或 `画布 >> scale_shift`

坏包一律抛出明确异常，不要 "尽力而为"。

### 10.3 Unity 特别注意

- `ImageConversion.LoadImage` **只支持 PNG / JPG / EXR，不支持 WebP**。用 WebP 需要插件，或在打包时改 `codec=1`（PNG）。
- 颜色用 sRGB 纹理，法线/深度/蒙版用线性纹理。
- 现有运行时 `PackReader` 会把资源引用整体当物理路径并做 `PngVerifier` 检查，`xxx.hjp#平静.color` 会在到达 `SpriteCache` 前被拒——子资源解析必须加在 `PackReader` 里。
- `SpriteCache` 的缓存键包含几何信息，同一张图不同显示尺寸会重复上传；共享纹理需要按内容身份另建键。

### 10.4 C# 骨架

```csharp
public sealed class Hjp : IDisposable {
    public static Hjp Open(string path);
    public HjpMeta Meta { get; }
    public bool Has(int expr, HjpLayer layer, int slot = 0);
    public (int W, int H) Size(int expr, HjpLayer layer, int slot = 0);
    public byte[] Raw(int expr, HjpLayer layer, int slot = 0);
    public Texture2D CreateTexture(int expr, HjpLayer layer, int slot = 0);
    public Texture2D Silhouette(int expr);
}
```

---

## 11. 常见问题

**Q：为什么包里没有「透明分区」？**
分区是 `颜色 + min(α, 蒙版)` 算出来的，存它是浪费。实测 900 张分区与公式结果逐像素一致。

**Q：为什么两个文件而不是一个？**
因为本体和材质要能分开下载、分开更新。装不上材质包的玩家只下几十 MB。合并后对上层代码就是一个对象。

**Q：只有 material 包会怎样？**
不会显示任何东西，也不会报错——这是设计行为。

**Q：表情改名会怎样？**
`baseId` 基于像素，不看名字，所以照样配对。但 `expr id` 由 `expr_ids.json` 分配，**只增不复用**，删除留墓碑，别手工改它。

**Q：颜色能无损吗？**
能，用 `--lossless`。代价是单张从 ~113 KB 涨到 ~690 KB。

**Q：为什么我的无损校验不过？**
先看 `verify_collection.py` 的失败项。常见原因：蒙版 RGB 不等、深度 RGB 不等、法线/深度 alpha 不同——这些都会让打包器直接拒绝，而不是静默降维。

**Q：透明区那些纯绿/纯红像素是什么？**
源图在近乎透明处留下的垃圾 RGB。现在不可见，但缩放或 mipmap 时会渗出绿边。建议上游预乘 alpha 或向外扩边。

---

## 12. 版本与兼容

| 变更 | 版本动作 | 老读取器 |
|---|---|---|
| 新增 layer / codec / ancillary 扩展块 | 次版本 +1 | 跳过，正常显示已有层 |
| 新增 critical 扩展块 | 主版本 +1 | 拒绝 |
| 改块表布局 / 层语义 | 主版本 +1 | 拒绝 |
| `materials` 变长 | 次版本 +1 | 只认前缀，记警告 |

跨实现一致性样本：`conformance/golden.hjp`（全部 `raw` 编码）+ `golden.index.json`。任何语言读到该文件后，`dump()` 的输出必须与期望值逐字段一致。

---

## 13. 文件清单

| 文件 | 作用 |
|---|---|
| `hjp.py` | 容器读写 + 严格校验 + 打包（旧布局） |
| `hjpbundle.py` | 上下游合并、配对、冲突裁决 |
| `pack_collection.py` | 新布局打包（7 类材质） |
| `verify_collection.py` | 新批次逐像素校验 |
| `verify_gate.py` | 旧批次门禁（带退出码与阈值） |
| `probe_reject.py` | 12 类坏包反例 |
| `test_bundle.py` | 15 条合并规则 |
| `make_conformance.py` | 生成一致性样本 |
| `run_checks.py` | 一键全跑 |
| `conformance/` | 跨语言一致性样本 |
| `HJP_v1_完整规格.md` | 字节级规格 |
| `expr_ids.json` | 表情 ID 身份源（**必须备份**） |

---

## 14. 三条铁律

1. **表情 ID 只增不复用、不重排。** 删掉的 ID 永久作废。
2. **材质顺序只追加不重排。** 顺序即语义。
3. **失败必须回退，不能白屏。** 缺包、坏块、缺编码器，都要退到「显示原版」而不是崩溃。
