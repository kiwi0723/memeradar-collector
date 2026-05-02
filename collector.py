#!/usr/bin/env python3
"""
Collector v7 — 低延迟: 30s窗口 + 并行Hermes + 去掉web搜索

数据源:
  GMGN  — 主数据源 (sm+kol + 钱包标签 + USD金额)
  OKX   — 交叉验证源 (tracker activities, 免费)

信号打分:
  wallet_score   — 钱包数 × 来源权重
  volume_score   — 买入量对数
  tag_score      — 标签稀有度
  okx_bonus      — 双源确认加成
  → score ≥ 阈值 → 推 Hermes

分析: Hermes + 6551 MCP (opennews + twitter) + gmgn-token
推送: Telegram (仅★★★)
"""

import requests
import json
import time
import subprocess
import os
import math
from datetime import datetime
from collections import defaultdict
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# === 配置 ===
HERMES_API = "http://127.0.0.1:8642/v1/chat/completions"
HERMES_MODEL = "hermes-agent"

CHAINS_GMGN = ["sol", "eth", "base", "bsc"]
CHAINS_OKX  = {"sol": "solana", "eth": "ethereum", "base": "base", "bsc": "bsc"}

POLL_INTERVAL = 12
CHAIN_POLL_DELAY = 1.0
CLUSTER_WINDOW = 30

# 信号打分权重
SIGNAL_THRESHOLD = 10       # 最低分数才推 Hermes
SCORE_WALLET_BASE = 2.0     # 每个钱包基础分
SCORE_SM_BONUS = 0.3        # smart_money 加成
SCORE_KOL_BONUS = 0.5       # KOL 加成 (kol更值钱)
SCORE_TAG_RARE = 1.0        # 稀有标签 (axiom/sigma/bullx) 加分
SCORE_TAG_PREMIUM = {       # 高价值标签
    "axiom": 2.0, "sigma": 2.0, "bullx": 1.5, "bananagun": 1.5,
    "app_smart_money": 1.5, "gmgn": 1.0,
}
SCORE_OKX_VERIFY = 3.0      # OKX 双源确认加分
SCORE_VOLUME_LOG = 1.5      # log10(volume) 系数

MAX_BUFFER_PER_TOKEN = int(os.environ.get("MAX_BUFFER_PER_TOKEN", 50))
POST_COOLDOWN = 600
MAX_POSTS_PER_CYCLE = 2

SEEN_POSTS = {}
TRADE_BUFFER = defaultdict(list)
SEEN_TXHASHES = set()
OKX_VERIFIED = set()
OKX_VERIFIED_TS = {}
OKX_VERIFY_TTL = 300

# ── 双推: Hermes 分类后, ★★★ 同时发旧bot ──
OLD_TG_TOKEN = os.environ.get("TG_PUSH_TOKEN", "")
OLD_TG_CHAT = os.environ.get("TG_PUSH_CHAT", "")

def push_to_old_bot(text: str):
    """直接调 Telegram API 发给旧bot"""
    try:
        requests.post(
            f"https://api.telegram.org/bot{OLD_TG_TOKEN}/sendMessage",
            json={"chat_id": OLD_TG_CHAT, "text": text},
            timeout=5
        )
    except Exception:
        pass

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "collector.log")

def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# ═══════════════════════════════════════════
# Retry / backoff
# ═══════════════════════════════════════════

def retry(max_attempts=3, base_delay=2.0):
    """指数退避重试装饰器"""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    last_err = e
                    if attempt < max_attempts:
                        delay = base_delay * (2 ** (attempt - 1))
                        time.sleep(delay)
            name = getattr(fn, '__name__', str(fn))
            log(f"  ⚠️ {name} failed after {max_attempts} attempts: {last_err}")
            return None  # fallback
        return wrapper
    return decorator

# ═══════════════════════════════════════════
# GMGN 数据采集 (主源)
# ═══════════════════════════════════════════

@retry(max_attempts=2, base_delay=1.0)
def fetch_gmgn_trades(track_type: str, chain: str) -> list:
    result = subprocess.run(
        ["gmgn-cli", "track", track_type,
         "--chain", chain, "--side", "buy",
         "--limit", "100", "--raw"],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        raise RuntimeError(f"CLI error: {result.stderr[:100]}")
    data = json.loads(result.stdout)
    return data.get("list", []) or []

def process_gmgn_trades(trades: list, source: str, chain: str):
    now = time.time()
    new = 0
    for t in (trades or []):
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
            "source": source,
        })
        new += 1
    return new

