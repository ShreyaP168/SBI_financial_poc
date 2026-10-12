"""Explicit model download/setup command; never invoked while reading case documents."""

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path

from underwriting_platform.configuration.paths import RUNTIME_ROOT


def prepare_configuration(config, resolve):
    """Provision only OCR, layout and table components, with all models local."""
    config.update(
        batch_size=1,
        use_doc_preprocessor=False,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_seal_recognition=False,
        use_formula_recognition=False,
        use_chart_recognition=False,
        use_region_detection=False,
    )
    modules = config["SubModules"]
    for name in ("ChartRecognition", "RegionDetection"):
        modules.pop(name, None)
    pipelines = config["SubPipelines"]
    for name in ("DocPreprocessor", "SealRecognition", "FormulaRecognition"):
        pipelines.pop(name, None)

    def walk(value):
        if isinstance(value, dict):
            if "model_name" in value:
                value["model_dir"] = str(Path(resolve(value["model_name"])).resolve())
            if "batch_size" in value:
                value["batch_size"] = 1
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(config)
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=RUNTIME_ROOT / "paddle")
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(root / "cache")
    os.environ["PADDLE_PDX_MODEL_SOURCE"] = "bos"
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    import yaml
    from paddlex.inference.utils.official_models import official_models

    distribution = importlib.metadata.distribution("paddlex")
    template = distribution.locate_file("paddlex/configs/pipelines/PP-StructureV3.yaml")
    config = prepare_configuration(
        yaml.safe_load(Path(template).read_text(encoding="utf8")),
        lambda name: official_models[name],
    )
    target = root / "pipeline.yaml"
    target.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf8")
    manifest = {
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("paddleocr", "paddlepaddle", "paddlex")
        },
        "files": {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (root / "cache" / "official_models").rglob("*")
            if path.is_file()
        },
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf8")
    print(f"UW_PADDLE_CONFIG={target}")


if __name__ == "__main__":
    main()
