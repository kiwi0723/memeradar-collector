import requests, json, time, subprocess
from collections import defaultdict

# 1. Fetch data from gmgn-cli
print("【1】Fetching GMGN data (SOL)...")
sm = subprocess.run(['gmgn-cli','track','smartmoney','--chain','sol','--side','buy','--limit','100','--raw'],
    capture_output=True, text=True, timeout=15)
kol = subprocess.run(['gmgn-cli','track','kol','--chain','sol','--side','buy','--limit','100','--raw'],
    capture_output=True, text=True, timeout=15)

sm_trades = json.loads(sm.stdout).get('list',[])
kol_trades = json.loads(kol.stdout).get('list',[])
print(f"SM buys: {len(sm_trades)}, KOL buys: {len(kol_trades)}")

# 2. Cluster
groups = defaultdict(lambda: {'wallets': set(), 'total_usd': 0, 'symbol': '?', 'tags': set(), 'sm': 0, 'kol': 0})
for t in sm_trades:
    addr = t['base_address']
    g = groups[addr]
    g['wallets'].add(t['maker'])
    g['total_usd'] += float(t.get('amount_usd', 0))
    g['symbol'] = t.get('base_token',{}).get('symbol','?')
    g['tags'].update(t.get('maker_info',{}).get('tags',[]))
    g['sm'] += 1
for t in kol_trades:
    addr = t['base_address']
    g = groups[addr]
    g['wallets'].add(t['maker'])
    g['total_usd'] += float(t.get('amount_usd', 0))
    g['symbol'] = t.get('base_token',{}).get('symbol','?')
    g['tags'].update(t.get('maker_info',{}).get('tags',[]))
    g['kol'] += 1

signals = [(a,g) for a,g in groups.items() if len(g['wallets']) >= 3 and g['total_usd'] >= 100]
signals.sort(key=lambda x: len(x[1]['wallets']), reverse=True)
print(f"Signals: {len(signals)}")

if not signals:
    print("No signals found")
    exit()

addr, sig = signals[0]
print(f"\n【2】Testing: {sig['symbol']} | {len(sig['wallets'])}w (SM:{sig['sm']} KOL:{sig['kol']}) | ${sig['total_usd']:,.0f}")

prompt = f"""Quick SM cluster — classify & push if ★★★:

Token: {sig['symbol']} 🟣SOL
CA: {addr}
Buyers: {len(sig['wallets'])} wallets (SM:{sig['sm']} KOL:{sig['kol']})
Buy: ${sig['total_usd']:,.0f}
Tags: {', '.join(sorted(sig['tags'])[:5])}

Steps (max 3 tool calls):
1. mcp_opennews_search_news(keyword="{sig['symbol']}") — check for news coverage.
2. mcp_twitter_search_twitter(keywords="{sig['symbol']}") — check if Musk/Trump/CZ tweeting about it.
3. If NEWS or TWEETS connect to Musk/Trump/CZ/Binance → ★★★ → push TG via send_message.
4. If nothing → SKIP immediately.

Rules:
- Use 6551 MCP tools (opennews + twitter) for narrative verification.
- Respond ONE line: PUSHED: symbol — narrative | SKIP: symbol — reason (≤10 words)."""

t0 = time.time()
r = requests.post('http://127.0.0.1:8642/v1/chat/completions',
    json={'model':'hermes-agent','messages':[{'role':'user','content':prompt}],'max_tokens':100},
    timeout=60)
elapsed = time.time() - t0
resp = r.json()['choices'][0]['message']['content']
print(f"\n【3】Hermes [{elapsed:.1f}s]: {resp}")
