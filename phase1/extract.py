"""Stage 3: extract concrete references (PATH, SCRIPT, TOOL, IDENT) from context-file versions.

Usage:
  python -m phase1.extract              # versions.jsonl -> references.jsonl (+ globs.jsonl)
  python -m phase1.extract --sample 20  # print a seeded sample for HUMAN CHECKPOINT 1

Extraction is a heuristic and is deliberately recall-oriented: a reference only becomes a
staleness candidate in Stage 4 if it resolved when the version was written, so tokens that
never resolve (prose like "and/or", product names) fall out as NEVER_RESOLVED.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator

from phase1.config import DATA_INTERIM, phase1_config


@dataclass
class Ref:
    line_no: int          # 1-based
    line_text: str
    ref_type: str         # PATH | SCRIPT | TOOL | IDENT | GLOB (GLOB is logged, not resolved)
    ref_value: str
    context: str          # fence | inline | plain | link | tree
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractConfig:
    known_extensions: set[str]
    known_filenames: set[str]
    known_dotfiles: set[str]
    path_stoplist: set[str]
    system_prefixes: set[str]
    ident_stoplist: set[str]
    ident_min_length: int
    script_builtins: set[str]
    reference_types: set[str]

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None = None) -> "ExtractConfig":
        cfg = cfg or phase1_config()
        ex = cfg["extract"]
        return cls(
            known_extensions={e.lower() for e in ex["known_extensions"]},
            known_filenames=set(ex["known_filenames"]),
            known_dotfiles=set(ex["known_dotfiles"]),
            path_stoplist={s.lower() for s in ex["path_stoplist"]},
            system_prefixes=set(ex["system_path_prefixes"]),
            ident_stoplist=set(ex["ident_stoplist"]),
            ident_min_length=ex["ident_min_length"],
            script_builtins=set(ex["script_builtins"]),
            reference_types=set(cfg["reference_types"]),
        )


# ------------------------------------------------------------------ markdown
# Any indentation: context files often nest fences inside list items.
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
INLINE_CODE = re.compile(r"(`+)(.+?)\1")
MD_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HTML_COMMENT = re.compile(r"<!--.*?-->")
TREE_CHARS = "├└│"
TREE_ITEM = re.compile(r"^(?P<prefix>[\s│├└─|`+\-]*?)(?:[├└|`+]\s*[─\-]{1,3}\s*)(?P<name>\S.*)$")


def iter_segments(content: str) -> Iterator[tuple[int, str, str, str]]:
    """Yield (line_no, line_text, context, text) segments of a markdown document."""
    lines = content.splitlines()
    in_fence, fence_marker = False, ""
    fence_block: list[tuple[int, str]] = []
    in_comment = False
    for i, line in enumerate(lines, start=1):
        m = FENCE.match(line)
        if in_fence:
            if m and m.group(1)[0] == fence_marker[0] and len(m.group(1)) >= len(fence_marker) \
                    and not line.strip()[len(m.group(1)):].strip():
                yield from _fence_segments(fence_block)
                in_fence, fence_block = False, []
            else:
                fence_block.append((i, line))
            continue
        if m:
            in_fence, fence_marker, fence_block = True, m.group(1), []
            continue
        # Skip HTML comments (single- and multi-line).
        text = HTML_COMMENT.sub("", line)
        if in_comment:
            if "-->" in text:
                in_comment, text = False, text.split("-->", 1)[1]
            else:
                continue
        if "<!--" in text:
            in_comment, text = True, text.split("<!--", 1)[0]
        for lm in MD_LINK.finditer(text):
            yield i, line, "link", lm.group(1)
        text = MD_LINK.sub(" ", text)
        for cm in INLINE_CODE.finditer(text):
            yield i, line, "inline", cm.group(2).strip()
        yield i, line, "plain", INLINE_CODE.sub(" ", text)
    if in_fence:  # unterminated fence: treat the remainder as code
        yield from _fence_segments(fence_block)


def _fence_segments(block: list[tuple[int, str]]) -> Iterator[tuple[int, str, str, str]]:
    tree_paths = dict(parse_tree(block)) if any(c in ln for _, ln in block for c in TREE_CHARS) else {}
    for i, line in block:
        if i in tree_paths:
            yield i, line, "tree", tree_paths[i]
        elif tree_paths and re.fullmatch(r"\s*[\w.\-]+/?\s*(?:#.*)?", line):
            continue  # tree root line such as "my-project/"
        elif not any(c in line for c in TREE_CHARS):
            yield i, line, "fence", line


def _strip_tree_comment(name: str) -> str:
    name = re.split(r"\s+(?:#|//|<-|←|-{1,2}\s|—|–|\()", name, maxsplit=1)[0]
    return name.strip().strip("`*")


def parse_tree(block: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Rebuild full paths from a box-drawing directory tree. Returns (line_no, path)."""
    out: list[tuple[int, str]] = []
    stack: list[tuple[int, str]] = []  # (indent column, name)
    for i, line in block:
        m = TREE_ITEM.match(line)
        if not m or not any(c in line for c in "├└"):
            # Root line (e.g. "my-project/" or "."): resets the tree, contributes no segment.
            if line.strip() and not any(c in line for c in TREE_CHARS):
                stack = []
            continue
        indent = len(m.group("prefix"))
        name = _strip_tree_comment(m.group("name"))
        if not name or name in {".", ".."}:
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = "/".join([s for _, s in stack] + [name.rstrip("/")])
        out.append((i, path))
        if name.endswith("/") or "." not in name:
            stack.append((indent, name.rstrip("/")))
    return out


