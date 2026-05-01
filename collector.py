#!/usr/bin/env python3
"""
Collector v4 — 双源交叉验证信号采集器

数据源:
  GMGN  — 主数据源 (sm+koldata + 钱包标签 + USD金额)
  OKX   — 交叉验证源 (tracker activities, 免费)

分析:
  Hermes API (8642) — 接收信号, 调用:
    6551 opennews MCP — 新闻验证
    6551 twitter MCP  — 推文验证
    gmgn-token skill — token详情

推送:
  Hermes send_message → Telegram (仅★★★)

三层:
  采集层: GMGN + OKX 双源
  分析层: Hermes + 6551 MCP (opennews + twitter)
  推送层: Telegram
"""

import requests
import json
import time
import subprocess
import os
from datetime import datetime
from collections import defaultdict

# === 配置 ===
HERMES_API = "http://127.0.0.1:8642/v1/chat/completions"
HERMES_MODEL = "hermes-agent"

CHAINS_GMGN = ["sol", "eth", "base"]
CHAINS_OKX  = {"sol": "solana", "eth": "ethereum", "base": "base"}

POLL_INTERVAL = 12
CHAIN_POLL_DELAY = 1.0
CLUSTER_WINDOW = 120
MIN_WALLETS = 3
MIN_TOTAL_USD = 100
MIN_MC = 5000
POST_COOLDOWN = 600
MAX_POSTS_PER_CYCLE = 2
MAX_BUFFER_PER_TOKEN = 50

SEEN_POSTS = {}
TRADE_BUFFER = defaultdict(list)
SEEN_TXHASHES = set()
OKX_VERIFIED = set()          # OKX也看到这些地址 → 高置信
OKX_VERIFIED_TS = {}          # {address: timestamp}
OKX_VERIFY_TTL = 300          # OKX验证有效期(秒)

LOG_FILE = os.path.expanduser("~/crypto-trading/collector.log")

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# ═══════════════════════════════════════════
# GMGN 数据采集 (主源)
# ═══════════════════════════════════════════

