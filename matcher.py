import os
import re
import json
import textwrap
from datetime import date
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Each user's config lives in their own USER_DIR set by the parent process.
_USER_DIR = os.environ.get("USER_DIR", os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(_USER_DIR, "config.json"), "r", encoding="utf-8") as f:
    _cfg = json.load(f)

_name      = _cfg.get("name", "")
_email     = _cfg.get("email", "")
_phone     = _cfg.get("phone", "")
_location  = _cfg.get("location", "")
_linkedin  = _cfg.get("linkedin", "")
_portfolio = _cfg.get("portfolio", "")
_max_years = _cfg.get("max_years_experience", 0)   # 0 = no filter

# Contact line used in cover letters and resume headers
_contact = " | ".join(p for p in [_email, _phone, _location, _linkedin, _portfolio] if p)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _exceeds_experience(description, title=""):
    """
    Return True if the job requires more experience than _max_years.

    Two-stage check:
    1. Title keywords — "Senior", "Staff", "Principal" etc. imply a
       minimum experience band even when the JD never says "X years".
    2. Description text — scans explicit "N years" / "N+ years" /
       "minimum N years" / "at least N years" / "N or more years" patterns.
    """
    if _max_years == 0:
        return False

    # ── 1. Title-based seniority filter ──────────────────────────────
    if title:
        t = title.lower()
        seniority_tiers = [
            (2, ["senior", "sr ", "sr.", "lead ", "staff ", "principal",
                 "director", "vp ", "head of", "architect"]),
            (4, ["staff ", "principal", "director", "vp ", "head of",
                 "architect", "distinguished"]),
            (7, ["principal", "director", "vp ", "head of", "distinguished"]),
        ]
        for threshold, words in seniority_tiers:
            if _max_years <= threshold and any(w in t for w in words):
                return True

    # ── 2. Description explicit-year patterns ────────────────────────
    if not description:
        return False

    text = description.lower()
    patterns = [
        r"(\d+)\s*\+?\s*(?:to|-|–)?\s*\d*\s*years?",
        r"minimum\s+(?:of\s+)?(\d+)\s+years?",
        r"at\s+least\s+(\d+)\s+years?",
        r"(\d+)\s+or\s+more\s+years?",
        r"(\d+)\s+years?\s+(?:or\s+more|and\s+above)",
        r"experience[:\s]+(\d+)\s*\+?\s*years?",
    ]
    for pat in patterns:
        for m in re.findall(pat, text):
            try:
                if int(m) > _max_years:
                    return True
            except (ValueError, TypeError):
                continue
    return False


def _normalize_bullets(text):
    return "\n".join(
        re.sub(r"^\s*[-–\*•]+\s+", "• ", line)
        for line in text.split("\n")
    )


def _clean(raw):
    raw = re.sub(r"```.*?```", "", raw, flags=re.DOTALL)
    raw = raw.replace("```", "")
    raw = re.sub(r"\*\*(.*?)\*\*", r"\1", raw)
    raw = re.sub(r"\*(.*?)\*",     r"\1", raw)
    return raw.strip()


# ── Match scoring ─────────────────────────────────────────────────────────────

def calculate_match_score(job, resume_text):
    description = job.get("description", "")

    if _exceeds_experience(description, job.get("title", "")):
        return "Score: 0\nReason: Job requires more experience than your configured maximum."

    # Truncate both inputs for scoring — a compact summary is sufficient to
    # judge fit.  Sending the full 15 000-char résumé + long JD for every one
    # of 200+ candidates wastes tokens and adds ~300 ms per call.
    # 2 500 chars captures the summary, skills, and first 2–3 jobs — plenty
    # for an accurate relevance score.
    resume_for_score = resume_text[:2500]
    desc_for_score   = description[:2000]

    prompt = f"""You are a strict ATS job matching engine.

Score 0–100 using weighted criteria:
1. Required skills match     (40 %)
2. Years of experience       (20 %)
3. Tools & technologies      (15 %)
4. Domain relevance          (10 %)
5. Role similarity           (10 %)
6. Keyword coverage           (5 %)

Rules:
- Be strict. Do NOT give scores above 85 unless it is a strong match.
- If critical required skills are missing, score must be below 60.

Resume:
{resume_for_score}

Job Description:
{desc_for_score}

Return ONLY valid JSON:
{{"score": <0-100>, "reason": "Two concise lines explaining the score."}}
"""
    try:
        raw          = client.chat.completions.create(
            model="gpt-4o-mini", temperature=0.1,
            messages=[{"role": "user", "content": prompt}],
        ).choices[0].message.content.strip()
        score_m      = re.search(r'"score"\s*:\s*(\d+)', raw)
        reason_m     = re.search(r'"reason"\s*:\s*"([^"]+)"', raw)
        score        = score_m.group(1)  if score_m  else "0"
        reason       = reason_m.group(1) if reason_m else "Unable to evaluate."
        return f"Score: {score}\nReason: {reason}"
    except Exception as e:
        print("Match scoring error:", e)
        return "Score: 0\nReason: Error calculating match."


