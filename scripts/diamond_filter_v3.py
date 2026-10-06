import argparse
import json
import os
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from machine_common import fresh, age_seconds, tail_jsonl, finite_number, save_json_atomic as atomic_save

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
FLOW_FILE = DATA_DIR / "flow_state.json"
LIVE_TRADES_FILE = DATA_DIR / "live_trades.jsonl"
DIAMONDS_FILE = DATA_DIR / "diamonds.json"
CANDIDATES_FILE = DATA_DIR / "diamond_candidates.json"
ANALYSIS_FILE = DATA_DIR / "diamond_analysis_v3.json"
GENERATIONS_DIR = DATA_DIR / "diamond_generations"
GENERATION_MANIFEST_FILE = DATA_DIR / "diamond_generation.json"

MAX_LIVE_LINES = 10000
MIN_TRADES_5M = 8
MIN_VOLUME_5M = 500.0
MIN_STRENGTH_5M = 0.35
MIN_STRENGTH_15M = 0.25
MIN_DATA_CONFIDENCE = 0.55
MAX_LARGEST_TRADE_RATIO = 0.70
MIN_SIGNAL_QUALITY = 80.0
MIN_VERIFICATION = 75.0
MIN_ENTRY_QUALITY = 40.0
MIN_RESOLUTION_RELIABILITY = 70.0
LARGE_CASHFLOW_5M = 5000.0
VERY_LARGE_CASHFLOW_5M = 10000.0
EXTREME_CASHFLOW_5M = 25000.0
RELATIVE_SURGE_MIN_5M = 1500.0
RELATIVE_SURGE_RATIO_24H = 0.10


def load_json(path, default):
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f: return json.load(f)
    except Exception as exc: print(f"[V3] Could not read {path.name}: {exc}")
    return default


def save_json_atomic(path, data):
    atomic_save(path, data)


def load_required_json(path):
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def number(v, default=None):
    return finite_number(v, default)


def clamp(v,a,b): return max(a,min(b,v))
def sign(v):
    v=number(v,0) or 0
    return 1 if v>0 else -1 if v<0 else 0

def text(v): return "" if v is None else str(v).strip()

def flowwin(item,label):
    v=item.get(f"flow_{label}",{})
    return v if isinstance(v,dict) else {}

def first_num(obj,*names,default=None):
    for n in names:
        x=number(obj.get(n),None)
        if x is not None: return x
    return default

def conf(v):
    x=number(v,0) or 0
    if x>1: x/=100
    return clamp(x,0,1)

def market(item): return item.get("market",{}) if isinstance(item.get("market"),dict) else {}

def question(item): return text(market(item).get("question") or item.get("question"))
def outcome(item): return text(market(item).get("outcome") or item.get("outcome"))
def condition(item): return text(item.get("condition_id") or market(item).get("condition_id"))
def token(item): return text(market(item).get("token_id") or item.get("token_id"))

def key(item):
    return f"condition:{condition(item)}|outcome:{outcome(item).lower()}"


def recent_trades():
    return [row for row in tail_jsonl(LIVE_TRADES_FILE, MAX_LIVE_LINES)
            if number(row.get("collector_version"), 0) >= 3]


def same_market(item,row):
    if token(item) and row.get("token_id"):
        return token(item) == str(row["token_id"])
    ci=condition(item); cr=text(row.get("condition_id"))
    if ci and cr and ci==cr:
        oi=outcome(item).lower(); orow=text(row.get("outcome")).lower()
        return not oi or not orow or oi==orow
    ti=token(item); tr=text(row.get("token_id"))
    return bool(ti and tr and ti==tr)


def price_from(row):
    for k in ("fill_price","market_price","current_price","price"):
        x=number(row.get(k),None)
        if x is not None and 0<x<1: return x
    return None


