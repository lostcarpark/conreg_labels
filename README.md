# ConReg Labels

Print-server agent for [ConReg](https://git.drupalcode.org/project/conreg)'s
badge label printing feature — see its
[label printing docs](https://git.drupalcode.org/project/conreg/-/blob/1.x/docs/site-building/label-printing.md)
for the Drupal-side half of this feature (settings, entities, the
member check-in UI). This repo is deliberately separate from the main
ConReg Drupal module — different language/tooling, different release
cadence, no Drupal packaging conventions to clash with. The only
shared contract between the two repos is the JSON job/result payload
described in [`PRINT_JOB_API.md`](PRINT_JOB_API.md).

Label *rendering* (fonts, auto-fit name splitting, field positions,
label size) lives in ConReg itself now, not here — a `next` job already
carries a fully rendered (but unrotated) PNG. This repo only handles
what's left: rotating that image to match how the physical label stock
feeds, and sending it to CUPS.

Two scripts:

- **`print_image.py`** — given an already-rendered PNG, optionally
  rotates it and sends it to a CUPS printer. Knows nothing about
  ConReg or HTTP; fully usable standalone (`python3 print_image.py
  some.png --printer bilbo_baggins`) for testing "can this Pi print"
  independent of ConReg being reachable.
- **`conreg_print_agent.py`** — polls ConReg for pending print jobs,
  fetches print-time settings (page size, rotation, copies,
  suppress-printing) once at startup, and hands each job's decoded
  image to `print_image.py`'s `rotate_and_print()` directly (an import,
  not a subprocess — there's no heavy rendering step left to isolate).
  This is what actually runs continuously on a print-server device.

## Requirements

- Python 3.10+
- `pip install requests pillow` (Pillow is used only to rotate the
  already-rendered image this agent receives - rendering itself,
  including all font handling, lives in ConReg now, not here)
- CUPS, with the `printer-driver-dymo` package installed (Debian/
  Raspberry Pi OS: `sudo apt install cups printer-driver-dymo`)

## Setting up a printer

Each physical printer gets a "fun name" (e.g. "Bilbo Baggins",
stickered on the unit) which doubles as its CUPS queue name, so logs,
commands, and ConReg's UI all use the same vocabulary.

### 1. Install CUPS and the Dymo driver

On Debian/Raspberry Pi OS:

```
sudo apt install cups cups-client printer-driver-dymo
sudo systemctl enable --now cups
```

`printer-driver-dymo` (the `dymo-cups-drivers` package) supplies the
PPDs for the LabelWriter range - this is what lets CUPS talk to the
printer at all, and what step 2 below picks from. `cups-client`
provides the `lp`/`lpstat`/`lpoptions`/`lpadmin` commands used
throughout this doc; on most distros it's pulled in automatically by
`cups`, but installing it explicitly doesn't hurt.

The CUPS web interface (`https://localhost:631`, or
`https://<device-ip>:631` from another machine on the same network)
is the easiest way to do the rest of this section on a device with a
desktop/browser. For a headless print-server device (the normal case
for a Raspberry Pi), either SSH in with a local browser tunnelled via
`ssh -L 631:localhost:631 pi@<device-ip>` and use the web interface
that way, or use the `lpadmin`/`lpinfo` CLI commands given as an
alternative at each step below - both end up doing the same thing.

To manage printers from the CUPS web interface (add/remove queues) as
a non-root user, add that user to the `lpadmin` group and log back in:

```
sudo usermod -aG lpadmin $USER
```

### 2. Connect the printer and add it as a CUPS queue

Plug in the Dymo LabelWriter via USB and confirm Linux sees it:

```
lsusb | grep -i dymo
```

Then add it as a CUPS queue, named after its fun name (lowercased/
underscored is fine for the actual queue, e.g. `bilbo_baggins` — see
`conreg_print_agent.py --printer`, which is matched against ConReg's
`machine_name` for the printer, not the display name shown to staff):

- **Via the web interface**: Administration → Add Printer → select the
  detected DYMO USB device → choose the matching "DYMO LabelWriter ..."
  model from the driver list `printer-driver-dymo` installed → name the
  queue (e.g. `bilbo_baggins`) → Add Printer. Leave the default options
  as-is for now; label size is handled per-job by the agent (`--page-size`),
  not as a fixed CUPS default.
- **Via the CLI**, useful for a headless device with no browser at all:
  ```
  lpinfo -v | grep -i dymo          # find the exact device URI
  lpinfo -m | grep -i dymo          # find the exact driver/PPD name
  sudo lpadmin -p bilbo_baggins -E -v <device-uri> -m <driver-name>
  ```
  `-E` both enables the queue and lets it accept jobs; if a queue ever
  ends up disabled or rejecting jobs (e.g. after the printer was briefly
  unplugged), bring it back with:
  ```
  sudo cupsenable bilbo_baggins
  sudo cupsaccept bilbo_baggins
  ```

Confirm the queue exists and is accepting jobs:

```
lpstat -p bilbo_baggins
```

### 3. Confirm the label stock's PageSize keyword

```
lpoptions -p <printer> -l | grep -i PageSize
```

Define the label stock as a Label Size on ConReg's Label Printing
Settings page (width/height in mm) — ConReg computes the matching
`PageSize` keyword itself and serves it to the agent via the
`settings` endpoint; there's no constant to hand-edit in this repo
anymore.

### 4. Test printing

Test rotation/printing without needing ConReg reachable at all —
point it at any PNG you have lying around:

```
python3 print_image.py some_test.png --no-print --rotate 90
```

Check the saved image in `./label_output/` before sending anything
to the physical printer. Then test an actual print:

```
python3 print_image.py some_test.png --printer bilbo_baggins --rotate 90 --page-size w79h252
```

### 5. Register the printer in ConReg

In ConReg, create a `conreg_printer` entity for this event with a
matching `machine_name` via the Printers tab on the Label Printing
Settings page.

## Running the agent

```
python3 conreg_print_agent.py --conreg-url https://example.org \
    --eid 1 --printer bilbo_baggins --api-key <key>
```

Useful flags:

- `--once` — poll a single time and exit, instead of looping forever
  (handy for testing).
- `--no-print` — decode and save each fetched job's image but don't
  send it to a printer.
- `--interval` — seconds between polls when looping (default 5).

Run `python3 conreg_print_agent.py --help` for the full list.

### API key

ConReg refuses every print-job API request for an event that has no
key configured, so the agent needs one to do anything. Provide it via
`--api-key`, or (recommended, so it doesn't end up in shell history or
`ps` output) via the `CONREG_PRINT_API_KEY` environment variable:

```
export CONREG_PRINT_API_KEY=<key>
python3 conreg_print_agent.py --conreg-url https://example.org \
    --eid 1 --printer bilbo_baggins
```

**Generating and storing the key** (on the ConReg side):

1. Generate a strong random value: `openssl rand -hex 32`.
2. Store it as a Drupal Key entity at
   `/admin/config/system/keys/add`, key type **Authentication**,
   provider **Environment Variable** (avoids the secret sitting in
   exportable Drupal config) — set that environment variable on
   whatever runs the ConReg site (for DDEV, `web_environment` in
   `.ddev/config.yaml` or a local override).
3. In ConReg: Event Configuration → Check In Page → "Print Job API
   Key" → select the key you just created → save.
4. Give the print-server agent the same value, as above.

See [`PRINT_JOB_API.md`](PRINT_JOB_API.md) for the full authentication
and endpoint contract.

### Print-time settings

`conreg_print_agent.py` fetches ConReg's print-time settings (page
size, rotation, copies, suppress-printing) once when it starts, from
the `settings` endpoint documented in
[`PRINT_JOB_API.md`](PRINT_JOB_API.md) — it is **not** re-fetched while
the agent keeps running. Restart the agent process after changing
anything on ConReg's Label Printing Settings page for the change to
take effect. Label *content* settings (size in mm, field positions,
name line count) never reach the agent at all anymore — they're purely
a Drupal-side rendering concern.

If this startup fetch fails (ConReg briefly unreachable, a wrong/not-
yet-configured `--api-key`), the agent doesn't give up — it logs
`Could not fetch settings (...); retrying in <interval>s...` and keeps
retrying indefinitely, the same way the main polling loop already
tolerates a transient failure mid-run. On an unattended device this
means a network hiccup right at boot no longer requires a manual
restart; on a genuinely wrong API key it means the agent will sit
retrying forever rather than exiting, so check the logs if it never
gets past this message.

You may also see an occasional `Warning: rotated image is ...px,
expected ...px for page size "..."` on stderr. This is a sanity check
comparing the decoded/rotated image against the size ConReg's
`page_size` implies — a pixel or two is normal (ConReg and this check
derive that number via two independently-rounded unit conversions) and
not worth investigating, but a large discrepancy means ConReg's
rendered image and this agent's settings have drifted out of sync
(e.g. mid-deploy) and the label may print the wrong size.

## Deploying to a print-server device

Print-server devices (Raspberry Pi or similar; an old laptop works
identically and has a battery-backup advantage) run `CUPS` plus
`conreg_print_agent.py` in a loop, with no inbound firewall rules
needed — the agent only makes outbound HTTPS requests to ConReg. Venue
Wi-Fi/captive-portal setup is a per-venue logistics decision, not yet
standardized here.

## Standalone Label Generator

The current branch prints is designed for a PHP label generator, and 
just fetches an image and spools it to the printer.

For an earlier approach that rendered the labels within the Python
script, please see the `StandaloneLabels` branch.

