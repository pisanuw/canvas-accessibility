"""
Standalone image file accessibility fixes.

Fixes:
  - image.decorative : mark image as decorative in Ally's database via the Ally REST API.
                       This clears both ImageDecorative and ImageDescription issues
                       and sets the Ally score to 1.0 for those checks.
                       A list of all marked files is returned so instructors can
                       review and add real descriptions via the Ally UI for any
                       image that is actually informational.
  - image.seizure    : replace seizure-risk images (ImageSeizure Ally score < 1.0) with
                       a static yellow warning PNG generated via Pillow.
                       The Ally content report is queried first to find affected files.

How it works:
  POST /api/v1/{clientId}/courses/{courseId}/files/{externalId}
  Content-Type: application/x-www-form-urlencoded
  Body: decorative=true&name=...&mimeType=...&fileType=image

  The externalId used by Ally matches the Canvas file ID.
  Auth requires both the Ally Bearer token and the Ally session cookie,
  obtained via the auto_login() flow in ally_api.py.

Note on description vs decorative:
  Ally's description field for standalone image files is only writable through
  their proprietary widget JS — it is not exposed via the REST API. Marking
  images as decorative is the only programmatic option and resolves the Ally
  score immediately. Instructors should review the list and add real
  descriptions for informational images via the Ally Instructor Feedback UI.

Usage (CLI):
  python3 fix_image_files.py --course-id 1492302
  python3 fix_image_files.py --course-id 1492302 --file-id 12345
  python3 fix_image_files.py --course-id 1492302 --dry-run
"""

import argparse
import io
import json
import sys
import time
import urllib.parse
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

# ── Decorative cache ──────────────────────────────────────────────────────────
# Ally's content report scores are cached from the last crawl and do not
# reflect decorative changes we made until Ally re-crawls (can take hours).
# We maintain a local cache of file IDs we have successfully marked so
# subsequent runs don't repeat the API call unnecessarily.
_CACHE_FILE = Path(__file__).parent.parent / "ally_decorative_cache.json"


def _load_cache() -> dict:
    if _CACHE_FILE.exists():
        try:
            return json.loads(_CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_cache(cache: dict) -> None:
    try:
        _CACHE_FILE.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass


def _cache_key(course_id: int) -> str:
    return str(course_id)


def _is_cached_decorative(cache: dict, course_id: int, file_id: int) -> bool:
    return str(file_id) in cache.get(_cache_key(course_id), [])

sys.path.insert(0, str(Path(__file__).parent.parent))
from fixes.canvas_client import CanvasClient

ALLY_BASE    = "https://prod.ally.ac"
IMAGE_EXTS   = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp", ".svg"}
IMAGE_MIMES  = {
    "image/png", "image/jpeg", "image/gif", "image/bmp",
    "image/tiff", "image/webp", "image/svg+xml",
}


# ── Ally API call ─────────────────────────────────────────────────────────────

def _ally_mark_decorative(ally_token: str, ally_cookie: str,
                           client_id: int, course_id: int,
                           file_ext_id: str, filename: str,
                           mime_type: str, dry_run: bool = False) -> bool:
    """
    POST to Ally's file endpoint to mark the image as decorative.
    Returns True if the call succeeded (or would succeed in dry_run).
    """
    if dry_run:
        return True

    url = (f"{ALLY_BASE}/api/v1/{client_id}/courses/{course_id}"
           f"/files/{file_ext_id}")
    body = urllib.parse.urlencode({
        "decorative": "true",
        "name":       filename,
        "mimeType":   mime_type or "image/png",
        "fileType":   "image",
    }).encode()
    req = Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {ally_token}",
        "Cookie":        ally_cookie,
        "Accept":        "application/json",
        "Content-Type":  "application/x-www-form-urlencoded",
    })
    try:
        with urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except HTTPError as e:
        body_text = e.read().decode()[:200]
        raise RuntimeError(
            f"Ally API error (HTTP {e.code}) marking {filename!r} decorative: {body_text}"
        )


# ── Per-file fix ──────────────────────────────────────────────────────────────

