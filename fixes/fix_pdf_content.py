"""
PDF content accessibility fixes.

Fixes:
  - pdf.scanned               : OCR scanned PDFs to add machine-readable text layer
  - pdf.no_tags               : Add minimal accessibility tag structure (StructTreeRoot)
  - pdf.no_headings           : Add "Header to be Replaced" H1 to structure tree
  - pdf.table_headers         : Promote first-row TD cells to TH in structure tree
  - pdf.image_alt             : Set /Alt placeholder on /Figure tags missing alt text
  - pdf.links                 : Set /Contents on link annotations
  - pdf.headings_start_at_one : Shift heading levels so minimum is H1
  - pdf.headings_sequential   : Close heading level gaps (H1→H3 becomes H1→H2)

Fix order matters: scanned → tags+headings → headings_start_at_one →
                   headings_sequential → table_headers → image_alt → links.

Usage (CLI):
  python3 fix_pdf_content.py --course-id 1492302 --fix all
  python3 fix_pdf_content.py --course-id 1492302 --fix scanned,links
  python3 fix_pdf_content.py --course-id 1492302 --fix headings_start_at_one --dry-run
"""

import argparse
import io
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from fixes.canvas_client import CanvasClient

PDF_MIME = "application/pdf"

_FILENAME_RE = re.compile(
    r'\.[a-zA-Z0-9]{2,5}$'
    r'|^[\w\-. ]+\.(png|jpe?g|gif|svg|bmp|webp|tiff?|pdf|docx?|pptx?|xlsx?)$',
    re.IGNORECASE,
)


def _is_filename_alt(text: str) -> bool:
    t = text.strip()
    return bool(t and _FILENAME_RE.search(t))


# ── Fix 5: OCR scanned PDFs ───────────────────────────────────────────────────

_OCR_WORKER = Path(__file__).parent / "_ocr_worker.py"
_OCR_TIMEOUT = 300  # seconds per file


def fix_scanned(pdf_bytes: bytes) -> tuple[bytes, list[str]]:
    """
    Detect scanned PDFs (no extractable text) and run OCR in an isolated
    subprocess to add a machine-readable text layer.

    OCR runs in a subprocess so that all image memory (pdf2image + PIL) is
    released when the worker exits, preventing the Gunicorn process from
    accumulating allocations across files and hitting the 512 MB Render limit.

    Returns (updated_pdf_bytes, list_of_changes).
    """
    # Check whether PDF already has text — lightweight, stays in main process
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        pages_to_check = min(3, len(reader.pages))
        text_chars = sum(
            len((reader.pages[i].extract_text() or "").strip())
            for i in range(pages_to_check)
        )
        if text_chars >= 50:
            return pdf_bytes, []  # already has readable text
    except Exception:
        pass  # assume scanned, proceed with OCR

    # Spawn isolated worker — all OCR memory freed when subprocess exits
    try:
        proc = subprocess.run(
            [sys.executable, str(_OCR_WORKER)],
            input=pdf_bytes,
            capture_output=True,
            timeout=_OCR_TIMEOUT,
        )
        if proc.returncode == 0 and proc.stdout:
            stderr = proc.stderr.decode(errors="replace").strip()
            page_count = stderr if stderr.isdigit() else "?"
            return proc.stdout, [f"OCR applied: {page_count} page(s) processed"]
        err = proc.stderr.decode(errors="replace").strip()[:200]
        return pdf_bytes, [f"OCR failed (exit {proc.returncode}): {err}"]
    except subprocess.TimeoutExpired:
        return pdf_bytes, [f"OCR skipped — exceeded {_OCR_TIMEOUT}s timeout"]
    except Exception as exc:
        return pdf_bytes, [f"OCR skipped — {exc}"]


# ── Fix for pdf.no_tags + Fix 8: heading ─────────────────────────────────────

