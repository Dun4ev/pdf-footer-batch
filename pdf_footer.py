#!/usr/bin/env python3
"""Add a page-number footer to one PDF or a directory of PDFs."""
from __future__ import annotations

import argparse
import io
import sys
import tempfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas


class FooterError(Exception):
    """An input cannot be processed safely with the selected footer."""


def validate_document_number(value: str) -> str:
    if any(ord(char) < 32 or ord(char) > 126 for char in value):
        raise FooterError("document number must use printable ASCII characters")
    return value


def check_page(page, label: str, number: int) -> None:
    if page.rotation != 0:
        raise FooterError(f"page {number} has rotation {page.rotation}; handle it separately")
    media = tuple(float(v) for v in page.mediabox)
    crop = tuple(float(v) for v in page.cropbox)
    if media != crop or media[0] != 0 or media[1] != 0:
        raise FooterError(f"page {number} has a nonstandard crop or media box")
    width, height = media[2], media[3]
    if height < 100 or width < 300:
        raise FooterError(f"page {number} is too small for the footer")
    if label and 128 + stringWidth(label, "Helvetica", 9) >= width - 178:
        raise FooterError(f"document number does not fit on page {number}")


def inspect_pdf(path: Path, doc_number: str) -> int:
    reader = PdfReader(path)
    if reader.is_encrypted:
        raise FooterError("encrypted PDFs require separate handling")
    fields = reader.get_fields() or {}
    if any(str(field.get("/FT")) == "/Sig" for field in fields.values()):
        raise FooterError("PDF has a signature field; modification may invalidate signatures")
    if not reader.pages:
        raise FooterError("PDF contains no pages")
    for number, page in enumerate(reader.pages, start=1):
        check_page(page, doc_number, number)
    return len(reader.pages)


def make_footer(width: float, height: float, number: int, total: int, doc_number: str):
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(width, height))
    c.setFont("Helvetica", 9)
    c.setFillColorRGB(0, 0, 0)
    if doc_number:
        left, right, split = 57, width - 57, width - 170
        c.setStrokeColorRGB(0.64, 0.64, 0.64)
        c.setLineWidth(0.45)
        c.line(left, 52, right, 52)
        c.line(left, 27, right, 27)
        c.line(split, 27, split, 52)
        c.drawString(left + 5, 36, "Doc. number:")
        c.drawString(left + 71, 36, doc_number)
        c.drawString(split + 7, 36, "Page:")
        c.drawString(split + 45, 36, f"{number} of {total}")
    else:
        c.drawCentredString(width / 2, 25, f"Page {number} of {total}")
    c.save()
    buffer.seek(0)
    return PdfReader(buffer).pages[0]


def add_footer(source: Path, destination: Path, doc_number: str) -> int:
    pages = inspect_pdf(source, doc_number)
    reader = PdfReader(source)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    for number, page in enumerate(writer.pages, start=1):
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        page.merge_page(make_footer(width, height, number, pages, doc_number))
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents accidental overwriting, including on repeated runs.
    try:
        with tempfile.TemporaryFile(mode="w+b") as temp:
            writer.write(temp)
            temp.seek(0)
            with destination.open("xb") as target:
                try:
                    while chunk := temp.read(1024 * 1024):
                        target.write(chunk)
                except Exception:
                    target.close()
                    destination.unlink(missing_ok=True)
                    raise
    except FileExistsError as exc:
        raise FooterError("output already exists") from exc
    return pages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="PDF file or directory containing PDFs")
    parser.add_argument("output", type=Path, help="output file for one PDF, or directory for a batch")
    parser.add_argument("--doc-number", default="", help="printable ASCII document number, or {stem} to use each filename")
    parser.add_argument("--dry-run", action="store_true", help="inspect inputs without writing output")
    args = parser.parse_args(argv)

    source, destination = args.input.resolve(), args.output.resolve()
    if not source.exists():
        parser.error(f"input does not exist: {source}")
    is_batch = source.is_dir()
    if is_batch:
        if destination == source or destination.is_relative_to(source):
            parser.error("output directory must not be inside the input directory")
        files = sorted((p for p in source.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"), key=lambda p: str(p).lower())
        if not files:
            parser.error("input directory contains no PDFs")
    elif source.is_file() and source.suffix.lower() == ".pdf":
        if destination == source:
            parser.error("output must differ from the input")
        if destination.suffix.lower() != ".pdf":
            parser.error("output for a single input must be a .pdf file")
        files = [source]
    else:
        parser.error("input must be a PDF file or a directory")

    created = skipped = failed = 0
    for path in files:
        target = destination / path.relative_to(source) if is_batch else destination
        number = path.stem if args.doc_number == "{stem}" else args.doc_number
        try:
            number = validate_document_number(number)
            if target.exists():
                print(f"SKIP {path.name}: output already exists")
                skipped += 1
                continue
            if args.dry_run:
                count = inspect_pdf(path, number)
                print(f"READY {path.name}: {count} pages -> {target}")
            else:
                count = add_footer(path, target, number)
                print(f"CREATED {target}: {count} pages")
            created += 1
        except (FooterError, OSError, ValueError) as exc:
            print(f"ERROR {path.name}: {exc}", file=sys.stderr)
            failed += 1
        except Exception as exc:
            # Other parser failures are isolated to the current file in a batch.
            print(f"ERROR {path.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            failed += 1
    verb = "ready" if args.dry_run else "created"
    print(f"Summary: {created} {verb}, {skipped} skipped, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
