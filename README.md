# Canvas Accessibility Fixer

Automatically finds and fixes WCAG accessibility issues in Canvas LMS courses at the University of Washington. Works on Canvas pages (HTML), Word documents, PowerPoint files, PDFs, and images.

**Live app:** https://canvas-accessibility.onrender.com  
**Source:** https://github.com/pisanuw/canvas-accessibility

---

## What it fixes

| Content type | Issues fixed |
|---|---|
| **Canvas Pages** | Empty headings, heading order/presence, headings start at H1, table headers + captions, fake lists, color contrast, image alt text (AI or placeholder), link text (AI) |
| **Syllabus** | Same HTML fixes as Canvas Pages |
| **Assignment descriptions** | Same HTML fixes as Canvas Pages (except html_meta and headings_presence) |
| **Word (.docx)** | Heading presence + order, headings start at H1 (placeholder), table headers, document language, image alt text (AI or placeholder) |
| **PowerPoint (.pptx)** | Reading order, slide titles, presentation language, image alt text, link descriptions |
| **PDF metadata** | Document title, language, XMP tags |
| **PDF content** _(opt-in, slow)_ | Accessibility tag structure, heading levels, table headers, image alt placeholders, link descriptions |
| **OCR** _(opt-in, very slow)_ | OCR for scanned PDFs — makes text selectable; runs in subprocess, capped at 10 files |
| **Images (via Ally)** | Mark decorative images, replace seizure-risk images with a warning placeholder |
| **Broken links** _(opt-in, slow)_ | Check every link on every Canvas page; replace broken ones |

Items marked **AI** require an Anthropic API key (optional — all other fixes run without one).

---

## Option A — Web App (recommended for most courses)

The web app runs as a 6-step wizard. Go to https://canvas-accessibility.onrender.com, log in, and follow the steps.

### Setup

You need a **Canvas personal access token**:

1. Log in to Canvas at https://canvas.uw.edu
2. Click your name/avatar → **Settings**
3. Scroll to **Approved Integrations** → click **+ New Access Token**
4. Purpose: `canvas-accessibility`, leave expiry blank
5. Click **Generate Token** — copy it immediately (shown only once)

### Course search

On Step 1, you can search for your course by name instead of pasting a URL. Enter your Canvas token in the search box and start typing — matching courses appear in a dropdown.

### Credentials (Step 3)

In addition to the Canvas token and optional Anthropic API key, you can enter an **instructor email address**. After the run completes, a notification with the issue counts and fix summary is sent to both the admin email and the instructor email.

### Fix selector (Step 4)

A hierarchical checklist lets you choose exactly which fixes to run:

- All standard fixes are **checked by default** — Canvas pages, syllabus, assignment descriptions, Word docs, PDF metadata, PowerPoint, and Ally image fixes.
- **Slow fixes are unchecked by default** (amber): PDF content structure, OCR for scanned PDFs, and broken link checking. Enable them individually if needed.
- AI-dependent fixes (image alt text, link labels) are **greyed out** if no Anthropic key was provided; a placeholder alt text option is shown instead.
- Use **Select All** / **Clear All** to reset, or check/uncheck individual items.
- Check **Preview only** to calculate fixes without uploading anything to Canvas. The report shows what would have changed.

### Running (Step 5)

The progress page shows:
- A **progress bar** that advances file-by-file within each content type section
- An **elapsed timer** so you know how long the run has been going
- A **Copy** button to copy the full terminal log to your clipboard

### Report (Step 6)

- **Before / After / Ally score** cards at the top
- **Collapsible sections** for each content type — sections with changes start expanded, clean sections start collapsed
- **Filter buttons**: All · Changed · Errors Only
- Per-file detail table inside each section showing what changed (or the error message)
- A link to the **Ally dashboard** to check the updated score after Ally rescans (~1–2 hours)
- **LibraryReference** items are excluded from counts and explained in an "About the Ally Dashboard" notice

---

## Option B — CLI Tools (for large courses, batch runs, or local control)

Clone the repo and run the scripts directly. Requires Python 3.10+.

```bash
git clone https://github.com/pisanuw/canvas-accessibility.git
cd canvas-accessibility
pip install -r webapp/requirements.txt
```

Save your Canvas token:
```bash
echo "your-canvas-token-here" > canvas-token.txt
```

### Back up courses before fixing

```bash
# Step 1: start export jobs on Canvas (runs in background)
python3 canvas-backup.py start --all

# Check progress
python3 canvas-backup.py status

# Step 2: download completed exports
python3 canvas-backup.py download
```

Files are saved to `backups/` as `.imscc` archives.