def fetch_gmgn_trades(track_type: str, chain: str) -> list:
    try:
        result = subprocess.run(
            ["gmgn-cli", "track", track_type,
             "--chain", chain, "--side", "buy",
             "--limit", "100", "--raw"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return []
        return json.loads(result.stdout).get("list", [])
    except Exception as e:
        log(f"  gmgn {track_type}/{chain}: {e}")
        return []

def process_gmgn_trades(trades: list, source: str, chain: str):
    now = time.time()
    new = 0
    for t in trades:
        txh = t.get("transaction_hash", "")
        if txh and txh in SEEN_TXHASHES:
            continue
        if txh:
            SEEN_TXHASHES.add(txh)
            if len(SEEN_TXHASHES) > 50000:
                SEEN_TXHASHES.clear()

        addr = t.get("base_address", "")
        if not addr:
            continue
        try:
            amount_usd = float(t.get("amount_usd", 0))
        except (ValueError, TypeError):
            continue
        if amount_usd <= 0:
            continue

        buf = TRADE_BUFFER[addr]
        if len(buf) >= MAX_BUFFER_PER_TOKEN:
            continue

        buf.append({
            "ts": now,
            "wallet": t.get("maker", ""),
            "amount_usd": amount_usd,
            "symbol": t.get("base_token", {}).get("symbol", "???"),
            "tags": t.get("maker_info", {}).get("tags", []),
            "chain": chain.upper(),
            "launchpad": t.get("base_token", {}).get("launchpad", ""),
            "source": source,  # 'sm' | 'kol'
        })
        new += 1
    return new

# ═══════════════════════════════════════════
# OKX 交叉验证 (副源)
# ═══════════════════════════════════════════

def fetch_okx_trades(chain_okx: str) -> set:
    """拉取OKX tracker SM买入, 返回有≥2个SM钱包的token地址集合"""
    try:
        result = subprocess.run(
            ["onchainos", "tracker", "activities",
             "--tracker-type", "smart_money",
             "--chain", chain_okx,
             "--trade-type", "1"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            return set()
        data = json.loads(result.stdout)
        if not data.get("ok"):
            return set()

        trades = data.get("data", {}).get("trades", [])
        # 按地址聚类
        addr_wallets = defaultdict(set)
        for t in trades:
            addr = t.get("tokenContractAddress", "")
            wallet = t.get("walletAddress", "")
            if addr and wallet:
                addr_wallets[addr].add(wallet)

        # 返回 ≥2 SM钱包的地址
        return {addr for addr, wallets in addr_wallets.items() if len(wallets) >= 2}
    except Exception as e:
        log(f"  okx/{chain_okx}: {e}")
        return set()

def update_okx_verification():
    """轮询所有链的OKX数据, 更新OKX_VERIFIED集合"""
    global OKX_VERIFIED, OKX_VERIFIED_TS
    now = time.time()

    for gmgn_chain, okx_chain in CHAINS_OKX.items():
        verified_addrs = fetch_okx_trades(okx_chain)
        for addr in verified_addrs:
            OKX_VERIFIED.add(addr)
            OKX_VERIFIED_TS[addr] = now

    # 清理过期
    stale = [a for a, ts in OKX_VERIFIED_TS.items() if now - ts > OKX_VERIFY_TTL]
    for a in stale:
        OKX_VERIFIED.discard(a)
        del OKX_VERIFIED_TS[a]

# ═══════════════════════════════════════════
# 信号聚类
# ═══════════════════════════════════════════

def cluster_signals() -> list:
    global TRADE_BUFFER
    now = time.time()
    signals = []
    stale = []

    for addr, trades in TRADE_BUFFER.items():
        fresh = [t for t in trades if now - t["ts"] < CLUSTER_WINDOW]
        TRADE_BUFFER[addr] = fresh

        if not fresh:
            stale.append(addr)
            continue
        if len(fresh) < MIN_WALLETS:
            continue

        wallet_set = set(t["wallet"] for t in fresh)
        total_usd = sum(t["amount_usd"] for t in fresh)
        symbol = fresh[-1].get("symbol", "???")
        chain = fresh[-1].get("chain", "?")
        launchpad = fresh[-1].get("launchpad", "")
        all_tags = set()
        sm_count = kol_count = 0
        for t in fresh:
            all_tags.update(t.get("tags", []))
            if t.get("source") == "sm": sm_count += 1
            elif t.get("source") == "kol": kol_count += 1

        if len(wallet_set) >= MIN_WALLETS and total_usd >= MIN_TOTAL_USD:
            last_post = SEEN_POSTS.get(addr, 0)
            if now - last_post < POST_COOLDOWN:
                continue

            # OKX交叉验证
            okx_verified = addr in OKX_VERIFIED

            signals.append({
                "address": addr,
                "symbol": symbol,
                "chain": chain,
                "wallet_count": len(wallet_set),
                "sm_count": sm_count,
                "kol_count": kol_count,
                "total_usd": round(total_usd, 0),
                "trades": len(fresh),
                "tags": sorted(all_tags),
                "launchpad": launchpad,
                "okx_verified": okx_verified,
                "ts": now,
            })

    for addr in stale:
        del TRADE_BUFFER[addr]
    return signals

# ═══════════════════════════════════════════
# Hermes 通信
# ═══════════════════════════════════════════

def build_hermes_prompt(sig: dict) -> str:
    chain_emoji = {"SOL": "🟣", "ETH": "🔵", "BASE": "🔵"}
    emoji = chain_emoji.get(sig["chain"], "")
    tags_str = ", ".join(sig.get("tags", [])[:5])
    lp_info = f" | launchpad: {sig['launchpad']}" if sig.get("launchpad") else ""
    okx_note = " | ✅ OKX-verified" if sig.get("okx_verified") else ""

    return f"""Quick SM cluster — classify & push if ★★★{okx_note}:

Token: {sig['symbol']} {emoji}{sig['chain']}
CA: {sig['address']}
Buyers: {sig['wallet_count']} wallets (SM:{sig['sm_count']} KOL:{sig['kol_count']})
Buy: ${sig['total_usd']:,.0f}
Tags: {tags_str}{lp_info}

Steps (max 3 tool calls):
1. mcp_opennews_search_news(keyword="{sig['symbol']}") — news coverage.
2. mcp_twitter_search_twitter(keywords="{sig['symbol']}") — Musk/Trump/CZ tweets.
3. If NEWS or TWEETS connect to Musk/Trump/CZ/Binance → ★★★ → push TG.
4. If nothing → SKIP immediately.

Rules:
- Use 6551 MCP (opennews + twitter).
- Use gmgn-token for details if needed.
- OKX-verified = higher confidence, push if ★★★ even if borderline.
- Respond ONE line: PUSHED: symbol — narrative | SKIP: symbol — reason (≤10 words)."""

def send_to_hermes(sig: dict) -> str:
    prompt = build_hermes_prompt(sig)
    try:
        r = requests.post(HERMES_API, json={
            "model": HERMES_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 100,
        }, timeout=90)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
        return f"HTTP {r.status_code}"
    except requests.Timeout:
        return "TIMEOUT"
    except Exception as e:
        return f"ERROR: {e}"

# ═══════════════════════════════════════════
# 主循环
# ═══════════════════════════════════════════

def main():
    log("=== Collector v4 — GMGN+OKX dual source, Hermes+6551 MCP ===")
    log(f"Chains: GMGN {CHAINS_GMGN} | OKX {list(CHAINS_OKX.values())}")
    log(f"Thresholds: ≥{MIN_WALLETS}w | ≥${MIN_TOTAL_USD} | MC≥${MIN_MC:,}")
    log(f"Hermes: {HERMES_API}")

    cycle = 0
    while True:
        try:
            cycle += 1
            now = time.time()
            total_new = 0

            # ── OKX 交叉验证 (轻量, 先跑) ──
            update_okx_verification()

            # ── GMGN 主数据采集 ──
            for chain in CHAINS_GMGN:
                sm = fetch_gmgn_trades("smartmoney", chain)
                if sm: total_new += process_gmgn_trades(sm, "sm", chain)
                kol = fetch_gmgn_trades("kol", chain)
                if kol: total_new += process_gmgn_trades(kol, "kol", chain)
                time.sleep(CHAIN_POLL_DELAY)

            # ── 聚类 + 推送 ──
            signals = cluster_signals()
            pushed = 0
            for sig in signals[:MAX_POSTS_PER_CYCLE]:
                SEEN_POSTS[sig["address"]] = now
                verif = "✅OKX" if sig.get("okx_verified") else "⚠️GMGN-only"
                log(f"→ {verif} {sig['symbol']}({sig['chain']}) | {sig['wallet_count']}w | ${sig['total_usd']:,.0f}")
                result = send_to_hermes(sig)
                log(f"← Hermes: {result}")
                pushed += 1

            if cycle % 5 == 0:
                log(f"Status: c={cycle} | new={total_new} | buf={len(TRADE_BUFFER)} | okx={len(OKX_VERIFIED)} | seen={len(SEEN_POSTS)}")

        except KeyboardInterrupt:
            log("Shutting down...")
            break
        except Exception as e:
            log(f"Loop error: {e}")
            import traceback
            traceback.print_exc()

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
