#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合并器测试：用例即规格。任一断言失败即非零退出。"""
import os, sys, json, tempfile, itertools
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hjp import write_hjp, C_RAW, L_COLOR, L_NORMAL, L_DEPTH, L_MASK, L_ALPHA
from hjpbundle import merge_all, HjpMergeError

TMP = tempfile.mkdtemp(prefix='hjp-merge-')
FAILS = []

BASE_LAYERS = [(L_COLOR, 0, 3), (L_ALPHA, 0, 1)]
MAT_LAYERS = [(L_NORMAL, 0, 1), (L_NORMAL, 1, 1), (L_DEPTH, 0, 1),
              (L_MASK, 0, 4), (L_MASK, 1, 2)]


def build(name, *, stage=0, pri=100, replace=False, requires=None,
          exprs=(0, 1), layers=None, pari=None, seed=0, canvas=(4, 4)):
    W, H = canvas
    path = os.path.join(TMP, name)
    blocks = []
    for e in exprs:
        for (layer, slot, ch) in layers:
            g = np.empty((H, W, ch), np.uint8)
            for c in range(ch):
                g[..., c] = ((np.arange(W * H).reshape(H, W) * (3 + c) + e * 17 + layer * 5 + seed * 31) % 256)
            blocks.append(dict(expr=e, layer=layer, slot=slot,
                               data=(g[..., 0] if ch == 1 else g).tobytes(),
                               codec=C_RAW, channels=ch, shift=0))
    meta = {'character': 't', 'canvas': [W, H],
            'materials': ['m0', 'm1', 'm2', 'm3', 'm4', 'm5'],
            'expressions': [{'id': e, 'name': f'e{e}'} for e in exprs],
            'stage': stage, 'priority': pri, 'replace': replace, 'requires': requires,
            'profile': 'raw', 'revision': 1}
    write_hjp(path, meta, blocks, pari or [])
    return path


def expect(name, cond, detail=''):
    if cond:
        print(f"  OK   {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILS.append(name)


def main():
    base = build('base.hjp', layers=BASE_LAYERS, pari=[(0, 111), (1, 222)])

    # 1) 只有上游
    r = merge_all([base])
    b = r.get('t')
    expect('上游可单独加载', b is not None and b.has(0, L_COLOR) and b.has(0, L_ALPHA))
    expect('上游无材质', b is not None and not b.has(0, L_NORMAL))

    # 2) 上下游合并
    mat = build('mat.hjp', stage=1, requires={'stage': 0}, layers=MAT_LAYERS,
                pari=[(0, 111), (1, 222)])
    r = merge_all([base, mat])
    b = r['t']
    expect('合并后有法线/深度/蒙版',
           b.has(0, L_NORMAL, 0) and b.has(0, L_DEPTH) and b.has(0, L_MASK, 1))

    # 3) 只有下游 → 不出角色
    r = merge_all([mat])
    expect('只有下游不加载', 't' not in r, f'got {list(r)}')

    # 4) baseId 失配：只降级受影响的表情
    mat_bad = build('mat_bad.hjp', stage=1, requires={'stage': 0}, layers=MAT_LAYERS,
                    pari=[(0, 111), (1, 999)])
    b = merge_all([base, mat_bad])['t']
    expect('失配表情 1 无材质', not b.has(1, L_NORMAL))
    expect('未失配表情 0 正常', b.has(0, L_NORMAL))
    expect('记录降级原因', any(d['expr'] == 1 for d in b.degraded))

    # 5) submod 覆盖（replace=True + 更高 priority）
    sub = build('sub.hjp', stage=0, pri=500, replace=True, layers=[(L_COLOR, 0, 3)],
                pari=[(0, 111), (1, 222)], seed=7)
    b = merge_all([base, sub])['t']
    expect('replace 覆盖生效', b.provenance[(0, L_COLOR, 0)] == b.provenance[(1, L_COLOR, 0)])
    expect('覆盖者来自 sub', any(p.replace for p in b.packs))

    # 6) replace=False 但 priority 更高 → 必须失败
    try:
        merge_all([base, build('hi.hjp', stage=0, pri=900, layers=[(L_COLOR, 0, 3)],
                               pari=[(0, 111), (1, 222)], seed=9)])
        expect('replace=false 高优先级必须失败', False)
    except HjpMergeError:
        expect('replace=false 高优先级必须失败', True)

    # 7) 两个 replace 同优先级 → 失败
    a = build('r1.hjp', stage=0, pri=500, replace=True, layers=[(L_COLOR, 0, 3)],
              pari=[(0, 111), (1, 222)], seed=1)
    c = build('r2.hjp', stage=0, pri=500, replace=True, layers=[(L_COLOR, 0, 3)],
              pari=[(0, 111), (1, 222)], seed=2)
    try:
        merge_all([base, a, c])
        expect('同优先级 replace 必须失败', False)
    except HjpMergeError:
        expect('同优先级 replace 必须失败', True)

    # 8) A+A 幂等
    b1 = merge_all([base, mat])['t']
    b2 = merge_all([base, base, mat, mat])['t']
    expect('A+A 幂等', b1.dump()['blocks'] == b2.dump()['blocks'])

    # 9) 输入顺序无关
    dumps = set()
    for perm in itertools.permutations([base, mat]):
        dumps.add(json.dumps(merge_all(list(perm))['t'].dump(), sort_keys=True, ensure_ascii=False))
    expect('输入顺序无关', len(dumps) == 1, f'{len(dumps)} 种结果')

    # 10) 依赖链：缺 stage1 时 stage2 也应被丢弃
    s2 = build('s2.hjp', stage=2, requires={'stage': 1}, layers=[(L_DEPTH, 0, 1)],
               pari=[(0, 111), (1, 222)], seed=3)
    b = merge_all([base, s2])['t']
    expect('依赖链断裂逐级丢弃', not b.has(0, L_DEPTH), '仍有深度块')
    expect('s2 被丢弃', all(p.stage != 2 for p in b.packs), [p.stage for p in b.packs])

    print(f"\n{'PASS' if not FAILS else 'FAIL'}  ({len(FAILS)} 项失败)  {TMP}")
    sys.exit(0 if not FAILS else 1)


if __name__ == '__main__':
    main()
