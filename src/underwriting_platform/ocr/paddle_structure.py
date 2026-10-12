"""Optional PP-StructureV3 reader using explicitly provisioned local models."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory

from .rapidocr import TextLine


class PaddleStructureExtractor:
    engine_name = "paddle_structure_v3"
    model_name = "PP-StructureV3"
    model_version = "paddleocr-3.7.0"

    def __init__(self, backend=None):
        self.backend = backend
        self.last_tables = []

    def extract(self, image):
        if self.backend is None:
            config = os.getenv("UW_PADDLE_CONFIG")
            if not config or not Path(config).is_file():
                raise ValueError("Provision a local Paddle pipeline configuration first")
            import yaml

            configuration = yaml.safe_load(Path(config).read_text(encoding="utf8"))
            model_dirs = []

            def walk(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key == "model_dir":
                            if not child or not Path(child).is_dir():
                                raise ValueError("Paddle models must be provisioned locally")
                            model_dirs.append(child)
                        else:
                            walk(child)
                elif isinstance(value, list):
                    for child in value:
                        walk(child)

            walk(configuration)
            if not model_dirs:
                raise ValueError("Paddle configuration must specify local model directories")
            from paddleocr import PPStructureV3

            # Paddle 3.3 CPU oneDNN cannot execute some layout-model PIR attributes.
            # Use the portable CPU executor consistently across local platforms.
            self.backend = PPStructureV3(paddlex_config=config, device="cpu", enable_mkldnn=False)
        with TemporaryDirectory(prefix="uw_local_structure_") as temporary:
            path = Path(temporary) / "page.png"
            image.save(path)
            results = list(self.backend.predict(input=str(path)))
        lines, tables = [], []
        for result in results:
            data = result.json if hasattr(result, "json") else result
            data = data.get("res", data)
            ocr = data.get("overall_ocr_res") or {}
            for text, score, box in zip(
                ocr.get("rec_texts", []),
                ocr.get("rec_scores", []),
                ocr.get("rec_boxes", []),
                strict=True,
            ):
                lines.append(TextLine(str(text), *map(float, box), float(score)))
            for table in data.get("table_res_list", []):
                tables.append(
                    {
                        "html": table.get("pred_html", ""),
                        "cell_boxes": table.get("cell_box_list", []),
                        "basis": "paddle_structure_v3",
                    }
                )
        self.last_tables = tables
        return lines
