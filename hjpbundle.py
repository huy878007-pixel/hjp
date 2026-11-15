#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HJP 逻辑合并器（上游 / 下游 / submod）

规则（与规格 13.3 / 13.6 统一后的版本）：

1. 同一文件重复输入按内容 SHA-256 去重（A+A 幂等）。
2. 分层：只按 stage 判定依赖，不做隐式优先级。
3. 依赖：带 requires.stage 的包，只有在同角色、同画布存在该 stage 时才参与；
   不满足则整包丢弃（连同依赖它的更高层）。
4. 冲突：同一 (expr, layer, slot) 被多个包提供时——
     内容完全相同       → 去重
     全部 replace=false  → 失败（无法裁决，不按优先级硬盖）
     replace=true 者    → 必须严格高于所有 replace=false 者的 priority，且互相比 priority
     同优先级 replace    → 失败
5. 配对：每个表情的期望 baseId 来自该表情「颜色块」的提供者；
   其他包若声明了同表情但 baseId 不同 → 只禁用该包在该表情上的块。
6. 结果与输入顺序无关：所有排序都带内容哈希作为最终决胜键。
"""
import hashlib
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hjp import Hjp, HjpError, L_COLOR, L_ALPHA, SHARED  # noqa: E402


class HjpMergeError(HjpError, ValueError):
    """无法裁决的合并冲突"""


class PackRef:
    __slots__ = ('hjp', 'sig', 'stage', 'priority', 'replace', 'pid', 'requires')

    def __init__(self, hjp):
        self.hjp = hjp
        self.sig = hashlib.sha256(hjp.buf).hexdigest()
        m = hjp.meta
        self.stage = int(m.get('stage', 0) or 0)
        self.priority = int(m.get('priority', 0) or 0)
        self.replace = bool(m.get('replace', False))
        self.requires = m.get('requires')
        self.pid = m.get('character', '?')

    @property
    def sort_key(self):
        return (self.stage, self.priority, self.sig)

    def __repr__(self):
        return f"<Pack {self.pid} stage={self.stage} pri={self.priority} rep={self.replace}>"


class Bundle:
    """一个角色的逻辑对象：块可来自多个物理文件，零拷贝。"""

    def __init__(self, character, canvas, packs):
        self.character = character
        self.canvas = canvas
        self.packs = packs                      # list[PackRef]，按 sort_key 升序
        self.blocks = {}                        # key -> (pack_idx, block)
        self.provenance = {}                    # key -> sig
        self.degraded = []                      # [{expr, pack, reason}]
        self.dropped = []                       # [{sig, reason}]
        self._resolve()

    # -- 解析 -------------------------------------------------------
    def _resolve(self):
        # 1) requires 不满足的包整体丢弃，直到稳定
        alive = list(range(len(self.packs)))
        changed = True
        while changed:
            changed = False
            stages = {self.packs[i].stage for i in alive}
            keep = []
            for i in alive:
                req = self.packs[i].requires
                if req and req.get('stage') not in stages:
                    self.dropped.append({'sig': self.packs[i].sig,
                                         'reason': f"缺少 stage {req.get('stage')}"})
                    changed = True
                else:
                    keep.append(i)
            alive = keep
        # 被丢弃的包不参与，也不出现在合并结果里
        self.packs = [self.packs[i] for i in alive]
        if not self.packs:
            return

        # 2) 收集候选
        cand = defaultdict(list)
        for i in range(len(self.packs)):
            for k in self.packs[i].hjp.blocks:
                cand[k].append(i)

        # 3) 逐键裁决
        for key, who in sorted(cand.items()):
            winner = self._pick(key, who)
            self.blocks[key] = (winner, self.packs[winner].hjp.blocks[key])
            self.provenance[key] = self.packs[winner].sig

        # 4) 逐表情配对（对参与的全部包）
        self._pair(range(len(self.packs)))

    def _pick(self, key, who):
        if len(who) == 1:
            return who[0]
        # 内容完全相同的去重
        buckets = defaultdict(list)
        for i in who:
            b = self.packs[i].hjp.blocks[key]
            data = self.packs[i].hjp.buf[b['off']:b['off'] + b['len']]
            buckets[hashlib.sha256(data).hexdigest()].append(i)
        reps = [v[0] for v in buckets.values()]
        if len(reps) == 1:
            return reps[0]

        base = [i for i in reps if not self.packs[i].replace]
        over = [i for i in reps if self.packs[i].replace]
        if not over:
            raise HjpMergeError(
                f"{self.character} 键 {key} 有 {len(reps)} 个不同来源且都未声明 replace："
                + ", ".join(self.packs[i].sig[:8] for i in reps))
        if over:
            over.sort(key=lambda i: (-self.packs[i].priority, self.packs[i].sig))
            top = over[0]
            if len(over) > 1 and self.packs[over[1]].priority == self.packs[top].priority:
                raise HjpMergeError(f"{self.character} 键 {key}：多个 replace 包同优先级，无法裁决")
            if base and self.packs[top].priority <= max(self.packs[i].priority for i in base):
                raise HjpMergeError(
                    f"{self.character} 键 {key}：replace 包 priority {self.packs[top].priority} "
                    f"未严格高于被覆盖者")
            return top

    def _pair(self, alive):
        # 期望 baseId：每个表情颜色块的提供者
        expected = {}
        for (e, layer, slot), (pi, _b) in self.blocks.items():
            if layer == L_COLOR and slot == 0:
                expected[e] = self.packs[pi].hjp.pari.get(e)

        bad = defaultdict(list)
        for i in alive:
            h = self.packs[i].hjp
            for e, bid in h.pari.items():
                exp = expected.get(e)
                if exp is not None and bid != exp:
                    bad[i].append(e)

        for i, exprs in bad.items():
            for e in exprs:
                self.degraded.append({'expr': e, 'sig': self.packs[i].sig,
                                      'reason': f"baseId 失配（期望 {expected.get(e)}，实为 {self.packs[i].hjp.pari.get(e)}）"})
                for key in [k for k, (pi, _b) in self.blocks.items()
                            if pi == i and k[0] == e and k[1] != L_COLOR]:
                    del self.blocks[key]
                    self.provenance.pop(key, None)

    # -- 访问（与 Hjp 同接口）--------------------------------------
    def has(self, expr, layer, slot=0):
        return (expr, layer, slot) in self.blocks

    def raw(self, expr, layer, slot=0):
        pi, b = self.blocks[(expr, layer, slot)]
        return self.packs[pi].hjp.raw(expr, layer, slot)

    def image(self, expr, layer, slot=0):
        if (expr, layer, slot) not in self.blocks:
            raise KeyError(f"缺少块 {expr}/{layer}/{slot}")
        pi, _b = self.blocks[(expr, layer, slot)]
        h = self.packs[pi].hjp
        return h.image(expr, layer, slot)

    def gray(self, expr, layer, slot=0):
        pi, _b = self.blocks[(expr, layer, slot)]
        return self.packs[pi].hjp.gray(expr, layer, slot)

    def expected_size(self, expr, layer, slot=0):
        """块的真实像素尺寸（已考虑 dimn 与 scale_shift）"""
        pi, _b = self.blocks[(expr, layer, slot)]
        return self.packs[pi].hjp.expected_size(expr, layer, slot)

    def silhouette(self, expr):
        if self.has(expr, L_ALPHA, 0):
            return self.gray(expr, L_ALPHA, 0)
        if self.has(SHARED, L_ALPHA, 0):
            return self.gray(SHARED, L_ALPHA, 0)
        raise KeyError(f"表情 {expr} 无轮廓")

    @property
    def expressions(self):
        for i in range(len(self.packs)):
            ex = self.packs[i].hjp.meta.get('expressions')
            if ex:
                return ex
        return []

    def dump(self):
        return {
            'character': self.character,
            'canvas': list(self.canvas),
            'packs': [{'sig': p.sig[:16], 'stage': p.stage, 'priority': p.priority,
                       'replace': p.replace, 'stage_declared': p.hjp.meta.get('stage')}
                      for p in self.packs],
            'blocks': sorted([{'expr': k[0], 'layer': k[1], 'slot': k[2],
                               'from': self.provenance[k][:16]} for k in self.blocks],
                             key=lambda x: (x['expr'], x['layer'], x['slot'])),
            'degraded': self.degraded,
            'dropped': self.dropped,
        }


def merge_group(items):
    """items: list[PackRef]，同角色同画布"""
    canvas = items[0].hjp.canvas
    for p in items:
        if p.hjp.canvas != canvas:
            raise HjpMergeError(f"{p.hjp.meta['character']} 画布不一致：{p.hjp.canvas} vs {canvas}")
    return Bundle(items[0].hjp.meta['character'], canvas, sorted(items, key=lambda p: p.sort_key))


def merge_all(paths, decoders=None):
    """返回 {character: Bundle}。只含下游的角色不会出现在结果里。"""
    seen, refs = {}, []
    for p in paths:
        h = Hjp(p, decoders)
        sig = hashlib.sha256(h.buf).hexdigest()
        if sig in seen:
            continue
        seen[sig] = True
        refs.append(PackRef(h))
    groups = defaultdict(list)
    for r in refs:
        groups[r.hjp.meta['character']].append(r)
    out = {}
    for ch, items in groups.items():
        try:
            b = merge_group(items)
        except HjpMergeError:
            raise
        if not b.blocks:
            continue                      # 只有下游 → 不出逻辑对象
        out[ch] = b
    return out


if __name__ == '__main__':
    import json
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    res = merge_all(args)
    print(json.dumps({k: v.dump() for k, v in res.items()}, ensure_ascii=False, indent=1))
