"""
renderer/triage_sheet.py — SBI Smart Shield Plus "Triage Layout" case
sheet: a single-page dashboard reproducing the client reference
(CaseSheet_4CYJ836521_Variation4_Triage.pdf).

Structure (top to bottom), all driven from decision_json — nothing here
is hardcoded to a specific case:
  1. Brand header (wordmark + title) with a navy rule.
  2. ID summary bar (case id, LA, age/gender/marital, occupation, education,
     location, SA, premium).
  3. Decision bar (verdict label, FAIL/WARN counts, plain-language reason).
  4. Risk summary — HIGH / MEDIUM / LOW groups, each row a (title,
     evidence, action) triple curated from gating + flags.
  5. Four-column data table — Financials / IIB & Compliance / Docs-KYC /
     Policy, each a label/value list with per-field colour (red = bad,
     amber = warn, green = good, black = neutral fact).
  6. "Requirements to Raise" box (decision["requirements_to_raise"],
     computed by engine.requirements — see that module for the business
     rules behind which items appear).
  7. Footer.

This renderer is additive — it never touches renderer/pdf.py or
renderer/talic_sheet.py. Row/column heights are computed from actual
wrapped text, not fixed pixel offsets, so it degrades gracefully for
cases whose data is longer or shorter than the reference case.
"""

from __future__ import annotations

import re
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas as _canvas

# ── page geometry (exact — measured off the reference PDF's own vector
# paths via pdfplumber; the content frame is NOT symmetric: left edge
# 14.69, right edge 580.03) ─────────────────────────────────────────────
PAGE_W, PAGE_H = 595.0, 842.0
ML = 14.69
CONTENT_X0 = 14.69
CONTENT_X1 = 580.03
CONTENT_W = CONTENT_X1 - CONTENT_X0
# Vertical gap left after every top-level bordered section (ID bar, BHB
# panel, Requirements box, Decision bar, Risk Summary, detail table)
# before the next one starts. Each section used its own independently
# hand-picked trailing margin (4.5 / 5.2 / 8.0 / 3.4 / 6.3 ...) that added
# up to visibly uneven spacing between sections — standardized to one
# value so the page reads as evenly paced top to bottom rather than
# alternating cramped/loose gaps.
_SECTION_GAP = 6.3
# A severity header (HIGH/MEDIUM/LOW RISK) needs more breathing room
# before it than a plain row-to-row gap — see _draw_risk_summary for why
# this has to be applied explicitly rather than left to each section's
# static reference anchor.
_RISK_SECTION_GAP = 14.0

# ── palette (sampled from the client reference PDF) ────────────────────────
NAVY = HexColor("#1A2A5E")
MAGENTA = HexColor("#C71F5C")
RED = HexColor("#B3121F")
AMBER = HexColor("#A86100")
GREEN = HexColor("#12732E")
INK = HexColor("#121212")
ID_INK = HexColor("#121726")
ID_LABEL = HexColor("#4A5463")
LABEL_GRAY = HexColor("#454C5C")  # darkened from #6B7380 — too low-contrast
# at the data-table's small label size,
# read as "very light"
ACTION_INK = HexColor("#1F2938")
HEADER_BAR = HexColor("#374151")
ROW_LINE = HexColor("#EAEAEA")
SECTION_LINE = HexColor("#111111")
COL_LINE = HexColor("#D8D8D8")
CARD_LINE = HexColor("#CCCCCC")
MUTE = HexColor("#595959")  # darkened from #868686 — same
# too-light-to-read issue as LABEL_GRAY
# above, on the captions/footer that still
# use this color: "FINANCIAL & PROFILE RISK
# SUMMARY", the BHB subtitle, and the footer
MUTE2 = HexColor("#666666")
WHITE = HexColor("#FFFFFF")

_TONE_COLOR = {"bad": RED, "warn": AMBER, "good": GREEN, "neutral": INK}

# ── fonts ───────────────────────────────────────────────────────────────
# The reference PDF embeds DejaVu Sans / DejaVu Sans Mono (regular + bold).
# Bundled here as actual files (renderer/fonts/) rather than relying on a
# system install — DejaVu is public domain and these are the exact same
# files the reference was built from, so metrics (and therefore every
# measured x/y position below) match exactly, not just visually.
_FONTS_DIR = Path(__file__).parent / "fonts"
_SANS, _SANS_B, _MONO, _MONO_B = "Helvetica", "Helvetica-Bold", "Courier", "Courier-Bold"
_fonts_ready = False

# DejaVu Sans / Sans Mono, regular and bold, all share this ascent (in
# 1000-unit em space) — empirically confirmed against the family's actual
# vertical metrics. Used to convert a measured "distance from page top to
# glyph top" (the pdfplumber convention used throughout this file's
# layout constants) into the reportlab baseline y that reproduces it
# exactly: baseline_y = PAGE_H - top - _ASCENT_FRAC * size.
_ASCENT_FRAC = 759.765625 / 1000.0


def _register_fonts() -> None:
    global _SANS, _SANS_B, _MONO, _MONO_B, _fonts_ready
    if _fonts_ready:
        return
    try:
        cand = [
            ("TriageSans", _FONTS_DIR / "DejaVuSans.ttf"),
            ("TriageSans-Bold", _FONTS_DIR / "DejaVuSans-Bold.ttf"),
            ("TriageMono", _FONTS_DIR / "DejaVuSansMono.ttf"),
            ("TriageMono-Bold", _FONTS_DIR / "DejaVuSansMono-Bold.ttf"),
        ]
        if all(p.exists() for _, p in cand):
            for name, path in cand:
                pdfmetrics.registerFont(TTFont(name, str(path)))
            _SANS, _SANS_B, _MONO, _MONO_B = (
                "TriageSans",
                "TriageSans-Bold",
                "TriageMono",
                "TriageMono-Bold",
            )
        else:
            # Fallback (DejaVu not bundled/found): closest system fonts,
            # then reportlab's built-in Base14 as a last resort. Position
            # constants below were measured against DejaVu and will be
            # close but not pixel-exact on this path.
            cand2 = [
                ("TriageSans", Path(r"C:\Windows\Fonts\arial.ttf")),
                ("TriageSans-Bold", Path(r"C:\Windows\Fonts\arialbd.ttf")),
                ("TriageMono", Path(r"C:\Windows\Fonts\consola.ttf")),
                ("TriageMono-Bold", Path(r"C:\Windows\Fonts\consolab.ttf")),
            ]
            if all(p.exists() for _, p in cand2):
                for name, path in cand2:
                    pdfmetrics.registerFont(TTFont(name, str(path)))
                _SANS, _SANS_B, _MONO, _MONO_B = (
                    "TriageSans",
                    "TriageSans-Bold",
                    "TriageMono",
                    "TriageMono-Bold",
                )
    except Exception:
        pass
    _fonts_ready = True


def _ty(top: float, size: float) -> float:
    """Reportlab baseline y for text whose glyph-top should land at `top`
    (distance from the page's top edge — the same convention as every
    measured position in this file, taken directly off the reference
    PDF via pdfplumber). See _ASCENT_FRAC."""
    return PAGE_H - top - _ASCENT_FRAC * size


# ── small formatting helpers ───────────────────────────────────────────────


def _safe_int(v):
    try:
        if v in (None, "", "Not Available"):
            return None
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _inr(n) -> str:
    """Indian digit grouping, e.g. 'Rs. 8,09,007' — matches the reference."""
    v = _safe_int(n)
    if v is None:
        return "Not Available"
    sign = "-" if v < 0 else ""
    s = str(abs(v))
    if len(s) <= 3:
        grouped = s
    else:
        last3, rest = s[-3:], s[:-3]
        parts = []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        grouped = ",".join(parts) + "," + last3
    return f"Rs. {sign}{grouped}"


def _cv(calcs, name, default=None):
    c = (calcs or {}).get(name) or {}
    v = c.get("value")
    return v if v not in (None, "") else default


def _wrap(
    text: str, font: str, size: float, max_width: float, max_lines: int | None = None
) -> list[str]:
    """Greedy word-wrap using actual glyph widths. Truncates with an
    ellipsis if max_lines is given and the text overflows it — the
    overflow guard the task explicitly calls for."""
    from underwriting_platform.renderer.reference_tables import plain_text

    words = plain_text(text or "").split()
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if pdfmetrics.stringWidth(trial, font, size) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
        if max_lines and len(lines) >= max_lines:
            break
    if cur and (not max_lines or len(lines) < max_lines):
        lines.append(cur)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
    if max_lines and len(words) and " ".join(lines) != text and len(lines) == max_lines:
        last = lines[-1]
        # Trim whole trailing words first, so the ellipsis lands at a word
        # boundary ("...data-entry…") rather than mid-word ("...data-entr…").
        # Only fall back to character-by-character trimming once a single
        # remaining word is itself too long to fit — the rare case, not
        # the common one.
        while " " in last and pdfmetrics.stringWidth(last + "…", font, size) > max_width:
            last = last.rsplit(" ", 1)[0]
        while pdfmetrics.stringWidth(last + "…", font, size) > max_width and len(last) > 1:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines or [""]


# ── PII redaction ────────────────────────────────────────────────────────
# Several rows fall back to the gating/flag/reconciliation layer's own raw
# evidence/explanation text (agent-generated prose) rather than a field
# built from structured data — e.g. the generic risk-summary fallback for
# any criterion without a tailored row. That text can embed an unmasked
# PAN/Aadhaar/mobile/email, so it is redacted wherever displayed. Same
# patterns qc/checks_pii.py scans the rendered PDF for — kept in sync so
# this redaction actually prevents what that gate flags.
_PAN_RE = re.compile(r"\b[A-Z]{3}[PFCHATBLJG][A-Z]\d{4}[A-Z]\b")
_AADHAAR_RE = re.compile(r"\b\d{4}([\s\-])\d{4}\1\d{4}\b")
_MOBILE_RE = re.compile(r"(?<!\d)[6-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9-.]+")


def _mobile_redact_repl(m, t):
    # A bare 10-digit number sitting alone inside parentheses —
    # "(9394820309)" — is how this file cites a policy/workflow reference
    # number (e.g. check_pending_proposal_disclosure's own evidence text),
    # not a mobile number; the two shapes collide purely by coincidence.
    # Only skip redaction when BOTH the immediately preceding and following
    # characters are "(" / ")" — a genuine mobile number that merely ends up
    # before a closing paren for an unrelated reason (e.g. "(Mobile: 98...)")
    # still gets redacted, since its preceding character isn't "(".
    start, end = m.start(), m.end()
    if start > 0 and end < len(t) and t[start - 1] == "(" and t[end] == ")":
        return m.group(0)
    return "XXXXXX" + m.group(0)[-4:]


def _redact_pii(s):
    if s in (None, ""):
        return s
    t = str(s)
    t = _PAN_RE.sub(lambda m: "X" * (len(m.group(0)) - 4) + m.group(0)[-4:], t)
    t = _AADHAAR_RE.sub(lambda m: "XXXX XXXX " + m.group(0)[-4:], t)
    t = _MOBILE_RE.sub(lambda m: _mobile_redact_repl(m, t), t)
    t = _EMAIL_RE.sub(lambda m: "[email redacted]", t)
    return t


_OP_RE = re.compile(r"(≠|≥|≤|<|>)")


def _draw_mono_line(
    c,
    text: str,
    x: float,
    baseline_y: float,
    size: float,
    base_color=INK,
    bold_terms: tuple[str, ...] = (),
    op_color=None,
):
    """Draw one line of risk-row description text, colouring comparison
    operators bold (matches the reference's emphasis convention) and
    bolding any caller-supplied terms (e.g. 'Declared:', 'IIB:'). Uses
    _SANS/_SANS_B (not a separate monospace family) — one font throughout
    the sheet. Function name kept as-is (not renamed) to avoid touching
    every call site for a cosmetic rename.

    op_color overrides the operator's colour (RED by default) — callers
    pass GREEN for a row whose embedded comparison is favorable to the
    applicant despite the row itself being WARN/FAIL overall (e.g.
    "CIBIL score 697 ≥ 650" reads as a problem when coloured the same
    red as a genuinely bad finding)."""
    op_color = op_color if op_color is not None else RED
    parts = _OP_RE.split(text)
    cx = x
    for part in parts:
        if not part:
            continue
        if _OP_RE.fullmatch(part):
            c.setFont(_SANS_B, size)
            c.setFillColor(op_color)
            c.drawString(cx, baseline_y, part)
            cx += pdfmetrics.stringWidth(part, _SANS_B, size)
            continue
        # split further on bold terms, preserving order
        seg = part
        while seg:
            matched = None
            for term in bold_terms:
                if seg.startswith(term):
                    matched = term
                    break
            if matched:
                c.setFont(_SANS_B, size)
                c.setFillColor(base_color)
                c.drawString(cx, baseline_y, matched)
                cx += pdfmetrics.stringWidth(matched, _SANS_B, size)
                seg = seg[len(matched) :]
            else:
                nxt = min((seg.find(t) for t in bold_terms if seg.find(t) > 0), default=len(seg))
                chunk = seg[:nxt] if nxt > 0 else seg
                c.setFont(_SANS, size)
                c.setFillColor(base_color)
                c.drawString(cx, baseline_y, chunk)
                cx += pdfmetrics.stringWidth(chunk, _SANS, size)
                seg = seg[len(chunk) :]
                if not chunk:
                    break


# ── header ──────────────────────────────────────────────────────────────


def _draw_header(c) -> float:
    # Exact positions measured off the reference PDF (pdfplumber top/x0).
    c.setFont(_SANS_B, 9.14)
    c.setFillColor(NAVY)
    c.drawString(ML, _ty(16.5, 9.14), "SBI")
    w = pdfmetrics.stringWidth("SBI ", _SANS_B, 9.14)
    c.setFillColor(MAGENTA)
    c.drawString(ML + w, _ty(16.5, 9.14), "Life")
    c.setFont(_SANS, 4.92)
    c.setFillColor(MUTE)
    c.drawString(ML, _ty(24.9, 4.92), "Apne liye. Apno ke liye.")

    title = "Financial Underwriting Case Sheet"
    c.setFont(_SANS_B, 9.14)
    tw = pdfmetrics.stringWidth(title, _SANS_B, 9.14)
    tx = (PAGE_W - tw) / 2.0
    c.setFillColor(NAVY)
    c.drawString(tx, _ty(32.0, 9.14), title)
    sub = "TruAI UW · Datamatics · CONFIDENTIAL"
    c.setFont(_SANS, 4.92)
    sw = pdfmetrics.stringWidth(sub, _SANS, 4.92)
    c.setFillColor(MUTE2)
    c.drawString((PAGE_W - sw) / 2.0, _ty(41.6, 4.92), sub)

    rule_top = 48.69
    c.setFillColor(NAVY)
    c.rect(CONTENT_X0, PAGE_H - rule_top - 1.15, CONTENT_W, 1.15, stroke=0, fill=1)
    return 53.3


# ── ID summary bar ──────────────────────────────────────────────────────


