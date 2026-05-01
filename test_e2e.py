#!/usr/bin/env python3
"""End-to-end test for collector → Hermes pipeline"""
import subprocess, json, time, requests
from collections import defaultdict

# Step 1: Fetch trades
print('【1】Fetching trades...')
r = subprocess.run(['onchainos','tracker','activities','--tracker-type','smart_money',
    '--chain','solana','--trade-type','1'],
    capture_output=True, text=True, timeout=15)
data = json.loads(r.stdout)
trades = data['data']['trades']
print(f'Got {len(trades)} trades from {len(set(t["tokenContractAddress"] for t in trades))} tokens')

# Step 2: Group and find signals
groups = defaultdict(lambda: {'wallets': set(), 'total_sol': 0, 'symbol': '?', 'mc': 0})
for t in trades:
    addr = t['tokenContractAddress']
    g = groups[addr]
    g['wallets'].add(t['walletAddress'])
    g['total_sol'] += float(t['quoteTokenAmount'])
    g['symbol'] = t['tokenSymbol']
    g['mc'] = max(g['mc'], float(t['marketCap']))

signals = [(addr, g) for addr, g in groups.items() if len(g['wallets']) >= 2]
signals.sort(key=lambda x: len(x[1]['wallets']), reverse=True)

print(f'\n【2】Top signals (≥2 wallets):')
for addr, g in signals[:5]:
    status = "★★★ POTENTIAL" if any(kw in g['symbol'].lower() for kw in ['musk','trump','doge','elon','donald','cz','binance']) else ""
    print(f'  {g["symbol"]:15s} | {len(g["wallets"])} wallets | {g["total_sol"]:.2f} SOL | MC ${g["mc"]:,.0f} {status}')

if signals:
    best_addr, best = signals[0]
    print(f'\n【3】Testing Hermes with: {best["symbol"]} (CA: {best_addr[:10]}...)')
    
    prompt = f'''Quick SM cluster signal on Solana — classify & push if ★★★:

Token: {best['symbol']}
CA: {best_addr}
Smart Money: {len(best['wallets'])} wallets
Buy: {best['total_sol']:.2f} SOL (~${best['total_sol']*84:.0f})
MC: ${best['mc']:,.0f}

Steps (max 2 tool calls — be fast):
1. If token name/symbol CLEARLY relates to Elon Musk, Trump, CZ/Binance → ★★★ → push to TG via send_message.
2. If NOT clearly in those categories → SKIP immediately, no extra research needed.

Rules:
- Pump.fun token (address ends 'pump') — skip audit, just classify.
- Only push if ★★★ (Musk/Trump/CZ/Binance narrative).
- Respond in ONE line: PUSHED: symbol — narrative, or SKIP: symbol — reason (≤10 words).'''
    
    t0 = time.time()
    r = requests.post('http://127.0.0.1:8642/v1/chat/completions',
        json={'model':'hermes-agent','messages':[{'role':'user','content':prompt}],'max_tokens':100},
        timeout=50)
    elapsed = time.time() - t0
    resp = r.json()['choices'][0]['message']['content']
    print(f'\n【4】Hermes response ({elapsed:.1f}s):')
    print(f'  {resp}')
else:
    print('\nNo signals with ≥2 wallets')

print('\n✅ Test complete')
