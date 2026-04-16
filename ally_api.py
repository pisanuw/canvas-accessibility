#!/usr/bin/env python3
"""
Ally Course Accessibility Report

Automatically fetches the real Ally accessibility data using only the Canvas
API token (canvas-token.txt). No browser, no copy-paste from DevTools needed.

How it works:
  1. Canvas API `sessionless_launch` returns a signed launch URL
  2. Following that URL makes Canvas generate an LTI 1.3 JWT (id_token)
     and return an HTML auto-submit form targeting Ally's callback
  3. We POST that form to Ally, which sets a session cookie and lets us in
  4. The id_token becomes the Bearer token; the Ally session cookie completes auth

Usage:
  python3 ally_api.py --course-id 1492302
  python3 ally_api.py --course-id 1492302 --summary-only
  python3 ally_api.py --course-id 1492302 --feedback    # per-file detail
  python3 ally_api.py --course-id 1492302 --debug       # show raw HTTP

For multiple courses:
  for id in 1492302 1492292 1541338; do
    python3 ally_api.py --course-id $id --output ally_real_report_${id}.json
  done

Manual fallback (if auto-login fails):
  Paste a cURL from DevTools into ally-token.txt (see --help for details).
"""

import argparse
import base64
import http.cookiejar
import html as html_module
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

CANVAS_BASE  = os.environ.get("CANVAS_BASE_URL", "https://canvas.uw.edu").rstrip("/")
ALLY_BASE    = "https://prod.ally.ac"
ALLY_TOOL_ID = int(os.environ.get("ALLY_TOOL_ID", "148172"))
TOKEN_FILE   = Path(__file__).parent / "ally-token.txt"
CANVAS_TOKEN_FILE = Path(__file__).parent / "canvas-token.txt"
USER_AGENT   = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ── Auto-login: Canvas token → Ally JWT + session cookie ──────────────────────

