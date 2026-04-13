# Canvas Accessibility Codebase

Technical reference for developers. See `README.md` for user-facing instructions.

---

## Configuration

| Item | Value |
|---|---|
| Canvas base URL | `https://canvas.uw.edu` |
| API token file | `canvas-token.txt` (one line, trimmed; gitignored) |
| Ally base URL | `https://prod.ally.ac` |
| Ally LTI tool ID | `148172` (UW instance) |
| Ally client ID | `5` (UW; also auto-detected from session cookie) |
| Default test course | `1492292` — CSS 132 A Au 21 |

Token format: `10~<base64-like string>`

---

## Repository Layout

```
canvas-accessibility/
├── webapp/
│   ├── app.py                  Flask web app (SSE streaming, session auth, admin)
│   ├── admin_log.json          Run log (gitignored; lost on Render redeploy)
│   ├── requirements.txt        Flask, pikepdf, anthropic, resend, etc.
│   └── templates/
│       ├── base.html           Shared layout (header, footer, CSS link)
│       ├── login.html          User login
│       ├── course.html         Step 1 — enter Canvas course URL + fix list
│       ├── backup.html         Step 2 — confirm backup
│       ├── credentials.html    Step 3 — Canvas token + optional Anthropic key
│       ├── confirm.html        Step 4 — Ally scan + per-fix checkbox selector
│       ├── running.html        Step 5 — SSE progress stream
│       ├── report.html         Step 6 — summary table + download/rerun buttons
│       ├── links.html          Post-run: broken link check (separate SSE stream)
│       ├── busy.html           "Another run is in progress" page
│       ├── admin_login.html    Admin login
│       └── admin.html          Admin run log table with download links
├── fixes/
│   ├── canvas_client.py        Canvas REST API client (GET/POST/PUT/paginate/upload)
│   ├── ai_client.py            Anthropic API helpers (describe_image, link labels)
│   ├── fix_html_pages.py       HTML fixes for pages, syllabus, assignments (14 fix types)
│   ├── fix_word_docs.py        Word (.docx) accessibility fixes (7 fix types)
│   ├── fix_pdf_metadata.py     PDF metadata fixes (title, language, XMP)
│   ├── fix_pdf_content.py      PDF content fixes (tagging, OCR, headings, alt, links)
│   ├── fix_pptx_files.py       PowerPoint accessibility fixes
│   ├── fix_image_files.py      Image fixes via Ally API (decorative + seizure)
│   └── _ocr_worker.py          Subprocess worker for memory-isolated OCR
├── ally_api.py                 Ally LTI 1.3 OIDC auto-login + REST helpers
├── ally_decorative_cache.json  Local cache of file IDs already marked decorative (gitignored)
├── fix_all.py                  CLI orchestrator — all fix modules in priority order
├── canvas-backup.py            CLI backup tool — start and download Canvas exports
├── run-locally.sh              Load .env and start Flask (handles ! in passwords)
├── .env.example                Template for local environment variables
├── Dockerfile                  Production image (Python 3.11, tesseract, poppler)
├── render.yaml                 Render deployment config
├── FuturePlan.md               Planned improvements
└── archive/                    Old files kept for reference (not active code)
```

---

## Web App (`webapp/app.py`)

### Six-step wizard

| Step | Route | Description |
|---|---|---|
| 1 | `/course` | Enter Canvas URL or search by name; extract course ID; shows fix list and backup download |
| 2 | `/backup` | Checkbox confirm backup exists |
| 3 | `/credentials` | Canvas token + optional Anthropic key + optional instructor email; validates token live |
| 4 | `/confirm` (GET) | Ally issue count scan + per-fix checkbox selector + dry-run toggle |
| 4 | `/confirm` (POST) | Saves `selected_fixes`, `dry_run` flag to session; redirects to `/running` |
| 5 | `/running` | SSE stream of fix progress via `/stream`; shows progress bar, elapsed timer, copy button |
| 6 | `/report` | Collapsible per-type cards; Before/After/Ally score; filter/expand controls; Ally breadcrumb |

Additional routes:
- `/api/course-search` — Canvas course search by name for Step 1 dropdown (requires `?q=` and `?token=`)
- `/rerun` — clears job state, keeps course/credentials, returns to confirm (fix selector)
- `/links` + `/links-stream` — post-run broken link check with its own SSE stream
- `/report/download/<job_id>` — downloadable HTML report
- `/download/canvas-backup` — serves `canvas-backup.py` as a file download
- `/busy` — shown when another run is in progress
- `/restart` — clears full session, returns to step 1
- `/admin/login`, `/admin`, `/admin/logout` — admin dashboard
- `/health` — returns `"OK"` (Render health check)

