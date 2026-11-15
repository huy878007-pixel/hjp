#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HJP 验收门禁 —— 有失败即非零退出。

用法：
  python verify_gate.py                     # 全库，自动识别档位
  python verify_gate.py admin --lossless
  python verify_gate.py admin --json out.json
  python verify_gate.py admin --mutate black   # 反例自测：必须失败

阈值：verify_thresholds.json（先定义后验收，不得看到结果再改）
"""
import os, sys, json, argparse, hashlib
import numpy as np
from PIL import Image
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hjp import Hjp, L_COLOR, L_NORMAL, L_DEPTH, L_MASK, L_ALPHA, PARTS

Image.MAX_IMAGE_PIXELS = 200_000_000
ROOT = r"C:\Users\Administrator\Desktop\成品"
BASEDIR = os.path.dirname(os.path.abspath(__file__))
BUILDS = {'compact': os.path.join(BASEDIR, 'build'),
          'lossless': os.path.join(BASEDIR, 'build_lossless')}

DEFAULT_THRESHOLDS = {
    "lossless": {"all_max": 0},
    "compact": {
        "color_mean": 2.0, "color_max": 48, "color_gt32_ppm": 100,
        "normal_angle_mean": 1.0, "normal_angle_p999": 8.0, "normal_angle_max": 60.0,
        "depth_mean": 1.0, "depth_max": 24,
        "mask_max": 0, "alpha_max": 0,
        "partition_mean": 1.5, "partition_gt32_ppm": 100,
    },
    "common": {"require_samples": 1, "require_pari_match": True},
}


def load_thresholds():
    p = os.path.join(BASEDIR, 'verify_thresholds.json')
    if not os.path.exists(p):
        json.dump(DEFAULT_THRESHOLDS, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print(f"[init] 阈值文件已生成：{p}")
        return DEFAULT_THRESHOLDS
    return json.load(open(p, encoding='utf-8'))


def snap(a):
    return np.where(a >= 250, 255, np.where(a <= 5, 0, a)).astype(np.uint8)


class Stat:
    """流式累计，不保留数组"""
    __slots__ = ('n', 's', 'mx', 'gt8', 'gt32')

    def __init__(self):
        self.n = 0; self.s = 0; self.mx = 0; self.gt8 = 0; self.gt32 = 0

    def add(self, a, b):
        d = np.abs(a.astype(np.int16) - b.astype(np.int16))
        self.n += d.size
        self.s += int(d.sum())
        m = int(d.max()) if d.size else 0
        if m > self.mx:
            self.mx = m
        self.gt8 += int((d > 8).sum())
        self.gt32 += int((d > 32).sum())

    @property
    def mean(self):
        return self.s / self.n if self.n else 0.0

    def ppm(self, cnt):
        return cnt * 1_000_000 / self.n if self.n else 0.0


class AngleHist:
    __slots__ = ('n', 's', 'mx', 'hist')

    def __init__(self):
        self.n = 0; self.s = 0.0; self.mx = 0.0
        self.hist = np.zeros(1801, np.int64)   # 0.1° 分箱

    def add(self, deg):
        self.n += deg.size
        self.s += float(deg.sum())
        m = float(deg.max()) if deg.size else 0.0
        if m > self.mx:
            self.mx = m
        self.hist += np.bincount(np.minimum((deg * 10).astype(np.int32), 1800).ravel(),
                                 minlength=1801)

    @property
    def mean(self):
        return self.s / self.n if self.n else 0.0

    def p999(self):
        if not self.n:
            return 0.0
        target = self.n * 0.999
        return float(np.searchsorted(np.cumsum(self.hist), target)) / 10.0


def normal_from(mat, i):
    if mat.has(i, L_NORMAL, 1):
        nx = mat.gray(i, L_NORMAL, 0).astype(np.float32) / 127.5 - 1
        ny = mat.gray(i, L_NORMAL, 1).astype(np.float32) / 127.5 - 1
        nz = np.sqrt(np.maximum(0, 1 - nx * nx - ny * ny))
        return np.clip(np.dstack([nx, ny, nz]) * 127.5 + 127.5, 0, 255).astype(np.uint8)
    a = mat.image(i, L_NORMAL, 0)[0]
    return a[..., :3] if a.ndim == 3 else np.dstack([a] * 3)


def angle_deg(rec, org):
    o = org.astype(np.float32) / 127.5 - 1
    r = rec.astype(np.float32) / 127.5 - 1
    o = o / np.maximum(np.sqrt((o * o).sum(-1))[..., None], 1e-6)
    r = r / np.maximum(np.sqrt((r * r).sum(-1))[..., None], 1e-6)
    return np.degrees(np.arccos(np.clip((o * r).sum(-1), -1, 1)))


def verify(folders, profile_flag, mutate, thr):
    st = {k: Stat() for k in ('color', 'alpha', 'alpha_raw', 'normal', 'depth', 'mask', 'part')}
    ag = AngleHist()
    failures, per_char, samples = [], [], 0

    for folder in folders:
        cp = os.path.join(ROOT, folder)
        rec = json.load(open(os.path.join(cp, '记录.json'), encoding='utf-8'))
        vid = rec['sprite']
        prof = 'lossless' if profile_flag else 'compact'
        try:
            base = Hjp(os.path.join(BUILDS[prof], vid + '.base.hjp'))
            mat = Hjp(os.path.join(BUILDS[prof], vid + '.material.hjp'))
        except FileNotFoundError:
            failures.append(f"{vid}: 缺少 {prof} 档包文件")
            continue
        if base.meta.get('profile') not in (None, prof):
            failures.append(f"{vid}: profile 声明为 {base.meta.get('profile')}，期望 {prof}")
        if thr['common']['require_pari_match'] and base.pari != mat.pari:
            failures.append(f"{vid}: pari 表不一致，上下游无法配对")

        V = os.path.join(cp, '渲染贴图_V5')
        ch = {'expr': 0, 'max_color': 0, 'max_angle': 0.0}
        for e in base.meta['expressions']:
            i, name = e['id'], e['name']
            src = os.path.join(cp, name + '.png')
            org = np.array(Image.open(src).convert('RGBA'))
            color, _ = base.image(i, L_COLOR)
            if color.ndim == 2:
                color = np.dstack([color] * 3)
            if mutate == 'black':
                color = np.zeros_like(color)
            alpha = base.silhouette(i)
            tgt = org[..., 3] if prof == 'lossless' else snap(org[..., 3])

            st['color'].add(color[..., :3], org[..., :3])
            st['alpha'].add(alpha, tgt)
            if prof != 'lossless':
                st['alpha_raw'].add(alpha, org[..., 3])

            m0, _ = mat.image(i, L_MASK, 0)
            m1, _ = mat.image(i, L_MASK, 1)
            masks = [m0[..., 0], m0[..., 1], m0[..., 2], m0[..., 3], m1[..., 0], m1[..., 1]]
            for k, p in enumerate(PARTS):
                org_m = np.array(Image.open(os.path.join(V, name, '材质蒙版', p + '.png')).convert('L'))
                st['mask'].add(masks[k], org_m)
                org_p = np.array(Image.open(os.path.join(V, name, '透明分区', p + '.png')).convert('RGBA'))
                rec_p = np.dstack([color[..., :3], np.minimum(alpha, masks[k])])
                st['part'].add(rec_p, org_p)

            org_n = np.array(Image.open(os.path.join(V, name, '法线图.png')).convert('RGB'))
            rn = normal_from(mat, i)
            st['normal'].add(rn, org_n)
            d = angle_deg(rn, org_n)
            ag.add(d)
            ch['max_angle'] = max(ch['max_angle'], float(d.max()))

            org_d = np.array(Image.open(os.path.join(V, name, '深度图.png')).convert('L'))
            st['depth'].add(mat.gray(i, L_DEPTH), org_d)

            cd = np.abs(color[..., :3].astype(np.int16) - org[..., :3].astype(np.int16))
            ch['max_color'] = max(ch['max_color'], int(cd.max()))
            ch['expr'] += 1
            samples += 1
        per_char.append({'sprite': vid, **ch})

    return st, ag, failures, per_char, samples


def gate(st, ag, thr, prof):
    f = []
    t = thr['lossless'] if prof == 'lossless' else thr['compact']
    if prof == 'lossless':
        for k, s in st.items():
            if s.n and s.mx > t['all_max']:
                f.append(f"lossless {k}: max={s.mx} 应为 0")
        return f
    if st['color'].mean > t['color_mean']:
        f.append(f"color mean={st['color'].mean:.3f} > {t['color_mean']}")
    if st['color'].mx > t['color_max']:
        f.append(f"color max={st['color'].mx} > {t['color_max']}")
    if st['color'].ppm(st['color'].gt32) > t['color_gt32_ppm']:
        f.append(f"color >32 占比={st['color'].ppm(st['color'].gt32):.2f}ppm > {t['color_gt32_ppm']}ppm")
    if ag.mean > t['normal_angle_mean']:
        f.append(f"normal 角度 mean={ag.mean:.3f}° > {t['normal_angle_mean']}")
    if ag.p999() > t['normal_angle_p999']:
        f.append(f"normal 角度 p99.9={ag.p999():.2f}° > {t['normal_angle_p999']}")
    if ag.mx > t['normal_angle_max']:
        f.append(f"normal 角度 max={ag.mx:.2f}° > {t['normal_angle_max']}")
    if st['depth'].mean > t['depth_mean'] or st['depth'].mx > t['depth_max']:
        f.append(f"depth mean={st['depth'].mean:.3f} max={st['depth'].mx}")
    if st['mask'].mx > t['mask_max']:
        f.append(f"mask max={st['mask'].mx} 应精确")
    if st['alpha'].mx > t['alpha_max']:
        f.append(f"alpha max={st['alpha'].mx} 应精确（相对吸附目标）")
    if st['part'].mean > t['partition_mean']:
        f.append(f"partition mean={st['part'].mean:.3f} > {t['partition_mean']}")
    if st['part'].ppm(st['part'].gt32) > t['partition_gt32_ppm']:
        f.append(f"partition >32 占比={st['part'].ppm(st['part'].gt32):.2f}ppm > {t['partition_gt32_ppm']}ppm")
    return f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('prefix', nargs='?', default=None)
    ap.add_argument('--lossless', action='store_true')
    ap.add_argument('--json', default=None)
    ap.add_argument('--mutate', default=None, choices=[None, 'black'])
    ap.add_argument('--build', default=None, help='覆盖包目录（默认 build / build_lossless）')
    ap.add_argument('--root', default=None, help='源素材根目录（默认 Desktop\\成品）')
    a = ap.parse_args()

    global ROOT
    if a.root:
        ROOT = a.root
    if not os.path.isdir(ROOT):
        print(f"SKIP: 源素材目录不存在：{ROOT}（无法做像素比对）")
        sys.exit(3)

    if a.build:
        BUILDS['compact'] = BUILDS['lossless'] = a.build
    thr = load_thresholds()
    prof = 'lossless' if a.lossless else 'compact'
    allf = [d for d in sorted(os.listdir(ROOT)) if os.path.isdir(os.path.join(ROOT, d))]
    folders = [d for d in allf if a.prefix is None or d.startswith(a.prefix)]
    if not folders:
        print(f"FAIL: 前缀 '{a.prefix}' 没有匹配任何角色（0 样本）")
        sys.exit(2)

    st, ag, failures, per_char, samples = verify(folders, a.lossless, a.mutate, thr)
    if samples < thr['common']['require_samples']:
        failures.append(f"样本数 {samples} 少于要求 {thr['common']['require_samples']}")
    failures += gate(st, ag, thr, prof)

    print(f"=== profile={prof}  角色={len(folders)}  表情={samples}"
          f"{'  [MUTATE ' + a.mutate + ']' if a.mutate else ''}")
    for k in ('color', 'alpha', 'alpha_raw', 'normal', 'depth', 'mask', 'part'):
        if k == 'normal':
            print(f"    {'normal':10s} mean angle={ag.mean:6.3f}deg  p99.9={ag.p999():5.2f}deg  max={ag.mx:5.2f}deg  n={ag.n}")
            continue
        s = st[k]
        if s.n == 0:
            continue
        print(f"    {k:10s} mean={s.mean:7.4f}  max={s.mx:4d}  "
              f">8={s.gt8}({s.ppm(s.gt8):.1f}ppm)  >32={s.gt32}({s.ppm(s.gt32):.2f}ppm)")

    result = {'ok': not failures, 'profile': prof, 'characters': len(folders),
              'expressions': samples, 'failures': failures, 'per_character': per_char,
              'mutate': a.mutate}
    if a.json:
        json.dump(result, open(a.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print(f"    JSON → {a.json}")

    if failures:
        print(f"\nFAIL ({len(failures)} 项)：")
        for x in failures[:20]:
            print("  -", x)
        sys.exit(1)
    print("\nPASS")
    sys.exit(0)


if __name__ == '__main__':
    main()
