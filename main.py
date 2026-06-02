import os
import re
import sys
import json
import socket as _socket
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Force IPv4 globally ───────────────────────────────────────────────────────
# Render (and some other Linux hosts) advertise IPv6 addresses in DNS but do
# not actually route IPv6 traffic.  Python's socket.getaddrinfo returns AAAA
# records first on dual-stack hosts, so the very first connect() call gets
# OSError errno 101 (ENETUNREACH) and the whole process dies.
# Patching getaddrinfo to always request AF_INET makes every network library
# (requests, httpx/openai, smtplib, etc.) use IPv4 instead.
_orig_gai = _socket.getaddrinfo
def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_gai(host, port, _socket.AF_INET, type, proto, flags)
_socket.getaddrinfo = _ipv4_only

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from job_fetcher import fetch_today_jobs
from matcher import calculate_match_score, generate_cover_letter, tailor_resume, _exceeds_experience, parse_resume_once
from pdf_generator import generate_resume_pdf, generate_cover_letter_pdf
from email_sender import send_email

# ── Config ────────────────────────────────────────────────────────────────────
with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

MATCH_THRESHOLD = config.get("match_threshold", 70)
MAX_JOBS        = config.get("max_jobs", 15)

_contact = " | ".join(p for p in [
    config.get("email",     ""),
    config.get("phone",     ""),
    config.get("location",  ""),
    config.get("linkedin",  ""),
    config.get("portfolio", ""),
] if p)
_name = config.get("name", "")


# ── Helpers ───────────────────────────────────────────────────────────────────
def safe_format_date(ds):
    if not ds:
        return "Unknown"
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(ds[:19], fmt).strftime("%B %d, %Y")
        except ValueError:
            continue
    return "Unknown"


# ── Directories ───────────────────────────────────────────────────────────────
os.makedirs("output/resumes",       exist_ok=True)
os.makedirs("output/cover_letters", exist_ok=True)

# ── Step 1: Fetch jobs (parallel — all sources at once) ───────────────────────
print("Fetching jobs from all sources in parallel...")
jobs = fetch_today_jobs()

if not jobs:
    print("No jobs fetched.")
    sys.exit()

with open("resume.txt", "r", encoding="utf-8") as f:
    resume_text = f.read()

# ── Pre-parse resume ONCE — reused by every tailor_resume() call ──────────────
# Without this, each of the N matched jobs would make an identical parse API
# call, wasting time and money. One call here saves N-1 calls later.
print("Preparing to score jobs...")
_base_resume_json = parse_resume_once(resume_text)

# Guard: warn if ANY critical section (experience, skills, education) is
# missing after parsing.  The old check only tested experience — that missed
# the case where experience is present but skills / education were truncated.
_substantial = len(resume_text) > 200
_missing = _substantial and (
    not _base_resume_json.get("experience") or
    not _base_resume_json.get("skills")     or
    not _base_resume_json.get("education")
)
if _missing:
    print("  Warning: one or more resume sections missing — retrying parse...")
    _base_resume_json = parse_resume_once(resume_text)
    if (
        not _base_resume_json.get("experience") or
        not _base_resume_json.get("skills")     or
        not _base_resume_json.get("education")
    ):
        print("  Warning: resume still incomplete after retry.")
        print("  PDFs will use whatever sections were successfully extracted.")

# ── Step 2: Score all jobs in parallel ────────────────────────────────────────
print(f"Scoring {len(jobs)} jobs in parallel (24 threads)...")

def _score_one(job):
    """Score a single job. Returns (score, score_text, job) or None if below threshold."""
    try:
        # Title-aware experience filter (catches Senior/Lead/etc. in title)
        if _exceeds_experience(job.get("description", ""), job.get("title", "")):
            return None
        score_text = calculate_match_score(job, resume_text)
        m = re.search(r"Score:\s*(\d+)", score_text)
        if not m:
            return None
        score = int(m.group(1))
        if score < MATCH_THRESHOLD:
            return None
        return (score, score_text, job)
    except Exception as e:
        print(f"  Scoring error ({job.get('title','?')}): {e}")
        return None

