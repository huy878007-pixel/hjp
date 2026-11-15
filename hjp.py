#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HJP v1 —— 引擎无关的参考实现（打包 / 严格读取 / 规范化导建）

设计要点
  1. 读取器对任何输入都做完整校验，坏包一律抛异常，不"尽力而为"。
  2. 不用 assert 验证外部数据（-O 会移除）。
  3. 解码按块声明的 codec 分派，不交给 Pillow 猜格式。
  4. 像素总额有上限，不全局关闭 Pillow 的防爆闸。
  5. 表情 ID 由持久化注册表分配，只增、不复用、可留墓碑。

命令
  python hjp.py build  [前缀|all] [--lossless] [--out DIR]
  python hjp.py dump   <file.hjp> [--json out.json]
  python hjp.py check  <file.hjp>
"""
import os, io, json, struct, hashlib, zlib, sys
import numpy as np
from PIL import Image

# 不关闭 Pillow 的像素上限，给一个够用但有界的值（832x1248 = 1.04M）
Image.MAX_IMAGE_PIXELS = 64 * 1024 * 1024

MAGIC = b'HJP1'
VMINOR = 0
L_COLOR, L_NORMAL, L_DEPTH, L_MASK, L_ALPHA = 0, 1, 2, 3, 4
C_RAW, C_PNG, C_WEBP, C_BC7, C_BC4, C_ZLIB, C_LZ4, C_ASTC4, C_ASTC6 = range(9)
CODEC_NAME = {C_RAW: 'raw', C_PNG: 'png', C_WEBP: 'webp', C_BC7: 'bc7', C_BC4: 'bc4',
              C_ZLIB: 'zlib', C_LZ4: 'lz4', C_ASTC4: 'astc4x4', C_ASTC6: 'astc6x6'}
CODEC_ID = {v: k for k, v in CODEC_NAME.items()}
SUPPORTED_DECODERS = {'raw', 'png', 'webp'}          # 参考实现能解的
SHARED = 0xFFFF
PARTS = ["皮肤", "毛发_含眉毛", "服装", "金属饰品", "布料饰品", "眼睛_尚未拆分眼白虹膜"]

# (layer, slot) -> 允许的通道数
CHANNELS = {(0, 0): (3,), (1, 0): (1, 3), (1, 1): (1,),
            (2, 0): (1,), (3, 0): (4,), (3, 1): (2,), (3, 2): (1, 2),
            (4, 0): (1,), (4, 1): (1,)}

MAX_BLOCKS = 4096
MAX_EXPRS = 4096
MAX_CANVAS = 8192
MAX_PIXELS = MAX_CANVAS * MAX_CANVAS


class HjpError(Exception):
    """基类"""


class HjpFormatError(HjpError, ValueError):
    """文件结构不合法"""


class HjpUnsupported(HjpError, NotImplementedError):
    """依赖未实现的可选能力（codec / critical 扩展）"""


class HjpMissing(HjpError, KeyError):
    """请求的块不存在"""


class HjpBuildError(HjpError, RuntimeError):
    """构建前置检查失败"""


def align(n, a=16):
    return (n + a - 1) // a * a


# ------------------------------------------------------------------ 编码

def _save(arr, fmt, **kw):
    b = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(arr)).save(b, fmt, **kw)
    return b.getvalue()


def enc_rgb(arr, q, lossless):
    if lossless:
        return _save(arr, 'WEBP', lossless=True, method=6, quality=100)
    return _save(arr, 'WEBP', quality=q, method=6)


def _save_l(arr, lossless):
    return _save(arr, 'PNG', optimize=True) if lossless else _save(arr, 'WEBP', quality=90, method=6)


def enc_la(a, b_):
    im = Image.fromarray(np.ascontiguousarray(a), 'L')
    im.putalpha(Image.fromarray(np.ascontiguousarray(b_), 'L'))
    buf = io.BytesIO()
    im.save(buf, 'PNG', optimize=True)
    return buf.getvalue()


def source_id(rgb):
    """来源身份：颜色像素的 64 位指纹（与 HJP 编码无关，有损重编码不改身份）"""
    return int.from_bytes(hashlib.sha256(np.ascontiguousarray(rgb).tobytes()).digest()[:8], 'little')


# ------------------------------------------------------------------ 写

def write_hjp(path, meta, blocks, pari):
    keys = set()
    for b in blocks:
        k = (b['expr'], b['layer'], b['slot'])
        if k in keys:
            raise HjpBuildError(f"重复块 {k}")
        keys.add(k)
        ch = CHANNELS.get((b['layer'], b['slot']))
        if ch is None or b['channels'] not in ch:
            raise HjpBuildError(f"块 {k} 通道数 {b['channels']} 不合法")

    nblocks, nexpr = len(blocks), len(meta['expressions'])
    if nblocks > MAX_BLOCKS or nexpr > MAX_EXPRS:
        raise HjpBuildError("块或表情数量超限")

    meta_bytes = json.dumps(meta, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    meta_bytes += b' ' * ((-len(meta_bytes)) % 4)

    cw, chh = meta['canvas']
    chunks = b''
    if pari:
        body = struct.pack('<H', len(pari)) + b''.join(struct.pack('<HQ', e, h) for e, h in pari)
        c = b'pari' + struct.pack('<I', len(body)) + body
        c += b'\0' * ((-len(c)) % 4)
        chunks += c

    # dimn：块尺寸与「画布 >> scale_shift」不同时需要的显式尺寸
    def _wh(b):
        return (b.get('w', cw >> b['shift']), b.get('h', chh >> b['shift']))
    over = [(b['expr'], b['layer'], b['slot'], *_wh(b)) for b in blocks
            if _wh(b) != (cw >> b['shift'], chh >> b['shift'])]
    if over:
        body = struct.pack('<H', len(over)) + b''.join(
            struct.pack('<HBBHH', *o) for o in over)
        c = b'dimn' + struct.pack('<I', len(body)) + body
        c += b'\0' * ((-len(c)) % 4)
        chunks += c

    meta_off = 32 + nblocks * 16 + nexpr * 4
    data_start = align(meta_off + len(meta_bytes) + len(chunks))
    flags = 0x0002 if chunks else 0

    data = bytearray()
    table = bytearray()
    for b in blocks:
        data += b'\0' * ((-len(data)) % 16)
        table += struct.pack('<HBBIIBBBB', b['expr'], b['layer'], b['slot'], len(data),
                             len(b['data']), b['codec'], b['channels'], b['shift'], 0)
        data += b['data']

    expr_table = bytearray()
    for i, e in enumerate(meta['expressions']):
        expr_table += struct.pack('<HH', e['id'], i)

    hdr = struct.pack('<4sHHIIHHIII', MAGIC, 1, VMINOR, data_start, nblocks, nexpr,
                      flags, meta_off, len(meta_bytes), 0)
    crc = zlib.crc32(bytes(hdr[:28]))
    crc = zlib.crc32(bytes(table), crc)
    crc = zlib.crc32(bytes(expr_table), crc) & 0xFFFFFFFF
    hdr = hdr[:28] + struct.pack('<I', crc)

    head = hdr + bytes(table) + bytes(expr_table) + meta_bytes + chunks
    head += b'\0' * (data_start - len(head))

    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(head)
        f.write(bytes(data))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return len(head) + len(data)


# ------------------------------------------------------------------ 读

class Hjp:
    """严格读取器。构造即完成结构校验；任何不合法都会抛 HjpFormatError。"""

    def __init__(self, path, decoders=None):
        self.path = path
        self.decoders = set(SUPPORTED_DECODERS if decoders is None else decoders)
        with open(path, 'rb') as f:
            self.buf = f.read()
        self._parse()

    # -- 结构校验 ---------------------------------------------------
    def _parse(self):
        b, n = self.buf, len(self.buf)
        if n < 32:
            raise HjpFormatError("文件短于固定头")
        (magic, vmaj, vmin, hsize, nblocks, nexpr, flags,
         meta_off, meta_len, hcrc) = struct.unpack_from('<4sHHIIHHIII', b, 0)
        if magic != MAGIC:
            raise HjpFormatError(f"magic 不符：{magic!r}")
        if vmaj != 1:
            raise HjpFormatError(f"不支持的主版本 {vmaj}")
        if flags & ~0x0007:
            raise HjpFormatError(f"未知 flags 位 {flags:#x}")
        if hsize % 16 or hsize < 32 or hsize > n:
            raise HjpFormatError(f"header_size 非法：{hsize}")
        if nblocks > MAX_BLOCKS or nexpr > MAX_EXPRS:
            raise HjpFormatError("块或表情数量超限")
        tables_end = 32 + nblocks * 16 + nexpr * 4
        if tables_end > hsize:
            raise HjpFormatError("块表/表情表越界")
        if meta_off != tables_end:
            raise HjpFormatError(f"meta_offset 应为 {tables_end}，实为 {meta_off}")
        if meta_len == 0 or meta_off + meta_len > hsize:
            raise HjpFormatError("元数据越界")

        # 头 CRC：覆盖 [0:28] + 块表 + 表情表
        calc = zlib.crc32(b[:28])
        calc = zlib.crc32(b[32:tables_end], calc) & 0xFFFFFFFF
        if hcrc != 0 and calc != hcrc:
            raise HjpFormatError(f"头 CRC 不符：存 {hcrc:#x}，算 {calc:#x}")

        self.version = (vmaj, vmin)
        self.header_size = hsize
        self.flags = flags

        # 块表
        self.blocks = {}
        spans = []
        for i in range(nblocks):
            expr, layer, slot, off, ln, codec, ch, shift, bflags = struct.unpack_from(
                '<HBBIIBBBB', b, 32 + i * 16)
            key = (expr, layer, slot)
            if key in self.blocks:
                raise HjpFormatError(f"重复块键 {key}")
            if bflags & ~0x0001:
                raise HjpFormatError(f"块 {key} 未知 flags {bflags:#x}")
            if off % 16:
                raise HjpFormatError(f"块 {key} 未 16 字节对齐")
            if hsize + off + ln > n:
                raise HjpFormatError(f"块 {key} 越界")
            if codec not in CODEC_NAME:
                raise HjpFormatError(f"块 {key} 未知 codec {codec}")
            allowed = CHANNELS.get((layer, slot))
            if allowed is None:
                raise HjpFormatError(f"未知 layer/slot {layer}/{slot}")
            if ch not in allowed:
                raise HjpFormatError(f"块 {key} 通道数 {ch} 不允许（应为 {allowed}）")
            if shift > 3:
                raise HjpFormatError(f"块 {key} scale_shift {shift} 非法")
            spans.append((off, off + ln, key))
            self.blocks[key] = dict(off=off + hsize, len=ln, codec=codec,
                                    channels=ch, shift=shift, flags=bflags)
        spans.sort()
        for (s0, e0, k0), (s1, e1, k1) in zip(spans, spans[1:]):
            if s1 < e0:
                raise HjpFormatError(f"块重叠：{k0} 与 {k1}")

        # 表情表
        self.expr_names = {}
        seen = set()
        for i in range(nexpr):
            eid, ni = struct.unpack_from('<HH', b, 32 + nblocks * 16 + i * 4)
            if eid in seen:
                raise HjpFormatError(f"表情 id 重复：{eid}")
            if eid >= SHARED:
                raise HjpFormatError(f"表情 id 不得使用 0xFFFF：{eid}")
            seen.add(eid)
            self.expr_names[eid] = ni

        # 元数据
        raw_meta = b[meta_off:meta_off + meta_len]
        try:
            self.meta = json.loads(raw_meta.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise HjpFormatError(f"元数据 JSON 非法：{e}") from e
        self._check_meta()

        # 扩展块
        self.chunks = {}
        p = align(meta_off + meta_len, 4)
        while p < hsize:
            if hsize - p < 8:
                if any(b[p:hsize]):
                    raise HjpFormatError("扩展区尾部有非零残字节")
                break
            ctype = b[p:p + 4]
            if ctype == b'\0\0\0\0':
                if any(b[p:hsize]):
                    raise HjpFormatError("扩展区出现零类型但后续非零")
                break
            clen = struct.unpack_from('<I', b, p + 4)[0]
            if p + 8 + clen > hsize:
                raise HjpFormatError(f"扩展块 {ctype!r} 越界")
            body = b[p + 8:p + 8 + clen]
            critical = not (ctype[0] & 0x20)          # 首字节第 5 位 = 0 → critical
            if critical:
                raise HjpUnsupported(f"未知 critical 扩展块 {ctype!r}，拒绝加载")
            self.chunks[ctype] = body
            p = align(p + 8 + clen, 4)

        # dimn 显式尺寸（与画布 >> scale_shift 不同的块）
        self.sizes = {}
        if b'dimn' in self.chunks:
            body = self.chunks[b'dimn']
            if len(body) < 2:
                raise HjpFormatError("dimn 过短")
            cnt = struct.unpack_from('<H', body, 0)[0]
            if 2 + cnt * 8 > len(body):
                raise HjpFormatError("dimn 越界")
            for k in range(cnt):
                e, l, s, w, h = struct.unpack_from('<HBBHH', body, 2 + k * 8)
                key = (e, l, s)
                if key in self.sizes:
                    raise HjpFormatError(f"dimn 键重复 {key}")
                if not (0 < w <= MAX_CANVAS and 0 < h <= MAX_CANVAS):
                    raise HjpFormatError(f"dimn 尺寸非法 {w}x{h}")
                if key not in self.blocks:
                    raise HjpFormatError(f"dimn 指向不存在的块 {key}")
                self.sizes[key] = (w, h)

        # pari 配对表
        self.pari = {}
        if b'pari' in self.chunks:
            body = self.chunks[b'pari']
            if len(body) < 2:
                raise HjpFormatError("pari 过短")
            cnt = struct.unpack_from('<H', body, 0)[0]
            if 2 + cnt * 10 > len(body):
                raise HjpFormatError("pari 越界")
            for k in range(cnt):
                e, h = struct.unpack_from('<HQ', body, 2 + k * 10)
                if e in self.pari:
                    raise HjpFormatError(f"pari 表情 id 重复：{e}")
                self.pari[e] = h

    def _check_meta(self):
        m = self.meta
        if not isinstance(m, dict):
            raise HjpFormatError("元数据不是对象")
        for k in ('character', 'canvas', 'materials', 'expressions'):
            if k not in m:
                raise HjpFormatError(f"元数据缺字段 {k}")
        c = m['canvas']
        if not (isinstance(c, list) and len(c) == 2 and
                all(isinstance(v, int) and 0 < v <= MAX_CANVAS for v in c)):
            raise HjpFormatError(f"canvas 非法：{c}")
        if c[0] * c[1] > MAX_PIXELS:
            raise HjpFormatError("canvas 像素数超限")
        if not (isinstance(m['materials'], list) and m['materials'] and
                all(isinstance(x, str) and x for x in m['materials'])):
            raise HjpFormatError("materials 非法")
        if len(set(m['materials'])) != len(m['materials']):
            raise HjpFormatError("materials 有重复")
        ex = m['expressions']
        if not (isinstance(ex, list) and 0 < len(ex) <= MAX_EXPRS):
            raise HjpFormatError("expressions 非法")
        ids = set()
        for e in ex:
            if not isinstance(e, dict) or not isinstance(e.get('id'), int) or not isinstance(e.get('name'), str):
                raise HjpFormatError(f"expression 项非法：{e}")
            if e['id'] in ids or e['id'] >= SHARED:
                raise HjpFormatError(f"expression id 非法或重复：{e['id']}")
            ids.add(e['id'])
        if ids != set(self.expr_names):
            raise HjpFormatError("表情表与元数据 expressions 不一致")
        for eid, ni in self.expr_names.items():
            if ni >= len(ex):
                raise HjpFormatError(f"表情表 name_index {ni} 越界")
        self.canvas = tuple(c)

    # -- 访问 -------------------------------------------------------
    def has(self, expr, layer, slot=0):
        return (expr, layer, slot) in self.blocks

    def block(self, expr, layer, slot=0):
        try:
            return self.blocks[(expr, layer, slot)]
        except KeyError:
            raise HjpMissing(f"缺少块 expr={expr} layer={layer} slot={slot}") from None

    def expected_size(self, expr, layer, slot=0):
        self.block(expr, layer, slot)
        if (expr, layer, slot) in self.sizes:
            return self.sizes[(expr, layer, slot)]
        b = self.blocks[(expr, layer, slot)]
        return max(1, self.canvas[0] >> b['shift']), max(1, self.canvas[1] >> b['shift'])

    def raw(self, expr, layer, slot=0):
        b = self.block(expr, layer, slot)
        data = self.buf[b['off']:b['off'] + b['len']]
        if b['flags'] & 1:
            if len(data) < 4:
                raise HjpFormatError("块 CRC 截断")
            body, expect = data[:-4], struct.unpack('<I', data[-4:])[0]
            if zlib.crc32(body) & 0xFFFFFFFF != expect:
                raise HjpFormatError(f"块 CRC 不符 expr={expr} layer={layer} slot={slot}")
            return body
        return data

    def image(self, expr, layer, slot=0):
        """按声明 codec 解码；返回 (ndarray, block)"""
        b = self.block(expr, layer, slot)
        name = CODEC_NAME[b['codec']]
        if name not in self.decoders:
            raise HjpUnsupported(f"不支持的 codec {name}（expr={expr} layer={layer} slot={slot}）")
        data = self.raw(expr, layer, slot)
        w, h = self.expected_size(expr, layer, slot)

        if name == 'png':
            if data[:8] != b'\x89PNG\r\n\x1a\n':
                raise HjpFormatError("声明为 png 但签名不符")
            arr = np.array(Image.open(io.BytesIO(data)))
        elif name == 'webp':
            if data[:4] != b'RIFF' or data[8:12] != b'WEBP':
                raise HjpFormatError("声明为 webp 但签名不符")
            arr = np.array(Image.open(io.BytesIO(data)))
        elif name == 'raw':
            need = w * h * b['channels']
            if len(data) != need:
                raise HjpFormatError(f"raw 块长度 {len(data)} 应为 {need}")
            arr = np.frombuffer(data, np.uint8).reshape(h, w, b['channels'])
            arr = arr[..., 0] if b['channels'] == 1 else arr
        else:
            raise HjpUnsupported(f"codec {name} 未实现")

        got = (arr.shape[1], arr.shape[0])
        if got != (w, h):
            raise HjpFormatError(f"解码尺寸 {got} 与声明 {(w, h)} 不符")
        got_ch = 1 if arr.ndim == 2 else arr.shape[2]
        if b['channels'] == 1 and got_ch in (3, 4):
            arr = arr[..., 0]
        elif b['channels'] == 3 and got_ch == 4:
            arr = arr[..., :3]
        elif got_ch != b['channels']:
            raise HjpFormatError(f"解码通道 {got_ch} 与声明 {b['channels']} 不符")
        return arr, b

    def gray(self, expr, layer, slot=0):
        arr, _ = self.image(expr, layer, slot)
        return arr if arr.ndim == 2 else arr[..., 0]

    def silhouette(self, expr):
        if self.has(expr, L_ALPHA, 0):
            return self.gray(expr, L_ALPHA, 0)
        if self.has(SHARED, L_ALPHA, 0):
            return self.gray(SHARED, L_ALPHA, 0)
        raise HjpMissing(f"表情 {expr} 没有轮廓数据")

    # -- 导建 -------------------------------------------------------
    def dump(self):
        """规范化导建：任何语言的实现都应能产出相同结构，用于跨实现一致性比对"""
        blocks = []
        for (e, l, s), b in sorted(self.blocks.items()):
            data = self.buf[b['off']:b['off'] + b['len']]
            w, h = self.expected_size(e, l, s)
            blocks.append({
                'expr': e, 'layer': l, 'slot': s,
                'codec': CODEC_NAME[b['codec']], 'channels': b['channels'],
                'scale_shift': b['shift'], 'width': w, 'height': h,
                'length': b['len'],
                'sha256': hashlib.sha256(data).hexdigest()[:16],
            })
        return {
            'magic': MAGIC.decode(), 'version': list(self.version),
            'size': len(self.buf), 'header_size': self.header_size,
            'canvas': list(self.canvas), 'character': self.meta['character'],
            'stage': self.meta.get('stage'), 'profile': self.meta.get('profile'),
            'blocks': blocks,
            'chunks': sorted(k.decode('ascii', 'replace') for k in self.chunks),
            'pari': {str(k): v for k, v in sorted(self.pari.items())},
        }


# ------------------------------------------------------------------ 稳定 ID

REGISTRY = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'expr_ids.json')


def allocate_ids(character, names):
    """持久化 name→id。只增、不复用；已被删除的名字保留在表里作为墓碑。"""
    reg = {}
    if os.path.exists(REGISTRY):
        reg = json.load(open(REGISTRY, encoding='utf-8'))
    ent = reg.setdefault(character, {'next': 0, 'map': {}, 'removed': []})
    out = []
    for nm in names:
        if nm not in ent['map']:
            if ent['next'] >= SHARED - 1:
                raise HjpBuildError(f"{character}: 表情 id 用尽")
            ent['map'][nm] = ent['next']
            ent['next'] += 1
        out.append((nm, ent['map'][nm]))
    live = set(names)
    ent['removed'] = sorted(set(ent['removed']) | (set(ent['map']) - live))
    tmp = REGISTRY + '.tmp'
    json.dump(reg, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, REGISTRY)
    return out


# ------------------------------------------------------------------ 构建

def _load_rgba(p):
    return np.array(Image.open(p).convert('RGBA'))


def check_sources(cp, V, names, partitions='first'):
    """降维之前检查原始通道。任何一条不满足就拒绝构建。"""
    base0 = None
    for nm in names:
        base = _load_rgba(os.path.join(cp, nm + '.png'))
        if base0 is None:
            base0 = base
        elif base.shape != base0.shape:
            raise HjpBuildError(f"{nm}: 画布与其他表情不一致 {base.shape} vs {base0.shape}")
        nrm = _load_rgba(os.path.join(V, nm, '法线图.png'))
        dep = _load_rgba(os.path.join(V, nm, '深度图.png'))
        if not np.array_equal(nrm[..., 3], dep[..., 3]):
            raise HjpBuildError(f"{nm}: 法线/深度 alpha 不同，无法共享有效域")
        if not (np.array_equal(dep[..., 0], dep[..., 1]) and np.array_equal(dep[..., 0], dep[..., 2])):
            raise HjpBuildError(f"{nm}: 深度图 RGB 三通道不相等，单通道存储会丢数据")
        for p in PARTS:
            m = _load_rgba(os.path.join(V, nm, '材质蒙版', p + '.png'))
            if not (np.array_equal(m[..., 0], m[..., 1]) and np.array_equal(m[..., 0], m[..., 2])):
                raise HjpBuildError(f"{nm}/{p}: 蒙版 RGB 三通道不相等，单通道存储会丢数据")
            if not np.all(m[..., 3] == 255):
                raise HjpBuildError(f"{nm}/{p}: 蒙版 alpha 非全 255，当前布局未定义其含义")
        if partitions != 'none':
            todo = names if partitions == 'all' else names[:1]
            if nm in todo:
                for p in PARTS:
                    part = _load_rgba(os.path.join(V, nm, '透明分区', p + '.png'))
                    m = _load_rgba(os.path.join(V, nm, '材质蒙版', p + '.png'))[..., 0]
                    if not np.array_equal(part[..., :3], base[..., :3]):
                        raise HjpBuildError(f"{nm}/{p}: 分区 RGB 与原图不一致")
                    if not np.array_equal(part[..., 3], np.minimum(base[..., 3], m)):
                        raise HjpBuildError(f"{nm}/{p}: 分区 alpha != min(原图 alpha, 蒙版)")
    return base0.shape[:2]


def build_character(char_dir, out_dir, q=90, lossless=False, partitions='first'):
    rec = json.load(open(os.path.join(char_dir, '记录.json'), encoding='utf-8'))
    vid = rec['sprite']
    V = os.path.join(char_dir, '渲染贴图_V5')
    declared = [s['state'] for s in rec['states']]
    missing = [n for n in declared if not os.path.isdir(os.path.join(V, n))]
    if missing:
        raise HjpBuildError(f"{vid}: 缺少渲染目录 {missing}（拒绝静默过滤并重编号）")
    names = declared
    idmap = allocate_ids(vid, names)

    check_sources(char_dir, V, names, partitions)

    exprs, bb, mb, pari = [], [], [], []
    for (name, eid) in idmap:
        base = _load_rgba(os.path.join(char_dir, name + '.png'))
        rgb = np.ascontiguousarray(base[..., :3])
        a = base[..., 3]
        nrm = _load_rgba(os.path.join(V, name, '法线图.png'))
        dep = _load_rgba(os.path.join(V, name, '深度图.png'))
        ms = [np.array(Image.open(os.path.join(V, name, '材质蒙版', p + '.png')).convert('L'))
              for p in PARTS]

        exprs.append({'id': eid, 'name': name})
        pari.append((eid, source_id(rgb)))
        bb.append(dict(expr=eid, layer=L_COLOR, slot=0, data=enc_rgb(rgb, q, lossless),
                       codec=C_WEBP, channels=3, shift=0))
        bb.append(dict(expr=eid, layer=L_ALPHA, slot=0,
                       data=_save(a if lossless else np.where(a >= 250, 255, np.where(a <= 5, 0, a)).astype(np.uint8),
                                  'PNG', optimize=True),
                       codec=C_PNG, channels=1, shift=0))
        if lossless:
            bb.append(dict(expr=eid, layer=L_ALPHA, slot=1, data=_save(nrm[..., 3], 'PNG', optimize=True),
                           codec=C_PNG, channels=1, shift=0))
            mb.append(dict(expr=eid, layer=L_NORMAL, slot=0,
                           data=enc_rgb(np.ascontiguousarray(nrm[..., :3]), q, True),
                           codec=C_WEBP, channels=3, shift=0))
            mb.append(dict(expr=eid, layer=L_DEPTH, slot=0, data=_save(dep[..., 0], 'PNG', optimize=True),
                           codec=C_PNG, channels=1, shift=0))
        else:
            mb.append(dict(expr=eid, layer=L_NORMAL, slot=0,
                           data=_save_l(np.ascontiguousarray(nrm[..., 0]), False),
                           codec=C_WEBP, channels=1, shift=0))
            mb.append(dict(expr=eid, layer=L_NORMAL, slot=1,
                           data=_save_l(np.ascontiguousarray(nrm[..., 1]), False),
                           codec=C_WEBP, channels=1, shift=0))
            mb.append(dict(expr=eid, layer=L_DEPTH, slot=0,
                           data=_save_l(np.ascontiguousarray(dep[..., 0]), False),
                           codec=C_WEBP, channels=1, shift=0))
        mb.append(dict(expr=eid, layer=L_MASK, slot=0, data=_save(np.dstack(ms[:4]), 'PNG', optimize=True),
                       codec=C_PNG, channels=4, shift=0))
        mb.append(dict(expr=eid, layer=L_MASK, slot=1, data=enc_la(ms[4], ms[5]),
                       codec=C_PNG, channels=2, shift=0))

    h, w = base.shape[:2]
    common = dict(character=vid, name=rec.get('name', vid), origin=rec.get('origin', ''),
                  canvas=[w, h], materials=PARTS, expressions=exprs,
                  profile='lossless' if lossless else 'compact',
                  producer={'tool': 'hjp-pack', 'version': '2.0.0'},
                  conventions={'colorSpace': 'sRGB', 'normalSpace': 'tangent',
                               'normalY': 'up', 'normalEncoding': 'unsigned255',
                               'depthRange': 'normalized', 'depthPolarity': 'nearWhite',
                               'alphaMode': 'straight', 'partitionFormula': 'min(colorA, mask)'})
    mb_meta = dict(common, stage=0, role='base', priority=100, replace=False, revision=1, requires=None)
    mm_meta = dict(common, stage=1, role='material', priority=100, replace=False, revision=1,
                   requires={'stage': 0})

    os.makedirs(out_dir, exist_ok=True)
    pb = os.path.join(out_dir, vid + '.base.hjp')
    pm = os.path.join(out_dir, vid + '.material.hjp')
    sb = write_hjp(pb, mb_meta, bb, pari)
    sm = write_hjp(pm, mm_meta, mb, pari)
    print(f"  {vid:22s} n={len(names):2d}  base={sb/1048576:7.2f}MiB  material={sm/1048576:7.2f}MiB",
          flush=True)
    return dict(sprite=vid, n=len(names), base=sb, material=sm)


def main():
    root = r"C:\Users\Administrator\Desktop\成品"
    argv = sys.argv[1:]
    if argv and argv[0] == 'dump':
        h = Hjp(argv[1])
        out = h.dump()
        text = json.dumps(out, ensure_ascii=False, indent=1)
        if '--json' in argv:
            p = argv[argv.index('--json') + 1]
            open(p, 'w', encoding='utf-8').write(text)
            print(f"JSON → {p}")
        else:
            print(text)
        return
    if argv and argv[0] == 'check':
        h = Hjp(argv[1])
        print(f"OK  {argv[1]}  blocks={len(h.blocks)} chunks={list(h.chunks)} pari={len(h.pari)}")
        return

    if argv and argv[0] == 'build':
        argv = argv[1:]
    lossless = '--lossless' in argv
    out = None
    if '--out' in argv:
        out = argv[argv.index('--out') + 1]
        i = argv.index('--out')
        argv = argv[:i] + argv[i + 2:]
    args = [a for a in argv if not a.startswith('--')]
    which = args[0] if args else 'all'
    out = out or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'build_lossless' if lossless else 'build')
    chars = sorted(d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)))
    if which != 'all':
        chars = [c for c in chars if c.startswith(which)]
    rows = [build_character(os.path.join(root, c), out, lossless=lossless) for c in chars]
    tb = sum(r['base'] for r in rows); tm = sum(r['material'] for r in rows)
    print(f"\nTOTAL base={tb/1048576:.2f}MiB material={tm/1048576:.2f}MiB "
          f"sum={(tb+tm)/1048576:.2f}MiB ({len(rows)} 角色) "
          f"profile={'lossless' if lossless else 'compact'}")
    json.dump(rows, open(os.path.join(out, '_build.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