def auto_login(canvas_token: str, course_id: int,
               tool_id: int = ALLY_TOOL_ID,
               debug: bool = False) -> tuple[str, int, str]:
    """
    Use the Canvas API token to drive the LTI 1.3 launch flow and obtain:
      - Ally Bearer JWT (id_token from LTI launch)
      - Ally client ID (from the Ally callback redirect URL)
      - Ally session cookie string

    Returns (bearer_token, client_id, cookie_string).
    """
    print("  Auto-login: using Canvas token to obtain Ally session…")

    # ── Step 1: Canvas sessionless launch URL ─────────────────────────────────
    launch_url = _canvas_sessionless_launch_url(canvas_token, course_id, tool_id)
    if debug:
        print(f"  Launch URL: {launch_url}")

    # ── Step 2: Follow the launch URL through OIDC flow ───────────────────────
    # Canvas processes the launch, generates an LTI 1.3 id_token, and returns
    # an HTML page with an auto-submit form pointing to Ally's callback URL.
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        _NoRedirect(),           # handle redirects manually so we can inspect
    )

    html_body, final_url = _follow_to_form(opener, launch_url, debug=debug)

    if debug:
        print(f"  Phase 1 form URL: {final_url}")
        print(f"  Phase 1 form fields: {_parse_form(html_body)[1].keys()}")

    # ── Step 3a: Parse the OIDC initiation form (Phase 1) ────────────────────
    # Canvas returns an OIDC login_hint form first (LTI 1.3 3rd-party initiation).
    # We POST it to Ally's OIDC endpoint; Ally redirects back to Canvas which
    # then returns the real id_token form.
    form_action, form_data = _parse_form(html_body)
    if not form_action:
        raise RuntimeError(
            "Could not find LTI launch form in Canvas response.\n"
            "Run with --debug to see the HTML."
        )

    if "id_token" not in form_data:
        # Phase 1: OIDC initiation — POST to Ally, then follow back to Canvas
        if debug:
            print(f"  Phase 1 action: {form_action}")
            print(f"  Posting OIDC initiation to Ally…")
        post_body = urllib.parse.urlencode(form_data).encode()
        req = urllib.request.Request(
            form_action,
            data=post_body,
            headers={
                "User-Agent":   USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin":       CANVAS_BASE,
                "Referer":      final_url,
            },
        )
        try:
            resp = opener.open(req, timeout=30)
            html_body = resp.read().decode("utf-8", errors="replace")
            final_url = resp.geturl()
        except _RedirectStopped as e:
            html_body, final_url = _follow_to_form(opener, e.location, debug=debug)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                loc = e.headers.get("Location", "")
                if not loc.startswith("http"):
                    loc = ALLY_BASE + loc
                html_body, final_url = _follow_to_form(opener, loc, debug=debug)
            else:
                body = e.read().decode()[:300]
                raise RuntimeError(f"OIDC initiation POST failed: HTTP {e.code}: {body}")

        if debug:
            print(f"  Phase 2 form URL: {final_url}")

        form_action, form_data = _parse_form(html_body)
        if not form_action:
            raise RuntimeError(
                "Phase 2: Could not find id_token form.\n"
                f"Final URL: {final_url}\n"
                f"HTML (first 300): {html_body[:300]}"
            )

    # ── Step 3b: Extract id_token ─────────────────────────────────────────────
    bearer_token = form_data.get("id_token", "")
    if not bearer_token:
        raise RuntimeError(
            "LTI form found but no id_token field present.\n"
            f"Form fields: {list(form_data.keys())}\n"
            "Run with --debug for details."
        )

    if debug:
        print(f"  Form action: {form_action}")
        print(f"  Form fields: {list(form_data.keys())}")
        print(f"  id_token prefix: {bearer_token[:40]}…")

    # ── Step 4: POST to Ally's LTI callback ──────────────────────────────────
    # Ally validates the id_token, sets a session cookie, and redirects to the
    # Ally UI with a ?token=<jwt> query param — THAT is the real API Bearer token.
    post_body = urllib.parse.urlencode(form_data).encode()
    req = urllib.request.Request(
        form_action,
        data=post_body,
        headers={
            "User-Agent":   USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin":       CANVAS_BASE,
            "Referer":      CANVAS_BASE,
        },
    )
    redirect_url = ""
    try:
        resp = opener.open(req, timeout=30)
        resp.read()
        redirect_url = resp.geturl()
    except _RedirectStopped as e:
        redirect_url = e.location
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            redirect_url = e.headers.get("Location", "")
            if not redirect_url.startswith("http"):
                redirect_url = ALLY_BASE + redirect_url
        else:
            body = e.read().decode()[:300]
            if debug:
                print(f"  Ally callback HTTP {e.code}: {body}")
            raise RuntimeError(f"Ally callback failed: HTTP {e.code}: {body}")

    if debug:
        print(f"  Ally redirect URL: {redirect_url[:120]}")

    # Follow the redirect to actually load the page (which also sets session cookie)
    if redirect_url:
        try:
            resp2 = opener.open(
                urllib.request.Request(redirect_url,
                                       headers={"User-Agent": USER_AGENT,
                                                "Accept": "text/html,*/*"}),
                timeout=30)
            resp2.read()
            redirect_url = resp2.geturl()
        except _RedirectStopped as e:
            redirect_url = e.location
        except Exception:
            pass

    # ── Step 5: Extract real Ally Bearer token from redirect URL ─────────────
    # Ally embeds the API token as ?token=<jwt> in the UI redirect URL
    parsed_url = urllib.parse.urlparse(redirect_url)
    qs = urllib.parse.parse_qs(parsed_url.query)
    api_token = qs.get("token", [""])[0]
    if api_token:
        bearer_token = api_token  # replace LTI id_token with Ally's own JWT
    if debug:
        print(f"  API Bearer prefix: {bearer_token[:40]}…")

    # ── Step 6: Collect Ally session cookies ─────────────────────────────────
    ally_cookies = "; ".join(
        f"{c.name}={c.value}"
        for c in cj
        if "ally.ac" in c.domain
    )
    if debug:
        print(f"  Ally cookies: {ally_cookies[:100]}")

    # ── Step 7: Extract client ID from cookie name (session-{N}=) ────────────
    client_id = _extract_client_id_from_cookie(ally_cookies)
    if not client_id:
        # Also check the redirect URL path: /ir/clients/{N}/courses/...
        m = re.search(r'/clients/(\d+)/', redirect_url)
        if m:
            client_id = int(m.group(1))
    if not client_id:
        client_id = _extract_client_id_from_jwt(bearer_token)
    if not client_id:
        client_id = _discover_client_id(bearer_token, ally_cookies)

    print(f"  Auto-login succeeded  (client_id={client_id})")
    return bearer_token, client_id, ally_cookies


