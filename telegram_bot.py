import asyncio
import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from machine_common import fresh, age_seconds, tail_jsonl, finite_number, save_json_atomic

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

BASE=Path(__file__).resolve().parent
DATA=BASE/"data"
ANALYSIS=DATA/"diamond_analysis_v3.json"
DIAMONDS=DATA/"diamonds.json"
FLOW=DATA/"flow_state.json"
TRADES=DATA/"live_trades.jsonl"
PAPER=DATA/"paper_trades.json"
STATE=DATA/"telegram_state.json"
TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
ENV_CHAT=os.getenv("TELEGRAM_CHAT_ID","").strip()
PRICE_INTERVAL=int(os.getenv("PAPER_PRICE_INTERVAL","5"))
MAX_HOLD=float(os.getenv("PAPER_MAX_HOLD_MINUTES","30"))
STOP_LOSS=float(os.getenv("PAPER_STOP_LOSS_PCT","-10"))
MAX_POSITIONS=int(os.getenv("PAPER_MAX_OPEN_POSITIONS","5"))
MAX_EXPOSURE=float(os.getenv("PAPER_MAX_EXPOSURE_USD","300"))


def load(path,default):
    try:
        with path.open("r",encoding="utf-8") as f: return json.load(f)
    except Exception: return default

def save(path,data):
    save_json_atomic(path, data)

def num(v,d=0):
    return finite_number(v, d)

def authorized(update):
    return bool(ENV_CHAT and update.effective_chat and str(update.effective_chat.id) == ENV_CHAT)

def sid(row): return hashlib.sha1(str(row.get("market_key","")).encode()).hexdigest()[:12]
def analyses():
    x=load(ANALYSIS,[]); return x if isinstance(x,list) else []
def diamonds():
    x=load(DIAMONDS,[])
    return [r for r in x if fresh(r.get("source_updated_at")) and fresh(r.get("last_trade_at"))] if isinstance(x,list) else []
def state():
    x=load(STATE,{"chat_ids":[],"active_diamonds":[],"cashflow_levels":{}}); return x if isinstance(x,dict) else {"chat_ids":[],"active_diamonds":[],"cashflow_levels":{}}
def paper():
    x=load(PAPER,[]); return x if isinstance(x,list) else []
def find_row(short):
    for r in analyses():
        if sid(r)==short: return r
    return None

def money(v): return f"${num(v):,.2f}"
def pct(v): return f"{num(v):.1f}%"

def chat_ids():
    return {ENV_CHAT} if ENV_CHAT else set()

def register_chat(cid):
    if str(cid) != ENV_CHAT:
        return
    s=state(); s["chat_ids"]=[ENV_CHAT]; save(STATE,s)

def scoreline(r):
    s=r.get("scores",{})
    return f"Signal {num(s.get('signal_quality')):.0f} | Verify {num(s.get('verification')):.0f} | Entry {num(s.get('entry_quality')):.0f} | Resolution {num(s.get('resolution_reliability')):.0f}"

def short_message(r):
    cash=r.get("cashflow_alert",{})
    lines=[f"{r.get('classification','?')} | {r.get('question','Unknown')}",f"Outcome: {r.get('outcome','?')} @ {num(r.get('price')):.4f}",f"Flow direction: {r.get('direction','UNKNOWN')}",scoreline(r)]
    if cash.get("active"): lines.append(f"💰 {cash.get('tier')} {cash.get('quality')} | {money(cash.get('volume_5m'))}/5m")
    return "\n".join(lines)

