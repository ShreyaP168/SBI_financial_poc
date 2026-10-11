"""Verify ZIP extraction in the orchestrator's file collector.

Reproduces the C312315043 hand-off bug: case folder has 4 loose PDFs +
8 ZIPs (containing TMT, MER, blood reports, etc.). The pre-fix pipeline
saw only the 4 loose files; the medical agent had nothing to score.
"""
from __future__ import annotations
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from underwriting_platform.orchestrator.master import _collect_case_files  # noqa: E402

CASE_DIR = Path(r"D:/107_APA/TALIC/ITR-1/100_cases/All 100 cases/74_C312315043")


def main() -> int:
    if not CASE_DIR.exists():
        print(f"FAIL: case folder not found: {CASE_DIR}")
        return 1

    loose = [p for p in CASE_DIR.iterdir()
             if p.is_file() and p.suffix.lower() != ".zip"]
    zips = sorted(CASE_DIR.glob("*.zip"))
    print(f"Source: {len(loose)} loose files + {len(zips)} ZIP(s)")

    with tempfile.TemporaryDirectory() as tmp:
        collected = _collect_case_files(CASE_DIR, Path(tmp))
        names = sorted(p.name for p in collected)

        print(f"\nCollected {len(collected)} unique processable files:")
        for n in names:
            print(f"  - {n}")

        # Pre-fix behaviour: only 4 loose PDFs visible.
        # Post-fix expectations:
        #  * count > loose count (ZIP members included)
        #  * TMT.pdf present (from TMT reports done by TPA.zip)
        #  * an MER-like file present
        #  * 2D echo present (from Doctor Certificate 2025.zip)
        assert len(collected) > len(loose), \
            f"expected > {len(loose)} files after ZIP extraction; got {len(collected)}"

        def has(substr: str) -> bool:
            return any(substr.lower() in n.lower() for n in names)

        for needed in ("tmt", "medical examination", "blood report",
                       "doctor certificate", "2d echo", "hernia"):
            assert has(needed), f"expected to find '{needed}' after ZIP extraction"

    print("\nPASS — ZIP extraction now exposes medical evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