def _canvas_sessionless_launch_url(canvas_token: str, course_id: int,
                                    tool_id: int) -> str:
    """Call Canvas API to get a sessionless LTI launch URL."""
    url = (f"{CANVAS_BASE}/api/v1/courses/{course_id}"
           f"/external_tools/sessionless_launch?id={tool_id}")
    req = Request(url, headers={
        "Authorization": f"Bearer {canvas_token}",
        "Accept": "application/json",
    })
    try:
        data = json.loads(urlopen(req, timeout=30).read())
        return data["url"]
    except HTTPError as e:
        body = e.read().decode()[:300]
        raise RuntimeError(
            f"Canvas sessionless_launch failed (HTTP {e.code}): {body}\n"
            "Check that your canvas-token.txt is valid and the course ID is correct."
        )
    except KeyError:
        raise RuntimeError("Canvas sessionless_launch response has no 'url' field.")


def _follow_to_form(opener, start_url: str, debug: bool = False) -> tuple[str, str]:
    """
    Follow redirects from start_url until we reach an HTML page containing
    a <form> element (the LTI auto-submit form).
    Returns (html_body, final_url).
    """
    url = start_url
    for _ in range(10):
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,*/*",
        })
        try:
            resp = opener.open(req, timeout=30)
            body = resp.read().decode("utf-8", errors="replace")
            final_url = resp.geturl()
            if "<form" in body.lower():
                return body, final_url
            # Check for meta-refresh redirect
            m = re.search(r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^;]+;\s*url=([^"\']+)',
                          body, re.IGNORECASE)
            if m:
                url = html_module.unescape(m.group(1).strip())
                continue
            # If no form and no redirect, return what we have
            return body, final_url
        except _RedirectStopped as e:
            url = e.location
            continue
        except HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                url = e.headers.get("Location", "")
                if not url.startswith("http"):
                    url = CANVAS_BASE + url
                continue
            raise

    raise RuntimeError(f"Too many redirects following LTI launch from {start_url}")


def _parse_form(html_body: str) -> tuple[str, dict]:
    """
    Extract action URL and all input fields from the first <form> in the HTML.
    Returns (action_url, {field_name: field_value}).
    """
    # Find form action
    action_m = re.search(
        r'<form[^>]+action=["\']([^"\']+)["\']', html_body, re.IGNORECASE)
    if not action_m:
        return "", {}

    action = html_module.unescape(action_m.group(1))

    # Find all input fields (name/value in either order, value may span quotes)
    fields = {}
    for m in re.finditer(r'<input([^>]+)>', html_body, re.IGNORECASE):
        attrs = m.group(1)
        name_m  = re.search(r'\bname=["\']([^"\']*)["\']',  attrs, re.IGNORECASE)
        value_m = re.search(r'\bvalue=["\']([^"\']*)["\']', attrs, re.IGNORECASE)
        if name_m:
            fields[name_m.group(1)] = html_module.unescape(value_m.group(1)) \
                if value_m else ""

    return action, fields


def _extract_client_id_from_jwt(token: str) -> int | None:
    """Decode the JWT payload and look for an Ally client ID claim."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        # Ally embeds the client ID in a custom claim or the 'aud' field
        for key in ("clientId", "client_id", "ally_client_id"):
            if key in claims:
                return int(claims[key])
        # Sometimes encoded in the custom LTI claim
        custom = claims.get(
            "https://purl.imsglobal.org/spec/lti/claim/custom", {})
        if "client_id" in custom:
            return int(custom["client_id"])
    except Exception:
        pass
    return None


def _extract_client_id_from_cookie(cookie_str: str) -> int | None:
    """Extract Ally client ID from the session cookie name (e.g. 'session-5=...')."""
    m = re.search(r'\bsession-(\d+)=', cookie_str)
    return int(m.group(1)) if m else None


def _discover_client_id(token: str, cookie: str) -> int:
    """
    Call the Ally root endpoint /api/v1/5 to discover the client ID by trying
    the known UW value first, then probing 1-20.
    """
    for cid in [5, 1, 2, 3, 4] + list(range(6, 21)):
        url = f"{ALLY_BASE}/api/v1/{cid}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        try:
            req = Request(url, headers=headers)
            data = json.loads(urlopen(req, timeout=10).read())
            if isinstance(data, dict) and "lmsType" in data:
                return cid
        except Exception:
            continue
    raise RuntimeError(
        "Could not discover Ally client ID automatically.\n"
        "Set it manually with --client-id or add it to ally-token.txt."
    )


class _RedirectStopped(Exception):
    def __init__(self, location): self.location = location


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Raise instead of following redirects so we can inspect each step."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _RedirectStopped(newurl)


# ── Manual token file parsing (fallback) ─────────────────────────────────────

def parse_token_file(path: Path) -> tuple[str, int, str]:
    """
    Parse ally-token.txt — accepts either:
      - A cURL command pasted from "Copy as cURL" in Chrome DevTools
      - Three plain lines: token / client_id / cookie
    Returns (token, client_id, cookie).
    """
    text = path.read_text()

    if "curl " in text:
        token     = _extract_curl_bearer(text)
        client_id = _extract_curl_client_id(text)
        cookie    = _extract_curl_cookie(text)
        missing   = []
        if not token:     missing.append("Bearer token")
        if not client_id: missing.append("client ID")
        if missing:
            sys.exit(
                "Could not parse ally-token.txt.\n"
                "Missing: " + ", ".join(missing) + "\n"
                "Copy the cURL for the prod.ally.ac /api/v1/N?courseId=... request."
            )
        return token, client_id, cookie or ""

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        sys.exit(
            "ally-token.txt needs at least two lines: token / client_id\n"
            "Or use --canvas-token to use the Canvas token for auto-login."
        )
    return lines[0], int(lines[1]), lines[2] if len(lines) >= 3 else ""


def _extract_curl_bearer(text):
    m = re.search(r"-H\s+['\"](?:Authorization|authorization):\s*Bearer\s+([^\s'\"]+)",
                  text, re.IGNORECASE)
    return m.group(1) if m else ""


def _extract_curl_client_id(text):
    m = re.search(r'/api/v\d+/(?:clients/)?(\d+)', text)
    return int(m.group(1)) if m else None


def _extract_curl_cookie(text):
    for pat in [r"\s-b\s+'([^']+)'", r'\s-b\s+"([^"]+)"',
                r"-H\s+['\"](?:cookie|Cookie):\s*([^'\"]+)['\"]"]:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return ""


def _check_token_expiry(token: str) -> None:
    """Warn if the JWT exp claim is in the past."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return
        payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        exp = decoded.get("exp")
        if not exp:
            return
        import datetime
        exp_dt = datetime.datetime.fromtimestamp(exp)
        now    = datetime.datetime.now()
        if now > exp_dt:
            delta = int((now - exp_dt).total_seconds() / 60)
            sys.exit(
                f"Token expired {delta} minute(s) ago.\n"
                "Run without --token to use auto-login instead."
            )
        remaining = int((exp_dt - now).total_seconds() / 60)
        print(f"  Token valid ~{remaining} more minute(s) (expires {exp_dt:%H:%M:%S})")
    except Exception:
        pass


# ── Ally HTTP helpers ─────────────────────────────────────────────────────────

def ally_get(token: str, url: str, params: dict = None,
             cookie: str = "", debug: bool = False) -> dict | list:
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
            if debug:
                print(f"\n── DEBUG {url}\n{raw.decode()[:500]}")
            return json.loads(raw)
    except HTTPError as e:
        body = e.read().decode()
        if debug:
            print(f"\n── DEBUG HTTP {e.code} {url}\n{body[:1000]}")
        if e.code == 401:
            sys.exit("HTTP 401: token expired. Re-run to auto-login again.")
        if e.code == 403:
            sys.exit(f"HTTP 403 Forbidden from {url}\n{body[:300]}")
        raise RuntimeError(f"HTTP {e.code} from {url}: {body[:300]}")


# ── Ally API calls ────────────────────────────────────────────────────────────

def get_course_report(token, client_id, course_id, cookie="", debug=False):
    return ally_get(token,
        f"{ALLY_BASE}/api/v1/{client_id}/reports/courses/{course_id}",
        cookie=cookie, debug=debug)


def get_course_content(token, client_id, course_id, cookie="", debug=False):
    return ally_get(token,
        f"{ALLY_BASE}/api/v1/{client_id}/reports/courses/{course_id}/content",
        cookie=cookie, debug=debug)


def get_file_report(token, client_id, course_id, file_id, cookie="", debug=False):
    return ally_get(token,
        f"{ALLY_BASE}/api/v2/clients/{client_id}/courses/{course_id}/files/{file_id}/report",
        cookie=cookie, debug=debug)


# ── Report builder ────────────────────────────────────────────────────────────

def build_report(token: str, client_id: int, course_id: int,
                  cookie: str = "", include_file_feedback: bool = False,
                  debug: bool = False) -> dict:

    print(f"\nFetching Ally report — course {course_id}")
    print("  → Course report…")
    summary = get_course_report(token, client_id, course_id, cookie=cookie, debug=debug)

    print("  → Per-file scores…")
    content = get_course_content(token, client_id, course_id, cookie=cookie, debug=debug)
    files   = content.get("content") or []
    print(f"     {len(files)} item(s)")

    if include_file_feedback:
        print("  → Per-file detail…")
        for i, f in enumerate(files, 1):
            fid = f.get("id") or f.get("hash") or f.get("externalId")
            if fid:
                try:
                    f["_report"] = get_file_report(
                        token, client_id, course_id, fid, cookie=cookie, debug=debug)
                    time.sleep(0.3)
                except Exception:
                    pass
            if i % 10 == 0:
                print(f"     {i}/{len(files)}…")

    return {
        "source":    "ally_api",
        "client_id": client_id,
        "course_id": course_id,
        "summary":   summary,
        "content":   content.get("course", {}),
        "files":     files,
    }


# ── Summary printer ───────────────────────────────────────────────────────────

def print_report_summary(report: dict) -> None:
    summary = report.get("summary", {})
    score   = summary.get("score", {})
    issues  = summary.get("issues", {})

    print("\n── Ally Course Report ──")
    print(f"  Course  : {summary.get('name', '')}")
    print(f"  Issues  : {summary.get('total', '?')}")
    print(f"  Score   : {score.get('total', 0)*100:.1f}%  "
          f"(files: {score.get('files', 0)*100:.1f}%  "
          f"rich content: {score.get('richContent', 0)*100:.1f}%)")
    print(f"  Updated : {summary.get('lastReportTime', 'unknown')}")

    if issues:
        print(f"\n── Issues by type ──")
        print(f"{'Count':>6}  {'Issue':<30}  Scanned")
        print(f"{'─'*6}  {'─'*30}  {'─'*10}")
        for name, val in sorted(issues.items(),
                                 key=lambda x: x[1].get("count", 0), reverse=True):
            print(f"{val.get('count',0):>6}  {name:<30}  ({val.get('appliesTo','')} items)")
        total = sum(v.get("count", 0) for v in issues.values())
        print(f"{'─'*6}  {'─'*30}")
        print(f"{total:>6}  TOTAL  ({len(issues)} types)")

    files = report.get("files", [])
    if files:
        worst = sorted(files, key=lambda f: f.get("score", 1))[:20]
        print(f"\n── Worst files (lowest score) ──")
        print(f"  {'Score':>6}  Name")
        print(f"  {'─'*6}  {'─'*55}")
        for f in worst:
            sc = f.get("score", 1)
            nm = (f.get("name") or f.get("fileName") or f.get("id") or "?")[:60]
            print(f"  {sc*100:>5.1f}%  {nm}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fetch real Ally accessibility report (auto-login via Canvas token)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--course-id",    type=int, required=True)
    parser.add_argument("--canvas-token", default=None,
                        help="Canvas API token for auto-login "
                             "(default: read from canvas-token.txt)")
    parser.add_argument("--client-id",    type=int, default=None,
                        help="Override Ally client ID (auto-detected if omitted)")
    parser.add_argument("--output",       default=None,
                        help="Save JSON to file (default: ally_real_report_COURSEID.json)")
    parser.add_argument("--feedback",     action="store_true",
                        help="Fetch per-file detail (slower)")
    parser.add_argument("--summary-only", action="store_true",
                        help="Print summary and exit, do not save file")
    parser.add_argument("--debug",        action="store_true",
                        help="Show raw HTTP responses")
    parser.add_argument("--no-auto",      action="store_true",
                        help="Skip auto-login; use ally-token.txt manually")
    args = parser.parse_args()

    # ── Obtain Ally credentials ───────────────────────────────────────────────

    token = cookie = ""
    client_id = args.client_id

    if args.no_auto:
        # Manual mode: read from ally-token.txt
        if not TOKEN_FILE.exists():
            sys.exit("ally-token.txt not found. Remove --no-auto to use auto-login.")
        token, file_client_id, cookie = parse_token_file(TOKEN_FILE)
        if not client_id:
            client_id = file_client_id
        _check_token_expiry(token)
    else:
        # Auto-login mode: use Canvas token
        canvas_token = args.canvas_token
        if not canvas_token:
            if CANVAS_TOKEN_FILE.exists():
                canvas_token = CANVAS_TOKEN_FILE.read_text().strip()
            else:
                sys.exit(
                    "No Canvas token found.\n"
                    "Either:\n"
                    "  - Create canvas-token.txt with your Canvas API token, or\n"
                    "  - Pass --canvas-token YOUR_TOKEN, or\n"
                    "  - Use --no-auto and paste a cURL into ally-token.txt"
                )
        try:
            token, auto_client_id, cookie = auto_login(
                canvas_token, args.course_id, debug=args.debug)
            if not client_id:
                client_id = auto_client_id
        except Exception as e:
            print(f"Auto-login failed: {e}")
            print("Falling back to ally-token.txt if present…")
            if TOKEN_FILE.exists():
                token, file_client_id, cookie = parse_token_file(TOKEN_FILE)
                if not client_id:
                    client_id = file_client_id
                _check_token_expiry(token)
            else:
                sys.exit(
                    "Auto-login failed and no ally-token.txt found.\n"
                    "Run with --debug for details, or paste a cURL into ally-token.txt."
                )

    if not client_id:
        sys.exit("Could not determine Ally client ID. Pass --client-id N.")

    # ── Fetch and display report ──────────────────────────────────────────────
    report = build_report(token, client_id, args.course_id,
                           cookie=cookie,
                           include_file_feedback=args.feedback,
                           debug=args.debug)

    print_report_summary(report)

    if args.summary_only:
        return

    out = args.output or f"ally_real_report_{args.course_id}.json"
    Path(out).write_text(json.dumps(report, indent=2))
    print(f"\nSaved to {out}")


if __name__ == "__main__":
    main()
