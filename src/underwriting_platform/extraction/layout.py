"""Rebuild table-like rows from OCR line geometry so labels meet their values."""

GEOMETRY = ("x0", "y0", "x1", "y1")
ROW_TOLERANCE = 0.6


def has_geometry(lines):
    return bool(lines) and all(all(key in line for key in GEOMETRY) for line in lines)


def rows_text(lines, minimum_confidence=0.9):
    kept = [line for line in lines if line.get("confidence", 0) >= minimum_confidence]
    if not has_geometry(kept):
        return "\n".join(line["text"] for line in kept)
    ordered = sorted(kept, key=lambda line: (line["y0"] + line["y1"]) / 2)
    rows, current, centre, height = [], [], None, None
    for line in ordered:
        middle = (line["y0"] + line["y1"]) / 2
        size = line["y1"] - line["y0"]
        if current and abs(middle - centre) <= ROW_TOLERANCE * max(size, height):
            current.append(line)
        else:
            if current:
                rows.append(current)
            current, centre, height = [line], middle, size
    if current:
        rows.append(current)
    return "\n".join(" ".join(line["text"] for line in sorted(row, key=lambda x: x["x0"])) for row in rows)