def normalize(item, live):
    m=market(item); f1=flowwin(item,"1m"); f5=flowwin(item,"5m"); f15=flowwin(item,"15m")
    matches=[r for r in live if same_market(item,r)]
    latest=matches[-1] if matches else {}
    p=price_from(latest) or price_from(m) or price_from(item)
    prices=[price_from(r) for r in matches[-100:]]; prices=[x for x in prices if x is not None]
    ratio=first_num(f5,"largest_trade_ratio","single_trade_ratio","trade_to_volume_ratio",default=None)
    remaining = first_num(item, "remaining_seconds", default=None)
    end_age = age_seconds(m.get("end_date"))
    if end_age is not None:
        remaining = min(remaining, -end_age) if remaining is not None else -end_age
    return {
        "source_updated_at": item.get("source_updated_at") or item.get("updated_at"),
        "last_trade_at": latest.get("block_timestamp") or latest.get("detected_at") or item.get("last_trade_at") or m.get("last_trade_at"),
        "schema_version": item.get("schema_version"),
        "question": question(item), "outcome": outcome(item), "condition_id": condition(item), "token_id": token(item) or text(latest.get("token_id")),
        "price": p, "state": text(item.get("state") or "UNKNOWN").upper(), "verified": bool(item.get("verified",False)),
        "confirmations": int(first_num(item,"confirmations",default=0) or 0), "data_confidence": conf(item.get("data_confidence")),
        "resolution_state": text(item.get("resolution_state") or "UNKNOWN").upper(), "remaining_seconds": remaining,
        "end_date": m.get("end_date"), "active": latest.get("active",m.get("active")), "closed": latest.get("closed",m.get("closed")),
        "accepting_orders": latest.get("accepting_orders",m.get("accepting_orders")), "volume24hr": number(latest.get("volume24hr"), number(m.get("volume24hr"),None)),
        "trades_1m": first_num(f1,"trade_count","trades",default=0) or 0, "trades_5m": first_num(f5,"trade_count","trades",default=0) or 0,
        "trades_15m": first_num(f15,"trade_count","trades",default=0) or 0, "volume_1m": first_num(f1,"total_volume","volume",default=0) or 0,
        "volume_5m": first_num(f5,"total_volume","volume",default=0) or 0, "volume_15m": first_num(f15,"total_volume","volume",default=0) or 0,
        "net_1m": first_num(f1,"net_flow","net",default=0) or 0, "net_5m": first_num(f5,"net_flow","net",default=0) or 0,
        "net_15m": first_num(f15,"net_flow","net",default=0) or 0, "strength_1m": abs(first_num(f1,"directional_strength","strength",default=0) or 0),
        "strength_5m": abs(first_num(f5,"directional_strength","strength",default=0) or 0), "strength_15m": abs(first_num(f15,"directional_strength","strength",default=0) or 0),
        "largest_trade_5m": first_num(f5,"largest_trade","largest_trade_usd",default=0) or 0, "largest_trade_ratio": ratio,
        "recent_min_price": min(prices) if prices else None, "recent_max_price": max(prices) if prices else None,
    }


def signal_quality(m):
    reasons=[]; warnings=[]; d1,d5,d15=sign(m["net_1m"]),sign(m["net_5m"]),sign(m["net_15m"])
    if d5==0 or d15==0: persistence=5; warnings.append("5m/15m direction incomplete")
    elif d5!=d15: persistence=0; warnings.append("5m and 15m net flow contradict")
    elif d1==d5: persistence=25; reasons.append("1m, 5m and 15m flow aligned")
    elif d1==0: persistence=21; reasons.append("5m and 15m aligned; 1m neutral")
    else: persistence=14; warnings.append("1m currently opposes 5m/15m")
    strength=round(clamp((m["strength_5m"]-.20)/.45,0,1)*12.5 + clamp((m["strength_15m"]-.15)/.40,0,1)*12.5,1)
    t=m["trades_5m"]
    breadth=20 if t>=25 else 18 if t>=20 else 15 if t>=15 else 12 if t>=12 else 8 if t>=8 else 4 if t>=5 else 0
    v=m["volume_5m"]
    vol=15 if v>=25000 else 14 if v>=10000 else 13 if v>=5000 else 11 if v>=3000 else 9 if v>=1500 else 7 if v>=1000 else 5 if v>=500 else 2 if v>=250 else 0
    r=m["largest_trade_ratio"]
    if r is None: concentration=3; warnings.append("Largest-trade concentration unavailable")
    elif r<.25: concentration=15; reasons.append("Cashflow broadly distributed")
    elif r<.40: concentration=13; reasons.append("Single-trade concentration healthy")
    elif r<.55: concentration=9; warnings.append("Single-trade concentration moderate")
    elif r<.70: concentration=4; warnings.append("Single-trade concentration elevated")
    else: concentration=0; warnings.append("Flow dominated by one trade")
    if m["strength_5m"]>=.50: reasons.append(f"Strong 5m strength ({m['strength_5m']:.2f})")
    if m["strength_15m"]>=.35: reasons.append(f"Strong 15m strength ({m['strength_15m']:.2f})")
    if t>=12: reasons.append(f"Broad activity: {int(t)} trades / 5m")
    if v>=5000: reasons.append(f"Large cashflow: ${v:,.0f} / 5m")
    return {"score":round(persistence+strength+breadth+vol+concentration,1),"reasons":reasons,"warnings":warnings}


