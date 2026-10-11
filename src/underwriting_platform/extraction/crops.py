"""Small label/value crops only; never resend whole pages or folders."""

import base64
import io
import re

from PIL import Image


def field_crops(documents, maximum=2):
    crops = []
    for document in documents:
        for page in document.ocr_pages:
            number = page.get("page")
            indices = document.image_page_numbers or list(range(1, len(document.images_b64) + 1))
            if number not in indices or not page.get("image_size"):
                continue
            with Image.open(
                io.BytesIO(base64.b64decode(document.images_b64[indices.index(number)]))
            ) as image:
                width, height = page["image_size"]
                for line in page.get("lines", []):
                    if not re.search(
                        r"applicant|life assured|date of birth|dob|occupation|sum assured|education|residen",
                        line["text"],
                        re.I,
                    ):
                        continue
                    box = [
                        max(0, line["x0"] - 20),
                        max(0, line["y0"] - 20),
                        width,
                        min(height, line["y1"] + 50),
                    ]
                    region = image.crop(
                        (
                            box[0] * image.width / width,
                            box[1] * image.height / height,
                            box[2] * image.width / width,
                            box[3] * image.height / height,
                        )
                    ).convert("RGB")
                    region.thumbnail((1000, 250))
                    buffer = io.BytesIO()
                    region.save(buffer, format="JPEG", quality=75)
                    crops.append(
                        {
                            "document": document.filename,
                            "page": number,
                            "crop_box": box,
                            "image": base64.b64encode(buffer.getvalue()).decode("ascii"),
                        }
                    )
                    if len(crops) == maximum:
                        return crops
    return crops
