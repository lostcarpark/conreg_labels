#!/usr/bin/env python3
"""
print_image.py - Rotate a pre-rendered label image (if needed) and
send it to a CUPS printer queue.

Label *rendering* (fonts, auto-fit name splitting, field positions)
now lives in ConReg itself (see the `LabelRenderer` service), so both
a real print job and an on-demand preview go through the exact same
renderer with no dependency on this agent being online. This script's
job is what's left over: rotating the received (unrotated) image to
match how the physical media feeds through the printer, and handing it
to CUPS with the right page size / copies. It used to also render the
label from raw field values (name/number/days/badge type); that whole
pipeline (_compose_canvas, _best_name_layout, _fit_lines, the 3x3
field-position grid) has been removed - see git history if you need it.

Usage (manual "can this Pi print" testing, independent of ConReg):
    python3 print_image.py some_test.png --printer bilbo_baggins \
        --page-size w79h252 --rotate 90 --copies 2

    # Rotate and save only, without printing:
    python3 print_image.py some_test.png --no-print --rotate 90

Before running:
  1. Add your printer as a CUPS queue (see setup steps in README.md).
  2. Run `lpoptions -p <printer> -l | grep -i PageSize` and copy the
     exact PageSize keyword for your label stock (or configure it as
     a Label Size in ConReg's Label Printing Settings page).
"""

import argparse
import io
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image

DEFAULT_OUTPUT_DIR = Path("./label_output")

# Matches print_label.py's original hardcoded value for this label
# stock - kept as the CLI/agent default so an agent that never receives
# an explicit rotate_degrees still rotates the way this stock actually
# needs, rather than silently printing sideways.
DEFAULT_ROTATE_DEGREES = 90

# The DPI every label size is rendered/expected at (see ConReg's
# LabelRenderer::DPI) - used only for the dimension sanity check below,
# not for any actual image processing here.
DPI = 300


def label_filename(hint: str = "label") -> str:
    """A unique, human-readable filename for a saved label image."""
    slug = "".join(c if c.isalnum() else "_" for c in hint).strip("_")[:40] or "label"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"label_{timestamp}_{slug}.png"


