"""
HTML / Canvas Page accessibility fixes.

Fixes applied to Canvas wiki page HTML bodies:
  - html.image_alt             : <img> tags missing/filename alt text  (AI-assisted)
  - html.image_alt_placeholder : same, sets reviewer placeholder instead of AI description
  - html.table_headers  : <table> with no <th> cells   (fully automatic)
  - html.heading_order  : heading levels skip           (fully automatic)
  - html.empty_heading  : empty <h1>–<h6> tags          (fully automatic)
  - html.lists          : manual bullet/number lists    (fully automatic)
  - html.links          : non-descriptive link text     (AI-assisted)
  - html.color_contrast : inline color/background-color snapped to black/white
                          (fully automatic — color→#000000, bg→nearest extreme)
  - html.table_captions : <table> missing <caption>         (fully automatic)
  - html.headings_presence : page body has no headings at all (fully automatic)
  - html.broken_links      : hrefs that return 4xx/5xx          (fully automatic)
  - html.html_meta         : <html> missing lang, <head> missing <title> (fully automatic)

Usage (CLI):
  python3 fix_html_pages.py --course-id 1492292 --fix all
  python3 fix_html_pages.py --course-id 1492292 --fix image_alt --page syllabus
  python3 fix_html_pages.py --course-id 1492292 --fix table_headers --dry-run
  python3 fix_html_pages.py --course-id 1492292 --fix color_contrast --dry-run
  python3 fix_html_pages.py --course-id 1492292 --fix table_captions --dry-run
"""

import argparse
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent))
from fixes.canvas_client import CanvasClient
from fixes.ai_client import describe_image, generate_link_label

BASE_URL = "https://canvas.uw.edu"
NON_DESCRIPTIVE_LINKS = {"here", "click here", "read more", "link", "more",
                          "this", "url", "this link", "this page", "this site"}

# File-extension pattern — alt text that is just a filename is not descriptive
_FILENAME_RE = re.compile(
    r'\.[a-zA-Z0-9]{2,5}$'          # ends with an extension
    r'|^[\w\-. ]+\.(png|jpe?g|gif|svg|bmp|webp|tiff?|pdf|docx?|pptx?|xlsx?)$',
    re.IGNORECASE,
)


def _is_filename_alt(text: str) -> bool:
    """Return True if alt text looks like a raw filename rather than a description."""
    t = text.strip()
    if not t:
        return False
    # Matches "image001.png", "Register to Repl.it", "slide1.jpg", etc.
    return bool(_FILENAME_RE.search(t))


# ── Per-page fix functions ────────────────────────────────────────────────────

def fix_image_alt(html: str, client: CanvasClient, course_id: int) -> tuple[str, list[str]]:
    """
    Find <img> tags without alt or with empty alt, fetch the image,
    call Claude Vision to generate alt text, and insert it.
    Returns (updated_html, list_of_changes).
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    changes = []

    for img in soup.find_all("img"):
        alt = img.get("alt")
        if alt is not None and alt.strip() and not _is_filename_alt(alt):
            continue  # already has a real description

        src = img.get("src", "")
        if not src:
            img["alt"] = ""
            changes.append("Set empty alt on <img> with no src (decorative)")
            continue

        # Resolve relative Canvas URLs
        if src.startswith("/"):
            src = BASE_URL + src
        elif not src.startswith("http"):
            continue  # skip data URIs and unusual schemes

        try:
            image_bytes = client.download_url(src)
            mime = _mime_from_src(src)
            description = describe_image(image_bytes, mime)
            img["alt"] = description
            label = description[:60] + "…" if len(description) > 60 else description
            changes.append(f"Added alt text to img: '{label or 'DECORATIVE'}'")
        except Exception as e:
            img["alt"] = ""
            changes.append(f"Could not describe img (set empty alt): {e}")

    return str(soup), changes


_PLACEHOLDER_ALT = (
    "This image currently does not have a description. "
    "Your instructor will review it and add a description."
)


def fix_image_alt_placeholder(html: str) -> tuple[str, list[str]]:
    """
    Find <img> tags whose alt is missing, empty, or looks like a filename,
    and replace with a standard reviewer placeholder.
    Does not require an AI key. Idempotent — skips images that already have
    a real (non-filename) description.
    Returns (updated_html, list_of_changes).
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    changes = []

    for img in soup.find_all("img"):
        alt = img.get("alt", "")
        if alt == _PLACEHOLDER_ALT:
            continue  # already set by a previous run
        if alt.strip() and not _is_filename_alt(alt):
            continue  # already has a real description
        src = img.get("src", "")
        label = src.split("/")[-1].split("?")[0][:50] or "<img>"
        img["alt"] = _PLACEHOLDER_ALT
        changes.append(f"Set placeholder alt on img '{label}' (was: '{alt[:40]}')")

    return str(soup), changes


