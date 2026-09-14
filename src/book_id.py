"""Per-book identity file (`book.json`) for OCR output folders.

`pages/page_0000.md` cannot prove which PDF it came from: two books OCRed into
the same parent directory look identical, and the dual-layer step would happily
build one book's pages onto another book's raster. `book.json` binds a book
folder to its source PDF (absolute path + size + page count) so the binding can
be checked *before* anything is built, and so a parent directory can be resolved
back to exactly one book.

Credential red line: this file sits in the user's own output directory, but it is
still treated as untrusted storage -- `_strip_secrets` drops credential-shaped
keys and values on every read and write, and nothing in this module ever accepts
an API key. `engine` records only model + endpoint (never `base_url`: a gateway
URL can embed credentials or a private address).
"""

from __future__ import annotations

import json
import os
import re
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = "llm-ocr-book-v1"
BOOK_FILE = "book.json"

_PAGE_FILE_RE = re.compile(r"^page_(\d+)\.md$")
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|apikey|secret|token|passwd|password|credential|authorization|bearer|cookie)",
    re.IGNORECASE,
)
# Value shapes that are credentials no matter what the key is called.
_SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|ghp_[A-Za-z0-9]{8,}|xox[baprs]-[A-Za-z0-9\-]{8,}"
    r"|AKIA[0-9A-Z]{12,}|Bearer\s+[A-Za-z0-9._\-]{8,})"
)

STATUSES = ("bound", "unbound", "ambiguous", "not_found", "mismatch", "error")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def book_json_path(book_dir: str | Path) -> Path:
    return Path(book_dir) / BOOK_FILE


