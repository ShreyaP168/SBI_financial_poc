"""Conservative content deduplication; perceptual similarity never proves equality."""

import base64
import hashlib
import io

from PIL import Image


def collapse_identical_documents(documents):
    """Collapse re-encoded copies only with identical text and all page pixels.

    Partially rendered PDFs and unreadable sources cannot safely be collapsed.
    Keep aliases for audit; original source files remain retained independently.
    """
    seen, kept, aliases = {}, [], {}
    for document in documents:
        if (
            document.extraction_error
            or document.page_count < 1
            or len(document.images_b64) != document.page_count
        ):
            kept.append(document)
            continue
        digest = hashlib.sha256(document.text.encode())
        digest.update(str(document.page_count).encode())
        try:
            for encoded in document.images_b64:
                with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
                    pixels = image.convert("RGB")
                    digest.update(str(pixels.size).encode())
                    digest.update(pixels.tobytes())
        except (ValueError, OSError):
            kept.append(document)
            continue
        key = digest.hexdigest()
        if key in seen:
            aliases[document.filename] = seen[key]
        else:
            seen[key] = document.filename
            kept.append(document)
    return kept, aliases


def is_face_photo(document, kind):
    normalized = document.filename.lower().replace("_", "").replace("-", "")
    return kind in {
        "photograph",
        "photo",
        "recent_photograph",
        "piv_photo",
        "pivc_photo",
        "recentphotograph",
        "pivcphoto",
    } or any(marker in normalized for marker in ("recentphotograph", "pivcphoto"))