def analysis_message(r):
    parts=["🔎 MARKET ANALYSIS","",r.get("question","Unknown"),f"Outcome: {r.get('outcome','?')} @ {num(r.get('price')):.4f}",f"Status: {r.get('classification','?')}",scoreline(r)]
    e=r.get("entry",{}); res=r.get("resolution",{}); m=r.get("metrics",{})
    parts += ["",f"5m: {m.get('trades_5m',0)} trades | {money(m.get('volume_5m'))} | net {money(m.get('net_5m'))} | strength {num(m.get('strength_5m')):.2f}",f"15m: net {money(m.get('net_15m'))} | strength {num(m.get('strength_15m')):.2f}",f"Confirmations: {m.get('confirmations',0)}/3",f"Max gross room to $1: {e.get('max_resolution_upside_pct','?')}%",f"Timing: {res.get('derived_timing','UNKNOWN')} | end: {res.get('end_date') or 'unknown'}"]
    good=(r.get("signal",{}).get("reasons",[])+r.get("verification",{}).get("reasons",[])+r.get("entry",{}).get("reasons",[]))[:6]
    if good: parts += ["","WHY IT IS INTERESTING"]+[f"✅ {x}" for x in good]
    blocks=r.get("why_not_diamond",[])[:6]
    if blocks: parts += ["","WHY NOT DIAMOND"]+[f"⚠️ {x}" for x in blocks]
    parts += ["","Paper simulation only. Entry-quality score is not win probability."]
    return "\n".join(parts)

def keyboard(r):
    s=sid(r)
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔎 Analyze",callback_data=f"a:{s}")],[InlineKeyboardButton("$25 PAPER",callback_data=f"p:{s}:25"),InlineKeyboardButton("$50 PAPER",callback_data=f"p:{s}:50"),InlineKeyboardButton("$100 PAPER",callback_data=f"p:{s}:100")]])

def recent_live():
    return [r for r in tail_jsonl(TRADES) if num(r.get("collector_version")) >= 3]

def same_trade_market(pos,row):
    if pos.get("token_id") and row.get("token_id") and str(pos["token_id"])==str(row["token_id"]): return True
    if pos.get("condition_id") and row.get("condition_id") and str(pos["condition_id"])==str(row["condition_id"]):
        return str(pos.get("outcome","")).lower()==str(row.get("outcome","")).lower()
    return False

def open_paper(r,amount,chat_id):
    if str(chat_id) != ENV_CHAT: return None,"Chat not authorized"
    if amount not in (25, 50, 100): return None,"Invalid paper amount"
    if r.get("direction") != "BUY": return None,"Paper entry requires BUY flow for this outcome"
    if r.get("schema_version") != 4 or not fresh(r.get("source_updated_at")) or not fresh(r.get("last_trade_at")): return None,"Market data stale; wait for a fresh analysis"
    resolution = r.get("resolution", {})
    end_age = age_seconds(resolution.get("end_date"))
    if end_age is None or end_age >= 0: return None,"Market end date passed or unavailable"
    if num(resolution.get("remaining_seconds")) <= 0 or num(resolution.get("score")) < 70: return None,"Market timing/tradability not confirmed"
    rows=paper(); opens=[x for x in rows if x.get("status")=="OPEN"]
    if len(opens)>=MAX_POSITIONS: return None,"Max open paper positions reached"
    if sum(num(x.get("investment_usd")) for x in opens)+amount>MAX_EXPOSURE: return None,"Paper exposure limit reached"
    if any(x.get("market_key")==r.get("market_key") and x.get("status")=="OPEN" for x in rows): return None,"This market already has an open paper position"
    price=num(r.get("price"),0)
    if not (0<price<1): return None,"No usable observed price"
    tp=num(r.get("entry",{}).get("suggested_paper_take_profit_pct"),15)
    trade={"trade_id":"PAPER-"+uuid.uuid4().hex[:16],"market_key":r.get("market_key"),"question":r.get("question"),"outcome":r.get("outcome"),"condition_id":r.get("condition_id"),"token_id":r.get("token_id"),"classification_at_entry":r.get("classification"),"scores_at_entry":r.get("scores",{}),"status":"OPEN","opened_at":datetime.now(timezone.utc).isoformat(),"entry_price":price,"current_price":price,"investment_usd":amount,"shares":amount/price,"current_value_usd":amount,"pnl_usd":0.0,"return_pct":0.0,"take_profit_pct":tp,"stop_loss_pct":STOP_LOSS,"max_hold_minutes":MAX_HOLD,"chat_id":str(chat_id),"price_source":"last observed taker fill"}
    rows.append(trade); save(PAPER,rows); return trade,None

