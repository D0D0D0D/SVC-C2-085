from __future__ import annotations

import io
import zipfile


from ingest_file_sanitizer import TextQualityPolicy, assess_text_quality, sanitize_file_bytes, sniff_channel


def test_magic_bytes_reject_pdf_even_when_declared_csv():
    result = sanitize_file_bytes(
        b"%PDF-1.7 fake",
        declared_name="records.csv",
        declared_content_type="text/csv",
    )

    assert not result.accepted
    assert result.rejected[0]["reason"] == "unknown or unsupported input format (allowlist reject)"


def test_valid_csv_is_accepted_and_formula_cells_are_neutralized():
    raw = "id,event\n1,normal text\n2,=cmd|calc\n".encode()

    result = sanitize_file_bytes(raw)

    assert result.accepted
    assert result.channel == "csv"
    assert result.sanitized_rows[1]["event"] == "'=cmd|calc"


def test_unknown_binary_is_rejected_not_passed_through():
    result = sanitize_file_bytes(b"\x00\x01\x02\x03not allowed")

    assert not result.accepted
    assert result.sanitized_text is None
    assert result.sanitized_rows == []
    assert result.rejected


def test_text_quality_gate_rejects_mojibake_or_ocr_collapse():
    raw = ("id,event\n1," + ("\ufffd" * 30) + "\n").encode()

    result = sanitize_file_bytes(raw)

    assert not result.accepted
    assert "text quality rejected" in result.reject_reason
    assert result.rejected


def test_text_quality_gate_keeps_legitimate_japanese_text_unchanged():
    raw = "id,event\n1,充填工程で温度逸脱を検知\n".encode()

    result = sanitize_file_bytes(raw)

    assert result.accepted
    assert result.sanitized_rows == [{"id": "1", "event": "充填工程で温度逸脱を検知"}]


def test_real_docx_text_is_extracted_and_quality_checked():
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>逸脱番号 DEV-001</w:t></w:r></w:p></w:body></w:document>"
    ).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", document_xml)

    result = sanitize_file_bytes(buf.getvalue())

    assert result.accepted
    assert result.channel == "docx"
    assert result.sanitized_text == "逸脱番号 DEV-001"


def test_allowed_channels_and_quality_thresholds_are_configurable():
    assert sniff_channel(b"a,b\n1,2\n", declared_name="fake.pdf") == "csv"
    ok, reason = assess_text_quality("----", TextQualityPolicy(min_signal_ratio=0.9))
    assert not ok
    assert reason == "low_text_signal"

    result = sanitize_file_bytes(b"a,b\n1,2\n", allowed_channels=("docx",))
    assert not result.accepted
    assert "not allowed" in result.reject_reason


def _docx_bytes(parts: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in parts.items():
            zf.writestr(name, content)
    return buf.getvalue()


_SAFE_DOCUMENT_XML = (
    '<?xml version="1.0"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body><w:p><w:r><w:t>逸脱番号 DEV-001</w:t></w:r></w:p></w:body></w:document>"
)


def test_docx_with_external_entity_is_rejected_not_expanded():
    """XXE を含む docx は reject する（defusedxml が実際に効いていることの証明）。

    この経路は `importorskip` で長らく skip されており、一度も走っていなかった。
    """
    xxe = (
        '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:body></w:document>"
    )
    result = sanitize_file_bytes(
        _docx_bytes({"[Content_Types].xml": "<Types/>", "word/document.xml": xxe}),
        declared_name="x.docx",
    )

    assert not result.accepted
    assert "unsafe XML" in result.rejected[0]["reason"]
    assert "/etc/passwd" not in str(result.sanitized_rows)


def test_docx_without_document_xml_is_rejected_with_reason():
    result = sanitize_file_bytes(
        _docx_bytes({"[Content_Types].xml": "<Types/>", "word/other.xml": _SAFE_DOCUMENT_XML}),
        declared_name="x.docx",
    )

    assert not result.accepted
    assert "missing document.xml" in result.rejected[0]["reason"]


def test_corrupt_zip_declared_as_docx_is_rejected_without_raising():
    """壊れた zip は例外を外へ漏らさず、理由付きで reject する。"""
    result = sanitize_file_bytes(b"PK\x03\x04broken-not-a-zip-at-all", declared_name="x.docx")

    assert not result.accepted
    assert result.rejected[0]["reason"]
