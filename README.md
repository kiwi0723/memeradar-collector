# MemeRadar Collector v6 — 链上叙事雷达

双源交叉验证信号采集器，实时扫描 GMGN + OKX 聪明钱/KOL 交易，通过 Hermes Agent 分析叙事后推送 Telegram。

## 核心特点

- **双源交叉验证**：GMGN（主源）+ OKX（验证源），OKX 确认加分
- **信号评分体系**：钱包数 × 量 × 标签稀有度 × 双源确认
- **Hermes 叙事分析**：web 搜索验证叙事（新闻/推特/KOL），去噪只推高质量信号
- **四条链全覆盖**：SOL / ETH / BASE / BSC
- **每 12 秒一轮**，实时采集

## 架构

```
GMGN (sm+kol trades)  ──┐
                         ├──► 信号聚类 + 评分 ──► Hermes 叙事分析 ──► ★★★ → TG 推送
OKX (smart_money trades) ─┘                              │
                                                    web_search
                                                   (新闻/推特)
```

## 推送策略

| 级别 | 条件 | 推送 |
|------|------|------|
| ★★★ | 有叙事/热度/KOL 提及 | ✅ 推 @memeranderbot |
| SKIP | 无叙事/死币/跑路 | ❌ 跳过 |

## 安装与配置

```bash
# 1. 安装依赖
pip install requests python-dotenv

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填入你的 TG bot token 和 chat_id

# 3. 运行
python3 collector.py
```

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `TG_PUSH_TOKEN` | Telegram Bot Token | - |
| `TG_PUSH_CHAT` | 推送目标 Chat ID | - |
| `HERMES_API` | Hermes API 地址 | `http://127.0.0.1:8642/v1/chat/completions` |
| `MAX_BUFFER_PER_TOKEN` | 每个代币最大缓存交易数 | `50` |

## 数据源

- **GMGN CLI**：聪明钱 + KOL 买入交易
- **OKX OnchainOS**：smart_money tracker 交叉验证
- **Hermes Agent**：web 搜索验证叙事和热度

## 文件结构

```
~/crypto-trading/
├── collector.py           # 主程序
├── collector_launcher.sh  # 启动脚本
├── .env.example           # 环境变量模板
├── .env                   # 环境变量（不提交 git）
├── .gitignore
└── collector.log          # 运行日志
```

## License

MIT
