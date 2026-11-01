Day 7a: the agent console. A single self-contained page served by FastAPI at GET / (app/static/index.html, plus app/static/console.js and console.css; no build step; Tailwind via CDN is acceptable; no other frameworks). The page is the demo surface for the 3-minute video, so it must show the agent working, not a dashboard of numbers.



BRANDING: "RFP Copilot" wordmark with "DJ AI Business Consultant LLC" beneath; phoenix mark as inline SVG (simple, geometric, rising bird, not a stock icon); palette: near-black text, warm off-white background, one accent (deep ember orange #C8451B) for actions and the live-trace cursor, slate grey for secondary. No purple, no gradients, no generic AI aesthetic. System font stack. Dark mode not required.



LAYOUT (desktop first, 1280 wide, usable at 1024):

Left column (320px): Firm picker (GET /firms), the selected firm's profile card (name, HUBZone badge, NAICS codes, size standard), then its past-performance list: each entry with client, one-line scope, dates, and a VERIFIED or UNVERIFIED badge; verified entries get a small Remove button (see admin route below). Below that, a Solicitation picker listing GET /demo/solicitations plus every ingested solicitation (add GET /solicitations returning id, title, naics, set\_aside, participation\_goals, due\_date, demo flag, created\_at, newest first). The DASNY 7628 entry shows its full title including the "real NYS RFP ... due date shifted for demo" text.



Center column (flex): "Run agent" button. On click, POST /agent/run for the selected firm and solicitation and render the SSE stream live as a trace: one row per event, timestamped, with the tool name and arguments for tool calls, result summaries collapsed by default, and the verdict rendered as a card the moment run\_complete or the decision event arrives (verdict, score, verdict\_source, verified\_matches with their why text, top\_reasons, blockers, estimated\_effort\_hours). While streaming, the newest row has a blinking accent cursor. After the run: tabs for Matrix (table: requirement, type, status, evidence quote, draft response; Gap rows highlighted), Narrative, Capability Statement, and Download buttons wired to GET /agent/export for capability, matrix and bidpacket. Show tokens in/out, cached tokens, latency and est\_cost\_usd in a footer line for the run.



Replay: a "Replay last run" control that loads GET /runs/{run\_id} and plays the stored events at 8x with the same renderer (the live run takes \~2 minutes; the video needs the compressed version). Keep the last run\_id per firm in localStorage; wrap every localStorage read/write in try/catch.



Right column (360px): Voice intake. Two ways in: an Upload button (accepts audio/\*) and a Record button using MediaRecorder (audio/webm; the server already converts anything through ffmpeg). A Hints text field (placeholder: "names the transcript may misspell, comma-separated"). On submit, POST /voice/past-performance with firm\_id, hints and the file, show a progress state ("Transcribing with Muse Voice Transcribe" then "Extracting with Muse Spark"), then render the transcript and the seven fields, each as an editable input with its verbatim evidence quote beneath in small type, null fields shown as empty with "not mentioned". Confirm button -> POST /voice/confirm with pending\_id and any edits -> refresh the firm's past-performance list, where the new entry appears with VERIFIED. Reject button -> POST /voice/reject.



ADMIN ROUTE: DELETE /firms/{firm\_id}/past-performance/{entry\_index}, header X-Admin-Key must equal COPILOT\_ADMIN\_KEY, removes the entry and emits an event. The Remove button in the page prompts for the admin key once and keeps it in sessionStorage. This exists so the demo can be reset between takes: remove the Woodfork entry, run (no\_bid by rule), record the note, confirm, run again (needs\_review).



ERROR STATES: every fetch shows a human-readable error inline (not an alert dialog). 401 on admin -> "admin key rejected". 502 from voice -> show the API error body.



ROBUSTNESS: the page must work with the SSE stream over Cloud Run (no buffering assumptions; the server sets Cache-Control: no-cache and X-Accel-Buffering: no on the SSE response). Add a /health check in the page footer showing model name and API base.



TESTS: GET / returns the page; GET /solicitations lists ingested and demo rows newest first with participation\_goals; DELETE admin route rejects a missing or wrong key and removes an entry with the right key; SSE response headers. Full suite passes.



README: add a "Console" section with a screenshot placeholder path docs/console.png (I will add the image) and the demo-reset sequence.



Commit: "Day 7a: agent console page, GET /solicitations, admin remove past-performance, replay". Push to origin main.



Output: changed files, test count, and the exact URL and steps to open the console locally. No secrets.

