#!/usr/bin/env python3
"""
print_label.py - Minimal proof-of-concept: render a badge label and
send it to a CUPS printer queue.

This intentionally keeps "render the label" and "send to printer" as
two separate functions. That split maps directly onto the eventual
real system: ConReg will do the rendering (server-side, one place to
control layout), and the print-server agent will do the sending
(a thin call to `lp`). Keeping them separate now means later you can
lift render_label() into ConReg and send_to_printer() into the agent
with minimal rework.

Usage:
    python3 print_label.py "Jane Doe" --printer bilbo_baggins
    python3 print_label.py "Jane Doe" --printer bilbo_baggins \
        --number "M-4021" --days "Fri-Sun"

    # Render and save only, without printing (handy while testing
    # layout changes — saved to ./label_output/ by default):
    python3 print_label.py "Jane Doe" --number "M-4021" --days "Fri-Sun" --no-print

Before running:
  1. Add your printer as a CUPS queue (see setup steps).
  2. Run `lpoptions -p <printer> -l | grep -i PageSize` and copy the
     exact PageSize keyword for your label stock into PAGE_SIZE below.
  3. Measure (or look up) the physical label dimensions in inches and
     set LABEL_WIDTH_IN / LABEL_HEIGHT_IN to match.
"""

import argparse
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Where rendered labels are saved. Every render is kept (not just
# dry-run ones) so you've always got a record of what was sent to
# the printer, which is handy for debugging layout issues later.
DEFAULT_OUTPUT_DIR = Path("./label_output")

# --- Label geometry — EDIT THESE to match your printer/label stock ---
# Most Dymo LabelWriters render at 300 DPI.
DPI = 300

# Physical label size in inches — 28mm x 89mm Dymo Standard Address
# labels (S0722370 / equivalent to US 30252).
LABEL_WIDTH_IN = 28 / 25.4
LABEL_HEIGHT_IN = 89 / 25.4

# The exact CUPS PageSize keyword for this label stock, from:
#   lpoptions -p <printer> -l | grep -i PageSize
PAGE_SIZE = "w79h252"

# A bold TTF font that exists on most Debian/Raspberry Pi OS installs.
# Swap for another path if this isn't present on your system.
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

WIDTH_PX = int(LABEL_WIDTH_IN * DPI)
HEIGHT_PX = int(LABEL_HEIGHT_IN * DPI)

# Rotation applied after rendering, to convert our natural left-to-right
# text layout into the tall/narrow orientation CUPS expects for this
# media (width=across print head, height=feed direction). If the label
# comes out upside-down or mirrored, flip this to -90.
ROTATE_DEGREES = 90

# Size for the member number / days-attending detail text, in points.
DETAIL_FONT_PT = 14
DETAIL_FONT_PX = round(DETAIL_FONT_PT * DPI / 72)


def _fit_lines(draw, lines, max_width, max_height, font_path, max_start_size=400):
    """Find the largest font size at which every line in `lines` fits
    within max_width, and the whole block (all lines stacked, with
    spacing between them) fits within max_height.

    Returns (font_size, font, line_boxes, spacing).
    """
    font_size = max_start_size
    while font_size > 10:
        font = ImageFont.truetype(font_path, size=font_size)
        line_boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
        widths = [b[2] - b[0] for b in line_boxes]
        heights = [b[3] - b[1] for b in line_boxes]
        spacing = int(font_size * 0.15)
        total_height = sum(heights) + spacing * (len(lines) - 1)

        if max(widths) <= max_width and total_height <= max_height:
            return font_size, font, line_boxes, spacing

        font_size -= 2

    # Fallback: smallest size we tried, even if it doesn't fully fit.
    font = ImageFont.truetype(font_path, size=10)
    line_boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
    spacing = int(10 * 0.15)
    return 10, font, line_boxes, spacing


def _best_name_layout(draw, name, max_width, max_height, font_path):
    """Try the name as a single line, and (for multi-word names) every
    two-line split point. Return whichever combination lets the name
    print largest: (font_size, font, lines, line_boxes, spacing).
    """
    candidates = [[name]]

    words = name.split()
    for k in range(1, len(words)):
        candidates.append([" ".join(words[:k]), " ".join(words[k:])])

    best = None
    for lines in candidates:
        font_size, font, line_boxes, spacing = _fit_lines(
            draw, lines, max_width, max_height, font_path
        )
        if best is None or font_size > best[0]:
            best = (font_size, font, lines, line_boxes, spacing)

    return best


def _compose_canvas(name: str, member_number: str | None, days: str | None) -> Image.Image:
    """Lay out name + optional details on a natural, wide reading canvas.

    Everything is composed together on ONE canvas before any rotation
    happens, so relative positions (name centered, number bottom-left,
    days bottom-right) survive the later rotate() unchanged.
    """
    canvas_w, canvas_h = HEIGHT_PX, WIDTH_PX  # swapped: wide and short
    img = Image.new("L", (canvas_w, canvas_h), color=255)  # white background
    draw = ImageDraw.Draw(img)

    side_margin = int(canvas_w * 0.05)
    bottom_margin = int(canvas_h * 0.08)

    detail_font = ImageFont.truetype(FONT_PATH, size=DETAIL_FONT_PX)
    # Reserve room at the bottom for the detail row, even if this
    # particular label doesn't use it, so layout stays consistent.
    ref_bbox = draw.textbbox((0, 0), "Ag", font=detail_font)
    detail_row_height = ref_bbox[3] - ref_bbox[1]
    reserved_bottom = detail_row_height + bottom_margin

    # --- Name: try one line and every two-line split point; use
    # whichever prints largest without overlapping the reserved row ---
    name_area_height = canvas_h - reserved_bottom
    max_text_width = canvas_w - 2 * side_margin

    font_size, font, lines, line_boxes, spacing = _best_name_layout(
        draw, name, max_text_width, name_area_height, FONT_PATH
    )

    heights = [b[3] - b[1] for b in line_boxes]
    total_block_height = sum(heights) + spacing * (len(lines) - 1)
    y = (name_area_height - total_block_height) / 2

    for line, bbox, h in zip(lines, line_boxes, heights):
        w = bbox[2] - bbox[0]
        x = (canvas_w - w) / 2 - bbox[0]
        draw.text((x, y - bbox[1]), line, fill=0, font=font)
        y += h + spacing

    # --- Bottom-left: member number ---
    if member_number:
        bbox = draw.textbbox((0, 0), member_number, font=detail_font)
        y = canvas_h - bottom_margin - (bbox[3] - bbox[1]) - bbox[1]
        draw.text((side_margin, y), member_number, fill=0, font=detail_font)

    # --- Bottom-right: days attending ---
    if days:
        bbox = draw.textbbox((0, 0), days, font=detail_font)
        text_w = bbox[2] - bbox[0]
        x = canvas_w - side_margin - text_w - bbox[0]
        y = canvas_h - bottom_margin - (bbox[3] - bbox[1]) - bbox[1]
        draw.text((x, y), days, fill=0, font=detail_font)

    return img


def render_label(
    name: str,
    out_path: Path,
    member_number: str | None = None,
    days: str | None = None,
) -> None:
    """Compose label content, then rotate to match the media orientation."""
    img = _compose_canvas(name, member_number, days)
    rotated = img.rotate(ROTATE_DEGREES, expand=True)
    assert rotated.size == (WIDTH_PX, HEIGHT_PX), (
        f"Rotated size {rotated.size} doesn't match expected media "
        f"size {(WIDTH_PX, HEIGHT_PX)} — check LABEL_WIDTH_IN/HEIGHT_IN."
    )
    rotated.save(out_path)


def _label_filename(name: str) -> str:
    """A unique, human-readable filename for a rendered label."""
    slug = "".join(c if c.isalnum() else "_" for c in name).strip("_")[:40]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"label_{timestamp}_{slug}.png"


def send_to_printer(image_path: Path, printer: str) -> None:
    """Hand a pre-rendered, correctly-sized image to CUPS."""
    cmd = [
        "lp",
        "-d", printer,
        "-o", f"PageSize={PAGE_SIZE}",
        "-o", "fit-to-page",
        str(image_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Print failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    print(result.stdout.strip() or "Sent to printer.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Print a basic badge label via CUPS.")
    parser.add_argument("name", help="Text to print on the label")
    parser.add_argument("--printer", help="CUPS queue name, e.g. bilbo_baggins")
    parser.add_argument("--number", help="Member number, printed bottom-left", default=None)
    parser.add_argument("--days", help="Days attending, printed bottom-right", default=None)
    parser.add_argument(
        "--no-print",
        action="store_true",
        help="Render and save the label image, but don't send it to a printer.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Where to save rendered labels (default: {DEFAULT_OUTPUT_DIR})",
    )
    args = parser.parse_args()

    if not args.no_print and not args.printer:
        parser.error("--printer is required unless --no-print is set")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    label_path = args.output_dir / _label_filename(args.name)

    render_label(args.name, label_path, member_number=args.number, days=args.days)
    print(f"Rendered {WIDTH_PX}x{HEIGHT_PX}px label to {label_path}")

    if args.no_print:
        print("--no-print set: skipping printer.")
    else:
        send_to_printer(label_path, args.printer)


if __name__ == "__main__":
    main()