def fix_tags_and_headings(pdf_bytes: bytes) -> tuple[bytes, list[str]]:
    """
    Add a minimal accessibility tag structure (StructTreeRoot) if absent,
    and add an H1 heading element 'Header to be Replaced' if no headings exist.
    Returns (updated_pdf_bytes, list_of_changes).
    """
    import pikepdf

    changes = []
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        # Signal that this is a tagged PDF
        if "/MarkInfo" not in pdf.Root:
            pdf.Root["/MarkInfo"] = pikepdf.Dictionary(Marked=True)

        # Create StructTreeRoot + Document node if absent
        if "/StructTreeRoot" not in pdf.Root:
            doc_node = pdf.make_indirect(pikepdf.Dictionary(
                S=pikepdf.Name("/Document"),
                K=pikepdf.Array(),
            ))
            struct_root = pdf.make_indirect(pikepdf.Dictionary(
                Type=pikepdf.Name("/StructTreeRoot"),
                K=doc_node,
            ))
            doc_node["/P"] = struct_root
            pdf.Root["/StructTreeRoot"] = struct_root
            changes.append("Added minimal accessibility tag structure")

        # Locate the Document node inside the struct tree
        struct_root = pdf.Root.StructTreeRoot
        doc_node = _find_document_node(pdf, struct_root)

        # Check if headings already exist anywhere in the tree
        tag_names: set = set()
        _collect_tag_names(struct_root, tag_names)
        heading_tags = {"/H", "/H1", "/H2", "/H3", "/H4", "/H5", "/H6"}
        if not (tag_names & heading_tags):
            h1 = pdf.make_indirect(pikepdf.Dictionary(
                S=pikepdf.Name("/H1"),
                Alt=pikepdf.String("Header to be Replaced"),
                T=pikepdf.String("Header to be Replaced"),
            ))
            kids = doc_node.get("/K")
            if kids is None:
                doc_node["/K"] = pikepdf.Array([h1])
            elif isinstance(kids, pikepdf.Array):
                doc_node["/K"] = pikepdf.Array([h1] + list(kids))
            else:
                doc_node["/K"] = pikepdf.Array([h1, kids])
            changes.append("Added H1 heading 'Header to be Replaced' to structure tree")

        if changes:
            buf = io.BytesIO()
            pdf.save(buf)
            return buf.getvalue(), changes

    return pdf_bytes, []


# ── Fix 7: table headers ──────────────────────────────────────────────────────

def fix_table_headers(pdf_bytes: bytes) -> tuple[bytes, list[str]]:
    """
    In the PDF structure tree, promote all TD cells in the first row of each
    table to TH cells.
    Returns (updated_pdf_bytes, list_of_changes).
    """
    import pikepdf

    changes = []
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        if "/StructTreeRoot" not in pdf.Root:
            return pdf_bytes, []

        fixed_count = [0]

        def _fix_node(node, depth=0):
            if depth > 100 or not isinstance(node, pikepdf.Dictionary):
                return
            if str(node.get("/S", "")) == "/Table":
                _fix_table(node)
            kids = node.get("/K")
            if isinstance(kids, pikepdf.Array):
                for kid in kids:
                    _fix_node(kid, depth + 1)
            elif isinstance(kids, pikepdf.Dictionary):
                _fix_node(kids, depth + 1)

        def _fix_table(table_node):
            kids = table_node.get("/K")
            first_tr = None
            if isinstance(kids, pikepdf.Array):
                for kid in kids:
                    if isinstance(kid, pikepdf.Dictionary) and \
                            str(kid.get("/S", "")) == "/TR":
                        first_tr = kid
                        break
            elif isinstance(kids, pikepdf.Dictionary) and \
                    str(kids.get("/S", "")) == "/TR":
                first_tr = kids

            if first_tr is None:
                return
            tr_kids = first_tr.get("/K", pikepdf.Array())
            converted = 0
            if isinstance(tr_kids, pikepdf.Array):
                for cell in tr_kids:
                    if isinstance(cell, pikepdf.Dictionary) and \
                            str(cell.get("/S", "")) == "/TD":
                        cell["/S"] = pikepdf.Name("/TH")
                        converted += 1
            elif isinstance(tr_kids, pikepdf.Dictionary) and \
                    str(tr_kids.get("/S", "")) == "/TD":
                tr_kids["/S"] = pikepdf.Name("/TH")
                converted = 1
            if converted:
                fixed_count[0] += 1
                changes.append(
                    f"Promoted {converted} TD → TH in first row of a table")

        _fix_node(pdf.Root.StructTreeRoot)

        if changes:
            buf = io.BytesIO()
            pdf.save(buf)
            return buf.getvalue(), changes

    return pdf_bytes, []


# ── Fix 7: image alt text (placeholder) ──────────────────────────────────────

