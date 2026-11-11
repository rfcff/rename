#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rename.py — 通用标识符批量重命名工具（支持目录递归、前缀/后缀/自定义映射）

与项目完全解耦：没有任何内置目录、路径或符号黑名单。所有行为由命令行或 JSON 配置驱动，
可直接拷到任意工程使用。

核心能力
  1. 目录递归：可给多个目录，自动遍历所有子目录
  2. 改名策略：--prefix / --suffix（可组合）/ --map（精确 old->new 映射）
  3. 语言适配：按扩展名自动选择词法规则，只替换「代码」中的标识符，
     自动跳过字符串、字符字面量、注释、raw string、模板字符串、#include 行
  4. 符号来源：--symbols 清单 / ctags / 内置正则启发式 / --map，可插拔
  5. 安全默认：dry-run 为默认，--apply 才写盘；写盘前备份 + git 脏检查 + 原子写入
  6. 幂等：一次性 alternation 替换，自动跳过已改名的符号，--check 自检重复前后缀
  7. 可审计：symbols.txt / plan.csv / hit_counts.csv / changes.diff / report.json

快速开始
  python3 rename.py ./src --prefix acme_ --dry-run
  python3 rename.py ./src --prefix acme_ --apply
  python3 rename.py ./src ./include --suffix _v2 --dry-run
  python3 rename.py ./src --map mapping.json --dry-run
  python3 rename.py ./src --check --prefix acme_
  python3 rename.py --init-config rename.config.json

文档与更多示例见 --help。
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import difflib
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VERSION = "2.0.0"

# ---------------------------------------------------------------------------
# 语言档案：扩展名 / 词法规则 / ctags 语言名 / 关键字
# ---------------------------------------------------------------------------

