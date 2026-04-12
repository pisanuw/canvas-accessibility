# Future Plan

Ideas and improvements that are worth doing but not yet implemented.
Ordered roughly by impact.

---

## 1. GitHub Actions as Heavy-Compute Backend

Render's free tier has a 512 MB memory limit. OCR and large PDF structural repair can exceed this.

**Idea:** Add a `workflow_dispatch` GitHub Actions workflow that accepts `course_id` and `canvas_token` as inputs. The workflow runner has 7 GB RAM and runs up to 6 hours at no cost. Users with large courses trigger it from the GitHub UI with their Canvas token stored as a repo secret.

```yaml
# .github/workflows/fix-course.yml
on:
  workflow_dispatch:
    inputs:
      course_id: { required: true }
jobs:
  fix:
    runs-on: ubuntu-latest   # 7 GB RAM
    steps:
      - uses: actions/checkout@v4
      - run: pip install -r webapp/requirements.txt
      - run: python3 fix_all.py --course-id ${{ inputs.course_id }}
        env:
          CANVAS_TOKEN: ${{ secrets.CANVAS_TOKEN }}
```

The web app could detect large courses (many PDFs) and offer a "Run via GitHub Actions" button that deep-links to the dispatch UI.

---

## 2. Larger Render Plan (if budget allows)

Upgrading to Render's Standard plan ($25/month) gives 2 GB RAM and eliminates the memory constraint entirely. The Quick/Full mode toggle can be removed once this is done.

---

## 3. PDF Color Contrast

PDF color contrast failures (`Contrast` issue type — 1,497 occurrences in 55 courses) cannot currently be auto-fixed without AI analysis of the color values against their background. Options:

- Extract text+background color pairs from the PDF color space, compute WCAG ratio, snap failing values
- Potentially fixable with pikepdf + pdfminer.six for color extraction

---

## 4. Multi-Course Batch Processing via Web App

Currently the web wizard handles one course at a time. An admin mode could queue multiple courses and process them sequentially, sending a Resend email report per course.

---

## 5. AI Image Alt Text for PDFs

PDF image alt text is currently set to a generic placeholder ("Your instructor will review..."). With an Anthropic key, the `fix_image_alt()` function in `fix_pdf_content.py` could download the figure image and call the AI to generate a real description — same approach used for HTML pages and Word docs.

---

## 6. Persistent Admin Log

`webapp/admin_log.json` is lost on every Render redeploy. Options:
- Write it to a GitHub Gist via the GitHub API after each run
- Use a free-tier external store (Supabase, PlanetScale, Railway Postgres)
- The Resend email already provides a persistent per-run copy to `ADMIN_EMAIL`

---

## 7. Support Other Canvas Institutions

Currently hardcoded to `canvas.uw.edu` and UW's Ally LTI tool ID (`148172`) and client ID (`5`). Making these configurable env vars would allow other institutions to use the tool.

---

## 8. Update `fix_all.py` CLI to Match Web App

The CLI orchestrator (`fix_all.py`) does not yet cover:
- Syllabus body (`fix_course_syllabus`)
- Assignment descriptions (`fix_course_assignments`)
- `headings_start_at_one` for HTML pages
- `image_alt_placeholder` for HTML pages

These were added to the web app pipeline but `fix_all.py` was not updated. The CLI is used for large courses / batch runs and should stay in sync.

---

## 9. LibraryReference Explanation in Report

Ally's `LibraryReference` issue (3,358 occurrences) is not a real accessibility defect — it's Ally's own library-integration promotional flag. The report should prominently explain this to instructors so they don't waste time on it.

---

## 10. Canvas Instances Other Than UW

`ally_api.py`'s `auto_login()` is specific to the UW Canvas/Ally LTI setup. Generalizing it (configurable OIDC endpoints, client IDs) would make the tool broadly useful.