### Auth and config

```python
USERNAME       = os.environ.get("APP_USERNAME",   "")
PASSWORD       = os.environ.get("APP_PASSWORD",   "")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
```

Login is blocked if any credential env var is empty (no default credentials).
`FLASK_SECRET_KEY` must be set in Render env (or `.env` locally).

### SSE event types (`/stream`)

The `_Capture` class intercepts stdout from `_run()` and emits typed messages onto a queue. The SSE generator forwards them as named events:

| Event | Payload | Consumer |
|---|---|---|
| `log` | `text` — raw terminal line | Appended to `.terminal` div |
| `section` | `{"label": str}` | Resets progress bar to 0%; updates section label |
| `progress` | `{"current": int, "total": int}` | Fills progress bar; updates fraction display |
| `before` | `{"count": int}` | Stores issue count before fixes |
| `after` | `{"count": int, "async": bool}` | Stores issue count after fixes |
| `done` | result JSON | Stores report; hides spinner; stops timer |
| `error` | `{"message": str}` | Shows error notice; stops timer |
| `heartbeat` | _(none)_ | Keeps connection alive (sent every 15 s) |

`_Capture.write()` detects `(i/n)` patterns in log lines and emits `progress` events; detects `──` or `--` separator lines and emits `section` events.

### Fix pipeline in `_run()` (background thread)

`selected_fixes` (a list of checkbox values) is read from session and used to build per-module fix lists via the `_HTML_FIX_MAP`, `_WORD_FIX_MAP`, `_PDF_CONTENT_FIX_MAP`, `_PPTX_FIX_MAP` dicts in `app.py`. `dry_run` is read from session and passed to every fix orchestrator call.

1. Ally auto-login — reuses `ally_token/ally_client_id/ally_cookie` cached from confirm-scan
2. Before-scan via `ally_api.get_course_report()` — LibraryReference issues subtracted from total to match Canvas Ally UI
3. HTML page fixes via `fix_course_pages()` — published + unpublished pages
4. Syllabus fixes via `fix_course_syllabus()`
5. Assignment description fixes via `fix_course_assignments()`
6. Word doc fixes via `fix_course_word_files()`
7. Image decorative fixes via `fix_course_image_files()` — pre-filtered by Ally content report; cached in `ally_decorative_cache.json` to avoid re-marking
8. Image seizure replacements via `fix_course_seizure_images()`
9. PDF metadata fixes
10. PDF content fixes (tags, headings, links, image alt) — only if selected
11. OCR for scanned PDFs — only if selected; runs in subprocess; capped at 10 files
12. PowerPoint fixes
13. After-scan note — Ally re-scores asynchronously

After run: generates `report_html`, appends to `admin_log.json`, sends Resend email to admin and (if provided) to `instructor_email`.

### `_summarize()` — report row structure

Each row in `result.summary` has:

```python
{
  "key":     str,           # e.g. "html", "word"
  "label":   str,           # human-readable label
  "items":   int,           # total items scanned
  "changes": int,           # total individual change strings
  "updated": int,           # items where updated=True
  "errors":  int,           # items where error is set
  "detail":  [              # per-item breakdown for collapsible table
    {
      "name":    str,       # page title or filename
      "changes": list[str], # list of change description strings
      "updated": bool,
      "error":   str,       # empty string if no error
    },
    ...
  ],
}
```

`name` is resolved via `r.get("page") or r.get("file") or "—"` to handle both HTML (uses `"page"` key) and file-based modules (use `"file"` key).

### Fix selector (Step 4 — confirm page)

Replaces the old Quick/Full mode toggle. User sees a hierarchical checklist:

**Standard (checked by default):**
- Canvas HTML Pages — 10 individual fixes including `headings_start_at_one` (inserts placeholder H1 at top of page/syllabus/assignment if missing or not first)
- Word Documents — 6 fixes including `headings_start_at_one` (inserts placeholder H1 at top if first paragraph is not Heading 1)
- PDF Metadata
- PowerPoint
- Images via Ally

**Slow (unchecked by default, amber border):**
- PDF Content Fixes (tags, heading structure, image alt, links)
- OCR for Scanned PDFs (very slow; memory intensive)
- Broken Link Check (one HTTP request per link)

AI-dependent items (image alt, link text) are greyed out when no Anthropic key is provided; the placeholder image alt checkbox is shown instead.

---

## Fix Modules

### `fixes/fix_html_pages.py`