LANG_PROFILES: dict[str, dict] = {
    "c":        dict(ext=[".c", ".h"],                                              lexer="c_like",      ctags=("C", "c")),
    "cpp":      dict(ext=[".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".inl"], lexer="c_like", ctags=("C++", "c++")),
    "java":     dict(ext=[".java"],                                                lexer="c_like",      ctags=("Java", "java")),
    "kotlin":   dict(ext=[".kt", ".kts"],                                         lexer="c_like",      ctags=("Kotlin", "kotlin")),
    "swift":    dict(ext=[".swift"],                                              lexer="c_like",      ctags=("Swift", "swift")),
    "js":       dict(ext=[".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"],          lexer="js_like",     ctags=("JavaScript", "javascript")),
    "go":       dict(ext=[".go"],                                                  lexer="c_like",      ctags=("Go", "go")),
    "rust":     dict(ext=[".rs"],                                                  lexer="c_like",      ctags=("Rust", "rust")),
    "csharp":   dict(ext=[".cs"],                                                  lexer="c_like",      ctags=("C#", "csharp")),
    "php":      dict(ext=[".php"],                                                 lexer="c_like",      ctags=("PHP", "php")),
    "python":   dict(ext=[".py", ".pyi", ".pyw"],                                 lexer="python_like", ctags=("Python", "python")),
    "ruby":     dict(ext=[".rb"],                                                  lexer="python_like", ctags=("Ruby", "ruby")),
    "shell":    dict(ext=[".sh", ".bash", ".zsh"],                                lexer="python_like", ctags=("Sh", "sh")),
    "plain":    dict(ext=[],                                                       lexer="plain",       ctags=(None, None)),
}

# 扩展名 -> 词法规则（auto 模式按文件逐个判断，支持混合语言目录）
EXT_LEXER: dict[str, str] = {}
EXT_CTAGS_LANG: dict[str, tuple[str | None, str | None]] = {}
for _name, _prof in LANG_PROFILES.items():
    for _e in _prof["ext"]:
        EXT_LEXER.setdefault(_e, _prof["lexer"])
        EXT_CTAGS_LANG.setdefault(_e, _prof["ctags"])

ALL_EXTENSIONS = sorted(EXT_LEXER)

# 跨语言保留字并集：宁可少改，不可误改。业务上的第三方/标准库名请用 --exclude-symbol* 补充
RESERVED_WORDS: set[str] = set("""
    if else elif for while do switch case default break continue return goto
    sizeof alignof decltype typeid static_cast dynamic_cast const_cast reinterpret_cast
    new delete throw try catch finally this super null nullptr true false nil None True False
    class struct union enum interface trait namespace package import export from as is in of
    public private protected internal static final const var val let vararg inline virtual
    abstract override sealed open suspend operator fun func fn def function lambda yield await async
    typedef template typename using mutable explicit friend constexpr consteval constinit
    void bool char short int long float double signed unsigned wchar_t char8_t char16_t char32_t
    string String Object Any Unit Nothing Boolean Byte Integer Long Float Double Void
    auto register volatile extern thread_local noexcept requires concept co_await co_return co_yield
    not and or xor compl bitand bitor and_eq or_eq xor_eq not_eq
    pass with assert del global nonlocal raise except print len range enumerate zip map filter
    self cls type id input str int float list dict set tuple bool bytes format super
    func go defer chan select fallthrough type map range interface struct package
    let mut impl pub use mod where move ref match crate dyn unsafe impl trait
    undefined NaN Infinity console require module exports
""".split())

# 用 \Z 而非 $：Python 正则的 $ 允许字符串末尾有一个换行，
# .match() 会让 "Foo\n" 这类非法符号通过校验并进入 re.escape，生成跨行替换模式。
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")
PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")
SUFFIX_RE = re.compile(r"^[A-Za-z0-9_]+\Z")
INCLUDE_LINE_RE = re.compile(r"^\s*#\s*(?:include|import)\b")
HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

# 内置符号提取模式（从「代码段」中提取，不会命中字符串/注释）
BUILTIN_PATTERNS: list[str] = [
    r"\b(?:class|struct|union|enum|interface|trait|namespace|record)\s+([A-Za-z_][A-Za-z0-9_]*)",
    r"\b(?:def|fun|func|fn|function|sub)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(",
]

LEXER_SPEC: dict[str, dict] = {
    # C/C++/Java/Kotlin/Go/Rust...：不含反引号，避免把 C++ 中偶然出现的 ` 误判为字符串
    "c_like": dict(line="//", block=("/*", "*/"), quotes=('"', "'"),
                   triples=(), raw=True, include=True, digit_sep=True),
    # JS/TS：额外支持 `模板字符串`
    "js_like": dict(line="//", block=("/*", "*/"), quotes=('"', "'", "`"),
                    triples=(), raw=False, include=False),
    # Python/Ruby/Shell：# 行注释 + 三引号文档字符串
    "python_like": dict(line="#", block=None, quotes=('"', "'"),
                        triples=('"""', "'''"), raw=False, include=False),
    # 不做任何词法保护，全篇按标识符边界替换
    "plain": dict(line=None, block=None, quotes=(), triples=(), raw=False, include=False),
}


# ---------------------------------------------------------------------------
# 轻量词法分段：把源码切成 (片段, 是否为可替换代码)
# ---------------------------------------------------------------------------

def _scan_quoted(text: str, i: int, quote: str) -> int:
    n = len(text)
    j = i + 1
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == quote:
            return j + 1
        j += 1
    return n


def _scan_raw_string(text: str, i: int) -> int:
    n = len(text)
    j = i + 2
    while j < n and text[j] != "(":
        j += 1
    if j >= n:
        return n
    delim = text[i + 2 : j]
    end_marker = ")" + delim + '"'
    k = text.find(end_marker, j + 1)
    return n if k < 0 else k + len(end_marker)


def _include_spans(text: str) -> dict[int, int]:
    spans: dict[int, int] = {}
    off = 0
    for line in text.splitlines(keepends=True):
        if INCLUDE_LINE_RE.match(line):
            spans[off] = off + len(line)
        off += len(line)
    return spans


def split_segments(text: str, lexer: str, protect_include: bool = True) -> list[tuple[str, bool]]:
    """返回 [(片段, 是否可替换代码)]。字面量与注释恒为 False。"""
    spec = LEXER_SPEC[lexer]
    if lexer == "plain":
        return [(text, True)]

    n = len(text)
    inc = _include_spans(text) if (spec["include"] and protect_include) else {}
    line_tok: str | None = spec["line"]
    block: tuple[str, str] | None = spec["block"]
    quotes: tuple[str, ...] = spec["quotes"]
    triples: tuple[str, ...] = spec["triples"]

    segments: list[tuple[str, bool]] = []
    buf_start = 0
    i = 0

    def emit_literal(start: int, end: int) -> None:
        nonlocal buf_start, i
        if buf_start < start:
            segments.append((text[buf_start:start], True))
        segments.append((text[start:end], False))
        i = end
        buf_start = end

    while i < n:
        if i in inc:
            emit_literal(i, inc[i])
            continue

        if line_tok and text.startswith(line_tok, i):
            j = text.find("\n", i + len(line_tok))
            emit_literal(i, n if j < 0 else j)
            continue

        if block and text.startswith(block[0], i):
            k = text.find(block[1], i + len(block[0]))
            emit_literal(i, n if k < 0 else k + len(block[1]))
            continue

        hit_triple = False
        for t in triples:
            if text.startswith(t, i):
                k = text.find(t, i + len(t))
                emit_literal(i, n if k < 0 else k + len(t))
                hit_triple = True
                break
        if hit_triple:
            continue

        if spec["raw"] and text[i : i + 2] == 'R"':
            emit_literal(i, _scan_raw_string(text, i))
            continue

        # C++14 数字分隔符（1'000'000 / 0xAB'CD）里的单引号不是字符字面量。
        # 若不特判，_scan_quoted 会从第一个 ' 一路吞到下一个 '，把中间代码
        # 误判为字符串，造成整段静默漏替换。
        if (spec.get("digit_sep") and text[i] == "'"
                and 0 < i < n - 2
                and text[i - 1] in HEX_DIGITS and text[i + 1] in HEX_DIGITS
                and text[i + 2] != "'"):
            i += 1
            continue

        if text[i] in quotes:
            emit_literal(i, _scan_quoted(text, i, text[i]))
            continue

        i += 1

    if buf_start < n:
        segments.append((text[buf_start:n], True))
    return segments


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Config:
    dirs: list[Path]
    prefix: str
    suffix: str
    map_file: Path | None
    symbols_file: Path | None
    extract: str                     # auto | ctags | regex | file
    lang: str                        # auto | cpp | python | ...
    lexer_override: str | None       # 强制词法规则
    extensions: list[str] | None     # None = 按 lang 自动
    include_globs: list[str]
    exclude_dir_globs: list[str]
    exclude_file_globs: list[str]
    exclude_symbols: set[str]
    ctags_lang: str | None
    ctags_kinds: str
    symbol_pattern: str | None
    protect_include: bool
    follow_symlinks: bool
    skip_reserved: bool
    out_dir: Path
    backup_dir: Path | None
    diff_out: Path | None
    max_diff_lines: int
    allow_dirty: bool
    no_backup: bool
    mode: str                        # dry-run | apply | check


CONFIG_EXAMPLE: dict = {
    "_comment": "rename.py 配置示例。命令行参数优先级高于本文件；dirs 相对本配置文件所在目录解析。",
    "dirs": ["./src", "./include"],
    "prefix": "acme_",
    "suffix": "",
    "extract": "auto",
    "lang": "auto",
    "include_globs": [],
    "exclude_dir_globs": [".git", "build", "vendor", "node_modules", "third_party"],
    "exclude_file_globs": ["*_test.cpp", "*.generated.*"],
    "exclude_symbols": ["main", "printf", "malloc", "free"],
    "ctags_kinds": "fcspmgtuvd",
    "protect_include": True,
    "skip_reserved": True,
    "max_diff_lines": 200,
    "out_dir": "./.rename-out",
}


# ---------------------------------------------------------------------------
# 文件选择
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class FileRef:
    base: Path
    rel: Path

    @property
    def path(self) -> Path:
        return self.base / self.rel


def path_matches(rel: Path, pattern: str) -> bool:
    """glob 匹配相对路径、任一祖先路径或任一级目录名。"""
    s = rel.as_posix()
    parts = rel.parts
    if fnmatch.fnmatch(s, pattern):
        return True
    for k in range(1, len(parts) + 1):
        if fnmatch.fnmatch("/".join(parts[:k]), pattern):
            return True
        if fnmatch.fnmatch(parts[k - 1], pattern):
            return True
    return False


def dir_excluded(rel: Path, patterns: list[str]) -> bool:
    """--exclude-dir 只匹配目录路径，不把文件名本身当成目录名。"""
    parent = rel.parent
    if parent == Path("."):
        return False
    return any(path_matches(parent, g) for g in patterns)


def lexer_for(path: Path, cfg: Config) -> str:
    if cfg.lexer_override:
        return cfg.lexer_override
    if cfg.lang != "auto":
        return LANG_PROFILES[cfg.lang]["lexer"]
    return EXT_LEXER.get(path.suffix.lower(), "c_like")


def allowed_extensions(cfg: Config) -> set[str] | None:
    if cfg.extensions:
        return {e if e.startswith(".") else "." + e for e in cfg.extensions}
    if cfg.lexer_override == "plain":
        return None                     # plain：不限制扩展名，任意文本文件均可处理
    if cfg.lang != "auto":
        return set(LANG_PROFILES[cfg.lang]["ext"]) or None
    return set(ALL_EXTENSIONS)          # auto：所有已知语言扩展名


def iter_files(cfg: Config) -> list[FileRef]:
    """递归收集待处理文件。

    用 os.walk 而非 Path.rglob：rglob 在 Python < 3.13 不支持跟随符号链接目录，
    且原实现对符号链接文件无条件跳过，使 --follow-symlinks 形同虚设。
    visited 用于防止跟随符号链接目录时成环导致无限递归。
    """
    exts = allowed_extensions(cfg)
    out_paths = {p.resolve() for p in (cfg.out_dir, cfg.backup_dir, cfg.diff_out) if p}
    found: dict[Path, FileRef] = {}

    for base in cfg.dirs:
        if not base.is_dir():
            raise FileNotFoundError(f"目录不存在：{base}")
        visited: set[Path] = set()
        for dirpath_s, dirnames, filenames in os.walk(str(base), followlinks=cfg.follow_symlinks):
            dirpath = Path(dirpath_s)
            real_dir = dirpath.resolve()
            if real_dir in visited:
                dirnames[:] = []            # 符号链接成环，剪枝
                continue
            visited.add(real_dir)

            # 目录级剪枝：不跟随的符号链接目录、被 --exclude-dir 命中的目录
            keep: list[str] = []
            for d in dirnames:
                cand = dirpath / d
                if not cfg.follow_symlinks and cand.is_symlink():
                    continue
                if any(path_matches(cand.relative_to(base), g) for g in cfg.exclude_dir_globs):
                    continue
                keep.append(d)
            dirnames[:] = keep

            for name in filenames:
                p = dirpath / name
                if not p.is_file():
                    continue
                if p.is_symlink() and not cfg.follow_symlinks:
                    continue
                rel = p.relative_to(base)
                if dir_excluded(rel, cfg.exclude_dir_globs):
                    continue
                if any(path_matches(rel, g) for g in cfg.exclude_file_globs):
                    continue
                rp = p.resolve()
                if rp in out_paths or any(o in rp.parents for o in out_paths):
                    continue
                if exts is not None and p.suffix.lower() not in exts:
                    continue
                if cfg.include_globs and not any(path_matches(rel, g) for g in cfg.include_globs):
                    continue
                found[rp] = FileRef(base, rel)

    return sorted(found.values(), key=lambda f: (str(f.base), f.rel.as_posix()))


# ---------------------------------------------------------------------------
# 符号提取
# ---------------------------------------------------------------------------

def read_exact(path: Path) -> str:
    """按原样读取（newline="" 保证 CRLF 不被静默转换成 LF）。"""
    with path.open("r", encoding="utf-8", errors="surrogateescape", newline="") as f:
        return f.read()


def code_only(text: str, lexer: str, protect_include: bool) -> str:
    """把非代码段换成换行，避免跨段拼接产生假匹配。"""
    return "\n".join(seg if is_code else "\n" * seg.count("\n")
                     for seg, is_code in split_segments(text, lexer, protect_include))


def extract_regex(cfg: Config, files: list[FileRef]) -> set[str]:
    patterns = ([re.compile(cfg.symbol_pattern)] if cfg.symbol_pattern
                else [re.compile(p) for p in BUILTIN_PATTERNS])
    names: set[str] = set()
    for ref in files:
        code = code_only(read_exact(ref.path), lexer_for(ref.path, cfg), cfg.protect_include)
        for pat in patterns:
            for m in pat.finditer(code):
                name = m.group(1) if m.groups() else m.group(0)
                names.add(name)
    return names


def detect_ctags() -> tuple[str, str]:
    """返回 (可执行文件, 方言标识 universal/exuberant)。"""
    exe = shutil.which("ctags")
    if not exe:
        raise RuntimeError("未找到 ctags；请安装 universal-ctags，或改用 --extract regex / --symbols")
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
        out = (r.stdout or "") + (r.stderr or "")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"无法执行 {exe} --version：{exc}") from exc
    if "Universal Ctags" in out:
        return exe, "universal"
    if "Exuberant Ctags" in out:
        return exe, "exuberant"
    raise RuntimeError(
        f"{exe} 不是 Universal/Exuberant Ctags（macOS 自带 Xcode ctags 不支持 --*-kinds）。\n"
        "  解决：brew install universal-ctags；或 --extract regex / --symbols FILE"
    )


def ctags_lang_for(cfg: Config, files: list[FileRef]) -> tuple[str | None, str | None]:
    if cfg.ctags_lang:
        return cfg.ctags_lang, cfg.ctags_lang.lower()
    if cfg.lang != "auto":
        return LANG_PROFILES[cfg.lang]["ctags"]
    # auto：按文件扩展名取出现次数最多的 ctags 语言
    tally: dict[str, int] = {}
    for ref in files:
        pair = EXT_CTAGS_LANG.get(ref.path.suffix.lower())
        if pair and pair[0]:
            tally[pair[0]] = tally.get(pair[0], 0) + 1
    if not tally:
        return None, None
    top = max(tally, key=tally.get)  # type: ignore[arg-type]
    for pair in EXT_CTAGS_LANG.values():
        if pair[0] == top:
            return pair
    return top, top.lower()


def extract_ctags(cfg: Config, files: list[FileRef]) -> set[str]:
    exe, dialect = detect_ctags()
    uni_lang, exu_lang = ctags_lang_for(cfg, files)
    if not uni_lang:
        raise RuntimeError("无法确定 ctags 语言；请用 --ctags-lang 指定，或改用 --extract regex")
    lang = uni_lang if dialect == "universal" else exu_lang
    arg = (f"--kinds-{lang}={cfg.ctags_kinds}" if dialect == "universal"
           else f"--{lang}-kinds={cfg.ctags_kinds}")

    names: set[str] = set()
    BATCH = 200
    paths = [str(ref.path) for ref in files]
    for start in range(0, len(paths), BATCH):
        r = subprocess.run([exe, "-x", arg] + paths[start : start + BATCH],
                           capture_output=True, text=True, errors="replace", timeout=180)
        if r.returncode not in (0, 1):
            raise RuntimeError(f"ctags 失败（{r.returncode}）：{(r.stderr or '')[:300]}")
        for line in r.stdout.splitlines():
            parts = line.split(None, 3)
            if parts:
                names.add(parts[0])
    return names


def load_symbols_file(path: Path) -> set[str]:
    result: set[str] = set()
    for line in read_exact(path).splitlines():
        item = line.split("#", 1)[0].strip()
        if item:
            result.add(item)
    return result


def load_map_file(path: Path) -> dict[str, str]:
    raw = read_exact(path).strip()
    mapping: dict[str, str] = {}
    header_keys = {"old", "old_name", "from", "source", "symbol"}
    header_vals = {"new", "new_name", "to", "target", "replacement", "dest"}
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            mapping = {str(k): str(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            mapping = {str(it["old"]): str(it["new"]) for it in obj}
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        if raw.lstrip().startswith(("{", "[")):
            raise ValueError(f"映射文件看起来是 JSON 但解析失败：{exc}") from exc
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = re.split(r"[\s,=:\t]+", line, maxsplit=1)
            if len(parts) != 2:
                continue
            old, new = parts[0].strip(), parts[1].strip()
            # 只跳过真正的表头；不能只看 startswith("old")，否则会误伤
            # oldStyle / oldValue 这类以 old 开头的合法符号。
            if old.lower() in header_keys and new.lower() in header_vals:
                continue
            mapping[old] = new
    if not mapping:
        raise ValueError(f"映射文件为空或格式无法识别：{path}")
    return mapping


def build_plan(cfg: Config, files: list[FileRef]) -> tuple[dict[str, str], list[str], str, list[str]]:
    """返回 (old->new 映射, 被拒绝的符号, 提取方式, 因已带前后缀被跳过的符号)。"""
    skipped: list[str] = []
    if cfg.map_file:
        raw_map = load_map_file(cfg.map_file)
        source = f"map:{cfg.map_file.name}"
    else:
        if cfg.symbols_file:
            names = load_symbols_file(cfg.symbols_file)
            source = f"file:{cfg.symbols_file.name}"
        elif cfg.extract == "file":
            raise ValueError("--extract file 需要同时提供 --symbols FILE")
        elif cfg.extract == "regex":
            print("[WARN] 正则启发式会提取所有调用位置的名字（可能含标准库/第三方库函数），"
                  "务必 review symbols.txt，或用 --exclude-symbol* 排除", file=sys.stderr)
            names = extract_regex(cfg, files)
            source = "regex"
        elif cfg.extract == "ctags":
            names = extract_ctags(cfg, files)
            source = "ctags"
        else:  # auto
            try:
                names = extract_ctags(cfg, files)
                source = "ctags(auto)"
            except RuntimeError as exc:
                print(f"[WARN] ctags 不可用，回退内置正则启发式：{str(exc).splitlines()[0]}", file=sys.stderr)
                print(f"[WARN] 正则模式可能包含标准库/第三方函数名，务必 review {cfg.out_dir / 'symbols.txt'}",
                      file=sys.stderr)
                names = extract_regex(cfg, files)
                source = "regex(auto-fallback)"

        raw_map = {}
        for name in sorted(names):
            if cfg.skip_reserved and name in RESERVED_WORDS:
                continue
            if name in cfg.exclude_symbols:
                continue
            # 幂等：自动提取时源码里已改名的符号不能再次加前后缀。
            # 但白名单模式完全信任用户清单，避免误伤本来就带该前缀的符号。
            trust_list = bool(cfg.symbols_file)
            if not trust_list and cfg.prefix and name.startswith(cfg.prefix):
                skipped.append(name)
                continue
            if not trust_list and cfg.suffix and name.endswith(cfg.suffix):
                skipped.append(name)
                continue
            raw_map[name] = cfg.prefix + name + cfg.suffix

    plan: dict[str, str] = {}
    rejected: list[str] = []
    for old, new in sorted(raw_map.items()):
        if not IDENT_RE.match(old) or not IDENT_RE.match(new):
            rejected.append(old)
            continue
        if old == new:
            continue
        plan[old] = new
    return plan, rejected, source, skipped


# ---------------------------------------------------------------------------
# 替换
# ---------------------------------------------------------------------------

def build_pattern(names: list[str]) -> re.Pattern[str]:
    if not names:
        raise ValueError("改名计划为空")
    ordered = sorted(names, key=lambda s: (-len(s), s))
    body = "|".join(re.escape(s) for s in ordered)
    # 显式 ASCII 标识符边界；不用 \b（Unicode 模式下语义更宽）
    return re.compile(r"(?<![A-Za-z0-9_])(?:" + body + r")(?![A-Za-z0-9_])")


def transform(text: str, pattern: re.Pattern[str], plan: dict[str, str],
              lexer: str, protect_include: bool, counts: dict[str, int]) -> str:
    out: list[str] = []
    for segment, is_code in split_segments(text, lexer, protect_include):
        if not is_code:
            out.append(segment)
            continue

        def repl(m: re.Match[str]) -> str:
            name = m.group(0)
            counts[name] = counts.get(name, 0) + 1
            return plan[name]

        out.append(pattern.sub(repl, segment))
    return "".join(out)


# ---------------------------------------------------------------------------
# 安全与写盘
# ---------------------------------------------------------------------------

def git_roots(dirs: list[Path]) -> list[Path]:
    roots: set[Path] = set()
    for d in dirs:
        try:
            r = subprocess.run(["git", "-C", str(d), "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            # git 是可选工具（README 已声明）；缺失时不应让 --apply 直接崩溃
            return []
        if r.returncode == 0 and (r.stdout or "").strip():
            roots.add(Path(r.stdout.strip()).resolve())
    return sorted(roots)


def check_git_clean(roots: list[Path], allow_dirty: bool) -> str:
    if not roots:
        return "not-a-git-repo"
    dirty: list[Path] = []
    for root in roots:
        try:
            s = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return "git-unavailable"
        if (s.stdout or "").strip():
            dirty.append(root)
    if dirty and not allow_dirty:
        raise RuntimeError(
            "以下 git 工作区存在未提交改动，已拒绝 --apply：\n  "
            + "\n  ".join(str(p) for p in dirty)
            + "\n  请先提交/暂存，或显式加 --allow-dirty。"
        )
    return "dirty" if dirty else "clean"


def backup_files(refs: list[FileRef], dest: Path) -> int:
    # 不同目录可能有相同的 base.name（如 /a/src 与 /b/src），仅用 base.name
    # 分目录会让同名文件互相覆盖，导致原始内容丢失，故加唯一序号。
    bases = sorted({ref.base for ref in refs}, key=str)
    index = {b: i for i, b in enumerate(bases)}
    for ref in refs:
        target = dest / f"{index[ref.base]:02d}-{ref.base.name}" / ref.rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ref.path, target)
    return len(refs)


def write_atomic(path: Path, content: str) -> None:
    """原子写入，避免中途失败留下半文件；保留原文件权限。"""
    stat = path.stat()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".rename-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write(content)
        os.chmod(tmp, stat.st_mode & 0o7777)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="rename.py",
        description="通用标识符批量重命名：目录递归 + 前缀/后缀/映射，默认 dry-run",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  # C++ 目录递归，给函数/类加前缀，先预览
  python3 rename.py ./src --prefix acme_ --dry-run
  # 确认后写盘（自动备份 + 报告）
  python3 rename.py ./src --prefix acme_ --apply
  # 多目录 + 后缀
  python3 rename.py ./src ./include --suffix _v2 --dry-run
  # 前后缀同时使用
  python3 rename.py ./src --prefix acme_ --suffix _impl --dry-run
  # 人工 review 后的白名单驱动（最安全）
  python3 rename.py ./src --symbols symbols.txt --prefix acme_ --dry-run
  # 精确映射（JSON/CSV/old=new）
  python3 rename.py ./src --map mapping.json --apply
  # 只处理指定语言与文件
  python3 rename.py . --lang cpp --include '*.cpp' --exclude-dir 'vendor*' --prefix x_ --dry-run
  # 幂等自检
  python3 rename.py ./src --check --prefix acme_
  # 生成配置模板
  python3 rename.py --init-config rename.config.json
""",
    )
    p.add_argument("dirs", nargs="*", type=Path, help="要处理的目录，可多个，递归所有子目录")
    p.add_argument("--version", action="version", version=f"rename.py {VERSION}")

    strategy = p.add_argument_group("改名策略（--map 优先；否则至少需要 prefix 或 suffix）")
    strategy.add_argument("--prefix", help="统一加前缀，如 acme_（须为合法标识符开头）")
    strategy.add_argument("--suffix", help="统一加后缀，如 _v2")
    strategy.add_argument("--map", dest="map_file", type=Path, help="精确映射文件：JSON / CSV / old=new")

    src = p.add_argument_group("符号来源")
    src.add_argument("--symbols", dest="symbols_file", type=Path, help="符号清单，一行一个，# 为注释")
    src.add_argument("--extract", choices=["auto", "ctags", "regex", "file"], default=None,
                     help="auto(默认，ctags 优先失败回退正则) / ctags / regex(内置启发式) / file")
    src.add_argument("--symbol-pattern", help="自定义提取正则，第一个捕获组为符号名（覆盖内置）")
    src.add_argument("--ctags-lang", help="ctags 语言名，auto 模式按扩展名推断")
    src.add_argument("--ctags-kinds", default=None, help="ctags kinds，默认 fcspmgtuvd")
    src.add_argument("--no-skip-reserved", action="store_true", help="不过滤语言保留字（不推荐）")

    sel = p.add_argument_group("文件选择")
    sel.add_argument("--lang", choices=["auto", *LANG_PROFILES], default=None,
                     help="语言档案，决定默认扩展名与词法规则；auto 按扩展名逐个判断")
    sel.add_argument("--lexer", choices=list(LEXER_SPEC), default=None,
                     help="强制词法规则：c_like / js_like / python_like / plain(不保护字面量)")
    sel.add_argument("--ext", action="append", help="扩展名白名单，可多次或逗号分隔（覆盖 --lang）")
    sel.add_argument("--include", dest="include_globs", action="append", help="文件 glob，可多次")
    sel.add_argument("--exclude-dir", dest="exclude_dir_globs", action="append",
                     help="目录 glob，可多次；匹配相对路径或任一级目录名")
    sel.add_argument("--exclude-file", dest="exclude_file_globs", action="append", help="文件 glob，可多次")
    sel.add_argument("--exclude-symbol", dest="exclude_symbols", action="append", help="排除的符号名，可多次或逗号分隔")
    sel.add_argument("--exclude-symbol-file", type=Path, help="排除符号清单文件")
    sel.add_argument("--follow-symlinks", action="store_true", help="跟随符号链接（默认跳过）")
    sel.add_argument("--no-protect-include", action="store_true", help="不保护 #include / #import 行")

    safe = p.add_argument_group("安全与输出")
    mode = safe.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="仅预览（默认）")
    mode.add_argument("--apply", action="store_true", help="真正写盘")
    mode.add_argument("--check", action="store_true", help="只做重复前后缀自检")
    safe.add_argument("--out-dir", type=Path, help="报告/备份目录，默认 <cwd>/.rename-out")
    safe.add_argument("--backup-dir", type=Path, help="独立备份目录（默认在 out-dir 下）")
    safe.add_argument("--diff-out", type=Path, help="额外把 unified diff 写到指定文件")
    safe.add_argument("--max-diff-lines", type=int, help="终端打印 diff 行数上限，默认 200")
    safe.add_argument("--allow-dirty", action="store_true", help="允许在脏 git 工作区上 --apply")
    safe.add_argument("--no-backup", action="store_true", help="跳过备份（不推荐）")

    p.add_argument("--config", type=Path, help="JSON 配置文件；命令行优先级更高")
    p.add_argument("--init-config", type=Path, help="生成示例配置到指定路径后退出")
    return p.parse_args(argv)


def split_csv(values: list[str] | str | None) -> list[str]:
    """同时兼容命令行 append 列表与配置文件里的字符串/列表。"""
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for v in values:
        out += [x.strip() for x in str(v).split(",") if x.strip()]
    return out


def build_config(args: argparse.Namespace) -> Config:
    if args.init_config:
        target: Path = args.init_config
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(CONFIG_EXAMPLE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[OK] 示例配置已生成：{target}")
        print(f"     用法：python3 rename.py --config {target} --apply")
        raise SystemExit(0)

    raw: dict = {}
    if args.config:
        cfg_path: Path = args.config
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        raw.pop("_comment", None)
        # 配置里的相对路径按配置文件所在目录解析
        base = cfg_path.parent.resolve()

        def relp(value):
            if value is None:
                return None
            p = Path(value)
            return (base / p) if not p.is_absolute() else p

        raw["dirs"] = [relp(d) for d in raw.get("dirs", [])]
        for key in ("out_dir", "backup_dir", "symbols_file", "map_file", "exclude_symbol_file"):
            if key in raw:
                raw[key] = relp(raw[key])

    def pick(dest: str, default=None):
        v = getattr(args, dest, None)
        if v is None or v is False or v == []:
            return raw.get(dest, default)
        return v

    dirs = [Path(d) for d in (args.dirs or raw.get("dirs") or [])]
    for d in dirs[:]:
        if not d.exists():
            raise ValueError(f"目录不存在：{d}")
    dirs = [d.resolve() for d in dirs]

    mode = "apply" if args.apply else ("check" if args.check else "dry-run")
    prefix = pick("prefix", "") or ""
    suffix = pick("suffix", "") or ""
    map_file = pick("map_file")
    symbols_file = pick("symbols_file")
    exclude_symbol_file = pick("exclude_symbol_file")

    # 优先级：命令行 > 配置 > 默认值
    def pick_bool(dest: str, default: bool) -> bool:
        v = getattr(args, dest, None)
        if v:
            return True
        return bool(raw.get(dest, default))

    cfg = Config(
        dirs=dirs,
        prefix=prefix,
        suffix=suffix,
        map_file=Path(map_file) if map_file else None,
        symbols_file=Path(symbols_file) if symbols_file else None,
        extract=pick("extract", "auto"),
        lang=pick("lang", "auto"),
        lexer_override=pick("lexer"),
        extensions=split_csv(args.ext) or split_csv(raw.get("extensions")) or None,
        include_globs=split_csv(args.include_globs) or split_csv(raw.get("include_globs")),
        exclude_dir_globs=(split_csv(args.exclude_dir_globs) or split_csv(raw.get("exclude_dir_globs"))
                           or [".git", ".rename-out"]),
        exclude_file_globs=split_csv(args.exclude_file_globs) or split_csv(raw.get("exclude_file_globs")),
        exclude_symbols=set(split_csv(args.exclude_symbols) or split_csv(raw.get("exclude_symbols"))),
        ctags_lang=pick("ctags_lang"),
        ctags_kinds=pick("ctags_kinds", "fcspmgtuvd"),
        symbol_pattern=pick("symbol_pattern"),
        protect_include=not args.no_protect_include and bool(raw.get("protect_include", True)),
        follow_symlinks=pick_bool("follow_symlinks", False),
        skip_reserved=not args.no_skip_reserved and bool(raw.get("skip_reserved", True)),
        out_dir=Path(pick("out_dir", Path.cwd() / ".rename-out")).resolve(),
        backup_dir=Path(pick("backup_dir")).resolve() if pick("backup_dir") else None,
        diff_out=Path(pick("diff_out")).resolve() if pick("diff_out") else None,
        max_diff_lines=int(pick("max_diff_lines", 200)),
        allow_dirty=pick_bool("allow_dirty", False),
        no_backup=pick_bool("no_backup", False),
        mode=mode,
    )

    if exclude_symbol_file:
        cfg.exclude_symbols |= load_symbols_file(Path(exclude_symbol_file))
    if cfg.lang not in ["auto", *LANG_PROFILES]:
        raise ValueError(f"未知 --lang：{cfg.lang}")
    if cfg.extensions:
        cfg.extensions = [e if e.startswith(".") else "." + e for e in cfg.extensions]
    if prefix and not PREFIX_RE.match(prefix):
        raise ValueError("prefix 必须是合法标识符开头：^[A-Za-z_][A-Za-z0-9_]*$")
    if suffix and not SUFFIX_RE.match(suffix):
        raise ValueError("suffix 只能包含字母数字下划线：^[A-Za-z0-9_]+$")
    if not cfg.map_file and not prefix and not suffix and mode != "check":
        raise ValueError("必须指定 --prefix / --suffix / --map 之一")
    if not dirs:
        raise ValueError("请至少给出一个目录，或用 --config 提供 dirs")
    if cfg.lexer_override and cfg.lexer_override not in LEXER_SPEC:
        raise ValueError(f"未知 --lexer：{cfg.lexer_override}")
    return cfg


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_check(cfg: Config, files: list[FileRef]) -> int:
    """检测重复前后缀，判断是否被重复执行过。"""
    tokens: list[str] = []
    if cfg.prefix:
        tokens.append(cfg.prefix + cfg.prefix)
    if cfg.suffix:
        tokens.append(cfg.suffix + cfg.suffix)
    if not tokens:
        print("[CHECK] 未指定 --prefix/--suffix，无可自检内容")
        return 2
    bad: list[str] = []
    for ref in files:
        text = read_exact(ref.path)
        # 只在代码段查重，避免字符串/注释里的同名文本造成误报
        code = code_only(text, lexer_for(ref.path, cfg), cfg.protect_include)
        for tok in tokens:
            if tok in code:
                bad.append(f"{ref.path}  (含 {tok!r})")
                break
    if bad:
        print(f"[CHECK] 发现疑似重复改名，共 {len(bad)} 个文件：")
        for line in bad[:30]:
            print("   ", line)
        return 1
    print(f"[CHECK] 通过：扫描 {len(files)} 个文件，未发现重复前后缀")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = build_config(parse_args(argv if argv is not None else sys.argv[1:]))
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        print(f"[FATAL] 配置错误：{exc}", file=sys.stderr)
        return 2

    if cfg.mode != "check":
        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        if cfg.backup_dir:
            cfg.backup_dir.mkdir(parents=True, exist_ok=True)

    try:
        files = iter_files(cfg)
    except (FileNotFoundError, OSError) as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 2

    print(f"[INFO] version  = {VERSION}")
    print(f"[INFO] mode     = {cfg.mode}")
    print(f"[INFO] dirs     = {', '.join(str(d) for d in cfg.dirs)}")
    print(f"[INFO] files    = {len(files)}")
    print(f"[INFO] strategy = " + (f"map({cfg.map_file.name})" if cfg.map_file
                                   else f"'{cfg.prefix}' + name + '{cfg.suffix}'"))

    if cfg.mode == "check":
        return run_check(cfg, files)
    if not files:
        print("[WARN] 没有匹配到文件，退出")
        return 0

    try:
        plan, rejected, source, skipped = build_plan(cfg, files)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"[FATAL] 符号提取失败：{exc}", file=sys.stderr)
        return 2

    print(f"[INFO] source   = {source}")
    print(f"[INFO] plan     = {len(plan)} 个改名项")
    print(f"[INFO] rejected = {len(rejected)} 个非法符号")
    if skipped:
        shown = ", ".join(skipped[:8]) + (" ..." if len(skipped) > 8 else "")
        print(f"[WARN] {len(skipped)} 个已带前后缀的符号被跳过（视为已改名）：{shown}")
        print("[WARN] 若确实需要再加一次前后缀，请用 --map 显式指定")

    (cfg.out_dir / "symbols.txt").write_text("\n".join(sorted(plan)) + ("\n" if plan else ""), encoding="utf-8")
    (cfg.out_dir / "rejected.txt").write_text("\n".join(rejected) + ("\n" if rejected else ""), encoding="utf-8")
    (cfg.out_dir / "skipped.txt").write_text("\n".join(skipped) + ("\n" if skipped else ""), encoding="utf-8")
    with (cfg.out_dir / "plan.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["old", "new"])
        for old, new in sorted(plan.items()):
            w.writerow([old, new])

    if not plan:
        print("[WARN] 改名计划为空，退出。检查 symbols.txt / --exclude-symbol / 保留字过滤")
        return 0

    pattern = build_pattern(list(plan))
    per_symbol: dict[str, int] = {}
    changed: list[tuple[FileRef, str, str]] = []
    read_errors: list[str] = []

    for ref in files:
        try:
            old = read_exact(ref.path)
        except (OSError, UnicodeError) as exc:
            read_errors.append(f"{ref.path}: {exc}")
            continue
        counts: dict[str, int] = {}
        new = transform(old, pattern, plan, lexer_for(ref.path, cfg), cfg.protect_include, counts)
        if new != old:
            changed.append((ref, old, new))
            for k, v in counts.items():
                per_symbol[k] = per_symbol.get(k, 0) + v

    with (cfg.out_dir / "hit_counts.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "new_name", "hits"])
        for k in sorted(per_symbol, key=lambda x: (-per_symbol[x], x)):
            w.writerow([k, plan[k], per_symbol[k]])
        for s in plan:
            if s not in per_symbol:
                w.writerow([s, plan[s], 0])

    diff_text = "".join(
        line
        for ref, o, n in changed
        for line in difflib.unified_diff(o.splitlines(keepends=True), n.splitlines(keepends=True),
                                         fromfile=f"a/{ref.base.name}/{ref.rel.as_posix()}",
                                         tofile=f"b/{ref.base.name}/{ref.rel.as_posix()}")
    )
    (cfg.out_dir / "changes.diff").write_text(diff_text, encoding="utf-8", errors="surrogateescape")
    if cfg.diff_out:
        cfg.diff_out.parent.mkdir(parents=True, exist_ok=True)
        cfg.diff_out.write_text(diff_text, encoding="utf-8", errors="surrogateescape")

    report = {
        "version": VERSION, "mode": cfg.mode, "dirs": [str(d) for d in cfg.dirs],
        "strategy": {"prefix": cfg.prefix, "suffix": cfg.suffix,
                     "map": str(cfg.map_file) if cfg.map_file else None},
        "source": source, "lang": cfg.lang,
        "scanned_files": len(files), "plan_size": len(plan),
        "rejected_symbols": len(rejected), "skipped_already_renamed": skipped,
        "changed_files": len(changed),
        "total_hits": sum(per_symbol.values()),
        "zero_hit_symbols": [s for s in plan if s not in per_symbol],
        "read_errors": read_errors,
    }
    (cfg.out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[INFO] changed  = {len(changed)} 个文件, {report['total_hits']} 处替换")
    if read_errors:
        print(f"[WARN] 读取失败 {len(read_errors)} 个文件，详见 report.json")
    if report["zero_hit_symbols"]:
        print(f"[WARN] {len(report['zero_hit_symbols'])} 个符号零命中，详见 hit_counts.csv"
              "（若代码已改过名，请核对清单与当前 prefix/suffix 是否匹配）")

    lines = diff_text.splitlines()
    max_lines = max(0, cfg.max_diff_lines)      # 负值会被切片误读成"去掉最后 N 行"
    for line in lines[:max_lines]:
        print(line)
    if len(lines) > max_lines:
        print(f"... diff 已截断，完整内容见 {cfg.out_dir / 'changes.diff'}")

    if cfg.mode == "dry-run":
        print(f"\n[DRY-RUN] 未修改任何文件。产物在 {cfg.out_dir}")
        print("[DRY-RUN] 建议先 review symbols.txt / plan.csv / changes.diff，确认后加 --apply")
        return 0

    try:
        git_state = check_git_clean(git_roots(cfg.dirs), cfg.allow_dirty)
    except RuntimeError as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 2
    print(f"[INFO] git      = {git_state}")

    backup_path = None
    if not cfg.no_backup and changed:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = cfg.backup_dir or (cfg.out_dir / f"backup-{stamp}")
        dest.mkdir(parents=True, exist_ok=True)
        n = backup_files([ref for ref, _, _ in changed], dest)
        backup_path = dest
        print(f"[INFO] backup   = {dest} ({n} 个文件)")

    write_errors: list[str] = []
    applied_count = 0
    for ref, _, new in changed:
        try:
            write_atomic(ref.path, new)
            applied_count += 1
        except OSError as exc:
            write_errors.append(f"{ref.path}: {exc}")

    report.update(git_state=git_state, backup=str(backup_path) if backup_path else None,
                  applied=not write_errors, applied_files=applied_count,
                  write_errors=write_errors)
    (cfg.out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if write_errors:
        print(f"\n[FATAL] 部分应用：成功 {applied_count}/{len(changed)} 个文件，"
              f"失败 {len(write_errors)} 个。", file=sys.stderr)
        for item in write_errors[:10]:
            print(f"        {item}", file=sys.stderr)
        if backup_path:
            print(f"[FATAL] 原始内容备份在 {backup_path}，请人工核对后回滚或重试。", file=sys.stderr)
        else:
            print("[FATAL] 本次未创建备份，请用版本控制或手工恢复。", file=sys.stderr)
        return 3

    print(f"\n[APPLY] 已修改 {len(changed)} 个文件。报告在 {cfg.out_dir}")
    print("[NEXT ] 1) 编译/测试  2) 用同样的 --prefix/--suffix 执行 --check  3) git diff 逐条 review")
    return 0


def cli() -> None:
    """pyproject.toml console_scripts 入口，保证退出码正确传递给调用方。"""
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[ABORT] 用户中断", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    cli()
