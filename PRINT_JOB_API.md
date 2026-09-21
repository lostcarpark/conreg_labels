# Print job API contract

> **Status: rendering moved into ConReg.** Jobs and printers are
> `conreg_print_job`/`conreg_printer` content entities in ConReg, with
> atomic job claiming and per-event API key authentication. Label
> *rendering* (fonts, auto-fit name splitting, field positions, label
> size) now happens entirely in ConReg's `LabelRenderer` service, at
> job-creation time — the `next` endpoint hands the agent an
> already-rendered (but unrotated) image, not raw field values. This
> also means ConReg can generate an instant preview of a label without
> any agent being online at all (see the Label Printing Settings page).

This is the HTTP contract between ConReg (Drupal) and the print-server
agent (a separate process, possibly on separate hardware, that drives a
Dymo LabelWriter via CUPS — see `print_image.py` in this directory,
which does the printing/rotation half of what used to be
`print_label.py`'s job).

## Authentication

Both endpoints require an `Authorization: Bearer <key>` header, where
`<key>` is the shared secret configured for the event on the Check In
Page section of Event Configuration (`checkin.print_api_key`, stored
via the Key module). There is no per-device credential yet — every
agent polling for a given event uses the same key.

If no key is configured for an event, every request to that event's
endpoints is refused with `401` — there is no unauthenticated fallback.

`conreg_print_agent.py` sends this automatically from its `--api-key`
argument (or the `CONREG_PRINT_API_KEY` environment variable).

## Not yet implemented

- Per-device tokens — each print-server device having its own key,
  rather than one shared per-event key, would narrow the blast radius
  of a leaked credential. Deferred, see ConReg follow-up issues.
- Live-reload of print-time settings. `conreg_print_agent.py` fetches
  `settings` once at startup (see below), not on every poll — restart
  the agent process to pick up a change made on the Label Printing
  Settings page (e.g. a different label size or copies count).

## `GET /api/print-jobs/{eid}/settings`

Polled once by an agent at startup (not on every job poll) to fetch the
print-time parameters it needs: the CUPS page size, how far to rotate
the received image before printing, how many copies, and whether
printing is currently suppressed. Everything about *content* layout
(label size in mm, field positions, name line count) is a Drupal-side
rendering concern now and never appears here — the agent doesn't render
anything, so it doesn't need to know about it. `{eid}` gates access via
the same per-event key as the other endpoints, even though the settings
themselves are global (not per-event) — this avoids a second auth
mechanism for a single shared-secret model that already exists.

**`200 OK`:**

```json
{
  "page_size": "w102h252",
  "rotate_degrees": 90,
  "copies": 2,
  "suppress_printing": false
}
```

- `page_size` is the CUPS `PageSize` keyword, pre-computed by ConReg
  from the configured label size's `width_mm`/`height_mm`
  (`pt = round(mm / 25.4 * 72)`, formatted `w{pt}h{pt}`).
- `rotate_degrees`: degrees to rotate the received (unrotated) image
  before printing, to match how the physical label stock feeds through
  the printer. `0` means no rotation.
- `suppress_printing`: when `true`, the agent always decodes, rotates,
  and saves the label locally but never sends it to CUPS, regardless of
  its own `--no-print` flag — mainly for testing changes without
  burning labels.

**Errors:** `401` — missing, invalid, or unconfigured `Authorization`
key, same rules as the other endpoints.

`conreg_print_agent.py` fetches this once when it starts (and once in
`--once` mode) and reuses it for every job for the life of the process.
A failure here (network unreachable, `401`) doesn't crash the agent —
`fetch_settings_with_retry()` logs and retries indefinitely at
`--interval`, the same tolerance the main polling loop already has for
this class of error, so a transient issue at boot doesn't need a manual
restart.

## `GET /api/print-jobs/{eid}/next?printer=<name>`

Polled by an agent to ask "is there a job for me?". `{eid}` is the
ConReg event ID, and the `printer` query parameter is the printer's
name (e.g. `Bilbo Baggins`, URL-encoded) — both are required, since a
job is targeted at one specific printer. `conreg_print_agent.py` sends
both automatically from its `--eid`/`--printer` arguments.

ConReg atomically claims the oldest pending job for that event/printer
pair before returning it, so two agents polling on behalf of the same
printer can't both receive the same job. The returned image was
rendered by ConReg once, at the moment the job was created (when the
member checked in) — it's a snapshot, the same way `member_name`/
`member_number`/`days_attending` already are, so it stays correct even
if label settings change before the job is actually printed.

**Job available — `200 OK`:**

```json
{
  "job_id": "1",
  "member_name": "Jane Doe",
  "member_number": "4021",
  "days_attending": "Friday, Saturday, Sunday",
  "badge_type": "Attending",
  "image": "iVBORw0KGgoAAAANSUhEUgAA..."
}
```

- `image` is a base64-encoded PNG of the fully rendered label, in its
  natural "reading" orientation (**not yet rotated** — rotation is a
  print-time step the agent applies using `settings.rotate_degrees`,
  since it's about how the physical media feeds, not about content).
- `member_name`/`member_number`/`days_attending`/`badge_type` are still
  included even though the agent no longer uses them to render
  anything — they're useful for the agent's own log lines (e.g.
  `"Got job 1: Jane Doe"`) and for debugging.

**No job available — `204 No Content`:**

Empty body. This is a real HTTP status, not a JSON `null` — check the
status code rather than trying to parse a body.

**Errors:**

- `401` — missing or invalid `Authorization` header.
- `400` — the `printer` query parameter is missing.
- `404` — no printer with that name is registered for this event.

## `POST /api/print-jobs/{eid}/{id}/result`

Posted by an agent after attempting to print. `{eid}` is the same
event ID used to fetch the job, and `{id}` is the `job_id` from the
job it was given. The matching print job entity's `status`/`message`
are updated (and the result is still logged via `\Drupal::logger()`).

**Request body:**

```json
{ "status": "success", "message": "Printed OK" }
```

- `status`: free-form string for now. Conceptually `"success"` or
  `"error"`.
- `message`: optional human-readable detail.

**Response — `200 OK`:**

```json
{ "received": true }
```

**Errors:**

- `401` — missing or invalid `Authorization` header.
- `404` — no print job with that ID exists for this event.

## What the agent does with a job

Unlike before, there's no "field mapping" table here anymore — the
agent doesn't build label content from field values, it just decodes
and finishes an image ConReg already rendered:

1. `base64.b64decode(job["image"])` → raw PNG bytes.
2. Save a local copy under `--output-dir` (always, print or no-print —
   this is your record of what was/would have been printed).
3. If `settings.rotate_degrees` is non-zero, rotate the decoded image
   by that many degrees (`PIL.Image.rotate(..., expand=True)`). A
   sanity check compares the result's pixel size against what
   `settings.page_size` implies and logs a warning (not an error - the
   label still prints) if they disagree by more than a couple of
   pixels, which would mean ConReg's render and this agent's settings
   are out of sync.
4. Unless suppressed (`--no-print`, or `settings.suppress_printing`),
   send the rotated image to CUPS: `lp -o PageSize=<settings.page_size>
   -o fit-to-page -o copies=<settings.copies>`.

See `print_image.py`'s `rotate_and_print()` — that one function is the
entire rendering-adjacent logic left in this repo. Everything about
*what the label says and where* lives in ConReg's `LabelRenderer`
service now.
