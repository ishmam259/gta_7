"""Regenerate the 'Measured, not asserted' tile with current numbers.

Replaces the stale "233 offline tests" / "10-way concurrency" text from the
original PDF (pre-adversarial-suite) with current numbers from this repo.
"""
from pathlib import Path

import pymupdf

PDF = Path(__file__).resolve().parents[2] / "gridwise_presentation_1.pdf"
OUT = Path(__file__).resolve().parents[1] / "docs" / "figures" / "06_measured.png"


def find_and_replace_text(page, replacements: dict[str, str]) -> None:
    """Find each key on the page and replace it with its value, preserving styling."""
    for needle, replacement in replacements.items():
        rects = page.search_for(needle)
        for rect in rects:
            # Get the current font/size of the matched span so replacement text matches visually.
            # Use the dict-style API for cleaner redacting.
            page.add_redact_annot(rect, replacement, fontsize=0)  # 0 -> match existing
    page.apply_redactions()


def main() -> None:
    doc = pymupdf.open(PDF)
    page = doc[12]  # "Measured, not asserted" is slide 13 (0-indexed 12)

    # The tile contains "233" and "10-way load" which we want to update.
    replacements = {
        "233":   "383",            # actual test count (baseline + adversarial suite)
        "10-way load": "20-way load",  # measured concurrency
    }
    find_and_replace_text(page, replacements)

    mat = pymupdf.Matrix(2.0, 2.0)
    pix = page.get_pixmap(matrix=mat)
    pix.save(OUT)
    print(f"refreshed {OUT.name} ({pix.width}x{pix.height})")


if __name__ == "__main__":
    main()
