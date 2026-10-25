#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""krkr2renpy.py -- Kirikiri/KAG (.ks) -> Ren'Py 7.3.2 (.rpy) 半自动转换器。

设计原则
--------
1. 规则外置：所有标签映射、内存规则、转义表、资源布局都在 rules.json。
2. 不猜测：无法可靠转换的一律输出 ``# TODO`` 并写入报告，绝不静默丢弃。
3. 行号可追溯：每一行输出都带 ``# L####`` 注释，指向 .ks 原始行号。
4. 可运行：生成最小 screens/options/runtime，保证 Ren'Py 能直接启动。

用法
----
GUI:   python krkr2renpy.py
CLI:   python krkr2renpy.py --input <游戏目录> --output <输出目录> [--dry-run]
       python krkr2renpy.py --input 游戏解包 --dry-run --only A000.ks
"""

from __future__ import unicode_literals

import argparse
import codecs
import collections
import csv
import datetime
import io
import json
import os
import re
import shutil
import sys
import threading
import traceback

try:
    import queue
except ImportError:  # pragma: no cover - py2 fallback, not required
    import Queue as queue

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText
    HAS_TK = True
except Exception:  # pragma: no cover - headless machines
    HAS_TK = False

VERSION = "2.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RULES = os.path.join(HERE, "rules.json")
ENTRY = "entry"

LABEL_RE = re.compile(r"^\*([^\s|]+)\s*(?:\|(.*))?$")
TAG_HEAD_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)(.*)$", re.S)
ATTR_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)'|([^\s\]]*))")
PY_KEYWORDS = set(
    "and as assert break class continue def del elif else except exec finally for from global "
    "if import in is lambda not or pass print raise return try while with yield".split())


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def cfg(rules, path, default=None):
    """按 a.b.c 读取嵌套 dict；缺失时返回 default。"""
    cur = rules
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt_sec(value):
    """0.2 而不是 0.200；保留 3 位有效小数。"""
    text = ("%.3f" % float(value)).rstrip("0").rstrip(".")
    return text or "0"


def ensure_dir(path):
    if path and not os.path.isdir(path):
        os.makedirs(path)


def write_text(path, text):
    """UTF-8 + LF；Ren'Py 对两者都友好，统一 LF 便于 diff。"""
    ensure_dir(os.path.dirname(path))
    with io.open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def read_json(path):
    with io.open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_rules(path):
    rules = read_json(path)
    if not isinstance(rules.get("tags"), dict):
        raise ValueError("rules.json 缺少 tags 段")
    return rules


def has_expr(value):
    return bool(value) and bool(re.search(r"[&%]", value))


def sanitize_ident(name, lower=False):
    out = re.sub(r"[^A-Za-z0-9_]", "_", name or "")
    out = re.sub(r"_+", "_", out).strip("_")
    if not out:
        out = "x"
    if out[0].isdigit():
        out = "n_" + out
    return out.lower() if lower else out


# --------------------------------------------------------------------------
# 编码 / 词法
# --------------------------------------------------------------------------

# 中文汉化补丁以 GBK/GB18030 为主，日本原版以 CP932 为主。
# 默认顺序（rules.input.legacy_encodings）：gb18030 -> gbk -> cp932。
# gb18030 是 GBK 的超集，放最前保证中文补丁优先命中、不乱码。
DEFAULT_LEGACY_ORDER = ["gb18030", "gbk", "cp932"]

KANA_RE = re.compile(u"[\u3041-\u3096\u30a1-\u30fa]")
CJK_RE = re.compile(u"[\u4e00-\u9fff]")


def legacy_order(rules):
    order = cfg(rules, "input.legacy_encodings", DEFAULT_LEGACY_ORDER)
    return [str(x).lower() for x in order] or list(DEFAULT_LEGACY_ORDER)


def _decode_legacy(raw, order):
    """返回 [(encoding, text)]，只保留解码成功的候选（严格模式）。"""
    out = []
    for enc in order:
        try:
            out.append((enc, raw.decode(enc)))
        except (UnicodeDecodeError, LookupError):
            continue
    return out


def decode_bytes(raw, rules):
    """按 BOM -> GB18030/GBK/CP932 的顺序判定编码，返回 (text, encoding_name)。

    关键点：
      1. 先试纯 ASCII / 严格 UTF-8，这两类不会被 GB 系误判；
      2. 其余按 rules.input.legacy_encodings 顺序（gb18030 最前）严格解码；
      3. kana guard：若 GB 系解出「无假名」而 CP932 解出「有假名」，说明是日文原本，
         改用 CP932（可用 rules.input.japanese_kana_guard 关闭）。
    """
    if raw.startswith(codecs.BOM_UTF16_LE):
        return raw[2:].decode("utf-16-le", "replace"), "utf-16-le-bom"
    if raw.startswith(codecs.BOM_UTF16_BE):
        return raw[2:].decode("utf-16-be", "replace"), "utf-16-be-bom"
    if raw.startswith(codecs.BOM_UTF8):
        return raw[3:].decode("utf-8", "replace"), "utf-8-bom"

    # 纯 ASCII：任何编码结果都一样
    try:
        return raw.decode("ascii"), "utf-8"
    except UnicodeDecodeError:
        pass

    # 严格 UTF-8：能解且不是「纯拉丁」噪声，就用 UTF-8
    try:
        text = raw.decode("utf-8")
        if CJK_RE.search(text) or not KANA_RE.search(text):
            return text, "utf-8"
    except UnicodeDecodeError:
        pass

    order = legacy_order(rules)
    candidates = _decode_legacy(raw, order)
    if not candidates:
        return raw.decode("gb18030", "replace"), "gb18030(replace)"

    decoded = dict(candidates)
    best_enc, best_text = candidates[0]
    if cfg(rules, "input.japanese_kana_guard", True) and "cp932" in decoded:
        cjk_text = None
        for enc in order:
            if enc in ("gb18030", "gbk") and enc in decoded:
                cjk_text = decoded[enc]
                break
        if cjk_text is not None:
            jp_text = decoded["cp932"]
            if not KANA_RE.search(cjk_text) and KANA_RE.search(jp_text):
                return jp_text, "cp932"
    return best_text, best_enc


def scan_tag(line, start):
    """从 line[start] == '[' 起读取一个标签，返回 (end_index, inner) 或 (None, None)。

    引号内的 ']' 不算结束（例如 exp="a[0] == 1"）。
    """
    n = len(line)
    j = start + 1
    quote = None
    while j < n:
        c = line[j]
        if quote:
            if c == "\\":
                j += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "]":
            return j + 1, line[start + 1:j]
        elif c == "[":
            return None, None
        j += 1
    return None, None


def parse_segments(line):
    """把一行拆成 [('text', s), ('tag', inner), ...]，保持原始顺序。"""
    segs = []
    buf = []
    i = 0
    n = len(line)
    while i < n:
        if line[i] == "[":
            end, inner = scan_tag(line, i)
            if end is not None and TAG_HEAD_RE.match(inner):
                if buf:
                    segs.append(("text", "".join(buf)))
                    buf = []
                segs.append(("tag", inner))
                i = end
                continue
        buf.append(line[i])
        i += 1
    if buf:
        segs.append(("text", "".join(buf)))
    return segs


def split_tag(inner):
    """'bgi storage=x time=1' -> ('bgi', {'storage': 'x', 'time': '1'})"""
    m = TAG_HEAD_RE.match(inner)
    if not m:
        return None, {}
    name = m.group(1)
    attrs = {}
    for a in ATTR_RE.finditer(m.group(2)):
        v = a.group(2)
        if v is None:
            v = a.group(3)
        if v is None:
            v = a.group(4)
        attrs[a.group(1)] = v
    return name, attrs


def escape_text(text, rules):
    """按 rules.text.escape_order 逐字符替换，顺序固定（反斜杠最先）。"""
    table = cfg(rules, "text.escape", {})
    out = text
    for ch in cfg(rules, "text.escape_order", ["\\", "{", "}", "[", "\""]):
        rep = table.get(ch)
        if rep is not None:
            out = out.replace(ch, rep)
    return out


class FileInfo(object):
    """一个 .ks 文件的静态信息（预扫描结果）。"""

    def __init__(self, path, name):
        self.path = path
        self.name = name
        self.stem = os.path.splitext(name)[0]
        self.encoding = ""
        self.lines = []
        self.tier = "manual"
        self.labels = []          # [(line, name, title)]
        self.label_names = set()
        self.speakers = []        # [(line, name)]
        self.tags = collections.Counter()
        self.iscript = False
        self.loop_heads = set()   # 伪循环（[jump cond] 指回自身）的标签名
        self.segment_heads = set()
        self.jump_refs = []       # [(line, tag, storage, target, has_cond)]
        self.content_lines = 0
        self.selected = True      # --only 过滤后是否参与转换
        self.from_patch = False   # 该文件是否来自 patch.xp3（汉化补丁）

    @property
    def rid(self):
        return sanitize_ident(self.stem)


# --------------------------------------------------------------------------
# 名字罗马字化（角色变量名）
# --------------------------------------------------------------------------

def romanize_name(name, rules):
    """返回 (roman, method)。method: override / kana / fallback。"""
    overrides = cfg(rules, "characters.overrides", {})
    if name in overrides:
        return overrides[name], "override"
    table = cfg(rules, "characters.kana_roman", {})
    # 片假名 -> 平假名，复用同一张表
    s = "".join(chr(ord(c) - 0x60) if 0x30A1 <= ord(c) <= 0x30F6 else c for c in name)
    out = []
    i = 0
    dbl = False
    while i < len(s):
        c = s[i]
        if c == u"っ":
            dbl = True
            i += 1
            continue
        if c == u"ー":
            i += 1
            continue
        pair = s[i:i + 2]
        rom = table.get(pair)
        step = 2 if rom is not None else 1
        if rom is None:
            rom = table.get(c)
        if rom is None:
            return None, "unmapped"
        if dbl and rom and rom[0] not in "aeiou":
            rom = rom[0] + rom
        dbl = False
        out.append(rom)
        i += step
    roman = "".join(out).lower()
    roman = re.sub(r"[^a-z0-9]", "", roman)
    return (roman or None), ("kana" if roman else "unmapped")


# --------------------------------------------------------------------------
# 转换结果
# --------------------------------------------------------------------------

class Result(object):
    def __init__(self):
        self.files = []
        self.todos = []
        self.stats = collections.Counter()
        self.memory = collections.Counter()
        self.warnings = []
        self.characters = []
        self.missing_assets = []
        self.writes = []
        self.copies = []
        self.report_md = ""
        self.report_csv = ""
        self.output_dir = ""


# --------------------------------------------------------------------------
# 主转换器
# --------------------------------------------------------------------------

