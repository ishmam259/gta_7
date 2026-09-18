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

    # The tile had stale "233" / "80/81" / "≈3 s" / "20-way load" before.
    # Earlier refresh already updated "233" -> "383" and "10-way" -> "20-way".
    # Now revert 20-way -> 10-way (teammate's measurement) and 80/81 -> 81/81.
    # Also update p95 "3 s" -> "2.4 s" (the ≈ glyph is its own font span, leave it alone).
    # IMPORTANT: only replace the numeric/word spans, never the ≈ glyph
    # (replacing it produces a missing-glyph '?' artifact).
    replacements = {
        "233": "383",                    # 233 offline tests -> 383 (post-adversarial-suite)
        "80/81": "81/81",                # paraphrase corpus -> teammate's 81/81
        "20-way load": "10-way load",    # revert to teammate's smoke-test concurrency
        "3 s": "2.4 s",                  # p95 3 s -> 2.4 s (matches resolved table)
    }
    find_and_replace_text(page, replacements)

    mat = pymupdf.Matrix(2.0, 2.0)
    pix = page.get_pixmap(matrix=mat)
    pix.save(OUT)
    print(f"refreshed {OUT.name} ({pix.width}x{pix.height})")


if __name__ == "__main__":
    main()