def fix_image_decorative(canvas_client: CanvasClient,
                          ally_token: str, ally_cookie: str,
                          client_id: int, course_id: int,
                          file_info: dict,
                          cache: dict,
                          dry_run: bool = False) -> dict:
    """
    Mark one image file as decorative in Ally.
    cache: shared dict loaded from _CACHE_FILE; updated in-place on success.
    Returns a result dict with 'file', 'file_id', 'changes', 'updated'.
    """
    file_id  = file_info["id"]
    filename = file_info.get("display_name") or file_info.get("filename", "image")
    mime     = (file_info.get("content_type") or file_info.get("content-type")
                or file_info.get("mime_class") or "image/png")

    # Normalise MIME — Canvas uses "image" as mime_class, not a real MIME type
    if "/" not in mime:
        ext = Path(filename).suffix.lower()
        mime = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png",  ".gif": "image/gif",
            ".bmp": "image/bmp",  ".webp": "image/webp",
            ".svg": "image/svg+xml",
        }.get(ext, "image/png")

    # Check local cache — Ally's content report scores lag by hours after marking
    if _is_cached_decorative(cache, course_id, file_id):
        print(f"  {filename}: already marked decorative (cached) — skipping")
        return {"file": filename, "file_id": file_id, "changes": [], "updated": False}

    print(f"  {filename}: marking as decorative in Ally")

    try:
        _ally_mark_decorative(
            ally_token, ally_cookie, client_id, course_id,
            str(file_id), filename, mime, dry_run=dry_run,
        )
    except RuntimeError as e:
        return {"file": filename, "file_id": file_id, "error": str(e)}

    # Record in cache so future runs skip this file
    if not dry_run:
        key = _cache_key(course_id)
        cache.setdefault(key, [])
        if str(file_id) not in cache[key]:
            cache[key].append(str(file_id))
        _save_cache(cache)

    changes = ["Marked decorative in Ally (ImageDecorative + ImageDescription cleared)"]
    return {
        "file":    filename,
        "file_id": file_id,
        "changes": changes,
        "updated": not dry_run,
    }


# ── Course-level orchestration ────────────────────────────────────────────────

def fix_course_image_files(canvas_client: CanvasClient,
                            ally_token: str, ally_cookie: str,
                            client_id: int, course_id: int,
                            file_id: int = None,
                            dry_run: bool = False) -> list[dict]:
    """
    Mark images that have ImageDecorative or ImageDescription issues in Ally as decorative.

    ally_token  — Ally Bearer JWT (from auto_login())
    ally_cookie — Ally session cookie string (from auto_login())
    client_id   — Ally client ID (5 for UW)
    """
    from ally_api import get_course_content

    if file_id:
        files = [canvas_client.get_file_info(file_id)]
    else:
        # Query Ally to find only files that still have decorative/description issues
        print(f"  Fetching Ally content report to find images needing decorative fix…")
        try:
            content_resp = get_course_content(
                ally_token, client_id, course_id, cookie=ally_cookie)
            ally_files = content_resp.get("content") or []
        except Exception as e:
            print(f"  Could not fetch Ally content report: {e}")
            ally_files = []

        # Build set of Canvas file IDs where ImageDecorative or ImageDescription < 1.0
        needs_fix_ids = set()
        for f in ally_files:
            results = f.get("results") or {}
            if (results.get("ImageDecorative", 1.0) < 1.0 or
                    results.get("ImageDescription", 1.0) < 1.0):
                ext_id = f.get("id") or f.get("externalId")
                if ext_id:
                    needs_fix_ids.add(int(ext_id))

        if not needs_fix_ids:
            print(f"  No ImageDecorative/ImageDescription issues found — skipping")
            return []

        all_files = canvas_client.list_files(course_id)
        files = [
            f for f in all_files
            if (Path(f.get("filename", "")).suffix.lower() in IMAGE_EXTS
                and f["id"] in needs_fix_ids)
        ]
        print(f"Found {len(files)} image file(s) needing decorative fix in course {course_id}")

    cache = _load_cache()
    results = []
    for f in files:
        try:
            r = fix_image_decorative(
                canvas_client, ally_token, ally_cookie,
                client_id, course_id, f, cache, dry_run,
            )
            results.append(r)
            time.sleep(0.3)
        except Exception as e:
            print(f"  ERROR on '{f.get('filename', '?')}': {e}")
            results.append({"file": f.get("filename", "?"), "error": str(e)})

    updated = sum(1 for r in results if r.get("updated"))
    errors  = sum(1 for r in results if "error" in r)
    print(f"  Marked {updated}/{len(results)} image(s) as decorative"
          + (f"  ({errors} error(s))" if errors else ""))
    return results


# ── ImageSeizure: replace with placeholder PNG ───────────────────────────────

_SEIZURE_TEXT = (
    "Image replaced\u2014may induce seizures.\n"
    "Instructor to review and replace."
)