# --------------------------------------------------------------------- PATH
TOKEN_SPLIT = re.compile(r"[\s`'\",;|&=]+")
URL_LIKE = re.compile(r"^(?:[a-z][a-z0-9+.\-]*://|www\.|mailto:)", re.I)
DOMAIN_FIRST = re.compile(r"^[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.(?:com|org|net|io|dev|ai|app|co|sh|"
                          r"gov|edu|me|xyz|cloud|so|tech|info)(?:/|:|$)", re.I)
LINE_SUFFIX = re.compile(r":\d+(?::\d+)?$")
PATH_CHARS = re.compile(r"^[\w.\-/@+\[\]()~]+$")
VERSIONISH = re.compile(r"^v?\d+(?:\.\d+)*[a-z]?$", re.I)


def classify_path_token(tok: str, ec: ExtractConfig, context: str) -> tuple[str, str] | None:
    """Return ("PATH", value) / ("GLOB", value), or None if tok is not a path reference."""
    raw = tok
    if not raw:
        return None
    if URL_LIKE.match(raw) or DOMAIN_FIRST.match(raw.lstrip("(")):
        return None
    # Placeholders
    if any(c in raw for c in "<>{}$") or re.search(r"(?:^|/)your[-_]", raw, re.I) \
            or "path/to/" in raw.lower() or "..." in raw or "…" in raw:
        return None
    tok = raw.strip("([\"'").rstrip(")]\"'.,:;!?")
    if tok.startswith("@"):
        tok = tok[1:]
        if not _has_known_ext(tok, ec):
            return None  # npm scope like @types/node, or a mention
    if any(c in tok for c in "*?"):
        return ("GLOB", tok) if ("/" in tok or "." in tok) else None
    tok = LINE_SUFFIX.sub("", tok)
    while tok.startswith("./"):
        tok = tok[2:]
    if tok.startswith(("../", "~", "-")):
        return None
    if tok.startswith("/"):
        first = tok.lstrip("/").split("/", 1)[0]
        if first in ec.system_prefixes or not first:
            return None
        tok = tok.lstrip("/")
    is_dir = tok.endswith("/")
    tok = tok.rstrip("/")
    if not tok or not PATH_CHARS.match(tok) or tok.lower() in ec.path_stoplist:
        return None
    if "//" in tok or VERSIONISH.match(tok):
        return None
    segments = tok.split("/")
    if not all(re.search(r"[A-Za-z]", s) for s in segments):
        return None
    base = segments[-1]
    if len(segments) > 1:
        if any(s in {".", ".."} for s in segments):
            return None
        return ("PATH", tok)
    # Single segment: must look like a directory ("docs/") or a file.
    if is_dir or base in ec.known_filenames or _is_dotfile(base, ec) or _has_known_ext(base, ec):
        return ("PATH", tok)
    return None


def _has_known_ext(name: str, ec: ExtractConfig) -> bool:
    base = name.rsplit("/", 1)[-1]
    if "." not in base:
        return False
    stem, ext = base.rsplit(".", 1)
    return ext.lower() in ec.known_extensions and bool(stem) and bool(re.search(r"[A-Za-z]", stem)) \
        and stem != "."


def _is_dotfile(base: str, ec: ExtractConfig) -> bool:
    if not base.startswith("."):
        return False
    return base in ec.known_dotfiles or base.startswith(".env") or \
        (base.count(".") >= 2 and _has_known_ext(base, ec))


def extract_paths(text: str, context: str, ec: ExtractConfig) -> list[tuple[str, str]]:
    if context in {"tree", "link"}:
        tok = text.split("#", 1)[0] if context == "link" else text
        if context == "link" and (not tok or tok.startswith("#")):
            return []
        r = classify_path_token(tok, ec, context)
        if r is None and context == "tree" and PATH_CHARS.match(tok.rstrip("/")) \
                and not URL_LIKE.match(tok):
            r = ("PATH", tok.rstrip("/"))   # tree entries are paths by construction
        return [r] if r else []
    if context == "fence":
        text = re.sub(r"^\s*(?:\$|>|#)\s+", "", text)
    out = []
    for tok in TOKEN_SPLIT.split(text):
        r = classify_path_token(tok, ec, context)
        if r:
            out.append(r)
    return out


