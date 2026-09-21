#!/usr/bin/env python3
"""
conreg_print_agent.py - Poll ConReg for pending badge label print jobs
and hand each one's pre-rendered image to print_image.py's rotate/print
step.

Rendering (fonts, auto-fit name splitting, field positions) lives in
ConReg now, not here - each job ConReg hands back already carries a
rendered (but unrotated) PNG. This script's job is just: decode it,
save a local copy, rotate it to match the label stock's feed
orientation, and send it to CUPS with the right copies count.

Usage:
    python3 conreg_print_agent.py --conreg-url https://example.org \
        --eid 1 --printer bilbo_baggins --api-key <key>

    # Or set the key via environment variable instead of the command
    # line, so it doesn't end up in shell history / `ps` output:
    export CONREG_PRINT_API_KEY=<key>
    python3 conreg_print_agent.py --conreg-url https://example.org \
        --eid 1 --printer bilbo_baggins

    # Poll once and exit, instead of looping forever (handy for testing):
    python3 conreg_print_agent.py --conreg-url https://example.org \
        --eid 1 --printer bilbo_baggins --api-key <key> --once

    # Dry run: fetch a real job from ConReg but don't actually print it
    # (still decodes, saves, and rotates the image - just skips `lp`):
    python3 conreg_print_agent.py --conreg-url https://example.org \
        --eid 1 --printer bilbo_baggins --api-key <key> --no-print --once

Requires the `requests` and `Pillow` packages.
"""

import argparse
import base64
import os
import sys
import time
from pathlib import Path

import requests

from print_image import DEFAULT_OUTPUT_DIR, DEFAULT_ROTATE_DEGREES, label_filename, rotate_and_print


def fetch_job(conreg_url: str, eid: int, printer: str, api_key: str) -> dict | None:
    """GET the next print job for this event/printer. Returns None if
    there's nothing to print (204 No Content)."""
    resp = requests.get(
        f"{conreg_url}/api/print-jobs/{eid}/next",
        params={"printer": printer},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )
    if resp.status_code == 204:
        return None
    resp.raise_for_status()
    return resp.json()