def verification_quality(m):
    reasons=[]; warnings=[]; c=m["confirmations"]
    cp=50 if c>=3 else 34 if c==2 else 17 if c==1 else 0
    if c>=3: reasons.append(f"{c} new-evidence confirmations passed")
    else: warnings.append(f"{c}/3 new-evidence confirmations")
    if m["verified"] or m["state"]=="VERIFIED": sp=20; reasons.append("Tracker state VERIFIED")
    elif m["state"]=="VERIFYING": sp=10; warnings.append("Tracker still VERIFYING")
    else: sp=0
    d1,d5,d15=sign(m["net_1m"]),sign(m["net_5m"]),sign(m["net_15m"])
    if d5!=0 and d5==d15 and d1==d5: pp=20; reasons.append("Direction persists across 1m/5m/15m")
    elif d5!=0 and d5==d15: pp=14; reasons.append("Direction persists across 5m/15m")
    else: pp=0; warnings.append("Directional persistence incomplete")
    dc=round(m["data_confidence"]*10,1)
    if m["data_confidence"]<MIN_DATA_CONFIDENCE: warnings.append(f"Low data confidence ({m['data_confidence']*100:.0f}%)")
    return {"score":round(cp+sp+pp+dc,1),"reasons":reasons,"warnings":warnings}


def max_upside(price):
    return ((1-price)/price)*100 if price is not None and 0<price<1 else None


def entry_quality(m):
    reasons=[]; warnings=[]; p=m["price"]; up=max_upside(p)
    if up is None: return {"score":0.0,"price":p,"max_resolution_upside_pct":None,"suggested_paper_take_profit_pct":None,"reasons":[],"warnings":["No usable current price"]}
    if up<3: score=5; warnings.append(f"Very little gross room to $1 ({up:.2f}%)")
    elif up<5: score=15; warnings.append(f"Very limited gross room to $1 ({up:.2f}%)")
    elif up<10: score=35; warnings.append(f"Limited gross room to $1 ({up:.1f}%)")
    elif up<20: score=60; reasons.append(f"Moderate gross room to $1 ({up:.1f}%)")
    elif up<50: score=80; reasons.append(f"Good gross room to $1 ({up:.1f}%)")
    else: score=90; reasons.append(f"Large gross room to $1 ({up:.1f}%)"); warnings.append("Large upside alone does not imply positive expected value")
    lo,hi=m["recent_min_price"],m["recent_max_price"]
    if lo is not None and hi is not None and lo>0:
        mv=(hi-lo)/lo*100
        if mv>=15: score-=20; warnings.append(f"Recent observed price range moved ~{mv:.1f}%")
        elif mv>=8: score-=10; warnings.append(f"Recent observed price range moved ~{mv:.1f}%")
        elif mv>=4: score-=5; warnings.append(f"Recent observed price range moved ~{mv:.1f}%")
    tp=round(max(1.0,min(15.0,up*.60)),2)
    return {"score":round(clamp(score,0,100),1),"price":round(p,6),"max_resolution_upside_pct":round(up,2),"suggested_paper_take_profit_pct":tp,"reasons":reasons,"warnings":warnings,"note":"Payoff-room metric only; not win probability or expected value."}


def derived_state(sec):
    if sec is None: return "UNKNOWN"
    if sec<=0: return "EXPIRED"
    if sec<=5*60: return "CRITICAL"
    if sec<=30*60: return "IMMINENT"
    if sec<=6*3600: return "CLOSING_SOON"
    if sec<=24*3600: return "ACTIVE"
    return "FAR"


def resolution_quality(m):
    reasons=[]; warnings=[]; stored=m["resolution_state"]; derived=derived_state(m["remaining_seconds"])
    if m["remaining_seconds"] is not None and m["remaining_seconds"] <= 0: score=0; warnings.append("Market end date has passed")
    elif m["active"] is not True or m["closed"] is not False or m["accepting_orders"] is not True: score=0; warnings.append("Market not confirmed active and accepting orders")
    elif m["remaining_seconds"] is None and stored=="UNKNOWN": score=25; warnings.append("Resolution/end timing unavailable")
    elif m["remaining_seconds"] is not None:
        # Same boundaries as flow_tracker: numeric and categorical fields are generated from the same end_date.
        score=75; reasons.append("End-date timing available")
        if stored==derived: score+=10; reasons.append("Stored timing matches numeric timing")
        else: warnings.append(f"Timing fields differ: stored={stored}, numeric={derived}")
        if m["active"] is True and m["closed"] is False: score+=5; reasons.append("Market metadata says active/open")
    else: score=60; warnings.append("Only categorical timing is available")
    return {"score":round(clamp(score,0,100),1),"stored_state":stored,"derived_timing":derived,"remaining_seconds":m["remaining_seconds"],"end_date":m["end_date"],"reasons":reasons,"warnings":warnings,"note":"Reliability reflects metadata completeness/consistency, not a guarantee of the written resolution rules."}