def fix_image_alt(pdf_bytes: bytes) -> tuple[bytes, list[str]]:
    """
    Set /Alt placeholder on every /Figure tag in the struct tree that currently
    has no alt text. No AI needed — a human-review placeholder is used.
    Returns (updated_pdf_bytes, list_of_changes).
    """
    import pikepdf

    PLACEHOLDER = (
        "This image currently does not have a description. "
        "Your instructor will review it and add a description"
    )
    changes = []

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        if "/StructTreeRoot" not in pdf.Root:
            return pdf_bytes, []

        fixed = [0]

        def _walk(node, depth=0):
            if depth > 100 or not isinstance(node, pikepdf.Dictionary):
                return
            if str(node.get("/S", "")) == "/Figure":
                current = str(node.get("/Alt", "")).strip()
                if not current or _is_filename_alt(current):
                    node["/Alt"] = pikepdf.String(PLACEHOLDER)
                    fixed[0] += 1
            kids = node.get("/K")
            if isinstance(kids, pikepdf.Array):
                for kid in kids:
                    _walk(kid, depth + 1)
            elif isinstance(kids, pikepdf.Dictionary):
                _walk(kids, depth + 1)

        _walk(pdf.Root.StructTreeRoot)

        if fixed[0]:
            changes.append(
                f"Set placeholder alt text on {fixed[0]} image(s) in struct tree")
            buf = io.BytesIO()
            pdf.save(buf)
            return buf.getvalue(), changes

    return pdf_bytes, []


# ── Fix 6: non-descriptive links ──────────────────────────────────────────────

def fix_links(pdf_bytes: bytes) -> tuple[bytes, list[str]]:
    """
    Set /Contents = 'linking to external content' on every link annotation
    that currently has no /Contents or a non-descriptive one.
    Screen readers use /Contents as the accessible link name.
    Returns (updated_pdf_bytes, list_of_changes).
    """
    import pikepdf

    LINK_LABEL = "linking to external content"
    NON_DESC = {"", "here", "click here", "read more", "link",
                "more", "this", "url"}
    changes = []

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_num, page in enumerate(pdf.pages):
            annots = page.get("/Annots")
            if annots is None:
                continue
            annot_list = list(annots) if isinstance(annots, pikepdf.Array) \
                else [annots]
            for annot in annot_list:
                if not isinstance(annot, pikepdf.Dictionary):
                    continue
                if str(annot.get("/Subtype", "")) != "/Link":
                    continue
                action = annot.get("/A")
                if not isinstance(action, pikepdf.Dictionary):
                    continue
                if not str(action.get("/URI", "")):
                    continue
                current = str(annot.get("/Contents", "")).strip()
                if current and current.lower() not in NON_DESC:
                    continue  # already has a descriptive label
                annot["/Contents"] = pikepdf.String(LINK_LABEL)
                changes.append(
                    f"Page {page_num + 1}: set link description to "
                    f"'linking to external content'")

        if changes:
            buf = io.BytesIO()
            pdf.save(buf)
            return buf.getvalue(), changes

    return pdf_bytes, []


# ── Helpers ───────────────────────────────────────────────────────────────────

_HEADING_TAGS = {"/H1", "/H2", "/H3", "/H4", "/H5", "/H6"}


def _collect_tag_names(node, names: set, depth: int = 0) -> None:
    """Recursively collect all /S tag names from a struct tree node."""
    if depth > 100:
        return
    try:
        import pikepdf
        if not isinstance(node, pikepdf.Dictionary):
            return
        s = str(node.get("/S", ""))
        if s:
            names.add(s)
        kids = node.get("/K")
        if isinstance(kids, pikepdf.Array):
            for kid in kids:
                _collect_tag_names(kid, names, depth + 1)
        elif isinstance(kids, pikepdf.Dictionary):
            _collect_tag_names(kids, names, depth + 1)
    except Exception:
        pass


def _collect_heading_nodes(node, out: list, depth: int = 0) -> None:
    """
    Collect all /H1–/H6 struct nodes in document (DFS) order.
    Ignores the bare /H tag (no level information).
    """
    if depth > 100:
        return
    try:
        import pikepdf
        if not isinstance(node, pikepdf.Dictionary):
            return
        if str(node.get("/S", "")) in _HEADING_TAGS:
            out.append(node)
        kids = node.get("/K")
        if isinstance(kids, pikepdf.Array):
            for kid in kids:
                _collect_heading_nodes(kid, out, depth + 1)
        elif isinstance(kids, pikepdf.Dictionary):
            _collect_heading_nodes(kids, out, depth + 1)
    except Exception:
        pass


def _heading_level(node) -> int | None:
    """Return the integer level (1–6) of a heading struct node, or None."""
    s = str(node.get("/S", ""))
    if s in _HEADING_TAGS:
        return int(s[2])
    return None


