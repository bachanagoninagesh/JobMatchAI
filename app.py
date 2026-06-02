import os
import re
import json
import time
import secrets
import subprocess
import sys
import socket as _socket
import threading
from flask import (Flask, request, jsonify, render_template,
                   make_response, session, redirect, url_for)

# ── Force IPv4 globally ───────────────────────────────────────────────────────
# Render (and some other Linux hosts) have IPv6 in DNS but no IPv6 routing.
# Python tries IPv6 first → OSError errno 101 (ENETUNREACH).
# This patch makes every library (requests, smtplib, httpx/openai) use IPv4.
_orig_gai = _socket.getaddrinfo
def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_gai(host, port, _socket.AF_INET, type, proto, flags)
_socket.getaddrinfo = _ipv4_only

app = Flask(__name__)
# Secret key for session cookies — auto-generated at startup so it's
# unique per deployment without needing a separate env var.
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

pipeline_status = {"running": False, "output": [], "done": False, "error": None}
_lock = threading.Lock()
BASE = os.path.dirname(os.path.abspath(__file__))

# Path where the last successfully extracted resume text is cached.
_LAST_EXTRACT = os.path.join(BASE, "last_extract.txt")

# Site password — set SITE_PASSWORD in .env / Render env vars.
# If not set, the site is open (useful for local development).
_SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "")