# ═══════════════════════════════════════════
# OKX 交叉验证
# ═══════════════════════════════════════════

@retry(max_attempts=2, base_delay=1.0)
def fetch_okx_trades(chain_okx: str) -> set:
    result = subprocess.run(
        ["onchainos", "tracker", "activities",
         "--tracker-type", "smart_money",
         "--chain", chain_okx,
         "--trade-type", "1"],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        raise RuntimeError(f"CLI error: {result.stderr[:100]}")
    data = json.loads(result.stdout)
    if not data.get("ok"):
        raise RuntimeError(f"API error: {data.get('error','?')[:80]}")

    trades = data.get("data", {}).get("trades", [])
    addr_wallets = defaultdict(set)
    for t in trades:
        addr = t.get("tokenContractAddress", "")
        wallet = t.get("walletAddress", "")
        if addr and wallet:
            addr_wallets[addr].add(wallet)
    return {addr for addr, wallets in addr_wallets.items() if len(wallets) >= 2}

def update_okx_verification():
    global OKX_VERIFIED, OKX_VERIFIED_TS
    now = time.time()
    for gmgn_chain, okx_chain in CHAINS_OKX.items():
        verified = fetch_okx_trades(okx_chain)
        if verified is None:
            continue
        for addr in verified:
            OKX_VERIFIED.add(addr)
            OKX_VERIFIED_TS[addr] = now
    stale = [a for a, ts in OKX_VERIFIED_TS.items() if now - ts > OKX_VERIFY_TTL]
    for a in stale:
        OKX_VERIFIED.discard(a)
        del OKX_VERIFIED_TS[a]

# ═══════════════════════════════════════════
# 信号打分 (核心升级)
# ═══════════════════════════════════════════

def score_signal(wallet_count, sm_count, kol_count, total_usd, tags, okx_verified) -> float:
    """
    加权打分:
      - 钱包数量 (SM加成0.3, KOL加成0.5)
      - 买入量 (log10)
      - 稀有标签
      - OKX双源确认
    """
    score = 0.0

    # 钱包分: 每个钱包基础分 + 类型加成
    score += wallet_count * SCORE_WALLET_BASE
    score += sm_count * SCORE_SM_BONUS
    score += kol_count * SCORE_KOL_BONUS

    # 量分: log10, 最低1分
    score += max(1.0, math.log10(max(total_usd, 10))) * SCORE_VOLUME_LOG

    # 标签分
    for tag in tags:
        if tag in SCORE_TAG_PREMIUM:
            score += SCORE_TAG_PREMIUM[tag]
        elif tag not in ("smart_degen", "fresh_wallet", "padre", "photon", "kol"):
            score += SCORE_TAG_RARE  # 不常见的标签加分

    # OKX 双源确认
    if okx_verified:
        score += SCORE_OKX_VERIFY

    return round(score, 1)

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

        if len(wallet_set) < 2:  # 至少2个钱包才打分
            continue

        okx_verified = addr in OKX_VERIFIED
        sc = score_signal(
            len(wallet_set), sm_count, kol_count, total_usd,
            sorted(all_tags), okx_verified
        )

        if sc < SIGNAL_THRESHOLD:
            continue

        last_post = SEEN_POSTS.get(addr, 0)
        if now - last_post < POST_COOLDOWN:
            continue

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
            "score": sc,
            "ts": now,
        })

    for addr in stale:
        del TRADE_BUFFER[addr]

    # 按分数降序
    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals

# ═══════════════════════════════════════════
# Hermes 通信
# ═══════════════════════════════════════════

