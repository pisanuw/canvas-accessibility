"""
Claude API client — AI-assisted content generation for remediation scripts.

Uses the Anthropic SDK (claude-sonnet-4-6) for:
  - Image alt text generation (vision)
  - Slide title generation
  - Descriptive link label generation
  - Heading identification in plain text

Reads ANTHROPIC_API_KEY from environment.
"""

import base64
import os
from pathlib import Path

# Lazy import so non-AI scripts don't require anthropic to be importable
_client = None


def _get_client():
    global _client
    if _client is None:
        try:
            import anthropic
        except ImportError:
            raise RuntimeError(
                "anthropic library not installed. "
                "Run: pip install anthropic"
            )
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY environment variable not set. "
                "Export it before running AI-assisted fixes:\n"
                "  export ANTHROPIC_API_KEY=your_key_here"
            )
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


MODEL = "claude-sonnet-4-6"

# ── Image Alt Text ────────────────────────────────────────────────────────────

def describe_image(image_bytes: bytes, mime_type: str = "image/png") -> str:
    """
    Generate alt text for an image using Claude Vision.

    Returns the description string, or '' if the image appears decorative.
    Decorative images (icons, dividers, spacers) should have empty alt text.
    """
    client = _get_client()
    b64 = base64.standard_b64encode(image_bytes).decode()

    # Limit image size — resize large images to avoid excessive token use
    image_bytes, mime_type = _maybe_downscale(image_bytes, mime_type)
    b64 = base64.standard_b64encode(image_bytes).decode()

    resp = client.messages.create(
        model=MODEL,
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime_type,
                        "data": b64,
                    },
                },
                {
                    "type": "text",
                    "text": (
                        "Write concise alt text for this image (1–2 sentences, "
                        "under 125 characters) suitable for a screen reader. "
                        "Focus on what the image communicates, not what it looks like. "
                        "If the image is purely decorative (a divider, spacer, "
                        "background pattern, or icon with no informational value), "
                        "reply with exactly: DECORATIVE"
                    ),
                },
            ],
        }],
    )
    text = resp.content[0].text.strip()
    return "" if text.upper() == "DECORATIVE" else text


# ── Slide Title Generation ────────────────────────────────────────────────────

def generate_slide_title(slide_text: str, slide_number: int = 0) -> str:
    """
    Generate a concise title for a PowerPoint slide from its text content.
    Returns a 4–8 word title string.
    """
    client = _get_client()
    prompt = (
        f"Generate a concise title (4–8 words) for slide {slide_number} "
        f"with this content:\n\n{slide_text[:800]}\n\n"
        "Reply with only the title text, no quotes or punctuation at the end."
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=50,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip().rstrip(".")


# ── Link Label Generation ─────────────────────────────────────────────────────

def generate_link_label(link_text: str, context: str, href: str) -> str:
    """
    Generate a descriptive label for a non-descriptive hyperlink.

    link_text: current link text ('here', 'click here', etc.)
    context:   surrounding paragraph text
    href:      the URL the link points to
    Returns a short, descriptive label (3–8 words).
    """
    client = _get_client()
    prompt = (
        f"A hyperlink has the non-descriptive text: '{link_text}'\n"
        f"It links to: {href}\n"
        f"Surrounding paragraph context:\n{context[:500]}\n\n"
        "Write a concise, descriptive link label (3–8 words) that describes "
        "where the link goes or what it does. "
        "Reply with only the label text, no quotes."
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=50,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip().rstrip(".")


# ── Heading Identification ────────────────────────────────────────────────────

def identify_headings(paragraphs: list[dict]) -> list[dict]:
    """
    Given a list of paragraph dicts {text, font_size, bold, index},
    return the same list with 'heading_level' added (1–3 or None).

    Used for Word/PDF documents that lack heading styles.
    """
    client = _get_client()
    items = "\n".join(
        f"[{p['index']}] size={p.get('font_size','?')} bold={p.get('bold','?')}: {p['text'][:100]}"
        for p in paragraphs[:60]
    )
    prompt = (
        "Below are paragraphs from a document with their font size and bold status.\n"
        "Identify which ones are headings (H1, H2, H3) vs body text.\n\n"
        f"{items}\n\n"
        "Reply with a JSON array like: "
        '[{"index": 0, "level": 1}, {"index": 3, "level": 2}, ...] '
        "Only include paragraphs that are headings. "
        "Reply with only the JSON array."
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    import json, re
    text = resp.content[0].text.strip()
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if not match:
        return paragraphs
    heading_map = {h["index"]: h["level"] for h in json.loads(match.group())}
    for p in paragraphs:
        p["heading_level"] = heading_map.get(p["index"])
    return paragraphs


# ── Helpers ───────────────────────────────────────────────────────────────────

def _maybe_downscale(image_bytes: bytes, mime_type: str,
                     max_bytes: int = 1_000_000) -> tuple[bytes, str]:
    """Downscale image if it exceeds max_bytes, to keep API costs low."""
    if len(image_bytes) <= max_bytes:
        return image_bytes, mime_type
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_bytes))
        # Halve dimensions until under limit
        while len(image_bytes) > max_bytes and min(img.size) > 100:
            img = img.resize((img.width // 2, img.height // 2), Image.LANCZOS)
            buf = io.BytesIO()
            fmt = "JPEG" if mime_type == "image/jpeg" else "PNG"
            img.save(buf, format=fmt, optimize=True)
            image_bytes = buf.getvalue()
        return image_bytes, mime_type
    except Exception:
        return image_bytes, mime_type