def _draw_id_bar(c, decision: dict, top: float) -> float:
    app = decision.get("applicant") or {}
    case_id = decision.get("case_id") or "Not Available"
    name = app.get("applicant_name") or "Not Available"
    age = app.get("age")
    gender = (app.get("gender") or "")[:1].upper() or "Not Available"
    marital = app.get("marital_status") or "Not Available"
    occ = app.get("occupation") or "Not Available"
    employer = _display_employer(decision)
    edu = app.get("education_level") or "Not Available"
    loc = app.get("city") or "Not Available"
    pin = app.get("pin_code") or ""
    sa = _inr(decision.get("proposed_sa") or app.get("sum_assured"))
    prem = app.get("annual_premium")
    prem_txt = f"{_safe_int(prem):,}/yr" if _safe_int(prem) else "Not Available"

    fields = [
        ("LA", 76.6, f"{name}", 84.5),
        ("AGE", 128.7, f"{age or 'Not Available'} {gender} {marital}", 140.4),
        ("OCC", 186.7, f"{occ}" + (f" ({employer})" if employer else ""), 198.6),
        ("EDU", 321.1, edu, 333.1),
        ("LOC", 380.1, f"{loc} {pin}".strip(), 391.6),
        ("SA", 449.3, sa.replace("Rs. ", ""), 457.6),
        ("PREM", 494.2, prem_txt, 509.9),
    ]
    # Field label/value x-positions are the reference's own measured grid
    # for THIS case's field widths. A longer value (e.g. a longer LA name
    # than the reference's "Synthetic Applicant", or a long OCC/employer combo)
    # can run past the NEXT field's fixed label position — checked per
    # field below (not just once at the row's end) by comparing the
    # running cursor against each field's own hardcoded label_x; once a
    # collision is detected, every field from there on uses a dynamic
    # cursor from actual glyph widths. If the content is long enough that
    # even dynamic placement would run the card's right edge, wrap the
    # remaining fields onto a second line instead of clipping them —
    # the card grows a row taller rather than ever losing text.
    # Fonts bumped again 8.44->9.28 / 4.92->5.41 / 6.33->6.96 for
    # readability. The hardcoded field label_x/value_x
    # grid below is still calibrated for the smaller font, but that's
    # fine by design — the overflow-detection logic already switches any
    # field whose bigger glyphs collide with the next field's fixed
    # position into dynamic (measured) placement, same mechanism already
    # built for a long LA name/OCC value (see the comment above).
    first_x = 21.0 + pdfmetrics.stringWidth(case_id, _SANS_B, 9.28) + 8
    cursor = first_x
    overflow = False
    line = 0
    placements = []  # (line, lx, label, vx, value)
    for label, label_x, value, value_x in fields:
        if not overflow and label_x < cursor:
            overflow = True
        lx = cursor if overflow else label_x
        vx = (lx + pdfmetrics.stringWidth(label, _SANS_B, 5.41) + 3.2) if overflow else value_x
        field_end = vx + pdfmetrics.stringWidth(value, _SANS_B, 6.96)
        if field_end > CONTENT_X1 - 6:
            line += 1
            lx = ML + 6.3
            vx = lx + pdfmetrics.stringWidth(label, _SANS_B, 5.41) + 3.2
            overflow = True  # every field after a wrap stays in dynamic mode
            field_end = vx + pdfmetrics.stringWidth(value, _SANS_B, 6.96)
        placements.append((line, lx, label, vx, value))
        cursor = field_end + 10.5

    n_lines = placements[-1][0] + 1
    line_h = 11.44
    box_top = 53.3
    box_bottom = 71.17 + max(n_lines - 1, 0) * line_h
    c.setStrokeColor(HexColor("#1A1A1A"))
    c.setLineWidth(1.0)
    c.roundRect(
        CONTENT_X0, PAGE_H - box_bottom, CONTENT_W, box_bottom - box_top, 1.7, stroke=1, fill=0
    )

    c.setFont(_SANS_B, 9.28)
    c.setFillColor(ID_INK)
    c.drawString(21.0, _ty(59.5, 9.28), case_id)

    for ln, lx, label, vx, value in placements:
        y_label, y_value = 61.2 + ln * line_h, 60.3 + ln * line_h
        c.setFont(_SANS_B, 5.41)
        c.setFillColor(ID_LABEL)
        c.drawString(lx, _ty(y_label, 5.41), label)
        c.setFont(_SANS_B, 6.96)
        c.setFillColor(ID_INK)
        c.drawString(vx, _ty(y_value, 6.96), value)

    return box_bottom + _SECTION_GAP


def _bhb_spouse_profile(app: dict) -> dict | None:
    """Better Half Benefit (BHB) is a rider on the Life Assured's own
    policy whose eligibility depends on the spouse's own profile — not a
    second insured life in the joint-life sense (engine.sbi_financial.
    check_bhb_eligibility: SA cap, spouse age, LA/spouse age gap, spouse
    qualification — qualification is a real SBI rule, Undewriting
    Guidelines for Smart Shield Plus V02.docx, BHB table: "Minimum
    Qualification for Beneficiary: Graduate"). Returns None when BHB isn't
    opted or no matching nominee record exists.

    education_level is read from agents/prompts/identity.txt's nominees[]
    schema; most source proposal forms only carry a "Qualification" field
    for the primary applicant, not the spouse, so "Not on file" is the
    correct and expected render for most BHB cases.

    share_pct (the nominee's benefit %, e.g. BHB SUM ASSURED's split) was
    already captured correctly by the extraction schema but never read
    into this function or shown on the BHB panel — added below."""
    bhb_sa = _safe_int(app.get("bhb_sum_assured"))
    if not bhb_sa:
        return None
    nominees = app.get("nominees") or []
    spouse = next(
        (
            n
            for n in nominees
            if (n.get("relation") or "").strip().lower() in ("wife", "husband", "spouse")
        ),
        None,
    )
    if not spouse:
        return None
    sp_name = _title_name(spouse.get("name")) if spouse.get("name") else "Not Available"
    sp_relation = (spouse.get("relation") or "Not Available").strip()
    sp_edu = spouse.get("education_level") or "Not on file"
    sp_age_txt = "Not Available"
    sp_dob = spouse.get("dob") or ""
    m_dob = re.match(r"(\d{4})-(\d{2})-(\d{2})", sp_dob)
    if m_dob:
        try:
            sy, sm, sd = (int(g) for g in m_dob.groups())
            anchor = app.get("submission_date") or ""
            m_anchor = re.match(r"(\d{4})-(\d{2})-(\d{2})", anchor)
            if m_anchor:
                ay, am, ad = (int(g) for g in m_anchor.groups())
            else:
                from datetime import date as _date

                _today = _date.today()
                ay, am, ad = _today.year, _today.month, _today.day
            sp_age_txt = str(ay - sy - ((am, ad) < (sm, sd)))
        except Exception:
            sp_age_txt = "Not Available"
    sp_share = spouse.get("share_pct")
    sp_share_txt = f"{sp_share}%" if sp_share is not None else "Not Available"
    return {
        "name": sp_name,
        "relation": sp_relation,
        "age": sp_age_txt,
        "edu": sp_edu,
        "bhb_sa": _inr(bhb_sa).replace("Rs. ", ""),
        "share_pct": sp_share_txt,
    }


def _draw_bhb_panel(c, decision: dict, top: float) -> float:
    """Separate bordered panel for the BHB (Better Half Benefit) spouse
    profile, own section — not folded into the Life Assured's ID-bar card
    — so a case with multiple lives to be underwritten (Life Assured +
    BHB) shows both profiles comprehensively and distinctly. A no-op
    (returns `top` unchanged) for cases where BHB isn't opted, same
    pattern as _draw_requirements_box's empty-list no-op."""
    app = decision.get("applicant") or {}
    spouse = _bhb_spouse_profile(app)
    if spouse is None:
        return top

    box_top = top
    # Same heading-offset rhythm as the Requirements box (_REQ_HEADING_
    # OFFSET / _REQ_FIRST_ITEM_OFFSET) — both are "bordered box, heading,
    # content below" panels, so they should read as using the same
    # vertical grid rather than each having its own hand-picked numbers.
    heading_top = box_top + _REQ_HEADING_OFFSET
    inner_x0 = ML + 6.3

    c.setFont(_SANS_B, 6.34)
    c.setFillColor(INK)
    c.drawString(inner_x0, _ty(heading_top, 6.34), "BHB / SECOND LIFE PROFILE")
    sub = "Better Half Benefit: rider on this policy, eligibility tied to spouse profile"
    c.setFont(_SANS, 4.43)
    c.setFillColor(MUTE)
    sub_w = pdfmetrics.stringWidth(sub, _SANS, 4.43)
    c.drawString(CONTENT_X1 - 6.3 - sub_w, _ty(heading_top, 4.43), sub)

    row_items = [
        ("NAME", spouse["name"]),
        ("RELATION", spouse["relation"]),
        ("AGE", spouse["age"]),
        ("EDUCATION", spouse["edu"]),
        ("BENEFIT %", spouse["share_pct"]),
        ("BHB SUM ASSURED", spouse["bhb_sa"]),
    ]
    row_top = heading_top + _REQ_FIRST_ITEM_OFFSET
    cursor = inner_x0
    line = 0
    placements = []
    for label, value in row_items:
        lx = cursor
        vx = lx + pdfmetrics.stringWidth(label, _SANS_B, 5.41) + 3.2
        field_end = vx + pdfmetrics.stringWidth(value, _SANS_B, 6.96)
        # Same inter-field gap (10.5) the Life Assured's own ID-bar fields
        # use (see first_x/cursor logic in _draw_id_bar above) — this used
        # to be a different, wider 16.0 gap here, making the two "LABEL:
        # value" field rows look inconsistently spaced even though they
        # sit one box apart on the page.
        if field_end > CONTENT_X1 - 6.3 and lx != inner_x0:
            line += 1
            lx = inner_x0
            vx = lx + pdfmetrics.stringWidth(label, _SANS_B, 5.41) + 3.2
            field_end = vx + pdfmetrics.stringWidth(value, _SANS_B, 6.96)
        placements.append((line, lx, label, vx, value))
        cursor = field_end + 10.5

    n_lines = placements[-1][0] + 1
    line_h = 11.44
    # row_top marks the TOP (_ty's reference point) of the field row's
    # text, not its visual bottom — same glyph-descender gap as the risk-
    # row dividers (_DIVIDER_MIN_CLEARANCE above): a flat "+1.5" left the
    # border drawn through the text itself instead of below it. Clear by
    # (approximately) the value font's own size instead.
    box_bottom = row_top + 6.96 + 2.0 + max(n_lines - 1, 0) * line_h

    c.setStrokeColor(HexColor("#1A1A1A"))
    c.setLineWidth(0.9)
    c.roundRect(
        CONTENT_X0, PAGE_H - box_bottom, CONTENT_W, box_bottom - box_top, 1.7, stroke=1, fill=0
    )

    for ln, lx, label, vx, value in placements:
        # Label (smaller font) and value (larger font) get baselines 0.9pt
        # apart, same as the ID bar's y_label/y_value split — drawing both
        # at the identical `top` reference left the label looking slightly
        # mis-aligned against the value next to it.
        y_label, y_value = row_top + ln * line_h + 0.9, row_top + ln * line_h
        c.setFont(_SANS_B, 5.41)
        c.setFillColor(ID_LABEL)
        c.drawString(lx, _ty(y_label, 5.41), label)
        c.setFont(_SANS_B, 6.96)
        c.setFillColor(ID_INK)
        c.drawString(vx, _ty(y_value, 6.96), value)

    return box_bottom + _SECTION_GAP


# ── decision bar ────────────────────────────────────────────────────────

# The decision-bar badge is explicitly scoped to "FINANCIAL & PROFILE RISK
# SUMMARY" (see the panel header below it) — medical-axis gates are a
# separate risk dimension shown nowhere on this dashboard, so they're
# excluded from the FAIL/WARN counts the same way the reference excludes
# the medical_compliance FAIL from its "4 FAIL" badge. sbi_piwc_requirement
# is excluded too: it's a near-universal WARN (no agent ever captures PIWC
# completion) whose evidence text references "Video MER" as a fallback
# verification method — medical-exam terminology with no place on a
# Financial UW case sheet. Suppressed entirely rather than reworded.
_NON_PROFILE_CRITERIA = {"medical_compliance", "fraud_signal", "sbi_piwc_requirement"}

# Distinct from _NON_PROFILE_CRITERIA above — these ARE genuine, in-scope
# SBI findings (unlike medical/fraud/PIWC), just ones whose Requirements-
# to-Raise box item already states the finding fully enough that a
# generic-fallback risk row underneath would only repeat it in different
# words rather than add anything (contrast sbi_hlv_remaining, whose own
# MEDIUM row below the box's terse "surrogate income" ask adds real
# numbers — HLV/remaining-eligibility figures — not in the box at all).
_REDUNDANT_WITH_REQUIREMENTS_BOX = {"sbi_pending_proposal_disclosure"}


def _fail_warn_counts(gating: list[dict]) -> tuple[int, int]:
    # Same sbi_qualification-passed exclusion as _build_risk_rows and
    # engine.requirements: once the authoritative SBI qualification gate
    # has PASSED, min_income_for_sa's FAIL is already suppressed
    # everywhere else on the sheet (no risk row, no requirement) —
    # leaving it in this badge count would show "1 FAIL" with no visible
    # reason anywhere on the page for what failed.
    exclude = set(_NON_PROFILE_CRITERIA)
    sq = next((g for g in gating if g.get("criterion") == "sbi_qualification"), None)
    if sq is not None and (sq.get("status") or "").upper() == "PASS":
        exclude.add("min_income_for_sa")
    scoped = [g for g in gating if g.get("criterion") not in exclude]
    n_fail = sum(1 for g in scoped if (g.get("status") or "").upper() == "FAIL")
    n_warn = sum(1 for g in scoped if (g.get("status") or "").upper() == "WARN")
    return n_fail, n_warn


def _primary_decision_criterion(decision: dict) -> tuple[str, dict] | None:
    """Picks the SAME primary finding _build_risk_rows would put first in
    the HIGH bucket (tailored criteria first, then the income-adequacy
    merge, then the first generic FAIL in gating order), but returns the
    raw (criterion, gating_item) pair instead of a rendered row — so the
    decision-bar sentence can be worded completely independently of the
    risk-table's own (unchanged, approved) wording."""
    gating = decision.get("gating") or []
    by_crit = {g.get("criterion"): g for g in gating}
    if (decision.get("verdict") or {}).get("label") == "DECLINE":
        drivers = " ".join((decision.get("verdict") or {}).get("decision_drivers") or []).lower()
        for g in gating:
            evidence = (g.get("evidence") or "").lower()
            if g.get("status") == "FAIL" and evidence and evidence in drivers:
                return g.get("criterion"), g
    for crit in _HIGH_CRITERIA:
        g = by_crit.get(crit)
        if g and (g.get("status") or "").upper() == "FAIL":
            return crit, g
    gq = by_crit.get("sbi_qualification") or by_crit.get("min_income_for_sa")
    if gq and (gq.get("status") or "").upper() == "FAIL":
        return gq.get("criterion"), gq
    # Same sbi_qualification-passed exclusion as _build_risk_rows /
    # _fail_warn_counts / engine.requirements: without it, this generic
    # fallback picks up min_income_for_sa's FAIL and the decision-bar
    # sentence ends up citing the superseded generic floor even though
    # no risk row or requirement anywhere else on the sheet still
    # mentions it.
    _sq_for_exclusion = by_crit.get("sbi_qualification")
    _exclude_here = set(_NON_PROFILE_CRITERIA)
    if _sq_for_exclusion is not None and (_sq_for_exclusion.get("status") or "").upper() == "PASS":
        _exclude_here.add("min_income_for_sa")
    for g in gating:
        if (g.get("status") or "").upper() == "FAIL" and g.get("criterion") not in _exclude_here:
            return g.get("criterion"), g
    for crit in _MEDIUM_CRITERIA:
        g = by_crit.get(crit)
        if g and (g.get("status") or "").upper() in ("WARN", "FAIL"):
            return crit, g
    for g in gating:
        if (g.get("status") or "").upper() == "WARN" and g.get(
            "criterion"
        ) not in _NON_PROFILE_CRITERIA:
            return g.get("criterion"), g
    return None


