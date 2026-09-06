# Print job API contract

> **Status: backed by real entities, no UI yet.** Jobs and printers are
> now `conreg_print_job`/`conreg_printer` content entities in ConReg,
> with atomic job claiming. There's still no Member Check-In UI trigger
> and no per-device authentication — see ConReg follow-up issues.

This is the HTTP contract between ConReg (Drupal) and the print-server
agent (a separate process, possibly on separate hardware, that drives a
Dymo LabelWriter via CUPS — see `print_label.py` in this directory for
the current rendering/printing proof of concept).

## Not yet implemented

- Authentication / per-device tokens.
- Any UI to create jobs. `PrintJobManager::createJob(int $mid, string
  $printerName)` (in the ConReg module) creates a `pending` print job
  for a member, but nothing calls it yet — jobs must currently be
  created directly, e.g. via `drush php:eval`.

## `GET /api/print-jobs/{eid}/next?printer=<name>`

Polled by an agent to ask "is there a job for me?". `{eid}` is the
ConReg event ID, and the `printer` query parameter is the printer's
name (e.g. `Bilbo Baggins`, URL-encoded) — both are required, since a
job is targeted at one specific printer. `conreg_print_agent.py` sends
both automatically from its `--eid`/`--printer` arguments.

ConReg atomically claims the oldest pending job for that event/printer
pair before returning it, so two agents polling on behalf of the same
printer can't both receive the same job.

**Job available — `200 OK`:**

```json
{
  "job_id": "1",
  "member_name": "Jane Doe",
  "member_number": "4021",
  "days_attending": "Friday, Saturday, Sunday"
}
```

**No job available — `204 No Content`:**

Empty body. This is a real HTTP status, not a JSON `null` — check the
status code rather than trying to parse a body.

**Errors:**

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
  `"error"`, matching the success/failure branches already in
  `print_label.py`.
- `message`: optional human-readable detail (e.g. stdout/stderr text).

**Response — `200 OK`:**

```json
{ "received": true }
```

**Errors:**

- `404` — no print job with that ID exists for this event.

## Field mapping to `print_label.py`

| JSON field        | `print_label.py` argument      |
|--------------------|--------------------------------|
| `member_name`      | positional `name`               |
| `member_number`    | `--number`                      |
| `days_attending`   | `--days`                        |

`print_label.py` itself stays CLI-only and knows nothing about ConReg
or HTTP — that keeps `python3 print_label.py "Test Name" --no-print`
usable for testing the printer/layout in isolation. `conreg_print_agent.py`
is the piece that fetches jobs from these endpoints and invokes
`print_label.py` (as a subprocess) using the mapping above, then posts
the result back.
