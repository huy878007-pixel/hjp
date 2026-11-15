# HJP — 立绘分层容器格式

一个角色一个文件。里面装这个角色的全部表情，每个表情含颜色、透明度、法线、深度、材质蒙版。

**透明分区不存**，由 `颜色 × 轮廓 × 蒙版` 在渲染时现算——这一项就是 70% 的体积。

## 效果

| | 原始散图 | HJP |
|---|---:|---:|
| 20 角色 / 8 表情 | 1688 MB，9041 文件 | **56.34 MiB，40 文件**（30×） |
| 150 表情（早期批次） | 1593.5 MB，2250 文件 | **59.02 MiB，40 文件**（27×） |
| 同一批，全无损档 | 1593.5 MB | **268.94 MiB**（逐像素零误差） |

体积收益来自三件事，按贡献排序：

1. **透明分区是派生物**——`分区.RGB = 颜色.RGB`，`分区.A = min(颜色.A, 蒙版)`，实测 900 张逐像素成立，所以一个字节都不存。
2. **冗余通道**——法线/深度重复的 alpha、深度 RGB 三通道相同、蒙版存成 RGB，全部去掉。
3. **有损编码**——WebP；法线拆成 x/y 两块灰度（整张 RGB 走 YUV 4:2:0 会让角误差 p99.9 从 4.8° 涨到 33.9°）。

## 仓库内容

| 文件 | 作用 |
|---|---|
| `hjp.py` | 容器读写、严格校验、打包（旧目录布局） |
| `hjpbundle.py` | 上游/下游合并、配对、冲突裁决 |
| `pack_collection.py` | 打包「立绘大合集_按角色」布局 |
| `verify_collection.py` | 解码并与源图逐像素比对 |
| `verify_gate.py` | 像素门禁（带退出码与阈值） |
| `probe_reject.py` | 12 类坏包反例，必须全部被拒 |
| `test_bundle.py` | 15 条合并规则 |
| `make_conformance.py` | 生成跨语言一致性样本 |
| `run_checks.py` | 一键跑完上述检查 |
| `conformance/` | `golden.hjp`（全 raw 编码，零依赖可读）+ 期望索引 |
| `HJP_使用手册.md` | 上手、层/槽语义、合并、引擎接入、FAQ |
| `HJP_v1_完整规格.md` | 字节级规格 |

## 快速开始

```bash
# 打包（源目录含 <角色>/角色索引与映射.json、法线贴图/、深度贴图/、材质切分/）
python pack_collection.py --src <素材目录> --out <输出目录> --limit 20

# 校验：解码每个块并与源图逐像素比对，退出码 0 才算通过
python verify_collection.py --src <素材目录> --out <输出目录> --limit 20

# 格式、合并、一致性、坏包反例一起跑
python run_checks.py
```

读一个包：

```python
from hjpbundle import merge_all
from hjp import L_COLOR, L_NORMAL, L_DEPTH, L_MASK

b = merge_all(["xxx.base.hjp", "xxx.material.hjp"])["xxx"]
color = b.image(0, L_COLOR)[0]          # (H, W, 3) uint8, sRGB
alpha = b.silhouette(0)                  # (H, W)
normal = b.image(0, L_NORMAL, 0)[0]      # 有损档：slot0/1 是 x/y 两块灰度
depth = b.gray(0, L_DEPTH)               # (H, W)
skin = b.image(0, L_MASK, 0)[0][..., 0]  # 第 0 类材质
```

依赖：Python 3 + Pillow + numpy。无其它依赖。

## 格式要点

- 容器与引擎无关：任何能读字节、能解 PNG/WebP 的环境都能实现。C 级读者（只解析容器）是合法实现。
- 上下游：`base` 可单独加载，`material` 声明 `requires.stage = 0`，缺上游则整包不加载。
- 尺寸：每个块可自带尺寸（`dimn` 扩展块），蒙版与颜色分辨率不同也能精确对齐。
- 失败一律回退到原版显示，不白屏。

## 许可

代码与文档采用 **GNU AGPL-3.0**，见 [LICENSE](LICENSE)。

本仓库不包含任何游戏美术素材。素材版权归原权利方所有。
