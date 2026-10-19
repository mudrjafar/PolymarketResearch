"""H1 collector tests: only mocked Gamma responses and temporary files."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from scripts import market_discovery as m

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
C = "0x" + "ab" * 32


def market(mid="1", hours=48, **changes):
    row = {
        "id": mid, "conditionId": "0x" + format(int(mid), "064x"),
        "question": f"Market {mid}?", "outcomes": '["Yes", "No"]',
        "clobTokenIds": json.dumps([str(1000 + int(mid) * 2), str(1001 + int(mid) * 2)]),
        "active": True, "closed": False, "acceptingOrders": True,
        "tags": [{"slug": "politics", "label": "Politics"}],
        "endDate": m.utc_iso(NOW + timedelta(hours=hours)),
    }
    row.update(changes)
    return row


class Response:
    def __init__(self, value, status_code=200):
        self.value, self.status_code = value, status_code

    def json(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


def get_for(rows, next_cursor=None):
    calls = []
    def get(url, params=None, timeout=None):
        calls.append((url, params, timeout))
        return Response({"markets": rows, "next_cursor": next_cursor})
    return get, calls


def test_t01_valid_in_two_days():
    assert m.candidate(market(), NOW)[0]["market_id"] == "1"


def test_t02_expired_and_exact_now_rejected():
    assert m.candidate(market(hours=-1), NOW)[0] is None
    assert m.candidate(market(hours=0), NOW)[0] is None


def test_t03_exact_five_days_accepted():
    assert m.candidate(market(hours=120), NOW)[0] is not None


def test_t04_one_second_past_five_days_rejected():
    assert m.candidate(market(endDate=m.utc_iso(NOW + timedelta(days=5, seconds=1))), NOW)[0] is None


@pytest.mark.parametrize("date", [None, "not-a-date", "2026-10-19T10:00:00"])
def test_t05_missing_invalid_naive_end_date_rejected(date):
    assert m.candidate(market(endDate=date), NOW)[1] == "invalid_end_date"


@pytest.mark.parametrize("field,value", [("active", False), ("closed", True), ("acceptingOrders", False), ("acceptingOrders", None)])
def test_t06_not_trading_rejected(field, value):
    assert m.candidate(market(**{field: value}), NOW)[0] is None


@pytest.mark.parametrize("labels", ['["Yes","No","Draw"]', '["YES","yes"]'])
def test_t07_nonbinary_rejected(labels):
    assert m.candidate(market(outcomes=labels), NOW)[1] == "not_binary"


def test_t08_identical_duplicate_deduplicates():
    rows, counts, eligible = m.select_markets([market(), market()], NOW)
    assert len(rows) == eligible == 1
    assert counts["duplicate_identical"] == 1


def test_t09_market_identity_conflict_fails_closed():
    with pytest.raises(m.DiscoveryError, match="duplicate"):
        m.select_markets([market(), market(question="Changed!")], NOW)


def test_t10_outcome_asset_pair_kept_distinct():
    row, error = m.candidate(market(), NOW)
    assert error is None
    assert row["outcomes"] == [{"name": "Yes", "asset_id": "1002"}, {"name": "No", "asset_id": "1003"}]


def test_t11_unknown_protocol_never_grants_execution():
    row, error = m.candidate(market(positionIds=["22", "23"]), NOW)
    assert error is None
    assert row["protocol_identity_status"] == "UNVERIFIED"
    assert row["execution_eligible"] is False
    assert "ctf_token_id" not in row


def test_t12_more_than_100_response_fails_closed():
    get, calls = get_for([market(str(i)) for i in range(1, 102)])
    with pytest.raises(m.DiscoveryError, match="100"):
        m.fetch_market_page(NOW, getter=get)


def test_t13_exhausted_429_preserves_snapshot(tmp_path):
    target = tmp_path / "markets.json"
    target.write_bytes(b"PREVIOUS")
    calls = []
    def get(*args, **kwargs):
        calls.append(1)
        return Response(None, 429)
    with pytest.raises(m.DiscoveryError):
        m.run(now=NOW, output=target, getter=get, sleeper=lambda t: None)
    assert target.read_bytes() == b"PREVIOUS"
    assert len(calls) == 3


@pytest.mark.parametrize("response", [[], {"wrong": []}, {"markets": "not-list"}, {"markets": [], "next_cursor": ""}])
def test_t14_bad_envelope_fails(response):
    with pytest.raises(m.DiscoveryError):
        m.fetch_market_page(NOW, getter=lambda *a, **kw: Response(response))


def test_t15_second_run_replaces_first(tmp_path):
    target = tmp_path / "markets.json"
    m.run(now=NOW, output=target, getter=get_for([market("1")])[0], printer=lambda x: None)
    m.run(now=NOW, output=target, getter=get_for([market("2")])[0], printer=lambda x: None)
    obj = json.loads(target.read_text())
    assert [x["market_id"] for x in obj["markets"]] == ["2"]


def test_t16_failed_second_run_keeps_bytes(tmp_path):
    target = tmp_path / "markets.json"
    m.run(now=NOW, output=target, getter=get_for([market()])[0], printer=lambda x: None)
    before = target.read_bytes()
    with pytest.raises(m.DiscoveryError):
        m.run(now=NOW, output=target, getter=lambda *a, **kw: Response([], 403), printer=lambda x: None)
    assert target.read_bytes() == before


def test_t17_empty_valid_snapshot_is_explicit_limited_sample(tmp_path):
    target = tmp_path / "markets.json"
    snap = m.run(now=NOW, output=target, getter=get_for([market(hours=-1)])[0], printer=lambda x: None)
    assert snap["counts"]["saved"] == 0
    assert snap["scope"] == "LIMITED_SAMPLE"
    assert target.exists()


def test_t18_sort_and_cap_25():
    src = [market(str(i), hours=120 - i) for i in range(1, 36)]
    snap = m.build_snapshot(NOW, src, True)
    assert len(snap["markets"]) == 25
    assert snap["counts"]["omitted_due_to_candidate_cap"] == 10
    assert snap["markets"][0]["market_id"] == "35"
    assert snap["limits"]["next_cursor_present"] is True


def test_t19_rpc_not_required(monkeypatch, tmp_path):
    monkeypatch.delenv("POLYMARKET_RPC_URL", raising=False)
    target = tmp_path / "markets.json"
    snap = m.run(now=NOW, output=target, getter=get_for([market()])[0], printer=lambda x: None)
    assert snap["counts"]["saved"] == 1


def test_t20_timezone_aware_conversion():
    row = market(endDate="2026-10-20T14:00:00+02:00")
    parsed, error = m.candidate(row, NOW)
    assert error is None
    assert parsed["end_date"] == "2026-10-20T12:00:00Z"


def test_gamma_query_is_bounded_to_one_page_and_five_days():
    get, calls = get_for([])
    markets, more = m.fetch_market_page(NOW, getter=get)
    assert markets == [] and not more
    assert len(calls) == 1
    _, params, timeout = calls[0]
    assert params["limit"] == 100
    assert params["end_date_min"] == m.utc_iso(NOW)
    assert params["end_date_max"] == m.utc_iso(NOW + timedelta(days=5))
    assert timeout == 15


def test_conflicting_shared_condition_or_asset_rejected():
    with pytest.raises(m.DiscoveryError, match="Condition"):
        m.select_markets([market("1"), market("2", conditionId=market("1")["conditionId"])], NOW)
    with pytest.raises(m.DiscoveryError, match="Asset"):
        m.select_markets([market("1"), market("2", clobTokenIds=market("1")["clobTokenIds"])], NOW)


def test_retries_then_succeeds():
    calls = []
    def get(*args, **kwargs):
        calls.append(1)
        return Response(None, 503) if len(calls) == 1 else Response({"markets": []})
    assert m.fetch_market_page(NOW, getter=get, sleeper=lambda t: None) == ([], False)
    assert len(calls) == 2


def test_missing_or_invalid_assets_rejected():
    assert m.candidate(market(clobTokenIds='["123", "123"]'), NOW)[1] == "missing_or_ambiguous_identity"
    assert m.candidate(market(clobTokenIds='["x", "y"]'), NOW)[1] == "missing_or_ambiguous_identity"


def test_partial_market_response_does_not_become_success(tmp_path):
    output = tmp_path / "markets.json"
    output.write_bytes(b"ORIGINAL")
    with pytest.raises(m.DiscoveryError):
        m.run(now=NOW, output=output, getter=lambda *a, **kw: Response(ValueError("bad json")))
    assert output.read_bytes() == b"ORIGINAL"


# H1.1 sport and unknown category checks

def test_sport_tag_excluded_even_with_politics_tag():
    src = market(tags=[{"slug": "sports"}, {"slug": "politics"}])
    assert m.candidate(src, NOW)[1] == "sports_excluded"


def test_soccer_from_event_tags_excluded():
    src = market(tags=[], events=[{"tags": [{"slug": "soccer"}]}])
    assert m.candidate(src, NOW)[1] == "sports_excluded"


def test_game_id_excluded_even_with_nonsport_tag():
    assert m.candidate(market(gameId="some-game"), NOW)[1] == "sports_excluded"


def test_unknown_tag_fails_closed():
    assert m.candidate(market(tags=[{"slug": "mystery-taxonomy"}]), NOW)[1] == "unknown_category_excluded"
    assert m.candidate(market(tags=[]), NOW)[1] == "unknown_category_excluded"


def test_missing_tags_fails_closed():
    src = market()
    del src["tags"]
    assert m.candidate(src, NOW)[1] == "unknown_category_excluded"


def test_malformed_tags_fails_closed():
    assert m.candidate(market(tags="sports"), NOW)[1] == "unknown_category_excluded"


def test_verified_nonsport_retained():
    row, error = m.candidate(market(tags=[{"slug": "politics"}]), NOW)
    assert error is None and row["market_id"] == "1"


def test_same_event_sport_excluded_reduces_candidates():
    rows = [market(str(i), tags=[{"slug": "soccer"}]) for i in range(1, 25)]
    rows.append(market("25", tags=[{"slug": "politics"}]))
    snap = m.build_snapshot(NOW, rows, True)
    assert snap["counts"]["saved"] == 1
    assert snap["counts"]["excluded"]["sports_excluded"] == 24


def test_tag_hydration_requested_in_one_market_page():
    get, calls = get_for([])
    m.fetch_market_page(NOW, getter=get)
    assert len(calls) == 1
    assert calls[0][1]["include_tag"] == "true"
