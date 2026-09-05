# Print job API (POC contract)

> **Status: proof-of-concept.** No authentication, a single hardcoded
> job, no persistence. This contract will change once a real
> `print_jobs` entity backs it (see ConReg follow-up issues). Treat
> field names as stable but the underlying implementation as
> throwaway.

This is the HTTP contract between ConReg (Drupal) and the print-server
agent (a separate process, possibly on separate hardware, that drives a
Dymo LabelWriter via CUPS — see `print_label.py` in this directory for
the current rendering/printing proof of concept).

## Not yet implemented

- Authentication / per-device tokens.
- Job claiming or concurrency handling (multiple agents polling at
  once will currently all see the same job).
- Persistence — the job is a hardcoded array in
  `src/Controller/PrintJobController.php`, and results are only
  written to the Drupal log, not stored anywhere queryable.

## `GET /api/print-jobs/{eid}/next`

Polled by an agent to ask "is there a job for me?". `{eid}` is the
ConReg event ID and is required — an agent must be told which event
it's printing for (e.g. via `--eid` on `conreg_print_agent.py`).

**Job available — `200 OK`:**

```json
{
  "job_id": "1",
  "member_name": "Jane Doe",
  "member_number": "M-4021",
  "days_attending": "Fri-Sun"
}
```

**No job available — `204 No Content`:**

Empty body. This is a real HTTP status, not a JSON `null` — check the
status code rather than trying to parse a body.

## `POST /api/print-jobs/{eid}/{id}/result`

Posted by an agent after attempting to print. `{eid}` is the same
event ID used to fetch the job, and `{id}` is the `job_id` from the
job it was given.

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