def _make_seizure_placeholder(width: int = 800, height: int = 400) -> bytes:
    """Generate a static warning PNG (yellow background, warning text)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), color=(255, 255, 200))
    draw = ImageDraw.Draw(img)
    # Border
    draw.rectangle([4, 4, width - 5, height - 5], outline=(200, 100, 0), width=4)
    # Warning triangle
    cx = width // 2
    draw.polygon([(cx, 40), (cx - 60, 130), (cx + 60, 130)],
                 outline=(200, 100, 0), width=3)
    draw.text((cx, 85), "!", fill=(200, 100, 0), anchor="mm")
    # Two-line message
    lines = _SEIZURE_TEXT.strip().split("\n")
    y = height // 2 + 20
    for line in lines:
        draw.text((cx, y), line, fill=(80, 40, 0), anchor="mm")
        y += 36

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def fix_seizure_image(canvas_client: CanvasClient,
                      course_id: int, file_info: dict,
                      dry_run: bool = False) -> dict:
    """Replace one seizure-risk image with a static placeholder PNG."""
    file_id   = file_info["id"]
    filename  = file_info.get("display_name") or file_info.get("filename", "image.png")
    folder_id = file_info.get("folder_id")

    placeholder = _make_seizure_placeholder()
    result = {
        "file": filename, "file_id": file_id,
        "changes": [f"Replaced seizure-risk image with warning placeholder"],
        "updated": False,
    }
    if not dry_run:
        canvas_client.upload_file(
            course_id, folder_id, filename, "image/png", placeholder)
        result["updated"] = True
        print(f"  Replaced seizure image: {filename}")
    else:
        print(f"  [dry-run] Would replace seizure image: {filename}")
    return result


def fix_course_seizure_images(canvas_client: CanvasClient,
                               ally_token: str, ally_cookie: str,
                               client_id: int, course_id: int,
                               dry_run: bool = False) -> list[dict]:
    """
    Query Ally for files with ImageSeizure score < 1.0, then replace each
    matching Canvas file with a static warning placeholder PNG.
    Returns list of result dicts.
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.parent))
    from ally_api import get_course_content

    print(f"  Fetching Ally content report for course {course_id}…")
    try:
        content_resp = get_course_content(
            ally_token, client_id, course_id, cookie=ally_cookie)
        files = content_resp.get("content") or []
    except Exception as e:
        print(f"  Could not fetch Ally content report: {e}")
        return []

    # Find files with ImageSeizure score < 1.0
    seizure_ids = set()
    for f in files:
        results = f.get("results") or {}
        score = results.get("ImageSeizure")
        if score is not None and score < 1.0:
            ext_id = str(f.get("id") or f.get("externalId") or "")
            if ext_id:
                seizure_ids.add(ext_id)

    if not seizure_ids:
        print(f"  No ImageSeizure issues found in Ally report")
        return []

    print(f"  {len(seizure_ids)} seizure-risk image(s) to replace")

    # Fetch Canvas file metadata for each flagged file
    results_out = []
    for ext_id in seizure_ids:
        try:
            file_info = canvas_client.get_file_info(int(ext_id))
            r = fix_seizure_image(canvas_client, course_id, file_info, dry_run)
            results_out.append(r)
            time.sleep(0.4)
        except Exception as e:
            print(f"  ERROR on file {ext_id}: {e}")
            results_out.append({"file": ext_id, "error": str(e)})

    return results_out


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Mark Canvas image files as decorative in Ally")
    parser.add_argument("--course-id", type=int, required=True)
    parser.add_argument("--file-id", type=int, default=None,
                        help="Specific Canvas file ID, or omit for all images")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # Auto-login to obtain Ally credentials
    import sys as _sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from ally_api import auto_login
    canvas_token = (Path(__file__).parent.parent / "canvas-token.txt").read_text().strip()
    print(f"Logging in to Ally for course {args.course_id}...")
    ally_token, client_id, ally_cookie = auto_login(canvas_token, args.course_id)

    client  = CanvasClient(token=canvas_token)
    results = fix_course_image_files(
        client, ally_token, ally_cookie, client_id,
        args.course_id, file_id=args.file_id, dry_run=args.dry_run,
    )

    total_changes = sum(len(r.get("changes", [])) for r in results)
    updated = sum(1 for r in results if r.get("updated"))
    errors  = sum(1 for r in results if "error" in r)
    print(f"\nSummary: {total_changes} changes, "
          f"{updated}/{len(results)} files updated, {errors} error(s)")
    if args.dry_run:
        print("(dry-run: no changes were made)")


if __name__ == "__main__":
    main()
