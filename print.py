#!/usr/bin/env python3
"""
TCS Wipe Certificate Generator (standalone debug tool)
- Overlays job data at fixed XY coordinates onto a PDF template (wipe_cert.pdf)
- Provides debug guides (grid, crosshairs, rulers, bounding boxes) to tune positions
- Supports global nudges (dx/dy) from the CLI during iteration
- Optional: prints via CUPS `lp` once satisfied

Coordinate model:
- By default, positions are defined in POINTS from the TOP-LEFT of the page
  (easier when you eyeball positions from a Word/PDF export).
- Internally, we convert to ReportLab's bottom-left origin.

Author: TCS
"""

import argparse
import io
import os
import subprocess
from dataclasses import dataclass
from typing import Dict, Tuple

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.colors import Color, black, HexColor
from PyPDF2 import PdfReader, PdfWriter, PdfMerger

# -------------- Config --------------

DEFAULT_TEMPLATE = "wipe_cert.pdf"

# Fonts: we try DejaVuSans if present (nicer), else Helvetica core font.
FALLBACK_FONT = "Helvetica"
TRY_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]

# Fake job data for preview
FAKE = {
    "MODEL":    "WDC WD4000AAKS-00A7B0",
    "SERIAL":   "WD-WMASY0110628",
    "CAPACITY": "400.1 GB",
    "LEVEL":    "Low (Client Quick Wipe)",
    "METHOD":   "Zero-fill (1 pass)",
    "STARTED":  "2025-08-22 10:15",
    "FINISHED": "2025-08-22 11:42",
    "RESULT":   "SUCCESS",
    "OPERATOR": "JG / TCS",
    "CERT_ID":  "CERT-20250822-0001",
}

# Field placements (points) measured from TOP-LEFT.
# Adjust these while previewing with --debug/--grid/--crosshair.
# Tip: Start with rough values, then nudge using --dx/--dy until perfect.
FIELDS_TOPLEFT: Dict[str, Tuple[float, float]] = {
    # Device details block
    "MODEL":    (200,  185),   # x, y-from-top
    "SERIAL":   (200,  218),
    "CAPACITY": (200,  245),

    # Sanitization details
    "LEVEL":    (200,  310),
    "METHOD":   (200,  336),
    "STARTED":  (200,  365),
    "FINISHED": (200,  390),
    "RESULT":   (200,  420),

    # Verified by
    "OPERATOR": (200,  458),

    # Optional: certificate id small at bottom-right or header corner
    "CERT_ID":  (440,   10),
}

# Font sizes per field (override defaults if needed)
FIELD_FONTSIZE = {
    "CERT_ID": 9.5,
}

DEFAULT_FONT_SIZE = 11.5
FIELD_COLOR = black

# Debug drawing options (enabled by CLI flags)
GRID_STEP = 36    # 0.5 inch grid (72 pt = 1in)
CROSSHAIR_SIZE = 6
BOUNDING_BOX_PADDING = 2

# -------------- Helpers --------------

@dataclass
class PageGeom:
    width: float
    height: float

def detect_template_geometry(path: str) -> PageGeom:
    """Read the first page size from the template PDF."""
    reader = PdfReader(path)
    page = reader.pages[0]
    # PyPDF2 returns a RectangleObject; use .width/.height
    w = float(page.mediabox.width)
    h = float(page.mediabox.height)
    return PageGeom(width=w, height=h)

def top_left_to_rl(x_tl: float, y_tl: float, geom: PageGeom,
                   dx: float = 0, dy: float = 0) -> Tuple[float, float]:
    """
    Convert top-left origin to ReportLab's bottom-left origin.
    Apply global dx/dy nudges after conversion (dx to the right, dy downward).
    """
    x = x_tl + dx
    y = geom.height - y_tl + dy
    return x, y

def pick_font(c: canvas.Canvas) -> str:
    """Try to register and use DejaVuSans if available; else Helvetica."""
    for p in TRY_FONT_PATHS:
        if os.path.exists(p):
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            try:
                pdfmetrics.registerFont(TTFont("DejaVuSans", p))
                c.setFont("DejaVuSans", DEFAULT_FONT_SIZE)
                return "DejaVuSans"
            except Exception:
                pass
    c.setFont(FALLBACK_FONT, DEFAULT_FONT_SIZE)
    return FALLBACK_FONT

def draw_guides(c: canvas.Canvas, geom: PageGeom, grid: bool, rulers: bool):
    """Optional debug rulers and grid lines."""
    c.saveState()
    c.setStrokeColor(HexColor("#223056"))
    c.setLineWidth(0.3)

    if grid:
        # vertical
        x = 0
        while x <= geom.width:
            c.line(x, 0, x, geom.height)
            x += GRID_STEP
        # horizontal
        y = 0
        while y <= geom.height:
            c.line(0, y, geom.width, y)
            y += GRID_STEP

    if rulers:
        # Top ruler (0 at left)
        c.setFillColor(HexColor("#9fb0c8"))
        for x in range(0, int(geom.width), 72):  # inches in points
            c.drawString(x + 2, geom.height - 12, f"{x//72}\"")
        # Left ruler (0 at top)
        for i, y in enumerate(range(0, int(geom.height), 72)):
            c.drawString(2, geom.height - y - 12, f"{i}\"")
    c.restoreState()