# ------------------------------------------------------------------- SCRIPT
PKG_RUN = re.compile(r"\b(npm|pnpm|yarn|bun)\s+(?:(?:--?[\w-]+(?:[= ](?!run\b)[\w@./:-]+)?)\s+)*"
                     r"run\s+(?P<name>[\w:.@/-]+)")
NPM_SHORT = re.compile(r"\bnpm\s+(?P<name>test|start|stop|restart|t|tst)\b")
PNPM_YARN_DIRECT = re.compile(r"\b(pnpm|yarn)\s+(?:(?:--?[\w-]+(?:[= ][\w@./:-]+)?)\s+)*"
                              r"(?P<name>[a-z][\w:.-]*)")
WORKSPACE_FLAG = re.compile(r"\s(?:--filter|-F|--workspace|-w|--workspaces|-ws|--cwd|--dir|-C|"
                            r"--prefix|workspace)\b")
MAKE_CMD = re.compile(r"(?:^|[\s;&|(])make\s+(?P<args>[^;&|)#]*)")
JUST_CMD = re.compile(r"(?:^|[\s;&|(])just\s+(?P<args>[^;&|)#]*)")
TARGET = re.compile(r"^[A-Za-z0-9_][\w.\-/]*$")


def extract_scripts(text: str, context: str, ec: ExtractConfig) -> list[tuple[str, dict[str, Any]]]:
    """Return (name, extra) pairs. make/just only from code (fence/inline)."""
    out: list[tuple[str, dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()

    def add(name: str, extra: dict[str, Any]) -> None:
        key = (extra["kind"], name)
        if key not in seen:
            seen.add(key)
            out.append((name, extra))

    for m in PKG_RUN.finditer(text):
        add(m.group("name"), {"kind": "package", "runner": m.group(1),
                              "workspace": bool(WORKSPACE_FLAG.search(text))})
    for m in NPM_SHORT.finditer(text):
        name = {"t": "test", "tst": "test"}.get(m.group("name"), m.group("name"))
        add(name, {"kind": "package", "runner": "npm",
                   "workspace": bool(WORKSPACE_FLAG.search(text))})
    if context in {"fence", "inline"}:
        for m in PNPM_YARN_DIRECT.finditer(text):
            name = m.group("name")
            if name == "run" or name in ec.script_builtins:
                continue
            add(name, {"kind": "package", "runner": m.group(1),
                       "workspace": bool(WORKSPACE_FLAG.search(text))})
        for cmd, kind in ((MAKE_CMD, "make"), (JUST_CMD, "just")):
            for m in cmd.finditer(text):
                args = m.group("args").split()
                directory = None
                k = 0
                while k < len(args):
                    a = args[k]
                    if a in {"-C", "--directory", "-f", "--file", "--justfile",
                             "--working-directory", "-d"} and k + 1 < len(args):
                        if a in {"-C", "--directory", "-d", "--working-directory"}:
                            directory = args[k + 1].rstrip("/")
                        k += 2
                        continue
                    if a.startswith("-") or "=" in a:
                        k += 1
                        continue
                    if TARGET.match(a) and not any(c in a for c in "<>{}$*"):
                        add(a, {"kind": kind, "dir": directory})
                    else:
                        break
                    k += 1
    return out


# --------------------------------------------------------------------- TOOL
TOOL_CMD = re.compile(r"(?:^|[\s;&|(`$])(npm|pnpm|yarn|bun|pip3?|uv|poetry)\s+(?P<sub>[a-z][\w-]*)")
TOOL_SUBCOMMANDS = {
    "npm": {"install", "i", "ci", "run", "test", "start", "add", "exec", "publish", "update"},
    "pnpm": {"install", "i", "add", "run", "dev", "build", "test", "exec", "dlx", "lint"},
    "yarn": {"install", "add", "run", "dev", "build", "test", "lint", "start", "workspace"},
    "bun": {"install", "i", "add", "run", "test", "x", "build", "dev"},
    "pip": {"install", "uninstall", "freeze"},
    "uv": {"sync", "run", "add", "pip", "lock", "venv", "remove", "tool"},
    "poetry": {"install", "run", "add", "lock", "shell", "update", "build"},
}


def extract_tools(text: str, context: str) -> list[tuple[str, dict[str, Any]]]:
    out, seen = [], set()
    for m in TOOL_CMD.finditer(text):
        tool, sub = m.group(1), m.group("sub")
        tool = "pip" if tool == "pip3" else tool
        # "uv pip install" implies uv, not pip.
        if tool == "pip" and text[:m.start(1)].rstrip().endswith("uv"):
            continue
        # In prose, require a recognised subcommand ("pip install", not "yarn a lot").
        if context == "plain" and sub not in TOOL_SUBCOMMANDS[tool]:
            continue
        if tool not in seen:
            seen.add(tool)
            out.append((tool, {"subcommand": sub}))
    return out


# -------------------------------------------------------------------- IDENT
CAMEL = re.compile(r"^(?:[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*|[a-z][a-z0-9]*[A-Z][A-Za-z0-9]*)$")
SNAKE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+$")
DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")


def extract_ident(span: str, ec: ExtractConfig) -> tuple[str, str] | None:
    """Identifier from an inline code span with no whitespace. Returns (value, kind)."""
    s = span.strip()
    if not s or any(c.isspace() for c in s) or s.startswith("-"):
        return None
    s = re.sub(r"\(.*\)$", "", s)          # foo() / foo(bar)
    s = re.sub(r"<[^<>]*>$", "", s)        # Generic<T>
    s = s.rstrip(":;,.")
    if len(s) < ec.ident_min_length or s in ec.ident_stoplist:
        return None
    if DOTTED.match(s):
        if _has_known_ext(s, ec):
            return None                    # it's a filename, handled as PATH
        return s, "dotted"
    if SNAKE.match(s):
        return s, "snake"
    if CAMEL.match(s):
        return s, "camel"
    return None


# ------------------------------------------------------------------- driver
def extract_references(content: str, ec: ExtractConfig) -> tuple[list[Ref], list[Ref]]:
    """Return (references, globs) for one version of a context file."""
    refs: list[Ref] = []
    globs: list[Ref] = []
    seen: set[tuple[int, str, str]] = set()
    types = ec.reference_types

    def add(r: Ref) -> None:
        key = (r.line_no, r.ref_type, r.ref_value)
        if key in seen:
            return
        seen.add(key)
        (globs if r.ref_type == "GLOB" else refs).append(r)

    for line_no, line, context, text in iter_segments(content):
        line_text = line.rstrip("\r\n")
        if "PATH" in types:
            for kind, value in extract_paths(text, context, ec):
                add(Ref(line_no, line_text, kind, value, context))
        if context in {"fence", "inline", "plain"}:
            if "SCRIPT" in types:
                for name, extra in extract_scripts(text, context, ec):
                    add(Ref(line_no, line_text, "SCRIPT", name, context, extra))
            if "TOOL" in types:
                for tool, extra in extract_tools(text, context):
                    add(Ref(line_no, line_text, "TOOL", tool, context, extra))
        if context == "inline" and "IDENT" in types:
            if classify_path_token(text, ec, context) is None:
                r = extract_ident(text, ec)
                if r:
                    add(Ref(line_no, line_text, "IDENT", r[0], context, {"ident_kind": r[1]}))
    return refs, globs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=int, default=0,
                    help="print N seeded random references (HUMAN CHECKPOINT 1) and exit")
    args = ap.parse_args()
    cfg = phase1_config()
    ref_path = DATA_INTERIM / "references.jsonl"

    if args.sample:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
        rows =[json.loads(ln) for ln in open(ref_path, encoding="utf-8")]
        rng = random.Random(cfg["seed"])
        for r in rng.sample(rows, min(args.sample, len(rows))):
            print(f"[{r['ref_type']:6}] {r['ref_value']!r}  ({r['context']}, {r['repo']} "
                  f"{r['path']} v{r['version_idx']} L{r['line_no']})")
            print(f"         line: {r['line_text'].strip()[:160]}")
        return 0

    ec = ExtractConfig.from_config(cfg)
    n_versions, counts, n_globs = 0, {}, 0
    with open(DATA_INTERIM / "versions.jsonl", encoding="utf-8") as fin, \
            open(ref_path, "w", encoding="utf-8") as fout, \
            open(DATA_INTERIM / "globs.jsonl", "w", encoding="utf-8") as fglob:
        for ln in fin:
            v = json.loads(ln)
            if v.get("is_alias") or v.get("deleted") or v.get("content") is None \
                    or v.get("duplicate_of_other_chain"):
                continue
            n_versions += 1
            refs, globs = extract_references(v["content"], ec)
            base = {"repo": v["repo"], "path": v["path"], "version_idx": v["version_idx"],
                    "commit": v["commit"]}
            for r in refs:
                fout.write(json.dumps({**base, **asdict(r)}) + "\n")
                counts[r.ref_type] = counts.get(r.ref_type, 0) + 1
            for g in globs:
                fglob.write(json.dumps({**base, **asdict(g)}) + "\n")
                n_globs += 1
    print(f"versions processed: {n_versions}")
    print("references per type:", dict(sorted(counts.items())))
    print(f"globs logged (excluded): {n_globs}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