def fix_table_headers(html: str) -> tuple[str, list[str]]:
    """
    Find <table> elements with no <th> cells.
    Convert the first row's <td> cells to <th scope='col'>.
    Wrap in <thead>/<tbody> if not already present.
    Returns (updated_html, list_of_changes).
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    changes = []

    for table in soup.find_all("table"):
        if table.find("th"):
            continue  # already has headers

        rows = table.find_all("tr")
        if not rows:
            continue

        first_row = rows[0]
        tds = first_row.find_all("td")
        if not tds:
            continue

        # Convert first row tds → th scope="col"
        for td in tds:
            td.name = "th"
            td["scope"] = "col"

        # Wrap in thead/tbody if not present
        if not table.find("thead"):
            thead = soup.new_tag("thead")
            first_row.wrap(thead)
            if len(rows) > 1 and not table.find("tbody"):
                tbody = soup.new_tag("tbody")
                for row in rows[1:]:
                    tbody.append(row.extract())
                table.append(tbody)

        changes.append(f"Added <th scope='col'> to table ({len(tds)} columns)")

    return str(soup), changes


def fix_heading_order(html: str) -> tuple[str, list[str]]:
    """
    Normalize heading level sequence so no level is skipped.
    E.g. h1 → h3 becomes h1 → h2.
    Returns (updated_html, list_of_changes).
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    changes = []

    headings = soup.find_all(re.compile(r'^h[1-6]$'))
    if not headings:
        return html, changes

    prev_level = 0
    for tag in headings:
        level = int(tag.name[1])
        if prev_level > 0 and level > prev_level + 1:
            new_level = prev_level + 1
            old_name = tag.name
            tag.name = f"h{new_level}"
            changes.append(f"Renamed <{old_name}> → <h{new_level}> (level skip fix)")
            level = new_level
        prev_level = level

    return str(soup), changes


_H1_PLACEHOLDER = "Document Title (placeholder — please replace with actual title)"


def fix_headings_start_at_one(html: str) -> tuple[str, list[str]]:
    """
    Ensure the page opens with an <h1>.  Two cases:
      1. No H1 exists at all (min heading is H2+): insert placeholder before first heading.
      2. H1 exists but is not the first heading: insert placeholder before the first heading.
    Idempotent: skips pages whose first heading is already our placeholder H1.
    Returns (updated_html, list_of_changes).
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    # Remove any misplaced placeholder from a prior run before re-evaluating
    for tag in soup.find_all("h1"):
        if _H1_PLACEHOLDER in (tag.get_text() or ""):
            tag.decompose()
            break

    headings = soup.find_all(re.compile(r'^h[1-6]$'))
    if not headings:
        return html, []

    first = headings[0]
    min_level = min(int(h.name[1]) for h in headings)

    # Check if the very first non-empty element is already a real H1 at the top
    first_elem = next(
        (c for c in (soup.find("body") or soup).children
         if hasattr(c, "name") and c.name),
        None
    )
    if first_elem and first_elem.name == "h1":
        return html, []  # already starts with H1

    h1 = soup.new_tag("h1")
    h1.string = _H1_PLACEHOLDER

    # Always insert at the very top of the body, not just before the first heading
    body_tag = soup.find("body")
    if body_tag and body_tag.contents:
        body_tag.insert(0, h1)
    elif soup.contents:
        soup.contents[0].insert_before(h1)
    else:
        soup.append(h1)

    if min_level == 1:
        reason = "H1 exists but is not the first element"
    else:
        reason = f"no H1 found (first heading was <h{min_level}>)"

    return str(soup), [f"Inserted placeholder <h1> at top ({reason})"]


def fix_empty_headings(html: str) -> tuple[str, list[str]]:
    """
    Remove heading tags that contain no visible text.
    Returns (updated_html, list_of_changes).
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    changes = []

    for tag in soup.find_all(re.compile(r'^h[1-6]$')):
        if not tag.get_text(strip=True):
            tag.decompose()
            changes.append(f"Removed empty <{tag.name}>")

    return str(soup), changes