def _decision_justification(decision: dict) -> str:
    """One clear, plain-language sentence explaining WHY the decision is
    what it is, written for an underwriter/reviewer with no knowledge of
    the system internals, naming the requirement/rule that drove the
    decision. Deliberately NOT reused from the risk-table's own
    (unchanged, approved) compact wording — this is a full sentence for
    standalone reading, built from the underlying facts via its own
    templates."""
    verdict = decision.get("verdict") or {}
    label = (verdict.get("label") or "").upper()
    # For SBI cases, engine.verdict.make_verdict's STANDARD_ISSUANCE vs
    # FURTHER_REQUIREMENT split keys directly off whether any
    # requirements_to_raise item exists at all (any item, not just ones
    # flagged critical — see the matching note in
    # engine.verdict.make_verdict) — a signal _primary_decision_criterion
    # below (which only reads `gating`) never sees. Checking gating alone
    # can pick an unrelated WARN that happens to appear on both a
    # STANDARD_ISSUANCE and a FURTHER_REQUIREMENT case (e.g.
    # existing_cover_disclosed), producing an IDENTICAL reason-bar
    # sentence for two different verdicts and hiding the real reason. The
    # requirement text itself is already plain-language
    # (engine.requirements.compute_requirements_to_raise), safe to show
    # as-is.
    open_items = [
        r
        for r in (decision.get("requirements_to_raise") or [])
        if isinstance(r, dict) and r.get("text")
    ]
    # Gated to FURTHER_REQUIREMENT specifically — requirements_to_raise
    # can be non-empty on a REFER or DECLINE case too (verdict.py's
    # earlier DECLINE/REFER branches run before the SBI
    # requirements-check branch, so a case can be REFERed for a reason
    # that has nothing to do with whatever also happens to be sitting in
    # the Requirements box). Unconditionally returning open_items[0] here
    # would pick an unrelated Requirements-box item as "the reason" for
    # those cases. Also, when it IS the driver (FURTHER_REQUIREMENT),
    # repeating open_items[0]'s full text here would duplicate the
    # "Requirements to Raise" box verbatim one section below it —
    # collapsed to a short pointer instead of restating it.
    if open_items and label == "FURTHER_REQUIREMENT":
        n = len(open_items)
        return (
            f"This case has {n} outstanding requirement{'s' if n != 1 else ''} "
            f"before it can proceed — see ‘Requirements to Raise’ above for details."
        )
    # STANDARD_ISSUANCE short-circuit: a clean-accept case must not show
    # a leftover gating WARN/FAIL as its reason (alarming and directly
    # contradicting "Standard Issuance" right above it). STANDARD_ISSUANCE
    # means requirements_to_raise was empty when engine.verdict.
    # make_verdict decided (open_items is empty here too, by the same
    # logic) — but gating can still carry a WARN/FAIL that never rose to a
    # requirement (an existing-cover signal the engine judged not
    # requirement-worthy, a generic non-SBI cross-check, etc.).
    # _primary_decision_criterion below doesn't know the verdict was
    # already decided on a DIFFERENT, empty signal — it just picks the
    # most severe-looking leftover gating row and explains THAT instead,
    # which reads as "this is why it's Standard Issuance" when it's
    # actually unrelated noise the verdict didn't act on. Checked first,
    # before gating is ever consulted for wording.
    if label == "INCOMPLETE":
        return "Required evidence or underwriting processing is incomplete; issuance is blocked pending manual review."
    if label == "STANDARD_ISSUANCE":
        return "No significant issues were found — the application meets this product's eligibility requirements."
    picked = _primary_decision_criterion(decision)
    if not picked:
        if label == "STANDARD_ACCEPTANCE":
            return "No significant issues were found — the application meets this product's eligibility requirements."
        return "See the Financial & Profile Risk Summary below for the supporting details."
    crit, g = picked
    ev = g.get("evidence") or ""
    bureau = decision.get("sbi_bureau") or {}
    calcs = decision.get("calculations") or {}

    if crit == "sbi_material_non_disclosure":
        policies = bureau.get("iib_policies") or []
        bad = ("declin", "postpone", "rated up", "rate up")
        flagged = [
            p
            for p in policies
            if any(t in (p.get("status") or "").lower() for t in bad)
            or any(t in (p.get("uw_decision") or "").lower() for t in bad)
        ]
        if flagged:
            p = flagged[0]
            insurer = _short_insurer(p.get("insurer"))
            status_word = (
                "declined"
                if "declin" in (p.get("status") or "").lower()
                else "postponed"
                if "postpone" in (p.get("status") or "").lower()
                else "rated up"
            )
            date = p.get("uw_decision_date") or ""
            when = f" in {date[:7]}" if date else ""
            return (
                f"A previous insurance proposal for this applicant with {insurer} was {status_word}"
                f"{when}, and this has not been explained or resolved yet."
            )
        return "A previous insurance proposal for this applicant was declined or postponed, and this has not been explained or resolved yet."

    if crit == "sbi_iib_gate":
        return (
            "This applicant's existing insurance policies, combined with this new proposal, "
            "add up to more cover than is allowed for this applicant's risk profile."
        )

    if crit in ("sbi_qualification", "min_income_for_sa", "sbi_drc_income_floor"):
        if "disallowed list" in ev.lower() or "not an acceptable combination" in ev.lower():
            # Don't restate the qualification finding here — the HIGH
            # Qualification row directly below already states it in full
            # (same don't-duplicate pattern as disallowed_petty_business_
            # profile / existing_cover_disclosed above).
            return (
                "This applicant's education level does not meet this product's qualification "
                "requirement — see the Qualification row below for the specific finding."
            )
        verified = _cv(calcs, "selected_income")
        return (
            f"The applicant's verified annual income (Rs. {_inr(verified).replace('Rs. ', '')}) is lower "
            f"than the minimum income required for the cover amount requested."
        )

    if crit == "sa_within_hlv":
        return "The cover amount requested is higher than what the income-based calculation supports for this applicant."

    if crit == "sbi_rider_stacked_exposure":
        return "The combined cover from the base policy and its riders is higher than allowed based on this applicant's income."

    if crit == "existing_cover_disclosed":
        # Unlike the income-adequacy template above (which always embeds
        # the verified figure), this one was a bare claim with no number
        # — inconsistent with the rest of the decision bar, and leaves an
        # underwriter unable to judge materiality without scrolling to
        # the MEDIUM risk row below. Mirrors _row_existing_cover's own
        # in-force-SA computation so the two stay consistent.
        bureau = decision.get("sbi_bureau") or {}
        policies = bureau.get("iib_policies") or []
        in_force_sa = sum(
            _safe_int(p.get("sum_assured_inr")) or 0
            for p in policies
            if any(
                t in (p.get("status") or "").lower()
                for t in ("inforce", "in force", "paid-up", "paid up")
            )
        )
        if in_force_sa > 0:
            return (
                f"This applicant has an undeclared existing life insurance policy "
                f"({_inr(in_force_sa)} in force) that was not declared on the proposal form."
            )
        # No bureau SA figure to cite (e.g. a bank-statement-detected
        # recurring premium debit rather than an IIB record) — point to
        # the specific finding below rather than assert a bare, unevidenced
        # claim with no number to back it.
        return (
            "This applicant has existing life insurance cover that was not declared on the "
            "proposal form — see the Existing-cover disclosure row below for the specific finding."
        )

    if crit == "sbi_occupation_income_proof":
        # This criterion's FAIL covers two unrelated findings that share
        # one gating row: a disallowed petty-business occupation (a fixed,
        # terminal fact about who the applicant is — proof on file is
        # irrelevant) vs. standard income proof genuinely missing for an
        # otherwise-acceptable occupation. Using the income-proof wording
        # for the former is actively wrong when proof IS on file (e.g. a
        # disallowed petty-business occupation with ITR on file — the
        # DECLINE reason is the occupation category, not missing proof,
        # and the HIGH-risk row directly below already states that real
        # reason). The gate's own evidence text distinguishes the two
        # (see engine.sbi_financial.check_occupation_income_proof).
        if "disallowed petty-business profile" in ev.lower():
            # Point to the Occupation/income-proof risk row below rather
            # than restate its exact evidence text here — same
            # don't-duplicate pattern as existing_cover_disclosed above.
            return (
                "This applicant's occupation is in a disallowed petty-business category for this "
                "product — see the Occupation / income-proof row below for the specific finding."
            )
        return "This applicant's occupation requires standard income proof, which has not been provided."

    if crit == "income_documented":
        return "The declared income for this applicant is not supported by any salary slip, Form-16, ITR, or CA computation."

    if crit == "sbi_cibil_score":
        return "This applicant's income could not be verified, and the CIBIL-based alternative route is not available for this case."

    if crit == "kyc":
        # ev is specific when engine.sbi_financial._upgrade_kyc_evidence_
        # with_specifics has run (e.g. "Missing from the document set:
        # Photo ID ..., address proof.") — show that directly rather than
        # a generic "incomplete" sentence that names no actual gap. Falls
        # back to the generic phrasing only if that upgrade didn't apply
        # (non-SBI flow, or no kyc_evidence on file).
        if ev.lower().startswith("missing from the document set"):
            return f"This applicant's KYC documentation is incomplete — {ev[0].lower()}{ev[1:]}"
        return "This applicant's KYC documentation is incomplete."

    if crit == "sbi_bhb_eligibility":
        # The gating evidence itself (shown verbatim in the MEDIUM risk
        # row below) is a dense, semicolon-separated checklist dump —
        # appropriate for that detail row, not for the one-sentence
        # decision-bar headline.
        status = (g.get("status") or "").upper()
        if status == "FAIL":
            return (
                "This applicant's Better Half Benefit (spouse) rider does not meet one or more "
                "of this product's eligibility conditions."
            )
        return (
            "This applicant's Better Half Benefit (spouse) rider is provisionally eligible, but "
            "one condition could not be verified from the documents on file and needs confirmation."
        )

    # Fallback for any criterion without a dedicated plain-language
    # template above: strip the raw evidence of internal-reading
    # phrasing (parenthetical asides, "disallowed list"/"not an
    # acceptable combination" legalese already handled above) rather
    # than show it verbatim.
    clean = re.sub(r"\s*\[.*?\]\s*", " ", ev).strip()
    return clean or "See the Financial & Profile Risk Summary below for the supporting details."


def _draw_decision_bar(c, decision: dict, top: float) -> float:
    verdict = decision.get("verdict") or {}
    label = (verdict.get("label") or "REFER").upper()
    # STANDARD_ISSUANCE / FURTHER_REQUIREMENT are SBI's own decision terms
    # (engine.verdict.make_verdict, SBI-case-only). STANDARD_ACCEPTANCE /
    # CONDITIONAL are kept here only as a defensive fallback label — this
    # renderer only ever receives SBI decisions in practice.
    label_map = {
        "STANDARD_ISSUANCE": "STANDARD ISSUANCE",
        "FURTHER_REQUIREMENT": "FURTHER REQUIREMENT",
        "STANDARD_ACCEPTANCE": "STANDARD ACCEPTANCE",
        "CONDITIONAL": "CONDITIONAL ACCEPT",
        "REFER": "REFER TO MANUAL UW",
        "DECLINE": "DECLINE",
        "ERROR": "REVIEW REQUIRED",
        "INCOMPLETE": "INCOMPLETE - MANUAL REVIEW",
    }
    decision_text = label_map.get(label, label.replace("_", " "))
    # Three-tier colour, not two: a flat green on every STANDARD_ISSUANCE
    # case was misleading when that same case still has a HIGH or MEDIUM
    # risk row below it (e.g. income below the floor) — STANDARD_ISSUANCE
    # only means "no CRITICAL requirement blocking issuance", not "no
    # risk factors at all" (see engine.verdict.make_verdict). Green only
    # for a genuinely clean case (zero HIGH/MEDIUM rows), amber for
    # STANDARD_ISSUANCE with risk items still noted, red unchanged for
    # FURTHER_REQUIREMENT/REFER/DECLINE.
    is_accept = label in ("STANDARD_ISSUANCE", "STANDARD_ACCEPTANCE")
    if is_accept:
        _risk_rows = _build_risk_rows(decision)
        has_risk_items = bool(_risk_rows.get("HIGH") or _risk_rows.get("MEDIUM"))
        if has_risk_items:
            bar_color = AMBER
            badge_light = HexColor("#F2DCB8")
        else:
            bar_color = GREEN
            badge_light = HexColor("#C9E8D1")
    else:
        bar_color = RED
        badge_light = HexColor("#F5C9CC")
    gating = decision.get("gating") or []
    n_fail, n_warn = _fail_warn_counts(gating)

    # Every y-coordinate below is the reference's exact measurement for a
    # single-line ID bar (top=75.7 going in). When the ID bar above wraps
    # to a 2nd line (long OCC/employer combos, etc.) it returns a larger
    # `top`, and this whole bar shifts down by that same delta rather
    # than sitting at its old fixed position and overlapping the taller
    # card above it.
    dy = max(0.0, top - 75.7)

    bar_top = 74.62 + dy
    badge_bottom = 95.95 + dy

    # Justification strip — band below the badge row. Wrapped to as many
    # lines as it needs; the bar's bottom (and everything below) shifts
    # down to fit. Text size matches _RISK_BODY_SIZE, the same size used
    # for risk-row title/desc text, so the decision's own reason reads at
    # the same weight as the findings it's explaining. just_lead reuses
    # _RISK_TITLE_LEAD (the matching line-pitch for that same font size)
    # rather than a separate hand-picked value.
    #
    # Suppressed entirely on FURTHER_REQUIREMENT — for that one verdict,
    # the sentence would be a third restatement of the same fact already
    # shown twice (the Requirements-to-Raise box's own items, and the
    # badge's matching count). DECLINE/REFER/STANDARD_ISSUANCE keep it —
    # for those it's the ONLY place the reason appears (DECLINE's box is
    # itself suppressed; REFER and STANDARD_ISSUANCE never had one).
    _skip_justification = label == "FURTHER_REQUIREMENT"
    just_text = "" if _skip_justification else _decision_justification(decision)
    just_x0, just_x1 = 21.6, CONTENT_X1 - 6.0
    just_lines = (
        []
        if _skip_justification
        else _wrap(just_text, _SANS, _RISK_BODY_SIZE, just_x1 - just_x0, max_lines=4)
    )
    just_lead = _RISK_TITLE_LEAD
    just_top = badge_bottom + 3.4
    # FURTHER_REQUIREMENT has no justification text (see _skip_justification
    # above), so close the box right at the badge instead of leaving a bare
    # empty strip under the "N CRITERIA NOT MET" badge, and skip the inner
    # divider line that would otherwise separate the badge from the
    # (now absent) justification text below it.
    bar_bottom = (
        badge_bottom if _skip_justification else just_top + len(just_lines) * just_lead + 2.6
    )
    bar_h = bar_bottom - bar_top

    c.setStrokeColor(bar_color)
    c.setLineWidth(1.15)
    c.rect(CONTENT_X0, PAGE_H - bar_bottom, CONTENT_W, bar_h, stroke=1, fill=0)
    if not _skip_justification:
        c.setStrokeColor(bar_color)
        c.setLineWidth(0.58)
        c.line(CONTENT_X0, PAGE_H - badge_bottom, CONTENT_X1, PAGE_H - badge_bottom)

    # badge_x1 (and everything positioned to its right) must grow with the
    # decision-label font size instead of staying fixed — "REFER TO MANUAL
    # UW" at this font otherwise runs past a hardcoded box edge and clips
    # to "REFER TO MANUAL". Measure the actual text and only widen the box
    # (and shift what follows) when the longest label needs it; short
    # labels like "DECLINE" keep the original, tighter box.
    badge_x0 = 15.84
    decision_text_w = pdfmetrics.stringWidth(decision_text, _SANS_B, 10.84)
    badge_x1 = max(131.68, 21.6 + decision_text_w + 9.0)
    badge_shift = badge_x1 - 131.68
    c.setFillColor(bar_color)
    c.rect(badge_x0, PAGE_H - (94.79 + dy), badge_x1 - badge_x0, 94.79 - 75.78, stroke=0, fill=1)
    c.setFont(_SANS, 5.41)
    c.setFillColor(badge_light)
    c.drawString(21.6, _ty(79.6 + dy, 5.41), "DECISION")
    c.setFont(_SANS_B, 10.84)
    c.setFillColor(WHITE)
    c.drawString(21.6, _ty(84.0 + dy, 10.84), decision_text)

    # "N CRITERIA NOT MET" count badge — shown ONLY on FURTHER_REQUIREMENT:
    # every other verdict is driven by exactly ONE reason, stated in the
    # sentence below, and a count next to it would misread as "N problems"
    # either way it was sourced — the raw gating FAIL+WARN tally (e.g. "11
    # criteria not met" next to a DECLINE whose own sentence names a
    # single disallowed-qualification fact) or the Requirements-box count
    # (STANDARD_ISSUANCE cases can carry real gating WARNs — e.g. an
    # undeclared-existing-cover finding — that never became a requirement;
    # showing "4 criteria not met" on a clean-accept decision reads as a
    # direct contradiction). FURTHER_REQUIREMENT is the one verdict where
    # the count is both genuine (requirements_to_raise is always
    # non-empty there — see engine.verdict.make_verdict's is_sbi_case
    # branch) and independently checkable against the Requirements-to-Raise
    # box right above it.
    if label == "FURTHER_REQUIREMENT":
        n_not_met = len(decision.get("requirements_to_raise") or [])
        c.setFont(_SANS_B, 11.59)
        c.setFillColor(bar_color)
        c.drawString(142.9 + badge_shift, _ty(79.0 + dy, 11.59), str(n_not_met))
        c.setFont(_SANS, 4.64)
        c.setFillColor(MUTE2)
        # Single line, not stacked — plenty of clear width to the right
        # (same proven y=88.1 clearance under the number the old single-
        # word "FAIL"/"WARN" caption used); a two-line stack left too
        # little vertical gap under the number and visually collided
        # with it.
        c.drawString(141.2 + badge_shift, _ty(88.1 + dy, 4.64), "CRITERIA NOT MET")
    else:
        c.setFont(_SANS_B, 7.0)
        c.setFillColor(bar_color)
        c.drawString(141.2 + badge_shift, _ty(83.0 + dy, 7.0), f"{n_fail} RULE FAIL")
        c.drawString(212.0 + badge_shift, _ty(83.0 + dy, 7.0), f"{n_warn} RULE REVIEW")

    # "QC CONCURS" was previously hardcoded here unconditionally — no
    # field anywhere in the case/decision data represents an actual
    # external QC/underwriter check to concur with, so it printed on
    # every single case sheet regardless of outcome, fabricating
    # agreement from a QC review that never happened. Removed.
    #
    # "STP BLOCKED"/"STP CLEAR" was also removed here: STP
    # (Straight-Through Processing) is a generic-engine reconciler
    # concept with no grounding in any SBI reference document
    # (Case sheet format.docx, Underwriting Guidelines for Smart Shield
    # Plus V02.docx, SBI_Financial_Categories.docx) or the actual QC
    # Excel's own decision vocabulary.

    # No "WHY:" label — keep the reason, drop the label, in plain
    # language so it reads as a normal sentence
    # directly under the decision rather than a system-style annotation.
    c.setFont(_SANS, _RISK_BODY_SIZE)
    c.setFillColor(INK)
    jy = just_top
    for ln in just_lines:
        c.drawString(just_x0, _ty(jy + 2.0, _RISK_BODY_SIZE), ln)
        jy += just_lead

    return bar_bottom + _SECTION_GAP


