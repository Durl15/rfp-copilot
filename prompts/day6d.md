Day 6d: fix the verdict floor, the ingestion tagger, and voice extraction, based on live runs against a real RFP.



LIVE EVIDENCE

Firm 1 (DJ AI) now has one verified=true past-performance entry (client "The Woodfork Group", scope mentions MWBE pilot, bid platform, business processes, staff training). Go/no-go against solicitation 03f8333d-1d96-4ec3-a011-323186a81212 (DASNY RFP 7628, Small Business Capacity Building Services, ingested from PDF): the search\_past\_performance tool call with keywords \[capacity building, contractor development, MWBE, curriculum, bonding] returned the Woodfork entry as a match, and the model's own reasoning said "one verified scope match ... needs\_review, cap 65". The deterministic floor then reported "No verified past performance matches this solicitation's scope; verdict forced to no\_bid, score capped at 35", verdict\_source=rule. The floor counted zero while the tool returned one verified match. Decision id is the newest row in decisions for firm 1.



1\. DIAGNOSE first. Read app/agent/go\_no\_go.py and wherever the floor counts verified matches. Explain in the summary exactly why the count was zero (wrong data source, filter on a field the match lacks, counting only the first tool call, string vs bool on verified, etc.). Do not guess; trace it with the stored run/trace for that decision if available.



2\. REPLACE the counting mechanism. Add a required field to the go/no-go structured output schema: verified\_matches, a list of objects {entry\_index: int, why: string (one sentence)}. Instruct the model to list only past-performance entries it relied on as scope-relevant. Validate deterministically after the call: each entry\_index must exist on the firm profile and have verified=true; drop any that fail and record them in a new decision field rejected\_matches with the reason. The floor rule uses len(validated verified\_matches): 0 -> no\_bid cap 35; 1-2 -> needs\_review cap 65; 3+ -> no cap; blockers -> no\_bid. Keep search\_past\_performance as a tool, but it no longer drives the floor. Store verified\_matches and rejected\_matches on the decision and return them in the API response. Expose entry\_index in get\_firm\_profile and search\_past\_performance results so the model can reference entries unambiguously.



3\. INGESTION TAGGER fixes (app/ingest.py), from the DASNY 7628 run:

&#x20;  a. set\_aside: distinguish a set-aside (contract restricted to a category: HUBZone, 8(a), SDVOSB, WOSB, small business, MWBE set-aside) from utilization/participation goals (MWBE 18%, WBE 12%, SDVOB 6%, DBE goals). Goals are not a set-aside. Schema: set\_aside (string or null) plus a new participation\_goals field (string or null, e.g. "MBE 18%, WBE 12%, SDVOB 6%"). The DASNY text was tagged set\_aside=SDVOSB; it must come out set\_aside=null, participation\_goals populated. Migration adds the column.

&#x20;  b. due\_date: when several dates appear, pick the proposal/response due date, never the questions deadline, pre-proposal conference, or issue date. DASNY came out 2026-01-20 (questions deadline) and must come out 2026-02-02. Add the rule to the regex pass and to the LLM gap-fill prompt, and add a test with a short fixture containing all four kinds of dates.

&#x20;  c. A NYS/local RFP has no federal Section C/L/M labels; the LLM fill handled it, which is fine, but record in llm\_filled (already done) and in the README that state and local documents are mapped onto C/L/M by the model.



4\. VOICE EXTRACTION tweaks (app/voice.py), from the live Woodfork run:

&#x20;  a. Define scope in the extraction prompt as "what was built, delivered or performed", not the engagement title. On the Woodfork fixture, scope must include the administrative platform, the client portal, bid finding and response, business process improvement, and staff training (hints applied, evidence verbatim). Update the test expectation.

&#x20;  b. dates: capture every time reference in the note (duration, start, end, expected completion). On the fixture, dates must mention both "four months" and "late February 2027".

&#x20;  c. Start extraction at max\_output\_tokens=3000, reasoning effort minimal. Remove the 1500 first attempt. Keep the single retry on incomplete.



5\. DEMO LABEL route: POST /solicitations/{id}/demo-label, header X-Admin-Key = COPILOT\_ADMIN\_KEY, body {title?, due\_date?, set\_aside?, participation\_goals?, demo: bool}. Sets demo=true and the given fields. Replaces the manual scripts/demo\_label.py edit; delete that script. The README explains that the DASNY 7628 demo solicitation is a real closed NYS RFP with its due date shifted and says so in its title.



6\. Tests: floor uses validated verified\_matches (0, 1, 3 cases; a model-claimed match on an unverified entry is rejected and not counted; an out-of-range index is rejected); tagger set-aside vs goals; tagger due-date selection; voice scope/dates on the Woodfork fixture; demo-label route auth and effect. Full suite must pass.



7\. Re-run live (MODEL\_API\_KEY in .env): go/no-go firm 1 against 03f8333d-1d96-4ec3-a011-323186a81212. Report verdict, score, verdict\_source, verified\_matches, rejected\_matches, top\_reasons. Expected: needs\_review, score <= 65, verified\_matches contains the Woodfork entry, verdict\_source=rule or model consistently with the new floor. If the live result differs, say so and why; do not tune anything to force it.



8\. Commit: "Day 6d: verified\_matches floor, tagger set-aside/goals and due-date rules, voice scope/dates, demo-label route". Push to origin main.



Output: the diagnosis from step 1 in plain words, changed files, test count, and the live re-run result. No secrets, no request headers.

