Day 1b: rewrite app/llm/meta_client.py to use the OpenAI Python SDK against Meta's Responses API instead of the anthropic SDK. Keep the same public interface (complete, stream, llm_calls logging, retry) so nothing else changes.

- client = openai.OpenAI(base_url=config.META_API_BASE, api_key=config.MODEL_API_KEY)
- complete(): client.responses.create(model=..., input=messages_or_string, instructions=system, reasoning={"effort": effort}, max_output_tokens=..., tools=tools, text={"format": {"type": "json_schema", "name": schema_name, "schema": json_schema, "strict": True}} when json_schema is given). Return output_text, parsed JSON when a schema was given, tool_calls list, usage, and response.id (so later calls can pass previous_response_id to preserve reasoning across agent turns).
- stream(): client.responses.create(..., stream=True), yield text deltas and tool-call deltas.
- Effort values: "minimal" | "low" | "medium" | "high". Default medium.
- Replace anthropic with openai in requirements.txt. Update the ingestion tagger call to pass its JSON schema through the new text.format path.
- Add scripts/meta_smoke.py: one real call with reasoning effort low asking for the single word OK, prints status and usage. Do not run it in tests.
- Update README Meta capabilities table to say Responses API.
- SQLite is the intended store for this project; it deploys to Cloud Run with /tmp plus GCS sync. Do not flag it.
- All tests green. Commit: "Day 1b: Meta client on Responses API with structured output and reasoning effort". Do not push.
