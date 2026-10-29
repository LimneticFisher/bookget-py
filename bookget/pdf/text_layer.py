"""Invisible, searchable text over page images ("double-layer" PDF).

Reads ``page_layout.json`` (written by adapters that know where every
character sits on the scan, e.g. 识典古籍's word boxes) and places each
character, in text render mode 3 (invisible), on its box. The scan stays
exactly as it is; the PDF becomes searchable and copyable, and a search hit
highlights the character on the image.

Needs PyMuPDF (``pip install bookget[pdf]``).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

LAYOUT_FILE = "page_layout.json"


def load_layout(book_dir: Path) -> Optional[Dict]:
    path = Path(book_dir) / LAYOUT_FILE
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(f"Unreadable {path}: {e}")
        return None


def _box(ch: Dict, sx: float, sy: float):
    xs, ys = ch["poly"][0::2], ch["poly"][1::2]
    return min(xs) * sx, min(ys) * sy, max(xs) * sx, max(ys) * sy


def _columns(chars: List[Dict], sx: float, sy: float) -> List[List[tuple]]:
    """Split characters (in the site's reading order) into vertical runs.

    A run continues while the character stays in the same text line, sits in
    the same column (x centre within half a column width) and moves down the
    page. Double-row small-print notes therefore become two runs.
    """
    runs: List[List[tuple]] = []
    for ch in chars:
        box = _box(ch, sx, sy)
        xc, yc, w = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, box[2] - box[0]
        if runs:
            prev_ch, prev = runs[-1][-1]
            pxc, pyc, pw = (prev[0] + prev[2]) / 2, (prev[1] + prev[3]) / 2, prev[2] - prev[0]
            if (ch.get("line") == prev_ch.get("line") and abs(xc - pxc) <= max(w, pw) / 2
                    and yc > pyc):
                runs[-1].append((ch, box))
                continue
        runs.append([(ch, box)])
    return runs


def _write_column(page, run, font, line_h: float, pymupdf) -> None:
    """One column as one line of invisible text running downward.

    Characters are laid out horizontally from a pivot at the column's top
    and the whole run is rotated about it so it reads top to bottom
    (PyMuPDF's morph with Matrix(-90)). Each glyph is sized so it reaches the
    next character's start: viewers turn gaps between glyphs into spaces or
    line breaks, which would stop phrase searches matching across them.
    """
    xs0 = sorted(b[0] for _, b in run)
    xs1 = sorted(b[2] for _, b in run)
    x0, x1 = xs0[len(xs0) // 2], xs1[len(xs1) // 2]          # median column edges
    base = max((x1 - x0) / line_h, 1.0)
    # Baseline on the column's left edge: after rotation the glyphs' "up"
    # points right, so the line box spans the column's width.
    px, py = x0 + (-font.descender) * base, run[0][1][1]
    tw = pymupdf.TextWriter(page.rect)
    for i, (ch, (_, y0, _, y1)) in enumerate(run):
        reach = (run[i + 1][1][1] if i + 1 < len(run) else y1) - y0
        size = base if reach > 1.5 * base else min(max(reach, 0.6 * base), 1.5 * base)
        adv = font.text_length(ch["t"], fontsize=size) or size
        size *= size / adv                                       # advance == reach
        tw.append((px + (y0 - py), py), ch["t"], font=font, fontsize=size)
    tw.write_text(page, render_mode=3, morph=(pymupdf.Point(px, py), pymupdf.Matrix(-90)))


def add_text_layer(pdf_path: Path, book_dir: Path, page_files: List[str],
                   font_path: str) -> int:
    """Add the text layer in place; returns the number of pages that got text.

    ``page_files`` lists the image file of each leading PDF page, in order
    (the native engine puts image pages first). Box coordinates are pixels of
    that image and are scaled to the page.
    """
    import pymupdf  # optional dependency

    layout = load_layout(book_dir)
    if not layout or not layout.get("pages"):
        return 0
    font = pymupdf.Font(fontfile=font_path)
    # Viewers highlight a hit over the font's whole line box (ascender to
    # descender, ~1.3 em for CJK fonts), so size each glyph by that height to
    # make the highlight land on the character's box.
    line_h = max(font.ascender - font.descender, 1.0)
    doc = pymupdf.open(str(pdf_path))
    done = 0
    try:
        for i, rel in enumerate(page_files):
            entry = layout["pages"].get(Path(rel).name)
            if not entry or i >= doc.page_count or not entry.get("chars"):
                continue
            page = doc[i]
            img = page.get_images(full=True)
            if not img:
                continue
            img_w, img_h = img[0][2], img[0][3]
            sx, sy = page.rect.width / img_w, page.rect.height / img_h
            for run in _columns(entry["chars"], sx, sy):
                _write_column(page, run, font, line_h, pymupdf)
            done += 1
        if done:
            doc.subset_fonts()
            tmp = Path(str(pdf_path) + ".tmp")
            doc.save(str(tmp), garbage=3, deflate=True)
    finally:
        doc.close()
    if done:
        os.replace(tmp, pdf_path)
    return done
