Day 6c: make voice intake work against the real Muse Voice Transcribe endpoint. Live testing found three facts the Day 6 implementation got wrong. Fix app/voice.py and everything downstream accordingly.



FACTS FROM THE LIVE API (POST {META\_API\_BASE}/asr/transcribe, bearer MODEL\_API\_KEY):

1\. Multipart body needs exactly two parts: "request" (JSON, content-type application/json, value {"model": "muse-voice-transcribe-1.0", "audioEncoding": "WAV"}) and "audio" (the file bytes, content-type audio/wav). Any other field names return 400 "Both a 'request' and an 'audio' part are required".

2\. The only accepted encoding is WAV, and the file must be mono 16-bit PCM at 16 kHz or 24 kHz. m4a, mp3 and other encodings are rejected ("Malformed request" or "must be a mono 16-bit PCM WAV at 16 or 24 kHz").

3\. Success response (200) shape: {"sessionId": "...", "transcript": "...", "audioDurationMs": 60000, "turns": \[]}.



CHANGES:

1\. app/voice.py transcribe(): accept any audio upload (m4a, mp3, wav, ogg, webm, aac). Convert to 16 kHz mono 16-bit PCM WAV with ffmpeg via the imageio-ffmpeg package (imageio\_ffmpeg.get\_ffmpeg\_exe(); args -y -i <in> -ac 1 -ar 16000 -sample\_fmt s16 -f wav <out>; use tempfile, always clean up). If ffmpeg fails, raise a VoiceError that the route maps to 400 "unsupported or corrupt audio" with the first 200 chars of ffmpeg stderr in the detail. Then POST the two parts exactly as in fact 1, timeout 180 s, one retry on 5xx/timeout, 502 on a second failure. Parse "transcript" and "audioDurationMs". Log to llm\_calls with feature="voice\_transcribe", model muse-voice-transcribe-1.0, latency, ok/error, and store audio\_duration\_ms (add a nullable column via a new migration if the table lacks one). Store the raw response JSON on the pending entry.

2\. Add imageio-ffmpeg to requirements.txt and requirements.lock.txt (pin the installed version). No system ffmpeg dependency; the Dockerfile needs no change, but confirm the package wheel bundles a linux x86\_64 binary and note that in the README.

3\. POST /voice/past-performance: add an optional form field "hints" (comma-separated proper nouns the firm knows, e.g. "The Woodfork Group, East Adams, Terry Woodfork"). Pass hints into the Muse Spark extraction prompt with this rule: if a transcribed name is a close phonetic match to a hint, use the hint spelling in the field VALUE, but the evidence quote stays verbatim from the transcript. Without hints, behavior is unchanged. The existing confirm-with-edits flow remains the human correction path.

4\. Extraction prompt: the first sentence of a note is often a preamble ("this is a past performance note for <firm>"); that firm is the speaker, never the client. Add a test case using the transcript fixture below.

5\. scripts/voice\_smoke.py: usage python scripts/voice\_smoke.py <audio> \[firm\_id] \[--hints "a, b"]. Print: converted WAV size, transcript, audioDurationMs, then each extracted field with its evidence quote, then the pending\_id. Exit non-zero on failure with the API error body printed.

6\. Logging safety: LOG\_LEVEL default INFO. Never set httpx/httpcore loggers to DEBUG from app code, because httpcore DEBUG prints request headers including Authorization. Add a one-line comment where logging is configured.

7\. Tests (mock httpx; do not call the network): transcribe builds the two parts with the exact names and the JSON request value; m4a input is converted (mock subprocess or feed a real tiny WAV generated with the stdlib wave module through the real ffmpeg); ffmpeg failure -> 400; API 200 with the response shape in fact 3 -> transcript stored and audio\_duration\_ms recorded; hints change the field value but not the evidence quote; preamble firm is not extracted as client. Put the transcript fixture in tests/fixtures/voice\_transcript\_woodfork.txt. It is a real Muse Voice Transcribe output, kept verbatim including its errors, and it reads:



this is a current performance note for DJ AI business consultant. A HubZone certified firm in Syracuse. The client was the Woodford Group, a minority owned MWBE contractor in Syracuse. I worked as their subcontractor on the East Adam RFQ project and the City MWBE project my role was was AI platform development and and staff training Over four months I built both the administrative platform and the client portal the client portal helped MWBEs and their team find and respond to bids along along with sharpening their skills and improving business processes processes and training their people to how to use them. The results was faster faster bid turnaround the pilot program is an ongoing and looking for completion in late February 2027 for reference you can contact Terry Woodford president of the Woodford Group.



Expected with hints "The Woodfork Group, East Adams, Terry Woodfork": client = "The Woodfork Group", reference mentions "Terry Woodfork", scope mentions East Adams, role = AI platform development and staff training, dates mention four months and late February 2027, outcome mentions faster bid turnaround, value\_range = null.

8\. Housekeeping: delete scripts/voice\_debug.py. Add data/\*.m4a, data/\*.wav, data/\*.mp3 to .gitignore. Update README voice section with the real endpoint contract (facts 1 to 3) and the conversion step.

9\. Run the full test suite; all must pass. Commit: "Day 6c: voice intake against live Muse Voice Transcribe (WAV conversion, request/audio parts, hints)". Push to origin main.



Output: summary of changed files, test count, and the exact command to run the smoke test. Do not print any secret or any request header.

