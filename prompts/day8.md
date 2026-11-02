Day 8: verdict floor symmetry, solicitation cleanup, README for judges.



LIVE EVIDENCE: on the deployed service, firm 1 with one validated verified match (Woodfork, listed in verified\_matches) got verdict no\_bid score 28 verdict\_source=model. Friday's run on the same inputs gave needs\_review 55. The floor only caps the score for 1-2 matches; it does not constrain the verdict direction, so the direction varies run to run. Fix that.



1\. VERDICT FLOOR, symmetric and deterministic in direction (app/agent/go\_no\_go.py):

&#x20;  - blockers non-empty -> no\_bid, score <= 35, verdict\_source=rule, rule\_reason names the blocker.

&#x20;  - 0 validated verified\_matches and no blockers -> no\_bid, score <= 35, rule.

&#x20;  - 1-2 validated verified\_matches and no blockers -> verdict must be needs\_review, score clamped to 40..65; if the model said no\_bid or bid, override and set verdict\_source=rule with rule\_reason "one or two verified scope matches: review required, bid not yet supported"; if the model already said needs\_review within range, verdict\_source stays model.

&#x20;  - 3+ validated verified\_matches and no blockers -> model verdict stands, no cap.

&#x20;  - Keep the model's top\_reasons and rationale untouched in every case; the rule changes verdict and score only.

&#x20;  - Tighten the schema instruction for verified\_matches: list EVERY firm entry marked verified whose scope overlaps the solicitation in any material way; relevance strength belongs in top\_reasons, not in the decision to list. Tests for all four bands including the override case; update any test that assumed the old behavior.



2\. SOLICITATION CLEANUP:

&#x20;  - DELETE /solicitations/{id}, X-Admin-Key required; also deletes that solicitation's decisions, matrix rows, artifacts and runs. Returns counts.

&#x20;  - POST /solicitations/dedupe, X-Admin-Key: removes rows with empty or null title, and for rows sharing the same title and source keeps the newest and deletes the rest (cascade as above). Returns what it removed. Dry-run flag ?dry\_run=true returns the plan without deleting.

&#x20;  - Console: ingested (non-demo) solicitations get a small Remove button behind the admin key, same pattern as past-performance Remove.

&#x20;  - Tests for both routes, cascade, dry run, and auth.



3\. README rewrite for a judge who has 10 minutes. Sections in this order:

&#x20;  - One-paragraph pitch (use the existing one).

&#x20;  - "Try it in 3 minutes": live URL, the demo sequence (pick DJ AI, pick DASNY 7628, Run; upload a voice note or use the sample sequence; Confirm; Run again), what to expect at each step, and the honesty rules that explain the verdicts.

&#x20;  - Architecture: a text diagram of console -> FastAPI -> agent loop -> Meta Model API (Muse Spark 1.3 Responses API, Muse Voice Transcribe) -> SQLite -> GCS; Cloud Scheduler daily run. Keep it ASCII, no image dependency.

&#x20;  - Meta capabilities table: capability, where it is used (file and route), why it matters. Rows: tool calling in the agent loop; structured outputs validated by pydantic (go/no-go, matrix, voice extraction); reasoning effort dial per task; prompt caching on the firm/rubric prefix with cached\_tokens logged; token counting as a guard before each run; stored responses with previous\_response\_id and stateless fallback; SSE streaming of the run; Muse Voice Transcribe with the WAV contract. Only list what the code actually does; verify each row against the code before writing it. Do not mention Muse Code or any Meta tool not used.

&#x20;  - Honesty guards: verbatim evidence quotes, verified flag and claim-word validator, verified\_matches validated in code, the deterministic verdict floor table, "Draft for human review" footer, confirm-before-save on voice.

&#x20;  - What pre-existed vs what was built Oct 27 to Nov 4: pre-existing = CivicProcureBot (private, read-only shortlist dependency, optional, with fallback); everything in this repo is new. Day-by-day commit summary from git log.

&#x20;  - Demo data disclosure: demo firms and the electrical solicitation are fictional and labeled; DASNY 7628 is a real closed NYS RFP with the due date shifted and labeled; DJ AI is a real firm and the Woodfork entry is a real engagement confirmed by voice note.

&#x20;  - Run locally, deploy (the three scripts), env table, abuse limits, costs (PRICE\_\* placeholders, say they are placeholders until Meta publishes rates in the console).

&#x20;  - Known limits and what's next (TradesBrain voice intake, open-weight side-by-side, CivicProcureBot Pro tier).

&#x20;  Keep docs/console.png referenced near the top.



4\. Full suite passes. Commit: "Day 8: symmetric verdict floor, solicitation cleanup routes, README for judges". Push to origin main.



Output: changed files, test count, and the exact curl commands to dedupe production (dry run first) and to delete one solicitation by id. No secrets.

