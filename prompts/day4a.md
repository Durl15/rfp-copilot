Day 4a: fix compliance matrix honesty and classification.

1. Schema: add requirement_type to each matrix row: "capability" (work the firm must be able to perform), "eligibility" (certifications, set-aside, NAICS, location, registrations), or "submission" (format, page limits, volumes, deadlines, forms). gap applies fully to capability and eligibility rows. For submission rows, gap is always "none" and draft_response is a short action item (e.g. "Prepare technical volume within 15 pages") with no capability claims.
2. draft_response rules, enforced in the prompt and by a validator:
   - gap "none": may state the capability, using only facts quoted in firm_capability.
   - gap "partial": may draft only from quoted evidence and must end with one sentence naming what still has to be supplied.
   - gap "full": must NOT claim capability. It must begin with "Gap:" and state what is missing, then give one closing option from: teaming partner, subcontractor, hire, or acquire certification. No "We will deliver/execute/provide" language for capabilities the profile does not show.
   - Validator rejects any "full" draft that starts with "We will" or lacks the "Gap:" prefix; one retry with the reasons, then 502.
3. Unquoted-claim guard: every sentence in firm_capability must either contain a verbatim quote from the profile or equal exactly "No matching evidence in profile." Reject otherwise (same one-retry path).
4. Eligibility check: a requirement the profile satisfies by a quoted certification (e.g. HUBZone in certifications) is gap "none"; add to evidence_needed only a verification action ("Confirm HUBZone status is active in SAM.gov before submission").
5. Summary: compute overall_readiness_pct from capability and eligibility rows only; add counts_by_type to the summary.
6. Tests: submission rows are always gap none; a full-gap draft starting "We will" is rejected; an unquoted claim is rejected; readiness ignores submission rows.
Commit: "Day 4a: matrix honesty rules, requirement_type, unquoted-claim guard". Do not push.