def draw_crosshair(c: canvas.Canvas, x: float, y: float, size: float = CROSSHAIR_SIZE, color="#2d7ef7"):
    c.saveState()
    c.setStrokeColor(HexColor(color))
    c.setLineWidth(0.8)
    c.line(x - size, y, x + size, y)
    c.line(x, y - size, x, y + size)
    c.restoreState()

def draw_bounding_box(c: canvas.Canvas, x: float, y: float, text: str, font_name: str, font_size: float):
    """Very rough bounding rect (width is approximate using 0.55*fontsize per char)."""
    c.saveState()
    c.setStrokeColor(HexColor("#76b3fa"))
    c.setLineWidth(0.5)
    est_w = len(text) * font_size * 0.55
    est_h = font_size
    c.rect(x - BOUNDING_BOX_PADDING,
           y - BOUNDING_BOX_PADDING,
           est_w + (2*BOUNDING_BOX_PADDING),
           est_h + (2*BOUNDING_BOX_PADDING),
           stroke=1, fill=0)
    c.restoreState()

# -------------- Core overlay renderer --------------

def render_overlay(geom: PageGeom, template_path: str, out_path: str, data: Dict[str, str],
                   dx: float = 0, dy: float = 0, debug: bool = False,
                   grid: bool = False, crosshair: bool = False, rulers: bool = False):
    """
    Create an overlay PDF (in-memory) and merge onto the template.
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(geom.width, geom.height))

    font_name = pick_font(c)

    # Guides first (so text sits on top)
    if debug:
        draw_guides(c, geom, grid=grid, rulers=rulers)

    # Draw fields
    for key, (x_tl, y_tl) in FIELDS_TOPLEFT.items():
        value = data.get(key, "")
        if not value:
            continue
        x, y = top_left_to_rl(x_tl, y_tl, geom, dx=dx, dy=dy)
        size = FIELD_FONTSIZE.get(key, DEFAULT_FONT_SIZE)
        c.setFont(font_name, size)
        c.setFillColor(FIELD_COLOR)
        c.drawString(x, y, value)

        if debug:
            if crosshair:
                draw_crosshair(c, x, y)
            draw_bounding_box(c, x, y, value, font_name, size)

    c.showPage()
    c.save()
    buf.seek(0)

    # Merge overlay onto template
    base = PdfReader(template_path)
    over = PdfReader(buf)
    out = PdfWriter()

    page = base.pages[0]
    page.merge_page(over.pages[0])
    out.add_page(page)

    with open(out_path, "wb") as f:
        out.write(f)

# -------------- Printing --------------

def send_to_printer(pdf_path: str, printer: str = None):
    """
    Call CUPS lp to print the file.
    If printer is None, rely on system default.
    """
    cmd = ["lp", pdf_path]
    if printer:
        cmd = ["lp", "-d", printer, pdf_path]
    try:
        subprocess.check_call(cmd)
        print(f"[print] sent to printer: {printer or '(default)'}")
    except FileNotFoundError:
        print("[print] 'lp' not found. Is CUPS installed?")
    except subprocess.CalledProcessError as e:
        print(f"[print] lp failed: {e}")

# -------------- CLI --------------

def parse_args():
    p = argparse.ArgumentParser(description="TCS certificate overlay/print tool")
    p.add_argument("--template", default=DEFAULT_TEMPLATE, help="Path to base template PDF (exported from Word)")
    p.add_argument("--out", default="preview.pdf", help="Output PDF path")
    p.add_argument("--dx", type=float, default=0.0, help="Global nudge in points (X, +right)")
    p.add_argument("--dy", type=float, default=0.0, help="Global nudge in points (Y, +down)")
    p.add_argument("--debug", action="store_true", help="Enable debug aids (bounding boxes, etc.)")
    p.add_argument("--grid", action="store_true", help="Draw a faint grid (0.5in)")
    p.add_argument("--rulers", action="store_true", help="Draw inch rulers along top/left")
    p.add_argument("--crosshair", action="store_true", help="Draw crosshair at each field anchor")
    p.add_argument("--print", dest="do_print", action="store_true", help="Send the output to CUPS via 'lp'")
    p.add_argument("--printer", help="Specific printer name for CUPS (e.g., 'HP_LaserJet_M127fn')")
    p.add_argument("--fake", action="store_true", help="Use built-in fake data (default)")
    return p.parse_args()

def main():
    args = parse_args()

    if not os.path.exists(args.template):
        raise SystemExit(f"Template not found: {args.template}")

    geom = detect_template_geometry(args.template)
    print(f"[geom] template size: {geom.width:.1f} x {geom.height:.1f} pt")

    # For now we always use FAKE; later you can pass real job data here.
    data = dict(FAKE)

    print(f"[render] dx={args.dx}, dy={args.dy}, debug={args.debug}, grid={args.grid}, crosshair={args.crosshair}")
    render_overlay(
        geom=geom,
        template_path=args.template,
        out_path=args.out,
        data=data,
        dx=args.dx,
        dy=args.dy,
        debug=args.debug,
        grid=args.grid,
        crosshair=args.crosshair,
        rulers=args.rulers,
    )
    print(f"[ok] wrote {args.out}")

    if args.do_print:
        send_to_printer(args.out, printer=args.printer)

if __name__ == "__main__":
    main()