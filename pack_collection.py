#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按「立绘大合集_按角色」布局打包成 HJP。

目录布局：
  <角色目录>/
    角色索引与映射.json
    <文件>.png                     颜色
    法线贴图/<文件>.png
    深度贴图/<文件>.png
    材质切分/<文件去扩展名>/材质蒙版/<材质>.png
    材质切分/<文件去扩展名>/透明分区/<材质>.png

关键事实（2026-11-15 实测）：
  * 材质 7 类：皮肤 / 毛发 / 服装 / 金属饰品 / 布料饰品 / 眼白 / 虹膜
  * **同一角色内，基础原图与差分的画布尺寸不同**（如 1024x1536 vs 832x1248）
  * 蒙版尺寸与颜色也不同（832x1248）

策略：不强行统一。每个块写入自己的真实尺寸（dimn 扩展块），
      元数据 canvas 取最大者，expressions[] 里记录每个表情的实际尺寸。
      渲染端按 UV 采样，尺寸差异由显示几何消化。
"""
import os, sys, json, shutil
import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hjp as _hjp
from hjp import (write_hjp, allocate_ids, source_id, HjpBuildError,
                 C_PNG, C_WEBP, L_COLOR, L_NORMAL, L_DEPTH, L_MASK, L_ALPHA,
                 _save, enc_rgb, enc_la)

SRC = r"D:\Probably Stolen\02_美术素材\立绘大合集_按角色_2026-11-15"
OUT = r"D:\Probably Stolen\成品素材"
MATERIALS = ["皮肤", "毛发", "服装", "金属饰品", "布料饰品", "眼白", "虹膜"]


def load_rgba(p):
    return np.array(Image.open(p).convert('RGBA'))


def load_l(p):
    return np.array(Image.open(p).convert('L'))


def read_index(cdir):
    idx = json.load(open(os.path.join(cdir, '角色索引与映射.json'), encoding='utf-8'))
    exprs = []
    for f in idx['files']:
        stem = os.path.splitext(f['file'])[0]
        name = f.get('state') or ('基础原图' if f.get('kind') == '基础原图' else stem)
        exprs.append(dict(file=f['file'], stem=stem, name=name, kind=f.get('kind', '')))
    return idx, exprs


def plan(cdir, exprs):
    """逐表情确认尺寸，不做任何缩放。返回每个表情的 (颜色尺寸, 蒙版尺寸)。"""
    plan_out, warnings = [], []
    for e in exprs:
        cp = os.path.join(cdir, e['file'])
        np_ = os.path.join(cdir, '法线贴图', e['file'])
        dp = os.path.join(cdir, '深度贴图', e['file'])
        md = os.path.join(cdir, '材质切分', e['stem'], '材质蒙版')
        for p in (cp, np_, dp):
            if not os.path.exists(p):
                raise HjpBuildError(f"{e['stem']}: 缺少 {os.path.basename(p)}")
        if not os.path.isdir(md):
            raise HjpBuildError(f"{e['stem']}: 缺少材质蒙版目录")
        missing = [m for m in MATERIALS if not os.path.exists(os.path.join(md, m + '.png'))]
        if missing:
            raise HjpBuildError(f"{e['stem']}: 缺少材质 {missing}")

        sizes = []
        for p in (cp, np_, dp):
            im = Image.open(p)
            sizes.append(im.size)
        if len(set(sizes)) != 1:
            raise HjpBuildError(f"{e['stem']}: 颜色/法线/深度尺寸不一致 {sizes}")
        msize = Image.open(os.path.join(md, MATERIALS[0] + '.png')).size
        plan_out.append(dict(expr=e, size=sizes[0], mask=msize))
        if sizes[0] != msize:
            warnings.append(f"{e['stem']}: 颜色 {sizes[0][0]}x{sizes[0][1]} vs 蒙版 {msize[0]}x{msize[1]}")
    sizes = {p['size'] for p in plan_out}
    if len(sizes) > 1:
        warnings.append(f"同一角色内画布不一致：{sorted(sizes)}")
    return plan_out, warnings


def build_character(cdir, out_dir, lossless=False, q=90, ids=None, verbose=True):
    idx, exprs = read_index(cdir)
    vid = idx['id']
    plan_out, warnings = plan(cdir, exprs)
    canvas = (max(p['size'][0] for p in plan_out), max(p['size'][1] for p in plan_out))

    idmap = ids if ids is not None else dict(allocate_ids(vid, [e['name'] for e in exprs]))
    blocks_b, blocks_m, pari, metas = [], [], [], []
    for p in plan_out:
        e = p['expr']; eid = idmap[e['name']]
        W, H = p['size']; MW, MH = p['mask']
        base = load_rgba(os.path.join(cdir, e['file']))
        rgb = np.ascontiguousarray(base[..., :3])
        a = base[..., 3]
        nrm = load_rgba(os.path.join(cdir, '法线贴图', e['file']))
        dep = load_rgba(os.path.join(cdir, '深度贴图', e['file']))
        md = os.path.join(cdir, '材质切分', e['stem'], '材质蒙版')
        ms = [load_l(os.path.join(md, m + '.png')) for m in MATERIALS]

        if not (np.array_equal(dep[..., 0], dep[..., 1]) and np.array_equal(dep[..., 0], dep[..., 2])):
            raise HjpBuildError(f"{e['stem']}: 深度 RGB 不相等")
        if not np.array_equal(nrm[..., 3], dep[..., 3]):
            raise HjpBuildError(f"{e['stem']}: 法线/深度 alpha 不同")

        metas.append({'id': eid, 'name': e['name'], 'width': W, 'height': H,
                      'kind': e['kind']})
        pari.append((eid, source_id(rgb)))

        blocks_b.append(dict(expr=eid, layer=L_COLOR, slot=0, data=enc_rgb(rgb, q, lossless),
                             codec=C_WEBP, channels=3, shift=0, w=W, h=H))
        a_out = a if lossless else np.where(a >= 250, 255, np.where(a <= 5, 0, a)).astype(np.uint8)
        blocks_b.append(dict(expr=eid, layer=L_ALPHA, slot=0, data=_save(a_out, 'PNG', optimize=True),
                             codec=C_PNG, channels=1, shift=0, w=W, h=H))

        if lossless:
            blocks_b.append(dict(expr=eid, layer=L_ALPHA, slot=1,
                                 data=_save(nrm[..., 3], 'PNG', optimize=True),
                                 codec=C_PNG, channels=1, shift=0, w=W, h=H))
            blocks_m.append(dict(expr=eid, layer=L_NORMAL, slot=0,
                                 data=enc_rgb(np.ascontiguousarray(nrm[..., :3]), q, True),
                                 codec=C_WEBP, channels=3, shift=0, w=W, h=H))
            blocks_m.append(dict(expr=eid, layer=L_DEPTH, slot=0,
                                 data=_save(dep[..., 0], 'PNG', optimize=True),
                                 codec=C_PNG, channels=1, shift=0, w=W, h=H))
        else:
            for slot, ch_i in ((0, 0), (1, 1)):
                blocks_m.append(dict(expr=eid, layer=L_NORMAL, slot=slot,
                                     data=enc_rgb(np.ascontiguousarray(nrm[..., ch_i]), q, False),
                                     codec=C_WEBP, channels=1, shift=0, w=W, h=H))
            blocks_m.append(dict(expr=eid, layer=L_DEPTH, slot=0,
                                 data=enc_rgb(np.ascontiguousarray(dep[..., 0]), q, False),
                                 codec=C_WEBP, channels=1, shift=0, w=W, h=H))

        for slot, (arr, ch) in enumerate([(np.dstack(ms[0:4]), 4),
                                          (np.dstack(ms[4:6]), 2),
                                          (np.dstack(ms[6:7]), 1)]):
            if ch == 4:
                data = _save(arr, 'PNG', optimize=True)
            elif ch == 2:
                data = enc_la(arr[..., 0], arr[..., 1])
            else:
                data = _save(arr[..., 0], 'PNG', optimize=True)
            blocks_m.append(dict(expr=eid, layer=L_MASK, slot=slot, data=data,
                                 codec=C_PNG, channels=ch, shift=0, w=MW, h=MH))

    common = dict(character=vid, name=idx.get('name', vid), origin=idx.get('category', ''),
                  canvas=list(canvas), materials=MATERIALS, expressions=metas,
                  profile='lossless' if lossless else 'compact',
                  sourceWarnings=warnings,
                  producer={'tool': 'pack_collection', 'version': '1.1.0'},
                  conventions={'colorSpace': 'sRGB', 'normalSpace': 'tangent', 'normalY': 'up',
                               'normalEncoding': 'unsigned255', 'depthRange': 'normalized',
                               'depthPolarity': 'nearWhite', 'alphaMode': 'straight',
                               'partitionFormula': 'min(colorA, mask)',
                               'layerSizeRule': 'explicit-dimn'})
    mb = dict(common, stage=0, role='base', priority=100, replace=False, revision=1, requires=None)
    mm = dict(common, stage=1, role='material', priority=100, replace=False, revision=1,
              requires={'stage': 0})

    os.makedirs(out_dir, exist_ok=True)
    pb = os.path.join(out_dir, vid + '.base.hjp')
    pm = os.path.join(out_dir, vid + '.material.hjp')
    sb = write_hjp(pb, mb, blocks_b, pari)
    sm = write_hjp(pm, mm, blocks_m, pari)
    flag = '  [尺寸不一致]' if warnings else ''
    if verbose:
        print(f"  {vid:16s} n={len(exprs)}  canvas_max={canvas[0]}x{canvas[1]}"
              f"  base={sb/1048576:6.2f}MiB  material={sm/1048576:7.2f}MiB{flag}", flush=True)
    return dict(sprite=vid, name=idx.get('name'), n=len(exprs), base=sb, material=sm,
                canvas=list(canvas), warnings=warnings, summary=flag)


def _worker(args):
    cdir, out, lossless, ids, method = args
    _hjp.LOSSLESS_METHOD = method
    try:
        return build_character(cdir, out, lossless, ids=ids, verbose=False), None
    except Exception as e:                      # noqa: BLE001
        return None, f"{os.path.basename(cdir)}: {type(e).__name__}: {e}"


def main():
    argv = sys.argv[1:]
    lossless = '--lossless' in argv
    limit = int(argv[argv.index('--limit') + 1]) if '--limit' in argv else None
    jobs = int(argv[argv.index('--jobs') + 1]) if '--jobs' in argv else 1
    method = int(argv[argv.index('--webp-method') + 1]) if '--webp-method' in argv else _hjp.LOSSLESS_METHOD
    _hjp.LOSSLESS_METHOD = method
    src = argv[argv.index('--src') + 1] if '--src' in argv else SRC
    if '--out' in argv:
        out = argv[argv.index('--out') + 1]
    else:
        out = OUT if not lossless else OUT.replace('成品素材', '成品素材_无损')

    chars = sorted(d for d in os.listdir(src)
                   if os.path.isdir(os.path.join(src, d)) and not d.startswith('_'))
    if limit:
        chars = chars[:limit]
    print(f"源 {src}\n输出 {out}\n角色 {len(chars)} 个，profile={'lossless' if lossless else 'compact'}"
          f"，并行 {jobs}\n", flush=True)
    rows, skips = [], []

    if jobs > 1:
        pre = {}
        for c in chars:
            idx, exprs = read_index(os.path.join(src, c))
            pre[c] = dict(allocate_ids(idx['id'], [e['name'] for e in exprs]))
        from multiprocessing import Pool
        tasks = [(os.path.join(src, c), out, lossless, pre[c], method) for c in chars]
        done = 0
        with Pool(jobs) as pool:
            for row, err in pool.imap_unordered(_worker, tasks):
                done += 1
                if err:
                    skips.append(err)
                    print(f"  SKIP {err}", flush=True)
                else:
                    rows.append(row)
                    print(f"  [{done}/{len(chars)}] {row['sprite']:16s} n={row['n']} "
                          f"base={row['base']/1048576:5.2f} material={row['material']/1048576:6.2f} MiB"
                          f"{row['summary']}", flush=True)
    else:
        for c in chars:
            try:
                row = build_character(os.path.join(src, c), out, lossless=lossless)
                rows.append(row)
            except HjpBuildError as e:
                skips.append(f"{c}: {e}")
                print(f"  SKIP {c}: {e}", flush=True)

    tb = sum(r['base'] for r in rows); tm = sum(r['material'] for r in rows)
    print(f"\nTOTAL base={tb/1048576:.2f}MiB material={tm/1048576:.2f}MiB "
          f"sum={(tb+tm)/1048576:.2f}MiB ({len(rows)} 角色, 跳过 {len(skips)})")
    os.makedirs(out, exist_ok=True)
    json.dump({'profile': 'lossless' if lossless else 'compact', 'source': src,
               'characters': rows, 'skips': skips,
               'total': {'base': tb, 'material': tm}},
              open(os.path.join(out, '_build.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    reg = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'expr_ids.json')
    if os.path.exists(reg):
        shutil.copyfile(reg, os.path.join(out, 'expr_ids.snapshot.json'))
    print(f"清单与 ID 快照已写入 {out}")


if __name__ == '__main__':
    main()