def fix_manual_lists(html: str) -> tuple[str, list[str]]:
    """
    Detect consecutive <p> tags that start with bullet/number patterns
    and convert them to proper <ul> or <ol> elements.
    Returns (updated_html, list_of_changes).
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    changes = []

    UNORDERED = re.compile(r'^[\s\u00a0]*[•\-\*–◦]\s+')
    ORDERED   = re.compile(r'^[\s\u00a0]*(\d+[.)]\s+|[a-zA-Z][.)]\s+)')

    paragraphs = soup.find_all("p")
    i = 0
    while i < len(paragraphs):
        p = paragraphs[i]
        text = p.get_text()
        if UNORDERED.match(text):
            list_tag, strip_pat, kind = soup.new_tag("ul"), UNORDERED, "ul"
        elif ORDERED.match(text):
            list_tag, strip_pat, kind = soup.new_tag("ol"), ORDERED, "ol"
        else:
            i += 1
            continue

        # Collect consecutive matching paragraphs
        group = []
        j = i
        while j < len(paragraphs) and strip_pat.match(paragraphs[j].get_text()):
            group.append(paragraphs[j])
            j += 1

        if len(group) < 2:
            i += 1
            continue

        # Build list
        for gp in group:
            li = soup.new_tag("li")
            inner = strip_pat.sub("", gp.get_text()).strip()
            li.string = inner
            list_tag.append(li)

        # Insert list before first item, remove group
        group[0].insert_before(list_tag)
        for gp in group:
            gp.decompose()

        changes.append(f"Converted {len(group)} <p> bullets to <{kind}>")
        # Refresh paragraph list
        paragraphs = soup.find_all("p")
        i = 0

    return str(soup), changes


def fix_links(html: str, context_hint: str = "") -> tuple[str, list[str]]:
    """
    Fix non-descriptive link text ('here', 'click here', bare URLs, etc.).
    For bare URLs: use a human-readable version of the URL.
    For generic words: use Claude API with surrounding context.
    Returns (updated_html, list_of_changes).
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    changes = []

    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a.get("href", "")
        if not text:
            continue

        low = text.lower()

        # Bare URL as link text
        if re.match(r'https?://', text):
            label = _readable_url(text)
            a.string = label
            changes.append(f"Replaced bare URL link with: '{label}'")
            continue

        if low in NON_DESCRIPTIVE_LINKS:
            # Get surrounding paragraph context for Claude
            parent = a.find_parent(["p", "li", "td", "div"])
            context = parent.get_text(strip=True) if parent else context_hint
            try:
                label = generate_link_label(text, context, href)
                a.string = label
                changes.append(f"Replaced '{text}' link with: '{label}'")
            except Exception as e:
                changes.append(f"Could not relabel '{text}' link: {e}")

    return str(soup), changes