def _find_document_node(pdf, struct_root):
    """Find or create the /Document node inside the struct tree."""
    import pikepdf
    kids = struct_root.get("/K")
    if isinstance(kids, pikepdf.Array):
        for kid in kids:
            if isinstance(kid, pikepdf.Dictionary) and \
                    str(kid.get("/S", "")) == "/Document":
                return kid
    elif isinstance(kids, pikepdf.Dictionary) and \
            str(kids.get("/S", "")) == "/Document":
        return kids

    # Create one
    doc_node = pdf.make_indirect(pikepdf.Dictionary(
        S=pikepdf.Name("/Document"),
        P=struct_root,
        K=pikepdf.Array(),
    ))
    if isinstance(struct_root.get("/K"), pikepdf.Array):
        struct_root["/K"].append(doc_node)
    else:
        existing = struct_root.get("/K")
        struct_root["/K"] = pikepdf.Array(
            [doc_node] + ([existing] if existing else [])
        )
    return doc_node


# ── Fix: heading start at one ────────────────────────────────────────────────

def fix_headings_start_at_one(pdf_bytes: bytes) -> tuple[bytes, list[str]]:
    """
    If the minimum heading level in the struct tree is > 1 (e.g. all headings
    start at /H2 or /H3), shift every heading level down so the minimum
    becomes /H1.  E.g. H2/H3/H4 → H1/H2/H3.
    Returns (updated_pdf_bytes, list_of_changes).
    """
    import pikepdf

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        if "/StructTreeRoot" not in pdf.Root:
            return pdf_bytes, []

        nodes = []
        _collect_heading_nodes(pdf.Root.StructTreeRoot, nodes)

        levels = [(n, _heading_level(n)) for n in nodes]
        levels = [(n, lv) for n, lv in levels if lv is not None]
        if not levels:
            return pdf_bytes, []

        min_level = min(lv for _, lv in levels)
        if min_level == 1:
            return pdf_bytes, []

        shift = min_level - 1
        for node, lv in levels:
            node["/S"] = pikepdf.Name(f"/H{lv - shift}")

        changes = [
            f"Shifted heading levels by -{shift}: "
            f"H{min_level}→H1 ({len(levels)} heading(s) adjusted)"
        ]
        buf = io.BytesIO()
        pdf.save(buf)
        return buf.getvalue(), changes

    return pdf_bytes, []


# ── Fix: sequential headings ──────────────────────────────────────────────────

def fix_headings_sequential(pdf_bytes: bytes) -> tuple[bytes, list[str]]:
    """
    Walk all heading struct nodes in document order and close any level gap
    (e.g. /H1 followed by /H3 → /H1 followed by /H2).
    Returns (updated_pdf_bytes, list_of_changes).
    """
    import pikepdf

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        if "/StructTreeRoot" not in pdf.Root:
            return pdf_bytes, []

        nodes = []
        _collect_heading_nodes(pdf.Root.StructTreeRoot, nodes)

        changes = []
        prev_level = 0

        for node in nodes:
            level = _heading_level(node)
            if level is None:
                continue

            if prev_level > 0 and level > prev_level + 1:
                new_level = prev_level + 1
                old_tag = str(node.get("/S", ""))
                node["/S"] = pikepdf.Name(f"/H{new_level}")
                changes.append(f"{old_tag} → /H{new_level} (level skip fix)")
                level = new_level

            prev_level = level

        if changes:
            buf = io.BytesIO()
            pdf.save(buf)
            return buf.getvalue(), changes

    return pdf_bytes, []


# ── Orchestrate fixes on one file ─────────────────────────────────────────────

