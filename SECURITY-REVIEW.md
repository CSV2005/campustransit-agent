# Security review: chat data exposure and answer grounding

Review scope: chat request/profile/history handling, LLM grounding checks, Ollama health
responses, and the outbound inference path.

## Findings and remediation

| # | Severity | Finding | Status |
|---|---|---|---|
| 1 | MEDIUM | Client-supplied profile values could be interpolated into the system prompt, and client-supplied assistant history was trusted as prior model output. | Fixed: the chat API now validates profile fields, places normalized preferences in a separate user message, and only forwards user-role history. |
| 2 | MEDIUM | A matching tool family could allow unsupported metro-line or schedule claims, and a fare could pass when its amount appeared in an unrelated numeric field. | Fixed for these identified claim classes: line names and explicit schedule times must occur in structured tool data; fare amounts must occur in currency-denominated fields from approved fare tools. |
| 3 | MEDIUM | The unauthenticated chat-health response could expose URL credentials configured in `OLLAMA_BASE_URL`. | Fixed: public status contains only the endpoint origin; error details redact URL userinfo and sensitive query parameters. |

## Remaining data-flow considerations

- Chat messages, user history, profile preferences, and tool results are sent to the configured
  Ollama endpoint. The default is local. For remote inference, use an HTTPS endpoint and
  ensure the provider's data-handling terms are appropriate.
- When configured, Google Places receives autocomplete queries. Location text therefore
  leaves the app for that optional provider.
- These guards are deterministic checks, not a proof that every free-form natural-language
  claim is correct. The LLM can still produce unsupported claims outside the specific
  structured checks; when a check fails, the app withholds the LLM answer and uses its
  deterministic rules path.

## Validation

Focused offline checks passed:

- `pytest -q tests/test_ollama_client.py -k 'health_status_strips or price_audit or grounding_accepts or grounding_rejects_an_unsupported or chat_endpoint_does_not_forward or chat_profile_rejects or llm_profile_and_history'`
- `pytest -q tests/test_ollama_client.py -k 'not live_model'`

The project directory did not contain Git metadata during this task, so this report and
the code changes could not be committed.
