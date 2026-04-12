#!/usr/bin/env python3
"""
canvas-backup.py - Back up Canvas courses in two steps: start exports, then download.

Run without arguments for usage instructions.
"""

import sys

MIN_PYTHON = (3, 10)

if sys.version_info < MIN_PYTHON:
    print(f"""
ERROR: Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required.
You are running Python {sys.version_info.major}.{sys.version_info.minor}.

How to upgrade:
  macOS (Homebrew):   brew install python@3.13
                      Then run:  python3.13 canvas-backup.py ...
  macOS (installer):  https://www.python.org/downloads/
  Linux:              sudo apt install python3.11   (Ubuntu/Debian)
  Windows:            https://www.python.org/downloads/

After upgrading, verify with:  python3 --version
""")
    sys.exit(1)

import argparse
import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent
_CLIENT_PATH = _ROOT / "fixes" / "canvas_client.py"

if not _CLIENT_PATH.exists():
    print(f"ERROR: Required file not found: {_CLIENT_PATH}\n"
          f"Run this script from the canvas-accessibility project directory.")
    sys.exit(1)

sys.path.insert(0, str(_ROOT))
from fixes.canvas_client import CanvasClient

TOKEN_FILE  = _ROOT / "canvas-token.txt"
JOBS_FILE   = _ROOT / "backups" / "backup_jobs.json"
DEFAULT_OUT = _ROOT / "backups"
CHUNK_SIZE  = 1 << 20
SKIP_PATTERN = "497"


def check_token() -> str:
    if not TOKEN_FILE.exists():
        print(f"""
ERROR: Canvas API token file not found: {TOKEN_FILE}

HOW TO CREATE A CANVAS TOKEN:
  1. Log in to Canvas at https://canvas.uw.edu
  2. Click your name/avatar (top-left) -> Settings
  3. Scroll to "Approved Integrations" -> click "+ New Access Token"
  4. Purpose: e.g. "canvas-backup"  |  Expiry: leave blank
  5. Click "Generate Token"
  6. COPY the token immediately -- Canvas will not show it again!

Save it:
  echo "paste-your-token-here" > {TOKEN_FILE}
""")
        sys.exit(1)
    token = TOKEN_FILE.read_text().strip()
    if not token:
        print(f"ERROR: {TOKEN_FILE} is empty. Paste your Canvas token into it.")
        sys.exit(1)
    return token


USAGE = f"""
canvas-backup.py -- Back up Canvas courses to your local machine.

QUICK START (two steps):

  Step 1 -- Tell Canvas to prepare exports:
      python3 canvas-backup.py start --all
      (Canvas processes exports in the background, usually a few minutes each)

  Check progress any time:
      python3 canvas-backup.py status

  Step 2 -- Download completed exports:
      python3 canvas-backup.py download
      (Files saved to backups/  -- re-run to pick up any still pending)

COMMANDS:
  start      Start export jobs on Canvas (does NOT download yet)
  download   Download completed exports to disk
  status     Show a table of all jobs and their state

EXAMPLES:
  python3 canvas-backup.py start --all               # all courses (skips 497)
  python3 canvas-backup.py start --all --dry-run     # preview without doing anything
  python3 canvas-backup.py start --course-id 1492292 # single course
  python3 canvas-backup.py start --all --force       # force-restart all exports
  python3 canvas-backup.py status                    # show all job states
  python3 canvas-backup.py download                  # download all ready exports
  python3 canvas-backup.py download --course-id ID   # one course
  python3 canvas-backup.py download --out-dir /path  # save elsewhere
  python3 canvas-backup.py download --force          # re-download already done

SETUP:
  Token file:  {TOKEN_FILE}
  See HOW TO CREATE A CANVAS TOKEN above if the file is missing.
"""


def load_jobs(require_exists: bool = True) -> list[dict]:
    if not JOBS_FILE.exists():
        if require_exists:
            print(f"ERROR: No jobs file at {JOBS_FILE}\n"
                  f"Run:  python3 canvas-backup.py start --all  first.")
            sys.exit(1)
        return []
    return json.loads(JOBS_FILE.read_text())


def save_jobs(jobs: list[dict]) -> None:
    JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    JOBS_FILE.write_text(json.dumps(jobs, indent=2))


def jobs_by_course_id(jobs: list[dict]) -> dict:
    return {j["course_id"]: j for j in jobs}