# ── Cover letter ──────────────────────────────────────────────────────────────

def generate_cover_letter(job, resume_text):
    title       = job.get("title", "")
    company     = job.get("company", "")
    description = job.get("description", "")
    today       = date.today().strftime("%B %d, %Y")

    prompt = f"""You are a professional career consultant writing a targeted job application cover letter.

Write a concise, keyword-rich cover letter. Follow these rules exactly:

RULES:
- Exactly 3 paragraphs (opening, evidence, closing)
- Total 250–280 words
- Do NOT fabricate any experience, certification, or company name
- Use ONLY facts from the resume provided
- Naturally weave in keywords from the job description
- Professional but warm tone
- No clichéd openers like "I am writing to express my interest"

STRUCTURE:
Paragraph 1 (3 sentences): Name the specific role and company. State years of experience and most relevant skill. One sentence on why this company/role excites you.
Paragraph 2 (4 sentences): Lead with the strongest quantified achievement matching the JD. Connect 2–3 specific skills/tools from the JD to real experience. Reference one specific project or outcome by name.
Paragraph 3 (2 sentences): Express readiness to contribute. Invite them to review the attached resume and connect.

End with:
Sincerely,

{_name}
{_contact}

Job Title: {title}
Company: {company}

Job Description:
{description[:2500]}

Resume:
{resume_text[:2000]}

Start directly with today's date ({today}). No subject line. Return only the letter text.
"""
    try:
        return client.chat.completions.create(
            model="gpt-4o-mini", temperature=0.3,
            messages=[{"role": "user", "content": prompt}],
        ).choices[0].message.content.strip()
    except Exception as e:
        print("Cover letter error:", e)
        return "Error generating cover letter."


# ── Resume tailoring ──────────────────────────────────────────────────────────

_SCHEMA_DOC = textwrap.dedent("""
{
  "summary": "string — 10-12 sentences",
  "experience": [
    {
      "title": "string",
      "company": "string",
      "location": "string",
      "dates": "string",
      "projects": [
        {
          "name": "string or null  (null when there is no sub-project heading)",
          "bullets": ["string"]
        }
      ]
    }
  ],
  "skills": [
    {"category": "string", "items": ["string"]}
  ],
  "education": [
    {
      "degree": "string",
      "school": "string",
      "location": "string",
      "gpa": "string or null",
      "dates": "string"
    }
  ],
  "certifications": ["string"],
  "leadership":    [{"role": "string", "dates": "string"}],
  "activities":    [{"name": "string", "dates": "string"}],
  "achievements":  ["string"]
}
""").strip()


