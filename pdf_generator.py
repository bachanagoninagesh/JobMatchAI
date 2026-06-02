"""
Resume + Cover Letter PDF generator — single-page auto-fill.

generate_resume_pdf() binary-searches (8 iterations, ±0.004 precision)
the largest font/spacing scale at which all content fits on one letter
page, then writes the final PDF at that scale.

Result: every resume fills the page top-to-bottom with no wasted space
and never spills onto a second page.
"""

import io
import re

from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, HRFlowable,
    Table, TableStyle, KeepInFrame,
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.pagesizes import letter
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.lib import colors

BLUE = colors.HexColor("#1f618d")
GRAY = colors.HexColor("#1a1a1a")

_PAGE_W, _PAGE_H = letter


# ── XML helper ────────────────────────────────────────────────────────────────

def _xe(text):
    """XML-escape a value for use inside a ReportLab Paragraph."""
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _sp(pts):
    """Spacer clamped to a minimum of 1 pt."""
    return Spacer(1, max(1, int(pts)))


# ── Scaled style factory ──────────────────────────────────────────────────────

def _build_styles(scale):
    """
    Return a dict of ParagraphStyles scaled by *scale* (1.0 = normal).

    Font sizes are CAPPED at a professional maximum so a short resume never
    ends up with comically large text.  Spacers (in _build_story) use the
    raw scale with no cap, so they keep growing and distribute content
    evenly across the full page for any amount of content.

    Result:
      Dense resume  → scale ~0.76,  font ~8 pt,  tight spacing
      Medium resume → scale ~1.10,  font ~11 pt, comfortable spacing
      Short resume  → scale ~2.00+, font capped at 12 pt, generous spacing
    """
    # Font size: grows with scale but hard-capped so text stays professional
    fs = min(12.0, 10.5 * scale)
    lh = fs * 1.42

    def S(tag, **kw):
        return ParagraphStyle(f"{tag}_{scale:.4f}", **kw)

    return {
        "_scale": scale,

        "name": S("name",
            fontName="Helvetica-Bold",
            fontSize=min(22, max(14, 18 * scale)),   # 14 – 22 pt
            textColor=BLUE, alignment=1,
            spaceAfter=int(2 * scale)),

        "contact": S("contact",
            fontName="Helvetica",
            fontSize=min(10.5, max(7.5, 9.5 * scale)),  # 7.5 – 10.5 pt
            textColor=GRAY, alignment=1, spaceAfter=0),

        "section": S("section",
            fontName="Helvetica-Bold",
            fontSize=min(12, max(8, 10.5 * scale)),  # 8 – 12 pt
            textColor=BLUE,
            spaceBefore=max(1, int(6 * scale)),   # uncapped — grows freely
            spaceAfter=max(1, int(1 * scale))),

        "normal": S("normal",
            fontName="Helvetica", fontSize=fs,
            textColor=GRAY, leading=lh),

        "italic": S("italic",
            fontName="Helvetica-Oblique",
            fontSize=min(11.5, max(7, 10.0 * scale)),
            textColor=GRAY, leading=lh),

        "bold": S("bold",
            fontName="Helvetica-Bold", fontSize=fs,
            textColor=GRAY, leading=lh),

        "bullet": S("bullet",
            fontName="Helvetica", fontSize=fs,
            textColor=GRAY, leading=lh,
            leftIndent=int(10 * scale)),

        "skill": S("skill",
            fontName="Helvetica", fontSize=fs,
            textColor=GRAY, leading=lh),

        "cert": S("cert",
            fontName="Helvetica",
            fontSize=min(11, max(7, 9.5 * scale)),
            textColor=GRAY, leading=lh * 0.95, leftIndent=4),
    }


# ── Low-level helpers ─────────────────────────────────────────────────────────

def _section_header(title, styles):
    sc = styles["_scale"]
    return [
        Paragraph(title, styles["section"]),
        HRFlowable(width="100%", thickness=0.6, color=BLUE,
                   spaceAfter=max(1, int(2 * sc))),
    ]


def _date_row(left, right, frame_w, styles):
    """Bold left label + italic right date on the same line."""
    sc = styles["_scale"]
    lp = Paragraph(f'<b>{_xe(left)}</b>', styles["normal"])
    rp = Paragraph(f'<i>{_xe(right)}</i>', styles["italic"])
    tbl = Table([[lp, rp]], colWidths=[frame_w * 0.70, frame_w * 0.30])
    tbl.setStyle(TableStyle([
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",   (0, 0), (-1, -1), 0),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), max(1, int(sc))),
        ("ALIGN",         (1, 0), (1, -1), "RIGHT"),
    ]))
    return [tbl]