# ── risk summary ────────────────────────────────────────────────────────

# Known-criterion display templates: criterion -> (short_title, action,
# bucket_status_filter). Covers the SBI Smart Shield Plus engine's own
# gating criteria; anything else falls back to a generic row (see
# _generic_risk_row) so the panel degrades gracefully for other cases.
_PRODUCT_SHORT = {
    "pure term assurance": "term",
    "endowment (including money back & whole life type)": "endowment",
    "any other product": "policy",
}


def _short_product(p: str) -> str:
    return _PRODUCT_SHORT.get(
        (p or "").strip().lower(), (p or "policy").split("(")[0].strip().lower() or "policy"
    )


def _short_insurer(name: str) -> str:
    """Drop corporate-suffix boilerplate ('Insurance Company Ltd.') so
    dense dashboard cells don't wrap/truncate on a long legal name —
    matches the reference's own abbreviation ('SBI Life Insurance
    Company Ltd.' -> 'SBI Life')."""
    n = (name or "").strip()
    if not n:
        return "Not Available"
    if "sbi life" in n.lower():
        return "SBI Life"
    n = re.sub(r"\s+Insurance\s+Company\s+Ltd\.?$", "", n, flags=re.I)
    n = re.sub(r"\s+Life\s+Insurance\s+Co\.?\s*Ltd\.?$", "", n, flags=re.I)
    return n


def _display_employer(decision: dict) -> str | None:
    """Prefer one consistent payslip employer; preserve the proposal field."""
    app = decision.get("applicant") or {}
    income = (decision.get("extraction_blocks") or {}).get("income") or {}
    employers = {
        str(s.get("employer")).strip()
        for s in income.get("salary_slips") or []
        if isinstance(s, dict) and s.get("employer")
    }
    if "salaried" in str(app.get("occupation") or "").lower() and len(employers) == 1:
        return next(iter(employers))
    return app.get("employer_name")


def _display_nominees(app: dict) -> list[dict]:
    """Show all people; suppress exact repeated source mentions, not conflicts."""
    candidates = [n for n in app.get("nominees") or [] if isinstance(n, dict)]

    def identity(n):
        name = re.sub(r"^(?:mrs|mr|ms|miss)(?:\.\s*|\s+)", "", str(n.get("name") or ""), flags=re.I)
        return (re.sub(r"\W+", "", name).casefold(), str(n.get("relation") or "").casefold())

    output = []
    seen = set()
    for nominee in candidates:
        person = identity(nominee)
        if nominee.get("share_pct") is None and any(
            identity(n) == person and n.get("share_pct") is not None for n in candidates
        ):
            continue
        key = (person, nominee.get("share_pct"), nominee.get("dob"))
        if key not in seen:
            output.append(nominee)
            seen.add(key)
    return output


def _title_name(name: str) -> str:
    """Normalise an extracted applicant/nominee name for display — source
    data is sometimes all-lowercase or mixed-case from OCR (e.g.
    'Mrs.shalilu ahuja')."""
    n = (name or "").strip()
    if not n or n == "Not Available":
        return "Not Available"
    n = re.sub(r"^(Mrs?|Ms|Dr)\.(?=\S)", r"\1. ", n, flags=re.I)
    return " ".join(w if w.lower() in ("&",) else w[:1].upper() + w[1:] for w in n.split())


def _row_material_non_disclosure(decision, g):
    bureau = decision.get("sbi_bureau") or {}
    # Excludes "withdraw" — see engine.sbi_financial.check_material_non_disclosure
    # and the _build_iib_col PRIOR DECLINE field for the same fix/rationale.
    # Kept as its own independent copy of the same filter list so this
    # risk row stays consistent with those other call sites.
    bad_terms = ("declin", "postpone", "rated up", "rate up")
    flagged = [
        p
        for p in (bureau.get("iib_policies") or [])
        if any(t in (p.get("status") or "").lower() for t in bad_terms)
        or any(t in (p.get("uw_decision") or "").lower() for t in bad_terms)
    ]
    if not flagged:
        return None
    p = flagged[0]
    date = p.get("uw_decision_date") or ""
    mon_yyyy = date
    m = re.match(r"(\d{4})-(\d{2})", date or "")
    if m:
        months = [
            "",
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ]
        mon_yyyy = f"{months[int(m.group(2))]}-{m.group(1)}"
    iib_bit = (
        f"{_short_insurer(p.get('insurer'))} "
        f"{_inr(p.get('sum_assured_inr')).replace('Rs. ', '')} {_short_product(p.get('product_type'))} "
        f"{(p.get('status') or '').upper()}{' ' + mon_yyyy if mon_yyyy else ''}"
    )
    # "cannot confirm" (the proposal form's own disclosure answer wasn't
    # extracted) is a DIFFERENT, weaker claim than an actually-confirmed
    # contradiction (declared No vs bureau shows adverse) — asserting the
    # contradiction when we don't actually know their answer overstates
    # the finding, so the WARN ("cannot confirm") case gets its own
    # accurate wording rather than reusing the confirmed-contradiction
    # phrasing.
    cannot_confirm = "cannot confirm" in (g.get("evidence") or "").lower()
    desc = (
        f"IIB: {iib_bit} — proposal form's own disclosure answer not captured"
        if cannot_confirm
        else f"Declared: no prior decline ≠ IIB: {iib_bit}"
    )
    # Rule 2: an adverse SBI Life record is highlighted only — no customer
    # letter is requested for it (see engine.requirements rule 1/2). A
    # non-SBI adverse record IS a real documentary-evidence ask (rule 1).
    action = (
        "Highlighted — no customer letter required"
        if "sbi life" in (p.get("insurer") or "").lower()
        else "Decline letter + explanation"
    )
    bold = ("IIB:",) if cannot_confirm else ("Declared:", "IIB:")
    return ("Prior-decline disclosure", desc, action, bold)


def _row_iib_gate(decision, g):
    ev = g.get("evidence") or ""
    n1 = re.search(r"(\d+)\s+SBI Life policy\(ies\) with disqualifying UW decision", ev)
    n2 = re.search(r"(\d+)\s+IIB policy\(ies\) with disqualifying proposal status", ev)
    n3 = re.search(r"combined IIB\+SBI Life SA ₹([\d,]+) exceeds cap ₹([\d,]+)", ev)
    bits = []
    bold_terms = []
    if n1:
        bits.append(f"{n1.group(1)} SBI Life policy with disqualifying UW decision")
        bold_terms.append("UW decision")
    if n2:
        bits.append(f"{n2.group(1)} with disqualifying proposal status")
        bold_terms.append("proposal status")
    if n3:
        bits.append(
            f"combined SA {_inr(n3.group(1).replace(',', ''))} exceeds cap {_inr(n3.group(2).replace(',', ''))}"
        )
        bold_terms.append("exceeds cap")
    desc = " + ".join(bits) if bits else ev
    return ("IIB aggregation", desc, "Reconcile disqualifying bureau records", tuple(bold_terms))


def _row_income_adequacy(decision, calcs, g, drc_gate=None):
    verified = _cv(calcs, "selected_income")
    m = re.search(r"(?:required|floor) ₹([\d,]+)", g.get("evidence") or "")
    floor = m.group(1).replace(",", "") if m else None
    # When sbi_drc_income_floor ALSO fails for the same case, fold it into
    # this same row instead of letting it fall through to a separate
    # generic-fallback HIGH row — two rows that both just say "income is
    # too low" (citing different thresholds) would read as repetitive to
    # a client-facing reviewer.
    drc_floor = None
    if drc_gate and (drc_gate.get("status") or "").upper() == "FAIL":
        dm = re.search(r"required minimum ₹([\d,]+)", drc_gate.get("evidence") or "")
        if dm:
            drc_floor = dm.group(1).replace(",", "")
    if drc_floor:
        desc = (
            f"Verified {_inr(verified).replace('Rs. ', '')} is below both the "
            f"{_inr(floor).replace('Rs. ', '') if floor else 'Not Available'} SA-band/qualification minimum "
            f"and the {_inr(drc_floor).replace('Rs. ', '')} High-Risk DRC minimum"
        )
    else:
        desc = (
            f"Verified {_inr(verified).replace('Rs. ', '')} < both "
            f"{_inr(floor).replace('Rs. ', '') if floor else 'Not Available'} minimums (SA-band & qualification)"
        )
    return ("Income adequacy", desc, "Fresh proof or SA reduction", ())


def _row_existing_cover(decision, g):
    bureau = decision.get("sbi_bureau") or {}
    policies = bureau.get("iib_policies") or []
    in_force_sa = sum(
        _safe_int(p.get("sum_assured_inr")) or 0
        for p in policies
        if any(
            t in (p.get("status") or "").lower()
            for t in ("inforce", "in force", "paid-up", "paid up")
        )
    )
    if in_force_sa <= 0:
        # Same fix as engine.requirements' existing-cover rule: this
        # WARN/FAIL can come from a non-IIB source (a reconciler
        # bank-statement signal with no bureau SA figure) — "₹0 in-force
        # undeclared policies" is actively misleading in that case,
        # reading as "nothing found" when the opposite is true.
        # Summarised to its first sentence rather than the full evidence
        # paragraph — this same finding's full narrative is already shown
        # in the "Requirements to Raise" box above when it rose to a
        # requirement, so repeating all of it here too both duplicates
        # that text and — being long — regularly overran the row's
        # 3-line wrap and ended mid-word in an ellipsis.
        ev_full = _redact_pii(g.get("evidence")) or "Not Available"
        ev_short = (
            ev_full.split(". ")[0].rstrip(".") + "." if ev_full != "Not Available" else ev_full
        )
        return ("Existing-cover disclosure", ev_short, "Re-declaration required", ())
    extras = []
    if any(
        "freelook" in (p.get("status") or "").lower()
        or "cooling off" in (p.get("status") or "").lower()
        for p in policies
    ):
        extras.append("freelook-cancelled")
    if any("surrender" in (p.get("status") or "").lower() for p in policies):
        extras.append("surrendered")
    extra_txt = (" + " + " + ".join(extras) + " policies") if extras else " policies"
    ecd = ((decision.get("extraction_blocks") or {}).get("compliance") or {}).get(
        "existing_cover_declared"
    ) or {}
    declared = ecd.get("total_life_cover_other")
    if declared is not None and _safe_int(declared) >= in_force_sa:
        desc = f"Declared {_inr(declared)}; bureau in-force {_inr(in_force_sa)}. Reconcile policy-level disclosure."
        return ("Existing-cover reconciliation", desc, "Confirm policy details", ())
    desc = f"Bureau in-force {_inr(in_force_sa)}; declared {_inr(declared) if declared is not None else 'not captured'}."
    return ("Existing-cover disclosure", desc, "Confirm policy details", ())


def _row_rider_exposure(decision, calcs, g):
    app = decision.get("applicant") or {}
    total_ad = sum(
        _safe_int(app.get(k)) or 0 for k in ("sum_assured", "adb_sum_assured", "bhb_sum_assured")
    )
    hlv = _cv(calcs, "hlv_base")
    desc = (
        f"Total AD payout {_inr(total_ad).replace('Rs. ', '')} > "
        f"HLV {_inr(hlv).replace('Rs. ', '')}"
    )
    return ("Total Rider Sum at Risk", desc, "Advisory review", ())


def _row_cibil_score(decision, g):
    # Dedicated tailored row (not the generic fallback, which always
    # colours embedded comparison operators red) — this gate's WARN
    # status with a "CIBIL score X ≥ Y" phrasing specifically means the
    # score genuinely CLEARED the bar (engine.sbi_financial.
    # check_cibil_gate only ever writes that phrasing in its score_i >=
    # min_score branch); the row is still MEDIUM-risk only because loan
    # details are separately unconfirmed, not because the score itself is
    # a problem. Marking it favorable so "≥" renders green, not red —
    # the always-red convention otherwise reads as the passing score
    # being the issue. The "no CIBIL record found" WARN variant has no
    # comparison operator in its text at all, so this flag is harmless
    # there too.
    return (
        g.get("label") or "CIBIL score eligibility",
        g.get("evidence") or "",
        "Review required",
        (),
        True,
    )


def _row_medical_grid(decision, g):
    # Dedicated tailored row (not the generic 3-slot-capped fallback) —
    # follows the SBI guideline's own Medical Grid (Undewriting
    # Guidelines for Smart Shield Plus V02.docx) so mandatory medical
    # tests are surfaced on the sheet whenever they apply (SA ≥ ₹25L).
    # See engine.sbi_financial.check_medical_requirement.
    return ("Mandatory medical tests", g.get("evidence") or "", "Confirm completion", ())


def _row_occupation(decision, g):
    app = decision.get("applicant") or {}
    occ = (app.get("occupation") or "").strip()
    if not occ or not any(t in occ.lower() for t in ("self", "business", "proprietor")):
        return None
    # When sbi_occupation_income_proof already FAILed (disallowed
    # petty-business profile — its own HIGH row states the occupation,
    # business name, and business nature together as the specific
    # finding), repeating business_nature here duplicates that row rather
    # than adding anything — this row's own job is just the generic
    # self-employed-income caution, not a second statement of the same
    # disallowed-trade finding.
    occ_gate = next(
        (
            gi
            for gi in (decision.get("gating") or [])
            if gi.get("criterion") == "sbi_occupation_income_proof"
        ),
        None,
    )
    occ_gate_disallowed = (
        occ_gate is not None
        and occ_gate.get("status") == "FAIL"
        and "disallowed petty-business profile" in (occ_gate.get("evidence") or "").lower()
    )
    nature = "" if occ_gate_disallowed else (app.get("business_nature") or "").strip()
    occ_txt = f"{occ} ({nature})" if nature else occ
    desc = f"{occ_txt} · variable income · no fixed-salary anchor"
    return ("Occupation type", desc, "Hard income proof", ())


