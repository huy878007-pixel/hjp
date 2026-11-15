#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反例探针：对坏包必须抛 HjpError。任一探针未被拒绝即视为失败。"""
import os, sys, shutil, tempfile, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hjp import Hjp, HjpError

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'build', 'admin.base.hjp')
TMP = tempfile.mkdtemp(prefix='hjp-probe-')


def load():
    return bytearray(open(SRC, 'rb').read())


def patch(b, off, data):
    b[off:off + len(data)] = data


def blocks(b):
    n = struct.unpack_from('<I', b, 12)[0]
    return n, [32 + i * 16 for i in range(n)]


def probe(name, mutate, expect: str):
    b = load()
    mutate(b)
    p = os.path.join(TMP, name + '.hjp')
    open(p, 'wb').write(b)
    try:
        Hjp(p)
    except HjpError as e:
        print(f"  OK   {name:22s} → 拒绝（{type(e).__name__}）")
        return True
    except Exception as e:                       # noqa: BLE001
        print(f"  BAD  {name:22s} → 抛了非 HjpError：{type(e).__name__}: {e}")
        return False
    print(f"  BAD  {name:22s} → 竟然接受了")
    return False


def main():
    print(f"探针样本：{SRC}")
    ok = True
    ok &= probe('truncated', lambda b: b.__delitem__(slice(len(b) // 2, None)), 'HjpFormatError')
    ok &= probe('bad_magic', lambda b: patch(b, 0, b'HJX1'), 'HjpFormatError')
    ok &= probe('bad_major', lambda b: patch(b, 4, struct.pack('<H', 99)), 'HjpFormatError')
    ok &= probe('bad_header_crc', lambda b: patch(b, 28, struct.pack('<I', 0xDEADBEEF)), 'HjpFormatError')
    ok &= probe('header_size_unaligned', lambda b: patch(b, 8, struct.pack('<I', 4097)), 'HjpFormatError')

    def dup_key(b):
        n, offs = blocks(b)
        patch(b, offs[1], b[offs[0]:offs[0] + 4])       # 复制第一块的 expr/layer/slot
    ok &= probe('duplicate_key', dup_key, 'HjpFormatError')

    def bad_codec(b):
        n, offs = blocks(b)
        patch(b, offs[0] + 12, b'\x63')
    ok &= probe('unknown_codec', bad_codec, 'HjpFormatError')

    def bad_channels(b):
        n, offs = blocks(b)
        patch(b, offs[0] + 13, b'\x01')                 # 颜色块声明 1 通道
    ok &= probe('bad_channels', bad_channels, 'HjpFormatError')

    def bad_shift(b):
        n, offs = blocks(b)
        patch(b, offs[0] + 14, b'\x07')
    ok &= probe('bad_scale_shift', bad_shift, 'HjpFormatError')

    def oob_offset(b):
        n, offs = blocks(b)
        patch(b, offs[0] + 4, struct.pack('<I', 0xFFFFFF0))
    ok &= probe('oob_offset', oob_offset, 'HjpFormatError')

    def critical_chunk(b):
        i = b.find(b'pari')
        if i < 0:
            raise RuntimeError('样本里没有 pari')
        patch(b, i, b'P')
    ok &= probe('unknown_critical_chunk', critical_chunk, 'HjpUnsupported')

    def bad_meta_offset(b):
        patch(b, 20, struct.pack('<I', 999))
    ok &= probe('bad_meta_offset', bad_meta_offset, 'HjpFormatError')

    print(f"\n{'PASS' if ok else 'FAIL'}  探针目录 {TMP}")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
