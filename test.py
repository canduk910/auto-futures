# diag_positions_eth.py
from binance.client import Client
import os, json, time
from binance.enums import *

# .env 파일에서 환경변수 로드
from dotenv import load_dotenv
load_dotenv()

api_key = os.getenv("BINANCE_TESTNET_API_KEY")
api_secret = os.getenv("BINANCE_TESTNET_SECRET_KEY")

# 테스트넷/라이브 선택
ENV = os.getenv("ENV", "paper")  # paper | live
client = Client(api_key=api_key, api_secret=api_secret, testnet=(ENV=="paper"))
if ENV == "paper":
    client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"

sym = "ETHUSDT"

print("FUTURES_URL:", client.FUTURES_URL)
print("Hedge Mode?:", client.futures_get_position_mode())

# 1) 원시 포지션(USDT-M)
raw_pos = client.futures_position_information(symbol=sym)
print("\n--- raw futures_position_information(ETHUSDT) ---")
print(json.dumps(raw_pos, indent=2))

# 2) 계정 뷰(UPNL·지갑잔고 비교)
acc = client.futures_account()
print("\n--- futures_account (snippet) ---")
print(json.dumps({
    "assets": [a for a in acc.get("assets", []) if a.get("asset")=="USDT"],
    "positions(ETHUSDT)": [p for p in acc.get("positions", []) if p.get("symbol")==sym]
}, indent=2))

# 3) 마크프라이스로 수동 UPNL 산출 테스트
mp = client.futures_mark_price(symbol=sym)
mark = float(mp["markPrice"])
print("\nmarkPrice:", mark)

for p in raw_pos:
    if p.get("symbol") != sym: 
        continue
    qty = float(p["positionAmt"])
    if qty == 0: 
        continue
    entry = float(p["entryPrice"])
    side = p.get("positionSide","BOTH")
    # 수동 UPNL (USDT-M): Long (mark-entry)*qty / Short (entry-mark)*qty
    upnl_calc = (mark-entry)*qty if qty>0 else (entry-mark)*abs(qty)
    print(f"\n[calc] side={side}, qty={qty}, entry={entry}, upnl_calc={upnl_calc}")
    print("  unRealizedProfit(raw):", p.get("unRealizedProfit"))
    print("  liquidationPrice(raw):", p.get("liquidationPrice"))


from binance_conn import fetch_account_and_positions, create_binance_client
from common_utils import safe_float

client = create_binance_client(env="paper")
acct = fetch_account_and_positions(client, symbol_filter="ETHUSDT")
print(acct["open_positions"])

print("MARKET =", FUTURE_ORDER_TYPE_MARKET)
print("LIMIT  =", FUTURE_ORDER_TYPE_LIMIT)
print("STOP_M =", FUTURE_ORDER_TYPE_STOP_MARKET)
print("TP_M   =", FUTURE_ORDER_TYPE_TAKE_PROFIT_MARKET)


import time, socket, ssl, urllib.request

def _tt(msg, t0): print(msg, round(time.monotonic()-t0,3), "s")

# 1) DNS
t=time.monotonic()
socket.getaddrinfo("fstream.binance.com", 443); _tt("DNS mainnet", t)
socket.getaddrinfo("stream.binancefuture.com", 443); _tt("DNS testnet", t)

# 2) TLS 핸드셰이크
def tls_head(host):
    t0=time.monotonic()
    ctx=ssl.create_default_context()
    s=socket.create_connection((host,443), timeout=5)
    ss=ctx.wrap_socket(s, server_hostname=host)
    ss.send(b"HEAD / HTTP/1.1\r\nHost: "+host.encode()+b"\r\nConnection: close\r\n\r\n")
    ss.recv(1024)
    ss.close()
    _tt("TLS "+host, t0)

tls_head("fstream.binance.com")
tls_head("stream.binancefuture.com")