_HIGH_CRITERIA = {
    "sbi_material_non_disclosure": _row_material_non_disclosure,
    "sbi_iib_gate": _row_iib_gate,
    "sbi_medical_grid": _row_medical_grid,
}
# sbi_medical_grid's own status is WARN, not FAIL (it's "unconfirmed",
# not a confirmed failure — the applicant may already have completed the
# tests, just not evidenced in the document set) — but the full test
# list is exactly what an underwriter needs to see prominently, not
# buried in MEDIUM, so the test names surface in the HIGH section.
# Scoped to this one criterion rather than loosening the HIGH loop's
# FAIL-only rule generally.
_HIGH_WARN_OK = {"sbi_medical_grid"}
_MEDIUM_CRITERIA = {
    "existing_cover_disclosed": _row_existing_cover,
    "sbi_rider_stacked_exposure": _row_rider_exposure,
    "sbi_cibil_score": _row_cibil_score,
    # WARN (not just FAIL) also needs a guaranteed slot — a WARN here
    # means "a prior adverse bureau record exists but we can't confirm
    # whether the applicant's own disclosure answer contradicts it," which
    # is still the single most decision-relevant fact on cases like this.
    # Previously only wired for FAIL via _HIGH_CRITERIA, so the WARN case
    # had to win one of 3 generic-fallback MEDIUM slots and often lost,
    # leaving a correctly-DECLINEd case with no visible trace of the
    # prior-decline record that drove it.
    "sbi_material_non_disclosure": _row_material_non_disclosure,
}


def _generic_row(g):
    # No pre-truncation here — _draw_risk_section already wraps the title
    # to 2 real (pixel-measured) lines via _wrap(..., _COL_TITLE_W,
    # max_lines=2), which fits most labels cleanly. A crude 34-char cutoff
    # applied here first was firing BEFORE that proper wrap ever got a
    # chance, producing ugly mid-word cuts like "Occupation / income-proof
    # require…" even though the untruncated label fits in 2 lines just
    # fine.
    title = (g.get("label") or g.get("criterion") or "Review item").split("(")[0].strip()
    return (title, _redact_pii(g.get("evidence")) or "", "Review required", ())


def _build_risk_rows(decision: dict) -> dict:
    calcs = decision.get("calculations") or {}
    gating = decision.get("gating") or []
    by_crit = {g.get("criterion"): g for g in gating}

    # Specific-criterion mappings (both HIGH's and MEDIUM's) are resolved
    # FIRST, before either bucket's generic fallback runs — otherwise a
    # criterion whose status escalates beyond what its tailored builder's
    # own check expected (e.g. existing_cover_disclosed hitting FAIL on
    # critical severity, not just its usual WARN) gets claimed by the
    # HIGH generic fallback first (since that loop runs over ALL FAIL
    # gating items) and never reaches its tailored MEDIUM formatter.
    high, medium, used = [], [], set()
    for crit, fn in _HIGH_CRITERIA.items():
        g = by_crit.get(crit)
        ok_statuses = ("FAIL", "WARN") if crit in _HIGH_WARN_OK else ("FAIL",)
        if g and (g.get("status") or "").upper() in ok_statuses:
            row = fn(decision, g) if fn.__code__.co_argcount == 2 else fn(decision, calcs, g)
            if row:
                high.append(row)
                used.add(crit)

    # _row_income_adequacy assumes a numeric income floor — it should NOT
    # fire for a qualification/occupation MISMATCH (disallowed list, or
    # the Agriculturist-specific Graduate-minimum check), which has no
    # floor value to report at all (that case has no "required ₹X"
    # figure, so building this row for it produces broken text like
    # "< both — minimums"). A mismatch-type sbi_qualification instead
    # falls through to the generic fallback below (clean raw evidence
    # text) — but that means min_income_for_sa, a GENUINE floor check,
    # must be picked explicitly rather than only via an `A or B` pattern
    # that always prefers sbi_qualification when present, which would
    # silently orphan min_income_for_sa into the SAME generic fallback
    # with its own un-stripped "[generic cross-check...]" caveat text and
    # a misleading truncated title.
    def _is_floor_type(g):
        ev = (g.get("evidence") or "").lower()
        return "disallowed list" not in ev and "not an acceptable combination" not in ev

    sq = by_crit.get("sbi_qualification")
    mi = by_crit.get("min_income_for_sa")
    income_doc_g = by_crit.get("income_documented")
    # When income is undocumented (declared-only, or nothing extracted at
    # all), the "verified X < floor Y" framing is misleading — X isn't
    # really verified, it's just whatever unverified figure the engine
    # had to fall back to, and the floor comparison restates the SAME
    # underlying problem income_documented's own row already states more
    # accurately ("no proof on file"). Suppress the floor-comparison row
    # entirely here (claim both criteria with no row) rather than show a
    # redundant or — when no number exists at all — broken ("< both —
    # minimums") row alongside it.
    income_undocumented = (
        income_doc_g is not None and (income_doc_g.get("status") or "").upper() == "FAIL"
    )
    sq_fail_floor = (
        sq is not None and (sq.get("status") or "").upper() == "FAIL" and _is_floor_type(sq)
    )
    # Mirror engine.verdict.make_verdict / engine.requirements' own
    # exclusion: once sbi_qualification (the authoritative SBI income
    # floor) has PASSED, min_income_for_sa's FAIL — a generic, explicitly
    # non-SBI-specific cross-check — must not surface as an "Income
    # adequacy" HIGH row either, or this risk summary contradicts the
    # verdict sitting right above it (e.g. sbi_qualification PASSED on a
    # Postgraduate floor while this row still cited the superseded,
    # lower generic floor, next to a STANDARD ISSUANCE decision). Only
    # suppresses mi_fail when sq is an authoritative PASS — a
    # WARN/NA/missing/mismatch-FAIL sbi_qualification still lets a
    # genuine min_income_for_sa FAIL surface normally.
    sq_authoritative_pass = sq is not None and (sq.get("status") or "").upper() == "PASS"
    mi_fail = (
        mi is not None and (mi.get("status") or "").upper() == "FAIL" and not sq_authoritative_pass
    )
    # Suppressing mi_fail above stops the TAILORED row from firing, but
    # min_income_for_sa's raw gating status is still FAIL — without this,
    # it falls through to the generic-fallback loop below (which has no
    # idea this criterion was deliberately superseded, and shows its raw
    # un-stripped evidence text, bracketed caveat included). Claim it
    # explicitly here so it's suppressed everywhere, the same as the
    # tailored-merge path already does for the cases where it wins.
    if sq_authoritative_pass and mi is not None and (mi.get("status") or "").upper() == "FAIL":
        used.add("min_income_for_sa")
    if income_undocumented:
        if sq_fail_floor:
            used.add("sbi_qualification")
        if mi_fail:
            used.add("min_income_for_sa")
        gq = None
    else:
        gq = sq if sq_fail_floor else (mi if mi_fail else (sq or mi))
    if (
        gq
        and (gq.get("status") or "").upper() == "FAIL"
        and _is_floor_type(gq)
        and "sbi_qualification" not in used
        and "min_income_for_sa" not in used
    ):
        drc_gate_for_merge = by_crit.get("sbi_drc_income_floor")
        high.append(_row_income_adequacy(decision, calcs, gq, drc_gate_for_merge))
        used.add(gq.get("criterion"))
        # sbi_qualification and min_income_for_sa are two SEPARATE gating
        # items that can both independently fail on the same underlying
        # "income too low" fact — whichever one wasn't picked as `gq`
        # would otherwise orphan into the generic fallback below with its
        # own raw, jargon-y evidence text (e.g. "[generic cross-check,
        # not an SBI-specific rule...]"), producing a second near-
        # duplicate HIGH row. Merge it in here instead. Only applies when
        # BOTH are genuine floor-type failures — a mismatch-type
        # sbi_qualification (disallowed list / Agriculturist) is a
        # different finding and must still get its own row via the
        # generic fallback.
        if sq_fail_floor and mi_fail:
            used.add("sbi_qualification")
            used.add("min_income_for_sa")
        if drc_gate_for_merge and (drc_gate_for_merge.get("status") or "").upper() == "FAIL":
            used.add("sbi_drc_income_floor")
    for crit, fn in _MEDIUM_CRITERIA.items():
        g = by_crit.get(crit)
        # WARN is the usual severity; FAIL happens on critical-severity
        # findings for some of these criteria (e.g. existing-cover) and
        # still belongs in this criterion's own tailored MEDIUM row, not
        # promoted to a generic HIGH one just because the status differs.
        if g and (g.get("status") or "").upper() in ("WARN", "FAIL") and crit not in used:
            row = fn(decision, g) if fn.__code__.co_argcount == 2 else fn(decision, calcs, g)
            if row:
                medium.append(row)
                used.add(crit)

    for g in gating:
        # sbi_qualification/min_income_for_sa are excluded here ONLY via
        # `used` (set above when _row_income_adequacy claims a genuine
        # income-floor FAIL) — NOT by criterion name, which would also
        # silently drop a qualification-MISMATCH FAIL (disallowed list /
        # Agriculturist) that _row_income_adequacy deliberately does NOT
        # claim (see above). That case needs to land here, in the generic
        # fallback, so it's still shown somewhere rather than vanishing
        # entirely.
        if (
            (g.get("status") or "").upper() == "FAIL"
            and g.get("criterion") not in used
            and g.get("criterion") not in _NON_PROFILE_CRITERIA
        ):
            high.append(_generic_row(g))
            used.add(g.get("criterion"))

    occ_row = _row_occupation(decision, None)
    if occ_row and len(medium) < 3:
        medium.append(occ_row)
    for g in gating:
        if len(medium) >= 3:
            break
        if (
            (g.get("status") or "").upper() == "WARN"
            and g.get("criterion") not in used
            and g.get("criterion") not in _NON_PROFILE_CRITERIA
            and g.get("criterion") not in _REDUNDANT_WITH_REQUIREMENTS_BOX
        ):
            medium.append(_generic_row(g))
            used.add(g.get("criterion"))

    low = []
    app = decision.get("applicant") or {}
    edu = app.get("education_level") or "Not Available"
    # "Qualification gate met" here means the EDUCATION VALUE ITSELF was a
    # recognised, non-disqualifying tier — distinct from whether verified
    # INCOME cleared that tier's floor (that result is the "Income
    # adequacy" HIGH-risk row above, and is intentionally independent of
    # this row: the reference shows "gate met" here even on cases where
    # the income floor fails). Only an actually disallowed/unrecognised
    # qualification value changes this row.
    qual_g = by_crit.get("sbi_qualification")
    qual_evidence = ((qual_g or {}).get("evidence") or "").lower()
    disallowed_education = (
        qual_g
        and qual_g.get("status") == "FAIL"
        and any(
            term in qual_evidence for term in ("disallowed list", "not an acceptable combination")
        )
    )
    if not disallowed_education and qual_g and qual_g.get("status") == "PASS":
        low.append(("Education", f"{edu} = qualification gate met", "Not Available", ()))
    elif (
        not disallowed_education
        and qual_g
        and qual_g.get("status") == "FAIL"
        and "required" in qual_evidence
    ):
        low.append(
            (
                "Education",
                f"{edu} — education tier recognised; income threshold assessed separately",
                "Not Available",
                (),
            )
        )

    nominees = _display_nominees(app)
    if nominees:
        descriptions = []
        for nominee in nominees:
            rel = (nominee.get("relation") or "Not Available").strip()
            share = nominee.get("share_pct")
            descriptions.append(f"{rel} {share}%" if share is not None else rel)
        desc = "; ".join(descriptions)
        share_total = sum(
            float(n.get("share_pct"))
            for n in nominees
            if isinstance(n.get("share_pct"), (int, float))
        )
        repeated_shares = share_total > 100
        desc += (
            " = source entries require reconciliation (shares exceed 100%)"
            if repeated_shares
            else " = insurable interest match"
        )
        name_fix = False
        for c in (decision.get("reconciliation") or {}).get("contradictions") or []:
            field = (c.get("field") or "").lower()
            # "low" severity = reconciler already resolved this to one
            # winning value and says no action is required (e.g. a
            # truncated field, or an honorific like "Devi" present in one
            # source and not another) — not a genuine KYC discrepancy.
            # See the matching note in engine.requirements.
            if (
                "nominee" in field
                and "name" in field
                and (c.get("severity") or "").lower() != "low"
            ):
                name_fix = True
                break
        action = (
            "Reconcile nominee source entries"
            if repeated_shares
            else "Correct nominee name (KYC)"
            if name_fix
            else "Not Available"
        )
        low.append(("Nominee relationship", desc, action, ()))

    return {"HIGH": high, "MEDIUM": medium[:3], "LOW": low[:3]}


_BADGE_COLOR = {"HIGH": RED, "MEDIUM": AMBER, "LOW": GREEN}
_COL_BADGE_X = CONTENT_X0
_COL_TITLE_X = CONTENT_X0 + 26.5
# Widened +14pt (borrowed from the description column, which has more
# slack) — at this title font size, many short titles ("Existing-cover
# disclosure" etc.) would otherwise wrap to a needless 2nd line under a
# narrower column.
_COL_DESC_X = CONTENT_X0 + 125.8
_COL_ACTION_X = CONTENT_X0 + 479.5
_COL_TITLE_W = _COL_DESC_X - _COL_TITLE_X - 2
_COL_DESC_W = _COL_ACTION_X - _COL_DESC_X - 2
_COL_ACTION_W = CONTENT_X1 - _COL_ACTION_X


# Exact row-anchor tops measured off the reference (pdfplumber top of the
# title/desc line, which is this row's vertical anchor — badge sits ~0.3pt
# below it, a wrapped action column centers around it). Indexed by row
# position within its severity bucket. A bucket with more rows than were
# measured (not possible with the current 3/3/2 cap, but kept for safety)
# extends the pattern using that bucket's own last measured delta.
# These were the exact reference-measured positions for a smaller
# (5.19pt body) font size. A later font-size increase grew the text but
# left these absolute positions untouched at first, so single-line rows
# — which never trigger the dynamic min_anchor push-down — ended up
# spaced for the OLD, smaller glyph size: rows, and whole
# HIGH/MEDIUM/LOW sections, read as visibly congested at the new font
# size even though nothing technically overlapped. Rescaled by that
# same ~1.22 factor around the HIGH header (100.9) as a fixed origin,
# so every gap in the table — row-to-row and section-to-section alike —
# grew in the same proportion the text did, instead of hand-picking new
# numbers.
_RISK_ANCHORS = {
    "HIGH": {"header": 100.90, "rows": [113.22, 128.72, 144.21]},
    "MEDIUM": {"header": 159.95, "rows": [171.54, 184.23, 197.67]},
    "LOW": {"header": 212.77, "rows": [224.97, 237.72]},
}
# Scaled ~1.1x again from a prior readability pass's values
# (7.69/6.83/6.33) — same proportional-rescale approach as that earlier
# pass, not hand-picked numbers.
_RISK_TITLE_LEAD = 8.46  # title/desc own-wrap leading
_RISK_ACTION_LEAD = 7.51  # action own-wrap leading
_RISK_BODY_SIZE = 6.96  # the title_lines/desc_lines font size (setFont/_wrap calls below)
# row_bottom marks the TOP of a row's last wrapped line (_ty's reference
# point), not its visual bottom — a glyph's own descender extends roughly
# a full font-size below that point (see _ty/_ASCENT_FRAC), so the divider
# needs at least _RISK_BODY_SIZE of clearance to clear it, not some
# fraction of the inter-line LEAD. The row-start gap must in turn stay
# bigger than that clearance, or the divider can be pushed past where the
# next row already began rendering — two independent hardcoded constants
# here used to satisfy that only by coincidence, which a font-size
# increase can break (the divider drawn through a row's own wrapped 2nd
# line).
_DIVIDER_MIN_CLEARANCE = _RISK_BODY_SIZE + 0.5
_ROW_START_MIN_GAP = _RISK_TITLE_LEAD


def _risk_row_anchors(label: str, n_rows: int) -> list[float]:
    spec = _RISK_ANCHORS[label]
    anchors = list(spec["rows"][:n_rows])
    if len(anchors) < n_rows:
        last_delta = (spec["rows"][-1] - spec["rows"][-2]) if len(spec["rows"]) > 1 else 12.0
        cursor = anchors[-1] if anchors else spec["header"] + 10.0
        while len(anchors) < n_rows:
            cursor += last_delta
            anchors.append(cursor)
    return anchors