def fetch_settings(conreg_url: str, eid: int, api_key: str) -> dict:
    """GET the event's print-time settings (page size, rotation,
    copies, suppress-printing). Fetched once at startup - see main().
    Label size/field positions/name lines are Drupal-internal rendering
    concerns now and never appear here."""
    resp = requests.get(
        f"{conreg_url}/api/print-jobs/{eid}/settings",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_settings_with_retry(conreg_url: str, eid: int, api_key: str, interval: float) -> dict:
    """Fetches print-time settings at startup, retrying rather than
    crashing on a transient failure (a momentary network blip, ConReg
    briefly unreachable) - the same resiliency the main polling loop
    below already has for exactly this class of error, extended to
    cover the one request that happens before that loop even starts.
    An unattended print-server device should survive a network hiccup
    at boot the same way it survives one mid-poll."""
    while True:
        try:
            return fetch_settings(conreg_url, eid, api_key)
        except requests.RequestException as exc:
            print(f"Could not fetch settings ({exc}); retrying in {interval}s...", file=sys.stderr)
            time.sleep(interval)


def post_result(conreg_url: str, eid: int, job_id: str, status: str, message: str, api_key: str) -> None:
    """POST the outcome of a print attempt back to ConReg."""
    resp = requests.post(
        f"{conreg_url}/api/print-jobs/{eid}/{job_id}/result",
        json={"status": status, "message": message},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10,
    )
    resp.raise_for_status()


def print_job(job: dict, printer: str, no_print: bool, output_dir: Path, settings: dict) -> tuple[str, str]:
    """Decode `job`'s rendered image and hand it to print_image.py's
    rotate/print step. Returns (status, message) where status is
    "success" or "error", matching the API contract."""
    if "page_size" not in settings:
        # No sane default exists for this one (unlike rotate_degrees/
        # copies below) - failing clearly here beats a bare, cryptic
        # KeyError string.
        return ("error", "Settings response is missing \"page_size\" - can't determine the label's physical size.")

    try:
        image_bytes = base64.b64decode(job["image"])
        out_path = output_dir / label_filename(job.get("member_name") or "label")
        suppress = no_print or settings.get("suppress_printing")
        rotate_and_print(
            image_bytes,
            out_path,
            settings["page_size"],
            # 0 is a legitimate, meaningful value here ("this stock
            # needs no rotation") - only fall back when the key is
            # genuinely absent, unlike copies below where 0 is never
            # actually wanted.
            settings.get("rotate_degrees", DEFAULT_ROTATE_DEGREES),
            copies=settings.get("copies") or 1,
            printer=None if suppress else printer,
        )
        return ("success", f"Saved to {out_path}" + ("" if suppress else "; sent to printer."))
    except Exception as exc:  # noqa: BLE001 - reported back to ConReg, not re-raised
        return ("error", str(exc))


def run_once(conreg_url: str, eid: int, printer: str, no_print: bool, output_dir: Path, api_key: str, settings: dict) -> bool:
    """Poll for and handle a single job. Returns True if a job was found."""
    job = fetch_job(conreg_url, eid, printer, api_key)
    if job is None:
        return False

    job_id = job.get("job_id")
    if job_id is None:
        # Can't post a result back with no ID to attach it to - log and
        # move on rather than raising a KeyError the main loop's
        # `except requests.RequestException` wouldn't catch anyway.
        print("Poll returned a job with no job_id - skipping.", file=sys.stderr)
        return True

    started = time.monotonic()
    print(f"[{time.strftime('%H:%M:%S')}] Got job {job_id}: {job.get('member_name') or '(unknown)'}")
    status, message = print_job(job, printer, no_print, output_dir, settings)
    elapsed = time.monotonic() - started
    print(f"  -> {status} in {elapsed:.2f}s: {message or '(no output)'}")
    post_result(conreg_url, eid, job_id, status, message, api_key)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Poll ConReg for badge label print jobs and print them."
    )
    parser.add_argument(
        "--conreg-url", required=True,
        help="Base URL of the ConReg site, e.g. https://example.org",
    )
    parser.add_argument("--eid", type=int, required=True, help="Event ID to poll jobs for")
    parser.add_argument(
        "--printer", required=True,
        help="Printer machine name, e.g. \"bilbo_baggins\" - must match the "
             "printer's machine_name in ConReg (distinct from its display "
             "name, e.g. \"Bilbo Baggins\", shown to staff there). Identifies "
             "which job queue to poll on ConReg, and (unless --no-print) "
             "doubles as the CUPS queue name.",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("CONREG_PRINT_API_KEY"),
        help="Shared API key for the print job endpoints, sent as "
             "'Authorization: Bearer <key>'. Can also be set via the "
             "CONREG_PRINT_API_KEY environment variable, which avoids the "
             "secret showing up in shell history or `ps` output.",
    )
    parser.add_argument(
        "--no-print",
        action="store_true",
        help="Decode and save each fetched job's image, but don't send it to a printer.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Where to save each job's (rotated) label image (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument("--interval", type=float, default=5.0, help="Seconds between polls (default: 5)")
    parser.add_argument(
        "--once", action="store_true",
        help="Poll a single time and exit, instead of looping forever",
    )
    args = parser.parse_args()

    if not args.api_key:
        parser.error("--api-key (or CONREG_PRINT_API_KEY) is required")

    conreg_url = args.conreg_url.rstrip("/")

    # Fetched once at startup, not on every poll - these settings change
    # rarely, and this saves an extra request per job. Pick up a config
    # change by restarting the agent.
    settings = fetch_settings_with_retry(conreg_url, args.eid, args.api_key, args.interval)

    if args.once:
        run_once(conreg_url, args.eid, args.printer, args.no_print, args.output_dir, args.api_key, settings)
        return

    print(f"Polling {conreg_url} for event {args.eid} every {args.interval}s. Ctrl+C to stop.")
    while True:
        try:
            found = run_once(conreg_url, args.eid, args.printer, args.no_print, args.output_dir, args.api_key, settings)
        except requests.RequestException as exc:
            print(f"Poll failed: {exc}", file=sys.stderr)
            found = False
        if not found:
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