| Function | Fix key | What it does |
|---|---|---|
| `fix_headings_start_at_one` | `headings_start_at_one` | Insert placeholder `<h1>` at very top if page doesn't open with H1 |
| `fix_empty_headings` | `empty_heading` | Remove `<h1>`–`<h6>` with no text |
| `fix_heading_order` | `heading_order` | Close level-skips (h1→h3 → h1→h2) |
| `fix_table_headers` | `table_headers` | First-row `<td>` → `<th scope="col">`, add `<thead>` |
| `fix_manual_lists` | `lists` | Consecutive `<p>` with bullet/number prefix → `<ul>`/`<ol>` |
| `fix_image_alt` | `image_alt` | AI-generated alt text for `<img>` missing/filename alt (Anthropic key required) |
| `fix_image_alt_placeholder` | `image_alt_placeholder` | Reviewer placeholder for `<img>` missing/filename alt (no AI needed) |
| `fix_links` | `links` | Replace non-descriptive link text via AI + bare-URL heuristic |
| `fix_color_contrast` | `color_contrast` | Snap inline `color:`→`#000000`; `background-color:`→nearest extreme |
| `fix_table_captions` | `table_captions` | Insert placeholder `<caption>` on tables missing one |
| `fix_headings_presence` | `headings_presence` | Insert `<h2>` placeholder if page has no headings at all |
| `fix_html_meta` | `html_meta` | Add `lang="en"` to `<html>`, `<title>` to `<head>` |
| `fix_broken_links` | `broken_links` | HEAD-request hrefs, replace broken with `href="#"` |

Orchestrators:
- `fix_page()` / `fix_course_pages()` — wiki pages (published + unpublished)
- `fix_course_syllabus()` — course syllabus body
- `fix_course_assignments()` — assignment description HTML (`html_meta` and `headings_presence` skipped for fragments)

**Key idempotency behaviours:**
- `fix_image_alt_placeholder`: skips images whose alt already equals the placeholder string
- `fix_headings_start_at_one`: removes a misplaced prior-run placeholder before re-inserting at the correct position (very top of body)
- `fix_color_contrast`: skips `background-color` already at `#000000` or `#ffffff`

### `fixes/fix_word_docs.py`

| Fix key | What it does |
|---|---|
| `headings_presence` | Restyle first non-empty paragraph as Heading 1 if no heading styles exist; creates style if missing |
| `headings_start_at_one` | Insert placeholder Heading 1 at top if first paragraph is not Heading 1 (handles both: no H1 anywhere, and H1 exists but is not first) |
| `heading_order` | Renormalize skipped levels |
| `table_headers` | Mark first row as header via `tblHeader` |
| `no_language` | Set language on all runs + Normal style (idempotent — checks existing value) |
| `image_alt` | AI-generated alt text (Anthropic key required) |
| `image_alt_placeholder` | Reviewer placeholder for missing/filename alt |

`_is_filename_alt(text)` treats alt text ending in a file extension as missing.

### `fixes/fix_pdf_metadata.py`

Sets `/Title`, `/Lang`, and XMP metadata (`dc:language`, `pdf:Language`).

### `fixes/fix_pdf_content.py`

| Function | Triggered by | Notes |
|---|---|---|
| `fix_scanned` | `"scanned"` | Spawns `_ocr_worker.py` subprocess; 300s timeout |
| `fix_tags_and_headings` | `"tags"` | Adds StructTreeRoot + H1 placeholder |
| `fix_headings_start_at_one` | `"headings_start_at_one"` | Shifts levels so min = H1 |
| `fix_headings_sequential` | `"headings_sequential"` | Closes level gaps |
| `fix_table_headers` | `"table_headers"` | Promotes first-row TD→TH in struct tree |
| `fix_image_alt` | `"image_alt"` | Placeholder /Alt on /Figure with no/filename alt |
| `fix_links` | `"links"` | Sets /Contents on bare URI link annotations |

`OCR_CAP = 10` — after 10 files queued for OCR, remaining files get all other fixes but not OCR.

### `fixes/_ocr_worker.py`

Standalone subprocess. Receives PDF bytes on stdin, writes OCR'd PDF bytes to stdout. Processes one page at a time at 200 DPI. All OCR memory is freed when the process exits.

### `fixes/fix_pptx_files.py`

Fix keys: `reading_order`, `no_language`, `slide_title`, `image_alt`, `links`

### `fixes/fix_image_files.py`