### Run all fixes on a course

```bash
# Dry run — show what would change, nothing uploaded
python3 fix_all.py --course-id 1492292 --dry-run

# Run all automatic fixes (no AI key needed)
python3 fix_all.py --course-id 1492292 --no-ai

# Run with AI-assisted image alt text and link fixes
export ANTHROPIC_API_KEY=your_key_here
python3 fix_all.py --course-id 1492292

# Run only HTML and PDF fixes
python3 fix_all.py --course-id 1492292 --types html,pdf

# Save a detailed JSON report
python3 fix_all.py --course-id 1492292 --output report.json
```

### Run individual fix modules

Each module in `fixes/` can be run standalone:

```bash
python3 fixes/fix_html_pages.py      --course-id 1492292 --dry-run
python3 fixes/fix_word_docs.py       --course-id 1492292 --dry-run
python3 fixes/fix_pdf_metadata.py    --course-id 1492292 --dry-run
python3 fixes/fix_pdf_content.py     --course-id 1492292 --dry-run
python3 fixes/fix_pptx_files.py      --course-id 1492292 --dry-run
```

---

## Run the web app locally

```bash
# Copy the example env file and fill in your values
cp .env.example .env
# Edit .env — at minimum set FLASK_SECRET_KEY, APP_USERNAME, APP_PASSWORD

# Start Flask
./run-locally.sh
# Open http://localhost:5000
```

---

## Deploy to Render

1. Fork this repo and connect it to Render as a **Docker** web service
2. Set these environment variables in the Render dashboard:

| Variable | Value |
|---|---|
| `FLASK_SECRET_KEY` | Generate: `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `APP_USERNAME` | Login username for the main tool |
| `APP_PASSWORD` | Login password |
| `ADMIN_USERNAME` | Admin dashboard username |
| `ADMIN_PASSWORD` | Admin dashboard password |
| `RESEND_API_KEY` | From resend.com (optional — enables email notifications) |
| `ADMIN_EMAIL` | Where run-completion emails are sent |
| `FROM_EMAIL` | Sender address (use `noreply@resend.dev` for free tier) |

3. `git push` — Render auto-deploys from `main`
4. Check `https://your-app.onrender.com/health` — should return `OK`

---

## Repository layout

```
canvas-accessibility/
├── webapp/                     Flask web app
│   ├── app.py                  Routes, SSE streaming, session auth, admin
│   ├── requirements.txt        Python dependencies
│   ├── templates/              Jinja2 HTML templates (6-step wizard)
│   └── static/style.css        Single stylesheet (UW purple/gold design)
├── fixes/                      Reusable fix modules (used by both webapp and CLI)
│   ├── canvas_client.py        Canvas REST API client
│   ├── ai_client.py            Anthropic API helpers (image alt, link text)
│   ├── fix_html_pages.py       Canvas wiki page fixes
│   ├── fix_word_docs.py        Word document fixes
│   ├── fix_pdf_metadata.py     PDF metadata fixes
│   ├── fix_pdf_content.py      PDF content fixes (OCR, tags, headings)
│   ├── fix_pptx_files.py       PowerPoint fixes
│   ├── fix_image_files.py      Image fixes via Ally API
│   └── _ocr_worker.py          Subprocess worker for memory-isolated OCR
├── ally_api.py                 Ally LTI 1.3 OIDC auto-login + course reports
├── fix_all.py                  CLI orchestrator — runs all fix modules
├── canvas-backup.py            CLI backup tool — start/download Canvas exports
├── run-locally.sh              Start Flask locally with .env loaded
├── Dockerfile                  Production image (Python 3.11, tesseract, poppler)
├── render.yaml                 Render deployment config
├── .env.example                Environment variable template
├── Codebase.md                 Technical reference for developers
├── FuturePlan.md               Planned improvements
└── backups/                    Downloaded .imscc course archives (gitignored)
```

---

## Known limitations

- **Memory (512 MB):** Render's free tier can run out of memory on courses with many large scanned PDFs. Use Quick mode or the CLI for those courses.
- **PDF color contrast:** Cannot be auto-fixed without AI analysis of color values in the PDF. Listed in the report as "requires manual review."
- **LibraryReference:** Ally's own library-integration flag (3,358 occurrences). Not a real accessibility defect — no fix needed.
- **Admin log:** `webapp/admin_log.json` is lost on Render redeploys. Resend email provides a persistent per-run copy.
- **UW only:** Currently hardcoded to `canvas.uw.edu` and UW's Ally LTI instance.

---

## Questions

Contact: [yusuf.pisan@gmail.com](mailto:yusuf.pisan@gmail.com)