def update_paper():
    rows=paper(); live=recent_live(); closed=[]; changed=False; now=datetime.now(timezone.utc)
    for t in rows:
        if t.get("status")!="OPEN": continue
        matches=[x for x in live if same_trade_market(t,x)]
        if matches:
            p=num(matches[-1].get("fill_price"),0)
            if 0<p<1 and fresh(matches[-1].get("block_timestamp") or matches[-1].get("detected_at")):
                t["last_price_at"] = matches[-1].get("block_timestamp") or matches[-1].get("detected_at")
                t.pop("exit_pending", None)
                t["current_price"]=p; t["current_value_usd"]=round(num(t.get("shares"))*p,2); t["pnl_usd"]=round(t["current_value_usd"]-num(t.get("investment_usd")),2); t["return_pct"]=round(t["pnl_usd"]/num(t.get("investment_usd"))*100,2); changed=True
        try: opened=datetime.fromisoformat(str(t.get("opened_at")).replace("Z","+00:00")); held=(now-opened).total_seconds()/60
        except Exception: held=0
        ret=num(t.get("return_pct")); reason=None
        if ret>=num(t.get("take_profit_pct"),15): reason="TAKE_PROFIT"
        elif ret<=num(t.get("stop_loss_pct"),-10): reason="STOP_LOSS"
        elif held>=num(t.get("max_hold_minutes"),30): reason="TIME_EXIT"
        if reason and not fresh(t.get("last_price_at")):
            t["exit_pending"] = "WAITING_FOR_FRESH_PRICE"
            changed = True
            continue
        if reason:
            t.update({"status":"CLOSED","closed_at":now.isoformat(),"exit_price":t.get("current_price"),"final_value_usd":t.get("current_value_usd"),"realized_pnl_usd":t.get("pnl_usd"),"realized_return_pct":t.get("return_pct"),"exit_reason":reason}); closed.append(dict(t)); changed=True
    if changed: save(PAPER,rows)
    return closed

async def start_cmd(update:Update,context:ContextTypes.DEFAULT_TYPE):
    if not authorized(update): return
    register_chat(update.effective_chat.id); await update.message.reply_text("💎 Diamond Intelligence V3 online.\n\n/diamonds /markets /positions /status")
async def status_cmd(update,context):
    if not authorized(update): return
    register_chat(update.effective_chat.id); a=analyses(); d=diamonds(); opens=[x for x in paper() if x.get("status")=="OPEN"]; await update.message.reply_text(f"🟢 System online\nMarkets analyzed: {len(a)}\nDiamonds: {len(d)}\nOpen paper trades: {len(opens)}")
async def diamonds_cmd(update,context):
    if not authorized(update): return
    register_chat(update.effective_chat.id); ds=diamonds()
    if not ds: await update.message.reply_text("No verified Diamonds right now."); return
    for r in ds[:5]: await update.message.reply_text(short_message(r),reply_markup=keyboard(r))
async def markets_cmd(update,context):
    if not authorized(update): return
    register_chat(update.effective_chat.id); rows=analyses(); rows=[r for r in rows if r.get("classification")!="LOW" or r.get("cashflow_alert",{}).get("active")][:6]
    if not rows: await update.message.reply_text("No interesting markets right now."); return
    for r in rows: await update.message.reply_text(short_message(r),reply_markup=keyboard(r))
async def positions_cmd(update,context):
    if not authorized(update): return
    register_chat(update.effective_chat.id); rows=[x for x in paper() if x.get("status")=="OPEN"]
    if not rows: await update.message.reply_text("No open paper trades."); return
    for t in rows: await update.message.reply_text(f"👁 {t.get('question')}\n{t.get('outcome')} | entry {num(t.get('entry_price')):.4f} | current {num(t.get('current_price')):.4f}\nP/L {money(t.get('pnl_usd'))} ({pct(t.get('return_pct'))})")
async def button(update,context):
    if not authorized(update): return
    q=update.callback_query; await q.answer(); data=q.data or ""
    if data.startswith("a:"):
        r=find_row(data.split(":",1)[1]); await q.message.reply_text(analysis_message(r) if r else "Market no longer available.",reply_markup=keyboard(r) if r else None)
    elif data.startswith("p:"):
        _,s,amt=data.split(":",2); r=find_row(s)
        if not r: await q.message.reply_text("Market no longer available."); return
        t,err=open_paper(r,float(amt),q.message.chat.id)
        if err: await q.message.reply_text("❌ "+err); return
        await q.message.reply_text(f"🟢 PAPER TRADE OPENED\n\n{t['question']}\n{t['outcome']} @ {t['entry_price']:.4f}\nInvestment: {money(t['investment_usd'])}\nTP: +{t['take_profit_pct']:.2f}% | SL: {t['stop_loss_pct']:.1f}% | max {t['max_hold_minutes']:.0f}m\n\nPrice source: last observed fill; PAPER only.")