def _parse_json_from_response(raw):
    """
    Extract and parse the first COMPLETE JSON object from an AI response string.

    Uses brace-depth tracking (accounting for quoted strings and escape
    sequences) to locate the TRUE matching closing brace.

    The old rfind('}') approach was the root cause of the missing-sections
    bug: when the model output was truncated at a token limit, rfind could
    land on a nested '}' that made the slice syntactically valid JSON but
    semantically incomplete — experience present, skills/education silently
    absent.  This function raises ValueError instead, letting the caller
    retry with a higher token budget.
    """
    # Strip markdown fences
    raw = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()

    start = raw.find("{")
    if start == -1:
        raise ValueError("No JSON object found in response")

    # Walk from the opening brace, tracking depth inside/outside strings
    depth       = 0
    in_string   = False
    escape_next = False
    end         = -1

    for i in range(start, len(raw)):
        ch = raw[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end == -1:
        # The JSON object was never closed — the model was cut off by the
        # token limit.  Raise so the retry loop can make another attempt.
        raise ValueError(
            "JSON object is incomplete — model output was truncated at the token limit"
        )

    return json.loads(raw[start:end])


def _parse_to_json(resume_text):
    """
    STEP 1 — Pure fact extraction.
    Converts plain-text resume → structured JSON with ZERO creativity.

    Every value is copied exactly as written: job titles, company names,
    employment/education dates, GPA, certification names, all numbers and
    percentages.

    Robustness guarantees
    ---------------------
    • max_tokens = 16 000  (gpt-4o-mini's actual maximum output)
      Eliminates the main cause of truncation for even the densest résumés.
    • finish_reason check  — if the API says "length" the output was still
      cut; retry immediately rather than returning incomplete data.
    • Section-completeness check  — retry if experience OR skills OR
      education is missing from a non-trivial résumé (not just experience).
    • Input capped at 30 000 chars (≈ 7 500 tokens).  This handles résumés up
      to ~10 pages with room to spare.  The cap exists only as a safety valve
      against pathological copy-pastes; it MUST be large enough to include the
      Skills / Education / Certifications sections that appear near the END of
      most résumés.  Never set it below 25 000 — at 12 000 those trailing
      sections get silently chopped off on a 4-page résumé.
    """
    # Input cap: must be generous enough to capture every section of even a
    # long résumé.  Skills / Education / Certs appear at the END — a small cap
    # like 12 000 silently drops them, producing a PDF with only summary/exp.
    # 30 000 chars ≈ 7 500 input tokens — well inside gpt-4o-mini's 128 K window.
    resume_capped = resume_text[:30000]

    prompt = f"""You are a resume parser — a transcription engine with zero creativity.

TASK: Convert the resume text below into the JSON schema provided.

ABSOLUTE RULES:
- Copy every value CHARACTER-FOR-CHARACTER as it appears in the resume.
  This includes: job titles, company names, employment dates, education dates,
  degree names, school names, GPA values, certification names, all numbers,
  all percentages, all quantified outcomes.
- Do NOT rephrase, expand, improve, or summarise anything.
- Do NOT add any information, skill, company, project, date, or tool
  that is not explicitly written in the resume.
- Omit optional keys (certifications, leadership, activities, achievements)
  if those sections do not exist in the resume.
- "projects[].name" is null when there is no named sub-project heading.
- "gpa" is null if not mentioned.
- Return ONLY valid JSON — no markdown, no backticks, no explanation.

Schema:
{_SCHEMA_DOC}

Resume:
{resume_capped}
"""

    _EMPTY_FALLBACK = {"summary": "", "experience": [], "skills": [], "education": []}

    for attempt in range(3):   # up to 3 attempts — covers transient API errors
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini", temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=16000,   # gpt-4o-mini maximum — eliminates truncation
            )
            raw           = resp.choices[0].message.content.strip()
            finish_reason = resp.choices[0].finish_reason   # "stop" | "length" | …

            # "length" means the model was still generating when the token
            # budget ran out — the JSON is guaranteed incomplete, retry.
            if finish_reason == "length":
                if attempt < 2:
                    continue
                print("  Warning: resume parse cut off by token limit after 3 attempts.")
                return _EMPTY_FALLBACK

            result = _parse_json_from_response(raw)

            # Completeness check — any missing critical section on a real résumé
            # means the parse is unreliable.  Retry rather than cache bad data.
            substantial = len(resume_text) > 300
            missing = (
                substantial and (
                    not result.get("experience") or
                    not result.get("skills")     or
                    not result.get("education")
                )
            )
            if missing and attempt < 2:
                continue   # try again

            return result

        except Exception as e:
            if attempt < 2:
                continue   # silent retry
            print(f"Resume parse error after 3 attempts: {e}")
            return _EMPTY_FALLBACK

    return _EMPTY_FALLBACK


