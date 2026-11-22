"""Experience cards: one small markdown file per lesson; INDEX.md is generated, never hand-edited."""

from __future__ import annotations

import re
from pathlib import Path

from myflow.core import PKG_ROOT, MfError, atomic_write, setting

KINDS = ("technique", "pitfall", "dead-end", "measurement")
REQUIRED = ("name", "kind", "summary", "applies", "confidence", "evidence")
MAX_BODY_LINES = 40
_ABS = re.compile(r"\b\d+(\.\d+)?\s?(us|µs|ms|TB/s|GB/s|TFLOPS|TF)\b")


def kb_dir() -> Path:
    """Where the experience cards live: MYFLOW_KB > `mf config kb=…` > the cards shipped with myflow."""
    return setting("kb")[0]


def parse(path: Path):
    text = path.read_text(encoding="utf-8")
    meta, body = {}, text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end > 0:
            for line in text[4:end].splitlines():
                if ":" in line and not line.lstrip().startswith("#"):
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            body = text[end + 4:].lstrip("\n")
    return meta, body


def cards() -> list:
    d = kb_dir() / "cards"
    return sorted(d.glob("*.md")) if d.is_dir() else []


def lint() -> list:
    problems = []
    seen = {}
    for p in cards():
        meta, body = parse(p)
        for k in REQUIRED:
            if not meta.get(k):
                problems.append(f"{p.name}: missing '{k}'")
        if meta.get("name") and meta["name"] != p.stem:
            problems.append(f"{p.name}: name '{meta['name']}' != file name")
        if meta.get("kind") and meta["kind"] not in KINDS:
            problems.append(f"{p.name}: kind must be one of {KINDS}")
        if len(meta.get("summary", "")) > 140:
            problems.append(f"{p.name}: summary > 140 chars (keep it one line)")
        n = len([x for x in body.splitlines() if x.strip()])
        if n > MAX_BODY_LINES:
            problems.append(f"{p.name}: body has {n} lines > {MAX_BODY_LINES}; split or trim")
        if _ABS.search(body) and "absolute-ok" not in meta.get("notes", ""):
            problems.append(f"{p.name}: (warn) absolute timings found; prefer ratios/% of peak, or add 'notes: absolute-ok'")
        key = meta.get("summary", "").lower()[:40]
        if key and key in seen:
            problems.append(f"{p.name}: summary looks like a duplicate of {seen[key]} (merge them)")
        seen[key] = p.name
    return problems


def index() -> Path:
    rows = []
    for p in cards():
        meta, _ = parse(p)
        rows.append((meta.get("kind", "?"), meta.get("name", p.stem), meta.get("summary", ""), meta.get("applies", ""),
                     meta.get("confidence", "")))
    rows.sort()
    lines = ["# 经验卡索引（生成文件：`mf kb check`，不要手改）", "",
             "先读这张表，按语义挑相关卡片再打开；卡片是先验，最终以本机测量为准。", "",
             "| kind | card | summary | applies | conf |", "|---|---|---|---|---|"]
    for kind, name, summ, applies, conf in rows:
        lines.append(f"| {kind} | [{name}](cards/{name}.md) | {summ.replace('|', '/')} | {applies.replace('|', '/')} | {conf} |")
    lines.append("")
    path = kb_dir() / "INDEX.md"
    atomic_write(path, "\n".join(lines))
    return path


def search(words) -> list:
    words = [w.lower() for w in words]
    hits = []
    for p in cards():
        text = p.read_text(encoding="utf-8").lower()
        if all(w in text for w in words):
            meta, _ = parse(p)
            hits.append(f"{meta.get('name', p.stem)} [{meta.get('kind', '?')}] {meta.get('summary', '')}  ({p})")
    return hits


def new(slug: str, kind: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,63}", slug):
        raise MfError("card name: lowercase-with-hyphens, 3-64 chars")
    if kind not in KINDS:
        raise MfError(f"kind must be one of {KINDS}")
    path = kb_dir() / "cards" / f"{slug}.md"
    if path.exists():
        raise MfError(f"{path} exists: update it instead of creating a duplicate")
    tpl = (PKG_ROOT / "templates" / "card.md").read_text(encoding="utf-8")
    atomic_write(path, tpl.replace("{name}", slug).replace("{kind}", kind))
    return path
