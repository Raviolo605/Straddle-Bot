import datetime
import pandas as pd
import streamlit as st
import requests
from datetime import date, timedelta, datetime as dt
import pytz

# ======================
# SAXO BANK API CONFIG
# ======================
SAXO_ACCESS_TOKEN = "your_saxo_api_token_here"
SAXO_BASE_URL = "https://gateway.saxobank.com/api/openapi"
ACCOUNT_KEY = "your_saxo_account_key_here"
TRADING_ECONOMICS_API_KEY = "your_trading_economics_api_key_here"

# ======================
# UTILS
# ======================

def get_this_week_friday():
    today = date.today()
    return today + timedelta((4 - today.weekday()) % 7)

def get_earnings_tickers():
    today = date.today()
    end = today + timedelta(days=7)
    url = (
        f"https://api.tradingeconomics.com/earnings"
        f"?c=United States&d1={today}&d2={end}&f=json&k={TRADING_ECONOMICS_API_KEY}"
    )
    try:
        res = requests.get(url)
        data = res.json()
        tickers = list(set([e["symbol"] for e in data if "symbol" in e and e["symbol"].isalpha()]))
        return tickers
    except:
        return []

def get_uic(ticker):
    headers = {"Authorization": f"Bearer {SAXO_ACCESS_TOKEN}"}
    res = requests.get(f"{SAXO_BASE_URL}/ref/v1/lookup", params={"Keyword": ticker}, headers=headers)
    items = res.json().get("Data", [])
    for item in items:
        if item["AssetType"] == "Stock" and item["Symbol"] == ticker:
            return item["Uic"]
    return None

def get_saxo_stock_price(uic):
    headers = {"Authorization": f"Bearer {SAXO_ACCESS_TOKEN}"}
    params = {"AssetType": "Stock", "Uic": uic}
    res = requests.get(f"{SAXO_BASE_URL}/trade/v1/infoprices", headers=headers, params=params)
    if res.status_code == 200:
        return res.json().get("Quote", {}).get("Price")
    return None

def find_weekly_atm_option(uic, price, option_type):
    headers = {"Authorization": f"Bearer {SAXO_ACCESS_TOKEN}"}
    expiry = get_this_week_friday().strftime("%Y-%m-%d")
    params = {
        "AssetType": "Option",
        "UnderlyingUic": uic,
        "OptionType": option_type,
        "StrikePriceNear": price,
        "ExpiryDate": expiry
    }
    res = requests.get(f"{SAXO_BASE_URL}/ref/v1/instruments", headers=headers, params=params)
    options = res.json().get("Data", [])
    return options[0] if options else None

def place_saxo_order(option_uic, action="Buy"):
    headers = {
        "Authorization": f"Bearer {SAXO_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    order = {
        "AccountKey": ACCOUNT_KEY,
        "Uic": option_uic,
        "AssetType": "Option",
        "Amount": 1,
        "BuySell": action,
        "OrderType": "Market",
        "OrderDuration": {"DurationType": "DayOrder"}
    }
    res = requests.post(f"{SAXO_BASE_URL}/trade/v2/orders", headers=headers, json=order)
    if res.status_code != 201:
        st.error(f"Order failed: {res.status_code} - {res.text}")
    return res.status_code == 201

def should_force_close():
    now = dt.now(pytz.timezone("Europe/Rome"))
    if now.weekday() == 4:  # Friday
        return now.hour == 20
    else:
        return now.hour == 21

def handle_directional_break(price, strike, total_cost, threshold=0.03):
    upper_breakeven = strike + total_cost
    lower_breakeven = strike - total_cost
    if price >= upper_breakeven * (1 + threshold):
        return "call_wins"
    elif price <= lower_breakeven * (1 - threshold):
        return "put_wins"
    return "none"

def handle_take_profit(current_total, entry_total):
    pct_gain = (current_total - entry_total) / entry_total
    return pct_gain >= 2.5

def handle_stop_loss(current_total, entry_total):
    pct_loss = (current_total - entry_total) / entry_total
    return pct_loss <= -0.3

# ======================
# STREAMLIT UI
# ======================
st.title("📈 Long Straddle Bot (SAXO)")

st.markdown("Fetching earnings-based tickers for this week...")
earnings_tickers = get_earnings_tickers()
straddles = []

for ticker in earnings_tickers:
    uic = get_uic(ticker)
    if not uic:
        continue

    price = get_saxo_stock_price(uic)
    if not price:
        continue

    call = find_weekly_atm_option(uic, price, "Call")
    put = find_weekly_atm_option(uic, price, "Put")

    if call and put:
        call_price = call.get("LastTraded", {}).get("Price")
        put_price = put.get("LastTraded", {}).get("Price")

        if call_price is None or put_price is None:
            continue

        total_cost = call_price + put_price
        strike = call["StrikePrice"]

        direction = handle_directional_break(price, strike, total_cost)
        if direction == "call_wins":
            place_saxo_order(put["Uic"], action="Sell")
            st.info(f"{ticker}: breakout detected above breakeven. Put leg closed.")
        elif direction == "put_wins":
            place_saxo_order(call["Uic"], action="Sell")
            st.info(f"{ticker}: breakout detected below breakeven. Call leg closed.")

        current_total = call_price + put_price

        if handle_stop_loss(current_total, total_cost):
            place_saxo_order(call["Uic"], action="Sell")
            place_saxo_order(put["Uic"], action="Sell")
            st.warning(f"{ticker}: -30% stop loss hit. Both legs closed.")
            continue

        if handle_take_profit(current_total, total_cost):
            place_saxo_order(call["Uic"], action="Sell")
            place_saxo_order(put["Uic"], action="Sell")
            st.success(f"{ticker}: +250% take profit hit. Both legs closed.")

        straddles.append({
            "Ticker": ticker,
            "Price": price,
            "Strike": strike,
            "Call Premium": call_price,
            "Put Premium": put_price,
            "Total Cost": total_cost,
            "Expiry": call["ExpiryDate"],
            "Call Uic": call["Uic"],
            "Put Uic": put["Uic"]
        })

if should_force_close():
    st.warning("⏰ Forced closing time reached. Manual position closing is recommended.")

if straddles:
    df = pd.DataFrame(straddles)
    st.dataframe(df)
    selected = st.selectbox("Select ticker:", df["Ticker"])
    if st.button("BUY STRADDLE"):
        row = df[df["Ticker"] == selected].iloc[0]
        call_uic = row["Call Uic"]
        put_uic = row["Put Uic"]
        success_call = place_saxo_order(call_uic, "Buy")
        success_put = place_saxo_order(put_uic, "Buy")
        if success_call and success_put:
            st.success("Straddle executed!")
        else:
            st.error("Order failed")
else:
    st.warning("No earnings-based straddles available this week.")

st.caption(f"Updated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

