#!/usr/bin/env python3
"""
Collector v8.0-nohermes — ZERO token, pure rule engine

数据源:
  GMGN  — 主数据源 (gmgn-cli, sm+kol+钱包标签+USD)
  OKX   — 交叉验证 (tracker activities, 免费)
  DEXScreener — MC fallback (非SOL链, 免费)

分析: 规则引擎 (gmgn-cli + DEXScreener, 零DeepSeek token)
推送: Telegram 直接 Bot API (零Hermes)
"""

VERSION = "v8.0-nohermes"

import json
import time
import subprocess
import os
import math
from datetime import datetime
from collections import defaultdict
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# === 配置 (v8: 去掉Hermes, gmgn-cli + 规则引擎) ===

CHAINS_GMGN = ["sol", "eth", "base", "bsc"]
CHAINS_OKX  = {"sol": "solana", "eth": "ethereum", "base": "base", "bsc": "bsc"}

POLL_INTERVAL = 12
CHAIN_POLL_DELAY = 1.0
CLUSTER_WINDOW = 180

SIGNAL_THRESHOLD = 15       # 最低分数才进入分析
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
POST_COOLDOWN = 3600       # 同一CA冷却1小时（从600s）
MAX_POSTS_PER_CYCLE = 2
MIN_MC = 50000             # 最低市值$50K，低于不推


# v8 规则引擎分类参数
PUSH_MIN_MC = 50000
CTO_MIN_SM = 10
CTO_MAX_AGE_HOURS = 168
STRONG_KOL_MIN = 5
STRONG_SM_MIN = 20
HIGH_SCORE_OKX = 25
HIGH_SM_SOLO = 30
BORDERLINE_SCORE = 20
BUNDLE_AUTO_SKIP = 0.60

POST_COOLDOWN = 3600       # 同一CA冷却1小时（从600s）
MAX_POSTS_PER_CYCLE = 2
MIN_MC = 50000             # 最低市值$50K，低于不推

SEEN_POSTS = {}
TRADE_BUFFER = defaultdict(list)
SEEN_TXHASHES = set()
OKX_VERIFIED = set()
OKX_VERIFIED_TS = {}
OKX_VERIFY_TTL = 300

OLD_TG_TOKEN = os.environ.get("TG_PUSH_TOKEN", "")
OLD_TG_CHAT = os.environ.get("TG_PUSH_CHAT", "")

def escape_mdv2(text: str) -> str:
    """Escape MarkdownV2 special chars, preserving intentional *bold* and `code`"""
    # Temp placeholders to protect intentional formatting
    text = text.replace(r'*', '\x00B\x00')
    text = text.replace(r'`', '\x00C\x00')
    for ch in '_*[]()~`>#+-=|{}.!':
        text = text.replace(ch, '\\' + ch)
    text = text.replace('\x00B\x00', '*')
    text = text.replace('\x00C\x00', '`')
    return text

def push_to_old_bot(text: str):
    """直接调 Telegram API 发给旧bot"""
    try:
        requests.post(
            f"https://api.telegram.org/bot{OLD_TG_TOKEN}/sendMessage",
            json={"chat_id": OLD_TG_CHAT, "text": escape_mdv2(text), "parse_mode": "MarkdownV2"},
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
        bundle_wallets = 0
        bundle_addresses = []
        bundle_tags = {"fresh_wallet", "smart_degen", "photon", "padre", "sandwich_bot", "mev_bot"}
        for t in fresh:
            all_tags.update(t.get("tags", []))
            if t.get("source") == "sm": sm_count += 1
            elif t.get("source") == "kol": kol_count += 1
            if bundle_tags & set(t.get("tags", [])):
                bundle_wallets += 1
                bundle_addresses.append(t.get("wallet", ""))

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
            "bundle_wallets": bundle_wallets,
            "bundle_addresses": bundle_addresses,
            "ts": now,
        })

    for addr in stale:
        del TRADE_BUFFER[addr]

    # 按分数降序
    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals

# ═══════════════════════════════════════════

# ═══════════════════════════════════════════
# GMGN Token Info + 规则分类 (v8: 取代Hermes)
# ═══════════════════════════════════════════