def send_to_printer(image_path: Path, printer: str | None, page_size: str, copies: int = 1) -> None:
    """Hand a pre-rendered, correctly-sized image to CUPS, once per copy.

    Deliberately not using `-o copies=N` for a single `lp` call: the Dymo
    CUPS driver (`printer-driver-dymo`, see README.md) declares
    `cupsManualCopies: True` in its PPD, meaning CUPS expects the
    driver/filter to loop internally for extra copies rather than doing it
    at the scheduler level - but this driver's filter doesn't actually
    implement that loop, so the `copies` job option is silently accepted
    and has no effect. Submitting one single-copy job per copy sidesteps
    the driver entirely and works regardless of whether it ever fixes
    this. See #3596651.

    `printer` being None means a dry run (`--no-print`, or
    `settings.suppress_printing`) - the copy loop and its console
    messages still run exactly as they would for a real print, so a dry
    run exercises the same "N copies" logic a real one does; only the
    actual `lp` call is skipped, kept to this one `if`.
    """
    copies = max(copies, 1)
    for copy_num in range(1, copies + 1):
        print(f"  Printing copy {copy_num}/{copies}...")
        if printer is None:
            continue
        cmd = [
            "lp",
            "-d", printer,
            "-o", f"PageSize={page_size}",
            "-o", "fit-to-page",
            str(image_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Print failed on copy {copy_num}/{copies}: {result.stderr.strip()}")


def _expected_pixel_size(page_size: str) -> tuple[int, int] | None:
    """Parses a CUPS PageSize keyword like "w79h252" (in points) into
    the pixel dimensions a correctly-rendered, correctly-rotated label
    should have at DPI. Returns None (rather than raising) for a
    keyword this simple pattern doesn't match - e.g. a named size like
    "Letter" - since this is only a best-effort sanity check, not an
    authoritative source of the label's real dimensions."""
    match = re.fullmatch(r"w(\d+)h(\d+)", page_size)
    if not match:
        return None
    width_pt, height_pt = (int(group) for group in match.groups())
    return (round(width_pt / 72 * DPI), round(height_pt / 72 * DPI))


def rotate_and_print(
    image_bytes: bytes,
    out_path: Path,
    page_size: str,
    rotate_degrees: int,
    copies: int = 1,
    printer: str | None = None,
) -> None:
    """Decode `image_bytes` (a PNG, as rendered unrotated by ConReg),
    rotate it to match the label stock's feed orientation, save a copy
    to `out_path`, and send it to CUPS.

    `printer` being None means "don't actually print" (a dry run /
    suppressed run) - passed straight through to `send_to_printer()`,
    which still runs its per-copy loop and console messages, just
    without the `lp` call itself. Everything up to and including that
    loop runs the same whether this is a dry run or a real print.
    """
    img = Image.open(io.BytesIO(image_bytes))
    if rotate_degrees:
        img = img.rotate(rotate_degrees, expand=True)

    expected_size = _expected_pixel_size(page_size)
    if expected_size is not None:
        # A couple of pixels of slop is expected and not a real
        # mismatch: ConReg derives its render size from mm directly
        # (truncated), while this check derives it from the CUPS
        # PageSize keyword's points (rounded) - two independently-
        # rounded conversions of the same physical size, verified
        # empirically to disagree by up to ~2px on ordinary labels. A
        # genuine mismatch (wrong label size, unit-conversion bug)
        # would be far larger than that.
        tolerance = 2
        if any(abs(a - b) > tolerance for a, b in zip(img.size, expected_size)):
            print(
                f"Warning: rotated image is {img.size[0]}x{img.size[1]}px, "
                f"expected {expected_size[0]}x{expected_size[1]}px for page "
                f"size \"{page_size}\" at {DPI} DPI - the label may print the "
                "wrong size (ConReg's rendered image and this agent's "
                "settings may be out of sync).",
                file=sys.stderr,
            )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)

    send_to_printer(out_path, printer, page_size, copies=copies)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rotate a pre-rendered label image and print it via CUPS."
    )
    parser.add_argument("image", type=Path, help="Path to a pre-rendered (unrotated) label PNG")
    parser.add_argument("--printer", help="CUPS queue name, e.g. bilbo_baggins")
    parser.add_argument(
        "--page-size", default="w79h252",
        help="CUPS PageSize keyword for the label stock (default: w79h252, 28x89mm)",
    )
    parser.add_argument(
        "--rotate", type=int, default=DEFAULT_ROTATE_DEGREES,
        help=f"Degrees to rotate before printing (default: {DEFAULT_ROTATE_DEGREES}; use 0 for no rotation)",
    )
    parser.add_argument("--copies", type=int, default=1, help="Number of copies to print (default: 1)")
    parser.add_argument(
        "--no-print", action="store_true",
        help="Rotate and save the label image, but don't send it to a printer.",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
        help=f"Where to save the (rotated) label image (default: {DEFAULT_OUTPUT_DIR})",
    )
    args = parser.parse_args()

    if not args.no_print and not args.printer:
        parser.error("--printer is required unless --no-print is set")

    image_bytes = args.image.read_bytes()
    out_path = args.output_dir / label_filename(args.image.stem)

    try:
        rotate_and_print(
            image_bytes,
            out_path,
            args.page_size,
            args.rotate,
            copies=args.copies,
            printer=None if args.no_print else args.printer,
        )
    except Exception as exc:  # noqa: BLE001 - reported cleanly, not as a raw traceback
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(f"Saved {'(rotated) ' if args.rotate else ''}label to {out_path}")
    if args.no_print:
        print("--no-print set: skipping printer.")
    else:
        print("Sent to printer.")


if __name__ == "__main__":
    main()