def _bullets(items, styles):
    sc  = styles["_scale"]
    out = []
    for b in items:
        b = b.strip()
        if not b:
            continue
        out.append(Paragraph(f"• {_xe(b)}", styles["bullet"]))
        out.append(_sp(sc))
    return out


# ── Section renderers (return [] when section data is absent) ─────────────────

def _render_summary(summary, styles):
    if not summary or not summary.strip():
        return []
    sc  = styles["_scale"]
    out = _section_header("PROFESSIONAL SUMMARY", styles)
    out.append(Paragraph(_xe(summary.strip()), styles["normal"]))
    out.append(_sp(3 * sc))
    return out


def _render_experience(experience, frame_w, styles):
    if not experience:
        return []
    sc  = styles["_scale"]
    out = _section_header("PROFESSIONAL EXPERIENCE", styles)

    for exp in experience:
        title    = exp.get("title",    "")
        company  = exp.get("company",  "")
        location = exp.get("location", "")
        dates    = exp.get("dates",    "")

        # Line 1: Company, Location (bold) | Dates (italic right)
        # Company is the primary heading — standard professional resume format
        company_line = ", ".join(p for p in [company, location] if p)
        out += _date_row(company_line, dates, frame_w, styles)

        # Line 2: Job Title (italic, sits directly below company line)
        if title:
            out.append(Paragraph(f"<i>{_xe(title)}</i>", styles["italic"]))
        out.append(_sp(2 * sc))

        for proj in exp.get("projects", []):
            name    = proj.get("name")
            bullets = proj.get("bullets", [])
            if name:
                out.append(Paragraph(f"<i>{_xe(name)}</i>", styles["italic"]))
                out.append(_sp(sc))
            out += _bullets(bullets, styles)
            out.append(_sp(1.5 * sc))

    return out


def _render_skills(skills, usable_w, styles):
    if not skills:
        return []
    sc    = styles["_scale"]
    out   = _section_header("TECHNICAL SKILLS", styles)
    col_w = usable_w / 2

    for i in range(0, len(skills), 2):
        pair = skills[i:i + 2]

        # Lone last entry → span full width so it doesn't look half-empty
        if len(pair) == 1:
            sk    = pair[0]
            cat   = _xe(sk.get("category", ""))
            items = ", ".join(_xe(it) for it in sk.get("items", []))
            out.append(Paragraph(
                f'<font name="Helvetica-Bold">{cat}:</font> {items}',
                styles["skill"],
            ))
            out.append(_sp(max(1, int(2.5 * sc))))
            continue

        cells = []
        for sk in pair:
            cat   = _xe(sk.get("category", ""))
            items = ", ".join(_xe(it) for it in sk.get("items", []))
            cells.append(Paragraph(
                f'<font name="Helvetica-Bold">{cat}:</font> {items}',
                styles["skill"],
            ))
        tbl = Table([cells], colWidths=[col_w, col_w])
        tbl.setStyle(TableStyle([
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING",   (0, 0), (-1, -1), 0),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
            ("TOPPADDING",    (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), max(1, int(2.5 * sc))),
        ]))
        out.append(tbl)

    return out


def _render_education(education, frame_w, styles):
    if not education:
        return []
    sc  = styles["_scale"]
    # Cap at 2 entries — show only the 2 most recently listed degrees.
    # Resumes are parsed most-recent-first, so [:2] gives the newest two.
    education = education[:2]
    out = _section_header("EDUCATION", styles)

    for edu in education:
        degree   = edu.get("degree",   "")
        school   = edu.get("school",   "")
        location = edu.get("location", "")
        gpa      = edu.get("gpa")
        dates    = edu.get("dates",    "")

        # Line 1: degree (bold-left) | dates (italic-right)
        out += _date_row(degree, dates, frame_w, styles)

        # Line 2: school | location | GPA
        parts = [p for p in [school, location, f"GPA: {gpa}" if gpa else ""] if p]
        if parts:
            out.append(Paragraph(
                f'<i>{" | ".join(_xe(p) for p in parts)}</i>',
                styles["italic"],
            ))
        out.append(_sp(4 * sc))

    return out


