import requests, json, time

prompt = """Quick SM cluster — classify & push if ★★★:

Token: CRABBOX 🔵BASE
CA: 0xCRABBOX
Buyers: 7 wallets (SM:6 KOL:11)
Buy: $6,104
Tags: gmgn, kol, smart_degen

Steps (max 3 tool calls):
1. mcp_opennews_search_news(keyword="CRABBOX") — check if token has real news behind it.
2. If token name/symbol CLEARLY relates to Musk, Trump, CZ/Binance → ★★★ → push TG via send_message.
3. If NOT → SKIP immediately.

Rules:
- Use 6551 MCP tools (opennews) for narrative verification.
- Respond ONE line: PUSHED: symbol — narrative | SKIP: symbol — reason (≤10 words)."""

t0 = time.time()
r = requests.post("http://127.0.0.1:8642/v1/chat/completions",
    json={"model":"hermes-agent","messages":[{"role":"user","content":prompt}],"max_tokens":100},
    timeout=45)
elapsed = time.time() - t0
resp = r.json()["choices"][0]["message"]["content"]
print(f"[{elapsed:.1f}s] {resp}")