def cashflow(m):
    v=m["volume_5m"]; v24=m["volume24hr"]; rel=(v/v24) if v24 and v24>0 else None; tier=None
    if v>=EXTREME_CASHFLOW_5M: tier="EXTREME"
    elif v>=VERY_LARGE_CASHFLOW_5M: tier="VERY_LARGE"
    elif v>=LARGE_CASHFLOW_5M: tier="LARGE"
    elif rel is not None and v>=RELATIVE_SURGE_MIN_5M and rel>=RELATIVE_SURGE_RATIO_24H: tier="RELATIVE_SURGE"
    if not tier: return {"active":False,"tier":None,"quality":None}
    r=m["largest_trade_ratio"]
    quality="WHALE_DOMINATED" if r is not None and r>=.70 else "BROAD" if r is not None and r<.40 and m["trades_5m"]>=10 else "MIXED"
    return {"active":True,"tier":tier,"quality":quality,"volume_5m":round(v,2),"net_flow_5m":round(m["net_5m"],2),"trades_5m":int(m["trades_5m"]),"largest_trade_ratio":round(r,4) if r is not None else None,"relative_to_24h":round(rel,4) if rel is not None else None}


def gates(m):
    failed=[]
    if m["schema_version"] != 4: failed.append("Legacy mixed-outcome flow; waiting for token-specific data")
    if not fresh(m["source_updated_at"]): failed.append("Flow data stale or timestamp missing")
    if not fresh(m["last_trade_at"]): failed.append("Observed trade stale or timestamp missing")
    if m["remaining_seconds"] is None or m["remaining_seconds"] <= 0: failed.append("Market expired or end timing unknown")
    if m["active"] is not True or m["closed"] is not False or m["accepting_orders"] is not True: failed.append("Market not confirmed active and tradable")
    if not m["condition_id"]: failed.append("condition_id missing")
    if not m["token_id"]: failed.append("token_id missing")
    if not m["outcome"]: failed.append("Outcome missing")
    if m["price"] is None: failed.append("No usable current price")
    if m["trades_5m"]<MIN_TRADES_5M: failed.append(f"5m trades {int(m['trades_5m'])} < {MIN_TRADES_5M}")
    if m["volume_5m"]<MIN_VOLUME_5M: failed.append(f"5m volume ${m['volume_5m']:,.0f} < ${MIN_VOLUME_5M:,.0f}")
    if m["strength_5m"]<MIN_STRENGTH_5M: failed.append(f"5m strength {m['strength_5m']:.2f} < {MIN_STRENGTH_5M:.2f}")
    if m["strength_15m"]<MIN_STRENGTH_15M: failed.append(f"15m strength {m['strength_15m']:.2f} < {MIN_STRENGTH_15M:.2f}")
    if m["data_confidence"]<MIN_DATA_CONFIDENCE: failed.append(f"Data confidence {m['data_confidence']:.2f} < {MIN_DATA_CONFIDENCE:.2f}")
    r=m["largest_trade_ratio"]
    if r is None or r>=MAX_LARGEST_TRADE_RATIO: failed.append("Single-trade concentration too high/unknown")
    if sign(m["net_5m"])==0 or sign(m["net_5m"])!=sign(m["net_15m"]): failed.append("5m/15m direction missing or contradictory")
    return failed