class ProjectConverter(object):

    def __init__(self, rules, rules_path, source_dir, output_dir, options=None):
        self.rules = rules
        self.rules_path = rules_path
        self.source_dir = os.path.abspath(source_dir)
        self.output_dir = os.path.abspath(output_dir)
        self.options = dict(options or {})
        # 汉化补丁目录（patch.xp3 解包内容）：同名剧本/资源优先于主目录
        _pd = self.options.get("patch_dir") or ""
        self.patch_dir = os.path.abspath(_pd) if _pd else None
        self.result = Result()
        self.result.output_dir = self.output_dir

        self.infos = []
        self.by_storage = {}
        self.mem_points = collections.defaultdict(set)
        self.macro_defs = {}
        self.image_lookup = {}
        self.image_defs = []
        self.audio_lookup = {}
        self.char_map = collections.OrderedDict()
        self.char_colors = {}
        self.used_audio = collections.OrderedDict()
        self.pending_writes = []
        self.pending_copies = []
        self.progress = None
        self.log_cb = None
        self.dry_run = False

    # ---------------- 日志 ----------------

    def rule(self, path, default=None):
        return cfg(self.rules, path, default)

    def log(self, msg):
        self.result.stats["log_lines"] += 1
        if self.log_cb:
            self.log_cb(msg)

    def warn(self, msg):
        self.result.warnings.append(msg)
        self.log("[warn] " + msg)

    # ---------------- 入口 ----------------

    def run(self, progress=None, log=None, dry_run=False):
        self.progress = progress
        self.log_cb = log
        self.dry_run = dry_run
        self.log("krkr2renpy %s  source=%s  out=%s  dry_run=%s"
                 % (VERSION, self.source_dir, self.output_dir, dry_run))

        self.scan_inputs()
        self.plan_memory()
        self.build_characters()
        self.build_images()
        self.build_audio_index()
        self.convert_all()
        self.build_aux_files()
        self.write_report()

        if not dry_run:
            self.flush_outputs()
        self.log("完成：%d 个文件，%d 条 TODO，%d 行输出"
                 % (self.result.stats["files"], len(self.result.todos),
                    self.result.stats["out_lines"]))
        return self.result

    # ---------------- 扫描 ----------------

    def scan_inputs(self):
        only = self.options.get("only") or []
        only_lower = set(x.lower() for x in only)
        auto = set(x.lower() for x in self.rule("files.auto", []))
        manual = set(x.lower() for x in self.rule("files.manual", []))
        unknown = self.rule("files.unknown_tier", "manual")
        paths = []
        # 收集 .ks：先主目录，再 patch 目录；同名文件由 patch 覆盖（汉化剧本优先）。
        roots = [(self.source_dir, False)]
        if self.patch_dir and os.path.isdir(self.patch_dir):
            roots.append((self.patch_dir, True))
        by_stem = {}
        for root_dir, is_patch in roots:
            for root, _dirs, names in os.walk(root_dir):
                for n in sorted(names):
                    if n.lower().endswith(".ks"):
                        p = os.path.join(root, n)
                        by_stem[os.path.splitext(n)[0].lower()] = (p, is_patch)
        if not by_stem:
            raise ValueError("在 %s 下没有找到 .ks 文件" % self.source_dir)
        paths = [(p, is_patch) for _stem, (p, is_patch) in sorted(by_stem.items())]

        for path, is_patch in paths:
            info = FileInfo(path, os.path.basename(path))
            info.from_patch = is_patch
            with open(path, "rb") as f:
                raw = f.read()
            text, enc = decode_bytes(raw, self.rules)
            info.encoding = enc
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            lines = text.split("\n")
            if lines and lines[-1] == "":
                lines.pop()
            info.lines = lines
            info.selected = (not only_lower) or info.name.lower() in only_lower
            low = info.name.lower()
            if low in auto:
                info.tier = "auto"
            elif low in manual:
                info.tier = "manual"
            else:
                info.tier = unknown
            self._prescan(info)
            self.infos.append(info)
            self.by_storage[info.stem.lower()] = info
            if not info.selected:
                continue
            self.result.stats["files"] += 1
            self.result.stats["lines_" + info.tier] += len(lines)
            self.result.stats["files_" + info.tier] += 1

        # 宏定义（跨文件收集，宏调用时用于给出更准确的 TODO 原因）
        for info in self.infos:
            for tag_name, attrs in self._collect_tags(info):
                if tag_name == "macro" and attrs.get("name"):
                    self.macro_defs[attrs["name"]] = info.name

        self.log("扫描 %d 个文件：auto=%d，manual=%d"
                 % (len(self.infos), self.result.stats["files_auto"],
                    self.result.stats["files_manual"]))

    def _collect_tags(self, info):
        out = []
        in_script = False
        for ln in info.lines:
            s = ln.strip()
            if s.startswith("[iscript]"):
                in_script = True
                continue
            if s.startswith("[endscript]"):
                in_script = False
                continue
            if in_script or s.startswith(";") or not s:
                continue
            for kind, val in parse_segments(ln):
                if kind == "tag":
                    nm, at = split_tag(val)
                    if nm:
                        out.append((nm, at))
        return out

    def _prescan(self, info):
        cur_label = None
        label_jumps = collections.defaultdict(set)
        label_has_image = collections.defaultdict(bool)
        image_tags = set(cfg(self.rules, "memory.loops.image_tags",
                             ["fgi", "bgi", "image", "pimage", "graph", "himage"]))
        seg_patterns = [re.compile(p) for p in self.rule("memory.segments.label_patterns", [])]
        in_script = False
        for idx, ln in enumerate(info.lines):
            src = idx + 1
            s = ln.strip()
            if not s:
                continue
            if s.startswith("[iscript]"):
                in_script = True
                info.iscript = True
                continue
            if s.startswith("[endscript]"):
                in_script = False
                continue
            if in_script:
                continue
            if s.startswith(";"):
                continue
            m = LABEL_RE.match(s)
            if m:
                cur_label = m.group(1)
                info.labels.append((src, cur_label, (m.group(2) or "").strip()))
                info.label_names.add(cur_label)
                if any(p.search(cur_label) for p in seg_patterns):
                    info.segment_heads.add(cur_label)
                continue
            for kind, val in parse_segments(ln):
                if kind != "tag":
                    continue
                name, attrs = split_tag(val)
                if not name:
                    continue
                info.tags[name] += 1
                if name == "speaker" and attrs.get("name"):
                    info.speakers.append((src, attrs["name"]))
                if name in image_tags and cur_label:
                    label_has_image[cur_label] = True
                if name in ("jump", "call"):
                    storage = attrs.get("storage") or attrs.get("file")
                    target = attrs.get("target")
                    info.jump_refs.append((src, name, storage, target, bool(attrs.get("cond"))))
                    if name == "jump" and attrs.get("cond") and target and cur_label:
                        label_jumps[target.lstrip("*")].add(cur_label)
        for target, owners in label_jumps.items():
            for lbl in owners:
                if target == lbl and label_has_image.get(lbl):
                    info.loop_heads.add(lbl)
        info.content_lines = sum(1 for x in info.lines
                                 if x.strip() and not x.strip().startswith(";"))

    # ---------------- 内存插入点规划（规则 2/3/4） ----------------

    def plan_memory(self):
        if not self.rule("memory.enabled", True):
            return
        for info in self.infos:
            for (_src, tag, storage, target, _cond) in info.jump_refs:
                if tag not in ("jump", "call"):
                    continue
                if storage and not has_expr(storage):
                    dest = self.by_storage.get(os.path.splitext(storage)[0].lower())
                    if dest is None:
                        continue
                    label = (target or "").lstrip("*") or ENTRY
                    if label != ENTRY and label not in dest.label_names:
                        continue
                    self.mem_points[(dest.stem, label)].add("label")
                elif not storage and target and not has_expr(target):
                    label = target.lstrip("*")
                    if label in info.label_names:
                        self.mem_points[(info.stem, label)].add("label")
            for lbl in info.loop_heads:
                self.mem_points[(info.stem, lbl)].add("loop")
            for lbl in info.segment_heads:
                self.mem_points[(info.stem, lbl)].add("segment")
        total = sum(len(v) for v in self.mem_points.values())
        self.log("内存规则 2/3/4 目标：%d 个 label" % total)

    # ---------------- 角色 ----------------

    def build_characters(self):
        used = set()
        palette = self.rule("characters.colors", ["#ffffff"])
        prefix = self.rule("characters.prefix", "speaker_")
        fallback = self.rule("characters.fallback", "chara_%d")
        seen = collections.OrderedDict()
        for info in self.infos:
            for (_src, name) in info.speakers:
                if name not in seen:
                    seen[name] = True
        index = 0
        for name in seen:
            index += 1
            roman, method = romanize_name(name, self.rules)
            if not roman:
                roman = fallback % index
                method = "fallback"
                self.warn("角色「%s」无法罗马字化，使用 %s（请在 rules.json characters.overrides 中指定）"
                          % (name, roman))
            var = prefix + sanitize_ident(roman, lower=True)
            base = var
            n = 2
            while var in used:
                var = "%s_%d" % (base, n)
                n += 1
            used.add(var)
            color = palette[(index - 1) % len(palette)]
            self.char_map[name] = var
            self.char_colors[name] = color
            self.result.characters.append(
                {"name": name, "var": var, "color": color, "method": method})
        self.log("角色 %d 个：%s" % (len(self.char_map),
                                    ", ".join("%s=%s" % (k, v) for k, v in self.char_map.items())))

    # ---------------- 资源索引 ----------------

    def asset_root(self):
        mode = self.rule("assets.root", "auto")
        if mode == "auto":
            base = self.source_dir.rstrip("\\/")
            if os.path.basename(base).lower() == "scenario":
                return os.path.dirname(base)
            return base
        return os.path.abspath(os.path.join(HERE, mode))

    def patch_asset_root(self):
        """patch.xp3 解包目录对应的资源根目录；无 patch 时返回 None。"""
        if not self.patch_dir:
            return None
        base = self.patch_dir.rstrip("\\/")
        if os.path.basename(base).lower() == "scenario":
            return os.path.dirname(base)
        return base

    def build_images(self):
        exts = set(x.lower() for x in self.rule("assets.extensions", [".png", ".jpg"]))
        names = set()
        # patch 先扫（同名图片优先用补丁版），主目录后扫补齐其余资源
        bases = []
        p = self.patch_asset_root()
        if p:
            bases.append(p)
        bases.append(self.asset_root())
        for base in bases:
            is_patch = (base != self.asset_root())
            for spec in self.rule("assets.image_dirs", []):
                src_dir = os.path.join(base, spec["dir"])
                if not os.path.isdir(src_dir):
                    if not is_patch:
                        self.warn("图片目录不存在：%s" % src_dir)
                    continue
                tag = spec["tag"]
                target = spec["target"]
                for fname in sorted(os.listdir(src_dir)):
                    stem, ext = os.path.splitext(fname)
                    if ext.lower() not in exts:
                        continue
                    key = (tag, stem.lower())
                    if key in self.image_lookup:
                        continue
                    name = "%s %s" % (tag, sanitize_ident(stem, lower=True))
                    if name in names:
                        n = 2
                        while "%s_%d" % (name, n) in names:
                            n += 1
                        name = "%s_%d" % (name, n)
                    names.add(name)
                    rel = "%s/%s" % (target, fname)
                    entry = {"name": name, "rel": rel, "src": os.path.join(src_dir, fname)}
                    self.image_lookup[key] = entry
                    self.image_defs.append(entry)
                    self.pending_copies.append((entry["src"], rel))
        self.log("图片定义 %d 条" % len(self.image_defs))

    def build_audio_index(self):
        exts = set(x.lower() for x in self.rule("assets.audio_extensions", [".ogg", ".wav", ".mp3"]))
        # patch 先扫（同名音频优先用补丁版），主目录后扫补齐其余资源
        bases = []
        p = self.patch_asset_root()
        if p:
            bases.append(p)
        bases.append(self.asset_root())
        for base in bases:
            for spec in self.rule("assets.audio_dirs", []):
                src_dir = os.path.join(base, spec["dir"])
                if not os.path.isdir(src_dir):
                    continue
                kind = spec["kind"]
                target = spec["target"]
                for fname in sorted(os.listdir(src_dir)):
                    stem, ext = os.path.splitext(fname)
                    if ext.lower() not in exts:
                        continue
                    key = (kind, stem.lower())
                    if key in self.audio_lookup:
                        continue
                    self.audio_lookup[key] = {
                        "rel": "%s/%s" % (target, fname),
                        "src": os.path.join(src_dir, fname),
                    }
        self.log("音频索引 %d 条" % len(self.audio_lookup))

    def resolve_image(self, storage, kind):
        if not storage:
            return None
        stem = os.path.splitext(storage.strip())[0].lower()
        for tag in self.rule("assets.lookup." + kind, []):
            hit = self.image_lookup.get((tag, stem.lower()))
            if hit:
                return hit
        return None

    def resolve_audio(self, kind, stem):
        if not stem:
            return None
        return self.audio_lookup.get((kind, os.path.splitext(stem.strip())[0].lower()))

    def use_audio(self, kind, stem, ext_pref=None):
        hit = self.resolve_audio(kind, stem)
        if hit:
            self.pending_copies.append((hit["src"], hit["rel"]))
            return hit
        return None

    # ---------------- 转换主体 ----------------

    def convert_all(self):
        targets = [i for i in self.infos if i.selected]
        total = len(targets)
        for idx, info in enumerate(targets):
            try:
                if info.tier == "auto":
                    self.convert_file(info)
                else:
                    self.make_manual_stub(info)
            except Exception as exc:  # 单文件失败不拖垮整批
                self.warn("转换 %s 失败：%s" % (info.name, exc))
                self.log(traceback.format_exc())
                self.emit_blocked_file(info, "内部错误：%s" % exc)
            if self.progress:
                self.progress(idx + 1, total, info.name)

    def emit_blocked_file(self, info, reason):
        lines = self.header_lines(info)
        lines.append("label %s:" % self.full_label(info, ENTRY))
        lines.append("    $ kr2r_todo(%r, 1, %r)" % (info.name, reason))
        lines.append("    return")
        self.pending_writes.append(("game/script/%s.rpy" % info.rid, "\n".join(lines)))
        self.result.stats["out_lines"] += len(lines)

    def header_lines(self, info):
        fmt = self.rule("output.header", ["# 源文件: %NAME%", "# 编码: %ENC%", "# 档位: %TIER%"])
        out = []
        for line in fmt:
            out.append(line.replace("%NAME%", info.name)
                           .replace("%ENC%", info.encoding)
                           .replace("%TIER%", info.tier))
        return out

    def make_manual_stub(self, info):
        """人工档：生成可跳转的入口占位 + 原文逐行注释存档。"""
        lines = self.header_lines(info)
        lines.append("#")
        lines.append("# 该文件被判定为人工档（系统菜单 / TJS / 宏定义），未自动转换。")
        lines.append("# 原文见 manual_raw/%s.txt；请手工移植。L0001" % info.stem)
        lines.append("")
        lines.append("label %s:" % self.full_label(info, ENTRY))
        lines.append("    # TODO L0001 人工档：%s（%d 行）未自动转换"
                     % (info.name, len(info.lines)))
        lines.append("    $ kr2r_todo(%r, 1, %r)"
                     % (info.name, "manual-tier: 需要人工移植"))
        lines.append("    return")
        self.pending_writes.append(("game/script/%s.rpy" % info.rid, "\n".join(lines)))
        self.result.stats["out_lines"] += len(lines)

        raw = []
        for i, ln in enumerate(info.lines, 1):
            raw.append("# L%04d  %s" % (i, ln))
        self.pending_writes.append(("manual_raw/%s.ks.txt" % info.stem, "\n".join(raw)))
        self.result.todos.append({
            "file": info.name, "line": 1, "tier": "manual", "category": "manual-file",
            "reason": "整文件人工档（%d 行），已生成入口占位与原文注释" % len(info.lines),
            "original": "",
        })

    # ---- 单文件转换 ----

    def convert_file(self, info):
        self.cur = info
        self.out = []
        self.depth = 0
        self.if_stack = []
        self.pending_speaker = None
        self.pending_voice = None
        self.pending_ruby = None
        self.run = []
        self.active_fg = collections.OrderedDict()
        self.active_fg_img = {}
        self.last_scene = None
        self.last_scene_trans = None
        self.last_scene_line = -9999
        self.last_label_line = -9999
        self.last_label = None
        self.scene_seen = 0
        self.file_todos = 0

        for line in self.header_lines(info):
            self.out.append(line)
        self.out.append("")

        self.emit_label(ENTRY, None)

        i = 0
        n = len(info.lines)
        while i < n:
            src = i + 1
            ln = info.lines[i]
            s = ln.strip()
            if not s:
                self.blank()
                i += 1
                continue
            if s.startswith(";"):
                self.comment(s[1:].strip(), src)
                i += 1
                continue
            if s.startswith("[iscript]") or s.startswith("@iscript"):
                end = self.find_block_end(info, i, ["iscript"], ["endscript"])
                self.todo_block(info, i, end, "TJS 脚本块（[iscript]）无法转换", "tjs")
                i = end + 1
                continue
            head = self.first_tag(ln)
            if head and head[0] == "macro":
                end = self.find_block_end(info, i, ["macro"], ["endmacro"])
                self.todo_block(info, i, end, "KAG 宏定义（%s）需人工展开"
                                % (head[1].get("name") or "?"), "macro")
                i = end + 1
                continue
            if head and head[0] == "if":
                ok, _py, why = self.check_if(head[1], len(self.if_stack))
                if not ok:
                    end = self.find_block_end(info, i, ["if"], ["endif"])
                    self.todo_block(info, i, end, "条件无法可靠转换：%s（%s）" % (why, head[1].get("exp", "")),
                                    "control-flow")
                    i = end + 1
                    continue
            if s.startswith("@"):
                self.handle_at(s, src)
                i += 1
                continue
            m = LABEL_RE.match(s)
            if m:
                self.flush_run(src)
                self.close_open_blocks(src)
                self.emit_label(m.group(1), src, m.group(2) or "")
                i += 1
                continue
            self.handle_line(ln, src)
            i += 1

        self.flush_run(n)
        self.close_open_blocks(n)
        if self.if_stack:
            self.warn("%s：文件结束时 if 块未闭合，已自动补 pass" % info.name)
        self.append_logo_handoff(info)
        self.result.stats["out_lines"] += len(self.out)
        rel = "game/script/%s.rpy" % info.rid
        self.pending_writes.append((rel, "\n".join(self.out)))

    def append_logo_handoff(self, info):
        """logo（片头）播完后衔接：默认 return 进入主菜单（label main_menu），
        主菜单里再 jump start 进入正篇；也可配 output.logo_jump_target=start 直接进正篇。
        """
        if not self.rule("output.logo_jump_start", True):
            return
        logo_stem = self.rule("output.splash_target", "logo.ks")
        logo_stem = os.path.splitext(os.path.basename(logo_stem))[0].lower()
        if info.stem.lower() != logo_stem:
            return
        last = ""
        for line in reversed(self.out):
            if line.strip():
                last = line.strip()
                break
        if last.startswith("jump ") or last.startswith("call ") or last.startswith("return"):
            return
        target = self.rule("output.logo_jump_target", "main_menu")
        self.out.append("")
        if target == "start":
            self.out.append("    # 片头结束：直接进入正篇（output.logo_jump_target=start）")
            self.out.append("    jump start")
        else:
            self.out.append("    # 片头结束：返回后进入主菜单（label main_menu）")
            self.out.append("    return")
        self.result.stats["logo_handoff"] += 1

    # ---- 块级跳过 ----

    def first_tag(self, ln):
        for kind, val in parse_segments(ln):
            if kind == "tag":
                nm, at = split_tag(val)
                if nm:
                    return (nm, at)
            else:
                return None
        return None

    def find_block_end(self, info, start, open_names, close_names):
        depth = 0
        j = start
        while j < len(info.lines):
            for kind, val in parse_segments(info.lines[j]):
                if kind != "tag":
                    continue
                nm, _at = split_tag(val)
                if nm in open_names:
                    depth += 1
                elif nm in close_names:
                    depth -= 1
                    if depth <= 0:
                        return j
            j += 1
        return len(info.lines) - 1

    def todo_block(self, info, start, end, reason, category):
        src = start + 1
        raw = info.lines[start].strip()
        self.todo(reason, src, raw, category, count=end - start + 1)
        for j in range(start + 1, end + 1):
            self.comment(info.lines[j].strip(), j + 1)

    # ---- 输出原语 ----

    def line_suffix(self, src):
        if src is None:
            return ""
        fmt = self.rule("output.line_comment_format", "  # L%04d")
        try:
            return fmt % int(src)
        except Exception:
            return "  # L%d" % src

    def emit(self, text, src=None, level=None):
        if level is None:
            level = self.depth
        if self.suppress():
            self.comment(text, src)
            return
        self.out.append("    " * level + text + self.line_suffix(src))

    def emit_extra(self, text, extra, src=None, level=None):
        if level is None:
            level = self.depth
        suffix = ""
        fmt = self.rule("output.line_comment_format", "  # L%04d")
        if src:
            suffix = fmt % int(src)
        if extra:
            suffix = "%s %s" % (suffix, extra) if suffix else "  # %s" % extra
        if self.suppress():
            self.comment(text, src)
            return
        self.out.append("    " * level + text + suffix)

    def blank(self):
        self.out.append("")

    def comment(self, text, src=None, level=0):
        text = (text or "").replace("\n", " ")
        if src is not None:
            fmt = self.rule("output.comment_line_format", "# L%04d  ")
            self.out.append(("    " * level) + (fmt % int(src)) + text)
        else:
            self.out.append(("    " * level) + "# " + text)

    def todo(self, reason, src, raw, category="unknown", count=1):
        self.file_todos += 1
        self.result.todos.append({
            "file": self.cur.name, "line": src, "tier": self.cur.tier,
            "category": category, "reason": reason, "original": (raw or "")[:400],
        })
        self.result.stats["todos"] += count
        fmt = self.rule("output.comment_line_format", "# L%04d  ")
        head = (fmt % int(src)) if src else "# "
        if raw:
            self.out.append("    " * self.depth + head + "TODO [%s] %s | 原文: %s"
                            % (category, reason, raw.replace("\n", " ")))
        else:
            self.out.append("    " * self.depth + head + "TODO [%s] %s" % (category, reason))

    def suppress(self):
        return self.depth < 0

    # ---- 标签/表达式 ----

    def full_label(self, info, label):
        prefix = self.rule("labels.prefix", "%STEM%_")
        return sanitize_ident(prefix.replace("%STEM%", info.rid) + label)

    def emit_label(self, label, src, title=""):
        info = self.cur
        name = self.full_label(info, label)
        if title:
            self.comment("章节标题：%s" % title, src, level=0)
        self.emit("label %s:" % name, src, level=0)
        self.depth = 1
        self.last_label = label
        self.last_label_line = src
        key = (info.stem, label)
        if key in self.mem_points and self.rule("memory.after_jump_call", True):
            reasons = self.mem_points[key]
            reason = "loop" if "loop" in reasons else ("segment" if "segment" in reasons else "label")
            self.emit_memory(reason, src)

    def check_if(self, attrs, depth):
        max_depth = self.rule("control.max_if_depth", 2)
        if depth >= max_depth:
            return False, None, "嵌套深度超过 %d" % max_depth
        py, ok, why = self.translate_expr(attrs.get("exp", ""))
        return ok, py, why

    def translate_expr(self, exp):
        """TJS 简单表达式 -> Python；保守拒绝一切不确定写法。"""
        if not exp or not exp.strip():
            return None, False, "缺少 exp"
        spec = self.rule("control.expression", {})
        s = exp.strip().rstrip(";")
        parts = re.split(r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')", s)
        mapping = spec.get("operators", [])
        for idx in range(0, len(parts), 2):
            seg = parts[idx]
            for old, new in mapping:
                if old == "!":
                    seg = re.sub(r"!(?!=)", " not ", seg)
                else:
                    seg = seg.replace(old, new)
            parts[idx] = seg
        s = "".join(parts)
        banned = spec.get("banned", ["[", "]", "{", "}", "(", ")", "?", ":", "%", "$", "+", "*", "/"])
        for ch in banned:
            if ch in s:
                return None, False, "含不支持的结构 %r" % ch
        if re.search(r"(?<![=!<>])=(?!=)", s):
            return None, False, "含赋值号"
        for prefix, repl in spec.get("var_prefixes", {}).items():
            if prefix in s:
                s = s.replace(prefix, repl)
        left = re.sub(r"persistent\.[A-Za-z_][A-Za-z0-9_]*", "", s)
        left = re.sub(r"\b(?:and|or|not|None|True|False|is)\b", "", left)
        for ident in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", left):
            if ident in PY_KEYWORDS:
                return None, False, "含 Python 关键字 %s" % ident
            if ident in spec.get("rejected_identifiers", []):
                return None, False, "含 TJS 对象 %s" % ident
        if re.search(r"[^A-Za-z0-9_\s\.\"'<>=!]", s):
            return None, False, "含未知符号"
        return s, True, ""

    # ---- 行处理 ----

    def handle_line(self, ln, src):
        for kind, val in parse_segments(ln):
            if kind == "text":
                self.add_text(val, src)
            else:
                name, attrs = split_tag(val)
                if not name:
                    self.add_text("[" + val + "]", src)
                else:
                    self.handle_tag(name, attrs, val, src)

    def add_text(self, text, src):
        if not text:
            return
        esc = escape_text(text, self.rules)
        if self.pending_ruby:
            tmpl = self.rule("text.ruby_template", "{rb}%CH%{/rb}{rt}%RT%{/rt}")
            rt = escape_text(self.pending_ruby, self.rules)
            for idx, ch in enumerate(esc):
                if ch.strip():
                    esc = esc[:idx] + tmpl.replace("%CH%", ch).replace("%RT%", rt) + esc[idx + 1:]
                    break
            self.pending_ruby = None
        self.run.append({"text": esc, "src": src})

    def flush_run(self, src):
        if not self.run:
            return
        pieces = self.run
        self.run = []
        body = "".join(p["text"] for p in pieces).strip()
        if not body:
            return
        who = self.pending_speaker
        voice = self.pending_voice
        self.pending_speaker = None
        self.pending_voice = None
        if voice:
            hit = self.use_audio("sound", voice)
            if hit:
                self.emit('voice "%s"' % hit["rel"], src)
            else:
                self.todo("语音文件缺失：%s" % voice, src, "", "asset-missing")
        if who and who in self.char_map:
            self.emit('%s "%s"' % (self.char_map[who], body), src)
        elif who:
            self.emit('"%s"' % body, src)
        else:
            self.emit('"%s"' % body, src)

    # ---- 动作 ----

    def handle_at(self, s, src):
        inner = s[1:].strip()
        if not inner:
            return
        name, attrs = split_tag(inner)
        if not name:
            self.todo("未知 @ 命令", src, s, "at-command")
            return
        self.handle_tag(name, attrs, inner, src, from_at=True)

    def handle_tag(self, name, attrs, raw, src, from_at=False):
        spec = self.rule("tags." + name)
        if spec is None:
            reason = "未知标签"
            if name in self.macro_defs:
                reason = "KAG 宏调用（定义于 %s），需人工展开" % self.macro_defs[name]
            self.todo(reason, src, raw, "unknown-tag")
            return
        action = spec.get("action", "todo")
        handler = getattr(self, "_act_" + action, None) if action else None
        if handler is None:
            self.todo(spec.get("reason", "未实现的动作 %s" % action), src, raw, "unsupported")
            return
        handler(name, attrs, spec, raw, src, from_at)

    # 各类动作实现（签名统一：name, attrs, spec, raw, src, from_at）

    def _act_say_end(self, name, attrs, spec, raw, src, from_at):
        self.flush_run(src)

    def _act_text_newline(self, name, attrs, spec, raw, src, from_at):
        if self.run:
            self.run.append({"text": spec.get("text", "\\n"), "src": src})
        else:
            self.comment("[%s] 空行" % name, src)

    def _act_text_click(self, name, attrs, spec, raw, src, from_at):
        if self.run:
            self.run.append({"text": spec.get("text", "\\n{w}"), "src": src})
        else:
            self.emit("pause", src)

    def _act_pause(self, name, attrs, spec, raw, src, from_at):
        self.flush_run(src)
        t = attrs.get("time")
        if t is None:
            self.emit("pause", src)
            return
        secs = self._ms_to_sec(t)
        if secs is None:
            self.todo("wait 的 time 无法解析：%s" % t, src, raw, "param")
            return
        self.emit("pause %s" % fmt_sec(secs), src)

    def _act_pause_click(self, name, attrs, spec, raw, src, from_at):
        self.flush_run(src)
        self.emit("pause", src)

    def _act_speaker(self, name, attrs, spec, raw, src, from_at):
        if self.run:
            self.flush_run(src)
        who = attrs.get("name")
        if who is None:
            self.todo("speaker 缺少 name", src, raw, "param")
            return
        if self.pending_speaker and self.pending_speaker != who:
            self.warn("%s:%d speaker「%s」未被使用即被「%s」覆盖"
                      % (self.cur.name, src, self.pending_speaker, who))
        if who not in self.char_map:
            self.warn("%s:%d 未在角色表中登记的说话人「%s」" % (self.cur.name, src, who))
        self.pending_speaker = who

    def _act_voice(self, name, attrs, spec, raw, src, from_at):
        v = attrs.get("file")
        if not v or has_expr(v):
            self.todo("voice 文件名为空或为表达式", src, raw, "param")
            return
        self.pending_voice = os.path.splitext(v)[0]

    def _act_ruby(self, name, attrs, spec, raw, src, from_at):
        text = attrs.get("text")
        if not text:
            self.todo("ruby 缺少 text", src, raw, "param")
            return
        self.pending_ruby = text

    def _act_inline_image(self, name, attrs, spec, raw, src, from_at):
        stem = spec.get("asset") or attrs.get("storage")
        hit = self.resolve_image(stem, spec.get("kind", "ui"))
        if not hit:
            self.todo("内联图像资源缺失：%s" % stem, src, raw, "asset-missing")
            return
        self.run.append({"text": "{image=%s}" % hit["rel"], "src": src})

    def _ms_to_sec(self, value):
        try:
            return int(str(value).strip().strip('"')) / 1000.0
        except (TypeError, ValueError):
            return None

    def transition_expr(self, attrs, src):
        rule_name = attrs.get("rule")
        method = attrs.get("method")
        secs = self._ms_to_sec(attrs.get("time", ""))
        if secs is None:
            secs = self.rule("text.default_transition_time", 0.5)
        if rule_name and rule_name not in ("crossfade", "dissolve"):
            img = self.resolve_image(rule_name, "rule")
            if img:
                self.pending_copies.append((img["src"], img["rel"]))
                return 'ImageDissolve("%s", %s, 8)' % (img["rel"], fmt_sec(secs))
            self.warn("%s:%d 转场规则图缺失：%s，退回 Dissolve" % (self.cur.name, src, rule_name))
        if secs <= 0:
            return None
        return "Dissolve(%s)" % fmt_sec(secs)

    def _act_scene(self, name, attrs, spec, raw, src, from_at):
        self.flush_run(src)
        storage = attrs.get("storage")
        if not storage or has_expr(storage):
            self.todo("背景 storage 为空或为表达式", src, raw, "asset-dynamic")
            return
        hit = self.resolve_image(storage, spec.get("lookup", "bgi"))
        if not hit:
            self.todo("背景资源缺失：%s" % storage, src, raw, "asset-missing")
            self.result.missing_assets.append("bg:%s (%s:%d)" % (storage, self.cur.name, src))
            return
        self.pending_copies.append((hit["src"], hit["rel"]))
        trans = self.transition_expr(attrs, src)
        if self.rule("memory.scene.dedupe_same_storage", True) and \
                storage == self.last_scene and trans == self.last_scene_trans and \
                "msgc" not in attrs:
            self.comment("[bgi storage=%s] 与上一场景完全相同，已跳过" % storage, src)
            return
        if "msgc" in attrs:
            self.emit("window hide", src)
        stmt = "scene %s" % hit["name"]
        if trans:
            stmt += " with " + trans
        self.emit(stmt, src)
        # 直接 scene 会清空 master 层，因此把仍在前台的立绘重新 show 回来。
        self.reshow_sprites(src)
        self.last_scene = storage
        self.last_scene_trans = trans
        self.last_scene_line = src
        self.scene_seen += 1
        self.memory_scene(attrs, src)

    def reshow_sprites(self, src):
        """scene 之后恢复仍在台上的立绘（KAG bgi 只换背景，不撤立绘）。"""
        for layer, pos in list(self.active_fg.items()):
            entry = self.active_fg_img.get(layer)
            if not entry:
                continue
            self.emit("show %s as fg%d at pos_%s" % (entry, layer, pos), src)

    def _act_sprite_show(self, name, attrs, spec, raw, src, from_at):
        self.flush_run(src)
        storage = attrs.get("storage")
        if not storage or has_expr(storage):
            self.todo("立绘 storage 为空或为表达式", src, raw, "asset-dynamic")
            return
        hit = self.resolve_image(storage, spec.get("lookup", "fgi"))
        if not hit:
            self.todo("立绘资源缺失：%s" % storage, src, raw, "asset-missing")
            return
        self.pending_copies.append((hit["src"], hit["rel"]))
        pos = attrs.get("pos") or "center"
        layer = self.rule("sprites.postolayer", {}).get(pos)
        if layer is None:
            layer = self.rule("sprites.default_layer", 2)
            if pos not in self.rule("sprites.positions", {}):
                self.todo("未知立绘位置 pos=%s（按 center 处理）" % pos, src, raw, "param")
        var = "fg%d" % layer
        self.emit("show %s as %s at pos_%s" % (hit["name"], var, pos), src)
        self.active_fg[layer] = pos
        self.active_fg_img[layer] = hit["name"]

    def _act_sprite_hide(self, name, attrs, spec, raw, src, from_at):
        self.flush_run(src)
        pos = attrs.get("pos")
        if pos:
            layer = self.rule("sprites.postolayer", {}).get(pos)
            layers = [layer] if layer is not None else list(self.active_fg.keys())
        else:
            layers = list(self.active_fg.keys())
        if not layers:
            self.comment("[%s] 无已记录立绘，跳过" % name, src)
            return
        for layer in sorted(layers):
            self.emit("hide fg%d" % layer, src)
            self.active_fg.pop(layer, None)

    def _act_window_hide(self, name, attrs, spec, raw, src, from_at):
        self.flush_run(src)
        self.emit("window hide", src)

    def _act_window_show(self, name, attrs, spec, raw, src, from_at):
        self.emit("window show", src)

    def _act_audio_play(self, name, attrs, spec, raw, src, from_at):
        self.flush_run(src)
        storage = attrs.get("storage")
        if not storage or has_expr(storage):
            self.todo("音频 storage 为空或为表达式", src, raw, "asset-dynamic")
            return
        kind = spec.get("kind", "bgm")
        hit = self.use_audio(kind, storage)
        if not hit:
            self.todo("音频资源缺失：%s" % storage, src, raw, "asset-missing")
            return
        channel = spec.get("channel", "music")
        # SE 支持 buf 参数指定通道（与 stopse 对称）：playse buf=2 -> se2，
        # 避免所有音效都挤在 se0 上互相打断。
        buf = attrs.get("buf")
        if buf is not None and channel.startswith("se"):
            try:
                channel = "se%d" % int(buf)
            except ValueError:
                self.todo("无法解析 buf=%s" % buf, src, raw, "param")
                return
        fade = self._ms_to_sec(attrs.get("time", ""))
        stmt = 'play %s "%s"' % (channel, hit["rel"])
        if fade:
            stmt += " fadein %s" % fmt_sec(fade)
        if attrs.get("loop") in ("true", "1"):
            stmt += " loop"
        self.emit(stmt, src)

    def _act_audio_stop(self, name, attrs, spec, raw, src, from_at):
        self.flush_run(src)
        channel = spec.get("channel", "music")
        buf = attrs.get("buf")
        if spec.get("kind") == "se" and buf is not None:
            try:
                channel = "se%d" % int(buf)
            except ValueError:
                self.todo("无法解析 buf=%s" % buf, src, raw, "param")
                return
        fade = self._ms_to_sec(attrs.get("time", ""))
        stmt = "stop %s" % channel
        if fade:
            stmt += " fadeout %s" % fmt_sec(fade)
        self.emit(stmt, src)

    def _act_jump(self, name, attrs, spec, raw, src, from_at):
        self.flush_run(src)
        if attrs.get("cond"):
            self.todo("条件跳转（KAG 伪循环）需人工改写", src, raw, "control-flow")
            return
        storage = attrs.get("storage") or attrs.get("file")
        target = attrs.get("target")
        if has_expr(storage) or has_expr(target or ""):
            self.todo("跳转目标为运行时表达式", src, raw, "control-flow")
            return
        if storage:
            dest = self.by_storage.get(os.path.splitext(storage)[0].lower())
            if dest is None:
                self.todo("跳转目标文件不存在：%s" % storage, src, raw, "control-flow")
                return
            if not dest.selected:
                self.todo("跳转目标文件未参与本次转换（--only 过滤）：%s" % storage,
                          src, raw, "control-flow")
                return
            label = (target or "").lstrip("*") or ENTRY
            if label != ENTRY and label not in dest.label_names:
                self.todo("目标 label 不存在：%s:%s" % (storage, label), src, raw, "control-flow")
                return
            if dest.tier == "manual" and label != ENTRY:
                self.todo("目标位于人工档：%s:%s" % (storage, label), src, raw, "control-flow")
                return
            self.emit("jump %s" % self.full_label(dest, label), src)
            return
        label = (target or "").lstrip("*")
        if not label or label not in self.cur.label_names:
            self.todo("同文件跳转目标不存在：%s" % (target or "?"), src, raw, "control-flow")
            return
        self.emit("jump %s" % self.full_label(self.cur, label), src)

    def _act_call(self, name, attrs, spec, raw, src, from_at):
        self.flush_run(src)
        storage = attrs.get("storage") or attrs.get("file")
        target = attrs.get("target")
        if has_expr(storage) or has_expr(target or ""):
            self.todo("call 目标为运行时表达式", src, raw, "control-flow")
            return
        if storage:
            dest = self.by_storage.get(os.path.splitext(storage)[0].lower())
            if dest is None:
                self.todo("call 目标文件不存在：%s" % storage, src, raw, "control-flow")
                return
            if not dest.selected:
                self.todo("call 目标文件未参与本次转换（--only 过滤）：%s" % storage,
                          src, raw, "control-flow")
                return
            label = (target or "").lstrip("*") or ENTRY
            if label != ENTRY and label not in dest.label_names:
                self.todo("call 目标 label 不存在：%s:%s" % (storage, label), src, raw, "control-flow")
                return
            if dest.tier == "manual" and label != ENTRY:
                self.todo("call 目标位于人工档：%s:%s" % (storage, label), src, raw, "control-flow")
                return
            self.emit("call %s" % self.full_label(dest, label), src)
            return
        label = (target or "").lstrip("*")
        if label not in self.cur.label_names:
            self.todo("call 目标不存在：%s" % (target or "?"), src, raw, "control-flow")
            return
        self.emit("call %s" % self.full_label(self.cur, label), src)

    def _act_return(self, name, attrs, spec, raw, src, from_at):
        self.flush_run(src)
        self.emit("return", src)

    def _act_clear_all(self, name, attrs, spec, raw, src, from_at):
        self.flush_run(src)
        storage = attrs.get("storage") or "white"
        hit = self.resolve_image(storage, "bgi")
        for layer in sorted(self.active_fg.keys()):
            self.emit("hide fg%d" % layer, src)
        self.active_fg.clear()
        self.active_fg_img.clear()
        self.emit("window hide", src)
        if hit:
            self.emit("scene %s with dissolve" % hit["name"], src)
            self.pending_copies.append((hit["src"], hit["rel"]))
            self.last_scene = storage
            self.last_scene_line = src
            self.last_scene_trans = "dissolve"
        else:
            self.todo("allclear 背景资源缺失：%s" % storage, src, raw, "asset-missing")
        self.memory_scene(attrs, src)

    def _act_image_layer(self, name, attrs, spec, raw, src, from_at):
        layer = (attrs.get("layer") or "").lower()
        if layer in ("base", "0"):
            self._act_scene(name, attrs, spec, raw, src, from_at)
        else:
            self.todo("image 标签用于非背景层（layer=%s）" % (layer or "?"), src, raw, "image")

    def _act_noop_comment(self, name, attrs, spec, raw, src, from_at):
        if self.run:
            self.flush_run(src)
        note = spec.get("note", "")
        self.comment("[%s] %s" % (name, note) if note else "[%s] 无对应语句，已忽略" % name, src)

    def _act_flag(self, name, attrs, spec, raw, src, from_at):
        self.flush_run(src)
        stmt = self.rule("macros." + name)
        if not stmt:
            self.todo("宏 %s 未在 rules.macros 中映射" % name, src, raw, "macro")
            return
        self.emit(stmt, src)

    def _act_todo(self, name, attrs, spec, raw, src, from_at):
        self.flush_run(src)
        self.todo(spec.get("reason", "该标签未定义转换规则"), src, raw,
                  spec.get("category", "unsupported-tag"))

    def _act_control_if(self, name, attrs, spec, raw, src, from_at):
        self.flush_run(src)
        ok, py, why = self.check_if(attrs, len(self.if_stack))
        if not ok:
            self.todo("条件无法可靠转换：%s" % why, src, raw, "control-flow")
            return
        self.emit("if %s:" % py, src)
        self.if_stack.append({"src": src, "body_depth": self.depth + 1})
        self.depth += 1

    def _act_control_elsif(self, name, attrs, spec, raw, src, from_at):
        if not self.if_stack:
            self.todo("elsif 没有对应的 if", src, raw, "control-flow")
            return
        ok, py, why = self.check_if(attrs, len(self.if_stack) - 1)
        if not ok:
            self.todo("elsif 条件无法可靠转换：%s" % why, src, raw, "control-flow")
            self.emit("else:", src, level=self.if_stack[-1]["body_depth"] - 1)
            self.depth = self.if_stack[-1]["body_depth"]
            return
        self.emit("elif %s:" % py, src, level=self.if_stack[-1]["body_depth"] - 1)
        self.depth = self.if_stack[-1]["body_depth"]

    def _act_control_else(self, name, attrs, spec, raw, src, from_at):
        if not self.if_stack:
            self.todo("else 没有对应的 if", src, raw, "control-flow")
            return
        self.emit("else:", src, level=self.if_stack[-1]["body_depth"] - 1)
        self.depth = self.if_stack[-1]["body_depth"]

    def _act_control_endif(self, name, attrs, spec, raw, src, from_at):
        if not self.if_stack:
            self.todo("endif 没有对应的 if", src, raw, "control-flow")
            return
        frame = self.if_stack.pop()
        self.depth = frame["body_depth"] - 1
        self.comment("[endif] L%d" % frame["src"], src)

    # ---- 内存插入 ----

    def emit_memory(self, reason, src):
        if not self.rule("memory.enabled", True):
            return
        fn = self.rule("memory.function", "renpy.free_memory")
        style = self.rule("memory.call_style", "plain")
        self.result.memory[reason] += 1
        self.result.stats["memory_inserts"] += 1
        tag = "%s:%s" % (self.rule("memory.comment_tag", "mem"), reason)
        if style == "guarded":
            flag = self.rule("memory.guard_flag", "use_free_memory")
            self.emit("if persistent.%s:" % flag, src)
            self.emit_extra("$ %s()" % fn, tag, src, level=self.depth + 1)
        else:
            self.emit_extra("$ %s()" % fn, tag, src)

    def memory_scene(self, attrs, src):
        if not self.rule("memory.enabled", True):
            return
        mode = self.options.get("scene_mode") or self.rule("memory.scene_mode", "boundary")
        boundary = True
        if mode == "boundary":
            sc = self.rule("memory.scene", {})
            storages = sc.get("boundary_storages", ["black", "white"])
            attrs_hit = sc.get("boundary_attrs", ["msgc"])
            gap = int(sc.get("line_gap", 10))
            window = int(sc.get("label_window", 3))
            line = int(src or 0)
            # 文件开头的 entry label 没有行号，last_label_line 可能为 None：
            # 此时「紧跟 label」这一条件按「距上一个 label 未超过窗口」无法判定，直接判 False。
            near_label = False
            if self.last_label_line is not None:
                try:
                    near_label = (line - int(self.last_label_line)) <= window
                except (TypeError, ValueError):
                    near_label = False
            far_from_prev_scene = True
            if self.last_scene_line is not None:
                try:
                    far_from_prev_scene = (line - int(self.last_scene_line)) >= gap
                except (TypeError, ValueError):
                    far_from_prev_scene = True
            boundary = (attrs.get("storage") in storages
                        or any(a in attrs for a in attrs_hit)
                        or near_label
                        or self.scene_seen == 0
                        or far_from_prev_scene)
        else:
            boundary = mode == "always"
        if boundary:
            self.emit_memory("scene", src)
        else:
            self.result.stats["memory_scene_skipped"] += 1

    # ---- if/块收尾 ----

    def close_open_blocks(self, src):
        while self.if_stack:
            frame = self.if_stack.pop()
            self.depth = frame["body_depth"]
            self.emit("pass", src)
            self.comment("[if L%d] 未闭合，已补 pass" % frame["src"], src)
            self.depth = frame["body_depth"] - 1

    # ---- 辅助文件 ----

    def build_aux_files(self):
        self.build_characters_rpy()
        self.build_images_rpy()
        self.build_memory_rpy()
        self.build_runtime_rpy()
        self.build_fonts()
        self.build_options_rpy()
        self.build_screens_rpy()
        self.build_start_rpy()
        self.build_manual_rpy()

    def tpl(self, key, default=""):
        text = self.rule("templates." + key, default)
        return text

    def build_characters_rpy(self):
        lines = ["# 自动生成：说话人 -> Ren'Py Character 变量", ""]
        for c in self.result.characters:
            lines.append('define %s = Character("%s", color="%s")'
                         % (c["var"], c["name"].replace('"', '\\"'), c["color"]))
        if not self.result.characters:
            lines.append("# 未扫描到 speaker 标签")
        self.pending_writes.append(("game/characters.rpy", "\n".join(lines)))

    def build_images_rpy(self):
        lines = ["# 自动生成：图片定义（扫描 %d 张）" % len(self.image_defs), ""]
        for d in self.image_defs:
            lines.append('image %s = "%s"' % (d["name"], d["rel"]))
        self.pending_writes.append(("game/images.rpy", "\n".join(lines)))

    def build_memory_rpy(self):
        decls = self.rule("memory.declarations",
                          ["default persistent.memory_mode = 0",
                           "default persistent.use_free_memory = True"])
        cache_mb = self.rule("memory.cache.mb", 256)
        lines = [
            "# 自动生成：内存默认值与低内存模式",
            "# memory_mode: 0=自动低内存(256MB)  1=保留 Ren'Py 默认  2=自定义 memory_custom_mb",
            "",
        ]
        lines.extend(decls)
        lines.append("default persistent.memory_custom_mb = %d" % self.rule("memory.cache.custom_mb", 384))
        lines.append("")
        lines.append("init -1 python:")
        lines.append("    if persistent.memory_mode == 0:")
        lines.append("        config.image_cache_size_mb = %d" % cache_mb)
        lines.append("    elif persistent.memory_mode == 2 and persistent.memory_custom_mb:")
        lines.append("        config.image_cache_size_mb = persistent.memory_custom_mb")
        self.pending_writes.append(("game/memory_defaults.rpy", "\n".join(lines)))

    def build_runtime_rpy(self):
        se_channels = int(self.rule("output.se_channels", 5))
        positions = self.rule("sprites.positions", {})
        pos_lines = []
        for name, x in positions.items():
            offset = int(x) - int(self.rule("sprites.screen_width", 1024) // 2)
            pos_lines.append("transform pos_%s:" % sanitize_ident(name, lower=True))
            pos_lines.append("    xoffset %d" % offset)
            pos_lines.append("    yalign 1.0")
            pos_lines.append("")
        lines = [
            "# 自动生成：运行时辅助（通道、变换、TODO 记录）",
            "# 不使用自定义图层：背景一律直接 scene，避免 onlayer 造成黑屏。",
            "",
            "init -1 python:",
            "    for _i in range(%d):" % se_channels,
            '        _ch = "se%d" % _i',
            "        try:",
            '            renpy.music.register_channel(_ch, mixer="sfx", loop=False)',
            "        except Exception:",
            "            pass",
            "",
            "init python:",
            "    kr2r_todo_list = []",
            "",
            "    def kr2r_todo(src_file, src_line, reason):",
            "        kr2r_todo_list.append((src_file, src_line, reason))",
            '        msg = "TODO %s:%s %s" % (src_file, src_line, reason)',
            "        print(msg)",
            "        if config.developer:",
            "            renpy.notify(msg)",
            "",
        ]
        lines.extend(pos_lines)
        if not positions:
            lines.append("transform pos_center:")
            lines.append("    xoffset 0")
            lines.append("    yalign 1.0")
            lines.append("")
        self.pending_writes.append(("game/kr2r_runtime.rpy", "\n".join(lines)))

    def build_fonts(self):
        """内置字体：把转换器自带的字体复制到 game/fonts/，供界面引用。

        字体源优先级：output.font_source（绝对路径）> 转换器项目内置 fonts/ 目录。
        """
        font = self.rule("output.font", "")
        if not font:
            return
        src = self.rule("output.font_source", "")
        if not src:
            src = os.path.join(HERE, "fonts", os.path.basename(font))
        if os.path.isfile(src):
            self.pending_copies.append((src, font))
            self.log("内置字体：%s -> %s" % (src, font))
        else:
            self.warn("内置字体缺失：%s（界面将回退到默认字体）" % src)

    def build_options_rpy(self):
        name = self.rule("output.game_title", "Krkr2Renpy Project")
        ver = self.rule("output.game_version", "1.0")
        quit_action = self.rule("output.quit_action", "Quit(confirm=True)")
        main_menu = self.rule("output.main_menu_action", "")
        developer = self.rule("output.developer", False)
        fullscreen = self.rule("output.fullscreen", True)
        skip_unseen = self.rule("output.skip_unseen", True)
        lines = [
            "# 自动生成：项目配置",
            'define config.name = "%s"' % name,
            'define config.version = "%s"' % ver,
            "define config.screen_width = %d" % self.rule("sprites.screen_width", 1024),
            "define config.screen_height = %d" % self.rule("sprites.screen_height", 768),
            'define config.window = "auto"',
            'define config.save_directory = "krkr2renpy-saves"',
            "define config.developer = %s" % ("True" if developer else "False"),
            "",
            "# 历史记录条数上限（界面里的历史记录依赖它）。",
            "define config.history_length = 300",
            "",
            "# 退出动作：点关闭/退出时执行。",
            "define config.quit_action = %s" % quit_action,
        ]
        # main_menu_action 在部分 7.x 版本不存在，留空即不生成，避免 unknown config variable。
        if main_menu:
            lines.append("define config.main_menu_action = %s" % main_menu)
        lines.append("")
        lines.append("# ---- 启动行为：全屏 + 跳过未读 ----")
        lines.append("init 999 python:")
        lines.append("    # 全屏：Switch / 无窗口环境下必须全屏，否则 4:3 内容会被横向拉伸成 16:9。")
        lines.append("    try:")
        lines.append("        renpy.game.preferences.fullscreen = %s"
                     % ("True" if fullscreen else "False"))
        lines.append("    except Exception:")
        lines.append("        pass")
        lines.append("    # 跳过未读：Ren'Py 默认只跳已读，遇到没读过的句子会停住（表现为按跳过键卡住）。")
        lines.append("    try:")
        lines.append("        renpy.game.preferences.skip_unseen = %s"
                     % ("True" if skip_unseen else "False"))
        lines.append("    except Exception:")
        lines.append("        pass")
        lines.append("")
        self.pending_writes.append(("game/options.rpy", "\n".join(lines)))

    def build_screens_rpy(self):
        tpl = self.tpl("screens_rpy")
        if not tpl:
            tpl = "\n".join([
                "# 最小界面定义（无 GUI 依赖）",
                "# 注意：ruby_style 必须先声明，say_dialogue 才能引用它。",
                "",
                "style ruby_style is default:",
                "    size 14",
                "    yoffset -16",
                "",
                "style say_window:",
                '    background "#00000088"',
                "    xfill True",
                "    yalign 1.0",
                "    padding (40, 24, 40, 24)",
                "",
                "style say_who:",
                '    color "#ffffff"',
                "    size 28",
                "",
                "style say_dialogue:",
                '    color "#ffffff"',
                "    size 26",
                "    line_leading 10",
                "    ruby_style style.ruby_style",
                "",
                "screen say(who, what):",
                "    window:",
                '        id "window"',
                '        style "say_window"',
                "        if who is not None:",
                '            text who id "who" style "say_who"',
                '        text what id "what" style "say_dialogue"',
                "",
                "screen choice(items):",
                "    vbox:",
                "        xalign 0.5",
                "        yalign 0.5",
                "        spacing 12",
                "        for i in items:",
                "            textbutton i.caption action i.action",
                "",
            ])
        # 字体内置：替换模板里的 {FONT_LINE} 占位符。未配置字体时生成注释行。
        font = self.rule("output.font", "")
        if font:
            font_line = '    font "%s"' % font
        else:
            font_line = '    # font "fonts/1.ttf"'
        tpl = tpl.replace("{FONT_LINE}", font_line)
        self.pending_writes.append(("game/screens.rpy", tpl))

    def build_start_rpy(self):
        """生成入口：splashscreen 播放片头，start 进入正篇。

        这样 logo 结尾的 `jump start` 不会自循环，同时也保留了片头。
        """
        target = self.rule("output.start_target", "")
        splash = self.rule("output.splash_target", "")
        lines = ["# 自动生成：游戏入口", "# splashscreen 播片头，start 进正篇，二者分离以避免自循环。", ""]

        def resolve(name):
            stem = os.path.splitext(os.path.basename(name))[0]
            info = self.by_storage.get(stem.lower())
            return (info, stem)

        splash_info, splash_stem = resolve(splash) if splash else (None, "")
        start_info, start_stem = resolve(target) if target else (None, "")

        if splash_info and splash_info.selected:
            lines.append("label splashscreen:")
            lines.append("    jump %s" % self.full_label(splash_info, ENTRY))
            lines.append("")
        elif splash and splash_info and not splash_info.selected:
            lines.append("# TODO 片头文件 %s 未参与本次转换（--only）" % splash)
            lines.append("")

        if start_info and start_info.selected:
            lines.append("label start:")
            lines.append("    jump %s" % self.full_label(start_info, ENTRY))
        elif start_info and not start_info.selected:
            lines.append("# TODO 起始文件 %s 未参与本次转换（--only），补全转换后重新生成。" % target)
            lines.append("label start:")
            lines.append("    return")
            self.result.todos.append({
                "file": "script.rpy", "line": 1, "tier": "auto",
                "category": "start-missing",
                "reason": "起始文件 %s 不在本次 --only 范围内" % target,
                "original": "",
            })
        elif target:
            lines.append("# TODO 起始文件不存在：%s" % target)
            lines.append("label start:")
            lines.append("    return")
        else:
            lines.append("# TODO 未配置 output.start_target")
            lines.append("label start:")
            lines.append("    return")
        self.pending_writes.append(("game/script.rpy", "\n".join(lines)))

    def build_manual_rpy(self):
        lines = ["# 自动生成：人工档入口说明", ""]
        manual = [i for i in self.infos if i.tier == "manual"]
        lines.append("# 共 %d 个文件判定为人工档，入口 label 已生成占位。" % len(manual))
        for info in manual:
            lines.append("#   %s -> %s" % (info.name, self.full_label(info, ENTRY)))
        if not manual:
            lines.append("# 无")
        self.pending_writes.append(("game/manual_todo.rpy", "\n".join(lines)))

    # ---- 报告 ----

    def write_report(self):
        r = self.result
        stats = r.stats
        mem = r.memory
        total_mem = sum(mem.values())
        lines = []
        lines.append("# Krkr → Ren'Py 转换报告")
        lines.append("")
        lines.append("- 生成时间：%s" % now_str())
        lines.append("- 源目录：`%s`" % self.source_dir)
        lines.append("- 输出目录：`%s`" % self.output_dir)
        lines.append("- 规则文件：`%s`" % self.rules_path)
        lines.append("- 模式：%s" % ("dry-run（未写盘）" if self.dry_run else "正式转换"))
        lines.append("")
        lines.append("## 总览")
        lines.append("")
        lines.append("| 项目 | 数值 |")
        lines.append("|---|---:|")
        lines.append("| .ks 文件 | %d |" % stats["files"])
        lines.append("| 自动档文件 | %d |" % stats["files_auto"])
        lines.append("| 人工档文件 | %d |" % stats["files_manual"])
        lines.append("| 源行数 | %d |" % (stats["lines_auto"] + stats["lines_manual"]))
        lines.append("| 输出行数 | %d |" % stats["out_lines"])
        lines.append("| TODO 条目 | %d |" % len(r.todos))
        lines.append("| 内存插入 | %d |" % total_mem)
        lines.append("")
        lines.append("## 内存清理插入统计")
        lines.append("")
        lines.append("| 规则 | 位置 | 次数 |")
        lines.append("|---|---|---:|")
        names = {"scene": "规则 1", "label": "规则 2", "loop": "规则 3", "segment": "规则 4"}
        for key in ("scene", "label", "loop", "segment"):
            lines.append("| %s | %s | %d |" % (names[key], key, mem.get(key, 0)))
        lines.append("")
        lines.append("被 boundary 模式跳过的场景内插入：%d 次。" % stats["memory_scene_skipped"])
        lines.append("")
        lines.append("## TODO 分类")
        lines.append("")
        by_cat = collections.Counter(t["category"] for t in r.todos)
        lines.append("| 分类 | 条数 |")
        lines.append("|---|---:|")
        for cat, num in by_cat.most_common():
            lines.append("| %s | %d |" % (cat, num))
        lines.append("")
        if r.missing_assets:
            lines.append("## 缺失资源（前 50 条）")
            lines.append("")
            for m in r.missing_assets[:50]:
                lines.append("- %s" % m)
            lines.append("")
        lines.append("## 角色映射")
        lines.append("")
        lines.append("| 原文 | 变量 | 颜色 | 罗马字来源 |")
        lines.append("|---|---|---|---|")
        for c in r.characters:
            lines.append("| %s | `%s` | %s | %s |" % (c["name"], c["var"], c["color"], c["method"]))
        lines.append("")
        lines.append("## 文件明细")
        lines.append("")
        lines.append("| 文件 | 编码 | 行数 | 档位 | TODO |")
        lines.append("|---|---|---:|---|---:|")
        todo_by_file = collections.Counter(t["file"] for t in r.todos)
        for info in self.infos:
            lines.append("| %s | %s | %d | %s | %d |" % (
                info.name, info.encoding, len(info.lines), info.tier,
                todo_by_file.get(info.name, 0)))
        lines.append("")
        lines.append("## 待办清单（前 300 条，完整列表见 todo_report.csv）")
        lines.append("")
        lines.append("| # | 文件 | 行 | 分类 | 原因 | 原文 |")
        lines.append("|---:|---|---:|---|---|---|")
        for idx, t in enumerate(r.todos[:300], 1):
            raw = (t["original"] or "").replace("|", "\\|")[:80]
            lines.append("| %d | %s | %d | %s | %s | `%s` |" % (
                idx, t["file"], t["line"], t["category"], t["reason"].replace("|", "\\|"), raw))
        lines.append("")
        lines.append("## 后续人工步骤")
        lines.append("")
        lines.append("1. 人工档（标题/存档/读档/设置/相册/回想/战斗/TJS 插件）需按 `manual_raw/` 原文手工移植。")
        lines.append("2. 所有 `# TODO` 行逐条确认，尤其是 `control-flow`、`macro`、`tjs` 三类。")
        lines.append("3. 立绘位置常量来自 KAG `scPositionX`，已换算成 `xoffset`，如需微调改 `kr2r_runtime.rpy`。")
        lines.append("4. 若仍 OOM，把 `persistent.memory_mode` 设为 0 并把 `image_cache_size_mb` 调至 384。")
        lines.append("")
        r.report_md = "\n".join(lines)

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["file", "line", "tier", "category", "reason", "original"])
        for t in r.todos:
            writer.writerow([t["file"], t["line"], t["tier"], t["category"],
                             t["reason"], t["original"]])
        r.report_csv = buf.getvalue()

        self.pending_writes.append((self.rule("output.report_md", "conversion_report.md"),
                                    r.report_md))
        self.pending_writes.append((self.rule("output.report_csv", "todo_report.csv"),
                                    r.report_csv))

    # ---- 写盘 ----

    def flush_outputs(self):
        written = 0
        for rel, content in self.pending_writes:
            path = os.path.join(self.output_dir, rel)
            write_text(path, content)
            written += 1
        self.log("写入 %d 个文本文件" % written)

        if self.options.get("skip_assets"):
            self.log("按选项跳过资源复制")
            return
        mode = self.rule("assets.copy_mode", "link")
        done = set()
        copied = 0
        failed = 0
        for src, rel in self.pending_copies:
            if rel in done:
                continue
            done.add(rel)
            dst = os.path.join(self.output_dir, "game", rel)
            if os.path.exists(dst):
                continue
            ensure_dir(os.path.dirname(dst))
            try:
                if mode == "link":
                    try:
                        os.link(src, dst)
                    except OSError:
                        shutil.copy2(src, dst)
                elif mode == "copy":
                    shutil.copy2(src, dst)
                else:
                    continue
                copied += 1
            except Exception as exc:
                failed += 1
                self.warn("复制资源失败 %s -> %s：%s" % (src, rel, exc))
        self.result.stats["assets_copied"] = copied
        self.log("资源 %d 个（失败 %d）" % (copied, failed))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        description="Krkr/KAG .ks -> Ren'Py 7.3.2 .rpy 半自动转换器")
    p.add_argument("--input", "-i", help="包含 .ks 的游戏目录（data.xp3 解包内容，会自动递归查找）")
    p.add_argument("--patch", help="patch.xp3 解包目录（汉化剧本/资源，同名文件优先于主目录）")
    p.add_argument("--output", "-o", default=os.path.join(HERE, "out"),
                   help="Ren'Py 项目输出目录（默认 ./out）")
    p.add_argument("--rules", "-r", default=DEFAULT_RULES, help="rules.json 路径")
    p.add_argument("--dry-run", action="store_true", help="只分析并打印摘要，不写任何文件")
    p.add_argument("--only", help="只转换指定文件（逗号分隔，例如 A000.ks,logo.ks）")
    p.add_argument("--scene-mode", choices=["boundary", "always"],
                   help="覆盖 rules.json 的 memory.scene_mode")
    p.add_argument("--skip-assets", action="store_true", help="不复制/链接图片与音频")
    p.add_argument("--print-script", action="store_true", help="把生成的脚本打印到 stdout（配合 --only）")
    p.add_argument("--max-print-lines", type=int, default=200)
    p.add_argument("--gui", action="store_true", help="强制启动图形界面")
    p.add_argument("--quiet", action="store_true")
    return p


