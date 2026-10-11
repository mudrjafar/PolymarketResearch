# V2-COVERAGE-1B read-only audit

This audit consumes a saved JSON report from `scripts/v2_coverage_probe.py`.
It corroborates a bounded sample of decoded `OrderFilled` events against their
transaction receipts and the provider's block header, performs exact Gamma
lookups, and reads sampled V3 position IDs through the public CLOB order-book
endpoint. It never writes Collector state, orders, wallet state, or worker inputs.

## Run

Save a 1A report, then pass it to the audit. The RPC endpoint is read only; the
optional env file reader only reads `POLYMARKET_RPC_URL`.

```powershell
python scripts/v2_coverage_probe.py --lookback-blocks 500 --gamma-sample 10 > v2-coverage-1a.json
python scripts/v2_coverage_1b_audit.py --report v2-coverage-1a.json --env-file path\to\rpc.env
```

The report is intentionally printed to stdout. The audit does not create or
modify the supplied 1A report.

## Evidence and identity rules

- Receipt checks match each sampled `(transaction_hash, log_index)` to exactly
  one decoded receipt log, compare decoded event fields and receipt block data,
  then compare the sampled block hash with `eth_getBlockByNumber` from the same
  configured provider.
- `RECEIPT_AND_CANONICAL_BLOCK_MATCH` corroborates that sample only. A single
  provider cannot independently prove `eth_getLogs` completeness or block
  canonicality across providers.
- Gamma identity requires one exact `positionIds` match. A miss is
  inconclusive; duplicate matches are ambiguous and fail closed.
- The public CLOB book read uses the asset ID input supported by the official
  unified Python SDK. Only a response whose `asset_id` exactly equals the
  sampled PositionId confirms that the CLOB recognized that ID. A lookup error
  or missing book remains inconclusive.
- `positionIds` and `clobTokenIds` are reported as separate identifiers. The
  official CLOB v2 client types distinguish `positionID` for Polymarket V2
  outcomes from `tokenID` for CTF outcomes, and the Python client documents
  `position_id` as the Exchange V3 route. The audit never treats a CTF
  `clobTokenId` as proof of a V3 position identity ([TypeScript type](https://github.com/Polymarket/clob-client-v2/blob/main/src/types/ordersV2.ts),
  [Python client usage](https://github.com/Polymarket/py-clob-client-v2),
  [official public book-read example](https://github.com/Polymarket/py-sdk/blob/main/examples/market_prices.py)).

All results remain research evidence. They do not authorize production
Collector changes, trading, or a completeness claim.