def _strip_secrets(record: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Copy of `record` with every credential-shaped key or value removed."""
    clean: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in record.items():
        if _SECRET_KEY_RE.search(str(key)):
            dropped.append(str(key))
            continue
        if isinstance(value, dict):
            value, sub = _strip_secrets(value)
            dropped.extend(f"{key}.{name}" for name in sub)
        elif isinstance(value, str) and _SECRET_VALUE_RE.search(value):
            dropped.append(f"{key} (credential-shaped value)")
            continue
        clean[key] = value
    return clean, dropped


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # Odd filesystem (e.g. exFAT quirks): a direct write beats losing the record.
        path.write_text(text, encoding="utf-8")
        try:
            tmp.unlink()
        except OSError:
            pass


def read_book(book_dir: str | Path) -> tuple[dict[str, Any] | None, str | None]:
    """Return `(record, error)`; a missing file is `(None, None)`, broken content is an error."""
    path = book_json_path(book_dir)
    if not path.is_file():
        return None, None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"读取 {path.name} 失败：{exc}"
    except UnicodeDecodeError as exc:
        return None, f"{path.name} 不是 UTF-8 文本：{exc}"
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"{path.name} 不是合法 JSON：{exc}"
    if not isinstance(record, dict):
        return None, f"{path.name} 顶层不是 JSON 对象，无法作为书籍身份文件"
    clean, dropped = _strip_secrets(record)
    if dropped:
        warnings.warn(f"{path}: dropped credential-shaped fields {dropped}", UserWarning)
    return clean, None


def _same_path(a: Any, b: Any) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(os.path.abspath(str(b)))


def _cmp(recorded: Any, actual: Any) -> str:
    """`same` / `diff` / `unknown` -- missing record fields never claim a mismatch."""
    if recorded is None or actual is None:
        return "unknown"
    return "same" if recorded == actual else "diff"


def source_facts(pdf_path: str | Path) -> dict[str, Any]:
    """Absolute path + name/stem + size + mtime; size/mtime stay None if unreadable."""
    path = Path(pdf_path)
    try:
        absolute = str(path.resolve())
    except OSError:
        absolute = str(path.absolute())
    facts: dict[str, Any] = {
        "source_pdf": absolute,
        "source_name": path.name,
        "source_stem": path.stem,
    }
    try:
        stat = path.stat()
        facts["source_size"] = int(stat.st_size)
        facts["source_mtime"] = float(stat.st_mtime)
    except OSError:
        pass
    return facts


def pdf_page_count(pdf_path: str | Path) -> int | None:
    """Page count via PyMuPDF; None when the file is missing or unreadable."""
    try:
        import fitz

        with fitz.open(str(pdf_path)) as document:
            return int(document.page_count)
    except Exception:  # noqa: BLE001 - fitz raises many shapes; "unknown" is the answer
        return None


def page_numbers(pages_dir: str | Path) -> list[int]:
    """Sorted 0-based page numbers that have a non-empty `page_<n>.md` on disk."""
    source = Path(pages_dir)
    numbers: list[int] = []
    if not source.is_dir():
        return numbers
    for path in source.glob("page_*.md"):
        match = _PAGE_FILE_RE.match(path.name)
        if match is None:
            continue
        try:
            if path.stat().st_size > 0:
                numbers.append(int(match.group(1)))
        except OSError:
            continue
    return sorted(set(numbers))


def update_book(
    book_dir: str | Path,
    *,
    source_pdf: str | Path | None = None,
    page_count: int | None = None,
    pages_done: list[int] | None = None,
    merged_md: str | Path | None = None,
    searchable_pdf: str | Path | None = None,
    model: str | None = None,
    endpoint: str | None = None,
) -> dict[str, Any]:
    """Create or idempotently refresh `<book_dir>/book.json`.

    Re-running the same book refreshes `updated` / `pages_done` / `artifacts` and
    keeps everything else (including `created` and any unknown fields). A folder
    re-bound to a different PDF keeps the old path in `source_pdf_previous` and
    warns instead of silently swapping identity.
    """
    root = Path(book_dir)
    existing, error = read_book(root)
    if error:
        warnings.warn(f"{book_json_path(root)} unreadable ({error}); rebuilding it", UserWarning)
        existing = None
    record: dict[str, Any] = dict(existing or {})
    created = record.get("created") or _now_iso()

    if source_pdf is not None:
        facts = source_facts(source_pdf)
        previous = record.get("source_pdf")
        if previous and not _same_path(previous, facts["source_pdf"]):
            record["source_pdf_previous"] = previous
            warnings.warn(
                f"book folder {root} was bound to {previous}, now {facts['source_pdf']}",
                UserWarning,
            )
        record.update(facts)

    if page_count is not None:
        record["page_count"] = int(page_count)
    if pages_done is not None:
        record["pages_done"] = sorted({int(p) for p in pages_done})

    artifacts = dict(record.get("artifacts") or {})
    artifacts.setdefault("merged_md", None)
    artifacts.setdefault("searchable_pdf", None)
    if merged_md:
        artifacts["merged_md"] = Path(merged_md).name
    if searchable_pdf:
        artifacts["searchable_pdf"] = Path(searchable_pdf).name
    record["artifacts"] = artifacts

    engine = dict(record.get("engine") or {})
    if model:
        engine["model"] = model
    if endpoint:
        engine["endpoint"] = endpoint
    record["engine"] = engine

    record["schema"] = SCHEMA
    record["created"] = created
    record["updated"] = _now_iso()

    clean, dropped = _strip_secrets(record)
    if dropped:
        warnings.warn(f"{book_json_path(root)}: dropped credential-shaped fields {dropped}", UserWarning)
    _write_json(book_json_path(root), clean)
    return clean


def _result(status: str, **fields: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": status,
        "book_dir": None,
        "pages_dir": None,
        "source_pdf": None,
        "recorded_pdf": None,
        "record": None,
        "candidates": [],
        "warnings": [],
        "message": "",
    }
    out.update(fields)
    return out


def _candidate(book_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    done = record.get("pages_done")
    return {
        "book_dir": str(book_dir),
        "source_pdf": record.get("source_pdf"),
        "source_name": record.get("source_name") or book_dir.name,
        "page_count": record.get("page_count"),
        "pages_done": len(done) if isinstance(done, list) else None,
        "updated": record.get("updated"),
    }


def _quality(record: dict[str, Any], facts: dict[str, Any] | None, pages: int | None) -> int:
    """3 = same file, 2 = same size *and* page count, 0 = not provably this book."""
    if facts is None:
        return 0
    if _same_path(record.get("source_pdf"), facts.get("source_pdf")):
        return 3
    if _cmp(record.get("source_size"), facts.get("source_size")) == "same" and _cmp(
        record.get("page_count"), pages
    ) == "same":
        return 2
    return 0


def _bind(
    book_dir: Path,
    record: dict[str, Any],
    facts: dict[str, Any] | None,
    pages: int | None,
) -> dict[str, Any]:
    """Verify an explicit PDF against the record and return the bound result."""
    warnings_out: list[str] = []
    recorded = record.get("source_pdf")
    if not record.get("source_pdf"):
        return _result(
            "mismatch",
            book_dir=str(book_dir),
            record=record,
            message=f"{book_dir} 的 book.json 没有记录源 PDF（字段缺失或损坏），无法校验它属于哪本书。",
        )

    if facts is None:
        return _result(
            "bound",
            book_dir=str(book_dir),
            pages_dir=str(book_dir / "pages"),
            source_pdf=recorded,
            recorded_pdf=recorded,
            record=record,
            message=f"已按 book.json 绑定：{record.get('source_name') or recorded}",
        )

    same_path = _same_path(recorded, facts.get("source_pdf"))
    size_cmp = _cmp(record.get("source_size"), facts.get("source_size"))
    pages_cmp = _cmp(record.get("page_count"), pages)
    shown = f"{record.get('source_name') or recorded}（记录 {record.get('page_count')} 页）"
    chosen = f"{facts.get('source_name')}（所选 {pages} 页）"

    if same_path and size_cmp == "same" and pages_cmp == "same":
        pass  # exact identity
    elif same_path:
        # A record written before size/page_count existed compares as "unknown":
        # there is nothing to contradict, so stay quiet instead of crying wolf.
        if size_cmp == "diff" or pages_cmp == "diff":
            warnings_out.append(
                f"源 PDF 路径与 book.json 一致，但页数/大小对不上（记录 {record.get('page_count')} 页/"
                f"{record.get('source_size')} 字节，现在 {pages} 页/{facts.get('source_size')} 字节）："
                "源文件可能被换过或改过，已按所选文件继续。"
            )
    elif size_cmp == "same" and pages_cmp == "same":
        warnings_out.append(
            f"所选 PDF 与 book.json 记录的路径不同，但大小与页数一致（可能被移动/复制过）："
            f"记录 {recorded}，所选 {facts.get('source_pdf')}。"
        )
    else:
        return _result(
            "mismatch",
            book_dir=str(book_dir),
            recorded_pdf=recorded,
            record=record,
            message=(
                f"所选 PDF 与这本书不是同一本，已拒绝构建以免用错书："
                f"{book_dir} 记录的是 {shown}，所选的是 {chosen}。"
            ),
        )

    done = record.get("pages_done")
    if isinstance(done, list) and done and isinstance(record.get("page_count"), int):
        if max(int(p) for p in done) + 1 > int(record["page_count"]):
            warnings_out.append(
                f"{book_dir}/pages 里已有第 {max(int(p) for p in done) + 1} 页，"
                f"超过 book.json 记录的 {record['page_count']} 页，这本文件夹可能混入了别的书。"
            )

    return _result(
        "bound",
        book_dir=str(book_dir),
        pages_dir=str(book_dir / "pages"),
        source_pdf=facts.get("source_pdf") or recorded,
        recorded_pdf=recorded,
        record=record,
        warnings=warnings_out,
        message=f"已绑定：{record.get('source_name') or recorded}"
        + ("" if not warnings_out else "（有警告，请查看）"),
    )


def _scan_candidates(target: Path, hint: str | Path | None) -> list[tuple[Path, dict[str, Any]]]:
    """Every direct subdirectory (plus the hint dir) that carries a readable book.json."""
    dirs: list[Path] = []
    if hint:
        dirs.append(Path(hint))
    try:
        dirs.extend(sorted(p for p in target.iterdir() if p.is_dir()))
    except OSError:
        pass
    seen: set[str] = set()
    found: list[tuple[Path, dict[str, Any]]] = []
    for candidate in dirs:
        key = os.path.normcase(os.path.abspath(str(candidate)))
        if key in seen:
            continue
        seen.add(key)
        record, error = read_book(candidate)
        if record is not None and not error:
            found.append((candidate, record))
    return found


def resolve_book(
    target_dir: str | Path,
    pdf_path: str | Path | None = None,
    *,
    book_dir_hint: str | Path | None = None,
) -> dict[str, Any]:
    """Resolve the book folder behind `target_dir`, optionally verifying it against a PDF.

    Three accepted inputs, and one rule: never guess when it is not provable.
      * book folder (has `book.json`) -> bind it, filling in the source PDF;
      * legacy OCR dir (no `book.json`, has `pages/`) -> `unbound`, used as-is;
      * parent dir + a source PDF -> the one subdirectory whose record matches the
        PDF wins; no match or several matches is a loud `not_found`/`ambiguous`
        error that lists the candidates.
    """
    target = Path(target_dir)
    if not target.is_dir():
        return _result("error", message=f"目录不存在：{target}")

    explicit = str(pdf_path).strip() if pdf_path else ""
    facts: dict[str, Any] | None = None
    pages: int | None = None
    if explicit:
        if not Path(explicit).is_file():
            return _result("error", message=f"源 PDF 不存在：{explicit}")
        pages = pdf_page_count(explicit)
        if pages is None:
            return _result("error", message=f"源 PDF 打不开（不是有效的 PDF？）：{explicit}")
        facts = source_facts(explicit)

    record, error = read_book(target)
    if error:
        return _result("error", book_dir=str(target), message=error)
    if record is not None:
        return _bind(target, record, facts, pages)

    if (target / "pages").is_dir():
        return _result(
            "unbound",
            book_dir=str(target),
            pages_dir=str(target / "pages"),
            source_pdf=explicit or None,
            message=(
                f"{target} 没有身份文件 book.json：无法校验它的 pages/ 是否属于所选 PDF，"
                "按旧行为直接使用该目录。"
            ),
        )

    candidates = _scan_candidates(target, book_dir_hint)
    listing = [_candidate(path, rec) for path, rec in candidates]
    if not candidates:
        return _result(
            "not_found",
            message=(
                f"{target} 下没有找到任何 book.json（也没有 pages/），"
                "它既不是书文件夹也不是 OCR 输出目录。"
            ),
        )

    if not explicit:
        if len(candidates) == 1:
            path, rec = candidates[0]
            return _bind(path, rec, None, None)
        return _result(
            "ambiguous",
            candidates=listing,
            message=f"{target} 下有 {len(candidates)} 本书，请再选择一次源 PDF 以确定用哪一本。",
        )

    scored = sorted(
        ((_quality(rec, facts, pages), path, rec) for path, rec in candidates),
        key=lambda item: item[0],
        reverse=True,
    )
    if scored[0][0] == 0:
        return _result(
            "not_found",
            candidates=listing,
            message=(
                f"{target} 下没有任何一本书的 book.json 与所选 PDF "
                f"（{facts.get('source_name')}）匹配，已拒绝构建以免用错书。"
            ),
        )
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return _result(
            "ambiguous",
            candidates=listing,
            message=(
                f"{target} 下有 {sum(1 for item in scored if item[0] == scored[0][0])} 本书"
                "都能匹配所选 PDF，无法确定用哪一本，已拒绝构建。"
            ),
        )
    return _bind(scored[0][1], scored[0][2], facts, pages)
