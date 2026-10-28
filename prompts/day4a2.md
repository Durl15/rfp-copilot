Fix: compliance matrix and go/no-go return status=incomplete with empty text because reasoning consumes the output cap.
1. app/agent/matrix.py: change reasoning effort from "high" to "medium" and raise max_output_tokens from 8000 to 24000.
2. In the shared client or both loops: if a response has status "incomplete" and incomplete_details.reason is "max_output_tokens", retry once with max_output_tokens doubled (cap 48000) and effort lowered one level, instead of returning or raising. Log both attempts.
3. Log reasoning_tokens vs visible output tokens at DEBUG each turn.
4. Apply the same incomplete-status handling in go_no_go.py (keep its effort at "high", cap 8000, same retry).
5. Test: incomplete-then-complete path in both loops with the mocked Responses-shaped model. All tests green.
Commit: "Fix: handle incomplete responses, medium effort, larger output cap". Do not push.