def fix_pdf_content_file(client: CanvasClient, course_id: int, file_info: dict,
                          fixes: list[str], dry_run: bool = False) -> dict:
    """Download a PDF, apply content fixes in dependency order, re-upload."""
    file_id = file_info["id"]
    filename = (file_info.get("display_name")
                or file_info.get("filename", "document.pdf"))
    folder_id = file_info.get("folder_id")

    pdf_bytes = client.download_url(file_info["url"])
    all_changes = []
    all_fixes = "all" in fixes

    # 1. OCR (must run before struct-tree fixes so text exists)
    if all_fixes or "scanned" in fixes:
        pdf_bytes, ch = fix_scanned(pdf_bytes)
        all_changes.extend(ch)

    # 2. Tags + Heading H1 (struct tree prerequisite for all heading/table fixes)
    if all_fixes or "tags" in fixes or "no_tags" in fixes \
            or "headings" in fixes or "no_headings" in fixes:
        pdf_bytes, ch = fix_tags_and_headings(pdf_bytes)
        all_changes.extend(ch)

    # 3a. Shift headings so minimum level is H1 (requires struct tree)
    if all_fixes or "headings_start_at_one" in fixes:
        pdf_bytes, ch = fix_headings_start_at_one(pdf_bytes)
        all_changes.extend(ch)

    # 3b. Close heading level gaps (requires struct tree; run after start-at-one)
    if all_fixes or "headings_sequential" in fixes:
        pdf_bytes, ch = fix_headings_sequential(pdf_bytes)
        all_changes.extend(ch)

    # 4. Table headers (requires struct tree)
    if all_fixes or "table_headers" in fixes:
        pdf_bytes, ch = fix_table_headers(pdf_bytes)
        all_changes.extend(ch)

    # 4. Image alt text placeholder (requires struct tree)
    if all_fixes or "image_alt" in fixes:
        pdf_bytes, ch = fix_image_alt(pdf_bytes)
        all_changes.extend(ch)

    # 5. Link annotations
    if all_fixes or "links" in fixes:
        pdf_bytes, ch = fix_links(pdf_bytes)
        all_changes.extend(ch)

    result = {
        "file": filename, "file_id": file_id,
        "changes": all_changes, "updated": False,
    }

    if all_changes and not dry_run:
        client.upload_file(course_id, folder_id, filename, PDF_MIME, pdf_bytes)
        result["updated"] = True
        print(f"    Re-uploaded with {len(all_changes)} fix(es)")
    elif all_changes:
        print(f"    [dry-run] {len(all_changes)} change(s) would be made")
    else:
        print(f"    No changes needed")

    for ch in all_changes:
        print(f"      • {ch}")

    return result


OCR_CAP = 10  # max PDFs to OCR per run (Render 520s timeout guard)

# All fix names except "scanned" — used when OCR cap is reached and fixes=["all"]
_ALL_FIXES_NO_OCR = [
    "tags", "no_tags", "headings", "no_headings",
    "headings_start_at_one", "headings_sequential",
    "table_headers", "image_alt", "links",
]


def fix_course_pdf_content(client: CanvasClient, course_id: int,
                            fixes: list[str], file_id: int = None,
                            dry_run: bool = False) -> list[dict]:
    """Run content fixes on one PDF or all PDFs in a course."""
    if file_id:
        files = [client.get_file_info(file_id)]
    else:
        all_files = client.list_files(course_id)
        files = [
            f for f in all_files
            if (f.get("mime_class") == "pdf"
                or f.get("content-type") == PDF_MIME
                or Path(f.get("filename", "")).suffix.lower() == ".pdf")
        ]
        print(f"Found {len(files)} PDF(s) in course {course_id}")

    wants_ocr = "scanned" in fixes or "all" in fixes
    ocr_count = 0
    ocr_cap_warned = False
    results = []
    total = len(files)

    for i, f in enumerate(files, 1):
        fname = f.get("display_name") or f.get("filename", "?")
        print(f"  Processing: {fname} ({i}/{total})")
        # Enforce OCR cap to stay within Render's 520s HTTP timeout
        if wants_ocr and ocr_count >= OCR_CAP:
            if not ocr_cap_warned:
                print(f"  ⚠ OCR cap reached ({OCR_CAP} PDFs). "
                      f"Skipping OCR for remaining files — re-run to continue.")
                ocr_cap_warned = True
            file_fixes = (_ALL_FIXES_NO_OCR if "all" in fixes
                          else [x for x in fixes if x != "scanned"])
        else:
            file_fixes = fixes

        try:
            r = fix_pdf_content_file(client, course_id, f, file_fixes, dry_run)
            if wants_ocr and file_fixes is fixes:
                ocr_count += 1
            results.append(r)
            time.sleep(0.3)
        except Exception as exc:
            print(f"  ERROR on '{fname}': {exc}")
            results.append({"file": fname, "error": str(exc)})

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fix PDF content accessibility issues")
    parser.add_argument("--course-id", type=int, required=True)
    parser.add_argument("--file-id", type=int, default=None,
                        help="Specific Canvas file ID, or omit for all PDFs")
    parser.add_argument("--fix", default="all",
                        help="Comma-separated: all,scanned,tags,headings,"
                             "headings_start_at_one,headings_sequential,"
                             "table_headers,image_alt,links")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    fix_list = [f.strip() for f in args.fix.split(",")]
    client = CanvasClient()

    results = fix_course_pdf_content(
        client, args.course_id, fix_list,
        file_id=args.file_id, dry_run=args.dry_run)

    total_changes = sum(len(r.get("changes", [])) for r in results)
    updated = sum(1 for r in results if r.get("updated"))
    print(f"\nSummary: {total_changes} changes, {updated}/{len(results)} files updated")


if __name__ == "__main__":
    main()
