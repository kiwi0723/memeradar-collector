# MemeRadar — 链上叙事雷达 / On-Chain Narrative Radar

[English](#english) | [中文](#中文)

---

## 中文

### 概述

**MemeRadar**（链上叙事雷达）是一个纯 Python 实现的链上 MEME 币监控工具。它实时扫描 GMGN + DEXScreener 数据，通过动量追踪 + 推特热点检测，发现有 MEME 潜力的代币并通过 Telegram 推送。

**核心特点：**
- 纯 Python，零 AI 成本（仅正则 + 词法分析）
- 四条链全覆盖：ETH / SOL / BSC / BASE
- 每 30 秒扫描一轮，实时推送
- 推特热点关键词自动提取（监控 Elon Musk / Trump / CZ / 何一）
- 反向推特检测：链上异动 → 全网搜推文确认叙事
- MEME 信号评分体系：从推文中提取 "会发币" 的信号
- 安全检查：蜜罐 / 可增发拦截
- 去重：SQLite 持久化

### 推送策略

只推 **★★★ 级叙事**，其余仅记录：

| 级别 | 分类 | 是否推送 |
|------|------|---------|
| ★★★ | 马斯克/川普概念 | ✅ |
| ★★★ | 币安/CZ/何一概念 | ✅ (仅 BSC) |
| ★★ | 名人/热点 | ❌ |
| ★ | 无明确叙事 | ❌ |

链上异动但分类不符 → 自动反向查推特 → 匹配到推文 → 提升为 ★★★ 推送。

### 安装与配置

```bash
# 1. 安装依赖
pip install requests

# 2. 配置 Telegram Bot
# 创建 ~/.env 文件：
echo 'TG_TOKEN=你的bot_token' >> ~/.env
echo 'TG_CHAT_ID=你的chat_id' >> ~/.env

# chat_id 获取方式：给 Bot 发消息后访问
# https://api.telegram.org/bot<TOKEN>/getUpdates

# 3. 运行
python3 narrative_radar.py
```

后台运行：
```bash
python3 -u narrative_radar.py > narrative_radar.log 2>&1 &
```

### 架构

```
GMGN 新币 (ETH/SOL/BSC/BASE)  ──►  动量追踪器  ──►  叙事分类  ──►  TG 推送
         │                              │
         │                              ├─ 连涨3轮 + 涨幅>5%
         │                              ├─ 安全检查
         │                              └─ 市值 $1K~$10M
         │
   推特 RSS ──► 正推：每2分钟提取关键词 ──► 热点关键词池
                                    │
   链上异动 ──► 反推：Nitter 全网搜索 ──► 匹配推文 → 提升推送
```

### 评分体系 (extract_meme_signals)

| 类别 | 分数 | 说明 |
|------|------|------|
| 多词短语/概念 | 15-20 | "freedom of money", "government efficiency" |
| Action 短语 | 17 | "launching a new project", "building something" |
| 专有名词 + $TICKER | 10-15 | "Melania", "Sam Altman", "$DOGE" |
| 全大写缩写 | 10 | DOGE, FSD, XBNB |
| 争议词/情绪词 | 8-12 | "pardoned", "assassinated", "fraud" |
| 数字信号 | 5-8 | "$6 billion", "200%" |
| 单 CapWord | 3-5 | 大写名词作为潜在币名候补 |

### 数据源

- **GMGN API**: 新币排行（按创建时间 + 交易量）
- **DEXScreener API**: 代币描述、社交链接
- **Pump.fun API**: SOL 链代币描述
- **Nitter RSS**: 推特监控（4 个账号 + 全网搜索）
- **RugCheck.xyz**: SOL 安全检查
- **GoPlus**: EVM 安全检查
- **SQLite**: 历史去重

### 文件结构

```
~/crypto-trading/
├── narrative_radar.py    # 主程序 (1972 行)
├── narrative_radar.log   # 运行日志
├── narrative_history.db  # SQLite 数据库
├── narrative_seen.json   # 已见代币缓存
└── flap_seen.json        # FLAP 代币缓存
```

### 注意事项

- Nitter 可能不稳定，偶尔超时不影响主循环
- SOL 链 MEME 币过多，涨幅门槛设为 15%（其他链 5%）
- 反向检测需要精确匹配 token 关键词，避免误报
- Telegram Markdown 发送失败会自动降级为纯文本

---

## English

### Overview

**MemeRadar** is a pure Python on-chain MEME token scanner. It monitors GMGN + DEXScreener data in real-time, detects trending tokens through momentum tracking + Twitter hot keywords, and pushes alerts via Telegram.

**Key Features:**
- Pure Python, zero AI cost (regex + lexical analysis only)
- Full chain coverage: ETH / SOL / BSC / BASE
- Scans every 30 seconds, real-time push
- Automatic Twitter keyword extraction (Elon Musk / Trump / CZ / He Yi)
- Reverse Twitter detection: on-chain anomaly → search Twitter globally
- MEME signal scoring: extract "about to launch a coin" signals from tweets
- Safety check: honeypot / mintable token detection
- Deduplication: SQLite persistence

### Push Strategy

Only **★★★ narrative** gets pushed:

| Level | Category | Push |
|-------|----------|------|
| ★★★ | Musk/Trump | ✅ |
| ★★★ | Binance/CZ/He Yi | ✅ (BSC only) |
| ★★ | Celebrity/Viral | ❌ |
| ★ | No clear narrative | ❌ |

On-chain anomaly + classification mismatch → reverse Twitter check → tweet matched → upgraded to ★★★ and pushed.

### Setup

```bash
# 1. Install dependency
pip install requests

# 2. Configure Telegram Bot
# Create ~/.env:
echo 'TG_TOKEN=your_bot_token' >> ~/.env
echo 'TG_CHAT_ID=your_chat_id' >> ~/.env

# Get chat_id: send a message to your bot then visit
# https://api.telegram.org/bot<TOKEN>/getUpdates

# 3. Run
python3 narrative_radar.py
```

Run in background:
```bash
python3 -u narrative_radar.py > narrative_radar.log 2>&1 &
```

### Architecture

```
GMGN new tokens (ETH/SOL/BSC/BASE)  ──►  Momentum Tracker  ──►  Narrative Classification  ──►  TG Push
         │                                      │
         │                                      ├─ 3 consecutive up + >5% gain
         │                                      ├─ Safety check
         │                                      └─ Market cap $1K~$10M
         │
   Twitter RSS ──►  Forward: extract keywords every 2min ──►  Hot Keywords Pool
                                              │
   On-chain anomaly ──►  Reverse: Nitter global search ──►  Tweet matched → promoted
```

### Scoring System (extract_meme_signals)

| Category | Score | Description |
|----------|-------|-------------|
| Multi-word phrase | 15-20 | "freedom of money", "government efficiency" |
| Action phrase | 17 | "launching a new project", "building something" |
| Proper noun + $TICKER | 10-15 | "Melania", "Sam Altman", "$DOGE" |
| All-caps abbreviation | 10 | DOGE, FSD, XBNB |
| Controversy/emotion | 8-12 | "pardoned", "assassinated", "fraud" |
| Number signal | 5-8 | "$6 billion", "200%" |
| Single CapWord | 3-5 | Capitalized noun as potential token name |

### Data Sources

- **GMGN API**: Token rankings (by creation time + volume)
- **DEXScreener API**: Token descriptions, social links
- **Pump.fun API**: SOL token descriptions
- **Nitter RSS**: Twitter monitoring (4 accounts + global search)
- **RugCheck.xyz**: SOL safety check
- **GoPlus**: EVM safety check
- **SQLite**: History deduplication

### File Structure

```
~/crypto-trading/
├── narrative_radar.py    # Main program (1972 lines)
├── narrative_radar.log   # Runtime log
├── narrative_history.db  # SQLite database
├── narrative_seen.json   # Seen tokens cache
└── flap_seen.json        # FLAP tokens cache
```

### Notes

- Nitter may be unstable; occasional timeouts won't break the main loop
- SOL chain has too many MEME coins, gain threshold set to 15% (other chains: 5%)
- Reverse detection requires exact token keyword match to avoid false positives
- Telegram Markdown failure auto-falls back to plain text

---

**License**: MIT