def _draw_risk_section(c, label: str, rows: list, top: float, is_first: bool = False) -> float:
    # top is where the previous section (or the decision bar, for the
    # first section) actually finished — normally equal to this section's
    # own hardcoded anchor, but a taller ID bar above, or a previous risk
    # section that needed extra room for long content, can push it later.
    # `dy` shifts every position in this section down by the same amount
    # so nothing overlaps what came before.
    #
    # Previously snapped to this section's own static per-label anchor as
    # a floor (max(anchor, top)) — meant to guarantee breathing room, but
    # it backfires whenever an EARLIER bucket has fewer rows than the
    # anchor was calibrated for (not just when it's completely empty):
    # e.g. a 1-row MEDIUM section still left LOW anchored at its old
    # fixed position, calibrated assuming a full 3-row MEDIUM above it —
    # same dead-gap bug as the empty-HIGH case, just not fully fixed by
    # only special-casing `is_first`. The static anchors were themselves
    # originally derived BY measuring natural flow against a fully-
    # populated reference case, so natural flow (`top`, already correctly
    # carrying forward from whatever rendered above — decision bar, BHB
    # panel, or a shorter/empty earlier section) reproduces the same
    # calibrated position in the normal full case anyway, with no need
    # for a separate static floor. Always use `top` directly now.
    header_top = top
    dy = header_top - _RISK_ANCHORS[label]["header"]
    c.setFont(_SANS_B, 5.41)
    c.setFillColor(INK)
    c.drawString(ML, _ty(header_top, 5.41), f"{label} RISK")
    # Panel title belongs to whichever severity bucket renders FIRST, not
    # hardcoded to HIGH — a CONDITIONAL-verdict case (0 gating FAILs) has
    # no HIGH rows at all, so the title would otherwise vanish from the
    # whole panel instead of just moving to MEDIUM.
    if is_first:
        rtxt = "FINANCIAL & PROFILE RISK SUMMARY"
        c.setFont(_SANS, 5.41)
        c.setFillColor(MUTE)
        rw = pdfmetrics.stringWidth(rtxt, _SANS, 5.41)
        c.drawString(CONTENT_X1 - rw, _ty(header_top, 5.41), rtxt)
    rule_top = header_top + 4.84
    c.setStrokeColor(SECTION_LINE)
    c.setLineWidth(0.58)
    c.line(CONTENT_X0, PAGE_H - rule_top, CONTENT_X1, PAGE_H - rule_top)

    anchors = [a + dy for a in _risk_row_anchors(label, len(rows))]
    last_bottom = rule_top
    for i, row in enumerate(rows):
        title, desc, action, bold_terms = row[:4]
        # Optional 5th element: True when this row's embedded comparison
        # (≥/≤/</>) is favorable to the applicant despite the row itself
        # sitting in a WARN/FAIL risk bucket — e.g. "CIBIL score 697 ≥
        # 650" is a passing score; the row is still MEDIUM-risk overall
        # because something else (loan details) is unconfirmed, but
        # colouring that passing comparison the same red as a genuinely
        # bad one (payout > HLV) read as the score itself being a
        # problem. Defaults to False
        # (current red-always behaviour, correct for the vast majority of
        # rows, which only ever reach this panel because their status is
        # WARN/FAIL in the first place) so every existing 4-tuple row
        # builder needs no change.
        op_favorable = row[4] if len(row) > 4 else False
        title_lines = _wrap(title, _SANS_B, _RISK_BODY_SIZE, _COL_TITLE_W, max_lines=3)
        desc_lines = _wrap(desc, _SANS, _RISK_BODY_SIZE, _COL_DESC_W, max_lines=4)
        action_lines = (
            _wrap(action, _SANS_B, 6.18, _COL_ACTION_W, max_lines=3)
            if action and action != "Not Available"
            else []
        )

        row_anchor = anchors[i]
        # Keep the measured anchor unless this row's own content needs
        # more room than the reference's did (longer text on another
        # case) — then push down just enough to avoid overlapping the
        # previous row, rather than silently colliding. This gap and the
        # divider's own min_clearance below it MUST stay derived from the
        # same font-dependent base (_RISK_TITLE_LEAD) with this one kept
        # safely larger — they used to be independent hardcoded constants
        # (5.2 here, "_RISK_TITLE_LEAD - 1.0" there) that happened to satisfy
        # gap > clearance at one font size only by coincidence; a later
        # font-size increase broke that coincidence (LEAD grew, this bare
        # 5.2 didn't), so the divider's floor could exceed this row's own
        # start floor and the divider was drawn through the NEXT row's own
        # title/desc text instead of the gap before it.
        min_anchor = last_bottom + _ROW_START_MIN_GAP
        if row_anchor < min_anchor:
            row_anchor = min_anchor

        c.setFont(_SANS_B, 5.41)
        c.setFillColor(_BADGE_COLOR[label])
        c.drawString(_COL_BADGE_X, _ty(row_anchor + 0.3, 5.41), label)

        ty = row_anchor
        c.setFont(_SANS_B, _RISK_BODY_SIZE)
        c.setFillColor(INK)
        for ln in title_lines:
            c.drawString(_COL_TITLE_X, _ty(ty, _RISK_BODY_SIZE), ln)
            ty += _RISK_TITLE_LEAD

        dy = row_anchor
        for ln in desc_lines:
            _draw_mono_line(
                c,
                ln,
                _COL_DESC_X,
                _ty(dy, _RISK_BODY_SIZE),
                _RISK_BODY_SIZE,
                INK,
                bold_terms,
                op_color=GREEN if op_favorable else RED,
            )
            dy += _RISK_TITLE_LEAD

        max_lines_here = max(len(title_lines), len(desc_lines), 1)
        if action_lines:
            # Vertically centre a wrapped action around the row anchor —
            # matches the reference's own flex-centered action column
            # (e.g. a 2-line action starts ~2.5pt ABOVE the row anchor).
            ay = row_anchor - (len(action_lines) - 1) * _RISK_ACTION_LEAD / 2.0
            c.setFont(_SANS_B, 6.18)
            c.setFillColor(ACTION_INK)
            for j, ln in enumerate(action_lines):
                txt = ("→ " + ln) if j == 0 else ln
                c.drawString(_COL_ACTION_X, _ty(ay, 6.18), txt)
                ay += _RISK_ACTION_LEAD
            max_lines_here = max(max_lines_here, len(action_lines))

        row_bottom = row_anchor + max(max_lines_here - 1, 0) * _RISK_TITLE_LEAD
        last_bottom = row_bottom
        if i < len(rows) - 1:
            # Guard against the row's own wrapped text: the pre-computed
            # next-row anchor assumes a single-line reference layout, so
            # when THIS row's description wraps to 2-3 lines the naive
            # "anchors[i+1] - 1.6" candidate can still land inside (or
            # just above) the wrapped text instead of below it. Compare
            # the actual candidate against the safety floor, not the raw
            # anchor.
            #
            # row_bottom is the TOP (_ty's reference point) of the row's
            # LAST wrapped line, not its visual bottom — a glyph's own
            # descender extends roughly a full font-size below that point
            # (see _ty/_ASCENT_FRAC), so a mere "+3" margin only reaches
            # the middle of that last line, not below it. Use a clearance
            # close to a full line pitch instead.
            min_clearance = _DIVIDER_MIN_CLEARANCE
            candidate = anchors[i + 1] - 1.6
            divider_top = (
                candidate if candidate > row_bottom + min_clearance else row_bottom + min_clearance
            )
            c.setStrokeColor(ROW_LINE)
            c.setLineWidth(0.58)
            c.line(_COL_BADGE_X + 4, PAGE_H - divider_top, _COL_ACTION_X - 4, PAGE_H - divider_top)

    return last_bottom


def _draw_risk_summary(c, decision: dict, top: float) -> float:
    # Each severity section's header sits at top + _RISK_SECTION_GAP (a
    # more generous gap than the bare _SECTION_GAP), purely from natural
    # flow — no static per-section floor (see _draw_risk_section). This
    # guarantees real breathing room before every severity header
    # regardless of how short or long the preceding content ran.
    rows = _build_risk_rows(decision)
    first_drawn = False
    for label in ("HIGH", "MEDIUM", "LOW"):
        if rows[label]:
            top = _draw_risk_section(
                c, label, rows[label], top + _RISK_SECTION_GAP, is_first=not first_drawn
            )
            first_drawn = True
    # Previously floored at 225.04 (the reference's measured FINANCIALS
    # header position for a fully-populated case) via max(225.04, top).
    # Same bug family as _draw_risk_section's old anchor floor: whenever
    # an earlier bucket renders with fewer rows than the reference case
    # had — not just zero rows — natural `top` lands below 225.04 and the
    # static floor reintroduces a dead gap right before the FINANCIALS
    # table. A `rows["HIGH"]` truthy check isn't enough to guard this
    # (HIGH can be non-empty but still short). `top` already flows
    # correctly from every section above (decision bar, BHB panel, HIGH/
    # MEDIUM/LOW, whichever actually rendered and however long each
    # ran) — no separate floor needed.
    return top + _RISK_SECTION_GAP


# ── four-column data table ─────────────────────────────────────────────


def _bool_tone(yes: bool) -> str:
    return "good" if yes else "bad"


def _build_financials_col(decision, calcs):
    app = decision.get("applicant") or {}
    declared = _safe_int(app.get("declared_annual_income"))
    verified = _safe_int(_cv(calcs, "selected_income"))
    gap_pct = None
    if declared and verified:
        gap_pct = (declared - verified) / verified * 100

    selected = calcs.get("selected_income") or {}
    method = (selected.get("method") or "").lower()
    label = "DECLARED ONLY" if "declared" in method else "ASSESSED INCOME"
    if method.startswith("itr"):
        audit_basis = (selected.get("inputs") or {}).get("itr_income_audit") or {}
        label = (
            "VERIFIED 2YR AVG" if audit_basis.get("basis") == "avg_latest_2_years" else "ITR INCOME"
        )
    elif "salary_slip" in method:
        label = "SALARY ANNUALISED"
    elif "bank" in method:
        label = "BANK CREDIT BASIS"
    items = [
        (
            label,
            _inr(_cv(calcs, "selected_income")).replace("Rs. ", ""),
            "warn" if "declared" in method else "neutral",
        )
    ]
    items.append(("INCOME BASIS", method.replace("_", " ") or "Not established", "neutral"))
    audit = ((calcs.get("selected_income") or {}).get("inputs") or {}).get("itr_income_audit") or {}
    per_year = audit.get("per_year_earned") or {}
    for yr, val in list(per_year.items())[:2]:
        items.append(
            (
                f"AY{yr.replace('20', '').replace('-', '-')}(ITR)",
                _inr(val).replace("Rs. ", ""),
                "neutral",
            )
        )
    decl_val = _inr(declared)
    # declared>0 but verified==0 is the single most extreme form of
    # over-declaration (100% of the declared figure is unsupported) — the
    # percentage formula above requires a non-zero verified figure to
    # divide by, so it silently produced no annotation at all here,
    # making the sheet's single biggest red flag on this case
    # indistinguishable from a routine, unremarkable declared-income
    # line (e.g. a declared figure against a confirmed,
    # hard-proof-verified ₹0 earned income).
    if declared and not verified:
        decl_val += " (100% unsupported, verified income is Rs. 0)"
    elif gap_pct is not None and abs(gap_pct) > 20:
        decl_val += (
            f" ({gap_pct:.1f}% over-declaration)"
            if gap_pct > 0
            else f" ({abs(gap_pct):.1f}% under-declaration)"
        )
    is_bad = (declared and not verified) or (gap_pct and abs(gap_pct) > 20)
    items.append(("DECLARED INCOME", decl_val, "bad" if is_bad else "neutral"))
    mult = (calcs.get("hlv_base") or {}).get("inputs") or {}
    items.append(
        (
            "MULTIPLIER",
            f"{mult.get('multiplier', 'Not Available')}× ({mult.get('age_band', 'Not Available')})",
            "neutral",
        )
    )
    items.append(("HLV", _inr(_cv(calcs, "hlv_base")).replace("Rs. ", ""), "neutral"))
    existing_sa = ((calcs.get("max_sa_eligible") or {}).get("inputs") or {}).get("existing_cover")
    items.append(("EXISTING SA", _inr(existing_sa).replace("Rs. ", ""), "neutral"))
    items.append(
        ("MAX ELIG SA", _inr(_cv(calcs, "max_sa_eligible")).replace("Rs. ", ""), "neutral")
    )
    proposed_sa = decision.get("proposed_sa") or app.get("sum_assured")
    gating = {g.get("criterion"): g for g in (decision.get("gating") or [])}
    # Same sbi_qualification-passed exclusion as _build_risk_rows /
    # _fail_warn_counts / _primary_decision_criterion / engine.requirements:
    # min_income_for_sa's FAIL alone must not mark SA APPLIED / TFSAR as a
    # problem once the authoritative SBI gate has PASSED, or this column
    # contradicts every other part of the sheet that's already been
    # corrected to ignore the superseded generic floor.
    _sq_gate_fin = gating.get("sbi_qualification")
    _sq_pass_fin = _sq_gate_fin is not None and _sq_gate_fin.get("status") == "PASS"
    floor_fail = (_sq_gate_fin or {}).get("status") == "FAIL" or (
        not _sq_pass_fin and (gating.get("min_income_for_sa") or {}).get("status") == "FAIL"
    )
    ceiling = _cv(calcs, "max_sa_eligible")
    sa_note = (
        " (eligibility not established)"
        if ceiling is None
        else (
            " (exceeds remaining eligibility)"
            if _safe_int(proposed_sa) > _safe_int(ceiling)
            else " (within remaining eligibility)"
        )
    )
    if "declared" in method:
        sa_note += " (provisional: income not verified)"
    items.append(("SA APPLIED", _inr(proposed_sa).replace("Rs. ", "") + sa_note, "neutral"))
    proof_g = gating.get("sbi_qualification")
    if (
        proof_g
        and proof_g.get("status") == "FAIL"
        and not any(
            t in (proof_g.get("evidence") or "").lower()
            for t in ("disallowed list", "not an acceptable combination")
        )
    ):
        m = re.search(r"required ₹([\d,]+)", proof_g.get("evidence") or "")
        floor = m.group(1).replace(",", "") if m else None
        items.append(
            (
                "INCOME PROOF BASIS",
                f"Below {_inr(floor)} SA-band & qualification minimum"
                if floor
                else "Below required minimum income",
                "bad",
            )
        )
    tfsar = None if existing_sa is None else existing_sa + (_safe_int(proposed_sa) or 0)
    items.append(("TFSAR", _inr(tfsar).replace("Rs. ", ""), "bad" if floor_fail else "neutral"))
    note = (
        f"TFSAR (Total Financial Sum at Risk) = Existing SA {_inr(existing_sa).replace('Rs. ', '')} + "
        f"SA Applied {_inr(proposed_sa).replace('Rs. ', '')}. Existing SA uses the higher declared/bureau cover."
    )
    if existing_sa is None:
        note = "Total financial sum at risk not assessed: existing cover evidence unavailable."
    if "declared" in method:
        note += " HLV is provisional until income verification."
    base_income = ((calcs.get("hlv_base") or {}).get("inputs") or {}).get("income")
    if base_income is not None and _safe_int(base_income) != verified:
        ratio = _safe_int(base_income) / verified if verified else None
        note += f" HLV income {_inr(base_income)}" + (
            f" ({ratio:.1f}× employment)." if ratio else "."
        )
    return items, note


_PHOTO_MATCH_LABELS = {
    "match": "Match",
    "partial-match": "Partial match",
    "no-match": "No match",
    "undetectable": "Undetectable",
}


def _row_photo_match_pct(label: str, result: dict | None) -> tuple:
    """One (label, value, tone) row for a single face-match comparison
    from applicant.photo_match_set — engine.photo_match.compare_faces_
    gemini's confidence (0.0-1.0) shown as a percentage alongside its
    categorical result."""
    if not result or result.get("match_result") in (None, "skipped"):
        # Just the reason, no "Not performed —" prefix: the prefix would
        # push these already-long reasons into choppy word-by-word
        # wrapping in this narrow column; the reason text alone already
        # reads as a complete statement.
        reason = (result or {}).get("reasoning") or "Not performed."
        return (label, reason, "neutral")
    mr = result.get("match_result")
    if mr == "undetectable":
        # Same reasoning as _draw_photo_match_block: "undetectable" means
        # no face could be located in one of the two images at all, so its
        # confidence isn't a real similarity score — showing it as a %
        # misleadingly reads as an actual comparison result.
        return (label, "No usable photo — image unreadable", "bad")
    conf = result.get("confidence")
    pct_txt = f"{round(float(conf) * 100)}%" if isinstance(conf, (int, float)) else "Not Available"
    mr_txt = _PHOTO_MATCH_LABELS.get(mr, (mr or "Not Available").title())
    tone = "good" if mr == "match" else ("bad" if mr == "no-match" else "warn")
    return (label, f"{mr_txt} · {pct_txt}", tone)


