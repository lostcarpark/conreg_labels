#!/usr/bin/env python3
"""
conreg_print_agent.py - Poll ConReg for pending badge label print jobs
and hand each one to print_label.py to render/print.

This script only knows how to talk to ConReg's print job API (see
PRINT_JOB_API.md) and how to turn a job into a call to print_label.py.
All rendering/printing logic stays in print_label.py, untouched and
independently testable:

    python3 print_label.py "Test Name" --no-print

still works exactly as before, with no dependency on ConReg being
reachable at all - handy for isolating "can this Pi print" from "can
this Pi reach ConReg".

Usage:
    python3 conreg_print_agent.py --conreg-url https://example.org \
        --eid 1 --printer bilbo_baggins

    # Poll once and exit, instead of looping forever (handy for testing):
    python3 conreg_print_agent.py --conreg-url https://example.org \
        --eid 1 --printer bilbo_baggins --once

    # Dry run: fetch a real job from ConReg but don't actually print it
    # (passed through to print_label.py's own --no-print):
    python3 conreg_print_agent.py --conreg-url https://example.org \
        --eid 1 --no-print --once

Requires the `requests` package (pip install requests).
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

import requests

PRINT_LABEL_SCRIPT = Path(__file__).parent / "print_label.py"


def fetch_job(conreg_url: str, eid: int) -> dict | None:
    """GET the next print job for this event. Returns None if there's
    nothing to print (204 No Content)."""
    resp = requests.get(f"{conreg_url}/api/print-jobs/{eid}/next", timeout=10)
    if resp.status_code == 204:
        return None
    resp.raise_for_status()
    return resp.json()


def post_result(conreg_url: str, eid: int, job_id: str, status: str, message: str) -> None:
    """POST the outcome of a print attempt back to ConReg."""
    resp = requests.post(
        f"{conreg_url}/api/print-jobs/{eid}/{job_id}/result",
        json={"status": status, "message": message},
        timeout=10,
    )
    resp.raise_for_status()


def print_job(job: dict, printer: str | None, no_print: bool, output_dir: Path) -> tuple[str, str]:
    """Call print_label.py to render/print `job`. Returns (status, message)
    where status is "success" or "error", matching the API contract."""
    cmd = [sys.executable, str(PRINT_LABEL_SCRIPT), job["member_name"]]
    if job.get("member_number"):
        cmd += ["--number", job["member_number"]]
    if job.get("days_attending"):
        cmd += ["--days", job["days_attending"]]
    if no_print:
        cmd += ["--no-print"]
    else:
        cmd += ["--printer", printer]
    cmd += ["--output-dir", str(output_dir)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()
    return ("success", output) if result.returncode == 0 else ("error", output)


def run_once(conreg_url: str, eid: int, printer: str | None, no_print: bool, output_dir: Path) -> bool:
    """Poll for and handle a single job. Returns True if a job was found."""
    job = fetch_job(conreg_url, eid)
    if job is None:
        return False

    print(f"Got job {job['job_id']}: {job['member_name']}")
    status, message = print_job(job, printer, no_print, output_dir)
    print(f"  -> {status}: {message or '(no output)'}")
    post_result(conreg_url, eid, job["job_id"], status, message)
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
    parser.add_argument("--printer", help="CUPS queue name, e.g. bilbo_baggins")
    parser.add_argument(
        "--no-print",
        action="store_true",
        help="Render each fetched job but don't send it to a printer (passed through to print_label.py).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("./label_output"),
        help="Where print_label.py should save rendered labels (default: ./label_output)",
    )
    parser.add_argument("--interval", type=float, default=5.0, help="Seconds between polls (default: 5)")
    parser.add_argument(
        "--once", action="store_true",
        help="Poll a single time and exit, instead of looping forever",
    )
    args = parser.parse_args()

    if not args.no_print and not args.printer:
        parser.error("--printer is required unless --no-print is set")

    conreg_url = args.conreg_url.rstrip("/")

    if args.once:
        run_once(conreg_url, args.eid, args.printer, args.no_print, args.output_dir)
        return

    print(f"Polling {conreg_url} for event {args.eid} every {args.interval}s. Ctrl+C to stop.")
    while True:
        try:
            found = run_once(conreg_url, args.eid, args.printer, args.no_print, args.output_dir)
        except requests.RequestException as exc:
            print(f"Poll failed: {exc}", file=sys.stderr)
            found = False
        if not found:
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
