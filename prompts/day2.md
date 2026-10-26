Day 2: fixes, firm profiles, tool functions, and the Go/No-Go decision.

Fixes first:
- app/db.py: expose init_db() that runs all migrations idempotently. Call it at FastAPI startup and lazily from MetaClient._log so scripts never hit "no such table".
- scripts/meta_smoke.py: add sys.path insert so it runs as `python scripts/meta_smoke.py` from the repo root. Change its effort to "minimal".

Firm profiles:
- Table firms (id, name, hubzone bool, certifications json, naics_codes json, size_standard, place_of_performance, past_performance json list of {client, scope, value_range, dates, role, outcome, reference}). Seed three firms in app/seed.py: firm 1 DJ AI Business Consultant LLC (HUBZone, NAICS 541511 541512 541611 518210, Syracuse NY, past performance: IT consulting, civic AI briefing system, grant intelligence system); firm 2 a fictional Syracuse electrical contractor (NAICS 238210, HUBZone + SDVOSB); firm 3 a fictional engineering firm (NAICS 541330, HUBZone + WOSB). Realistic but clearly fictional names for 2 and 3.
- GET /firms, GET /firms/{id}.

Tools (app/agent/tools.py), each a plain Python function plus a Responses-API tool definition (type "function", name, description, parameters JSON schema):
- get_firm_profile(firm_id)
- get_solicitation(solicitation_id) -> tagged sections, due date, NAICS, set-aside, place of performance, text excerpt
- search_past_performance(firm_id, keywords) -> matching past performance entries
- lookup_naics(code) -> title and SBA size standard from a small bundled table of 30 common codes
- estimate_effort(gap_counts) -> hours
- record_decision(solicitation_id, firm_id, verdict, score, reasons) -> writes table decisions
A dispatcher execute_tool(name, args) with pydantic validation of args.

Go/No-Go (app/agent/go_no_go.py):
- POST /agent/go-no-go {firm_id, solicitation_id}. System prompt: bid-desk analyst for HUBZone small businesses. Model may call tools; loop until it returns a final structured answer matching schema GoNoGo {score: int 0-100, verdict: "bid"|"no_bid"|"needs_review", top_reasons: [str] max 3, blockers: [str], estimated_effort_hours: int, set_aside_match: bool, naics_match: bool}. Use reasoning effort "high", max_output_tokens 4000, pass previous_response_id between tool-call turns. Persist via record_decision. Return the decision plus the tool-call trace (list of {tool, args, result_summary}) so the UI can show the agent thinking.
- Log each model call with feature "go_no_go".

Tests: tool dispatcher validation, lookup_naics, seed idempotency, go/no-go loop with a mocked model that issues two tool calls then a final JSON answer. All green.
Commit: "Day 2: firm profiles, agent tools, Go/No-Go decision loop". Do not push.
