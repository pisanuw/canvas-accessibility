"""
Canvas Accessibility Web App
Flask-based web interface for running accessibility fixes on Canvas courses.
"""

import json
import os
import queue
import secrets
import sys
import threading
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (Flask, Response, jsonify, redirect, render_template,
                   request, send_file, session, stream_with_context, url_for)

# Add project root to sys.path so fix modules are importable
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
_production = os.environ.get("RENDER") or os.environ.get("SESSION_COOKIE_SECURE")
app.config.update(
    SESSION_COOKIE_SECURE=bool(_production),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

# ── Auth ──────────────────────────────────────────────────────────────────────

USERNAME       = os.environ.get("APP_USERNAME",    "")
PASSWORD       = os.environ.get("APP_PASSWORD",    "")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME",  "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD",  "")

# ── Fix key → module fix-name maps ───────────────────────────────────────────

_HTML_FIX_MAP = {
    "html_empty_heading":         "empty_heading",
    "html_headings_start_at_one": "headings_start_at_one",
    "html_heading_order":         "heading_order",
    "html_headings_presence":     "headings_presence",
    "html_table_headers":    "table_headers",
    "html_table_captions":   "table_captions",
    "html_lists":            "lists",
    "html_color_contrast":   "color_contrast",
    "html_meta":             "html_meta",
    "html_image_alt":             "image_alt",
    "html_image_alt_placeholder": "image_alt_placeholder",
    "html_links":                 "links",
    "html_broken_links":          "broken_links",
}
_WORD_FIX_MAP = {
    "word_headings_presence":     "headings_presence",
    "word_headings_start_at_one": "headings_start_at_one",
    "word_heading_order":         "heading_order",
    "word_table_headers":         "table_headers",
    "word_no_language":           "no_language",
    "word_image_alt":             "image_alt",
    "word_image_alt_placeholder": "image_alt_placeholder",
}
_PDF_CONTENT_FIX_MAP = {
    "pdf_scanned":               "scanned",
    "pdf_tags":                  "tags",
    "pdf_headings_start_at_one": "headings_start_at_one",
    "pdf_headings_sequential":   "headings_sequential",
    "pdf_table_headers":         "table_headers",
    "pdf_image_alt":             "image_alt",
    "pdf_links":                 "links",
}
_PPTX_FIX_MAP = {
    "pptx_reading_order": "reading_order",
    "pptx_no_language":   "no_language",
    "pptx_slide_title":   "slide_title",
    "pptx_image_alt":     "image_alt",
    "pptx_links":         "links",
}

# ── Busy state ────────────────────────────────────────────────────────────────

_busy_lock  = threading.Lock()
_busy_course = None
_busy_since  = None

# ── Job results store (in-memory) ─────────────────────────────────────────────

_job_results = {}   # job_id → result dict

# ── Admin log path ────────────────────────────────────────────────────────────

ADMIN_LOG = Path(__file__).parent / "admin_log.json"


# ── Decorators ────────────────────────────────────────────────────────────────

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin_authenticated"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper


# ── Routes: User Auth ─────────────────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("login") if not session.get("authenticated") else url_for("course"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if (USERNAME and PASSWORD and
                request.form.get("username") == USERNAME and
                request.form.get("password") == PASSWORD):
            session.clear()
            session["authenticated"] = True
            return redirect(url_for("course"))
        error = "Incorrect username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Routes: Admin Auth ────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        if (ADMIN_USERNAME and ADMIN_PASSWORD and
                request.form.get("username") == ADMIN_USERNAME and
                request.form.get("password") == ADMIN_PASSWORD):
            session["admin_authenticated"] = True
            return redirect(url_for("admin"))
        error = "Incorrect admin credentials."
    return render_template("admin_login.html", error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_authenticated", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@require_admin
def admin():
    log = []
    if ADMIN_LOG.exists():
        try:
            log = json.loads(ADMIN_LOG.read_text())
        except Exception:
            log = []
    return render_template("admin.html", log=log)


# ── Routes: Wizard steps ──────────────────────────────────────────────────────

@app.route("/api/course-search")
@require_auth
def api_course_search():
    """Search the user's Canvas courses by name. Requires ?q= and ?token= params."""
    q     = request.args.get("q", "").strip()
    token = request.args.get("token", "").strip()
    if not token or len(q) < 2:
        return jsonify([])
    try:
        from fixes.canvas_client import CanvasClient
        client = CanvasClient(token=token)
        courses = client.get_all_pages("/courses", {
            "enrollment_type": "teacher",
            "search_term": q,
            "per_page": 20,
        })
        results = [
            {"id": c["id"], "name": c.get("name", ""), "code": c.get("course_code", "")}
            for c in courses if not c.get("access_restricted_by_date")
        ]
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/course", methods=["GET", "POST"])
@require_auth
def course():
    error = None
    if request.method == "POST":
        import re
        url = request.form.get("course_url", "").strip()
        search_token = request.form.get("search_token", "").strip()
        if not url.startswith("https://canvas.uw.edu/courses/"):
            error = "Course URL must start with https://canvas.uw.edu/courses/"
        else:
            match = re.search(r"/courses/(\d+)", url)
            if not match:
                error = "Could not find a course ID in the URL."
            else:
                session["course_id"] = int(match.group(1))
                session["course_url"] = url
                # Pre-fill canvas token from search if provided (user can change it on step 3)
                if search_token:
                    session["canvas_token"] = search_token
                for key in ("job_id", "backup_confirmed", "confirm_done",
                            "ally_token", "ally_client_id", "ally_cookie",
                            "ally_before_count", "course_code"):
                    session.pop(key, None)
                return redirect(url_for("backup"))
    return render_template("course.html", error=error,
                           course_url=session.get("course_url", ""))


@app.route("/backup", methods=["GET", "POST"])
@require_auth
def backup():
    if "course_id" not in session:
        return redirect(url_for("course"))
    if request.method == "POST":
        if request.form.get("confirmed"):
            session["backup_confirmed"] = True
            return redirect(url_for("credentials"))
        return render_template("backup.html", course_id=session["course_id"],
                               error="Please check the box to confirm you have a backup.")
    return render_template("backup.html", course_id=session["course_id"])


@app.route("/credentials", methods=["GET", "POST"])
@require_auth
def credentials():
    if not session.get("backup_confirmed"):
        return redirect(url_for("backup"))
    error = None
    if request.method == "POST":
        if not request.form.get("agree_credentials"):
            error = "You must acknowledge the security notice before proceeding."
        else:
            canvas_token     = request.form.get("canvas_token", "").strip()
            anthropic_key    = request.form.get("anthropic_key", "").strip()
            instructor_email = request.form.get("instructor_email", "").strip()
            if not canvas_token:
                error = "Canvas API token is required."
            else:
                try:
                    from fixes.canvas_client import CanvasClient
                    client = CanvasClient(token=canvas_token)
                    course_info = client.get(f"/courses/{session['course_id']}")
                    if "errors" in course_info:
                        error = (f"Cannot access course {session['course_id']} with this token. "
                                 "Please check your token and course URL.")
                    else:
                        session["canvas_token"]     = canvas_token
                        session["anthropic_key"]    = anthropic_key
                        session["instructor_email"] = instructor_email
                        session["course_name"]   = course_info.get(
                            "name", f"Course {session['course_id']}")
                        session["course_code"]   = course_info.get(
                            "course_code", course_info.get(
                            "sis_course_id", f"Course {session['course_id']}"))
                        session["job_id"]        = secrets.token_hex(16)
                        session.pop("confirm_done", None)
                        return redirect(url_for("confirm"))
                except Exception as e:
                    error = f"Could not connect to Canvas: {e}"
    return render_template("credentials.html", error=error,
                           instructor_email=session.get("instructor_email", ""))


@app.route("/confirm", methods=["GET", "POST"])
@require_auth
def confirm():
    if "job_id" not in session:
        return redirect(url_for("credentials"))
    if request.method == "POST":
        session["confirm_done"] = True
        session["selected_fixes"] = request.form.getlist("fix")
        return redirect(url_for("running"))
    has_ai = bool(session.get("anthropic_key", ""))
    return render_template("confirm.html",
                           course_name=session.get("course_name"),
                           course_id=session.get("course_id"),
                           has_ai=has_ai)


@app.route("/confirm-scan")
@require_auth
def confirm_scan():
    """JSON endpoint: Ally login + issue count scan. Caches result in session."""
    course_id    = session.get("course_id")
    canvas_token = session.get("canvas_token")
    if not course_id or not canvas_token:
        return jsonify({"error": "Session expired"}), 400

    try:
        from ally_api import auto_login as ally_auto_login, get_course_report
        ally_token, ally_client_id, ally_cookie = ally_auto_login(canvas_token, course_id)
        report = get_course_report(ally_token, ally_client_id, course_id, cookie=ally_cookie)
        total_count  = report.get("total", 0)
        issues       = report.get("issues", {})
        lib_ref      = issues.get("LibraryReference", {}).get("count", 0)
        issue_count  = total_count - lib_ref          # matches Canvas Ally UI
        score_pct    = round(report.get("score", {}).get("total", 0) * 100, 1)

        session["ally_token"]        = ally_token
        session["ally_client_id"]    = ally_client_id
        session["ally_cookie"]       = ally_cookie
        session["ally_before_count"] = issue_count
        session["ally_score_pct"]    = score_pct

        return jsonify({
            "issue_count": issue_count,
            "lib_ref_count": lib_ref,
            "score_pct":   score_pct,
            "course_name": session.get("course_name"),
            "course_code": session.get("course_code"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/running")
@require_auth
def running():
    if not session.get("confirm_done"):
        return redirect(url_for("confirm"))
    if "job_id" not in session:
        return redirect(url_for("credentials"))
    return render_template("running.html",
                           course_name=session.get("course_name"),
                           course_id=session.get("course_id"),
                           has_ai=bool(session.get("anthropic_key")))


@app.route("/stream")
@require_auth
def stream():
    job_id           = session.get("job_id")
    course_id        = session.get("course_id")
    canvas_token     = session.get("canvas_token")
    anthropic_key    = session.get("anthropic_key", "")
    instructor_email = session.get("instructor_email", "")
    course_name      = session.get("course_name", f"Course {course_id}")
    course_code      = session.get("course_code", "")
    has_ai           = bool(anthropic_key)
    ally_token       = session.get("ally_token", "")
    ally_client_id   = session.get("ally_client_id", 5)
    ally_cookie      = session.get("ally_cookie", "")
    ally_score_pct   = session.get("ally_score_pct", None)
    selected_fixes = session.get("selected_fixes", [])

    if not all([job_id, course_id, canvas_token]):
        def _err():
            yield f"data: {json.dumps({'type':'error','message':'Session expired. Please start over.'})}\n\n"
        return Response(_err(), mimetype="text/event-stream")

    def generate():
        global _busy_course, _busy_since

        if not _busy_lock.acquire(blocking=False):
            yield f"data: {json.dumps({'type':'busy'})}\n\n"
            return

        _busy_course = course_name
        _busy_since  = datetime.now().strftime("%H:%M")
        _job_results[job_id] = {"running": True}

        msg_q      = queue.Queue()
        result_box = {}

        class _Capture:
            def write(self, text):
                import re as _re
                stripped = text.strip()
                if not stripped:
                    return
                msg_q.put(("log", stripped))
                # Detect section header: lines starting with ──
                if stripped.startswith("──") or stripped.startswith("--"):
                    label = stripped.strip("─- \t")
                    msg_q.put(("section", {"label": label}))
                # Detect progress line: "  Processing: name (3/28)"
                _m = _re.search(r'\((\d+)/(\d+)\)$', stripped)
                if _m:
                    msg_q.put(("progress", {
                        "current": int(_m.group(1)),
                        "total":   int(_m.group(2)),
                    }))
            def flush(self): pass

        def _run():
            old_stdout = sys.stdout
            sys.stdout = _Capture()
            try:
                from fixes.canvas_client import CanvasClient
                client = CanvasClient(token=canvas_token)

                # ── Ally login (reuse session token if available) ─────────
                _ally_token     = ally_token
                _ally_client_id = ally_client_id
                _ally_cookie    = ally_cookie

                if not _ally_token:
                    try:
                        from ally_api import auto_login as ally_auto_login
                        _ally_token, _ally_client_id, _ally_cookie = ally_auto_login(
                            canvas_token, course_id)
                    except Exception as _e:
                        msg_q.put(("log", f"Ally auto-login failed: {_e} — image fixes skipped"))

                # ── Before scan ───────────────────────────────────────────
                msg_q.put(("log", "Scanning course for accessibility issues…"))
                before_count = 0
                if _ally_token:
                    try:
                        from ally_api import get_course_report
                        _rep = get_course_report(_ally_token, _ally_client_id,
                                                 course_id, cookie=_ally_cookie)
                        _lib_ref = _rep.get("issues", {}).get("LibraryReference", {}).get("count", 0)
                        before_count = _rep.get("total", 0) - _lib_ref
                    except Exception as _e:
                        msg_q.put(("log", f"Before scan failed: {_e}"))
                msg_q.put(("before", before_count))
                msg_q.put(("log", f"Found {before_count} issues before fixes.\n"))

                # Set Anthropic key for AI-assisted fixes
                if has_ai and anthropic_key:
                    os.environ["ANTHROPIC_API_KEY"] = anthropic_key

                fix_results = {}
                sel = set(selected_fixes)

                # ── HTML page + assignment fixes ───────────────────────────
                html_fixes = [v for k, v in _HTML_FIX_MAP.items() if k in sel]
                if html_fixes:
                    from fixes.fix_html_pages import (fix_course_pages,
                                                       fix_course_assignments,
                                                       fix_course_syllabus)
                    msg_q.put(("log", "── HTML Page Fixes ──────────────────────"))
                    fix_results["html"] = fix_course_pages(client, course_id, html_fixes)
                    msg_q.put(("log", "── Syllabus Fixes ───────────────────────"))
                    fix_results["syllabus"] = fix_course_syllabus(client, course_id, html_fixes)
                    msg_q.put(("log", "── Assignment Description Fixes ─────────"))
                    fix_results["assignments"] = fix_course_assignments(client, course_id, html_fixes)
                else:
                    fix_results["html"] = []
                    fix_results["syllabus"] = []
                    fix_results["assignments"] = []

                # ── Word doc fixes ─────────────────────────────────────────
                word_fixes = [v for k, v in _WORD_FIX_MAP.items() if k in sel]
                if word_fixes:
                    msg_q.put(("log", "── Word Document Fixes ──────────────────"))
                    from fixes.fix_word_docs import fix_course_word_files
                    fix_results["word"] = fix_course_word_files(client, course_id, word_fixes)
                else:
                    fix_results["word"] = []

                # ── Image fixes (via Ally API) ────────────────────────────
                run_decorative = "image_decorative" in sel
                run_seizure    = "image_seizure"    in sel
                if (run_decorative or run_seizure) and _ally_token:
                    from fixes.fix_image_files import (
                        fix_course_image_files, fix_course_seizure_images)
                    if run_decorative:
                        msg_q.put(("log", "── Image Fixes (Ally) ───────────────────"))
                        fix_results["images"] = fix_course_image_files(
                            client, _ally_token, _ally_cookie,
                            _ally_client_id, course_id)
                    else:
                        fix_results["images"] = []
                    if run_seizure:
                        msg_q.put(("log", "── Image Seizure Replacements ───────────"))
                        fix_results["seizure"] = fix_course_seizure_images(
                            client, _ally_token, _ally_cookie,
                            _ally_client_id, course_id)
                    else:
                        fix_results["seizure"] = []
                elif (run_decorative or run_seizure) and not _ally_token:
                    fix_results["images"]  = []
                    fix_results["seizure"] = []
                    msg_q.put(("log", "  Image fixes skipped — Ally login unavailable"))
                else:
                    fix_results["images"]  = []
                    fix_results["seizure"] = []

                # ── PDF metadata fixes ─────────────────────────────────────
                if "pdf_metadata_all" in sel:
                    msg_q.put(("log", "── PDF Metadata Fixes ───────────────────"))
                    from fixes.fix_pdf_metadata import fix_course_pdfs
                    fix_results["pdf_meta"] = fix_course_pdfs(client, course_id, ["all"])
                else:
                    fix_results["pdf_meta"] = []

                # ── PDF content fixes ──────────────────────────────────────
                pdf_content_fixes = [v for k, v in _PDF_CONTENT_FIX_MAP.items() if k in sel]
                if pdf_content_fixes:
                    msg_q.put(("log", "── PDF Content Fixes ────────────────────"))
                    from fixes.fix_pdf_content import fix_course_pdf_content
                    fix_results["pdf_content"] = fix_course_pdf_content(
                        client, course_id, pdf_content_fixes)
                else:
                    fix_results["pdf_content"] = []

                # ── PowerPoint fixes ───────────────────────────────────────
                pptx_fixes = [v for k, v in _PPTX_FIX_MAP.items() if k in sel]
                if pptx_fixes:
                    msg_q.put(("log", "── PowerPoint Fixes ─────────────────────"))
                    from fixes.fix_pptx_files import fix_course_pptx_files
                    fix_results["pptx"] = fix_course_pptx_files(client, course_id, pptx_fixes)
                else:
                    fix_results["pptx"] = []

                # ── After count ────────────────────────────────────────────
                # Ally processes uploaded files asynchronously — it can take
                # minutes to hours before updated scores are available.
                # Report the before count as "after" and note that Ally will
                # update on its own schedule.
                msg_q.put(("log", "\nNote: Ally re-scores files asynchronously."))
                msg_q.put(("log", "  Check the Ally dashboard in ~1 hour to see updated scores."))
                after_count = before_count  # placeholder; real count will differ after Ally rescans
                msg_q.put(("after", after_count))

                result_box["success"]      = True
                result_box["before"]       = before_count
                result_box["after"]        = after_count
                result_box["fix_results"]  = fix_results
                msg_q.put(("done", result_box))

            except Exception as exc:
                import traceback
                msg_q.put(("log", traceback.format_exc()))
                msg_q.put(("error", str(exc)))
            finally:
                sys.stdout = old_stdout
                os.environ.pop("ANTHROPIC_API_KEY", None)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        try:
            while True:
                try:
                    kind, data = msg_q.get(timeout=1.0)
                except queue.Empty:
                    if not thread.is_alive():
                        if not _job_results.get(job_id, {}).get("success"):
                            _job_results[job_id] = {"running": False, "success": False,
                                                     "error": "Job ended unexpectedly"}
                            yield f"data: {json.dumps({'type':'error','message':'Job ended unexpectedly'})}\n\n"
                        break
                    yield f"data: {json.dumps({'type':'heartbeat'})}\n\n"
                    continue

                if kind == "log":
                    yield f"data: {json.dumps({'type':'log','message':data})}\n\n"

                elif kind == "section":
                    yield f"data: {json.dumps({'type':'section','label':data['label']})}\n\n"

                elif kind == "progress":
                    yield f"data: {json.dumps({'type':'progress','current':data['current'],'total':data['total']})}\n\n"

                elif kind == "before":
                    yield f"data: {json.dumps({'type':'before','count':data})}\n\n"

                elif kind == "after":
                    yield f"data: {json.dumps({'type':'after','count':data})}\n\n"

                elif kind == "done":
                    before = data["before"]
                    after  = data["after"]
                    # after == before here because Ally rescores asynchronously
                    pct    = 0
                    completed_at = datetime.now().strftime("%Y-%m-%d %H:%M")
                    result = {
                        "running":          False,
                        "success":          True,
                        "before":           before,
                        "after":            after,
                        "ally_async":       True,  # flag: after count not yet meaningful
                        "pct_improvement":  pct,
                        "course_name":      course_name,
                        "course_id":        course_id,
                        "course_code":      course_code,
                        "has_ai":           has_ai,
                        "instructor_email": instructor_email,
                        "ally_score_pct":   ally_score_pct,
                        "summary":          _summarize(data["fix_results"]),
                        "completed_at":     completed_at,
                        "job_id":           job_id,
                    }
                    result["fix_results"]  = data["fix_results"]
                    result["report_html"]  = _generate_html_report(result)
                    _job_results[job_id]   = result
                    _append_admin_log(result)
                    _send_run_notification(result)
                    yield f"data: {json.dumps({'type':'done'})}\n\n"
                    break

                elif kind == "error":
                    _job_results[job_id] = {"running": False, "success": False, "error": data}
                    yield f"data: {json.dumps({'type':'error','message':data})}\n\n"
                    break

        finally:
            _busy_course = None
            _busy_since  = None
            _busy_lock.release()

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/report")
@require_auth
def report():
    job_id = session.get("job_id")
    if not job_id or job_id not in _job_results:
        return redirect(url_for("course"))
    result = _job_results[job_id]
    if result.get("running"):
        return redirect(url_for("running"))
    return render_template("report.html", result=result)


@app.route("/report/download/<job_id>")
@require_auth
def report_download(job_id):
    result = _job_results.get(job_id)
    if not result or "report_html" not in result:
        return "Report not found", 404
    ts = result.get("completed_at", "").replace(" ", "_").replace(":", "")
    filename = f"accessibility_report_{result['course_id']}_{ts}.html"
    return Response(
        result["report_html"],
        mimetype="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/links")
@require_auth
def links_check():
    if "job_id" not in session or "canvas_token" not in session:
        return redirect(url_for("course"))
    return render_template("links.html",
                           course_name=session.get("course_name"),
                           course_id=session.get("course_id"))


@app.route("/links-stream")
@require_auth
def links_stream():
    course_id    = session.get("course_id")
    canvas_token = session.get("canvas_token")
    course_name  = session.get("course_name", f"Course {course_id}")

    if not course_id or not canvas_token:
        def _err():
            yield f"data: {json.dumps({'type':'error','message':'Session expired.'})}\n\n"
        return Response(_err(), mimetype="text/event-stream")

    def generate():
        msg_q = queue.Queue()

        def _run():
            try:
                from fixes.canvas_client import CanvasClient
                from fixes.fix_html_pages import fix_broken_links

                client = CanvasClient(token=canvas_token)
                pages = client.list_pages(course_id)
                msg_q.put(("log", f"Found {len(pages)} page(s) to check"))

                for page_stub in pages:
                    page_url  = page_stub["url"]
                    page_title = page_stub.get("title", page_url)
                    msg_q.put(("log", f"── {page_title}"))

                    try:
                        page = client.get_page(course_id, page_url)
                        body = page.get("body") or ""
                        if not body.strip():
                            msg_q.put(("log", "  (empty page, skipped)"))
                            msg_q.put(("page", 1))
                            continue

                        updated_body, changes = fix_broken_links(body)
                        msg_q.put(("page", 1))

                        if changes:
                            msg_q.put(("broken", len(changes)))
                            for ch in changes:
                                msg_q.put(("log", f"  BROKEN: {ch}"))
                            client.update_page(course_id, page_url, updated_body)
                            msg_q.put(("log", f"  Updated page with {len(changes)} fix(es)"))
                        else:
                            msg_q.put(("log", "  No broken links found"))

                    except Exception as exc:
                        msg_q.put(("log", f"  ERROR: {exc}"))

                msg_q.put(("done", None))

            except Exception as exc:
                import traceback
                msg_q.put(("log", traceback.format_exc()))
                msg_q.put(("error", str(exc)))

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

        while True:
            try:
                kind, data = msg_q.get(timeout=1.0)
            except queue.Empty:
                if not thread.is_alive():
                    yield f"data: {json.dumps({'type':'done'})}\n\n"
                    break
                yield f"data: {json.dumps({'type':'heartbeat'})}\n\n"
                continue

            if kind == "log":
                yield f"data: {json.dumps({'type':'log','message':data})}\n\n"
            elif kind == "page":
                yield f"data: {json.dumps({'type':'page','count':data})}\n\n"
            elif kind == "broken":
                yield f"data: {json.dumps({'type':'broken','count':data})}\n\n"
            elif kind == "done":
                yield f"data: {json.dumps({'type':'done'})}\n\n"
                break
            elif kind == "error":
                yield f"data: {json.dumps({'type':'error','message':data})}\n\n"
                break

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/busy")
@require_auth
def busy():
    return render_template("busy.html",
                           busy_course=_busy_course,
                           busy_since=_busy_since)


@app.route("/rerun")
@require_auth
def rerun():
    """Go back to the confirm/fix-selector page for the same course and credentials."""
    session.pop("confirm_done", None)
    session.pop("selected_fixes", None)
    session["job_id"] = secrets.token_hex(16)
    return redirect(url_for("confirm"))


@app.route("/restart")
@require_auth
def restart():
    for key in ["course_id", "course_url", "course_name", "course_code",
                "backup_confirmed", "canvas_token", "anthropic_key", "job_id",
                "confirm_done", "selected_fixes", "ally_token", "ally_client_id",
                "ally_cookie", "ally_before_count"]:
        session.pop(key, None)
    return redirect(url_for("course"))


@app.route("/health")
def health():
    return "OK", 200


@app.route("/download/canvas-backup")
def download_canvas_backup():
    return send_file(ROOT / "canvas-backup.py", as_attachment=True,
                     download_name="canvas-backup.py")


# ── Helpers ───────────────────────────────────────────────────────────────────

TYPE_LABELS = {
    "html":        "Canvas Pages",
    "syllabus":    "Syllabus",
    "assignments": "Assignment Descriptions",
    "word":        "Word Documents",
    "images":      "Image Files (Decorative)",
    "seizure":     "Image Files (Seizure Replacement)",
    "pdf_meta":    "PDF Metadata",
    "pdf_content": "PDF Content (OCR/Tags/Links/Headings)",
    "pptx":        "PowerPoint Files",
}


def _summarize(fix_results: dict) -> list[dict]:
    rows = []
    for key, results in fix_results.items():
        changes = sum(len(r.get("changes", [])) for r in results)
        updated = sum(1 for r in results if r.get("updated"))
        errors  = sum(1 for r in results if "error" in r)
        detail = []
        for r in results:
            name = r.get("page") or r.get("file") or "—"
            detail.append({
                "name":    name,
                "changes": r.get("changes", []),
                "updated": r.get("updated", False),
                "error":   r.get("error", ""),
            })
        rows.append({
            "key":     key,
            "label":   TYPE_LABELS.get(key, key),
            "items":   len(results),
            "changes": changes,
            "updated": updated,
            "errors":  errors,
            "detail":  detail,
        })
    return rows


def _append_admin_log(result: dict):
    entry = {
        "timestamp":   result["completed_at"],
        "course_id":   result["course_id"],
        "course_code": result.get("course_code", ""),
        "course_name": result.get("course_name", ""),
        "before":      result["before"],
        "after":       result["after"],
        "pct":         result["pct_improvement"],
        "job_id":      result["job_id"],
    }
    log = []
    if ADMIN_LOG.exists():
        try:
            log = json.loads(ADMIN_LOG.read_text())
        except Exception:
            log = []
    log.append(entry)
    try:
        ADMIN_LOG.write_text(json.dumps(log, indent=2))
    except Exception:
        pass


def _send_run_notification(result: dict):
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        return
    try:
        import resend
        resend.api_key = api_key
        from_email  = os.environ.get("FROM_EMAIL",  "noreply@resend.dev")
        subject = (f"Accessibility Fix Complete: {result['course_name']} "
                   f"({result['before']} → {result['after']} issues)")
        report_html = result.get("report_html", "<p>Report unavailable.</p>")

        admin_email = os.environ.get("ADMIN_EMAIL", "")
        if admin_email:
            resend.Emails.send({
                "from":    from_email,
                "to":      admin_email,
                "subject": subject,
                "html":    report_html,
            })

        instructor_email = result.get("instructor_email", "")
        if instructor_email and instructor_email != admin_email:
            resend.Emails.send({
                "from":    from_email,
                "to":      instructor_email,
                "subject": subject,
                "html":    report_html,
            })
    except Exception:
        pass


def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _generate_html_report(result: dict) -> str:
    before      = result.get("before", 0)
    after       = result.get("after", 0)
    pct         = result.get("pct_improvement", 0)
    rows        = result.get("summary", [])
    fix_results = result.get("fix_results", {})

    # ── Summary table ─────────────────────────────────────────────────────────
    table_rows = ""
    for row in rows:
        chg_badge = (
            f'<span style="background:#16a34a;color:#fff;padding:2px 8px;border-radius:4px;font-size:.85rem;">'
            f'{row["changes"]}</span>' if row["changes"] > 0 else
            '<span style="color:#6b7280;">0</span>'
        )
        err_badge = (
            f'<span style="background:#dc2626;color:#fff;padding:2px 8px;border-radius:4px;font-size:.85rem;">'
            f'{row["errors"]}</span>' if row["errors"] > 0 else "&mdash;"
        )
        table_rows += f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;font-weight:600;">{row["label"]}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:center;">{row["items"]}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:center;">{chg_badge}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:center;">{row["updated"]}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:center;">{err_badge}</td>
        </tr>"""

    # ── Per-file detail sections ───────────────────────────────────────────────
    detail_sections = ""
    for type_key, file_results in fix_results.items():
        if not file_results:
            continue
        label = TYPE_LABELS.get(type_key, type_key)

        # Count files with changes vs errors vs no changes
        with_changes = [r for r in file_results if r.get("changes")]
        with_errors  = [r for r in file_results if r.get("error")]
        no_changes   = [r for r in file_results
                        if not r.get("changes") and not r.get("error")]

        file_rows = ""

        for r in with_changes:
            fname = _escape_html(r.get("file") or r.get("filename") or r.get("page", "—"))
            change_list = "".join(
                f'<li style="margin:3px 0;">{_escape_html(str(c))}</li>'
                for c in r.get("changes", [])
            )
            file_rows += f"""
            <tr>
              <td style="padding:8px 12px;border-bottom:1px solid #f3f4f6;vertical-align:top;
                         font-size:.88rem;color:#111827;width:30%;word-break:break-word;">
                {fname}
                {'<br><span style="font-size:.78rem;color:#16a34a;font-weight:600;">&#10003; updated</span>'
                 if r.get("updated") else ''}
              </td>
              <td style="padding:8px 12px;border-bottom:1px solid #f3f4f6;font-size:.85rem;">
                <ul style="margin:0;padding-left:18px;color:#374151;">{change_list}</ul>
              </td>
            </tr>"""

        for r in with_errors:
            fname = _escape_html(r.get("file") or r.get("filename") or r.get("page", "—"))
            err   = _escape_html(str(r.get("error", "")))
            file_rows += f"""
            <tr>
              <td style="padding:8px 12px;border-bottom:1px solid #f3f4f6;
                         font-size:.88rem;color:#111827;width:30%;word-break:break-word;">
                {fname}
              </td>
              <td style="padding:8px 12px;border-bottom:1px solid #f3f4f6;
                         font-size:.85rem;color:#dc2626;">
                &#10007; Error: {err}
              </td>
            </tr>"""

        no_change_names = ", ".join(
            _escape_html(r.get("file") or r.get("filename") or r.get("page", "?"))
            for r in no_changes
        )
        no_change_row = ""
        if no_changes:
            no_change_row = f"""
            <tr>
              <td colspan="2" style="padding:8px 12px;font-size:.82rem;color:#9ca3af;
                                     border-bottom:1px solid #f3f4f6;">
                No changes needed ({len(no_changes)} item(s)):
                {no_change_names}
              </td>
            </tr>"""

        detail_sections += f"""
  <details style="margin-top:20px;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;">
    <summary style="padding:12px 16px;background:#f9fafb;cursor:pointer;font-weight:600;
                    font-size:.95rem;list-style:none;display:flex;justify-content:space-between;
                    align-items:center;">
      <span>{label}</span>
      <span style="font-size:.82rem;font-weight:400;color:#6b7280;">
        {len(file_results)} item(s) &nbsp;&middot;&nbsp;
        {len(with_changes)} with changes &nbsp;&middot;&nbsp;
        {len(with_errors)} error(s)
      </span>
    </summary>
    <table style="width:100%;border-collapse:collapse;">
      <thead>
        <tr style="background:#f3f4f6;">
          <th style="padding:8px 12px;text-align:left;font-size:.82rem;
                     color:#6b7280;border-bottom:1px solid #e5e7eb;width:30%;">File / Page</th>
          <th style="padding:8px 12px;text-align:left;font-size:.82rem;
                     color:#6b7280;border-bottom:1px solid #e5e7eb;">Changes Applied</th>
        </tr>
      </thead>
      <tbody>
        {file_rows}
        {no_change_row}
      </tbody>
    </table>
  </details>"""

    # ── Banner ────────────────────────────────────────────────────────────────
    ally_async = result.get("ally_async", False)
    total_changes = sum(row["changes"] for row in rows)

    if ally_async:
        banner = (
            f'<div style="background:#eff6ff;border:1px solid #93c5fd;border-radius:8px;'
            f'padding:16px 20px;margin:20px 0;">'
            f'<strong>Fixes applied — Ally is re-scoring asynchronously.</strong><br>'
            f'<span style="font-size:.9rem;color:#374151;">'
            f'{total_changes} change(s) were made across your course files. '
            f'Ally typically updates scores within 1&ndash;2 hours. '
            f'Check the <a href="https://canvas.uw.edu/courses/{result.get("course_id")}/external_tools/148172"'
            f' target="_blank">Ally dashboard</a> later to confirm the improvement.</span></div>'
        )
    elif pct > 0:
        banner = (
            f'<div style="background:#d1fae5;border:1px solid #6ee7b7;border-radius:8px;'
            f'padding:20px 24px;margin:20px 0;text-align:center;">'
            f'<div style="font-size:2rem;font-weight:700;color:#065f46;">{pct}% improvement</div>'
            f'<div style="color:#065f46;margin-top:4px;">'
            f'{before - after} issues resolved out of {before} detected</div></div>'
        )
    elif before == 0:
        banner = (
            '<div style="background:#d1fae5;border:1px solid #6ee7b7;border-radius:8px;'
            'padding:20px 24px;margin:20px 0;text-align:center;">'
            '<div style="font-size:1.5rem;font-weight:700;color:#065f46;">&#10003; Already clean</div>'
            '<div style="color:#065f46;margin-top:4px;">No accessibility issues were detected.</div></div>'
        )
    else:
        banner = (
            f'<div style="background:#fef3c7;border:1px solid #fcd34d;border-radius:8px;'
            f'padding:16px 20px;margin:20px 0;">'
            f'<strong>{after} issues remain</strong> — some require manual intervention '
            f'(video captions, scanned PDFs, color contrast).</div>'
        )

    ai_note = "" if result.get("has_ai") else (
        '<li>Image alt text &amp; slide titles — re-run with an Anthropic API key for AI-powered fixes</li>'
    )

    remains_section = ""
    if after > 0:
        remains_section = f"""
  <h2 style="margin:28px 0 8px;font-size:1.1rem;color:#374151;border-bottom:2px solid #e5e7eb;padding-bottom:6px;">
    What Remains</h2>
  <div style="background:#fef3c7;border:1px solid #fcd34d;border-radius:6px;padding:16px 20px;">
    <strong>Issues that require manual attention:</strong>
    <ul style="margin:8px 0 0;padding-left:20px;line-height:2;">
      <li>Scanned PDFs (no machine-readable text) — need OCR or replacement</li>
      <li>Video captions — must be added through YouTube, Kaltura, or Canvas Media</li>
      <li>Color contrast — requires visual design changes</li>
      <li>Complex PDF tag structure — requires Adobe Acrobat or specialist tools</li>
      {ai_note}
    </ul>
  </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Accessibility Report — {_escape_html(result.get("course_name", ""))} — {result.get("completed_at", "")}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           max-width: 960px; margin: 40px auto; padding: 0 20px; color: #111827; }}
    h1 {{ font-size: 1.6rem; margin-bottom: 4px; }}
    .meta {{ color: #6b7280; font-size: .9rem; margin-bottom: 24px; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
    th {{ background: #f3f4f6; padding: 10px 12px; text-align: left;
          font-size: .85rem; color: #374151; border-bottom: 2px solid #d1d5db; }}
    th:not(:first-child) {{ text-align: center; }}
    details summary::-webkit-details-marker {{ display: none; }}
    details[open] summary {{ border-bottom: 1px solid #e5e7eb; }}
    footer {{ margin-top: 40px; font-size: .8rem; color: #9ca3af;
              border-top: 1px solid #e5e7eb; padding-top: 12px; }}
  </style>
</head>
<body>
  <h1>Accessibility Report</h1>
  <div class="meta">
    <strong>{_escape_html(result.get("course_name", ""))}</strong> &nbsp;&middot;&nbsp;
    Course {result.get("course_id", "")}
    &nbsp;&middot;&nbsp; {_escape_html(result.get("course_code", ""))}
    &nbsp;&middot;&nbsp; Completed {result.get("completed_at", "")}
    {'&nbsp;&middot;&nbsp; AI-assisted' if result.get("has_ai") else ""}
  </div>

  <div style="display:flex;gap:16px;margin-bottom:8px;">
    <div style="flex:1;background:#fff0f0;border:1px solid #fca5a5;border-radius:8px;padding:16px;text-align:center;">
      <div style="font-size:.8rem;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;">Issues Before</div>
      <div style="font-size:2.5rem;font-weight:700;color:#dc2626;">{before}</div>
    </div>
    {'<div style="flex:1;background:#f0f9ff;border:1px solid #93c5fd;border-radius:8px;padding:16px;text-align:center;"><div style="font-size:.8rem;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;">Issues After (pending Ally rescan)</div><div style="font-size:1rem;font-weight:700;color:#2563eb;padding-top:8px;">Check Ally in ~1 hr</div></div>' if ally_async else f'<div style="flex:1;background:#f0fdf4;border:1px solid #86efac;border-radius:8px;padding:16px;text-align:center;"><div style="font-size:.8rem;color:#6b7280;text-transform:uppercase;letter-spacing:.05em;">Issues After</div><div style="font-size:2.5rem;font-weight:700;color:#16a34a;">{after}</div></div>'}
  </div>

  {banner}

  <h2 style="margin:28px 0 8px;font-size:1.1rem;color:#374151;border-bottom:2px solid #e5e7eb;padding-bottom:6px;">
    Summary by Content Type</h2>
  <table>
    <thead>
      <tr>
        <th>Type</th>
        <th>Items Scanned</th>
        <th>Changes Made</th>
        <th>Files Updated</th>
        <th>Errors</th>
      </tr>
    </thead>
    <tbody>{table_rows}</tbody>
  </table>

  <h2 style="margin:32px 0 4px;font-size:1.1rem;color:#374151;border-bottom:2px solid #e5e7eb;padding-bottom:6px;">
    Detailed Changes per File</h2>
  <p style="font-size:.85rem;color:#6b7280;margin:4px 0 8px;">
    Click a section to expand. Only items with changes or errors are shown individually;
    items with no changes are listed at the bottom of each section.
  </p>
  {detail_sections}

  {remains_section}

  <footer>Generated by Canvas Accessibility Fixer</footer>
</body>
</html>"""


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
