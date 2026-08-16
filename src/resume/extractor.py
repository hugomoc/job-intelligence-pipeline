"""In-memory PDF/DOCX resume extraction.

Uploaded resumes are read into text and hashed, but the original file and raw
text are not persisted. Downstream steps cache only the structured AI profile.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from docx import Document
from pypdf import PdfReader


SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".docx",
}

SUPPORTED_MIME_TYPES = {
    "application/pdf": ".pdf",
    (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document"
    ): ".docx",
}

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024


class UploadedFileProtocol(Protocol):
    """
    Interface compatible with Streamlit's UploadedFile.
    """

    name: str
    type: str

    def getvalue(self) -> bytes:
        ...


@dataclass(frozen=True)
class ExtractedResume:
    filename: str
    file_extension: str
    mime_type: str | None
    file_size_bytes: int
    page_count: int | None
    word_count: int
    resume_hash: str
    text: str


class ResumeExtractionError(Exception):
    """Raised when a resume cannot be extracted."""


def normalize_resume_text(text: str) -> str:
    """
    Cleans extracted resume text while preserving paragraph
    and section boundaries.
    """
    if not text:
        return ""

    normalized = text.replace("\r\n", "\n")
    normalized = normalized.replace("\r", "\n")

    normalized = normalized.replace("\u00a0", " ")
    normalized = normalized.replace("\u200b", "")
    normalized = normalized.replace("\ufeff", "")

    lines: list[str] = []

    for line in normalized.splitlines():
        cleaned_line = re.sub(
            r"[ \t]+",
            " ",
            line,
        ).strip()

        lines.append(cleaned_line)

    # Collapse more than two blank lines into one blank line.
    normalized = "\n".join(lines)
    normalized = re.sub(
        r"\n{3,}",
        "\n\n",
        normalized,
    )

    return normalized.strip()


def create_resume_hash(file_bytes: bytes) -> str:
    """
    Creates a one-way identifier for caching AI results.

    The original file does not need to be stored.
    """
    return hashlib.sha256(
        file_bytes
    ).hexdigest()


def get_file_extension(
    filename: str,
    mime_type: str | None = None,
) -> str:
    extension = Path(filename).suffix.casefold()

    if extension in SUPPORTED_EXTENSIONS:
        return extension

    if mime_type:
        mapped_extension = SUPPORTED_MIME_TYPES.get(
            mime_type.casefold()
        )

        if mapped_extension:
            return mapped_extension

    raise ResumeExtractionError(
        "Unsupported resume format. "
        "Please upload a PDF or DOCX file."
    )


def validate_file_size(file_bytes: bytes) -> None:
    if not file_bytes:
        raise ResumeExtractionError(
            "The uploaded resume is empty."
        )

    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        raise ResumeExtractionError(
            "The resume is larger than 10 MB."
        )


def extract_pdf_text(
    file_bytes: bytes,
) -> tuple[str, int]:
    try:
        reader = PdfReader(
            io.BytesIO(file_bytes)
        )
    except Exception as error:
        raise ResumeExtractionError(
            "The PDF could not be opened."
        ) from error

    if reader.is_encrypted:
        try:
            decrypt_result = reader.decrypt("")
        except Exception as error:
            raise ResumeExtractionError(
                "The PDF is password protected."
            ) from error

        if decrypt_result == 0:
            raise ResumeExtractionError(
                "The PDF is password protected."
            )

    page_text: list[str] = []

    for page_number, page in enumerate(
        reader.pages,
        start=1,
    ):
        try:
            text = page.extract_text() or ""
        except Exception as error:
            raise ResumeExtractionError(
                "Unable to extract text from "
                f"PDF page {page_number}."
            ) from error

        if text.strip():
            page_text.append(text)

    extracted_text = "\n\n".join(
        page_text
    )

    return extracted_text, len(reader.pages)


def extract_docx_text(
    file_bytes: bytes,
) -> tuple[str, int | None]:
    try:
        document = Document(
            io.BytesIO(file_bytes)
        )
    except Exception as error:
        raise ResumeExtractionError(
            "The DOCX file could not be opened."
        ) from error

    content: list[str] = []

    for paragraph in document.paragraphs:
        text = paragraph.text.strip()

        if text:
            content.append(text)

    # Resume information is sometimes stored in tables.
    for table in document.tables:
        for row in table.rows:
            row_values = [
                cell.text.strip()
                for cell in row.cells
                if cell.text.strip()
            ]

            if row_values:
                content.append(
                    " | ".join(row_values)
                )

    extracted_text = "\n".join(content)

    # python-docx does not reliably expose rendered page count.
    return extracted_text, None


def extract_resume_bytes(
    file_bytes: bytes,
    filename: str,
    mime_type: str | None = None,
) -> ExtractedResume:
    validate_file_size(file_bytes)

    extension = get_file_extension(
        filename=filename,
        mime_type=mime_type,
    )

    if extension == ".pdf":
        text, page_count = extract_pdf_text(
            file_bytes
        )

    elif extension == ".docx":
        text, page_count = extract_docx_text(
            file_bytes
        )

    else:
        raise ResumeExtractionError(
            "Unsupported resume format."
        )

    cleaned_text = normalize_resume_text(
        text
    )

    if not cleaned_text:
        if extension == ".pdf":
            raise ResumeExtractionError(
                "No readable text was found in the PDF. "
                "It may be a scanned image and require OCR."
            )

        raise ResumeExtractionError(
            "No readable text was found in the resume."
        )

    word_count = len(
        cleaned_text.split()
    )

    if word_count < 20:
        raise ResumeExtractionError(
            "Very little text was extracted. "
            "Please verify the resume file."
        )

    return ExtractedResume(
        filename=filename,
        file_extension=extension,
        mime_type=mime_type,
        file_size_bytes=len(file_bytes),
        page_count=page_count,
        word_count=word_count,
        resume_hash=create_resume_hash(
            file_bytes
        ),
        text=cleaned_text,
    )


def extract_resume(
    uploaded_file: UploadedFileProtocol,
) -> ExtractedResume:
    """
    Main function used by the future Streamlit UI.

    Example:
        uploaded_file = st.file_uploader(...)
        resume = extract_resume(uploaded_file)
    """
    try:
        file_bytes = uploaded_file.getvalue()
    except AttributeError as error:
        raise ResumeExtractionError(
            "The uploaded file is not valid."
        ) from error

    return extract_resume_bytes(
        file_bytes=file_bytes,
        filename=uploaded_file.name,
        mime_type=getattr(
            uploaded_file,
            "type",
            None,
        ),
    )


def extract_resume_file(
    file_path: str | Path,
) -> ExtractedResume:
    """
    Local file helper for command-line testing.
    """
    path = Path(file_path)

    if not path.exists():
        raise ResumeExtractionError(
            f"File not found: {path}"
        )

    if not path.is_file():
        raise ResumeExtractionError(
            f"Not a file: {path}"
        )

    try:
        file_bytes = path.read_bytes()
    except OSError as error:
        raise ResumeExtractionError(
            f"Unable to read file: {path}"
        ) from error

    return extract_resume_bytes(
        file_bytes=file_bytes,
        filename=path.name,
    )