def analyze(item, live):
    m=normalize(item,live); s=signal_quality(m); v=verification_quality(m); e=entry_quality(m); r=resolution_quality(m); c=cashflow(m); gf=gates(m)
    if not fresh(m["source_updated_at"]) or not fresh(m["last_trade_at"]) or m["schema_version"] != 4 or m["remaining_seconds"] is None or m["remaining_seconds"] <= 0 or m["active"] is not True or m["closed"] is not False or m["accepting_orders"] is not True:
        c["active"] = False
    diamond=(not gf and s["score"]>=MIN_SIGNAL_QUALITY and v["score"]>=MIN_VERIFICATION and e["score"]>=MIN_ENTRY_QUALITY and r["score"]>=MIN_RESOLUTION_RELIABILITY)
    if diamond: cls="DIAMOND"
    elif not fresh(m["source_updated_at"]) or not fresh(m["last_trade_at"]) or m["schema_version"] != 4: cls="DATA_RISK"
    elif s["score"]>=MIN_SIGNAL_QUALITY and v["score"]<MIN_VERIFICATION: cls="VERIFYING"
    elif s["score"]>=MIN_SIGNAL_QUALITY and e["score"]<MIN_ENTRY_QUALITY: cls="ENTRY_BLOCKED"
    elif s["score"]>=MIN_SIGNAL_QUALITY and r["score"]<MIN_RESOLUTION_RELIABILITY: cls="DATA_RISK"
    elif s["score"]>=65: cls="CANDIDATE"
    elif s["score"]>=50: cls="WATCH"
    else: cls="LOW"
    blockers=list(gf)
    for label,val,thr in [("Signal Quality",s["score"],MIN_SIGNAL_QUALITY),("Verification",v["score"],MIN_VERIFICATION),("Entry Quality",e["score"],MIN_ENTRY_QUALITY),("Resolution Reliability",r["score"],MIN_RESOLUTION_RELIABILITY)]:
        if val<thr: blockers.append(f"{label} {val:.1f} < {thr:.0f}")
    blockers=list(dict.fromkeys(blockers))
    return {"generated_at":datetime.now(timezone.utc).isoformat(),"source_updated_at":m["source_updated_at"],"last_trade_at":m["last_trade_at"],"schema_version":m["schema_version"],"market_key":key(item),"question":m["question"],"outcome":m["outcome"],"condition_id":m["condition_id"],"token_id":m["token_id"],"price":m["price"],"classification":cls,"diamond":diamond,"direction":"BUY" if m["net_5m"]>0 else "SELL" if m["net_5m"]<0 else "NEUTRAL","scores":{"signal_quality":s["score"],"verification":v["score"],"entry_quality":e["score"],"resolution_reliability":r["score"]},"signal":s,"verification":v,"entry":e,"resolution":r,"cashflow_alert":c,"why_not_diamond":blockers,"metrics":{"state":m["state"],"verified":m["verified"],"confirmations":m["confirmations"],"data_confidence":round(m["data_confidence"],4),"trades_1m":int(m["trades_1m"]),"trades_5m":int(m["trades_5m"]),"trades_15m":int(m["trades_15m"]),"volume_1m":round(m["volume_1m"],2),"volume_5m":round(m["volume_5m"],2),"volume_15m":round(m["volume_15m"],2),"net_1m":round(m["net_1m"],2),"net_5m":round(m["net_5m"],2),"net_15m":round(m["net_15m"],2),"strength_1m":round(m["strength_1m"],4),"strength_5m":round(m["strength_5m"],4),"strength_15m":round(m["strength_15m"],4),"largest_trade_5m":round(m["largest_trade_5m"],2),"largest_trade_ratio":round(m["largest_trade_ratio"],4) if m["largest_trade_ratio"] is not None else None}}


