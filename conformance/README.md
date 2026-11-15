# HJP 一致性测试样本

这个目录用于**跨实现、跨语言**验证对 HJP 容器的最小理解。

## 文件

| 文件 | 说明 |
|---|---|
| `golden.hjp` | 2528 字节。画布 8×8，2 个表情，14 个块，**全部 `codec=raw`**（裸像素） |
| `golden.index.json` | 该文件规范化导建的期望值 |

## 为什么用 raw

区块全部是未压缩裸像素，所以这个样本**不需要 PNG/WebP/BC7 任何解码器**。它只考验：

- 固定头解析
- 块表解析与校验
- 表情表与元数据一致性
- 扩展块（`pari`）解析
- 数据区偏移与尺寸推导
- 规范化导建输出

图像编解码能力请用真实素材另行验证，不要把两件事混在一起。

## 合格判据

任何语言的实现读取 `golden.hjp`，产出的导建 JSON 必须与 `golden.index.json` 一致：

- `magic` / `version` / `canvas` / `character` / `stage` / `profile`
- `blocks` 数组：数量、顺序无关但**集合**必须一致，每项 `expr`/`layer`/`slot`/`codec`/`channels`/`scale_shift`/`length`/`sha256`
- `chunks` 集合
- `pari` 映射

`size` 与 `header_size` 用于校验文件完整性，也必须一致。

## 参考实现

Python：`../hjp.py`

```
python hjp.py dump conformance/golden.hjp > my.index.json
```

## 版本

与 `HJP_v1_完整规格.md` 的 v1 草案对应。格式主版本变化时，本样本必须同步更新并保留旧样本。