def _trim_for_inject(base_json):
    """
    Return a copy of base_json trimmed for use as _inject_keywords input.

    Keeps only the 3 most recent jobs and caps bullets at 5 per project.
    This reduces both the input token count (smaller JSON in the prompt) AND
    the required output token count (the model has to reproduce less content),
    making truncation far less likely in Step 2.
    """
    import copy
    trimmed = copy.deepcopy(base_json)

    # Keep at most 3 most recent jobs
    exp = trimmed.get("experience", [])[:3]
    for job in exp:
        for proj in job.get("projects", []):
            proj["bullets"] = proj.get("bullets", [])[:5]
    trimmed["experience"] = exp

    return trimmed


def _inject_keywords(base_json, title, company, description):
    """
    STEP 2 — Keyword injection only.
    Takes the verified, fact-locked JSON from Step 1 and rewrites ONLY the
    summary text and bullet wording to include JD keywords.
    Every structural field (dates, companies, titles, education, certs) is
    passed in as ground truth and must be copied verbatim into the output.
    """

    # Trim before building the prompt — reduces both input and required output size
    trimmed_base = _trim_for_inject(base_json)

    # Build an explicit lock list so the AI sees exactly what must not change
    locked_lines = []
    for exp in trimmed_base.get("experience", []):
        locked_lines.append(
            f'  title="{exp.get("title","")}" | company="{exp.get("company","")}" | '
            f'location="{exp.get("location","")}" | dates="{exp.get("dates","")}"'
        )
        for proj in exp.get("projects", []):
            if proj.get("name"):
                locked_lines.append(f'    project name="{proj["name"]}"')

    for edu in trimmed_base.get("education", []):
        locked_lines.append(
            f'  degree="{edu.get("degree","")}" | school="{edu.get("school","")}" | '
            f'location="{edu.get("location","")}" | gpa="{edu.get("gpa","")}" | '
            f'dates="{edu.get("dates","")}"'
        )

    locked_block = "\n".join(locked_lines) if locked_lines else "  (none)"

    prompt = f"""You are an ATS resume optimizer. You will receive a candidate's verified
resume as JSON and a job description. Your job is strictly limited to improving
keyword alignment — nothing else.

═══════════════════════════════════════════════════════
 WHAT YOU ARE ALLOWED TO CHANGE
═══════════════════════════════════════════════════════
1. "summary" — rewrite as exactly 4 concise sentences (no more, no less).
   • Sentence 1: years of experience + top 2 skills matching the JD.
   • Sentence 2: strongest quantified achievement from the resume (%, numbers, $).
   • Sentence 3: key tools/technologies from the resume that match the JD.
   • Sentence 4: career goal aligned to this specific role and company.
   • Do NOT invent any new facts, companies, or tools.

2. "experience" — include ONLY the 3 most recent positions (drop older ones).
   This is a hard cap for resume readability on a single page.

3. "projects[].bullets" — for each project keep MAXIMUM 5 bullets.
   • Pick the 5 most impactful bullets that best match this JD's keywords.
   • Drop weaker or less-relevant bullets — do NOT add new ones.
   • Rephrase kept bullets to naturally include JD keywords while keeping
     the EXACT same action, tool, and outcome.
   • Each bullet format: [strong action verb] + [tool/method from resume] + [outcome].

4. "skills[].items" — you may reorder items or use JD-matching terminology
   for tools the candidate ALREADY has. Do NOT add new tools or skills.

═══════════════════════════════════════════════════════
 WHAT YOU MUST COPY VERBATIM — DO NOT CHANGE BY EVEN ONE CHARACTER
═══════════════════════════════════════════════════════
The following values are locked ground truth from the candidate's real resume:
{locked_block}

Full locked sections (copy the entire array EXACTLY as shown — no changes):
- "certifications": {json.dumps(trimmed_base.get("certifications", []))}
- "leadership":     {json.dumps(trimmed_base.get("leadership", []))}
- "activities":     {json.dumps(trimmed_base.get("activities", []))}
- "achievements":   {json.dumps(trimmed_base.get("achievements", []))}

═══════════════════════════════════════════════════════
 INPUT RESUME JSON (ground truth — your output must include ALL sections)
═══════════════════════════════════════════════════════
{json.dumps(trimmed_base, indent=2)}

═══════════════════════════════════════════════════════
 JOB: {title} at {company}
═══════════════════════════════════════════════════════
{description[:3000]}

IMPORTANT: Your output JSON MUST contain all sections present in the input JSON
above (summary, experience, skills, education, and any optional sections).
Do NOT omit any section.

Return ONLY valid JSON using the same schema as the input. No markdown, no backticks.
"""
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini", temperature=0.1,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=16000,   # gpt-4o-mini maximum
        )
        raw           = resp.choices[0].message.content.strip()
        finish_reason = resp.choices[0].finish_reason

        if finish_reason == "length":
            # Output was cut off — skip injection and use the base as-is
            print(f"  Keyword injection cut off for {company} — using parsed resume directly")
            return base_json

        tailored = _parse_json_from_response(raw)

        # ── Safety net A: restore locked structural fields ───────────────────
        # Experience: title / company / location / dates / project names
        for i, exp in enumerate(trimmed_base.get("experience", [])):
            if i >= len(tailored.get("experience", [])):
                break
            t_exp = tailored["experience"][i]
            t_exp["title"]    = exp.get("title", "")
            t_exp["company"]  = exp.get("company", "")
            t_exp["location"] = exp.get("location", "")
            t_exp["dates"]    = exp.get("dates", "")
            for j, proj in enumerate(exp.get("projects", [])):
                if j < len(t_exp.get("projects", [])):
                    t_exp["projects"][j]["name"] = proj.get("name")

        # Education: all fields locked
        for i, edu in enumerate(trimmed_base.get("education", [])):
            if i < len(tailored.get("education", [])):
                t_edu = tailored["education"][i]
                t_edu["degree"]   = edu.get("degree", "")
                t_edu["school"]   = edu.get("school", "")
                t_edu["location"] = edu.get("location", "")
                t_edu["gpa"]      = edu.get("gpa")
                t_edu["dates"]    = edu.get("dates", "")

        # ── Safety net B: restore ENTIRE sections if AI left them empty ──────
        # This is the critical fix for the "only summary in PDF" bug.
        # The AI sometimes returns an empty list for skills / education /
        # experience if it misread the prompt or the input was borderline.
        # Always fall back to the ground-truth base_json values.
        for key in (
            "skills", "education", "experience",
            "certifications", "leadership", "activities", "achievements",
        ):
            base_val    = base_json.get(key)    # original full parse
            trimmed_val = trimmed_base.get(key) # trimmed version used in prompt
            tailored_val = tailored.get(key)

            if not tailored_val:
                # AI returned empty — use the trimmed base (or full base for non-trimmed keys)
                if trimmed_val:
                    tailored[key] = trimmed_val
                elif base_val:
                    tailored[key] = base_val
            elif key in ("certifications", "leadership", "activities", "achievements"):
                # Always restore these from base — they must never be modified
                if base_val:
                    tailored[key] = base_val
                elif key in tailored:
                    del tailored[key]

        return tailored

    except Exception as e:
        print(f"  Keyword injection error ({e}) — using parsed resume without tailoring")
        return base_json