def build_hermes_prompt(sig: dict) -> str:
    chain_emoji = {"SOL": "🟣", "ETH": "🔵", "BASE": "🔵"}
    emoji = chain_emoji.get(sig["chain"], "")
    tags_str = ", ".join(sig.get("tags", [])[:5])
    lp_info = f" | launchpad: {sig['launchpad']}" if sig.get("launchpad") else ""
    okx_note = " | ✅OKX" if sig.get("okx_verified") else ""

    return f"""Quick SM cluster — classify & push if ★★★ (NO web search, fast only):

Token: {sig['symbol']} {emoji}{sig['chain']}
CA: {sig['address']}
Buyers: {sig['wallet_count']}w (SM:{sig['sm_count']} KOL:{sig['kol_count']})
Buy: ${sig['total_usd']:,.0f}
Score: {sig['score']}{okx_note} | Tags: {tags_str}{lp_info}
GMGN: https://gmgn.ai/{sig['chain'].lower()}/token/{sig['address']}

Steps (max 3 tool calls, no web_search):
1. Open GMGN link → check Twitter/website/social links on token page.
2. If ANY of: CZ/Binance tweet / KOL shilling / major news / hot narrative / notable dev → ★★★.
3. Else → SKIP.

IMPORTANT: Do NOT use web_search. Do NOT use send_message. Only respond with text.
Reply EXACTLY one line, no prefix, no explanation:
PUSHED: symbol — narrative
or
SKIP: symbol — reason"""

def send_to_hermes(sig: dict) -> str:
    prompt = build_hermes_prompt(sig)
    r = requests.post(HERMES_API, json={
        "model": HERMES_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 100,
    }, timeout=60)
    if r.status_code == 200:
        return r.json()["choices"][0]["message"]["content"].strip()
    raise RuntimeError(f"HTTP {r.status_code}")

# ═══════════════════════════════════════════
# 主循环
# ═══════════════════════════════════════════

def main():
    log("=== Collector v7 — fast: 30s window + parallel + no web search ===")
    log(f"Chains: GMGN {CHAINS_GMGN} | OKX {list(CHAINS_OKX.values())}")
    log(f"Score threshold: ≥{SIGNAL_THRESHOLD} | Weights: wallet×{SCORE_WALLET_BASE} SM+{SCORE_SM_BONUS} KOL+{SCORE_KOL_BONUS} OKX+{SCORE_OKX_VERIFY}")

    cycle = 0
    while True:
        try:
            cycle += 1
            now = time.time()
            total_new = 0

            update_okx_verification()

            for chain in CHAINS_GMGN:
                sm = fetch_gmgn_trades("smartmoney", chain)
                if sm: total_new += process_gmgn_trades(sm, "sm", chain)
                kol = fetch_gmgn_trades("kol", chain)
                if kol: total_new += process_gmgn_trades(kol, "kol", chain)
                time.sleep(CHAIN_POLL_DELAY)

            signals = cluster_signals()

            if signals:
                batch = signals[:MAX_POSTS_PER_CYCLE]

                # Mark SEEN upfront (prevent parallel dupes)
                for sig in batch:
                    SEEN_POSTS[sig["address"]] = now
                    verif = "✅OKX" if sig.get("okx_verified") else "⚠️GMGN"
                    log(f"→ {verif} {sig['symbol']}({sig['chain']}) score={sig['score']} | {sig['wallet_count']}w | ${sig['total_usd']:,.0f}")

                # Parallel Hermes analysis
                def analyze(sig):
                    try:
                        result = send_to_hermes(sig)
                        return (sig, result or "TIMEOUT")
                    except Exception as e:
                        return (sig, f"ERROR: {e}")

                with ThreadPoolExecutor(max_workers=MAX_POSTS_PER_CYCLE) as pool:
                    futures = {pool.submit(analyze, sig): sig for sig in batch}
                    for future in as_completed(futures):
                        sig, result = future.result()
                        log(f"← Hermes [{sig['symbol']}]: {result}")
                        # ★★★ 推送到旧bot
                        if result and "PUSHED" in result:
                            narrative = result.replace("PUSHED:", "").strip()
                            chain_emoji_map = {"SOL":"🟣","ETH":"🔵","BASE":"🔵","BSC":"🟡"}
                            emoji = chain_emoji_map.get(sig["chain"], "")
                            verif = "✅OKX" if sig.get("okx_verified") else "⚠️GMGN"
                            msg = (
                                f"🚨 *{sig['symbol']}* {emoji}{sig['chain']}\n\n"
                                f"🎯 {narrative}\n\n"
                                f"📊 {sig['wallet_count']}w | ${sig['total_usd']:,.0f} | Score {sig['score']}\n"
                                f"{verif} | SM:{sig['sm_count']} KOL:{sig['kol_count']}\n\n"
                                f"`{sig['address']}`"
                            )
                            push_to_old_bot(msg)

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
