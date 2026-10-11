"""Full-width case-sheet tables matching the supplied Word reference."""

import re
from html import escape

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, Table, TableStyle


def plain_text(value):
    text = str(value)
    if re.search(r"income|qualification|verified|eligib|\bSA\b|\bHLV\b", text, re.I):
        return re.sub(r"\bfloors?\b", "minimum income", text, flags=re.I)
    return text


def draw_sections(canvas, decision, top, renderer):
    """Keep all rows; continue on a new branded page rather than clipping."""
    font = renderer._SANS
    label_style = ParagraphStyle(
        "reference-label", fontName=renderer._SANS_B, fontSize=7.1, leading=9
    )
    value_style = ParagraphStyle("reference-value", fontName=font, fontSize=7.3, leading=9.2)
    heading_style = ParagraphStyle(
        "reference-heading",
        fontName=renderer._SANS_B,
        fontSize=8.5,
        leading=10,
        textColor=renderer.NAVY,
    )
    width = renderer.CONTENT_W
    columns = [width * 0.18, width * 0.32, width * 0.18, width * 0.32]

    def paragraph(value, style, tone=None):
        text = escape(plain_text(value)).replace("\n", "<br/>")
        if tone:
            color = renderer._TONE_COLOR.get(tone, renderer.INK).hexval()
            text = f'<font color="{color}">{text}</font>'
        return Paragraph(text, style)

    def new_page():
        canvas.showPage()
        cursor = renderer._draw_header(canvas)
        canvas.setFont(renderer._SANS_B, 8)
        canvas.drawString(
            renderer.ML,
            renderer.PAGE_H - cursor - 10,
            f"Case {decision.get('case_id', '')} - continued",
        )
        return cursor + 20

    def draw_table(title, items, note=None):
        nonlocal top
        header = [paragraph(title, heading_style), "", "", ""]
        data = [header]
        for index in range(0, len(items), 2):
            row = []
            for label, value, tone in items[index : index + 2]:
                row.extend([paragraph(label, label_style), paragraph(value, value_style, tone)])
            row += [""] * (4 - len(row))
            data.append(row)
        if note:
            data.append([paragraph(note, value_style), "", "", ""])
        table = Table(data, colWidths=columns, repeatRows=1, splitInRow=1)
        commands = [
            ("SPAN", (0, 0), (-1, 0)),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF0F6")),
            ("TEXTCOLOR", (0, 0), (-1, 0), renderer.NAVY),
            ("BACKGROUND", (0, 1), (0, -1), colors.HexColor("#F4F5F7")),
            ("BACKGROUND", (2, 1), (2, -1), colors.HexColor("#F4F5F7")),
            ("GRID", (0, 1), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
        if note:
            commands += [
                ("SPAN", (0, -1), (-1, -1)),
                ("BACKGROUND", (0, -1), (-1, -1), colors.white),
            ]
        table.setStyle(TableStyle(commands))
        while True:
            available = renderer.PAGE_H - top - 24
            _, height = table.wrap(width, available)
            if height <= available:
                table.drawOn(canvas, renderer.CONTENT_X0, renderer.PAGE_H - top - height)
                top += height + 7
                break
            parts = table.split(width, available)
            if len(parts) > 1:
                _, height = parts[0].wrap(width, available)
                parts[0].drawOn(canvas, renderer.CONTENT_X0, renderer.PAGE_H - top - height)
                table = parts[1]
                top = new_page()
            elif top > 80:
                top = new_page()
            else:
                raise ValueError("Case sheet row cannot fit on a reference-format page")

    financial, financial_note = renderer._build_financials_col(
        decision, decision.get("calculations") or {}
    )
    docs = renderer._build_docs_col(decision)
    bank = [row for row in docs if row[0] in {"BANK STMT", "FOIR"}]
    draw_table("IIB & COMPLIANCE", renderer._build_iib_col(decision))
    draw_table("FINANCIAL OVERVIEW", financial, financial_note)
    draw_table("BANK STATEMENT ANALYSIS", bank)
    draw_table("DOCUMENTS / KYC", [row for row in docs if row not in bank])
    draw_table("POLICY DETAILS", renderer._build_policy_col(decision))
    if top > renderer.PAGE_H - 55:
        top = new_page()
    return top