def _render_certifications(certs, usable_w, styles):
    """
    Renders certifications in a dynamic horizontal layout.

    Column count is chosen automatically by measuring the actual text width
    of the longest cert name at the current font size:
      - Tries 3 per row first  (e.g. 6 certs → 2 lines)
      - Falls back to 2 per row if names are too long to fit in 3 columns
      - Falls back to 1 per row for very long names

    Works correctly for any resume: 2 certs or 12 certs, short names or long.
    """
    if not certs:
        return []
    sc       = styles["_scale"]
    cert_fs  = styles["cert"].fontSize          # actual pt size at current scale
    out      = _section_header("CERTIFICATIONS", styles)

    # ── Measure the widest cert label (including bullet prefix) ──────
    max_label_w = max(
        stringWidth("• " + c, "Helvetica", cert_fs)
        for c in certs
    )
    cell_padding = 10   # left(2) + right(2) + breathing room(6) per cell

    # ── Pick column count: 3 → 2 → 1 ────────────────────────────────
    for ncols in (3, 2, 1):
        if (max_label_w + cell_padding) * ncols <= usable_w:
            break   # this column count fits

    col_w = usable_w / ncols

    # ── Render rows ───────────────────────────────────────────────────
    for j in range(0, len(certs), ncols):
        group = certs[j:j + ncols]
        cells = [Paragraph("• " + _xe(c), styles["cert"]) for c in group]
        # Pad the last row to keep the table rectangular
        while len(cells) < ncols:
            cells.append(Paragraph("", styles["cert"]))
        tbl = Table([cells], colWidths=[col_w] * ncols)
        tbl.setStyle(TableStyle([
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING",   (0, 0), (-1, -1), 2),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 2),
            ("TOPPADDING",    (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), max(1, int(2 * sc))),
        ]))
        out.append(tbl)

    out.append(_sp(2 * sc))
    return out


def _render_role_grid(title, items, role_key, usable_w, styles):
    """Generic two-column grid: role/name (left) | dates (right)."""
    if not items:
        return []
    sc    = styles["_scale"]
    out   = _section_header(title, styles)
    col_l = usable_w * 0.72
    col_r = usable_w * 0.28

    for item in items:
        role  = item.get(role_key, "")
        dates = item.get("dates",   "")
        lp = Paragraph("• " + _xe(role), styles["bullet"])
        rp = Paragraph(f"<i>{_xe(dates)}</i>" if dates else "", styles["italic"])
        tbl = Table([[lp, rp]], colWidths=[col_l, col_r])
        tbl.setStyle(TableStyle([
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING",   (0, 0), (-1, -1), 4),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
            ("TOPPADDING",    (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), max(1, int(2 * sc))),
            ("ALIGN",         (1, 0), (1, -1), "RIGHT"),
        ]))
        out.append(tbl)

    return out


def _render_achievements(achievements, styles):
    if not achievements:
        return []
    out = _section_header("ACHIEVEMENTS", styles)
    out += _bullets(achievements, styles)
    return out


# ── AI output normalizer ─────────────────────────────────────────────────────

