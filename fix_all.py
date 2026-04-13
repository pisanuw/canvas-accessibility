#!/usr/bin/env python3
"""
Accessibility Remediation Orchestrator

Runs all (or selected) fix scripts against a Canvas course in priority order.
Produces a JSON report of every change made.

Usage:
  python3 fix_all.py --course-id 1492292 --dry-run
  python3 fix_all.py --course-id 1492292 --types html,word
  python3 fix_all.py --course-id 1492292 --output fix_report.json

Fix types and their order:
  html   — Canvas pages, syllabus, and assignment descriptions
           (empty_heading, heading_order, headings_presence, headings_start_at_one,
            table_headers, table_captions, lists, color_contrast, image_alt/placeholder, links)
  word   — Word .docx files (headings_presence, headings_start_at_one, heading_order,
                              table_headers, no_language, image_alt/placeholder)
  pptx   — PowerPoint .pptx (image_alt, slide_title, reading_order, no_language, links)
  pdf    — PDF metadata     (no_title, no_language)

AI-assisted fixes (image_alt, slide_title, links) require:
  export ANTHROPIC_API_KEY=your_key_here
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))
from fixes.canvas_client import CanvasClient


def run_html_fixes(client: CanvasClient, course_id: int,
                   dry_run: bool, skip_ai: bool) -> dict:
    from fixes.fix_html_pages import (fix_course_pages, fix_course_syllabus,
                                       fix_course_assignments)
    fixes = [
        "empty_heading", "heading_order", "headings_presence",
        "headings_start_at_one", "table_headers", "table_captions",
        "lists", "color_contrast", "html_meta",
    ]
    if not skip_ai:
        fixes += ["image_alt", "links"]
    else:
        fixes += ["image_alt_placeholder"]

    print(f"\n{'='*60}")
    print(f"HTML Page Fixes  ({', '.join(fixes)})")
    print(f"{'='*60}")
    pages = fix_course_pages(client, course_id, fixes, dry_run=dry_run)

    print(f"\n{'='*60}")
    print(f"Syllabus Fixes")
    print(f"{'='*60}")
    syllabus = fix_course_syllabus(client, course_id, fixes, dry_run=dry_run)

    print(f"\n{'='*60}")
    print(f"Assignment Description Fixes")
    print(f"{'='*60}")
    assignments = fix_course_assignments(client, course_id, fixes, dry_run=dry_run)

    return {"html": pages, "syllabus": syllabus, "assignments": assignments}


def run_word_fixes(client: CanvasClient, course_id: int,
                   dry_run: bool, skip_ai: bool) -> list[dict]:
    from fixes.fix_word_docs import fix_course_word_files
    fixes = ["headings_presence", "headings_start_at_one", "heading_order",
             "table_headers", "no_language"]
    if not skip_ai:
        fixes += ["image_alt"]
    else:
        fixes += ["image_alt_placeholder"]
    print(f"\n{'='*60}")
    print(f"Word Document Fixes  ({', '.join(fixes)})")
    print(f"{'='*60}")
    return fix_course_word_files(client, course_id, fixes, dry_run=dry_run)


def run_pptx_fixes(client: CanvasClient, course_id: int,
                   dry_run: bool, skip_ai: bool) -> list[dict]:
    from fixes.fix_pptx_files import fix_course_pptx_files
    fixes = ["reading_order", "no_language", "links"]
    if not skip_ai:
        fixes += ["image_alt", "slide_title"]
    print(f"\n{'='*60}")
    print(f"PowerPoint Fixes  ({', '.join(fixes)})")
    print(f"{'='*60}")
    return fix_course_pptx_files(client, course_id, fixes, dry_run=dry_run)


def run_pdf_fixes(client: CanvasClient, course_id: int,
                  dry_run: bool) -> list[dict]:
    from fixes.fix_pdf_metadata import fix_course_pdfs
    print(f"\n{'='*60}")
    print(f"PDF Metadata Fixes  (no_title, no_language)")
    print(f"{'='*60}")
    return fix_course_pdfs(client, course_id, ["all"], dry_run=dry_run)


def main():
    parser = argparse.ArgumentParser(
        description="Run all accessibility fixes on a Canvas course",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run — show what would change, nothing uploaded
  python3 fix_all.py --course-id 1492292 --dry-run

  # Run all fixes (requires ANTHROPIC_API_KEY for AI-assisted fixes)
  python3 fix_all.py --course-id 1492292

  # Run only fully-automatic fixes (no AI key needed)
  python3 fix_all.py --course-id 1492292 --no-ai

  # Run only HTML and PDF fixes
  python3 fix_all.py --course-id 1492292 --types html,pdf

  # Save detailed report
  python3 fix_all.py --course-id 1492292 --output fix_report.json
        """,
    )
    parser.add_argument("--course-id", type=int, required=True)
    parser.add_argument("--types", default="html,word,pptx,pdf",
                        help="Comma-separated fix types to run (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show changes without uploading to Canvas")
    parser.add_argument("--no-ai", action="store_true",
                        help="Skip AI-assisted fixes (no ANTHROPIC_API_KEY needed)")
    parser.add_argument("--output", default=None,
                        help="Save detailed fix report to this JSON file")
    args = parser.parse_args()

    types = {t.strip().lower() for t in args.types.split(",")}
    client = CanvasClient()
    course_id = args.course_id
    dry_run = args.dry_run
    skip_ai = args.no_ai

    # Check AI availability
    if not skip_ai:
        import os
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("Warning: ANTHROPIC_API_KEY not set — AI-assisted fixes will be skipped.")
            print("  Set it with:  export ANTHROPIC_API_KEY=your_key")
            print("  Or use --no-ai to suppress this warning.\n")
            skip_ai = True

    print(f"Canvas Accessibility Remediation")
    print(f"Course: {course_id}  |  Dry run: {dry_run}  |  AI: {not skip_ai}")
    print(f"Types: {', '.join(sorted(types))}")
    start = datetime.now()

    all_results = {}

    if "html" in types:
        html_group = run_html_fixes(client, course_id, dry_run, skip_ai)
        all_results.update(html_group)  # adds html, syllabus, assignments keys

    if "word" in types:
        all_results["word"] = run_word_fixes(client, course_id, dry_run, skip_ai)

    if "pptx" in types:
        all_results["pptx"] = run_pptx_fixes(client, course_id, dry_run, skip_ai)

    if "pdf" in types:
        all_results["pdf"] = run_pdf_fixes(client, course_id, dry_run)

    # ── Summary ───────────────────────────────────────────────────────────────
    elapsed = (datetime.now() - start).seconds
    print(f"\n{'='*60}")
    print(f"REMEDIATION COMPLETE  ({elapsed}s)")
    print(f"{'='*60}")

    total_items = 0
    total_changes = 0
    total_updated = 0
    total_errors = 0

    for type_name, results in all_results.items():
        items = len(results)
        changes = sum(len(r.get("changes", [])) for r in results)
        updated = sum(1 for r in results if r.get("updated"))
        errors = sum(1 for r in results if "error" in r)
        total_items += items
        total_changes += changes
        total_updated += updated
        total_errors += errors
        status = "✓" if not errors else "!"
        print(f"  {status} {type_name:<6}  {items:3d} items  "
              f"{changes:4d} changes  {updated:3d} updated  {errors} errors")

    print(f"  {'─'*50}")
    print(f"  {'TOTAL':<6}  {total_items:3d} items  "
          f"{total_changes:4d} changes  {total_updated:3d} updated  {total_errors} errors")

    if dry_run:
        print("\n  [DRY RUN] Nothing was uploaded to Canvas.")

    # ── Save report ───────────────────────────────────────────────────────────
    report = {
        "course_id": course_id,
        "dry_run": dry_run,
        "ai_enabled": not skip_ai,
        "run_at": start.isoformat(),
        "elapsed_seconds": elapsed,
        "summary": {
            "total_items": total_items,
            "total_changes": total_changes,
            "total_updated": total_updated,
            "total_errors": total_errors,
        },
        "results": all_results,
    }

    out = args.output or f"fix_report_{course_id}.json"
    Path(out).write_text(json.dumps(report, indent=2, default=str))
    print(f"\nReport saved to {out}")


if __name__ == "__main__":
    main()
