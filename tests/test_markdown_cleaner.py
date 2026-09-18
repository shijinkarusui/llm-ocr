from __future__ import annotations

import re
from pathlib import Path

import pytest

from src import batch_plan, ocr_page
from src.markdown_cleaner import (
    clean_merged_book,
    clean_single_page,
    merge_page_documents,
)


def test_single_page_normalizes_lines_images_and_protected_syntax() -> None:
    raw = (
        "中文,测试  \r\n\r\n\r\n"
        "![local](missing/page.png)  \r\n"
        "远程 ![remote](https://example.com/page.png)\r"
        "\n值是 3.14。\r\n"
        "`代码,不改`\r\n"
        "```\r\n![in-code](missing.png)\r\n```\r\n"
        "$x,y$\r\n"
        "· 12 ·  \r\n"
    )

    cleaned = clean_single_page(raw)

    assert "\r" not in cleaned
    assert "\n\n\n" not in cleaned
    assert "中文，测试" in cleaned
    assert "[图片]" in cleaned
    assert "https://example.com/page.png" in cleaned
    assert "`代码,不改`" in cleaned
    assert "![in-code](missing.png)" in cleaned
    assert "$x,y$" in cleaned
    assert "3.14" in cleaned
    assert "· 12 ·" in cleaned
    assert clean_single_page(cleaned) == cleaned


def test_single_page_does_not_reconstruct_page_structure() -> None:
    raw = "正文中的页码：第 42 页\n\n---\n\n合法横线后的文字"

    cleaned = clean_single_page(raw)

    assert "正文中的页码：第 42 页" in cleaned
    assert cleaned.count("---") == 1
    assert "第 42 页\n\n正文" not in cleaned


def test_merged_book_preserves_page_references_inside_body() -> None:
    raw = """# 测试书

---
第 1 页

正文提到第 12 页，但这不是新的页块。

---

---
第 2 页

第二页正文。

---
"""

    cleaned = clean_merged_book(raw)

    assert "正文提到第 12 页，但这不是新的页块。" in cleaned
    assert len(re.findall(r"^第 \d+ 页$", cleaned, re.MULTILINE)) == 2
    assert clean_merged_book(cleaned) == cleaned


def test_merged_book_converts_legacy_markers_and_removes_index() -> None:
    raw = """# 测试书

## Page Index
- Page 2: PAGE 2
- Page 5: PAGE 5

<!-- PAGE 5 -->

第 5 页

第五,页

<!-- PAGE 2 -->

第 2 页

第二,页
"""

    cleaned = clean_merged_book(raw)

    assert "## Page Index" not in cleaned
    assert "<!-- PAGE" not in cleaned
    assert cleaned.index("第 2 页") < cleaned.index("第 5 页")
    assert "---\n第 2 页\n\n第二，页\n\n---" in cleaned
    assert "---\n第 5 页\n\n第五，页\n\n---" in cleaned
    assert not re.search(r"第 \d+ 页\n\n---", cleaned)
    assert clean_merged_book(cleaned) == cleaned


def test_merged_book_without_page_blocks_still_removes_old_index() -> None:
    raw = """# 测试书

## Page Index
- Page 1: PAGE 1
- Page 2: PAGE 2

正文,内容
"""

    cleaned = clean_merged_book(raw)

    assert cleaned == "# 测试书\n\n正文，内容"
    assert "## Page Index" not in cleaned
    assert "PAGE 1" not in cleaned

def test_merge_page_documents_is_sorted_and_does_not_fill_gaps() -> None:
    merged = merge_page_documents(
        {
            5: "第五,页",
            2: "---\n第 2 页\n\n第二,页\n\n---",
        },
        "语音学,教程",
    )

    assert merged.startswith("# 语音学，教程\n")
    assert merged.index("第 2 页") < merged.index("第 5 页")
    assert "第 3 页" not in merged
    assert "第 4 页" not in merged
    assert "[原书排版或插页]" not in merged
    assert "[EMPTY PAGE]" not in merged
    assert "第 2 页\n\n第二，页\n\n---" in merged
    assert clean_merged_book(merged) == merged


def test_empty_page_keeps_only_canonical_boundary() -> None:
    merged = merge_page_documents({4: ""}, "书")

    assert merged == "# 书\n\n---\n第 4 页\n\n---\n"
    assert "[EMPTY PAGE]" not in merged
    assert clean_merged_book(merged) == merged


def test_merge_book_markdown_reads_existing_pages_and_final_cleans(tmp_path: Path) -> None:
    pdf = tmp_path / "我的书.pdf"
    root = tmp_path / "book"
    pages = root / "pages"
    pages.mkdir(parents=True)
    (pages / "page_0002.md").write_text("第三,页  \n", encoding="utf-8")
    (pages / "page_0000.md").write_text("第一,页\n", encoding="utf-8")

    target = batch_plan.merge_book_markdown(pdf, root)

    assert target == root / "我的书-ocr.md"
    output = target.read_text(encoding="utf-8")
    assert output.index("第 1 页") < output.index("第 3 页")
    assert "第一，页" in output and "第三，页" in output
    assert "第 2 页" not in output
    assert "## Page Index" not in output
    assert clean_merged_book(output) == output


def test_process_one_writes_cleaned_output_before_usage_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "pages" / "page_0000.md"
    usage = tmp_path / "usage.jsonl"
    item = {"pno_0based": 0, "page_number": 1, "output": output}

    monkeypatch.setattr(
        batch_plan,
        "_resolve_run",
        lambda **kwargs: {
            "base_url": "http://gateway/v1",
            "timeout": 120,
            "model": "model",
            "key": "key",
            "requested": "chat",
            "detail": "high",
        },
    )
    monkeypatch.setattr(batch_plan, "_ladder_render", lambda *args, **kwargs: (b"png", 200, 3, 4))
    monkeypatch.setattr(
        batch_plan,
        "chat_vision",
        lambda *args, **kwargs: ("中文,页  \n![x](missing.png)", {"total_tokens": 7}),
    )

    result = batch_plan._process_one(
        item,
        tmp_path / "book.pdf",
        "prompt",
        200,
        usage,
        0,
        base_url="http://gateway/v1",
        model="model",
        api_key="key",
        endpoint="chat",
        detail="high",
    )

    assert result["status"] == "success"
    assert output.read_text(encoding="utf-8") == "中文，页\n[图片]\n"
    assert '"status": "success"' in usage.read_text(encoding="utf-8")


def test_ocr_image_url_and_pdf_page_return_cleaned_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ocr_page,
        "_resolve_ocr",
        lambda **kwargs: ("http://gateway/v1", "model", "key", "chat", "high"),
    )
    monkeypatch.setattr(
        ocr_page,
        "chat_vision",
        lambda *args, **kwargs: ("中文,页  ", {}),
    )
    monkeypatch.setattr(
        ocr_page,
        "chat_vision_url",
        lambda *args, **kwargs: ("远程,页  ", {}),
    )
    monkeypatch.setattr(ocr_page, "_ladder_or_direct", lambda *args, **kwargs: (b"png", 200, 3, 4))

    assert ocr_page.ocr_image(b"png", prompt="prompt") == "中文，页"
    assert ocr_page.ocr_image_url("https://example.com/x.png", prompt="prompt") == "远程，页"
    assert ocr_page.ocr_pdf_page("book.pdf", 0, prompt="prompt") == "中文，页"
