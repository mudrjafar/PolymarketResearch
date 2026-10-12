# V3 Binary and NegRisk identity safety

Exchange V3 positions use a structured PositionId. The Runtime's
`scripts/v3_identity.py` decoder is the sole source for module classification
and validation. Binary and NegRisk IDs are eligible only with Polygon
resolution-chain encoding, binary outcomes, and the documented NegRisk arity
rules. Combinatorial IDs remain `RESEARCH_ONLY`; malformed IDs fail closed.

The read-only discovery command searches active Gamma markets, requires the
PositionId's derived condition and outcome to match exactly, checks the public
CLOB book's returned `asset_id`, then scans finalized Exchange V3
`OrderFilled` logs for that exact ID. A candidate is reported as confirmed only
after its transaction, log index, successful receipt, receipt block hash, and
canonical block header all agree. Gamma pagination and event lookback are
bounded; missing hits are inconclusive and never prove absence. The audit does
not print or serialize the RPC URL.

Run it with `python scripts/v2_coverage_1b_audit.py --discover-active --env-file .env --lookback-blocks 500 --gamma-max-pages 100`. The page and
block limits are recorded in the JSON result so a capped scan remains visibly
inconclusive.

Paper settlement now supports only V3 Binary and NegRisk identities derived
from the PositionId. It reads the module's `getResult(bytes31)` at the
repository's existing finalized Polygon block, first checks the deployed
module's `moduleId()` and `COLLATERAL_TOKEN()` against the decoded identity,
and requires a two-element payout
vector normalized to 1,000,000, and additionally requires fully binary
NegRisk payouts. An empty result at the finalized block remains unresolved; a
result visible only at `latest` is `RESOLVED_NOT_FINAL`. Combos, malformed
identities, condition/outcome mismatches, unknown contracts, unexpected
collateral, or malformed payouts fail closed. The V3 branch never calls the
legacy CTF settlement contract.

The protocol basis is the official
[Polymarket V2 contract repository](https://github.com/Polymarket/polymarket-v2-external):
its [PositionId layout and Polygon deployments](https://github.com/Polymarket/polymarket-v2-external),
[module result and NegRisk derivation rules](https://github.com/Polymarket/polymarket-v2-external/blob/main/docs/modules.md),
[PMCT collateral specification](https://github.com/Polymarket/polymarket-v2-external/blob/main/docs/collateral.md),
and [`BaseModule.getResult` / `getPayout`](https://github.com/Polymarket/polymarket-v2-external/blob/main/src/modules/abstract/BaseModule.sol).

Offline integration tests exercise synthetic Binary and NegRisk positions
through Collector normalization, Flow eligibility, Diamond identity matching,
Risk, Focus, Book identity validation, and Paper settlement. They do not start
the production Collector or write runtime state.