def _normalize_resume_json(raw):
    """
    Sanitize and normalize every field produced by the AI before any
    renderer touches it.  The AI occasionally returns:
      • a dict where a list is expected (skills, experience, education …)
      • a string where a list of strings is expected (certifications …)
      • missing keys / None values

    After this function every field has the exact Python type the renderers
    expect, so no renderer can ever crash on unexpected AI output.
    """
    if not isinstance(raw, dict):
        raw = {}

    out = {}

    # ── summary — hard cap: 4 sentences max ───────────────────────────
    summary = str(raw.get("summary") or "")
    # Split on sentence boundaries and keep first 4
    sentences = re.split(r'(?<=[.!?])\s+', summary.strip())
    out["summary"] = " ".join(sentences[:4])

    # ── experience ────────────────────────────────────────────────────
    exp_raw = raw.get("experience", [])
    if isinstance(exp_raw, dict):
        exp_raw = [exp_raw]
    if not isinstance(exp_raw, list):
        exp_raw = []

    experience = []
    for e in exp_raw:
        if not isinstance(e, dict):
            continue
        projs_raw = e.get("projects", [])
        if isinstance(projs_raw, dict):
            projs_raw = [projs_raw]
        if not isinstance(projs_raw, list):
            projs_raw = []

        projects = []
        for p in projs_raw:
            if isinstance(p, str):
                projects.append({"name": None, "bullets": [p]})
                continue
            if not isinstance(p, dict):
                continue
            bullets = p.get("bullets", [])
            if isinstance(bullets, str):
                bullets = [bullets]
            elif not isinstance(bullets, list):
                bullets = []
            projects.append({
                "name":    p.get("name") or None,
                # Hard cap: 5 bullets per project — keeps font readable
                "bullets": [str(b) for b in bullets if b][:5],
            })

        experience.append({
            "title":    str(e.get("title",    "") or ""),
            "company":  str(e.get("company",  "") or ""),
            "location": str(e.get("location", "") or ""),
            "dates":    str(e.get("dates",    "") or ""),
            "projects": projects,
        })
    # Hard cap: 3 most recent jobs only — older jobs pushed off the page
    out["experience"] = experience[:3]

    # ── skills ────────────────────────────────────────────────────────
    # AI sometimes returns {"SQL & DBs": ["T-SQL", ...], ...} instead of
    # [{"category": "SQL & DBs", "items": ["T-SQL", ...]}, ...]
    sk_raw = raw.get("skills", [])
    if isinstance(sk_raw, dict):
        sk_raw = [{"category": k, "items": v} for k, v in sk_raw.items()]
    if not isinstance(sk_raw, list):
        sk_raw = []

    skills = []
    for sk in sk_raw:
        if isinstance(sk, str):
            skills.append({"category": sk, "items": []})
            continue
        if not isinstance(sk, dict):
            continue
        items = sk.get("items", [])
        if isinstance(items, str):
            items = [items]
        elif not isinstance(items, list):
            items = []
        # Fix common AI typos in skill category names
        category = str(sk.get("category", "") or "")
        _typo_fixes = {
            "Version Contrology": "Version Control",
            "Version Contol":     "Version Control",
            "Contrology":         "Control",
            "Ides":               "IDEs",
            "ides":               "IDEs",
        }
        for wrong, right in _typo_fixes.items():
            category = category.replace(wrong, right)

        skills.append({
            "category": category,
            "items":    [str(i) for i in items if i],
        })
    out["skills"] = skills

    # ── education ─────────────────────────────────────────────────────
    edu_raw = raw.get("education", [])
    if isinstance(edu_raw, dict):
        edu_raw = [edu_raw]
    if not isinstance(edu_raw, list):
        edu_raw = []

    education = []
    for edu in edu_raw:
        if not isinstance(edu, dict):
            continue
        education.append({
            "degree":   str(edu.get("degree",   "") or ""),
            "school":   str(edu.get("school",   "") or ""),
            "location": str(edu.get("location", "") or ""),
            "gpa":      edu.get("gpa") or None,
            "dates":    str(edu.get("dates",    "") or ""),
        })
    out["education"] = education

    # ── certifications ────────────────────────────────────────────────
    cert_raw = raw.get("certifications", [])
    if isinstance(cert_raw, str):
        cert_raw = [cert_raw]
    elif isinstance(cert_raw, dict):
        cert_raw = list(cert_raw.values())
    if not isinstance(cert_raw, list):
        cert_raw = []
    out["certifications"] = [str(c) for c in cert_raw if c]

    # ── leadership ────────────────────────────────────────────────────
    lead_raw = raw.get("leadership", [])
    if isinstance(lead_raw, dict):
        lead_raw = [lead_raw]
    if not isinstance(lead_raw, list):
        lead_raw = []
    out["leadership"] = [l for l in lead_raw if isinstance(l, dict)]

    # ── activities ────────────────────────────────────────────────────
    act_raw = raw.get("activities", [])
    if isinstance(act_raw, dict):
        act_raw = [act_raw]
    if not isinstance(act_raw, list):
        act_raw = []
    out["activities"] = [a for a in act_raw if isinstance(a, dict)]

    # ── achievements ──────────────────────────────────────────────────
    ach_raw = raw.get("achievements", [])
    if isinstance(ach_raw, str):
        ach_raw = [ach_raw]
    if not isinstance(ach_raw, list):
        ach_raw = []
    out["achievements"] = [str(a) for a in ach_raw if a]

    return out


# ── Story assembler (rebuilds cheaply at any scale) ───────────────────────────

def _build_story(resume_json, name, contact, scale, usable_w):
    styles  = _build_styles(scale)
    sc      = scale
    frame_w = usable_w

    story = []

    # ── Header ───────────────────────────────────────────────────────
    # Name always displayed in ALL CAPS on the resume regardless of how it was entered
    story.append(Paragraph(_xe((name or "").upper()), styles["name"]))
    story.append(_sp(7 * sc))
    story.append(Paragraph(_xe(contact), styles["contact"]))
    story.append(_sp(5 * sc))
    story.append(HRFlowable(width="100%", thickness=0.8, color=BLUE))
    story.append(_sp(3 * sc))

    # ── Sections ─────────────────────────────────────────────────────
    story += _render_summary(   resume_json.get("summary",        ""),  styles)
    story += _render_experience(resume_json.get("experience",     []),  frame_w, styles)
    story += _render_skills(    resume_json.get("skills",         []),  usable_w, styles)
    story += _render_education( resume_json.get("education",      []),  frame_w, styles)
    story += _render_certifications(
                                resume_json.get("certifications", []),  usable_w, styles)
    story += _render_role_grid("LEADERSHIP",
                                resume_json.get("leadership",     []),
                                "role",  usable_w, styles)
    story += _render_role_grid("ACTIVITIES/CLUBS",
                                resume_json.get("activities",     []),
                                "name",  usable_w, styles)
    story += _render_achievements(
                                resume_json.get("achievements",   []),  styles)

    return story


