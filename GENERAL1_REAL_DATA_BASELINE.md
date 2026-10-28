# GENERAL1 REAL DATA BASELINE

**Audit date:** 2026-10-28. **Result:** NOT OBSERVABLE FROM GITHUB / INCONCLUSIVE. This report does not invent counts.

## Available source-of-truth

- main HEAD: `97d881311a68c54ec15a4f537d8a055e04729f44`.
- PR #52 HEAD: `f379ac3af68d3fbe8159226ec2cd4cca9059dc26` (open, unmerged).
- Prior Quant Validation audit HEAD: `c1cfb59ca50b4d47a6de51ad1f11ecb55781d3fa` (CI green).
- This branch inherits Quant Validation's `scripts/quant_validation1.py`.
- No user's local SQLite data, RPC credential, paper files, or private runtime log is available to this GitHub-connected session. The GitHub repository does **not** include live databases.
- Prior report for PR #43 describes **historical**, non-current isolated live measurements (815s, 225 blocks, 5,870 trades, ~0.276 blocks/s vs ~0.667 chain blocks/s, lag 521→630, 6 collector restarts); the new branch has not repeated these runs and prior data must not be treated as current success.

## Safe existing-data commands

Run from an already checked out copy of this gate branch:
```bash
python3 scripts/quant_validation1.py --data-dir data
python3 scripts/live_evidence_gate1.py --data-dir data
```
Both scripts open SQLite in `mode=ro`; neither creates a missing database or starts a worker. Do not submit DB files, .env files, RPC URLs, order IDs, wallet identifiers, raw logs, or market identities. The latter command provides sanitized counters for current local data.

## Existing-data audit status

| Measure | Result |
| --- | --- |
| Blocks/trades, distinct real market IDs | UNKNOWN (local DB unavailable) |
| DIAMOND / CANDIDATE / Risk PASS | UNKNOWN |
| Focus LOCKED / WAIT / READY | UNKNOWN |
| Book PASS | UNKNOWN |
| Paper P&L reconciliation | NOT VERIFIED |
| Out-of-sample trading alpha | NOT YET MEASURABLE |

**Do not conflate** total stored trades with supportable legacy-CTF executable signals. Timestamp provenance, schema versions, complete control population and future executable bid/ask quotes remain material. A missing observation is not a negative strategy result.
