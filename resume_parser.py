import re


SECTION_ORDER = [
    "PROFESSIONAL SUMMARY",
    "PROFESSIONAL EXPERIENCE",
    "TECHNICAL SKILLS",
    "CERTIFICATIONS",
    "EDUCATION",
]


def clean_ai_text(text):
    text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    text = text.replace('```', '')
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    text = re.sub(r'\*(.*?)\*',     r'\1', text)
    # Strip === and --- separator lines
    lines = [l for l in text.split('\n') if not re.match(r'^[=\-]{4,}$', l.strip())]
    return '\n'.join(lines).strip()


def normalize_bullets(text):
    result = []
    for line in text.split('\n'):
        result.append(re.sub(r'^\s*[-–\*•]+\s+', '• ', line))
    return '\n'.join(result)


def normalize_section_headings(text):
    lines = text.split('\n')
    return '\n'.join(
        line.upper() if line.strip().upper() in SECTION_ORDER else line
        for line in lines
    )


def reorder_sections(text):
    """Re-order body sections into SECTION_ORDER while preserving the 2-line header."""
    lines = text.split('\n')

    # Find where the first recognised section heading starts
    header_lines  = []
    first_sec_idx = None

    for idx, line in enumerate(lines):
        if line.strip().upper() in SECTION_ORDER:
            first_sec_idx = idx
            break
        header_lines.append(line)

    if first_sec_idx is None:
        return text

    # Parse sections
    sections        = {}
    current_section = None

    for line in lines[first_sec_idx:]:
        stripped = line.strip().upper()
        if stripped in SECTION_ORDER:
            current_section = stripped
            sections[current_section] = []
        elif current_section is not None:
            sections[current_section].append(line)

    # Rebuild in correct order
    ordered = list(header_lines)
    for section in SECTION_ORDER:
        if section in sections:
            ordered.append(section)
            ordered.extend(sections[section])
            ordered.append('')

    return '\n'.join(ordered).strip()


def parse_resume_for_pdf(raw_text):
    """Full cleaning pipeline: strip artifacts → normalize bullets → fix headings → reorder."""
    text = clean_ai_text(raw_text)
    text = normalize_bullets(text)
    text = normalize_section_headings(text)
    text = reorder_sections(text)
    return text
