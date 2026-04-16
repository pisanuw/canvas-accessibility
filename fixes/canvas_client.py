"""
Canvas API client — shared helpers for all remediation scripts.

Handles authentication, pagination, file download, file upload (two-step
Instructure FS flow), and page read/write.
"""

import http.client
import json
import os
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

BASE_URL = os.environ.get("CANVAS_BASE_URL", "https://canvas.uw.edu").rstrip("/")
TOKEN_FILE = Path(__file__).parent.parent / "canvas-token.txt"


def load_token() -> str:
    token = TOKEN_FILE.read_text().strip()
    if not token:
        raise RuntimeError("canvas-token.txt is empty")
    return token


class CanvasClient:
    def __init__(self, token: str = None, base_url: str = BASE_URL):
        self.token = token or load_token()
        self.base_url = base_url.rstrip("/")

    # ── Low-level HTTP ────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def get(self, path: str, params: dict = None) -> dict | list:
        url = f"{self.base_url}/api/v1{path}"
        if params:
            url += "?" + urlencode(params, doseq=True)
        req = Request(url, headers=self._headers())
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            if e.code in (403, 429):
                time.sleep(12)
                with urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read())
            raise RuntimeError(f"GET {url} → HTTP {e.code}: {e.read().decode()[:300]}")

    def post(self, path: str, data: dict = None) -> dict:
        url = f"{self.base_url}/api/v1{path}"
        body = urlencode(data or {}).encode()
        req = Request(url, data=body, headers={
            **self._headers(),
            "Content-Type": "application/x-www-form-urlencoded",
        })
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            if e.code in (403, 429):
                time.sleep(12)
                with urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read())
            raise RuntimeError(f"POST {url} → HTTP {e.code}: {e.read().decode()[:300]}")

    def put(self, path: str, data: dict = None) -> dict:
        url = f"{self.base_url}/api/v1{path}"
        body = urlencode(data or {}, quote_via=lambda s, safe, enc, err: s).encode("utf-8")
        # Use urllib.parse.quote for proper encoding of HTML bodies
        from urllib.parse import quote
        body = "&".join(
            f"{k}={quote(str(v), safe='')}" for k, v in (data or {}).items()
        ).encode("utf-8")
        req = Request(url, data=body, method="PUT", headers={
            **self._headers(),
            "Content-Type": "application/x-www-form-urlencoded",
        })
        try:
            with urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            raise RuntimeError(f"PUT {url} → HTTP {e.code}: {e.read().decode()[:300]}")

    def get_all_pages(self, path: str, params: dict = None) -> list:
        results = []
        page = 1
        base_params = {**(params or {}), "per_page": 100}
        while True:
            batch = self.get(path, {**base_params, "page": page})
            if not batch:
                break
            results.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return results

    # ── File Download ─────────────────────────────────────────────────────────

    def download_url(self, url: str) -> bytes:
        """Download any URL, adding auth header if it's a Canvas URL."""
        headers = self._headers() if self.base_url.split("//")[1] in url else {}
        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=60) as resp:
                return resp.read()
        except HTTPError as e:
            raise RuntimeError(f"Download {url} → HTTP {e.code}")

    def get_file_info(self, file_id: int) -> dict:
        return self.get(f"/files/{file_id}")

    # ── File Upload (two-step Instructure FS flow) ────────────────────────────

    def upload_file(self, course_id: int, folder_id: int,
                    filename: str, content_type: str, data: bytes) -> dict:
        """
        Upload a file to a Canvas course folder.
        Returns the Canvas file metadata dict (includes 'id', 'url', etc.).
        """
        # Step 1: Request an upload slot
        slot = self.post(f"/courses/{course_id}/files", {
            "name": filename,
            "size": len(data),
            "content_type": content_type,
            "parent_folder_id": folder_id,
            "on_duplicate": "overwrite",
        })
        upload_url = slot["upload_url"]
        upload_params = slot["upload_params"]

        # Step 2: Multipart POST to Instructure FS
        location = self._multipart_post(
            upload_url, upload_params, filename, content_type, data
        )

        # Step 3: The location header points to the confirmed Canvas file
        file_url = f"{self.base_url}/api/v1/files/{_file_id_from_location(location)}"
        req = Request(file_url, headers=self._headers())
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def _multipart_post(self, url: str, params: dict,
                        filename: str, content_type: str, data: bytes) -> str:
        """
        POST multipart/form-data to url (Instructure FS).
        Returns the Location header from the 201 response.
        """
        boundary = uuid.uuid4().hex
        body = b""
        for key, val in params.items():
            body += (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{val}\r\n"
            ).encode()
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
        body += data + f"\r\n--{boundary}--\r\n".encode()

        parsed = urlparse(url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        conn = http.client.HTTPSConnection(parsed.netloc, timeout=120)
        conn.request("POST", path, body=body, headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        })
        resp = conn.getresponse()
        location = resp.getheader("Location", "")
        resp.read()  # consume body
        conn.close()
        if resp.status not in (200, 201):
            raise RuntimeError(f"Instructure FS upload failed: HTTP {resp.status}")
        return location

    # ── Canvas Pages ──────────────────────────────────────────────────────────

    def list_pages(self, course_id: int) -> list[dict]:
        # Canvas API has no "all" filter — fetch published and unpublished separately
        # then merge, deduplicating by page url slug.
        published   = self.get_all_pages(f"/courses/{course_id}/pages")
        unpublished = self.get_all_pages(f"/courses/{course_id}/pages",
                                         {"published": "false"})
        seen = {p["url"] for p in published}
        combined = published + [p for p in unpublished if p["url"] not in seen]
        return combined

    def get_page(self, course_id: int, page_url: str) -> dict:
        return self.get(f"/courses/{course_id}/pages/{page_url}")

    def update_page(self, course_id: int, page_url: str,
                    body_html: str, title: str = None) -> dict:
        data = {"wiki_page[body]": body_html}
        if title:
            data["wiki_page[title]"] = title
        return self.put(f"/courses/{course_id}/pages/{page_url}", data)

    # ── Canvas Syllabus ───────────────────────────────────────────────────────

    def get_syllabus(self, course_id: int) -> str:
        """Return the syllabus HTML body, or empty string if none."""
        data = self.get(f"/courses/{course_id}", {"include[]": "syllabus_body"})
        return data.get("syllabus_body") or ""

    def update_syllabus(self, course_id: int, body_html: str) -> dict:
        return self.put(f"/courses/{course_id}",
                        {"course[syllabus_body]": body_html})

    # ── Canvas Assignments ────────────────────────────────────────────────────

    def list_assignments(self, course_id: int) -> list[dict]:
        return self.get_all_pages(f"/courses/{course_id}/assignments",
                                  {"include[]": "description"})

    def get_assignment(self, course_id: int, assignment_id: int) -> dict:
        return self.get(f"/courses/{course_id}/assignments/{assignment_id}",
                        {"include[]": "description"})

    def update_assignment(self, course_id: int, assignment_id: int,
                          description_html: str) -> dict:
        return self.put(f"/courses/{course_id}/assignments/{assignment_id}",
                        {"assignment[description]": description_html})

    # ── Canvas Files ──────────────────────────────────────────────────────────

    def list_files(self, course_id: int, content_types: list[str] = None) -> list[dict]:
        params = {}
        if content_types:
            params["content_types[]"] = content_types
        return self.get_all_pages(f"/courses/{course_id}/files", params)

    def get_folder(self, course_id: int, folder_name: str = "root") -> dict:
        return self.get(f"/courses/{course_id}/folders/{folder_name}")


def _file_id_from_location(location: str) -> int:
    """Extract file ID from Canvas file location URL."""
    # e.g. https://canvas.uw.edu/api/v1/files/148493689?include[]=enhanced_preview_url
    path = urlparse(location).path  # /api/v1/files/148493689
    return int(path.rstrip("/").split("/")[-1])
