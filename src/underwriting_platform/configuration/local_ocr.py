"""Central configuration for the SBI Bond local OCR adapters."""

import os
import shutil
from pathlib import Path


def get(key, default=None):
    defaults = {"OCR_TIMEOUT_SECONDS": 45, "OCR_PSM": 6, "OCR_LANGUAGE": "eng"}
    value = os.getenv(key, defaults.get(key, default))
    if key in {"OCR_TIMEOUT_SECONDS", "OCR_PSM"}:
        value = int(value)
        if key == "OCR_TIMEOUT_SECONDS" and not 1 <= value <= 60:
            raise ValueError("OCR timeout must be between 1 and 60 seconds")
        if key == "OCR_PSM" and not 0 <= value <= 13:
            raise ValueError("Invalid OCR page segmentation mode")
    return value


def provider():
    value = os.getenv("UW_LOCAL_OCR_PROVIDER", "tesseract")
    if value not in {"off", "tesseract", "rapidocr"}:
        raise ValueError("Unsupported local OCR provider")
    return value


def executable():
    value = get("TESSERACT_CMD") or shutil.which("tesseract")
    windows = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    return value or (str(windows) if windows.is_file() else None)


def assets():
    """Actual local model/executable inputs included in extraction revisions."""
    if provider() == "off":
        return []
    if provider() == "rapidocr":
        import importlib.util

        spec = importlib.util.find_spec("rapidocr_onnxruntime")
        return list(Path(spec.origin).parent.rglob("*.onnx")) if spec and spec.origin else []
    binary = executable()
    if not binary:
        return []
    roots = [Path(binary).parent / "tessdata", Path("/usr/share/tesseract-ocr/5/tessdata")]
    if os.getenv("TESSDATA_PREFIX"):
        roots.insert(0, Path(os.environ["TESSDATA_PREFIX"]))
    files = [Path(binary)]
    for language in dict.fromkeys(str(get("OCR_LANGUAGE")).split("+") + ["osd"]):
        found = next(
            (
                root / f"{language}.traineddata"
                for root in roots
                if (root / f"{language}.traineddata").is_file()
            ),
            None,
        )
        if found:
            files.append(found)
    return files
