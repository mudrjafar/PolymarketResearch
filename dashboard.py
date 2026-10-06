import json
from datetime import datetime
from pathlib import Path
import streamlit as st
from machine_common import fresh

BASE=Path(__file__).resolve().parent; DATA=BASE/"data"
def load(name,default):
    try:
        with (DATA/name).open("r",encoding="utf-8") as f:return json.load(f)
    except Exception:return default
def num(v,d=0):
    try:return float(v)
    except Exception:return d

a=load("diamond_analysis_v3.json",[]); a=a if isinstance(a,list) else []
stale_count=sum(not fresh(x.get("source_updated_at")) or not fresh(x.get("last_trade_at")) for x in a)
a=[x for x in a if fresh(x.get("source_updated_at")) and fresh(x.get("last_trade_at"))]
d=[x for x in a if x.get("diamond")]; p=load("paper_trades.json",[]); p=p if isinstance(p,list) else []
openp=[x for x in p if x.get("status")=="OPEN"]; closed=[x for x in p if x.get("status")=="CLOSED"]
cash=[x for x in a if x.get("cashflow_alert",{}).get("active")]

st.set_page_config(page_title="Diamond Intelligence",layout="wide")
st.title("💎 Diamond Intelligence V3")
st.caption("Live flow → verification → entry quality → resolution reliability · PAPER only")
if stale_count: st.warning(f"{stale_count} analyses hidden because source or price data is stale. Refresh after new trades arrive.")
if st.button("🔄 Refresh"): st.rerun()
cols=st.columns(6)
cols[0].metric("Markets",len(a)); cols[1].metric("Diamonds",len(d)); cols[2].metric("Cashflow alerts",len(cash)); cols[3].metric("Open paper",len(openp)); cols[4].metric("Open P/L",f"${sum(num(x.get('pnl_usd')) for x in openp):+.2f}"); cols[5].metric("Realized P/L",f"${sum(num(x.get('realized_pnl_usd')) for x in closed):+.2f}")

t1,t2,t3,t4=st.tabs(["Market Scanner","Diamonds","Paper Trades","Details"])
with t1:
    rows=[]
    for r in a:
        if r.get("classification")!="LOW" or r.get("cashflow_alert",{}).get("active"):
            s=r.get("scores",{}); c=r.get("cashflow_alert",{})
            rows.append({"Market":r.get("question"),"Outcome":r.get("outcome"),"Price":r.get("price"),"Class":r.get("classification"),"Signal":num(s.get("signal_quality")),"Verify":num(s.get("verification")),"Entry":num(s.get("entry_quality")),"Resolution":num(s.get("resolution_reliability")),"Cashflow":c.get("tier") or "-","5m $":num(r.get("metrics",{}).get("volume_5m")),"5m strength":num(r.get("metrics",{}).get("strength_5m"))})
    st.dataframe(rows,width="stretch",hide_index=True) if rows else st.info("No interesting markets right now.")
with t2:
    rows=[{"Market":r.get("question"),"Outcome":r.get("outcome"),"Price":r.get("price"),**r.get("scores",{})} for r in d]
    st.dataframe(rows,width="stretch",hide_index=True) if rows else st.info("No verified Diamonds right now.")
with t3:
    rows=[]
    for x in reversed(p): rows.append({"Market":x.get("question"),"Status":x.get("status"),"Position":x.get("outcome"),"Investment":num(x.get("investment_usd")),"Entry":num(x.get("entry_price")),"Current/Exit":num(x.get("exit_price"),num(x.get("current_price"))),"P/L":num(x.get("realized_pnl_usd"),num(x.get("pnl_usd"))),"Return %":num(x.get("realized_return_pct"),num(x.get("return_pct"))),"Exit":x.get("exit_reason","-")})
    st.dataframe(rows,width="stretch",hide_index=True) if rows else st.info("No paper trades yet. Use Telegram /markets.")
with t4:
    if not a: st.info("No analysis yet.")
    else:
        labels=[f"{r.get('classification')} | {r.get('question')}" for r in a]; i=st.selectbox("Market",range(len(labels)),format_func=lambda x:labels[x]); r=a[i]
        st.write("**Scores:**",r.get("scores")); st.write("**Signal reasons:**",r.get("signal",{}).get("reasons",[])); st.write("**Signal warnings:**",r.get("signal",{}).get("warnings",[])); st.write("**Verification:**",r.get("verification")); st.write("**Entry:**",r.get("entry")); st.write("**Resolution:**",r.get("resolution")); st.write("**Why not Diamond:**",r.get("why_not_diamond",[]))
st.caption("Last page refresh: "+datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
