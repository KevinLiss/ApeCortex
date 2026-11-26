"""文档文件解析（N1）——上传文件 → 纯文本/Markdown。

支持：.md / .markdown / .txt（直接读取）、.pdf（pypdf）、.docx（python-docx）。
全部失败/不识别抛 ValueError（上层转 400 给用户）。

.. design:: 依赖策略
   pypdf 与 python-docx 均为宽松许可的纯解析库，进主依赖；
   无混淆开销，导入放在函数内（未用到格式时零导入成本）。
"""
import io
import re

_TEXT_EXTS = {".md", ".markdown", ".txt"}
_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10MB 上限


def parse_file_to_text(filename: str, data: bytes) -> tuple[str, str]:
    """解析上传文件 → (title, content)。title 取文件名主干。"""
    if len(data) > _MAX_FILE_BYTES:
        raise ValueError("文件过大（上限 10MB）")
    if not data:
        raise ValueError("文件为空")

    stem = re.sub(r"\.[^.]+$", "", filename or "未命名文档").strip() or "未命名文档"
    ext = ""
    if "." in (filename or ""):
        ext = filename.rsplit(".", 1)[1].lower()

    if f".{ext}" in _TEXT_EXTS:
        text = data.decode("utf-8", errors="ignore").strip()
        if not text:
            raise ValueError("文件内容为空")
        return stem[:200], text

    if ext == "pdf":
        return stem[:200], _parse_pdf(data)
    if ext == "docx":
        return stem[:200], _parse_docx(data)

    raise ValueError(f"暂不支持的文件类型：.{ext or '?'}（支持 md/txt/pdf/docx）")


def _parse_pdf(data: bytes) -> str:
    from pypdf import PdfReader  # noqa: PLC0415 — 按需导入

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"PDF 无法解析：{exc}") from exc

    parts: list[str] = []
    for i, page in enumerate(reader.pages, 1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:  # noqa: BLE001 — 单页失败跳过，不废整个文件
            continue
        if text:
            parts.append(f"## 第 {i} 页\n\n{text}")
    if not parts:
        raise ValueError("PDF 未抽取到文本（可能是扫描件/图片型 PDF）")
    return "\n\n".join(parts)


def _parse_docx(data: bytes) -> str:
    import docx  # noqa: PLC0415 — 按需导入

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"DOCX 无法解析：{exc}") from exc

    blocks: list[str] = []
    for para in document.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = (para.style.name or "").lower()
        if "heading" in style and "title" not in style:
            m = re.search(r"(\d+)$", style)
            level = int(m.group(1)) if m else 2
            blocks.append(f"{'#' * max(1, min(level, 6))} {text}")
        else:
            blocks.append(text)

    for table in document.tables:
        rows = [
            [c.text.strip().replace("|", "\\|") for c in row.cells]
            for row in table.rows
        ]
        if not rows:
            continue
        width = max(len(r) for r in rows)
        md_rows = ["| " + " | ".join(r + [""] * (width - len(r))) + " |" for r in rows]
        blocks.append("\n".join([
            md_rows[0],
            "|" + "---|" * width,
            *md_rows[1:],
        ]))

    if not blocks:
        raise ValueError("DOCX 未抽取到文本")
    return "\n\n".join(blocks)