def _is_authenticated():
    """Return True if the visitor has entered the correct site password."""
    if not _SITE_PASSWORD:
        return True   # no password configured → open access (local dev)
    return session.get("auth") == _SITE_PASSWORD


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        if request.form.get("password") == _SITE_PASSWORD:
            session["auth"] = _SITE_PASSWORD
            return redirect(url_for("index"))
        error = "Wrong password — try again."
    # Inline login page — no extra template file needed
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>JobMatchAI — Login</title>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:#0f172a;display:flex;align-items:center;
         justify-content:center;min-height:100vh}}
    .card{{background:#1e293b;border-radius:16px;padding:40px 36px;
           width:100%;max-width:360px;box-shadow:0 20px 60px rgba(0,0,0,.5)}}
    h1{{color:#38bdf8;font-size:1.5rem;margin-bottom:6px;text-align:center}}
    p{{color:#94a3b8;font-size:.85rem;text-align:center;margin-bottom:28px}}
    input{{width:100%;padding:12px 14px;border-radius:8px;border:1.5px solid #334155;
           background:#0f172a;color:#f1f5f9;font-size:1rem;outline:none;
           transition:border .2s}}
    input:focus{{border-color:#38bdf8}}
    button{{margin-top:16px;width:100%;padding:13px;border-radius:8px;border:none;
            background:linear-gradient(135deg,#2563eb,#0ea5e9);color:#fff;
            font-size:1rem;font-weight:600;cursor:pointer;transition:opacity .2s}}
    button:hover{{opacity:.88}}
    .err{{color:#f87171;font-size:.84rem;margin-top:12px;text-align:center}}
  </style>
</head>
<body>
  <div class="card">
    <h1>🤖 JobMatchAI</h1>
    <p>Enter the site password to continue</p>
    <form method="post">
      <input type="password" name="password" placeholder="Password" autofocus required>
      <button type="submit">Enter</button>
    </form>
    {'<p class="err">' + error + '</p>' if error else ''}
  </div>
</body>
</html>"""


@app.route("/")
def index():
    if not _is_authenticated():
        return redirect(url_for("login"))
    resp = make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"]        = "no-cache"
    resp.headers["Expires"]       = "0"
    return resp


@app.route("/api/run", methods=["POST"])
def run_pipeline():
    if not _is_authenticated():
        return jsonify({"error": "Not authenticated. Please log in first."}), 401

    global pipeline_status

    with _lock:
        if pipeline_status["running"]:
            return jsonify({"error": "A pipeline is already running. Please wait."}), 409

        data = request.get_json(force=True, silent=True) or {}

        # Validate required fields
        if not data.get("name") or not data.get("email"):
            return jsonify({"error": "Name and email are required."}), 400
        if not data.get("target_roles"):
            return jsonify({"error": "At least one target role is required."}), 400

        resume_txt = data.get("resume_text", "").strip()
        if not resume_txt:
            return jsonify({"error": "Resume text is required."}), 400

        # ── Short-text auto-recovery ─────────────────────────────────────────
        # PDF.js (browser) silently truncates malformed PDFs to ~1 000 chars.
        # When that happens the browser sends us a stub instead of the full
        # resume, which produces a PDF with only the summary section.
        #
        # Fix: /api/parse-resume caches every successful server-side extraction
        # to last_extract.txt.  If the text we just received looks truncated
        # (< 1 500 chars) AND a recent cache file exists, swap in the full text
        # automatically — the user never sees an error.
        if len(resume_txt) < 1500:
            recovered = False
            try:
                cache_age = time.time() - os.path.getmtime(_LAST_EXTRACT)
                if cache_age < 1800:          # cache valid for 30 minutes
                    with open(_LAST_EXTRACT, "r", encoding="utf-8") as f:
                        cached = f.read().strip()
                    if len(cached) > len(resume_txt):
                        resume_txt = cached
                        recovered  = True
            except Exception:
                pass

            if not recovered:
                return jsonify({
                    "error": (
                        f"Resume text appears incomplete "
                        f"({len(resume_txt)} characters — a full resume is usually 2 000+).\n"
                        "Please re-upload your resume file or paste the full text."
                    )
                }), 400

        config = {
            "name":                 data.get("name", "").strip(),
            "email":                data.get("email", "").strip(),
            "receiver_email":       data.get("receiver_email", "").strip() or data.get("email", "").strip(),
            "phone":                data.get("phone", "").strip(),
            "location":             data.get("location", "").strip().title(),
            "linkedin":             data.get("linkedin", "").strip(),
            "portfolio":            data.get("portfolio", "").strip(),
            "target_roles":         data.get("target_roles", []),
            "max_years_experience": int(data.get("max_years_experience", 0)),
            "match_threshold":      int(data.get("match_threshold", 70)),
            "max_jobs":             int(data.get("max_jobs", 15)),
            "job_sources":          data.get("job_sources", {
                "adzuna": True, "remotive": True, "arbeitnow": True,
                "greenhouse": True, "themuse": True, "remoteok": True,
                "jobicy": True, "usajobs": True, "jooble": True, "findwork": True,
                "indeed": True, "lever": True, "weworkremotely": True,
                "workingnomads": True, "hnhiring": True,
            }),
            "greenhouse_companies": data.get("greenhouse_companies", []),
            "lever_companies":      data.get("lever_companies", []),
        }

        with open(os.path.join(BASE, "config.json"), "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

        with open(os.path.join(BASE, "resume.txt"), "w", encoding="utf-8") as f:
            f.write(resume_txt)

        pipeline_status = {"running": True, "output": [], "done": False, "error": None}

    def _run():
        global pipeline_status
        try:
            proc = subprocess.Popen(
                [sys.executable, "-W", "ignore", "main.py"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=BASE,
            )
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                if "site-packages" in line or "warnings.warn" in line:
                    continue
                if line.strip().startswith("C:\\") or line.strip().startswith("/usr/"):
                    continue
                pipeline_status["output"].append(line)
            proc.wait()
            if proc.returncode != 0:
                pipeline_status["error"] = f"Process exited with code {proc.returncode}"
        except Exception as exc:
            pipeline_status["error"] = str(exc)
        finally:
            pipeline_status["running"] = False
            pipeline_status["done"] = True

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"message": "Pipeline started"})


@app.route("/api/parse-resume", methods=["POST"])
def parse_resume():
    if not _is_authenticated():
        return jsonify({"error": "Not authenticated."}), 401
    """
    Extract plain text from an uploaded resume file.
    Supported: .txt  .pdf  .docx  .doc

    After every successful extraction the text is cached to last_extract.txt
    so that /api/run can recover automatically when the browser sends
    truncated text (e.g. PDF.js failing on a malformed PDF).
    """
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file provided"}), 400

    fname = file.filename.lower()

    def _save_cache(text):
        """Persist extracted text so /api/run can use it for auto-recovery."""
        try:
            with open(_LAST_EXTRACT, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass

    # ── Plain text ────────────────────────────────────────────────────────
    if fname.endswith(".txt"):
        try:
            text = file.read().decode("utf-8", errors="replace")
            _save_cache(text)
            return jsonify({"text": text})
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    # ── PDF ───────────────────────────────────────────────────────────────
    if fname.endswith(".pdf"):
        file_bytes = file.read()

        def _clean_pdf_text(raw):
            """Normalise text from PDF: collapse tabs/multiple spaces."""
            raw = raw.replace("\t", " ")
            raw = re.sub(r"[ ]{2,}", " ", raw)
            lines = [l.rstrip() for l in raw.splitlines()]
            return "\n".join(lines).strip()

        # Method 1: pypdf with strict=False
        # strict=False is essential: many resume PDFs have xref errors
        # ("wrong pointing object") that cause PDF.js to stop early and
        # return only ~1 000 chars.  pypdf tolerates those errors and
        # recovers the full text from every page.
        try:
            import pypdf, io
            reader = pypdf.PdfReader(io.BytesIO(file_bytes), strict=False)
            pages  = [p.extract_text() or "" for p in reader.pages]
            text   = _clean_pdf_text("\n\n".join(pages))
            if text:
                _save_cache(text)
                return jsonify({"text": text})
        except Exception:
            pass

        # Method 2: pdfplumber (different engine — fallback for edge cases)
        try:
            import pdfplumber, io
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                pages = [p.extract_text() or "" for p in pdf.pages]
            text = _clean_pdf_text("\n\n".join(pages))
            if text:
                _save_cache(text)
                return jsonify({"text": text})
        except Exception:
            pass

        return jsonify({
            "error": "No text could be extracted — the PDF may be scanned/image-based. "
                     "Please paste the text manually."
        }), 400

    # ── DOCX (Word 2007+) ─────────────────────────────────────────────────
    if fname.endswith(".docx"):
        try:
            from docx import Document
            from docx.oxml.ns import qn
            import io
            raw = file.read()
            doc = Document(io.BytesIO(raw))
            lines = []
            seen  = set()

            def _add(t):
                t = t.strip()
                if t and t not in seen:
                    lines.append(t)
                    seen.add(t)

            # Pass 1: top-level paragraphs
            for p in doc.paragraphs:
                _add(p.text)

            # Pass 2: table cells (many resume templates put content here)
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        for para in cell.paragraphs:
                            _add(para.text)

            # Pass 3: floating text boxes (w:txbxContent)
            # Modern Word resume templates often put Experience/Skills/Education
            # inside floating text boxes which python-docx skips by default.
            try:
                for txbx in doc.element.body.iter(qn("w:txbxContent")):
                    for para in txbx.iter(qn("w:p")):
                        t = "".join(run.text for run in para.iter(qn("w:t")))
                        _add(t)
            except Exception:
                pass

            # Pass 4: headers / footers
            try:
                for section in doc.sections:
                    for hdr in (section.header, section.footer):
                        if hdr and not hdr.is_linked_to_previous:
                            for p in hdr.paragraphs:
                                _add(p.text)
            except Exception:
                pass

            text = "\n".join(lines).strip()
            if not text:
                return jsonify({"error": "No text found in the DOCX file."}), 400
            _save_cache(text)
            return jsonify({"text": text})
        except ImportError:
            return jsonify({
                "error": "python-docx is not installed. Run: pip install python-docx"
            }), 500
        except Exception as e:
            return jsonify({"error": f"DOCX parsing error: {e}"}), 400

    # ── DOC (old binary Word format) ──────────────────────────────────────
    if fname.endswith(".doc"):
        file_bytes = file.read()

        # Method 1: Word COM automation (Windows + MS Word installed)
        try:
            import win32com.client
            import tempfile, pythoncom
            pythoncom.CoInitialize()
            with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as tf:
                tf.write(file_bytes)
                tmp_path = tf.name
            try:
                word = win32com.client.Dispatch("Word.Application")
                word.Visible = False
                word.DisplayAlerts = False
                doc  = word.Documents.Open(os.path.abspath(tmp_path),
                                           ReadOnly=True, AddToRecentFiles=False)
                text = doc.Content.Text.strip()
                doc.Close(False)
                word.Quit()
            finally:
                try: os.unlink(tmp_path)
                except: pass
                try: pythoncom.CoUninitialize()
                except: pass
            if text:
                _save_cache(text)
                return jsonify({"text": text})
        except ImportError:
            pass
        except Exception:
            pass

        # Method 2: python-docx (OOXML .doc files)
        try:
            from docx import Document
            import io
            doc   = Document(io.BytesIO(file_bytes))
            lines = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            seen  = set(lines)
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        for para in cell.paragraphs:
                            t = para.text.strip()
                            if t and t not in seen:
                                lines.append(t)
                                seen.add(t)
            text = "\n".join(lines).strip()
            if text:
                _save_cache(text)
                return jsonify({"text": text})
        except Exception:
            pass

        # Method 3: raw UTF-16-LE binary salvage
        try:
            raw_text = file_bytes.decode("utf-16-le", errors="ignore")
            salvaged = re.sub(r"[^\x20-\x7E\xA0-\xFF\n\t]+", " ", raw_text)
            salvaged = re.sub(r" {3,}", " ", salvaged).strip()
            if len(salvaged) > 200:
                _save_cache(salvaged)
                return jsonify({"text": salvaged})
        except Exception:
            pass

        return jsonify({
            "error": (
                "Could not extract text from this .doc file automatically.\n"
                "Options:\n"
                "1. Open in Microsoft Word → Save As .docx → re-upload.\n"
                "2. Open in Google Docs → Download as .docx → re-upload.\n"
                "3. Copy and paste the text directly into the resume box."
            )
        }), 400

    return jsonify({
        "error": "Unsupported format. Please upload a .txt, .pdf, .docx, or .doc file."
    }), 400


@app.route("/api/status")
def get_status():
    if not _is_authenticated():
        return jsonify({"error": "Not authenticated."}), 401
    return jsonify(pipeline_status)


if __name__ == "__main__":
    print("JobMatchAI for Everyone — starting at http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