- `fix_course_image_files` — pre-filters via Ally content report (`ImageDecorative` or `ImageDescription` < 1.0), then checks `ally_decorative_cache.json` before each POST to avoid re-marking images that were already marked in a prior run (Ally content report scores are cached and lag by hours after changes)
- `fix_course_seizure_images` — queries Ally content report for `ImageSeizure` < 1.0, replaces each with a static yellow warning PNG

**Note:** `HtmlImageAlt` (filename used as image alt in HTML pages) is a separate Ally issue from `ImageDecorative`/`ImageDescription` (standalone image files). The former is fixed by `fix_image_alt_placeholder` in `fix_html_pages.py`; the latter by `fix_image_files.py`.

---

## Ally API (`ally_api.py`)

LTI 1.3 OIDC two-phase auto-login:
1. Canvas sessionless launch → OIDC form (login_hint, etc.)
2. POST phase-1 to `https://prod.ally.ac/api/v2/auth/lti/1.3/login`
3. Follow redirect to Canvas `/api/lti/authorize_redirect`
4. Submit id_token form to Ally callback
5. Bearer token extracted from `?token=<jwt>` in post-callback redirect URL
6. Client ID extracted from session cookie name `session-{N}=`

Key exports:
- `auto_login(canvas_token, course_id) → (ally_token, client_id, ally_cookie)`
- `get_course_report(token, client_id, course_id, cookie) → dict`
- `get_course_content(token, client_id, course_id, cookie) → dict`

**Issue count display:** `LibraryReference` issues are subtracted from `report["total"]` before display. These are Ally's own integration promotional flag — not real accessibility defects — and are excluded from the Canvas Ally UI.

---

## Canvas Client (`fixes/canvas_client.py`)

| Method | Description |
|---|---|
| `get(path, params)` | GET with auth header; retries once on 403/429 |
| `post(path, data)` | POST form-encoded |
| `put(path, data)` | PUT form-encoded |
| `get_all_pages(path, params)` | Paginated GET (100 per page) |
| `download_url(url)` | Download any URL, adding auth if Canvas domain |
| `upload_file(course_id, folder_id, filename, content_type, data)` | Two-step Instructure FS upload |
| `list_files(course_id, content_types)` | List course files |
| `get_file_info(file_id)` | Single file metadata |
| `list_pages(course_id)` | List wiki pages — fetches published + unpublished separately and merges |
| `get_page(course_id, page_url)` | Fetch one page body |
| `update_page(course_id, page_url, body_html, title)` | Update page |
| `get_syllabus(course_id)` | Fetch syllabus HTML body |
| `update_syllabus(course_id, body_html)` | Update syllabus body |
| `list_assignments(course_id)` | List all assignments with descriptions included |
| `get_assignment(course_id, assignment_id)` | Fetch one assignment with description |
| `update_assignment(course_id, assignment_id, description_html)` | Update assignment description |

`BASE_URL` and `TOKEN_FILE` are module-level constants. Token is loaded from `canvas-token.txt` unless passed directly to `CanvasClient(token=...)`.

---

## CLI Tools

### `canvas-backup.py`

Unified backup tool (replaced `backup_start.py` + `backup_download.py`).

```
python3 canvas-backup.py start --all          # start exports for all courses
python3 canvas-backup.py start --all --dry-run
python3 canvas-backup.py status               # show export status table
python3 canvas-backup.py download             # download completed exports
python3 canvas-backup.py download --out-dir /path
```

### `fix_all.py`

CLI orchestrator. Runs HTML (pages, syllabus, assignment descriptions), Word, PPTX, and PDF metadata fixes in order. Does not run PDF content fixes (run directly via `fixes/fix_pdf_content.py`). Supports `--dry-run`, `--no-ai`, and `--types html,word,pptx,pdf` flags. Saves a JSON report after each run.

---

## Environment Variables

| Var | Purpose | Default |
|---|---|---|
| `FLASK_SECRET_KEY` | Session signing | Random (insecure if not set) |
| `APP_USERNAME` | Main user login | _(required — no default)_ |
| `APP_PASSWORD` | Main user login | _(required — no default)_ |
| `ADMIN_USERNAME` | Admin dashboard | _(required — no default)_ |
| `ADMIN_PASSWORD` | Admin dashboard | _(required — no default)_ |
| `RESEND_API_KEY` | Email notifications | (disabled if absent) |
| `ADMIN_EMAIL` | Notification recipient | _(required for email)_ |
| `FROM_EMAIL` | Resend sender | `noreply@resend.dev` |
| `RENDER` | Set automatically by Render | Enables `SESSION_COOKIE_SECURE` |

Load locally with `./run-locally.sh` (handles `!` and spaces in passwords).