def run_once(verbose=True):
    flow=load_required_json(FLOW_FILE)
    rows=list(flow.values()) if isinstance(flow,dict) else []
    live=recent_trades(); results=[analyze(x,live) for x in rows if isinstance(x,dict)]
    results.sort(key=lambda x:(x["diamond"],x["cashflow_alert"].get("active",False),x["scores"]["signal_quality"],x["scores"]["verification"]),reverse=True)
    diamonds=[x for x in results if x["diamond"]]
    watch=[x for x in results if x["classification"] in {"DIAMOND","VERIFYING","ENTRY_BLOCKED","DATA_RISK","CANDIDATE"} or x["cashflow_alert"].get("active")]

    published_at=datetime.now(timezone.utc).isoformat()
    generation_id=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:12]
    generation_dir=GENERATIONS_DIR / generation_id
    generation_dir.mkdir(parents=True,exist_ok=False)

    save_json_atomic(generation_dir / "flow_state.json",flow)
    save_json_atomic(generation_dir / "diamond_analysis_v3.json",results)
    save_json_atomic(generation_dir / "diamond_candidates.json",watch)
    save_json_atomic(generation_dir / "diamonds.json",diamonds)

    save_json_atomic(ANALYSIS_FILE,results)
    save_json_atomic(CANDIDATES_FILE,watch)
    save_json_atomic(DIAMONDS_FILE,diamonds)

    manifest={
        "schema_version":1,
        "generation_id":generation_id,
        "published_at":published_at,
        "generation_dir":f"diamond_generations/{generation_id}",
        "analysis_file":"diamond_analysis_v3.json",
        "candidates_file":"diamond_candidates.json",
        "diamonds_file":"diamonds.json",
        "flow_file":"flow_state.json",
        "markets_analyzed":len(results),
        "candidates":len(watch),
        "diamonds":len(diamonds),
    }
    save_json_atomic(GENERATION_MANIFEST_FILE,manifest)
    if verbose:
        print("="*72); print("DIAMOND INTELLIGENCE - FILTER V3.1"); print("="*72)
        print(f"[V3] Markets analyzed: {len(results)} | Diamonds: {len(diamonds)} | Watchlist: {len(watch)}")
        for row in watch[:8]:
            sc=row["scores"]; print(f"{row['classification']} | {row['question']}")
            print(f"  SIGNAL {sc['signal_quality']:.0f} | VERIFY {sc['verification']:.0f} | ENTRY {sc['entry_quality']:.0f} | RESOLUTION {sc['resolution_reliability']:.0f}")
            if row["cashflow_alert"].get("active"): print(f"  CASHFLOW {row['cashflow_alert']['tier']} / {row['cashflow_alert']['quality']} | ${row['cashflow_alert']['volume_5m']:,.0f}/5m")
            if row["why_not_diamond"]: print("  Blockers: " + "; ".join(row["why_not_diamond"][:4]))
    return diamonds,watch,results


def self_test():
    base={"condition_id":"test","market":{"question":"SELF TEST","outcome":"Yes","token_id":"yes-token","price":.42,"end_date":"2027-01-01T00:00:00Z","active":True,"closed":False,"accepting_orders":True},"state":"VERIFIED","verified":True,"confirmations":3,"data_confidence":90,"resolution_state":"FAR","remaining_seconds":7*24*3600,"flow_1m":{"trade_count":8,"total_volume":1200,"net_flow":800,"directional_strength":.67,"largest_trade_ratio":.2},"flow_5m":{"trade_count":22,"total_volume":6000,"net_flow":4000,"directional_strength":.67,"largest_trade_ratio":.22,"largest_trade":1320},"flow_15m":{"trade_count":45,"total_volume":12000,"net_flow":8000,"directional_strength":.67,"largest_trade_ratio":.2}}
    base["schema_version"] = 4
    base["updated_at"] = datetime.now(timezone.utc).isoformat()
    base["market"]["last_trade_at"] = base["updated_at"]
    strong=analyze(base,[]); assert strong["classification"]=="DIAMOND", strong
    x=deepcopy(base); x["state"]="VERIFYING"; x["verified"]=False; x["confirmations"]=0; assert analyze(x,[])["classification"]=="VERIFYING"
    x=deepcopy(base); x["market"]["price"]=.9805; assert analyze(x,[])["classification"]=="ENTRY_BLOCKED"
    x=deepcopy(base); x["flow_5m"]["largest_trade_ratio"]=.85; y=analyze(x,[]); assert not y["diamond"] and y["cashflow_alert"]["quality"]=="WHALE_DOMINATED"
    # FAR + 67h is consistent with the flow_tracker (>24h => FAR), not a conflict.
    x=deepcopy(base); x["resolution_state"]="FAR"; x["remaining_seconds"]=int(67.7*3600); y=analyze(x,[]); assert y["resolution"]["derived_timing"]=="FAR" and y["resolution"]["score"]>=70
    print("SELF-TEST OK")
    print("Strong verified setup -> DIAMOND: OK")
    print("0 confirmations -> VERIFYING: OK")
    print("Near-$1 entry -> ENTRY_BLOCKED: OK")
    print("Whale cashflow -> not Diamond: OK")
    print("FAR + 67.7h uses flow_tracker timing consistently: OK")


def main():
    p=argparse.ArgumentParser(); p.add_argument("--self-test",action="store_true"); p.add_argument("--watch",action="store_true"); p.add_argument("--interval",type=int,default=10); a=p.parse_args()
    if a.self_test: self_test(); return
    if a.watch:
        while True:
            try: run_once(verbose=False)
            except Exception as exc: print(f"[V3 ERROR] {exc}")
            time.sleep(max(5,a.interval))
    else: run_once(verbose=True)

if __name__=="__main__": main()