@retry(max_attempts=2, base_delay=2.0)
def fetch_gmgn_token_info(address: str, chain: str) -> dict:
    """拉取GMGN token完整信息 (免费, 零token)"""
    result = subprocess.run(
        ["gmgn-cli", "token", "info", "--raw", "--chain", chain, "--address", address],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        raise RuntimeError("gmgn-cli error: " + str(result.stderr[:100]))
    data = json.loads(result.stdout)
    return data if data else {}

@retry(max_attempts=1, base_delay=1.0)
def fetch_pumpfun_description(address: str) -> str:
    """Pump.fun代币description"""
    try:
        r = requests.get(
            "https://frontend-api.pump.fun/coins/" + address,
            timeout=5, headers={"User-Agent": "collector/8.0"}
        )
        if r.status_code == 200:
            return r.json().get("description", "") or ""
    except:
        pass
    return ""

@retry(max_attempts=1, base_delay=1.0)
def fetch_dexscreener_info(address: str) -> dict:
    """DEXScreener API - MC, social links, on-chain data (free, zero token)"""
    try:
        r = requests.get(
            "https://api.dexscreener.com/latest/dex/tokens/" + address,
            timeout=10, headers={"User-Agent": "collector/8.0"}
        )
        if r.status_code != 200:
            return {}
        data = r.json()
        pairs = data.get("pairs", [])
        if not pairs:
            return {}
        p = pairs[0]
        mc = p.get("marketCap", 0)
        if mc is None:
            mc = 0
        try:
            mc = float(mc)
        except (ValueError, TypeError):
            mc = 0
        return {
            "mc": mc,
            "price": float(p.get("priceUsd", 0) or 0),
            "chain": p.get("chainId", ""),
            "created": p.get("pairCreatedAt", 0),
            "twitter": (p.get("info", {}) or {}).get("twitter", ""),
            "website": "",
        }
    except Exception as e:
        log("DEX error for " + address[:10] + ": " + str(e)[:100])
        return {}

def extract_narrative(info: dict) -> str:
    """从GMGN/Pump.fun/Twitter提取叙事文本"""
    link = info.get("link", {})

    # 1. GMGN description
    desc = info.get("description", "")
    if desc and len(desc) > 5:
        return desc[:150]

    # 2. Pump.fun description
    addr = info.get("address", "")
    launchpad = info.get("launchpad", "").lower()
    if launchpad == "pump" and addr:
        pf_desc = fetch_pumpfun_description(addr)
        if pf_desc:
            return pf_desc[:150]

    # 3. Twitter bio via fxtwitter
    twitter = link.get("twitter_username", "")
    if twitter:
        username = twitter.split("/status/")[0].lstrip("/").split("/")[-1] if "/status/" in twitter else twitter
        if username:
            try:
                r = requests.get("https://api.fxtwitter.com/" + username, timeout=5)
                if r.status_code == 200:
                    bio = r.json().get("user", {}).get("description", "")
                    if bio:
                        return bio[:150]
            except:
                pass

    # 4. Website fallback
    website = link.get("website", "")
    if website:
        return "🔗 " + website[:120]

    return ""

def classify_and_narrate(sig: dict, info: dict) -> tuple:
    """规则引擎: Returns (verdict, narrative, mc_usd, creation_time_str)"""
    if not info:
        return ("SKIP", "gmgn-cli无数据", 0, "")

    dev = info.get("dev", {})
    link = info.get("link", {})
    stats = info.get("wallet_tags_stat", {})
    stat = info.get("stat", {})

    sm_wallets = stats.get("smart_wallets", 0)
    kol_wallets = stats.get("renowned_wallets", 0)
    cto_flag = dev.get("cto_flag", 0)
    creator_status = dev.get("creator_token_status", "")

    try:
        bundler_pct = float(stat.get("top_bundler_trader_percentage", 0))
    except (ValueError, TypeError):
        bundler_pct = 0

    try:
        price = float(info.get("price", 0))
        supply = int(info.get("circulating_supply", 0))
        decimals = int(info.get("decimals", 0))
        mc_usd = price * (supply / (10**decimals)) if decimals else price * supply
    except (ValueError, TypeError):
        mc_usd = 0

    # DEXScreener fallback — gmgn-cli 对非SOL链返回decimals=0，导致MC极小
    dx = {}
    if mc_usd < 1:  # MC < $1 = effectively zero, gmgn decimals broken
        dx = fetch_dexscreener_info(sig["address"])
        if dx and dx.get("mc", 0) > 0:
            mc_usd = dx["mc"]

    creation_ts = info.get("creation_timestamp", 0)
    if creation_ts == 0 and dx.get("created", 0) > 0:
        creation_ts = dx["created"] / 1000  # DEXScreener uses ms
    creation_time = datetime.fromtimestamp(creation_ts).strftime("%Y-%m-%d %H:%M") if creation_ts else ""
    age_hours = (time.time() - creation_ts) / 3600 if creation_ts else 9999

    narrative = extract_narrative(info)
    # Enrich narrative from DEXScreener if gmgn has no socials
    if not narrative and dx:
        if dx.get("twitter"):
            narrative = "🐦 " + dx["twitter"]
        elif dx.get("website"):
            narrative = "🔗 " + dx["website"]
    has_social = bool(link.get("twitter_username") or link.get("website"))

    # ═══ 规则引擎 ═══

    # 🔴 高捆绑率自动SKIP
    if bundler_pct > BUNDLE_AUTO_SKIP:
        return ("SKIP", "捆绑率" + str(int(bundler_pct * 100)) + "%", mc_usd, creation_time)

    # 🔴 Dev清仓无CTO
    if creator_status == "creator_close" and not cto_flag:
        return ("SKIP", "Dev已清仓无CTO", mc_usd, creation_time)

    # 🟢 CTO + SM足够 + 新鲜
    if cto_flag and sm_wallets >= CTO_MIN_SM and age_hours < CTO_MAX_AGE_HOURS:
        return ("PUSHED", narrative or "🔥CTO社区接管", mc_usd, creation_time)

    # 🟢 OKX双源 + 高评分 + 明显SM
    if sig.get("okx_verified") and sig["score"] >= HIGH_SCORE_OKX and sm_wallets >= 15:
        fb = "SM" + str(sm_wallets) + "+KOL" + str(kol_wallets) + " OKX验证"
        return ("PUSHED", narrative or fb, mc_usd, creation_time)

    # 🟢 强KOL入场
    if kol_wallets >= STRONG_KOL_MIN and sm_wallets >= STRONG_SM_MIN:
        fb = "KOL" + str(kol_wallets) + "+SM" + str(sm_wallets) + "集体入场"
        return ("PUSHED", narrative or fb, mc_usd, creation_time)

    # 🟢 高SM + 有叙事
    if sm_wallets >= HIGH_SM_SOLO and narrative:
        return ("PUSHED", narrative, mc_usd, creation_time)

    # 🔴 无叙事无社交
    if not narrative and not has_social:
        return ("SKIP", "无叙事无社交", mc_usd, creation_time)

    # 🟡 边界: 评分>=20 + 有社交
    if sig["score"] >= BORDERLINE_SCORE and has_social:
        fb = "信号SM" + str(sm_wallets) + "|评分" + str(sig["score"])
        return ("PUSHED", narrative or fb, mc_usd, creation_time)

    reason = "评分" + str(sig["score"]) + "KOL" + str(kol_wallets) + "不足"
    return ("SKIP", reason, mc_usd, creation_time)

# 主循环
# ═══════════════════════════════════════════

def main():
    log(f"=== Collector {VERSION} — fast: 30s window + parallel + no web search ===")
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
                # v8: gmgn-cli + 规则引擎 (零DeepSeek token)
                def analyze_v8(sig):
                    try:
                        info = fetch_gmgn_token_info(sig["address"], sig["chain"].lower())
                        verdict, narrative, mc_usd, crtime = classify_and_narrate(sig, info)
                        return (sig, verdict, narrative, mc_usd, crtime)
                    except Exception as e:
                        return (sig, "SKIP", "ERROR:" + str(e)[:50], 0, "")

                with ThreadPoolExecutor(max_workers=MAX_POSTS_PER_CYCLE) as pool:
                    futures = {pool.submit(analyze_v8, sig): sig for sig in batch}
                    for future in as_completed(futures):
                        sig, verdict, narrative, mc_usd, crtime = future.result()

                        mc_s = "$" + format(int(mc_usd), ",") if mc_usd > 0 else "?"
                        log("v " + verdict + " [" + sig["symbol"] + "]: " + narrative[:60] + " | MC:" + mc_s)

                        if verdict != "PUSHED":
                            continue

                        if mc_usd < PUSH_MIN_MC:
                            log("v SKIP [" + sig["symbol"] + "]: MC $" + format(int(mc_usd), ",") + " < $" + format(PUSH_MIN_MC, ","))
                            continue

                        ce = {"SOL":"🟣","ETH":"🔵","BASE":"🔵","BSC":"🟡"}
                        emoji = ce.get(sig["chain"], "")
                        verif = "✅OKX" if sig.get("okx_verified") else "⚠️GMGN"

                        parts = [
                            "🚨 *" + sig["symbol"] + "* " + emoji + sig["chain"],
                            "",
                            "🎯 " + narrative,
                        ]

                        meta = []
                        if mc_usd > 0:
                            meta.append("💰 MC: $" + format(int(mc_usd), ","))
                        if crtime:
                            meta.append("🕐 " + crtime)
                        meta.append("⭐ " + str(sig["score"]))
                        parts.append(" | ".join(meta))

                        bw = sig.get("bundle_wallets", 0)
                        bundle_line = ""
                        if bw >= 2:
                            ratio = int(bw / sig["wallet_count"] * 100)
                            bundle_line = "🧹 Bundle: " + str(bw) + "/" + str(sig["wallet_count"]) + "w (" + str(ratio) + "%)"

                        parts += [
                            "",
                            verif + " | SM:" + str(sig["sm_count"]) + " KOL:" + str(sig["kol_count"]),
                        ]
                        if bundle_line:
                            parts.append(bundle_line)
                        parts += [
                            "",
                            "🔗 `" + sig["address"] + "`",
                        ]
                        msg = "\n".join(parts)
                        push_to_old_bot(msg)
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