async def watcher(app):
    # Baseline current Diamonds on startup to prevent spam.
    s=state(); active=set(s.get("active_diamonds",[])) or set(r.get("market_key") for r in diamonds())
    cash_levels=s.get("cashflow_levels",{}) if isinstance(s.get("cashflow_levels",{}),dict) else {}
    rank={None:0,"LARGE":1,"RELATIVE_SURGE":1,"VERY_LARGE":2,"EXTREME":3}
    while True:
        try:
            ds=[r for r in diamonds() if fresh(r.get("source_updated_at")) and fresh(r.get("last_trade_at"))]; current=set(r.get("market_key") for r in ds)
            for r in ds:
                k=r.get("market_key")
                if k not in active:
                    for cid in chat_ids():
                        try: await app.bot.send_message(chat_id=cid,text="💎 NEW VERIFIED DIAMOND\n\n"+short_message(r)+"\n\nUse /diamonds or /markets for details.")
                        except Exception as exc: print("[TELEGRAM]",exc)
            active=current
            # Only VERY_LARGE/EXTREME non-whale flow creates automatic cashflow alerts.
            current_cash={}
            for r in analyses():
                c=r.get("cashflow_alert",{}); tier=c.get("tier"); k=r.get("market_key")
                if fresh(r.get("source_updated_at")) and fresh(r.get("last_trade_at")) and c.get("active") and rank.get(tier,0)>=2 and c.get("quality")!="WHALE_DOMINATED" and num(r.get("scores",{}).get("signal_quality"))>=65:
                    current_cash[k]=tier
                    if rank.get(tier,0)>rank.get(cash_levels.get(k),0):
                        for cid in chat_ids():
                            try: await app.bot.send_message(chat_id=cid,text=f"💰 {tier} CASHFLOW WATCH\n\n"+short_message(r)+"\n\nNot automatically a Diamond.")
                            except Exception as exc: print("[TELEGRAM]",exc)
            cash_levels=current_cash
            for t in update_paper():
                cid=t.get("chat_id") or ENV_CHAT
                if cid and str(cid) == ENV_CHAT:
                    try: await app.bot.send_message(chat_id=cid,text=f"🏁 PAPER TRADE CLOSED\n\n{t.get('question')}\n{t.get('outcome')}\nExit: {t.get('exit_reason')}\nEntry {num(t.get('entry_price')):.4f} → Exit {num(t.get('exit_price')):.4f}\nP/L {money(t.get('realized_pnl_usd'))} ({pct(t.get('realized_return_pct'))})")
                    except Exception as exc: print("[TELEGRAM]",exc)
            s=state(); s["active_diamonds"]=sorted(x for x in active if x); s["cashflow_levels"]=cash_levels; s["chat_ids"]=sorted(chat_ids()); save(STATE,s)
        except Exception as exc: print("[TELEGRAM WATCHER]",repr(exc))
        await asyncio.sleep(max(3,PRICE_INTERVAL))

async def post_init(app): app.create_task(watcher(app))

def main():
    if not TOKEN: raise SystemExit("Missing TELEGRAM_BOT_TOKEN")
    if not ENV_CHAT: raise SystemExit("Missing TELEGRAM_CHAT_ID: required for private bot access")
    app=Application.builder().token(TOKEN).post_init(post_init).build()
    for name,fn in [("start",start_cmd),("status",status_cmd),("diamonds",diamonds_cmd),("markets",markets_cmd),("positions",positions_cmd)]: app.add_handler(CommandHandler(name,fn))
    app.add_handler(CallbackQueryHandler(button))
    print("[TELEGRAM] Interactive Diamond V3 bot starting...")
    app.run_polling(drop_pending_updates=True)

if __name__=="__main__": main()