def _build_iib_col(decision):
    bureau = decision.get("sbi_bureau") or {}
    comp = (decision.get("extraction_blocks") or {}).get("compliance") or {}
    ecd = comp.get("existing_cover_declared") or {}
    items = [
        ("CIBIL SCORE", str(bureau.get("cibil_score") or "Not Available"), "neutral"),
        ("DRC CATEGORY", bureau.get("drc_category") or "Not Available", "neutral"),
        # Label is "CIBIL Profile" (not "...Profile Score"). The colour is
        # read directly off SBI's own "Scores- DRC, CIBIL, Profile.xlsx"
        # PROFILE SCORE column — an independent bureau-assessed field,
        # not derived from whether CIBIL SCORE is present (a case can
        # have a real CIBIL score but a 'Red' PROFILE SCORE, or no CIBIL
        # score but a 'Green' PROFILE SCORE — a missing score does not by
        # itself justify red).
        (
            "CIBIL PROFILE",
            bureau.get("profile_score") or "Not Available",
            "good" if (bureau.get("profile_score") or "").lower() == "green" else "warn",
        ),
    ]
    policies = bureau.get("iib_policies")
    iib_known = bureau.get("iib_policies") is not None
    items.append(
        (
            "IIB FINDINGS",
            f"{len(policies)} policy records"
            if policies
            else ("No records" if iib_known else "Evidence unavailable"),
            "neutral" if iib_known else "warn",
        )
    )
    declared_cover = ecd.get("total_life_cover_other")
    in_force_sa = sum(
        _safe_int(p.get("sum_assured_inr")) or 0
        for p in (policies or [])
        if any(
            t in (p.get("status") or "").lower()
            for t in ("inforce", "in force", "paid-up", "paid up")
        )
    )
    if not declared_cover and in_force_sa:
        items.append(
            ("EXISTING COVER (DECLARED)", f"None (IIB shows {_inr(in_force_sa)} in force)", "bad")
        )
    else:
        items.append(
            (
                "EXISTING COVER (DECLARED)",
                _inr(declared_cover).replace("Rs. ", "") or "None",
                "neutral",
            )
        )
    # Excludes "withdraw" (customer-initiated, not an insurer adverse
    # decision) — must stay in sync with engine.sbi_financial.
    # check_material_non_disclosure and engine.requirements._ADVERSE_TERMS,
    # or this field displays a withdrawn-only record as "PRIOR DECLINE"
    # even on a case with no genuine decline.
    bad_terms = ("declin", "postpone", "rated up", "rate up")
    flagged = [
        p
        for p in (policies or [])
        if any(t in (p.get("status") or "").lower() for t in bad_terms)
        or any(t in (p.get("uw_decision") or "").lower() for t in bad_terms)
    ]
    if flagged:
        p = flagged[0]
        date = p.get("uw_decision_date") or ""
        m = re.match(r"(\d{4})-(\d{2})", date or "")
        months = [
            "",
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ]
        mon_yyyy = f"{months[int(m.group(2))]} {m.group(1)}" if m else date
        items.append(
            (
                "PRIOR DECLINE",
                f"{_short_insurer(p.get('insurer'))} "
                f"{_inr(p.get('sum_assured_inr'))} "
                f"{_short_product(p.get('product_type'))} {(p.get('status') or '').upper()} "
                f"({mon_yyyy})".strip(),
                "bad",
            )
        )
    else:
        items.append(
            (
                "PRIOR DECLINE",
                "None found" if iib_known else "Not assessed: IIB evidence unavailable",
                "good" if iib_known else "warn",
            )
        )
    app = decision.get("applicant") or {}
    items.append(
        (
            "PHOTO ON FILE",
            app.get("photo_present") or "Not Available",
            _bool_tone((app.get("photo_present") or "").lower() == "yes"),
        )
    )
    uid_match = app.get("photo_matches_uid") or "Not Available"
    if uid_match.lower() == "unverified":
        # The real reason is already sitting in applicant.photo_match.
        # reasoning (e.g. "No KYC photo on file." / "No live photo on
        # file.") — read it directly rather than show a vague "reason not
        # captured in record". No "Not performed —" prefix: the prefix
        # pushed this already-long reason into choppy word-by-word
        # wrapping in this narrow column.
        _uid_reason = ((app.get("photo_match") or {}).get("reasoning") or "").strip()
        if "no kyc photo" in _uid_reason.lower():
            uid_value = "KYC photo unavailable"
        elif any(
            t in _uid_reason.lower() for t in ("blur", "discernible", "unreadable", "gradient")
        ):
            uid_value = "Unable to verify: unreadable image"
        else:
            uid_value = "Unable to verify; review photo evidence"
    else:
        uid_value = uid_match
    items.append(
        (
            "UID / AADHAAR MATCH",
            uid_value,
            "warn"
            if uid_match.lower() == "unverified"
            else ("good" if uid_match.lower() == "yes" else "neutral"),
        )
    )
    # Face-match percentages moved OUT of this narrow column — the long
    # comparison labels/reasons kept overflowing or wrapping choppily in
    # this ~140pt column no matter how the text was shortened. Now drawn
    # as its own full-width mini-table block below the 4-column table —
    # see _draw_photo_match_block, called from render_triage_sheet. Not
    # built here at all any more.
    # AML / Fraud risk — present in every original client-shared case sheet
    # as the IIB & Compliance box's final row.
    #
    # For SBI cases, sourced from compliance.aml_status / aml_evidence —
    # real, per-case data from the compliance agent's CAF self-declaration
    # review (sanctions screening, PEP status, high-risk
    # occupation/industry/country flags). The generic engine's own
    # fraud_signal gating item (engine.medical.compute_fraud_risk) is a
    # cotinine-mismatch / MER↔VMER-contradiction check with no SBI
    # grounding and no relevant data for this product, so it is not used
    # for SBI cases. Non-SBI cases keep the original fraud_signal source
    # unchanged.
    if decision.get("sbi_bureau"):
        aml_status = comp.get("aml_status") or "Not Available"
        ev = comp.get("aml_evidence") or {}
        parts = []
        if ev.get("sanctions_screening_clear") is True:
            parts.append("no sanctions concern declared")
        elif ev.get("sanctions_screening_clear") is False:
            parts.append("sanctions concern flagged")
        pep = ev.get("pep_self_declaration")
        if pep:
            parts.append(f"PEP: {pep}")
        if ev.get("occupation_high_risk") or ev.get("industry_high_risk"):
            parts.append("high-risk occupation/industry flagged")
        basis = "; ".join(parts) if parts else "Supporting evidence unavailable"
        verified_screening = ev.get("screening_verified") is True and bool(
            ev.get("screening_source_document")
        )
        if verified_screening:
            items.append(
                (
                    "AML / FRAUD RISK",
                    f"{aml_status}: {basis}",
                    "good" if str(aml_status).lower() == "compliant" else "warn",
                )
            )
        else:
            items.append(
                ("AML DECLARATION", basis + "; independent screening not evidenced", "neutral")
            )
    else:
        fraud_g = next(
            (g for g in (decision.get("gating") or []) if g.get("criterion") == "fraud_signal"),
            None,
        )
        if fraud_g:
            fstatus = (fraud_g.get("status") or "").upper()
            band_label = {"PASS": "Low", "WARN": "Medium", "FAIL": "High"}.get(
                fstatus, "Not Available"
            )
            tone = {"PASS": "good", "WARN": "warn", "FAIL": "bad"}.get(fstatus, "neutral")
            basis = _redact_pii(fraud_g.get("evidence")) or "Not Available"
            items.append(("AML / FRAUD RISK", f"{band_label}: {basis}", tone))
    return items


def _compact_name_contradiction(contradiction: dict) -> str:
    """Compact a reconciler name-mismatch contradiction for a narrow
    dashboard cell. The reconciler consistently writes this class of
    finding as "...spelled N different ways... 'X' ... error for 'Y'." —
    extract the count + the two quoted variants generically; fall back to
    the raw (wrapped) explanation if that pattern isn't present so an
    unexpected contradiction shape still renders instead of vanishing."""
    explanation = contradiction.get("explanation") or ""
    m_cnt = re.search(r"spelled (\w+) different ways", explanation, re.I)
    cnt = m_cnt.group(1) if m_cnt else "multiple"
    quotes = re.findall(r"'([^']+)'", explanation)
    if len(quotes) >= 2:
        return (
            f"Spelled {cnt} different ways on the same proposal form — "
            f'"{quotes[0]}" likely a data-entry error for "{quotes[-1]}"'
        )
    return _redact_pii(explanation)


def _build_docs_col(decision):
    bank = decision.get("bank_analysis") or {}
    calcs = decision.get("calculations") or {}
    foir = _cv(calcs, "foir")
    extracted = decision.get("extraction_blocks") or {}

    def statement_evidence(record):
        if not isinstance(record, dict):
            return False
        period = (
            record.get("period_from") or record.get("period_to") or record.get("statement_period")
        )
        amounts = any(
            record.get(k) is not None
            for k in ("closing_balance", "opening_balance", "average_monthly_balance")
        )
        return bool(record.get("transactions") or (period and amounts))

    records = ((extracted.get("assets_liabilities") or {}).get("bank_statements") or []) + (
        (extracted.get("income") or {}).get("bank_statements") or []
    )
    has_bank = bool(
        bank.get("monthly")
        or bank.get("monthly_summary")
        or any(statement_evidence(r) for r in records)
    )
    bank_income = "bank" in str((calcs.get("selected_income") or {}).get("method") or "").lower()
    foir_inputs = (calcs.get("foir") or {}).get("inputs") or {}
    liabilities = extracted.get("assets_liabilities") or {}
    has_liability_evidence = has_bank or any(
        isinstance(loan, dict) and loan.get("monthly_emi") is not None
        for loan in (liabilities.get("loans") or [])
    )
    foir_label = (
        f"{foir}% (documented liabilities)"
        if foir is not None and has_liability_evidence
        else "Not assessed: liability evidence unavailable"
    )
    bank_label = (
        "Provided"
        if has_bank
        else (
            "Income uses bank credits; statement inventory unconfirmed"
            if bank_income
            else "Not provided: no bank statement in the document set"
        )
    )
    items = [
        ("BANK STMT", bank_label, "good" if has_bank else "warn"),
        ("FOIR", foir_label, "neutral" if has_liability_evidence else "warn"),
    ]

    app = decision.get("applicant") or {}
    nominees = _display_nominees(app)
    if nominees:
        for index, nominee in enumerate(nominees, 1):
            name = _title_name(nominee.get("name"))
            share = nominee.get("share_pct")
            percentage = f" {share}%" if share is not None else " (share not available)"
            label = "NOMINEE" if index == 1 else f"NOMINEE {index}"
            items.append(
                (
                    label,
                    f"{name} · {nominee.get('relation') or 'Not Available'}{percentage}",
                    "neutral",
                )
            )
        name_fix = None
        for c in (decision.get("reconciliation") or {}).get("contradictions") or []:
            field = (c.get("field") or "").lower()
            # Skip "low" severity — the reconciler already resolved these
            # to one winning value with "no action required" (truncation,
            # an honorific like "Devi" present in one source and not
            # another, etc.); flagging it here as a likely data-entry
            # error contradicts that finding. See engine.requirements.
            if (
                "nominee" in field
                and "name" in field
                and (c.get("severity") or "").lower() != "low"
            ):
                name_fix = c
                break
        if name_fix:
            items.append(("NOMINEE NAME", _compact_name_contradiction(name_fix), "warn"))
    return items


def _format_product_name(name: str) -> str:
    """Insurer-brand/product-name extraction often lands as a bare hyphen
    with no spaces (e.g. 'SBI Life-Smart Shield Plus'); display it with
    the spaced em-dash the reference uses ('SBI Life — Smart Shield
    Plus')."""
    n = (name or "").strip()
    if not n:
        return "Not Available"
    return re.sub(r"(?<=\w)-(?=[A-Z])", " — ", n, count=1)


def _build_policy_col(decision):
    app = decision.get("applicant") or {}
    items = [
        ("PLAN", _format_product_name(app.get("product_name")), "neutral"),
        ("TYPE", app.get("plan_type") or "Not Available", "neutral"),
        (
            "TERM (YRS)",
            f"{app.get('policy_term_years', 'Not Available')}/{app.get('premium_payment_term', 'Not Available')}",
            "neutral",
        ),
        ("MODE", app.get("premium_payment_mode") or "Not Available", "neutral"),
        ("SUM ASSURED", _inr(app.get("sum_assured")).replace("Rs. ", ""), "neutral"),
        (
            "PREMIUM",
            f"{_safe_int(app.get('annual_premium')):,}/yr"
            if _safe_int(app.get("annual_premium"))
            else "Not Available",
            "neutral",
        ),
        # CHANNEL removed: not part of SBI's financial-UW requirements in
        # either source guideline (Case sheet format.docx or the Smart
        # Shield Plus V02 UW guideline), and the extraction schema never
        # populates it for this product — a blank row with no
        # underwriting content rather than a real finding.
    ]
    # "STP" (Straight-Through Processing) row removed here: it's a
    # generic-engine reconciler concept with zero grounding in any SBI
    # document or the actual QC data — see the matching removal in
    # _draw_decision_bar for the full citation.
    # Insta-PIWC/Video MER: a real SBI pre-issuance requirement (V02.docx,
    # PIWC row) but not one of the 8 "Requirements to Raise" rules and not
    # a financial-underwriting finding — shown here as its own operational
    # line instead, separate from both the Requirements box and the
    # financial risk-summary panel.
    piwc_gate = next(
        (g for g in (decision.get("gating") or []) if g.get("criterion") == "sbi_piwc_requirement"),
        None,
    )
    if piwc_gate is not None:
        piwc_status = (piwc_gate.get("status") or "").upper()
        if piwc_status == "WARN":
            items.append(
                (
                    "PRE-ISSUANCE",
                    "Insta-PIWC confirmation required (Video MER if unavailable)",
                    "warn",
                )
            )
        elif piwc_status == "NA":
            items.append(
                (
                    "PRE-ISSUANCE",
                    "Insta-PIWC exempt — confirm exemption applied operationally",
                    "neutral",
                )
            )
    # FOLLOW-UP row removed entirely: redundant once this information was
    # pointed at "see above" — the Requirements-to-Raise box above is the
    # one place it needs to live. No replacement content needed here.
    return items


# Scaled ~1.1x from their originally-measured values, from a later
# readability pass, to track the matching ~1.1x font bump below. Row
# height stays fully dynamic (grows with n_lines) either way, so this
# scale is just about matching leading to the larger glyph size rather
# than a hard layout requirement.
_DATA_ROW_OFFSET = 4.94  # row-top to first-line text-top
_DATA_ROW_LEAD = 8.77  # per-extra-wrapped-line cost
_DATA_ROW_BASE = 11.19  # single-line row height


