# Legacy Plus recovery: free budget guard check

- Executed on a temporary copy of `BUDGET.sqlite`; the live ledger and source were not mutated.
- A newly inserted ordinary `uncertain` row raised `QwenTransportError:legacy_unsettled_budget_requires_receipt_reconciliation`.
- `GlobalBudgetLedger.reserve(29.0)` with the existing settled spend and 30.0 CNY cap raised `QwenTransportError:global_budget_exceeded`.
- The audited waiver remained `amount_cny=0`, `actual_cny=NULL`, was excluded from guard reservations, and was retained under `waived_reservations` with its waiver ID.
- Provider factory calls: `0`; network calls: `0`.

Machine-readable evidence: `RECOVERY_FREE_GUARD_CHECK.json`.
