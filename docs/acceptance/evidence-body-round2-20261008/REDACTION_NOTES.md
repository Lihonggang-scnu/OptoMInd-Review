# Redaction and publication boundary

- This increment contains only the completed `packed_continuous` route. It is prepared locally for root review; it has not been committed or pushed.
- The public route keeps generated body/plan/request/response/usage/configuration/provenance material while omitting `new_round2_budget.sqlite` and every `transport/*.sse.raw` stream.
- Recursive JSON-field redaction replaced credential, API-key, bearer, authorization, cookie, password, private-key, signed-URL, authentication-signature, request/reservation/call-ID and key-file fields. Text redaction also covers bearer values, secret-like API-key strings, signed URL query strings and personal email addresses.
- Authentication signatures and signed download URLs are removed, while ordinary cache/scientific signature fields are preserved. Rights-bound papers and full source passages are not intentionally included. `FULL_BODY_INPUT.json` and `WRITING_EVIDENCE.json` are project-generated input/evidence snapshots; their local canonical byte counts and hashes remain in `packed_continuous/PROVENANCE.json` for audit.
- The public input and evidence snapshots are split only at strict UTF-8 character boundaries. Each `.parts.json` manifest records the sanitized canonical payload length and SHA-256, and the verifier reconstructs it before accepting the archive.
- Local paths are labels for authorized local audit and are not links. Original raw files remain under the run output; only sanitized public copies are under this package.
- Other round-2 routes remain outside this increment. No final selection or overall round conclusion is asserted.


