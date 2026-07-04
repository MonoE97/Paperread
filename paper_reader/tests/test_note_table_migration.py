from paper_reader.note_table_migration import (
    classify_note_content,
    convert_note_tables_to_html,
    has_markdown_table_separator,
    note_content_hash,
)


def test_classify_plain_markdown_with_table() -> None:
    content = "# Note\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n"

    assert classify_note_content(content) == "plain_markdown"


def test_classify_html_with_markdown_table_text() -> None:
    content = "<h1>Note</h1><p>| A | B |<br>| --- | --- |<br>| 1 | 2 |</p>"

    assert classify_note_content(content) == "html_with_markdown_tables"


def test_classify_already_html_table() -> None:
    content = "<h1>Note</h1><table><tbody><tr><td>1</td></tr></tbody></table>"

    assert classify_note_content(content) == "already_html_table"


def test_classify_no_markdown_tables() -> None:
    content = "<p>This note has a pipe | but no table separator.</p>"

    assert classify_note_content(content) == "no_markdown_tables"


def test_detects_markdown_table_separator_only_when_separator_line_exists() -> None:
    assert has_markdown_table_separator("| A | B |\n| --- | --- |\n| 1 | 2 |") is True
    assert has_markdown_table_separator("a | b but no separator") is False


def test_convert_plain_markdown_table_to_html_table() -> None:
    content = "# Note\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n"

    result = convert_note_tables_to_html(content)

    assert result.status == "converted"
    assert result.content_type == "plain_markdown"
    assert "<table>" in result.content
    assert "<th>A</th>" in result.content
    assert "<td>1</td>" in result.content
    assert "| --- | --- |" not in result.content


def test_convert_html_paragraph_markdown_table_without_escaping_existing_heading() -> None:
    content = "<h1>Note</h1><p>| A | B |<br>| --- | --- |<br>| 1 | 2 |</p>"

    result = convert_note_tables_to_html(content)

    assert result.status == "converted"
    assert result.content_type == "html_with_markdown_tables"
    assert "<h1>Note</h1>" in result.content
    assert "<table>" in result.content
    assert "<th>A</th>" in result.content
    assert "<td>2</td>" in result.content
    assert "| --- | --- |" not in result.content


def test_skip_already_html_table() -> None:
    content = "<h1>Note</h1><table><tbody><tr><td>1</td></tr></tbody></table>"

    result = convert_note_tables_to_html(content)

    assert result.status == "skipped"
    assert result.content_type == "already_html_table"
    assert result.content == content


def test_block_unknown_html_mixed_table_text() -> None:
    content = "<span>| A | B |<br>| --- | --- |<br>| 1 | 2 |</span>"

    result = convert_note_tables_to_html(content)

    assert result.status == "blocked"
    assert result.content_type == "html_with_markdown_tables"
    assert "unsupported_table_container:span" in result.reason


def test_note_content_hash_is_stable_sha256() -> None:
    assert note_content_hash("abc") == (
        "ba7816bf8f01cfea414140de5dae2223"
        "b00361a396177a9cb410ff61f20015ad"
    )
