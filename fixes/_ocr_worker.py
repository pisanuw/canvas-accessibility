#!/usr/bin/env python3
"""
fixes/_ocr_worker.py — Isolated OCR subprocess.

Reads raw PDF bytes from stdin.
Writes the OCR'd PDF bytes to stdout.
Writes the page count (integer) to stderr on success, or an error message on failure.

Run via fix_scanned() in fix_pdf_content.py — never call directly.

Why a subprocess?
  OCR with pdf2image/pytesseract allocates large PIL Image objects (one per page
  at render DPI).  Running inside Gunicorn, those allocations accumulate and the
  process never gives the memory back to the OS, eventually hitting Render's
  512 MB limit.  Spawning a fresh process for each file means the OS reclaims
  all memory the moment the worker exits.

Memory strategy inside the worker:
  - 200 DPI instead of 300 DPI  (~56% less image memory per page)
  - One page rendered at a time  (peak = one page, not whole document)
  - Explicit del + gc.collect()  after each page
"""

import gc
import io
import sys


def main() -> None:
    pdf_bytes = sys.stdin.buffer.read()
    if not pdf_bytes:
        sys.stderr.write("OCR worker: no input received")
        sys.exit(1)

    import fitz  # pymupdf
    import pytesseract
    from pdf2image import convert_from_bytes

    # Count pages without holding the full rendered images
    doc = fitz.open("pdf", pdf_bytes)
    page_count = len(doc)
    doc.close()

    merged = fitz.open()

    for page_num in range(1, page_count + 1):
        # Render one page at 200 DPI — good OCR quality, ~56% less RAM than 300 DPI
        images = convert_from_bytes(
            pdf_bytes, dpi=200, first_page=page_num, last_page=page_num
        )
        img = images[0]
        del images  # release the list immediately

        page_pdf_bytes = pytesseract.image_to_pdf_or_hocr(img, extension="pdf")
        del img
        gc.collect()

        page_doc = fitz.open("pdf", page_pdf_bytes)
        merged.insert_pdf(page_doc)
        page_doc.close()
        del page_pdf_bytes
        gc.collect()

    buf = io.BytesIO()
    merged.save(buf)
    merged.close()

    sys.stdout.buffer.write(buf.getvalue())
    sys.stderr.write(str(page_count))  # caller reads this for the change log


if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except Exception as exc:
        sys.stderr.write(f"OCR error: {exc}")
        sys.exit(1)