qualified_jobs = []
with ThreadPoolExecutor(max_workers=24) as executor:
    futures = [executor.submit(_score_one, job) for job in jobs]
    completed = 0
    for future in as_completed(futures):
        completed += 1
        result = future.result()
        if result:
            qualified_jobs.append(result)
        # Progress every 5 jobs
        if completed % 5 == 0 or completed == len(futures):
            print(f"  Scored {completed}/{len(futures)}  |  {len(qualified_jobs)} qualified")

qualified_jobs.sort(key=lambda x: x[0], reverse=True)
qualified_jobs = qualified_jobs[:MAX_JOBS]

if not qualified_jobs:
    print("No jobs met the score threshold.")
    sys.exit()

# ── Step 3: Tailor resume + cover letter for each job in parallel ─────────────
print(f"\nProcessing {len(qualified_jobs)} qualified jobs in parallel (8 threads)...")

def _process_one(args):
    """
    Full pipeline for one job:
      1. Tailor resume (2 GPT calls)
      2. Generate resume PDF
      3. Write cover letter (1 GPT call)
      4. Generate cover letter PDF
    Returns (result_text, [resume_pdf_path, cover_pdf_path])
    """
    score, score_text, job = args
    company_safe      = re.sub(r"[^a-zA-Z0-9_]", "_", job.get("company", "Company"))
    created_formatted = safe_format_date(job.get("created"))

    try:
        # ── Resume tailoring + cover letter run in parallel ──────────
        # tailor_resume (keyword injection, ~3 s) and generate_cover_letter
        # (~2 s) are completely independent — fire both at once and wait
        # for the slower one.  Saves ~2 s per job vs. running sequentially.
        with ThreadPoolExecutor(max_workers=2) as _inner:
            _f_resume = _inner.submit(
                tailor_resume, job, resume_text, base_json=_base_resume_json
            )
            _f_cover  = _inner.submit(generate_cover_letter, job, resume_text)
            resume_json  = _f_resume.result()
            cover_letter = _f_cover.result()

        resume_path = f"output/resumes/{company_safe}_Resume.pdf"
        generate_resume_pdf(resume_path, resume_json, _name, _contact)

        cover_pdf = f"output/cover_letters/{company_safe}_Cover_Letter.pdf"
        generate_cover_letter_pdf(cover_pdf, cover_letter)

        print(f"  [{company_safe}] Done  (score {score})")

        result_text = (
            f"\n{'-'*50}\n"
            f"Match Score : {score}%\n\n"
            f"{job.get('title')} — {job.get('company')}\n"
            f"Location    : {job.get('location')}\n"
            f"Posted On   : {created_formatted}\n"
            f"Apply       : {job.get('redirect_url')}\n\n"
            f"{score_text}\n"
            f"{'-'*50}\n"
        )
        return result_text, [resume_path, cover_pdf]

    except Exception as e:
        print(f"  [{company_safe}] ERROR: {e}")
        return None, []

results     = []
attachments = []

with ThreadPoolExecutor(max_workers=8) as executor:
    futures = [executor.submit(_process_one, args) for args in qualified_jobs]
    for future in as_completed(futures):
        result_text, files = future.result()
        if result_text:
            results.append(result_text)
            attachments.extend(files)

if not results:
    print("All jobs failed during processing.")
    sys.exit()

# ── Step 4: Send email ────────────────────────────────────────────────────────
print(f"\nSending email with {len(attachments)} attachments...")
try:
    send_email("\n".join(results), attachments)
    print("Email sent successfully.")
except Exception as _mail_err:
    print(f"Email failed: {_mail_err}")
    print("Jobs were processed — PDFs generated. Check your email settings.")
print("Process finished successfully.")
