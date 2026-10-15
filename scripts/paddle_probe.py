import os
import sys
import time

from PIL import Image

os.environ["UW_LOCAL_OCR_PROVIDER"] = "paddle_structure"
os.environ["UW_PADDLE_CONFIG"] = r"C:\Users\Shreya.poojary\Desktop\project\SBIF_UW\data\runtime\paddle\pipeline.yaml"

from underwriting_platform.ocr import read_page

for path in sys.argv[1:]:
    started = time.time()
    with Image.open(path) as image:
        result = read_page(image.convert("RGB"), 1)
    print("#####", os.path.basename(path), result.get("status"), result.get("provider"), f"{time.time() - started:.0f}s", "lines", len(result.get("lines", [])))
    for line in result.get("lines", []):
        text = line["text"]
        if any(key in text.lower() for key in ("gross", "month", "earning", "pay", "total", "basic", "net")):
            print("   ", round(line["confidence"], 2), text[:110])
