#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HJP 统一检查入口。任一子项失败即非零退出。

  python run_checks.py            # 快速（每档 2 个角色）
  python run_checks.py --full     # 全库

子项：
  1. probe_reject   —— 12 个坏包必须被拒
  2. test_bundle    —— 15 条合并规则
  3. conformance    —— golden.hjp 导建与期望一致
  4. verify_gate    —— 两档像素门禁（含全黑注入反例）
"""
import os, sys, json, subprocess, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
FULL = '--full' in sys.argv
QUICK = ['admin', 'femaleBM5'] if not FULL else [None]


def run(label, args):
    r = subprocess.run([PY] + args, cwd=HERE, capture_output=True, text=True, encoding='utf-8')
    ok = r.returncode == 0
    print(f"[{'PASS' if ok else 'FAIL'}] {label}  (exit {r.returncode})")
    if not ok:
        tail = (r.stdout or '')[-1200:] + (r.stderr or '')[-800:]
        print(tail)
    return ok


def main():
    results = []
    results.append(run('坏包探针', [os.path.join(HERE, 'probe_reject.py')]))
    results.append(run('合并规则', [os.path.join(HERE, 'test_bundle.py')]))

    # 一致性：重新导建并与期望比对
    g = os.path.join(HERE, 'conformance', 'golden.hjp')
    exp = os.path.join(HERE, 'conformance', 'golden.index.json')
    with tempfile.NamedTemporaryFile('r', suffix='.json', delete=False, encoding='utf-8') as t:
        tmp = t.name
    r = subprocess.run([PY, os.path.join(HERE, 'hjp.py'), 'dump', g, '--json', tmp],
                       capture_output=True, text=True, encoding='utf-8')
    ok = r.returncode == 0
    if ok:
        a = json.load(open(exp, encoding='utf-8'))
        b = json.load(open(tmp, encoding='utf-8'))
        ok = a == b
    os.unlink(tmp)
    print(f"[{'PASS' if ok else 'FAIL'}] 一致性样本")
    results.append(ok)

    LEGACY = r"C:\Users\Administrator\Desktop\成品"
    if not os.path.isdir(LEGACY):
        print(f"[SKIP] 像素门禁：源素材目录已不存在 {LEGACY}")
        print("       （旧 20 角色基线已并入新合集，改用 verify_collection.py 校验新批次）")
    else:
        for prefix in QUICK:
            tag = prefix or '全库'
            base = [os.path.join(HERE, 'verify_gate.py')] + ([prefix] if prefix else [])
            results.append(run(f'门禁 lossless {tag}', base + ['--lossless']))
            results.append(run(f'门禁 compact {tag}', base))

        # 反例：全黑注入必须失败
        args = [os.path.join(HERE, 'verify_gate.py')] + ([QUICK[0]] if QUICK[0] else ['admin']) + \
               ['--lossless', '--mutate', 'black']
        r = subprocess.run([PY] + args, cwd=HERE, capture_output=True, text=True, encoding='utf-8')
        ok = r.returncode != 0
        print(f"[{'PASS' if ok else 'FAIL'}] 反例：全黑注入必须失败 (exit {r.returncode})")
        results.append(ok)

    bad = results.count(False)
    print(f"\n{'ALL PASS' if bad == 0 else f'{bad} 项失败'}  ({len(results)} 项)")
    sys.exit(0 if bad == 0 else 1)


if __name__ == '__main__':
    main()
