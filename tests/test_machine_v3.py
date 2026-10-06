import importlib.util
import os
from copy import deepcopy
from pathlib import Path

BASE=Path(__file__).resolve().parents[1]

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

# Collector import requires RPC env but does not network-connect because main is guarded.
os.environ.setdefault("POLYMARKET_RPC_URL","http://127.0.0.1:1")
collector=load("collector",BASE/"scripts"/"live_active_trades.py")
flow=load("flow",BASE/"scripts"/"flow_tracker.py")
diamond=load("diamond",BASE/"scripts"/"diamond_filter_v3.py")

buy={"maker_amount":25_000_000,"taker_amount":50_000_000,"side":0}
m=collector.calculate_trade_metrics(buy)
assert m["trade_usd"]==25 and m["token_amount"]==50 and abs(m["fill_price"]-.5)<1e-9
sell={"maker_amount":50_000_000,"taker_amount":25_000_000,"side":1}
m=collector.calculate_trade_metrics(sell)
assert m["trade_usd"]==25 and m["token_amount"]==50 and abs(m["fill_price"]-.5)<1e-9
print("PASS: V2 BUY/SELL USD/share normalization")

# Confirmations require new evidence.
flow.market_states={}
a=flow.update_market_state("cid",True,True,"trade-A")
assert a["confirmations"]==1
a=flow.update_market_state("cid",True,True,"trade-A")
assert a["confirmations"]==1
a=flow.update_market_state("cid",True,True,"trade-B")
assert a["confirmations"]==2
a=flow.update_market_state("cid",True,True,"trade-C")
assert a["confirmations"]==3 and a["state"]=="VERIFIED"
print("PASS: repeated unchanged evidence does not inflate confirmations")

diamond.self_test()
print("PASS: Diamond V3.1 logic")
print("ALL TESTS PASSED")