def run_cli(args):
    rules = load_rules(args.rules)
    only = [x.strip() for x in (args.only or "").split(",") if x.strip()]
    options = {
        "scene_mode": args.scene_mode,
        "only": only,
        "skip_assets": args.skip_assets,
        "patch_dir": getattr(args, "patch", ""),
    }
    conv = ProjectConverter(rules, args.rules, args.input, args.output, options)
    last = [0]

    def progress(done, total, name):
        if args.quiet:
            return
        if done == total or done - last[0] >= max(1, total // 20):
            last[0] = done
            sys.stdout.write("\r[%d/%d] %-24s" % (done, total, name[:24]))
            sys.stdout.flush()

    def log(msg):
        if not args.quiet:
            sys.stdout.write("\n" + msg + "\n")
            sys.stdout.flush()

    result = conv.run(progress=progress, log=log, dry_run=args.dry_run)
    if not args.quiet:
        sys.stdout.write("\n")
    if args.print_script or (args.dry_run and len(only) == 1):
        for rel, content in conv.pending_writes:
            if rel.startswith("game/script/") and rel.endswith(".rpy"):
                print("# ---- %s ----" % rel)
                for line in content.split("\n")[:args.max_print_lines]:
                    print(line)
                break
    print("文件 %d（auto %d / manual %d），TODO %d，内存插入 %d"
          % (result.stats["files"], result.stats["files_auto"],
             result.stats["files_manual"], len(result.todos),
             result.stats["memory_inserts"]))
    if args.dry_run:
        print("dry-run：未写入任何文件。报告预览：\n")
        print(result.report_md[:2000])
    else:
        print("报告：%s" % os.path.join(args.output, "conversion_report.md"))
    return 0


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

class ConverterApp(object):

    def __init__(self, root, rules_path):
        self.root = root
        self.rules_path = rules_path
        self.worker = None
        self.q = queue.Queue()
        self.last_result = None
        self.last_converter = None
        root.title("krkr2renpy — Krkr 剧本转 Ren'Py")
        root.geometry("860x720")
        root.minsize(740, 620)
        self._setup_style()
        self._build()

    def _setup_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        BG = "#f5f6f9"
        FG = "#2c3038"
        MUTED = "#7a8090"
        ACCENT = "#e2558a"
        ACCENT_HOVER = "#c94a78"
        self.root.configure(bg=BG)
        style.configure(".", background=BG, foreground=FG,
                         font=("Microsoft YaHei UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Title.TLabel", background=BG, foreground="#1b1b2f",
                        font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Sub.TLabel", background=BG, foreground=MUTED,
                        font=("Microsoft YaHei UI", 9))
        style.configure("TLabelframe", background=BG, bordercolor="#e2e4ea")
        style.configure("TLabelframe.Label", background=BG, foreground="#4a5060",
                        font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TButton", background="#ffffff", foreground=FG,
                        bordercolor="#d5d8e0", padding=(12, 6))
        style.map("TButton",
                  background=[("active", "#eef0f5"), ("disabled", "#eceef2")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                        bordercolor=ACCENT, font=("Microsoft YaHei UI", 12, "bold"),
                        padding=(20, 10))
        style.map("Accent.TButton",
                  background=[("active", ACCENT_HOVER), ("disabled", "#c9cbd3")])
        style.configure("TEntry", fieldbackground="#ffffff", bordercolor="#d5d8e0")
        style.configure("TCombobox", fieldbackground="#ffffff")
        style.configure("TCheckbutton", background=BG)
        style.configure("TProgressbar", background=ACCENT, troughcolor="#e6e8ef",
                        borderwidth=0, thickness=14)

    def _build(self):
        pad = {"padx": 14, "pady": 6}

        # 标题区
        head = ttk.Frame(self.root)
        head.pack(fill="x", padx=14, pady=(14, 2))
        ttk.Label(head, text="krkr2renpy", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="v%s" % VERSION, style="Sub.TLabel").pack(side="left", padx=(8, 0), pady=(8, 0))
        ttk.Label(self.root,
                  text="把 Kirikiri/KAG 的 .ks 剧本转换成 Ren'Py 工程，自动生成界面、字体与配置。",
                  style="Sub.TLabel").pack(anchor="w", padx=16)

        # 输入输出
        io = ttk.LabelFrame(self.root, text=" 输入输出 ")
        io.pack(fill="x", **pad)
        io.columnconfigure(1, weight=1)

        ttk.Label(io, text="游戏目录（data.xp3）").grid(row=0, column=0, sticky="w", padx=6, pady=5)
        self.var_in = tk.StringVar(value=os.path.join(HERE, u"游戏解包"))
        ttk.Entry(io, textvariable=self.var_in).grid(row=0, column=1, sticky="we", padx=6, pady=5)
        ttk.Button(io, text="浏览…", command=lambda: self._pick_dir(self.var_in)).grid(row=0, column=2, padx=6, pady=5)

        ttk.Label(io, text="补丁目录（patch.xp3，可选）").grid(row=1, column=0, sticky="w", padx=6, pady=5)
        self.var_patch = tk.StringVar(value="")
        ttk.Entry(io, textvariable=self.var_patch).grid(row=1, column=1, sticky="we", padx=6, pady=5)
        ttk.Button(io, text="浏览…", command=lambda: self._pick_dir(self.var_patch)).grid(row=1, column=2, padx=6, pady=5)

        ttk.Label(io, text="输出目录").grid(row=2, column=0, sticky="w", padx=6, pady=5)
        self.var_out = tk.StringVar(value=os.path.join(HERE, "out"))
        ttk.Entry(io, textvariable=self.var_out).grid(row=2, column=1, sticky="we", padx=6, pady=5)
        ttk.Button(io, text="浏览…", command=lambda: self._pick_dir(self.var_out)).grid(row=2, column=2, padx=6, pady=5)

        ttk.Label(io, text="规则文件 rules.json").grid(row=3, column=0, sticky="w", padx=6, pady=5)
        self.var_rules = tk.StringVar(value=self.rules_path)
        ttk.Entry(io, textvariable=self.var_rules).grid(row=3, column=1, sticky="we", padx=6, pady=5)
        ttk.Button(io, text="浏览…", command=self._pick_rules).grid(row=3, column=2, padx=6, pady=5)

        # 选项
        opt = ttk.LabelFrame(self.root, text=" 选项 ")
        opt.pack(fill="x", **pad)
        ttk.Label(opt, text="内存清理模式").grid(row=0, column=0, sticky="w", padx=6, pady=5)
        self.var_mode = tk.StringVar(value="boundary")
        ttk.Combobox(opt, textvariable=self.var_mode, values=["boundary", "always"],
                     state="readonly", width=14).grid(row=0, column=1, sticky="w", padx=6, pady=5)
        self.var_dry = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="试运行（不写文件）", variable=self.var_dry).grid(row=0, column=2, sticky="w", padx=14, pady=5)
        self.var_skip = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="跳过资源复制", variable=self.var_skip).grid(row=0, column=3, sticky="w", padx=14, pady=5)

        # 操作按钮 + 进度
        act = ttk.LabelFrame(self.root, text=" 转换 ")
        act.pack(fill="x", **pad)
        self.btn_run = ttk.Button(act, text="▶  开始转换", style="Accent.TButton", command=self.start)
        self.btn_run.grid(row=0, column=0, rowspan=2, sticky="w", padx=(6, 10), pady=8)
        self.btn_save = ttk.Button(act, text="保存待办报告", command=self.save_report, state="disabled")
        self.btn_save.grid(row=0, column=1, sticky="w", padx=4, pady=4)
        ttk.Button(act, text="打开输出目录", command=self.open_out).grid(row=0, column=2, sticky="w", padx=4, pady=4)
        ttk.Button(act, text="退出", command=self.root.destroy).grid(row=0, column=3, sticky="e", padx=4, pady=4)
        self.progress = ttk.Progressbar(act, mode="determinate")
        self.progress.grid(row=1, column=1, columnspan=3, sticky="we", padx=4, pady=4)
        self.var_status = tk.StringVar(value="就绪")
        ttk.Label(act, textvariable=self.var_status, style="Sub.TLabel").grid(row=1, column=4, sticky="w", padx=6, pady=4)
        act.columnconfigure(1, weight=1)

        # 日志
        log = ttk.LabelFrame(self.root, text=" 日志 ")
        log.pack(fill="both", expand=True, **pad)
        self.logbox = ScrolledText(log, height=20, state="disabled", wrap="word",
                                   font=("Consolas", 9), bg="#1b1b2f", fg="#e6e6ea",
                                   insertbackground="#ffffff", relief="flat", padx=10, pady=8)
        self.logbox.pack(fill="both", expand=True, padx=6, pady=6)

    def _pick_dir(self, var):
        d = filedialog.askdirectory(initialdir=var.get() or HERE)
        if d:
            var.set(d)

    def _pick_rules(self):
        f = filedialog.askopenfilename(initialdir=HERE, filetypes=[("rules", "*.json")])
        if f:
            self.var_rules.set(f)

    def log(self, msg):
        self.q.put(("log", msg))

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            rules = load_rules(self.var_rules.get())
        except Exception as exc:
            messagebox.showerror("rules.json 读取失败", str(exc))
            return
        options = {
            "scene_mode": self.var_mode.get(),
            "skip_assets": bool(self.var_skip.get()),
            "patch_dir": self.var_patch.get().strip(),
        }
        dry = bool(self.var_dry.get())
        self.btn_run.config(state="disabled")
        self.btn_save.config(state="disabled")
        self.progress.config(value=0, maximum=100)
        self._set_log("")
        self.worker = threading.Thread(
            target=self._work,
            args=(rules, self.var_rules.get(), self.var_in.get(), self.var_out.get(), options, dry),
            daemon=True)
        self.worker.start()
        self.root.after(80, self._poll)

    def _work(self, rules, rules_path, src, out, options, dry):
        try:
            conv = ProjectConverter(rules, rules_path, src, out, options)
            res = conv.run(progress=lambda d, t, n: self.q.put(("progress", d, t, n)),
                           log=lambda m: self.q.put(("log", m)),
                           dry_run=dry)
            self.last_converter = conv
            self.last_result = res
            self.q.put(("done", res))
        except Exception:
            self.q.put(("error", traceback.format_exc()))

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "log":
                    self._append_log(item[1])
                elif item[0] == "progress":
                    _tag, d, t, n = item
                    self.progress.config(maximum=max(1, t), value=d)
                    self.var_status.set("[%d/%d] %s" % (d, t, n))
                elif item[0] == "done":
                    res = item[1]
                    self.var_status.set("完成：TODO %d 条" % len(res.todos))
                    self.btn_run.config(state="normal")
                    self.btn_save.config(state="normal")
                    self._append_log("==== 完成：文件 %d，TODO %d，内存插入 %d ===="
                                     % (res.stats["files"], len(res.todos),
                                        res.stats["memory_inserts"]))
                    return
                elif item[0] == "error":
                    self._append_log(item[1])
                    self.var_status.set("失败")
                    self.btn_run.config(state="normal")
                    messagebox.showerror("转换失败", item[1][-1500:])
                    return
        except queue.Empty:
            pass
        if self.worker and self.worker.is_alive():
            self.root.after(120, self._poll)

    def _set_log(self, text):
        self.logbox.config(state="normal")
        self.logbox.delete("1.0", "end")
        if text:
            self.logbox.insert("end", text)
        self.logbox.config(state="disabled")

    def _append_log(self, text):
        self.logbox.config(state="normal")
        self.logbox.insert("end", text + "\n")
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def save_report(self):
        if not self.last_result:
            messagebox.showinfo("提示", "还没有可保存的报告，请先运行一次转换。")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("CSV", "*.csv")],
            initialdir=self.var_out.get() or HERE,
            initialfile="conversion_report.md")
        if not path:
            return
        data = self.last_result.report_csv if path.lower().endswith(".csv") else self.last_result.report_md
        try:
            write_text(path, data)
            messagebox.showinfo("已保存", path)
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def open_out(self):
        path = self.var_out.get()
        if os.path.isdir(path):
            try:
                os.startfile(path)  # Windows
            except Exception:
                pass
        else:
            messagebox.showinfo("提示", "输出目录尚不存在：%s" % path)


def run_gui(rules_path):
    if not HAS_TK:
        print("当前 Python 没有 tkinter，请使用命令行模式（--input ...）")
        return 1
    root = tk.Tk()
    app = ConverterApp(root, rules_path)
    root.mainloop()
    return 0


# --------------------------------------------------------------------------

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.gui or (not args.input and not argv):
        return run_gui(args.rules)
    if not args.input:
        parser.error("缺少 --input")
    if not os.path.isdir(args.input):
        parser.error("输入目录不存在：%s" % args.input)
    return run_cli(args)


if __name__ == "__main__":
    sys.exit(main())