def parse_resume_once(resume_text):
    """
    Pre-parse the resume into locked JSON one time, then pass the result
    into every tailor_resume() call via base_json=.
    Avoids repeating the same expensive API call for every matched job.
    """
    return _parse_to_json(resume_text)


def tailor_resume(job, resume_text, base_json=None):
    """
    Two-step resume tailoring:
      Step 1 — Parse original resume into locked JSON (zero creativity, facts only).
               Skipped when base_json is supplied (pre-parsed once by the caller).
      Step 2 — Inject JD keywords into summary + bullets only; all dates/companies/
               education/certs are restored from Step 1 as a hard safety net.
    Returns a dict consumed by pdf_generator.generate_resume_pdf().
    """
    if base_json is None:
        base_json = _parse_to_json(resume_text)

    result = _inject_keywords(
        base_json,
        job.get("title", ""),
        job.get("company", ""),
        job.get("description", ""),
    )

    # ── Final safety pass ─────────────────────────────────────────────────────
    # If _inject_keywords returned anything with empty critical sections
    # (shouldn't happen after the fixes above, but belts-and-suspenders),
    # silently fill from base_json so the PDF is always complete.
    for key in ("experience", "skills", "education", "certifications"):
        if not result.get(key) and base_json.get(key):
            result[key] = base_json[key]

    return result