def print_status(jobs: list[dict]) -> None:
    total      = len(jobs)
    exported   = sum(1 for j in jobs if j["workflow_state"] == "exported")
    downloaded = sum(1 for j in jobs if j["downloaded"])
    pending    = sum(1 for j in jobs if j["workflow_state"] in ("created", "exporting"))
    failed     = sum(1 for j in jobs if j["workflow_state"] == "failed")

    STATE = {"exported": "ready", "exporting": "working", "created": "queued", "failed": "FAILED"}
    print(f"\n{'Course':55}  {'State':10}  Downloaded")
    print("-" * 90)
    for j in sorted(jobs, key=lambda x: x["course_name"]):
        state = STATE.get(j["workflow_state"], j["workflow_state"])
        dl    = "yes  " + (j["downloaded_at"] or "") if j["downloaded"] else ""
        print(f"  {j['course_name'][:54]:54}  {state:10}  {dl}")
    print("-" * 90)
    print(f"Total: {total}  ready: {exported}  downloaded: {downloaded}  "
          f"pending: {pending}  failed: {failed}")


def list_teacher_courses(client: CanvasClient) -> list[dict]:
    return client.get_all_pages("/courses", {
        "enrollment_type": "teacher",
        "state[]": ["available", "completed"],
    })


def get_export_status(client: CanvasClient, course_id: int, export_id: int) -> dict:
    return client.get(f"/courses/{course_id}/content_exports/{export_id}")


