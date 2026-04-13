"""
PDF metadata accessibility fixes.

Fixes:
  - pdf.no_title    : PDF missing document title   (fully automatic)
  - pdf.no_language : PDF missing document language (fully automatic)

These are the two safest PDF fixes — they only touch metadata, never content.
More complex PDF fixes (tagging, heading structure, image alt text) require
pikepdf + pymupdf and are planned for a future script.

Usage (CLI):
  python3 fix_pdf_metadata.py --course-id 1492292 --fix all
  python3 fix_pdf_metadata.py --course-id 1492292 --fix no_title --file-id 79530470
  python3 fix_pdf_metadata.py --course-id 1492292 --fix all --dry-run
"""

import argparse
import io
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).parent.parent))
from fixes.canvas_client import CanvasClient

PDF_MIME = "application/pdf"


# ── Fix: document title ───────────────────────────────────────────────────────

def fix_title(pdf_bytes: bytes, filename: str) -> tuple[bytes, list[str]]:
    """
    Set /Title in the PDF Info dictionary if it is missing or empty.
    Title is derived from the cleaned filename.
    Returns (updated_pdf_bytes, list_of_changes).
    """
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    existing_title = ""
    if reader.metadata:
        existing_title = (reader.metadata.get("/Title") or "").strip()
    if existing_title:
        return pdf_bytes, []  # already has a title

    # Derive title from filename: strip extension, URL-decode, replace separators
    stem = Path(unquote(filename)).stem
    title = re.sub(r'[_+\-]+', ' ', stem).strip()
    title = re.sub(r'\s+', ' ', title).title()

    writer = pypdf.PdfWriter()
    writer.clone_reader_document_root(reader)
    writer.add_metadata({"/Title": title})

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue(), [f"Set PDF title: '{title}'"]


# ── Fix: document language ────────────────────────────────────────────────────

def fix_language(pdf_bytes: bytes, lang: str = "en") -> tuple[bytes, list[str]]:
    """
    Set /Lang in the PDF catalog if it is missing.
    Returns (updated_pdf_bytes, list_of_changes).
    """
    import pikepdf

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        # Check existing language
        existing = str(pdf.Root.get("/Lang", "")).strip()
        if existing:
            return pdf_bytes, []  # already set

        pdf.Root["/Lang"] = pikepdf.String(lang)
        buf = io.BytesIO()
        pdf.save(buf)

    return buf.getvalue(), [f"Set PDF language: '{lang}'"]


# ── Check PDF structure (informational) ──────────────────────────────────────

def inspect_pdf(pdf_bytes: bytes) -> dict:
    """
    Return a dict of key accessibility properties of the PDF.
    Used by fix_all.py to decide which fixes to apply.
    """
    import pikepdf
    import pypdf

    info = {
        "has_title": False,
        "has_language": False,
        "has_struct_tree": False,
        "has_text": False,
        "page_count": 0,
    }
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        info["page_count"] = len(reader.pages)
        if reader.metadata:
            info["has_title"] = bool((reader.metadata.get("/Title") or "").strip())
        # Check for text
        if reader.pages:
            text = reader.pages[0].extract_text() or ""
            info["has_text"] = len(text.strip()) > 20
    except Exception:
        pass

    try:
        with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
            info["has_language"] = bool(str(pdf.Root.get("/Lang", "")).strip())
            info["has_struct_tree"] = "/StructTreeRoot" in pdf.Root
    except Exception:
        pass

    return info


# ── Orchestrate fixes on one file ─────────────────────────────────────────────

def fix_pdf_file(client: CanvasClient, course_id: int, file_info: dict,
                 fixes: list[str], lang: str = "en",
                 dry_run: bool = False) -> dict:
    """Download a PDF, apply metadata fixes, re-upload."""
    file_id = file_info["id"]
    filename = file_info.get("display_name") or file_info.get("filename", "document.pdf")
    folder_id = file_info.get("folder_id")

    pdf_bytes = client.download_url(file_info["url"])
    all_changes = []
    all_fixes = "all" in fixes

    if all_fixes or "no_title" in fixes:
        pdf_bytes, ch = fix_title(pdf_bytes, filename)
        all_changes.extend(ch)

    if all_fixes or "no_language" in fixes or "language" in fixes:
        pdf_bytes, ch = fix_language(pdf_bytes, lang)
        all_changes.extend(ch)

    result = {"file": filename, "file_id": file_id, "changes": all_changes, "updated": False}

    if all_changes and not dry_run:
        client.upload_file(course_id, folder_id, filename, PDF_MIME, pdf_bytes)
        result["updated"] = True
        print(f"    Re-uploaded: {', '.join(all_changes)}")
    elif all_changes:
        print(f"    [dry-run] {', '.join(all_changes)}")
    else:
        print(f"    No changes needed")

    return result


def fix_course_pdfs(client: CanvasClient, course_id: int,
                    fixes: list[str], file_id: int = None,
                    lang: str = "en", dry_run: bool = False) -> list[dict]:
    """Run metadata fixes on one PDF or all PDFs in a course."""
    if file_id:
        files = [client.get_file_info(file_id)]
    else:
        all_files = client.list_files(course_id)
        files = [f for f in all_files if f.get("mime_class") == "pdf"
                 or f.get("content-type") == PDF_MIME
                 or Path(f.get("filename", "")).suffix.lower() == ".pdf"]
        print(f"Found {len(files)} PDF(s) in course {course_id}")

    results = []
    total = len(files)
    for i, f in enumerate(files, 1):
        fname = f.get("display_name") or f.get("filename", "?")
        print(f"  Processing: {fname} ({i}/{total})")
        try:
            r = fix_pdf_file(client, course_id, f, fixes, lang, dry_run)
            results.append(r)
            time.sleep(0.3)
        except Exception as e:
            print(f"  ERROR on '{fname}': {e}")
            results.append({"file": fname, "error": str(e)})

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fix PDF metadata accessibility issues")
    parser.add_argument("--course-id", type=int, required=True)
    parser.add_argument("--file-id", type=int, default=None,
                        help="Canvas file ID of a specific PDF, or omit for all PDFs")
    parser.add_argument("--fix", default="all",
                        help="Comma-separated: all,no_title,no_language")
    parser.add_argument("--lang", default="en",
                        help="Language code to set (default: en)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    fix_list = [f.strip() for f in args.fix.split(",")]
    client = CanvasClient()

    results = fix_course_pdfs(client, args.course_id, fix_list,
                               file_id=args.file_id, lang=args.lang,
                               dry_run=args.dry_run)

    total_changes = sum(len(r.get("changes", [])) for r in results)
    updated = sum(1 for r in results if r.get("updated"))
    print(f"\nSummary: {total_changes} changes, {updated}/{len(results)} files updated")


if __name__ == "__main__":
    main()