def _draw_data_column(
    c, x0: float, x1: float, items: list, top0: float, note: str | None = None
) -> float:
    # Label column width is sized to THIS column's own longest label (capped)
    # rather than a fixed offset — a column of short labels (DOCS / KYC)
    # gives its values much more room than one with long labels (IIB &
    # COMPLIANCE's "EXISTING COVER (DECLARED)"), matching the reference.
    # Label/value fonts bumped again 5.54→6.09 and 6.33→6.96 for
    # readability. label_w's own measurement font must track the label
    # font size, or a wider glyph set at the new size would wrap/clip
    # against a column-width budget sized for the old, narrower font.
    max_label_w = max((pdfmetrics.stringWidth(lbl, _SANS, 6.09) for lbl, _, _ in items), default=40)
    label_w = min(max(max_label_w + 4, 40.0), 85.0)
    value_x = x0 + label_w + 6
    value_w = x1 - value_x - 3
    top = top0
    for i, (label, value, tone) in enumerate(items):
        # max_lines raised again (4->5 / 8->10) for the same reason as
        # the earlier bump: a larger font wraps the SAME text to more
        # lines within the same column width. Row height is fully dynamic
        # (_DATA_ROW_BASE grows with n_lines) — no fixed-height reason to
        # cap tightly, so raised generously rather than hand-tuning per
        # column width again.
        label_lines = _wrap(label, _SANS, 6.09, label_w - 4, max_lines=5)
        value_lines = _wrap(str(value), _SANS_B, 6.96, value_w, max_lines=10)
        n_lines = max(len(label_lines), len(value_lines), 1)
        row_h = _DATA_ROW_BASE + (n_lines - 1) * _DATA_ROW_LEAD

        c.setFont(_SANS, 6.09)
        c.setFillColor(LABEL_GRAY)
        ly = top + _DATA_ROW_OFFSET
        for ln in label_lines:
            c.drawString(x0 + 3, _ty(ly, 6.09), ln)
            ly += _DATA_ROW_LEAD

        vy = top + _DATA_ROW_OFFSET
        c.setFont(_SANS_B, 6.96)
        c.setFillColor(_TONE_COLOR.get(tone, INK))
        for ln in value_lines:
            c.drawString(value_x, _ty(vy, 6.96), ln)
            vy += _DATA_ROW_LEAD

        top += row_h
        if i < len(items) - 1:
            c.setStrokeColor(ROW_LINE)
            c.setLineWidth(0.58)
            c.line(x0 + 3, PAGE_H - top, x1 - 3, PAGE_H - top)
    if note:
        top += 1.8
        c.setStrokeColor(CARD_LINE)
        c.setLineWidth(0.58)
        c.line(x0, PAGE_H - top, x1, PAGE_H - top)
        top += 4.2
        for ln in _wrap(note, _SANS, 6.09, (x1 - x0) - 6, max_lines=5):
            c.setFont(_SANS, 6.09)
            c.setFillColor(LABEL_GRAY)
            c.drawString(x0 + 1, _ty(top + 2.2, 6.09), ln)
            top += 6.45
    # `top` tracks glyph-TOP positions throughout this file, so it doesn't
    # account for descenders (g/p/y) hanging below the last line's
    # baseline — without this, a divider/border drawn exactly at the
    # returned value clips through descenders of the column's last line
    # (observed: the FINANCIALS note's "labelling." ran through the
    # table's bottom rule).
    return top + 1.5


# ── four-column data table ─────────────────────────────────────────────
# (4-side-by-side-column layout; _draw_data_column above carries the
# font-size/row-metric readability bump, only the column orchestration
# lives in this function.)


def _draw_four_column_table(c, decision: dict, top: float) -> float:
    calcs = decision.get("calculations") or {}
    col_bounds = [
        CONTENT_X0,
        CONTENT_X0 + 141.19,
        CONTENT_X0 + 282.96,
        CONTENT_X0 + 424.15,
        CONTENT_X1,
    ]
    titles = ["FINANCIALS", "IIB & COMPLIANCE", "DOCS / KYC", "POLICY"]

    # Header-bar font/padding bumped again 5.54->6.09 — same ~1.1x ratio
    # as the body label/value bump elsewhere in this pass. header_h/text_top
    # scaled by the same ratio so the bar still frames the bigger text
    # with the same proportional padding.
    header_h = 9.27
    c.setFillColor(HEADER_BAR)
    c.rect(CONTENT_X0, PAGE_H - top - header_h, CONTENT_W, header_h, stroke=0, fill=1)
    c.setFont(_SANS_B, 6.09)
    c.setFillColor(WHITE)
    text_top = top + 2.55  # bar-top-to-label-top padding, scaled from the measured 2.32/5.54
    for i, t in enumerate(titles):
        c.drawString(col_bounds[i] + 3, _ty(text_top, 6.09), t)
    body_top = top + header_h

    fin_items, fin_note = _build_financials_col(decision, calcs)
    cols_items = [
        fin_items,
        _build_iib_col(decision),
        _build_docs_col(decision),
        _build_policy_col(decision),
    ]
    notes = [fin_note, None, None, None]

    bottoms = []
    for i in range(4):
        x0, x1 = col_bounds[i], col_bounds[i + 1]
        bottoms.append(_draw_data_column(c, x0, x1, cols_items[i], body_top, notes[i]))
    box_bottom = max(bottoms)

    c.setStrokeColor(COL_LINE)
    c.setLineWidth(0.58)
    for x in col_bounds[1:-1]:
        c.line(x, PAGE_H - body_top, x, PAGE_H - box_bottom)
    c.line(CONTENT_X0, PAGE_H - box_bottom, CONTENT_X1, PAGE_H - box_bottom)

    return box_bottom + _SECTION_GAP


# ── requirements to raise ──────────────────────────────────────────────

# Exact measured deltas (reference): heading-top to item1-top = 9.1;
# item-to-item averages 7.78 (7.5, 8.1, 7.5, 8.0 across the reference's
# 5 items); no item wrapped to a 2nd line there, so the intra-item
# continuation lead reuses the risk-summary's own measured value (7.6).
# Scaled ~1.1x again along with the Requirements-box / BHB-panel font
# sizes that use them.
_REQ_HEADING_OFFSET = 13.42  # box-top to heading-top
_REQ_FIRST_ITEM_OFFSET = 12.21  # heading-top to item1-top
_REQ_ITEM_LEAD = 10.47  # item-to-item
_REQ_LINE_LEAD = 10.20  # wrapped continuation line within one item


def _draw_requirements_box(c, decision: dict, top: float) -> float:
    items = decision.get("requirements_to_raise") or []
    # Suppressed on DECLINE — DECLINE is this engine's one genuinely
    # terminal verdict (confirmed fraud / disallowed category /
    # verified-insufficient income — see engine.verdict.make_verdict's
    # hard-DECLINE branches, each explicitly "not resolvable by
    # documentation"). requirements_to_raise is computed independently
    # of the verdict, so a DECLINE case can still carry open items (e.g.
    # declined for a disallowed occupation/qualification combination,
    # which no document changes, yet still having "fresh income proof"
    # listed) — showing them implies the case could still proceed if
    # addressed, which isn't true once it's been declined on a terminal
    # ground.
    # REFER keeps the box (genuinely resolvable — the requirements ARE
    # the path forward); FURTHER_REQUIREMENT keeps it (it's the entire
    # basis of that verdict); STANDARD_ISSUANCE never has items to show.
    label = ((decision.get("verdict") or {}).get("label") or "").upper()
    if label == "DECLINE":
        items = [
            "No additional requirements raised for this terminal decline. See the decision and supporting evidence."
        ]
    elif not items:
        items = ["No outstanding requirements recorded."]
    heading = "Requirements to Raise"
    box_top = top
    heading_top = box_top + _REQ_HEADING_OFFSET
    inner_x0, inner_x1 = ML + 6.3, CONTENT_X1 - 6.3
    text_w = inner_x1 - inner_x0 - 14.0

    item_tops, cursor = [], heading_top + _REQ_FIRST_ITEM_OFFSET
    lines_per_item = []
    for item in items:
        text = item["text"] if isinstance(item, dict) else str(item)
        lines = _wrap(text, _SANS, _RISK_BODY_SIZE, text_w, max_lines=5)
        lines_per_item.append(lines)
        item_tops.append(cursor)
        cursor += max(len(lines) - 1, 0) * _REQ_LINE_LEAD + _REQ_ITEM_LEAD
    box_bottom = cursor - _REQ_ITEM_LEAD + 10.0
    box_h = box_bottom - box_top

    c.setStrokeColor(HexColor("#1A1A1A"))
    c.setLineWidth(0.9)
    c.roundRect(CONTENT_X0, PAGE_H - box_bottom, CONTENT_W, box_h, 1.7, stroke=1, fill=0)

    c.setFont(_SANS_B, 7.73)
    c.setFillColor(INK)
    c.drawString(inner_x0, _ty(heading_top, 7.73), heading)

    for idx, (lines, item_top) in enumerate(zip(lines_per_item, item_tops), start=1):
        prefix = f"{idx}."
        c.setFont(_SANS, _RISK_BODY_SIZE)
        c.setFillColor(
            RED if (isinstance(items[idx - 1], dict) and items[idx - 1].get("critical")) else INK
        )
        c.drawString(inner_x0, _ty(item_top, _RISK_BODY_SIZE), prefix)
        tx = inner_x0 + 13.0
        for j, ln in enumerate(lines):
            c.drawString(tx, _ty(item_top + j * _REQ_LINE_LEAD, _RISK_BODY_SIZE), ln)

    return box_bottom + _SECTION_GAP


# ── photo match (full-width mini-table) ────────────────────────────────

_PM_COMPARISON_LABELS = (
    ("PROPOSAL vs KYC", "vs_kyc"),
    ("PROPOSAL vs PIWC", "vs_piwc"),
    ("PROPOSAL vs MEDICAL", "vs_medical"),
)


def _draw_photo_match_block(c, decision: dict, top: float) -> float:
    """Full-width face-match mini-table — proposal photo as base image,
    compared separately against the KYC photo, the Insta-PIWC capture,
    and any medical-document photo. Moved out of the narrow ~140pt IIB
    & Compliance cell — the comparison labels/reasons kept overflowing
    or wrapping choppily no matter how the text was shortened — into
    its own full-width block below the 4-column table, modelled on the
    client's own reference layout: a small blue header bar per
    comparison with the result + confidence % centred underneath it."""
    app = decision.get("applicant") or {}
    pm_set = app.get("photo_match_set") or {}
    results = [(label, pm_set.get(key)) for label, key in _PM_COMPARISON_LABELS]

    # engine.identity_verification composes a fixed, specific reasoning
    # string per missing-photo case (not LLM free text) — map those to a
    # short column-width label instead of the generic "Not performed",
    # which would tell the reader nothing about WHICH of the three
    # photos was actually missing.
    _skip_short = {
        "No proposal/base photo (RECENTPHOTOGRAPH) on file.": "Proposal photo not available",
        "No KYC photo on file.": "KYC photo not available",
        "No Insta-PIWC photo on file.": "PIWC photo not available",
        "No medical-document photo on file.": "No medical photo on file",
    }

    # For an "undetectable" result (no face could be located in one of the
    # two images), determine whether the broken image is the shared
    # proposal/base photo or that column's own compare photo: all three
    # comparisons anchor on the SAME base_file (the proposal photo), so if
    # every comparison sharing a given base_file comes back "undetectable",
    # that shared photo is almost certainly the actual problem rather than
    # each different compare file independently being corrupt (e.g. two
    # comparisons sharing one base proposal photo both coming back
    # "undetectable" against two different, unrelated compare files —
    # the common element is the proposal photo, not either compare
    # file).
    _base_file_outcomes: dict[str, set] = {}
    for _, r in results:
        bf = (r or {}).get("base_file")
        if bf:
            _base_file_outcomes.setdefault(bf, set()).add((r or {}).get("match_result"))

    _COMPARE_SIDE_LABEL = {"KYC": "KYC", "PIWC": "PIWC", "MEDICAL": "Medical"}

    def _undetectable_text(label: str, result: dict) -> str:
        bf = result.get("base_file")
        outcomes = _base_file_outcomes.get(bf, set())
        if bf and outcomes == {"undetectable"}:
            return "Proposal photo not available"
        compare_key = label.split(" vs ")[-1] if " vs " in label else ""
        compare_name = _COMPARE_SIDE_LABEL.get(compare_key, "Comparison")
        return f"{compare_name} photo not available"

    label_w = 72.0
    c.setFont(_SANS_B, 6.33)
    c.setFillColor(INK)
    c.drawString(CONTENT_X0 + 2, _ty(top + 6.0, 6.33), "PHOTO MATCH")

    # All three skipped for the identical reason (almost always "no
    # proposal/base photo on file") — one plain line instead of three
    # empty-looking header cells with nothing under them.
    reasons = {(r or {}).get("reasoning") for _, r in results}
    all_skipped = all((r or {}).get("match_result") in (None, "skipped") for _, r in results)
    if all_skipped and len(reasons) == 1:
        reason = (results[0][1] or {}).get("reasoning") or "Not performed."
        c.setFont(_SANS, 5.76)
        c.setFillColor(LABEL_GRAY)
        c.drawString(CONTENT_X0 + label_w, _ty(top + 6.0, 5.76), reason)
        return top + 13.0

    n = len(results)
    gutter = 6.0
    table_x0 = CONTENT_X0 + label_w
    table_w = CONTENT_X1 - table_x0
    col_w = (table_w - gutter * (n - 1)) / n
    header_h = 9.0

    for i, (label, result) in enumerate(results):
        cx0 = table_x0 + i * (col_w + gutter)
        c.setFillColor(HEADER_BAR)
        c.rect(cx0, PAGE_H - (top + header_h), col_w, header_h, stroke=0, fill=1)
        c.setFont(_SANS_B, 5.41)
        c.setFillColor(WHITE)
        label_w_px = pdfmetrics.stringWidth(label, _SANS_B, 5.41)
        c.drawString(cx0 + max((col_w - label_w_px) / 2, 2), _ty(top + 2.3, 5.41), label)

        result_top = top + header_h + 4.0
        mr = (result or {}).get("match_result")
        if not result or mr in (None, "skipped"):
            reasoning = (result or {}).get("reasoning") or ""
            txt = _skip_short.get(reasoning, "Not performed")
            tone_color = MUTE2
        elif mr == "undetectable":
            # "undetectable" means the model could not even locate a face
            # in one of the two images (a blank/corrupted/unreadable
            # photo) — its confidence value is not a real similarity
            # score, so showing it as a percentage (e.g. "Undetectable —
            # 5%") reads as "we compared them and they're 5% alike," which
            # is misleading (e.g. a file matched by the RECENTPHOTOGRAPH
            # filename convention that exists but is a blank/blurry scan
            # with no face at all — functionally the same as no usable
            # proposal photo being on file, so it's worded the same way,
            # naming which specific photo is the problem, rather than as
            # a misleading %).
            txt, tone_color = _undetectable_text(label, result), RED
        else:
            conf = result.get("confidence")
            pct_txt = f"{round(float(conf) * 100)}%" if isinstance(conf, (int, float)) else "—"
            mr_txt = _PHOTO_MATCH_LABELS.get(mr, (mr or "Not Available").title())
            txt = f"{mr_txt} — {pct_txt}"
            tone_color = GREEN if mr == "match" else (RED if mr == "no-match" else AMBER)
        c.setFont(_SANS_B, 6.09)
        c.setFillColor(tone_color)
        txt_w = pdfmetrics.stringWidth(txt, _SANS_B, 6.09)
        c.drawString(cx0 + max((col_w - txt_w) / 2, 2), _ty(result_top, 6.09), txt)

    return top + header_h + 4.0 + 7.0 + 4.0


# ── entry point ─────────────────────────────────────────────────────────


def render_triage_sheet(decision: dict, pdf_path) -> None:
    """Render the single-page SBI Smart Shield Plus "Triage Layout" case
    sheet to pdf_path. This is the sole case-sheet renderer in the
    pipeline (pipeline/run.py and pipeline/batch.py call it
    unconditionally)."""
    _register_fonts()
    c = _canvas.Canvas(str(pdf_path), pagesize=(PAGE_W, PAGE_H))
    c.setTitle(f"Financial Underwriting Case Sheet — {decision.get('case_id', '')}")

    # Order follows the source document's own element sequence (Case
    # sheet format.docx): ID bar -> Requirements to Raise -> Decision +
    # Risk Summary -> detail tables. Underwriting actionable items come
    # up front, ahead of the decision and its supporting detail.
    top = _draw_header(c)
    top = _draw_id_bar(c, decision, top)
    top = _draw_bhb_panel(c, decision, top)
    top = _draw_requirements_box(c, decision, top)
    top = _draw_decision_bar(c, decision, top)
    top = _draw_risk_summary(c, decision, top)
    from underwriting_platform.renderer.reference_tables import draw_sections
    import sys

    content_bottom = draw_sections(c, decision, top, sys.modules[__name__])
    content_bottom = _draw_photo_match_block(c, decision, content_bottom + 4.0)

    c.showPage()
    c.save()
