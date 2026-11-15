#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成一致性黄金样本。

设计要点：全部使用 codec=raw（未压缩裸像素），
因此任何语言、零依赖都能解码——它只考验「容器」逻辑，不考验图像编解码器。
"""
import os, sys, json
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hjp import (write_hjp, Hjp, C_RAW, L_COLOR, L_NORMAL, L_DEPTH, L_MASK, L_ALPHA)

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'conformance')
W = H = 8


def pattern(seed, ch):
    grid = np.arange(W * H, dtype=np.uint16).reshape(H, W)
    out = np.empty((H, W, ch), np.uint8)
    for c in range(ch):
        out[..., c] = ((grid * (7 + c * 3) + seed * 29 + c * 40) % 256).astype(np.uint8)
    return out if ch > 1 else out[..., 0]


def main():
    os.makedirs(OUT, exist_ok=True)
    exprs, pari, blocks = [], [], []
    for eid, name in enumerate(['平静', '惊恐']):
        exprs.append({'id': eid, 'name': name})
        pari.append((eid, 0x1111222233334444 + eid * 0x1111111111111111))
        for layer, slot, ch in [(L_COLOR, 0, 3), (L_ALPHA, 0, 1), (L_NORMAL, 0, 1),
                                (L_NORMAL, 1, 1), (L_DEPTH, 0, 1),
                                (L_MASK, 0, 4), (L_MASK, 1, 2)]:
            blocks.append(dict(expr=eid, layer=layer, slot=slot,
                               data=pattern(eid * 10 + layer, ch).tobytes(),
                               codec=C_RAW, channels=ch, shift=0))

    meta = {
        'character': 'golden', 'name': '一致性样本', 'canvas': [W, H],
        'materials': ['m0', 'm1', 'm2', 'm3', 'm4', 'm5'],
        'expressions': exprs, 'stage': 0, 'role': 'conformance',
        'profile': 'raw-conformance', 'priority': 0, 'replace': False, 'revision': 1,
        'requires': None,
        'producer': {'tool': 'make_conformance', 'version': '1.0.0'},
        'conventions': {'colorSpace': 'sRGB', 'normalSpace': 'tangent', 'normalY': 'up',
                        'normalEncoding': 'unsigned255', 'depthRange': 'normalized',
                        'depthPolarity': 'nearWhite', 'alphaMode': 'straight',
                        'partitionFormula': 'min(colorA, mask)'},
    }
    path = os.path.join(OUT, 'golden.hjp')
    size = write_hjp(path, meta, blocks, pari)

    h = Hjp(path)
    dump = h.dump()
    json.dump(dump, open(os.path.join(OUT, 'golden.index.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)

    # 自检：逐块往返
    ok = True
    for b in dump['blocks']:
        arr, blk = h.image(b['expr'], b['layer'], b['slot'])
        src = pattern(b['expr'] * 10 + b['layer'], b['channels'])
        if not np.array_equal(arr, src):
            ok = False
            print(f"  往返失败 expr={b['expr']} layer={b['layer']} slot={b['slot']}")
    print(f"golden.hjp  {size} 字节  {len(dump['blocks'])} 块  往返={'OK' if ok else 'FAIL'}")
    print(f"golden.index.json  {len(open(os.path.join(OUT,'golden.index.json'),'rb').read())} 字节")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
