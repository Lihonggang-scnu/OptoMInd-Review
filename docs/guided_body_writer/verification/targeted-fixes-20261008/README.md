# Targeted guide/author fixes: offline verification

Base: `5f66b0f745cb254460318cdcec259f6a457e68ad`. The maker and author fixes are separate commits. Frozen planning and scientific source records were not rewritten. No provider calls were made.

## Tests

- Maker focused tests: **91 passed**.
- Author contracts/runtime/CLI: **78 passed**.
- Baseline full `tests/upgrade3`: **2057 passed, 17 failed, 10 subtests passed**.
- Combined fixed full suite: **2092 passed, 17 failed, 10 subtests passed**.
- The exact 17 failure names are unchanged; see `REGRESSION_COMPARISON.json`. This is not a claim that the entire repository suite is green.
- `git diff --check`: passed.

## Real-archive checks

The independent offline projection check used the archived cold-start guide and the real guided BODY inputs. Every chapter request exposes all seven writing arrangements. Removing only `outline_action` from the baseline materials yields the same remaining material hash as the fixed projection for all seven chapters; the evidence atom counts are unchanged. The historical leakage counts 15/5/15/8/18/17/2 become zero.

Manual A/B guide global strings already contain the arrangements verbatim: their lengths stay 4681/6947 characters. The renderer does not append duplicate copies to those guides.

The archived B chapter-six response remains incomplete when its completion metadata is missing. Recovery requires an explicit declaration bound to the saved stage/request, response and body; it cannot silently migrate old-code responses. Tests confirm that a valid declaration reuses the exact body/prefix and does not call the author again. Transport truncation and stale bindings are rejected.

The citation adapter preserves the canonical `FULL_BODY.md` and produces a separate `DELIVERY_BODY.md`. Tests use the actual downstream citation consumer with the archived response, including existing grouped citations. The independent full-B check formats 177 handle mentions without changing their identity sequence and is idempotent. Unknown identities are not invented.

## Interpretation and local retest

These checks demonstrate contracts, material preservation, deterministic recovery and delivery formatting. They do not prove that the revised prompts produce better real-model prose or guides. Use new output directories for the next real tests; preserve the original 30 CNY guide ledger and 60 CNY author ledger independently and check actual balances. See each module's `LOCAL_AGENT_HANDOFF.md`.

Both local agents should pin the final combined branch commit after both fixes have been published, while testing only their assigned module. This avoids changing implementation hashes during either run.