def download_export(url: str, dest: Path, token: str) -> None:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done  = 0
            with open(tmp, "wb") as f:
                while chunk := resp.read(CHUNK_SIZE):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        print(f"    {done*100//total:3d}%  {done/(1<<20):.1f} MiB",
                              end="\r", flush=True)
        print()
        tmp.rename(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _safe_filename(name: str, cid: int) -> str:
    slug = re.sub(r'[^\w\s-]', '', name.lower())
    slug = re.sub(r'[\s_]+', '-', slug).strip('-')[:60]
    return f"{cid}_{slug}.imscc"


def cmd_start(args, client: CanvasClient) -> None:
    jobs     = load_jobs(require_exists=False)
    existing = jobs_by_course_id(jobs)

    if args.course_id:
        info = client.get(f"/courses/{args.course_id}")
        if "errors" in info:
            print(f"ERROR: Cannot access course {args.course_id}: {info['errors']}")
            sys.exit(1)
        targets = [info]
    else:
        print("Fetching course list from Canvas...")
        all_courses = list_teacher_courses(client)
        targets = [c for c in all_courses
                   if SKIP_PATTERN not in str(c.get("course_code") or "")
                   and SKIP_PATTERN not in str(c.get("name") or "")]
        print(f"  Found {len(all_courses)} teacher courses, "
              f"{len(targets)} after filtering '{SKIP_PATTERN}'")

    started = skipped = errors = 0
    for course in targets:
        cid  = course["id"]
        name = course.get("name") or course.get("course_code") or str(cid)

        if not args.force and cid in existing:
            old = existing[cid]
            print(f"  SKIP   {cid}  {name[:50]}  "
                  f"(job {old['export_id']} -- {old['workflow_state']})")
            skipped += 1
            continue

        if args.dry_run:
            print(f"  [dry-run] Would start: {cid}  {name[:50]}")
            started += 1
            continue

        try:
            resp      = client.post(f"/courses/{cid}/content_exports",
                                    {"export_type": "common_cartridge"})
            export_id = resp["id"]
            workflow  = resp.get("workflow_state", "created")
            job = {
                "course_id": cid, "course_name": name,
                "export_id": export_id, "export_type": "common_cartridge",
                "workflow_state": workflow,
                "started_at": resp.get("created_at",
                                       datetime.now(timezone.utc).isoformat()),
                "filename": None, "downloaded": False, "downloaded_at": None,
            }
            jobs = [j for j in jobs if j["course_id"] != cid]
            jobs.append(job)
            existing[cid] = job
            save_jobs(jobs)
            print(f"  STARTED {cid}  {name[:50]}  -> export {export_id}")
            started += 1
            time.sleep(0.4)
        except Exception as exc:
            print(f"  ERROR   {cid}  {name[:50]}  -> {exc}")
            errors += 1

    print(f"\nDone: {started} started, {skipped} skipped, {errors} errors")
    if not args.dry_run:
        print(f"State: {JOBS_FILE}")
        print("Next:  python3 canvas-backup.py status")
        print("       python3 canvas-backup.py download")


def cmd_download(args, client: CanvasClient) -> None:
    jobs = load_jobs()
    targets = ([j for j in jobs if j["course_id"] == args.course_id]
               if args.course_id else jobs)

    if args.course_id and not targets:
        print(f"ERROR: No job for course {args.course_id} -- run start first.")
        sys.exit(1)

    pending = [j for j in targets if j["workflow_state"] not in ("exported", "failed")]
    if pending:
        print(f"Checking {len(pending)} pending export(s)...")
        for job in pending:
            try:
                resp      = get_export_status(client, job["course_id"], job["export_id"])
                new_state = resp.get("workflow_state", job["workflow_state"])
                if new_state != job["workflow_state"]:
                    print(f"  {job['course_name'][:55]}  {job['workflow_state']} -> {new_state}")
                    job["workflow_state"] = new_state
                    if new_state == "exported":
                        att = resp.get("attachment") or {}
                        job["download_url"] = att.get("url") or resp.get("url", "")
                        if not job.get("filename"):
                            job["filename"] = (att.get("filename") or
                                               _safe_filename(job["course_name"],
                                                              job["course_id"]))
                else:
                    print(f"  {job['course_name'][:55]}  still {new_state}")
                time.sleep(0.3)
            except Exception as exc:
                print(f"  ERROR checking {job['course_name'][:55]}: {exc}")
        save_jobs(jobs)

    to_dl = [j for j in targets
             if j["workflow_state"] == "exported"
             and (not j["downloaded"] or args.force)
             and j.get("download_url")]

    if not to_dl:
        print("Nothing to download (all done, or exports still pending).")
        print_status(targets if args.course_id else jobs)
        return

    out_dir = args.out_dir
    print(f"\nDownloading {len(to_dl)} export(s) to {out_dir}/")
    n_ok = n_fail = 0

    for job in to_dl:
        filename = job.get("filename") or _safe_filename(job["course_name"], job["course_id"])
        dest     = out_dir / filename

        if dest.exists() and not args.force:
            size_mb = dest.stat().st_size / (1 << 20)
            print(f"  SKIP  {job['course_name'][:52]}  ({filename}, {size_mb:.1f} MiB exists)")
            job["downloaded"]   = True
            job["filename"]     = filename
            job.setdefault("downloaded_at",
                           datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            save_jobs(jobs)
            n_ok += 1
            continue

        print(f"  v  {job['course_name'][:55]}\n     -> {filename}")
        try:
            download_export(job["download_url"], dest, client.token)
            mb = dest.stat().st_size / (1 << 20)
            print(f"     OK  {mb:.1f} MiB saved")
            job["downloaded"]    = True
            job["filename"]      = filename
            job["downloaded_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            save_jobs(jobs)
            n_ok += 1
        except Exception as exc:
            print(f"     FAILED: {exc}")
            n_fail += 1
        time.sleep(0.5)

    print(f"\nDone: {n_ok} downloaded, {n_fail} failed.")
    print_status(targets if args.course_id else jobs)


def cmd_status(args, client: CanvasClient) -> None:
    jobs = load_jobs()
    if args.course_id:
        targets = [j for j in jobs if j["course_id"] == args.course_id]
        if not targets:
            print(f"ERROR: No job for course {args.course_id}")
            sys.exit(1)
    else:
        targets = jobs
    print_status(targets)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="canvas-backup.py",
        description="Back up Canvas courses: start exports, then download them.")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p_start = sub.add_parser("start",
        help="Tell Canvas to prepare export files.",
        description="POST to Canvas to begin Common Cartridge exports. "
                    "Re-running is safe -- existing jobs are skipped unless --force.")
    g = p_start.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true",
                   help=f"All courses (skips those containing '{SKIP_PATTERN}')")
    g.add_argument("--course-id", type=int, metavar="ID", help="Single course")
    p_start.add_argument("--dry-run", action="store_true",
                         help="Preview without actually starting exports")
    p_start.add_argument("--force", action="store_true",
                         help="Restart even if a job already exists")

    p_dl = sub.add_parser("download",
        help="Download completed exports to disk.",
        description="Refreshes status from Canvas, then downloads completed exports. "
                    "Already-downloaded files are skipped. Safe to re-run.")
    p_dl.add_argument("--course-id", type=int, metavar="ID",
                      help="Single course only")
    p_dl.add_argument("--out-dir", type=Path, default=DEFAULT_OUT, metavar="PATH",
                      help=f"Save directory (default: {DEFAULT_OUT})")
    p_dl.add_argument("--force", action="store_true",
                      help="Re-download even if already done")

    p_st = sub.add_parser("status", help="Show all job states.")
    p_st.add_argument("--course-id", type=int, metavar="ID",
                      help="Single course only")

    return parser


def main():
    parser = build_parser()
    args   = parser.parse_args()

    if args.command is None:
        print(USAGE)
        sys.exit(0)

    token  = check_token()
    client = CanvasClient(token=token)

    if args.command == "start":
        cmd_start(args, client)
    elif args.command == "download":
        cmd_download(args, client)
    elif args.command == "status":
        cmd_status(args, client)


if __name__ == "__main__":
    main()
