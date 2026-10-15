"""Offline face detection used only to recognise person photographs."""

import base64
import io
from functools import lru_cache

from PIL import Image

MIN_FACE_AREA_RATIO = 0.03
MAX_TEXT_CHARS = 40


@lru_cache(maxsize=1)
def _cascade():
    import cv2

    return cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )


def contains_face(encoded_image):
    try:
        import cv2
        import numpy as np
    except ImportError:
        return False
    try:
        with Image.open(io.BytesIO(base64.b64decode(encoded_image))) as image:
            gray = np.asarray(image.convert("L"))
        height, width = gray.shape
        faces = _cascade().detectMultiScale(
            gray,
            scaleFactor=1.1,
            minNeighbors=5,
            minSize=(max(40, width // 12), max(40, height // 12)),
        )
    except (ValueError, OSError, cv2.error):
        return False
    return any(w * h >= MIN_FACE_AREA_RATIO * width * height for _, _, w, h in faces)


def page_image(document, number):
    numbers = document.image_page_numbers or list(range(1, len(document.images_b64) + 1))
    if number in numbers and numbers.index(number) < len(document.images_b64):
        return document.images_b64[numbers.index(number)]
    return None
