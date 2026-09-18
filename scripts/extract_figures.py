"""Render specific pages of gridwise_presentation_1.pdf to PNG so they can be
embedded in README.md.

Pages are 1-indexed in the human view; PyMuPDF is 0-indexed.
The presentation is landscape, so each slide is one figure.
"""
from pathlib import Path

import pymupdf

PDF = Path(__file__).resolve().parents[2] / "gridwise_presentation_1.pdf"
OUT = Path(__file__).resolve().parents[1] / "docs" / "figures"
OUT.mkdir(parents=True, exist_ok=True)

# (page_index_0based, output_filename, caption)
TARGETS = [
    (1,  "01_challenge.png",          "Challenge in one picture"),
    (3,  "02_architecture.png",       "Five-stage architecture pipeline"),
    (5,  "03_six_directives.png",     "The closed vocabulary of six directives"),
    (7,  "04_schedule_sample01.png",  "SAMPLE-01: 24-hour load + battery schedule (LP optimal)"),
    (8,  "05_solar_reduction.png",    "One note applied: solar_reduction over the midday window"),
    (12, "06_measured.png",           "Measured, not asserted: live numbers from the deployed service"),
]

doc = pymupdf.open(PDF)
for idx, name, caption in TARGETS:
    page = doc[idx]
    # 2x scale so the rendered PNG is sharp on hi-DPI screens
    mat = pymupdf.Matrix(2.0, 2.0)
    pix = page.get_pixmap(matrix=mat)
    out_path = OUT / name
    pix.save(out_path)
    print(f"wrote {out_path.name} ({pix.width}x{pix.height})  -- {caption}")
print(f"\nAll figures in: {OUT}")