def fix_broken_links(html: str, timeout: int = 5,
                     max_links: int = 30) -> tuple[str, list[str]]:
    """
    HEAD-request each external <a href> link; replace broken ones (4xx/5xx or
    connection error) with href="#" and annotate the link text.
    Skips mailto:, tel:, anchor (#), and Canvas-internal links.
    Caps at max_links per page to limit latency.
    Returns (updated_html, list_of_changes).
    """
    import urllib.request
    from urllib.error import HTTPError, URLError
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    changes = []
    checked = 0

    for a in soup.find_all("a", href=True):
        if checked >= max_links:
            break
        href = a.get("href", "").strip()
        if not href or not href.startswith("http"):
            continue  # skip relative, anchor, mailto:, tel:
        checked += 1

        try:
            req = urllib.request.Request(
                href, method="HEAD",
                headers={"User-Agent": "Canvas-Accessibility-Checker/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status < 400:
                    continue  # OK
                broken = True
        except HTTPError as e:
            broken = e.code >= 400
        except URLError:
            broken = True
        except Exception:
            broken = True

        if broken:
            original = a.get_text(strip=True)[:60]
            a["href"] = "#"
            a.string = f"[broken link — was: '{original}' — instructor to review]"
            changes.append(f"Replaced broken link ({href[:60]})")

    return str(soup), changes


def fix_html_meta(html: str, page_title: str = "") -> tuple[str, list[str]]:
    """
    Add lang='en' to <html> if missing, and <title> to <head> if missing.
    Canvas page bodies are partial HTML fragments; this handles both full
    documents and bare fragments gracefully.
    Returns (updated_html, list_of_changes).
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    changes = []

    html_tag = soup.find("html")
    if html_tag and not html_tag.get("lang"):
        html_tag["lang"] = "en"
        changes.append("Added lang='en' to <html>")

    head = soup.find("head")
    if head and not head.find("title"):
        title_tag = soup.new_tag("title")
        title_tag.string = page_title or "Canvas Page"
        head.insert(0, title_tag)
        changes.append(f"Added <title>{title_tag.string}</title>")

    return str(soup), changes


def fix_table_captions(html: str) -> tuple[str, list[str]]:
    """
    Add a placeholder <caption> to every <table> that lacks one.
    Ally requires each table to have a visible caption for context.
    Returns (updated_html, list_of_changes).
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    changes = []
    CAPTION_TEXT = "Table — caption to be reviewed by instructor"

    for table in soup.find_all("table"):
        if table.find("caption"):
            continue
        cap = soup.new_tag("caption")
        cap.string = CAPTION_TEXT
        table.insert(0, cap)
        changes.append("Added placeholder <caption> to table")

    return str(soup), changes


def fix_headings_presence(html: str) -> tuple[str, list[str]]:
    """
    If the page body contains no heading elements (<h1>–<h6>), insert a
    placeholder <h2> before the first block-level element.
    Returns (updated_html, list_of_changes).
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    if soup.find(re.compile(r'^h[1-6]$')):
        return html, []  # already has at least one heading

    PLACEHOLDER = "Section — Heading to be Reviewed by Instructor"
    BLOCK_TAGS = {"p", "div", "table", "ul", "ol", "blockquote", "pre", "figure"}

    first_block = soup.find(BLOCK_TAGS)
    h2 = soup.new_tag("h2")
    h2.string = PLACEHOLDER

    if first_block:
        first_block.insert_before(h2)
    else:
        # Fallback: prepend to the whole body
        soup.insert(0, h2)

    return str(soup), [f"Inserted placeholder <h2>: '{PLACEHOLDER}'"]


def fix_color_contrast(html: str) -> tuple[str, list[str]]:
    """
    Snap inline style color values to black or white for WCAG AA contrast.

      color:            (text)  → always #000000 (black)
      background-color:         → luminance > 0.5 → #ffffff (white)
                                  luminance ≤ 0.5 → #000000 (black)

    Only affects inline style= attributes; CSS classes are not changed.
    Returns (updated_html, list_of_changes).
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    changes = []

    for tag in soup.find_all(style=True):
        style = tag["style"]
        new_style, tag_changes = _snap_style_colors(style, tag.name)
        if tag_changes:
            tag["style"] = new_style
            changes.extend(tag_changes)

    return str(soup), changes


# ── Color helpers (used by fix_color_contrast) ────────────────────────────────

_NAMED_COLORS = {
    "black": "#000000", "white": "#ffffff", "red": "#ff0000",
    "green": "#008000", "blue": "#0000ff", "yellow": "#ffff00",
    "orange": "#ffa500", "purple": "#800080", "gray": "#808080",
    "grey": "#808080", "pink": "#ffc0cb", "brown": "#a52a2a",
    "cyan": "#00ffff", "magenta": "#ff00ff", "lime": "#00ff00",
    "maroon": "#800000", "navy": "#000080", "olive": "#808000",
    "teal": "#008080", "silver": "#c0c0c0", "aqua": "#00ffff",
    "fuchsia": "#ff00ff", "coral": "#ff7f50", "salmon": "#fa8072",
    "gold": "#ffd700", "khaki": "#f0e68c", "indigo": "#4b0082",
    "violet": "#ee82ee", "turquoise": "#40e0d0",
    "transparent": None,  # skip
}


def _parse_color(value: str):
    """Return (r, g, b) ints from a CSS color string, or None if unparseable."""
    v = value.strip().lower()
    if v in _NAMED_COLORS:
        h = _NAMED_COLORS[v]
        if h is None:
            return None
        v = h
    if v.startswith("#"):
        h = v[1:]
        if len(h) == 3:
            h = h[0] * 2 + h[1] * 2 + h[2] * 2
        if len(h) == 6:
            try:
                return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
            except ValueError:
                return None
        return None
    m = re.match(r'rgb\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)', v)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _relative_luminance(r: int, g: int, b: int) -> float:
    """WCAG relative luminance: 0 = black, 1 = white."""
    def _lin(c):
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _snap_style_colors(style: str, tag_name: str) -> tuple[str, list[str]]:
    """
    Process a CSS style= string, snap color/background-color to black/white.
    Splits on ';' to avoid matching partial property names.
    Returns (new_style_string, list_of_human_readable_changes).
    """
    changes = []
    parts = style.split(";")
    new_parts = []

    for part in parts:
        stripped = part.strip()
        if not stripped or ":" not in stripped:
            new_parts.append(part)
            continue

        prop, _, val = stripped.partition(":")
        prop = prop.strip().lower()
        val = val.strip()

        if prop not in ("color", "background-color"):
            new_parts.append(part)
            continue

        # Already at target value — leave unchanged
        if prop == "color" and val.lower() in ("#000000", "#000", "black"):
            new_parts.append(part)
            continue
        if prop == "background-color" and val.lower() in (
                "#ffffff", "#fff", "white", "#000000", "#000", "black"):
            new_parts.append(part)
            continue

        rgb = _parse_color(val)
        if rgb is None:
            new_parts.append(part)
            continue

        if prop == "color":
            new_val = "#000000"
        else:  # background-color
            lum = _relative_luminance(*rgb)
            new_val = "#ffffff" if lum > 0.5 else "#000000"

        changes.append(f"<{tag_name}> {prop}: {val} → {new_val}")
        new_parts.append(f" {prop}: {new_val}")

    return ";".join(new_parts), changes


# ── Orchestrate all fixes on one page ─────────────────────────────────────────

def fix_page(client: CanvasClient, course_id: int, page: dict,
             fixes: list[str], dry_run: bool = False) -> dict:
    """
    Apply the requested fixes to a single page.
    fixes: list of fix names from ['image_alt', 'table_headers',
           'heading_order', 'empty_heading', 'lists', 'links', 'all']
    Returns a result dict with 'page', 'changes', 'updated' keys.
    """
    page_url = page.get("url")
    title = page.get("title", page_url)

    # Fetch full page with body
    full = client.get_page(course_id, page_url)
    body = full.get("body") or ""
    if not body.strip():
        return {"page": title, "changes": [], "updated": False}

    all_fixes = "all" in fixes
    all_changes = []

    if all_fixes or "empty_heading" in fixes:
        body, ch = fix_empty_headings(body)
        all_changes.extend(ch)

    if all_fixes or "headings_start_at_one" in fixes:
        body, ch = fix_headings_start_at_one(body)
        all_changes.extend(ch)

    if all_fixes or "heading_order" in fixes:
        body, ch = fix_heading_order(body)
        all_changes.extend(ch)

    if all_fixes or "table_headers" in fixes:
        body, ch = fix_table_headers(body)
        all_changes.extend(ch)

    if all_fixes or "lists" in fixes:
        body, ch = fix_manual_lists(body)
        all_changes.extend(ch)

    if all_fixes or "image_alt" in fixes:
        body, ch = fix_image_alt(body, client, course_id)
        all_changes.extend(ch)

    if "image_alt_placeholder" in fixes:
        body, ch = fix_image_alt_placeholder(body)
        all_changes.extend(ch)

    if all_fixes or "links" in fixes:
        body, ch = fix_links(body)
        all_changes.extend(ch)

    if all_fixes or "color_contrast" in fixes:
        body, ch = fix_color_contrast(body)
        all_changes.extend(ch)

    if all_fixes or "table_captions" in fixes:
        body, ch = fix_table_captions(body)
        all_changes.extend(ch)

    if all_fixes or "headings_presence" in fixes:
        body, ch = fix_headings_presence(body)
        all_changes.extend(ch)

    if "broken_links" in fixes:  # NOT included in "all" — too slow for every page
        body, ch = fix_broken_links(body)
        all_changes.extend(ch)

    if all_fixes or "html_meta" in fixes:
        body, ch = fix_html_meta(body, page_title=title)
        all_changes.extend(ch)

    result = {"page": title, "changes": all_changes, "updated": False}

    if all_changes and not dry_run:
        client.update_page(course_id, page_url, body)
        result["updated"] = True
        print(f"  Updated: {title} ({len(all_changes)} fix(es))")
        for ch in all_changes:
            print(f"    • {ch}")
    elif all_changes:
        print(f"  [dry-run] Would update: {title} ({len(all_changes)} changes)")
        for ch in all_changes:
            print(f"    • {ch}")
    else:
        print(f"  No changes: {title}")

    return result


def fix_course_pages(client: CanvasClient, course_id: int,
                     fixes: list[str], page_url: str = None,
                     dry_run: bool = False) -> list[dict]:
    """
    Run fixes on one page (if page_url given) or all pages in a course.
    Returns list of result dicts.
    """
    if page_url:
        pages = [client.get_page(course_id, page_url)]
    else:
        pages = client.list_pages(course_id)
        print(f"Found {len(pages)} pages in course {course_id}")

    results = []
    for page in pages:
        try:
            r = fix_page(client, course_id, page, fixes, dry_run)
            results.append(r)
            time.sleep(0.3)  # gentle rate limiting
        except Exception as e:
            print(f"  ERROR on page '{page.get('title', '?')}': {e}")
            results.append({"page": page.get("title", "?"), "error": str(e)})

    return results


def fix_course_syllabus(client: CanvasClient, course_id: int,
                        fixes: list[str], dry_run: bool = False) -> list[dict]:
    """
    Run HTML fixes on the course syllabus body.
    Returns a one-element list (or empty list if no syllabus).
    """
    body = client.get_syllabus(course_id)
    if not body or not body.strip():
        print("  No syllabus body found — skipping")
        return []

    print("  Processing: Syllabus")
    all_changes = []
    all_fixes = "all" in fixes

    if all_fixes or "headings_start_at_one" in fixes:
        body, ch = fix_headings_start_at_one(body)
        all_changes.extend(ch)
    if all_fixes or "empty_heading" in fixes:
        body, ch = fix_empty_headings(body)
        all_changes.extend(ch)
    if all_fixes or "heading_order" in fixes:
        body, ch = fix_heading_order(body)
        all_changes.extend(ch)
    if all_fixes or "table_headers" in fixes:
        body, ch = fix_table_headers(body)
        all_changes.extend(ch)
    if all_fixes or "lists" in fixes:
        body, ch = fix_manual_lists(body)
        all_changes.extend(ch)
    if all_fixes or "image_alt" in fixes:
        body, ch = fix_image_alt(body, client, course_id)
        all_changes.extend(ch)
    if "image_alt_placeholder" in fixes:
        body, ch = fix_image_alt_placeholder(body)
        all_changes.extend(ch)
    if all_fixes or "links" in fixes:
        body, ch = fix_links(body)
        all_changes.extend(ch)
    if all_fixes or "color_contrast" in fixes:
        body, ch = fix_color_contrast(body)
        all_changes.extend(ch)
    if all_fixes or "table_captions" in fixes:
        body, ch = fix_table_captions(body)
        all_changes.extend(ch)

    result = {"page": "Syllabus", "changes": all_changes, "updated": False}

    if all_changes and not dry_run:
        try:
            client.update_syllabus(course_id, body)
            result["updated"] = True
            print(f"  Updated: Syllabus ({len(all_changes)} fix(es))")
            for ch in all_changes:
                print(f"    • {ch}")
        except Exception as e:
            print(f"  ERROR updating syllabus: {e}")
            result["error"] = str(e)
    elif all_changes:
        print(f"  [dry-run] Would update: Syllabus ({len(all_changes)} changes)")
        for ch in all_changes:
            print(f"    • {ch}")
    else:
        print(f"  No changes: Syllabus")

    return [result]


def fix_course_assignments(client: CanvasClient, course_id: int,
                            fixes: list[str], dry_run: bool = False) -> list[dict]:
    """
    Run the same HTML fixes on assignment description bodies.
    Only fixes that apply to HTML content are run (html_meta and headings_presence
    are skipped — assignment descriptions are fragments, not full documents).
    Returns list of result dicts.
    """
    # Subset of fixes that make sense on assignment descriptions
    _SKIP_FOR_ASSIGNMENTS = {"html_meta", "headings_presence", "broken_links"}
    assignment_fixes = [f for f in fixes if f not in _SKIP_FOR_ASSIGNMENTS]
    if not assignment_fixes:
        return []

    assignments = client.list_assignments(course_id)
    # Only process assignments that have an HTML description
    assignments = [a for a in assignments if a.get("description")]
    print(f"Found {len(assignments)} assignment(s) with descriptions in course {course_id}")

    results = []
    for a in assignments:
        assignment_id = a["id"]
        title = a.get("name", f"Assignment {assignment_id}")
        body = a.get("description") or ""

        print(f"  Processing assignment: {title}")
        all_changes = []
        all_fixes = "all" in assignment_fixes

        if all_fixes or "empty_heading" in assignment_fixes:
            body, ch = fix_empty_headings(body)
            all_changes.extend(ch)
        if all_fixes or "heading_order" in assignment_fixes:
            body, ch = fix_heading_order(body)
            all_changes.extend(ch)
        if all_fixes or "table_headers" in assignment_fixes:
            body, ch = fix_table_headers(body)
            all_changes.extend(ch)
        if all_fixes or "lists" in assignment_fixes:
            body, ch = fix_manual_lists(body)
            all_changes.extend(ch)
        if all_fixes or "image_alt" in assignment_fixes:
            body, ch = fix_image_alt(body, client, course_id)
            all_changes.extend(ch)
        if "image_alt_placeholder" in assignment_fixes:
            body, ch = fix_image_alt_placeholder(body)
            all_changes.extend(ch)
        if all_fixes or "links" in assignment_fixes:
            body, ch = fix_links(body)
            all_changes.extend(ch)
        if all_fixes or "color_contrast" in assignment_fixes:
            body, ch = fix_color_contrast(body)
            all_changes.extend(ch)
        if all_fixes or "table_captions" in assignment_fixes:
            body, ch = fix_table_captions(body)
            all_changes.extend(ch)

        result = {"page": title, "changes": all_changes, "updated": False}

        if all_changes and not dry_run:
            try:
                client.update_assignment(course_id, assignment_id, body)
                result["updated"] = True
                print(f"  Updated: {title} ({len(all_changes)} fix(es))")
                for ch in all_changes:
                    print(f"    • {ch}")
            except Exception as e:
                print(f"  ERROR updating assignment '{title}': {e}")
                result["error"] = str(e)
        elif all_changes:
            print(f"  [dry-run] Would update: {title} ({len(all_changes)} changes)")
            for ch in all_changes:
                print(f"    • {ch}")
        else:
            print(f"  No changes: {title}")

        results.append(result)
        time.sleep(0.3)

    return results


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mime_from_src(src: str) -> str:
    ext = Path(urlparse(src).path).suffix.lower()
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif",
        ".webp": "image/webp", ".bmp": "image/bmp",
    }.get(ext, "image/png")


def _readable_url(url: str) -> str:
    parsed = urlparse(url)
    domain = parsed.netloc.replace("www.", "")
    slug = parsed.path.rstrip("/").split("/")[-1].replace("-", " ").replace("_", " ")
    slug = re.sub(r'\.\w+$', '', slug)  # remove extension
    return f"{slug} ({domain})" if slug else domain


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fix HTML accessibility issues in Canvas pages")
    parser.add_argument("--course-id", type=int, required=True)
    parser.add_argument("--page", default=None,
                        help="URL slug of a specific page, or omit for all pages")
    parser.add_argument("--fix", default="all",
                        help="Comma-separated: all,image_alt,table_headers,"
                             "heading_order,empty_heading,lists,links,"
                             "color_contrast,table_captions,headings_presence,"
                             "broken_links,html_meta")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without updating Canvas")
    args = parser.parse_args()

    fix_list = [f.strip() for f in args.fix.split(",")]
    client = CanvasClient()

    results = fix_course_pages(client, args.course_id, fix_list,
                               page_url=args.page, dry_run=args.dry_run)

    total_changes = sum(len(r.get("changes", [])) for r in results)
    updated = sum(1 for r in results if r.get("updated"))
    print(f"\nSummary: {total_changes} changes across {updated}/{len(results)} pages")


if __name__ == "__main__":
    main()