# ── Public API ────────────────────────────────────────────────────────────────

def generate_resume_pdf(file_path, resume_json, name, contact):
    """
    Build a single-page, page-filling resume PDF.

    Algorithm
    ---------
    1. Binary-search (8 iterations ≈ ±0.004 precision) for the largest
       font/spacing scale at which all content still fits on one letter page.
    2. Build the final PDF at that scale — content always fills the page.
    3. Wrap in KeepInFrame(mode='shrink') as a safety net for edge cases.

    Parameters
    ----------
    file_path   : output path (.pdf)
    resume_json : dict produced by matcher.tailor_resume()
    name        : candidate's full name  (from config)
    contact     : pre-formatted contact line (from config)
    """
    # Sanitize AI output first — guarantees every field has the right type
    resume_json = _normalize_resume_json(resume_json)

    L = R = 40
    T = B = 28
    usable_w = _PAGE_W - L - R
    usable_h = _PAGE_H - T - B

    # ── Page-count helper (builds to a throw-away buffer) ─────────────
    def _count_pages(scale):
        story = _build_story(resume_json, name, contact, scale, usable_w)
        buf   = io.BytesIO()
        n     = [0]

        def _cp(canvas, doc):
            n[0] += 1

        SimpleDocTemplate(
            buf, pagesize=letter,
            leftMargin=L, rightMargin=R, topMargin=T, bottomMargin=B,
        ).build(story, onFirstPage=_cp, onLaterPages=_cp)
        return n[0]

    # ── Binary search: largest scale that still fits on 1 page ────────
    #    Range 0.55 – 1.42 covers everything from a very dense resume
    #    (many jobs, many bullets) to a very short one (entry-level).
    lo, hi = 0.76, 1.42
    best   = lo         # fallback: min readable scale (10.5 × 0.76 ≈ 8pt body font)

    for _ in range(6):  # 6 iterations → precision ≈ (1.42-0.55)/64 ≈ 0.013 (imperceptible)
        mid = (lo + hi) / 2
        if _count_pages(mid) <= 1:
            best = mid
            lo   = mid   # fits → try a larger scale
        else:
            hi   = mid   # overflows → try a smaller scale

    # ── Final build wrapped in KeepInFrame (safety net) ───────────────
    story = _build_story(resume_json, name, contact, best, usable_w)
    kif   = KeepInFrame(usable_w, usable_h, story, mode="shrink")

    SimpleDocTemplate(
        file_path, pagesize=letter,
        leftMargin=L, rightMargin=R, topMargin=T, bottomMargin=B,
    ).build([kif])


# ── Cover Letter PDF (unchanged) ──────────────────────────────────────────────

def generate_cover_letter_pdf(file_path, content):
    """Render a plain-text cover letter into a clean single-page PDF."""
    doc = SimpleDocTemplate(
        file_path, pagesize=letter,
        leftMargin=72, rightMargin=72,
        topMargin=60, bottomMargin=60,
    )

    body_style = ParagraphStyle("CLBody",
        fontName="Helvetica", fontSize=10.5,
        textColor=GRAY, leading=16, spaceAfter=14)

    sig_style = ParagraphStyle("CLSig",
        fontName="Helvetica", fontSize=10.5,
        textColor=GRAY, leading=15, spaceAfter=3)

    story = []
    story.append(HRFlowable(width="100%", thickness=3, color=BLUE, spaceAfter=18))

    blocks = re.split(r"\n{2,}", content.strip())
    total  = len(blocks)

    for idx, block in enumerate(blocks):
        block = block.strip()
        if not block:
            continue
        escaped = (block
                   .replace("&", "&amp;")
                   .replace("<", "&lt;")
                   .replace(">", "&gt;")
                   .replace("\n", "<br/>"))
        style = sig_style if idx >= total - 2 else body_style
        story.append(Paragraph(escaped, style))

    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=0.6, color=BLUE))

    doc.build(story)
