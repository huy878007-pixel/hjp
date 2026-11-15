#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""校验「立绘大合集」批次的 HJP 包：解码 → 与源图逐像素/角度比对。

  python verify_collection.py --limit 3
  python verify_collection.py --limit 3 --lossless
"""
import os, sys, json, argparse
import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hjp import L_COLOR, L_NORMAL, L_DEPTH, L_MASK, L_ALPHA
from hjpbundle import merge_all
from verify_gate import Stat, AngleHist, normal_from, angle_deg, snap

Image.MAX_IMAGE_PIXELS = 200_000_000
SRC = r"D:\Probably Stolen\02_美术素材\立绘大合集_按角色_2026-11-15"
OUT = r"D:\Probably Stolen\成品素材"
MATERIALS = ["皮肤", "毛发", "服装", "金属饰品", "布料饰品", "眼白", "虹膜"]


def main():
    global SRC
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=3)
    ap.add_argument('--lossless', action='store_true')
    ap.add_argument('--src', default=SRC)
    ap.add_argument('--out', default=None)
    ap.add_argument('--threshold-max', type=int, default=None,
                    help='无损档默认要求 0；有损档不设硬门槛（仅报告）')
    a = ap.parse_args()
    SRC = a.src
    out = a.out or (OUT if not a.lossless else OUT.replace('成品素材', '成品素材_无损'))
    prof = 'lossless' if a.lossless else 'compact'

    dirs = sorted(d for d in os.listdir(SRC) if os.path.isdir(os.path.join(SRC, d)) and not d.startswith('_'))
    packs = {}
    for f in sorted(os.listdir(out)):
        if f.endswith('.hjp'):
            p = os.path.join(out, f)
            packs.setdefault(f.split('.')[0], []).append(p)

    checked, fails = 0, []
    for d in dirs[:a.limit]:
        cdir = os.path.join(SRC, d)
        idx = json.load(open(os.path.join(cdir, '角色索引与映射.json'), encoding='utf-8'))
        vid = idx['id']
        paths = packs.get(vid)
        if not paths:
            fails.append(f"{vid}: 输出里没有包")
            continue
        res = merge_all(paths)
        b = res.get(vid)
        if b is None:
            fails.append(f"{vid}: 合并后无逻辑对象")
            continue

        st = {k: Stat() for k in ('color', 'alpha', 'normal', 'depth', 'mask')}
        ag = AngleHist()
        sz_ok = True
        for f in idx['files']:
            stem = os.path.splitext(f['file'])[0]
            name = f.get('state') or '基础原图'
            eid = next(e['id'] for e in b.expressions if e['name'] == name)
            src = np.array(Image.open(os.path.join(cdir, f['file'])).convert('RGBA'))
            color = b.image(eid, L_COLOR)[0]
            if color.ndim == 2:
                color = np.dstack([color] * 3)
            if color.shape[:2] != src.shape[:2]:
                sz_ok = False
                fails.append(f"{vid}/{name}: 颜色尺寸 {color.shape[:2]} != 源 {src.shape[:2]}")
                continue
            alpha = b.silhouette(eid)
            if alpha.shape != src.shape[:2]:
                sz_ok = False
                fails.append(f"{vid}/{name}: alpha 尺寸不符")
                continue
            # 颜色误差只统计可见像素（alpha>128）。
            # alpha<=8 的区域常见源图垃圾 RGB（纯绿/纯红），不构成画面内容。
            vis = src[..., 3] > 128
            st['color'].add(color[vis], src[vis][:, :3])
            ghost = src[..., 3] <= 8
            if ghost.any():
                gd = np.abs(color[ghost].astype(np.int16) - src[ghost][:, :3].astype(np.int16))
                st.setdefault('ghost', Stat())
                st['ghost'].n += int(ghost.sum())
                st['ghost'].mx = max(st['ghost'].mx, int(gd.max()) if gd.size else 0)
                st['ghost'].gt32 += int((gd.max(-1) > 32).sum()) if gd.size else 0
            st['alpha'].add(alpha, src[..., 3] if prof == 'lossless' else snap(src[..., 3]))
            rn = normal_from(b, eid)
            org_n = np.array(Image.open(os.path.join(cdir, '法线贴图', f['file'])).convert('RGB'))
            st['normal'].add(rn, org_n)
            ag.add(angle_deg(rn, org_n))
            st['depth'].add(b.gray(eid, L_DEPTH), np.array(
                Image.open(os.path.join(cdir, '深度贴图', f['file'])).convert('L')))
            md = os.path.join(cdir, '材质切分', stem, '材质蒙版')
            m0, _ = b.image(eid, L_MASK, 0)
            m1, _ = b.image(eid, L_MASK, 1)
            m2, _ = b.image(eid, L_MASK, 2)
            def one(a):
                return a if a.ndim == 2 else a[..., 0]
            chans = [m0[..., 0], m0[..., 1], m0[..., 2], m0[..., 3],
                     m1[..., 0], m1[..., 1], one(m2)]
            for k, mn in enumerate(MATERIALS):
                org_m = np.array(Image.open(os.path.join(md, mn + '.png')).convert('L'))
                if chans[k].shape != org_m.shape:
                    fails.append(f"{vid}/{name}/{mn}: 蒙版尺寸 {chans[k].shape} != {org_m.shape}")
                    sz_ok = False
                    continue
                st['mask'].add(chans[k], org_m)
        checked += 1
        g = st.get('ghost')
        gtxt = f" ghost_px={g.n} ghost_max={g.mx}" if g and g.n else ""
        line = (f"  {vid:14s} expr={len(idx['files'])} "
                f"color_mean={st['color'].mean:.3f} max={st['color'].mx} "
                f"ang={ag.mean:.2f}° p99.9={ag.p999():.1f}° "
                f"mask_max={st['mask'].mx} depth_max={st['depth'].mx}{gtxt} "
                f"size={'OK' if sz_ok else 'MISMATCH'}")
        print(line)
        if a.lossless:
            for k, s in st.items():
                if k == 'ghost':
                    continue
                if s.n and s.mx != 0:
                    fails.append(f"{vid}: lossless {k} max={s.mx}")
            if ag.mx > 0.5:
                fails.append(f"{vid}: lossless normal 角度 max={ag.mx:.2f}°")
        if st['color'].mean > 2.5:
            fails.append(f"{vid}: 可见区颜色 mean={st['color'].mean:.3f} > 2.5")
        ppm = st['color'].ppm(st['color'].gt32)
        if ppm > 100:
            fails.append(f"{vid}: 可见区颜色 >32 占比 {ppm:.1f}ppm > 100ppm")
        if ag.mean > 1.0:
            fails.append(f"{vid}: 法线角度 mean={ag.mean:.3f}° > 1.0°")
        if ag.p999() > 8.0:
            fails.append(f"{vid}: 法线角度 p99.9={ag.p999():.2f}° > 8.0°")

    print(f"\n检查 {checked} 个角色，失败 {len(fails)} 项")
    for x in fails[:10]:
        print("  -", x)
    sys.exit(0 if not fails else 1)


if __name__ == '__main__':
    main()
