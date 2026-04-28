#!/usr/bin/env python3
"""
叙事雷达 → 链上雷达 v1
纯Python，零AI成本（关键词匹配 + 叙事去重）

推送策略：只推★★★级叙事（马斯克/川普 + 币安/CZ/何一）
其他叙事仅分类记录，不推送。

数据源：GMGN新币 + DEXScreener搜索
叙事历史：SQLite去重
"""

import requests
import json
import time
import os
import re
import sqlite3
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from difflib import SequenceMatcher

# === 配置 ===
DATA_DIR = os.path.expanduser("~/crypto-trading")
DB_FILE = os.path.join(DATA_DIR, "narrative_history.db")
LOG_FILE = os.path.join(DATA_DIR, "narrative_radar.log")
SEEN_FILE = os.path.join(DATA_DIR, "narrative_seen.json")
FLAP_SEEN_FILE = os.path.join(DATA_DIR, "flap_seen.json")

# 扫描间隔
SCAN_INTERVAL = 30  # 30秒（GMGN数据约1-5分钟刷新一次，10秒太频繁且数据不变）

# 动量追踪器 — 内存中记录每个币的价格/市值快照
# {address: [{'ts': timestamp, 'mc': market_cap, 'vol': volume, 'price': price}, ...]}
MOMENTUM_TRACKER = {}
MOMENTUM_PUSHED = {}  # {address: {'count': N, 'last_ts': ts, 'last_mc': mc}} 推送计数
MOMENTUM_CONSECUTIVE_UP = 3  # 连续涨3轮（数据实际变化时才算一轮）

# 推特热点关键词 — 实时监控马斯克/川普/CZ/何一推文
# {'keyword': {'expire': expire_ts, 'category': 'musk_trump'|'binance_cz'}}
HOT_TWITTER_KEYWORDS = {}
HOT_KEYWORDS_TTL = 7200  # 秒
HOT_KEYWORDS_LAST_FETCH = 0
HOT_KEYWORDS_FETCH_INTERVAL = 120  # 每2分钟拉一次

# 监控账号
MONITORED_ACCOUNTS = {
    'elonmusk': 'musk_trump',
    'realDonaldTrump': 'musk_trump',
    'cz_binance': 'binance_cz',
    'heyibinance': 'binance_cz',
}

# 从.env读取TG配置
def load_env():
    env = {}
    env_file = os.path.expanduser("~/.env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if '=' in line and not line.startswith('#'):
                    k, v = line.split('=', 1)
                    env[k] = v
    return env

ENV = load_env()
TG_TOKEN = ENV.get('TG_TOKEN', '')
TG_CHAT_ID = int(ENV.get('TG_CHAT_ID', '0'))

GMGN_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Referer': 'https://gmgn.ai/',
}

# ============================================================
# 马斯克/川普关键词库（大小写不敏感）
# ============================================================
MUSK_TRUMP_KEYWORDS = {
    # 马斯克核心
    'musk', 'elon', 'elonmusk',
    # SpaceX/Tesla/X
    'spacex', 'starship', 'tesla', 'cybertruck', 'roadster',
    'neuralink', 'boring', 'hyperloop', 'xai', 'grok',
    # 马斯克相关人物/宠物/梗
    'floki', 'shiba',  # 只在新币上下文中用
    'doge father', 'dogefather', 'technoking',
    'mars colony', 'mars',
    # 川普核心
    'trump', 'donald', 'maga', 'potus', 'trump47',
    'melania', 'barron', 'ivanka',
    # 川普相关
    'dark maga', 'darkmaga', 'ultra maga', 'save america',
    'truth social', 'covfefe',
    # 马斯克+川普联动
    'doge department', 'd.o.g.e', 'government efficiency',
}

# 马斯克/川普正则（捕捉变体）
MUSK_TRUMP_PATTERNS = [
    r'\belon\b', r'\bmusk\b', r'\btrump\b', r'\bmaga\b',
    r'\bspacex\b', r'\bstarship\b', r'\btesla\b', r'\bgrok\b',
    r'\bmelania\b', r'\bbarron\b', r'\bdoge\s*department\b',
    r'\bd\.?o\.?g\.?e\b',  # D.O.G.E变体
    r'\bx\s*ai\b', r'\bneuralink\b',
]

# ============================================================
# 币安/CZ关键词库
# ============================================================
BINANCE_CZ_KEYWORDS = {
    # CZ核心
    'cz', 'changpeng', 'zhao', 'czb', 'czbinance',
    # 何一（BSC现在的核心推手！）
    'heyi', 'yi he', 'he yi', '何一', 'yihe',
    'sister yi', 'yi jie', '一姐', '何一姐',
    # 币安品牌
    'binance', 'bnb', 'pancake', 'pancakeswap',
    # CZ相关动态词（书、活动、推特高频词）
    'giggle academy', 'binance life', 'bnb chain',
    'principles', 'cz book',
    # YZi Labs (原Binance Labs)
    'yzi', 'yzi labs',
    # 中文关键词（BSC上常见）
    '赵长鹏', '币安', '长鹏', 'cz的', '何一的',
    # Four.meme平台相关
    'fourmeme', 'four meme', '4meme',
    # CZ/何一推特互动高频词
    'czs dog', 'cz dog', 'bnb dog',
    'build on bnb', 'bnb ecosystem',
}

BINANCE_CZ_PATTERNS = [
    r'\bcz\b', r'\bbinance\b', r'\bbnb\b',
    r'\bheyi\b', r'\byi\s*he\b', r'\bhe\s*yi\b',
    r'\b何一\b', r'\b一姐\b',
    r'\bpancake\b', r'\bgiggle\b', r'\byzi\b',
    r'\bfourmeme\b', r'\b4meme\b',
]

# ============================================================
# 推特热点/名人关键词库（★★级别）
# ============================================================
CELEBRITY_VIRAL_KEYWORDS = {
    # 科技名人
    'vitalik', 'buterin', 'sam altman', 'satoshi',
    'michael saylor', 'saylor', 'cathie wood',
    'jack dorsey', 'zuckerberg', 'bezos',
    'jensen huang', 'nvidia', 'tim cook',
    # 币圈名人
    'justin sun', 'sun yuchen', '孙宇晨', 'tron',
    'arthur hayes', 'su zhu', '3ac',
    'brian armstrong', 'coinbase',
    'larry fink', 'blackrock',
    'gary gensler', 'sec',
    'michael novogratz', 'galaxy',
    # 政治/社会名人
    'biden', 'obama', 'putin', 'xi jinping',
    'kanye', 'drake', 'snoop dogg', 'paris hilton',
    'mark cuban', 'mr beast', 'mrbeast',
    # 病毒式传播热词（龙虾级别的梗）
    'lobster', '龙虾', 'lobsta',
    'hawk tuah', 'griddy', 'skibidi',
    'rizz', 'sigma', 'gyatt',
    # 重大事件关键词
    'etf', 'halving', '减半',
    'world war', 'wwiii',
    'fed', 'rate cut', '降息',
    'tiktok ban', 'tiktok',
}

CELEBRITY_VIRAL_PATTERNS = [
    r'\bvitalik\b', r'\bsaylor\b', r'\bblackrock\b',
    r'\bcoinbase\b', r'\bjustin\s*sun\b', r'\blobster\b',
    r'\betf\b', r'\bhalving\b', r'\bmrbeast\b',
    r'\bsnoop\b', r'\bkanye\b', r'\bdrake\b',
]

# ============================================================
# 通用垃圾词（过滤明显的骗局/低质量币）
# ============================================================
SPAM_PATTERNS = [
    r'airdrop', r'presale', r'pre\s*sale',
    r'1000x', r'100x guaranteed',
    r'safe\s*moon', r'baby\s*\w+',  # babydoge等仿盘
    r'pornhub', r'porn', r'xxx', r'nsfw',
    r'nigga', r'nigger', r'faggot',
    r'scam', r'rugpull', r'rug\s*pull',
    r'official\s*token', r'official\s*coin',
]

# 常见无叙事意义的单词（过滤单词名币）
COMMON_NOISE_WORDS = {
    'nice', 'good', 'bad', 'cool', 'hot', 'big', 'small',
    'life', 'love', 'hate', 'happy', 'sad', 'fun', 'lol',
    'cat', 'dog', 'moon', 'sun', 'star', 'king', 'queen',
    'gold', 'rich', 'cash', 'money', 'pay', 'buy', 'sell',
    'pump', 'dump', 'bull', 'bear', 'green', 'red',
    'hello', 'world', 'yes', 'no', 'wow', 'omg', 'lmao',
    'simp', 'chad', 'based', 'cope', 'seethe',
    'test', 'new', 'old', 'real', 'fake',
    # 垃圾币名常见词
    'shit', 'shitcoin', 'fuck', 'fart', 'poop', 'pee',
    'cum', 'dick', 'ass', 'boob', 'tit',
    'nigga', 'retard', 'slop',
    # 超通用币名
    'the', 'and', 'for', 'from', 'with', 'this', 'that',
    'coin', 'token', 'meme', 'pepe', 'wojak',
    'peg', 'usd', 'usdt', 'usdc', 'dai',
}

# ============================================================
# 工具函数
# ============================================================
def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\
')

def load_flap_seen():
    if os.path.exists(FLAP_SEEN_FILE):
        try:
            with open(FLAP_SEEN_FILE) as f:
                return json.load(f)
        except:
            pass
    return {}

def save_flap_seen(data):
    # 只保留7天内的
    cutoff = int(time.time()) - 86400 * 7
    data = {k: v for k, v in data.items() if v > cutoff}
    with open(FLAP_SEEN_FILE, 'w') as f:
        json.dump(data, f)

def tg_send(text, parse_mode='Markdown'):
    if not TG_TOKEN:
        log(f"[TG] No token, skip: {text[:80]}")
        return False
    try:
        resp = requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT_ID, 'text': text, 'parse_mode': parse_mode},
            timeout=10
        )
        result = resp.json()
        if not result.get('ok'):
            # Markdown失败时降级到纯文本
            if 'can\'t parse' in str(result.get('description', '')).lower():
                resp = requests.post(
                    f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                    json={'chat_id': TG_CHAT_ID, 'text': text},
                    timeout=10
                )
            else:
                log(f"[TG] Error: {result.get('description', '')}")
                return False
        return True
    except Exception as e:
        log(f"[TG] Send error: {e}")
        return False

# ============================================================
# 叙事历史数据库
# ============================================================
def init_db():
    """初始化SQLite叙事历史库"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    # 所有见过的叙事主题
    c.execute('''CREATE TABLE IF NOT EXISTS narratives (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        theme TEXT NOT NULL,           -- 归一化的叙事主题（小写）
        first_token_name TEXT,         -- 第一次出现时的代币名
        first_token_address TEXT,      -- 第一次出现时的地址
        first_chain TEXT,              -- 第一次出现的链
        first_seen_at INTEGER,         -- 第一次看到的时间戳
        token_count INTEGER DEFAULT 1, -- 出现过多少次
        last_seen_at INTEGER           -- 最近一次看到
    )''')
    
    # 所有扫描过的代币
    c.execute('''CREATE TABLE IF NOT EXISTS tokens_seen (
        address TEXT PRIMARY KEY,
        chain TEXT,
        name TEXT,
        symbol TEXT,
        narrative_theme TEXT,
        category TEXT,                 -- 'musk_trump' / 'binance_cz' / 'novel' / 'common'
        first_seen_at INTEGER,
        market_cap REAL,
        pushed INTEGER DEFAULT 0,      -- 是否已推送
        seen_count INTEGER DEFAULT 1   -- 出现次数
    )''')
    
    # 索引
    c.execute('CREATE INDEX IF NOT EXISTS idx_theme ON narratives(theme)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_addr ON tokens_seen(address)')
    
    conn.commit()
    return conn

def normalize_theme(name, symbol):
    """
    从代币名称+符号提取归一化的叙事主题
    例如：'Elon Mars Colony' → 'elon mars colony'
          'TRUMP2028' → 'trump'
          'PancakeBunny' → 'pancake bunny'
    """
    # 合并name和symbol
    text = f"{name} {symbol}".lower().strip()
    
    # 去除常见后缀/前缀
    noise = ['token', 'coin', 'inu', 'swap', 'finance', 'protocol',
             'dao', 'defi', 'nft', 'meta', 'verse', 'fi', 'ai',
             'pepe', 'wojak', 'chad', 'based']
    
    # 分割camelCase
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    # 去除数字（如2028、1000x）
    text = re.sub(r'\d+x?', '', text)
    # 只保留字母和空格
    text = re.sub(r'[^a-z\s]', ' ', text)
    # 去噪
    words = [w for w in text.split() if w and len(w) > 1 and w not in noise]
    
    if not words:
        return name.lower().strip()
    
    return ' '.join(sorted(set(words)))

def is_similar_theme(theme1, theme2, threshold=0.7):
    """模糊匹配两个叙事主题"""
    if theme1 == theme2:
        return True
    # 子串匹配
    if theme1 in theme2 or theme2 in theme1:
        return True
    # 词重叠
    words1 = set(theme1.split())
    words2 = set(theme2.split())
    if words1 and words2:
        overlap = len(words1 & words2) / min(len(words1), len(words2))
        if overlap >= 0.6:
            return True
    # 序列匹配
    return SequenceMatcher(None, theme1, theme2).ratio() >= threshold

def check_narrative_novelty(conn, theme, name, symbol, address, chain):
    """
    检查叙事状态
    返回：
      ('novel', None)                    — 第一次见到
      ('heating', narrative_row)         — 短时间内持续出现新币！热点信号！
      ('existing', existing_theme_row)   — 已有叙事，不热
    
    核心逻辑：同一主题在30分钟内出现2+个不同的币 = 热点
    """
    c = conn.cursor()
    now = int(time.time())
    HEAT_WINDOW = 1800  # 30分钟窗口
    HEAT_THRESHOLD = 2  # 窗口内出现2个以上同主题币就是热点
    
    # 精确匹配
    c.execute('SELECT id, theme, first_token_name, first_token_address, first_chain, first_seen_at, token_count, last_seen_at FROM narratives WHERE theme = ?', (theme,))
    exact = c.fetchone()
    if exact:
        row_id, _, _, _, _, first_seen, count, last_seen = exact
        # 更新计数
        new_count = count + 1
        c.execute('UPDATE narratives SET token_count = ?, last_seen_at = ? WHERE theme = ?',
                  (new_count, now, theme))
        conn.commit()
        
        # 热点判断：在HEAT_WINDOW内出现了多个币
        if now - first_seen < HEAT_WINDOW and new_count >= HEAT_THRESHOLD:
            return ('heating', exact)
        # 或者：最近一次和这次间隔很短（说明持续在冒）
        if now - last_seen < HEAT_WINDOW and new_count >= HEAT_THRESHOLD:
            return ('heating', exact)
        
        return ('existing', exact)
    
    # 模糊匹配 — 取最近1000个主题比对
    c.execute('SELECT id, theme, first_token_name, first_token_address, first_chain, first_seen_at, token_count, last_seen_at FROM narratives ORDER BY last_seen_at DESC LIMIT 1000')
    for row in c.fetchall():
        if is_similar_theme(theme, row[1]):
            row_id, _, _, _, _, first_seen, count, last_seen = row
            new_count = count + 1
            c.execute('UPDATE narratives SET token_count = ?, last_seen_at = ? WHERE id = ?',
                      (new_count, now, row[0]))
            conn.commit()
            
            # 热点判断
            if now - last_seen < HEAT_WINDOW and new_count >= HEAT_THRESHOLD:
                return ('heating', row)
            
            return ('existing', row)
    
    # 第一次见到 — 记录
    c.execute('''INSERT INTO narratives (theme, first_token_name, first_token_address, first_chain, first_seen_at, last_seen_at)
                 VALUES (?, ?, ?, ?, ?, ?)''',
              (theme, name, address, chain, now, now))
    conn.commit()
    return ('novel', None)

def get_token_seen_count(conn, address):
    """获取代币出现次数"""
    c = conn.cursor()
    c.execute('SELECT seen_count FROM tokens_seen WHERE address = ?', (address,))
    row = c.fetchone()
    return row[0] if row else 0

def is_token_seen(conn, address):
    """检查代币是否已经扫描过"""
    c = conn.cursor()
    c.execute('SELECT address FROM tokens_seen WHERE address = ?', (address,))
    return c.fetchone() is not None

def record_token(conn, address, chain, name, symbol, theme, category, mc, pushed=False):
    """记录已扫描的代币 — 重复出现时计数+1"""
    c = conn.cursor()
    # 检查是否已存在
    c.execute('SELECT seen_count FROM tokens_seen WHERE address = ?', (address,))
    existing = c.fetchone()
    if existing:
        # 已存在：计数+1，更新市值
        new_count = existing[0] + 1
        c.execute('''UPDATE tokens_seen SET seen_count = ?, market_cap = ?, category = ?
                     WHERE address = ?''', (new_count, mc, category, address))
    else:
        # 新记录
        c.execute('''INSERT INTO tokens_seen 
                     (address, chain, name, symbol, narrative_theme, category, first_seen_at, market_cap, pushed, seen_count)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)''',
                  (address, chain, name, symbol, theme, category, int(time.time()), mc, 1 if pushed else 0))
    conn.commit()

# ============================================================
# 推特关键词监控
# ============================================================
def extract_meme_signals(text):
    """
    MEME 玩家视角的分析 —— 不是机械切词，而是找"会发币"的信号。

    评分体系（Meme 潜力强度）：
      多词短语/概念      15-20  — "freedom of money", "asset seizure"
      专有名词+$TICKER   10-15  — "Melania", "Sam Altman", "$DOGE"
      争议词/情绪词      8-12   — "assassinated", "pardoned", "fraud"
      数字信号           5-8    — "$6 billion", "200%"
      单 CapWord         3-5    — 大写单词，可能的自定义 ticker

    门槛：score >= 5 才写入，过滤掉日常用语。
    """
    signals = {}
    text_lower = text.lower()

    # ================================================================
    # 1. 多词短语/概念 (15-20分) — 最有 meme 潜力的完整概念
    # ================================================================

    # 1a. "X of Y" 结构
    for m in re.finditer(
        r'\b(freedom|seizure|confiscation|end|rise|fall|death|birth|war|peace|power|dream|age|era|cult|church|army|empire|kingdom|republic|revolution|movement|standard|currency|money|asset|wealth|truth|justice|liberty|tyranny|chaos|order|fate|destiny|glory|pride|shame|scandal|panic|fear|hope|love|hate|greed)\s+of\s+[a-z]+(?:\s+[a-z]+)?\b',
        text_lower
    ):
        phrase = m.group().strip()
        if len(phrase.split()) >= 2:
            signals[phrase] = max(signals.get(phrase, 0), 18)

    # 1b. "X and Y" 并列概念 + "'s 所有格
    for m in re.finditer(r'\b([a-z]+(?:\s+[a-z]+)?\s+and\s+[a-z]+(?:\s+[a-z]+)?)\b', text_lower):
        phrase = m.group().strip()
        words = phrase.split()
        if 3 <= len(words) <= 5:
            signals[phrase] = max(signals.get(phrase, 0), 15)

    for m in re.finditer(r"\b([a-z]+'s\s+[a-z]+(?:\s+[a-z]+)?)\b", text_lower):
        phrase = m.group().strip()
        if len(phrase.split()) >= 2:
            signals[phrase] = max(signals.get(phrase, 0), 15)

    # 1c. Gerund/Action 短语 — "building this", "launching something"
    for m in re.finditer(
        r'\b(creating|launching|building|burning|banning|fighting|surviving|killing|saving|breaking|making|taking|buying|selling|holding|releasing|announcing|teasing|hinting|developing|designing|minting|founding|leading|running|owning|ruling|conquering|destroying|ending|starting|changing|shaping|defining)\s+(?:the\s+|a\s+|an\s+)?(?:new\s+|next\s+|first\s+|last\s+)?[a-z]+(?:\s+[a-z]+)?\b',
        text_lower
    ):
        phrase = m.group().strip()
        if len(phrase.split()) >= 2:
            signals[phrase] = max(signals.get(phrase, 0), 17)

    # 1d. 特定高信号短语库（人工维护，持续补充）
    HIGH_SIGNAL_PHRASES = {
        # 马斯克/川普高信号
        'freedom of money': 20, 'asset seizure': 18,
        'government efficiency': 18, 'department of government efficiency': 20,
        'doge department': 18, 'd.o.g.e': 18,
        'fighting in the trenches': 16, 'nobody is using bitcoin': 16,
        'from prison and back': 18, 'crypto richest man to prison': 18,
        'building from zero': 15, 'record breaker': 14,
        'second inauguration': 15, 'first lady': 14,
        'dark maga': 18, 'save america': 16,
        'truth social': 15, 'covfefe': 16,
        'mars colony': 16, 'colonizing mars': 16,
        'billionaire feud': 16, 'crypto war': 15,
        # CZ/币安相关
        'bnb chain': 18, 'build n build': 15,
        'crypto freedom': 16, 'financial freedom': 15,
        'crypto adoption': 14, 'mass adoption': 14,
        # 通用高信号
        'made in china': 16, 'american made': 15,
        'new standard': 14, 'next generation': 14,
        'game changer': 15, 'paradigm shift': 15,
        'this is interesting': 12, 'something new': 12,
    }
    for phrase, score in HIGH_SIGNAL_PHRASES.items():
        if phrase in text_lower:
            signals[phrase] = max(signals.get(phrase, 0), score)

    # ================================================================
    # 2. 专有名词/人名 ($TICKER + 知名实体, 10-15分)
    # ================================================================

    # 2a. $TICKER 格式 — 最直接的发币信号
    for t in re.findall(r'\$([A-Z][A-Z0-9]{1,9})', text):
        signals[t.lower()] = max(signals.get(t.lower(), 0), 14)

    # 2b. 已知高信号实体
    HIGH_SIGNAL_ENTITIES = {
        'sam altman': 15, 'openai': 13,
        'melania': 15, 'ivanka': 13, 'barron': 12,
        'donald trump': 15, 'elon musk': 15,
        'cz binance': 15, 'heyibinance': 13, 'binance': 13,
        'kazakhstan': 11, 'almaty': 10,
        'vitalik': 13, 'satoshi': 13,
        'michael saylor': 12, 'jack dorsey': 12,
        'mark zuckerberg': 12, 'zuck': 11,
    }
    for entity, score in HIGH_SIGNAL_ENTITIES.items():
        if entity in text_lower:
            signals[entity] = max(signals.get(entity, 0), score)

    # 2c. CapWord 探测 — 潜在的新币名/ticker（专有名词风格的大写词）
    # 排除句首大写、常见词、短词
    capwords = re.findall(r'\b([A-Z][a-z]{2,9})\b', text)
    COMMON_CAPWORDS = {
        'The', 'This', 'That', 'What', 'When', 'Where', 'Which', 'Who',
        'How', 'Why', 'And', 'For', 'But', 'Not', 'Are', 'Was', 'Has',
        'Had', 'Can', 'Will', 'With', 'From', 'They', 'Have', 'Been',
        'Just', 'All', 'New', 'Now', 'Get', 'Got', 'One', 'Two', 'Three',
        'First', 'Last', 'Next', 'Best', 'Great', 'Good', 'Well',
        'Here', 'There', 'More', 'Most', 'Some', 'Any', 'Many', 'Much',
        'Very', 'Even', 'Still', 'Also', 'Only', 'Just', 'Like', 'Make',
        'Made', 'Take', 'Took', 'Come', 'Came', 'Go', 'Went', 'See',
        'Say', 'Said', 'Know', 'Think', 'Want', 'Need', 'Let', 'Look',
        'Back', 'Down', 'Away', 'Out', 'Over', 'Again', 'Then', 'Than',
        'Also', 'Because', 'Before', 'After', 'Since', 'Until', 'While',
        'Every', 'Each', 'Both', 'Few', 'More', 'Less', 'Other', 'Another',
        'Photo', 'Video', 'Link', 'Post', 'Tweet', 'Thread',
    }
    for cap in capwords:
        if cap not in COMMON_CAPWORDS:
            # 出现在引号/上下文中的更有可能是币名
            if f'"{cap}"' in text or f"'{cap}'" in text:
                signals[cap.lower()] = max(signals.get(cap.lower(), 0), 12)
            else:
                signals[cap.lower()] = max(signals.get(cap.lower(), 0), 5)

    # 2d. 全大写缩写 (3-6字母) — 可能是 ticker
    STOP_ABBR = {'THE','AND','FOR','BUT','NOT','ARE','WAS','HAS','HAD','CAN',
                 'WILL','WITH','THAT','THIS','FROM','THEY','HAVE','BEEN','WHEN',
                 'WHAT','WHICH','THERE','THEIR','WOULD','ABOUT','COULD','SHOULD',
                 'HTTP','HTTPS','VIA','CEO','USA','NEW','JUST','ALL'}
    for a in re.findall(r'\b([A-Z]{3,8})\b', text):
        if a not in STOP_ABBR and not a.isdigit():
            signals[a.lower()] = max(signals.get(a.lower(), 0), 10)

    # ================================================================
    # 3. 争议词/情绪词 (8-12分)
    # ================================================================
    CONTROVERSY_WORDS = {
        'assassinated': 12, 'assassination': 12, 'survived': 11, 'survivor': 10,
        'pardoned': 12, 'pardon': 11, 'prison': 12, 'jail': 11,
        'deranged': 10, 'fraud': 10, 'scam': 9, 'corrupt': 10, 'corruption': 10,
        'seizure': 11, 'confiscation': 11, 'opaque': 10,
        'censorship': 11, 'endangering': 10, 'doom': 10,
        'feud': 10, 'enemy': 10, 'rival': 9,
        'war': 10, 'battle': 9, 'trenches': 11,
        'freedom': 11, 'liberty': 10, 'tyranny': 11,
        'ban': 9, 'banning': 9, 'banned': 9,
        'revolution': 11, 'movement': 10, 'uprising': 10,
        'rebellion': 10, 'resistance': 10,
        'wealth': 8, 'power': 8, 'glory': 8,
        'panic': 9, 'fear': 8, 'greed': 8,
        'moon': 8, 'lambo': 8,
    }
    for word, score in CONTROVERSY_WORDS.items():
        if re.search(r'\b' + re.escape(word) + r'\b', text_lower):
            signals[word] = max(signals.get(word, 0), score)

    # ================================================================
    # 4. 数字信号 (5-8分)
    # ================================================================
    dollar_amounts = re.findall(r'\$(\d+(?:\.\d+)?)\s*(billion|million|B|M)',
                                text_lower)
    for amt, unit in dollar_amounts:
        kw = f"${amt}{unit}"
        score = 8 if unit in ('billion', 'B') else 6
        signals[kw] = max(signals.get(kw, 0), score)

    for p in re.findall(r'(\d+)%', text):
        val = int(p)
        if val >= 100:
            signals[f"{val}%"] = max(signals.get(f"{val}%", 0), 6)

    # ================================================================
    # 5. 去重 + 过滤噪声
    # ================================================================
    NOISE = {
        'just', 'only', 'more', 'less', 'most', 'some', 'any', 'many',
        'much', 'very', 'also', 'even', 'ever', 'never', 'still',
        'after', 'before', 'first', 'last', 'next', 'then', 'than',
        'into', 'onto', 'upon', 'over', 'under', 'each', 'every',
        'other', 'another', 'their', 'because', 'which', 'where',
        'there', 'here', 'said', 'like', 'make', 'made', 'take',
        'took', 'come', 'came', 'went', 'seen', 'see', 'got', 'get',
        'one', 'two', 'three', 'best', 'better', 'right', 'left',
        'good', 'great', 'well', 'back', 'down', 'away', 'out',
        'video', 'photo', 'link', 'post', 'tweet', 'thread', 'reply',
        'today', 'yesterday', 'tomorrow', 'morning', 'evening', 'night',
        'people', 'person', 'thing', 'something', 'everything', 'nothing',
        'really', 'actually', 'literally', 'basically', 'honestly',
        'going', 'doing', 'saying', 'thinking', 'wanting', 'getting',
        'know', 'think', 'want', 'need', 'let', 'look', 'feel',
        'yes', 'no', 'ok', 'okay', 'lol', 'lmao', 'lmfao', 'omg',
        'thanks', 'thank', 'welcome', 'please', 'sorry',
        'minute', 'hour', 'day', 'week', 'month', 'year',
        'big', 'small', 'huge', 'massive', 'tiny',
    }

    # 门槛 score >= 5
    signals = {k: v for k, v in signals.items()
               if v >= 5 and k not in NOISE and len(k) >= 2}

    return signals

def fetch_twitter_keywords():
    """拉取监控账号最新推文，提取关键词注入 HOT_TWITTER_KEYWORDS"""
    global HOT_TWITTER_KEYWORDS, HOT_KEYWORDS_LAST_FETCH
    
    now = time.time()
    if now - HOT_KEYWORDS_LAST_FETCH < HOT_KEYWORDS_FETCH_INTERVAL:
        return  # 还没到拉取间隔
    
    HOT_KEYWORDS_LAST_FETCH = now
    
    # 清理过期关键词
    HOT_TWITTER_KEYWORDS = {k: v for k, v in HOT_TWITTER_KEYWORDS.items() if v['expire'] > now}
    
    import xml.etree.ElementTree as ET
    
    headers = {'User-Agent': 'RSS Reader'}
    new_count = 0
    
    for account, category in MONITORED_ACCOUNTS.items():
        try:
            url = f'https://nitter.net/{account}/rss'
            resp = requests.get(url, headers=headers, timeout=12)
            if resp.status_code != 200:
                continue
            
            root = ET.fromstring(resp.text)
            items = root.findall('.//item')[:8]  # 每人取最新8条
            
            for item in items:
                title_el = item.find('title')
                desc_el = item.find('description')
                title = title_el.text if title_el is not None and title_el.text else ''
                desc = desc_el.text if desc_el is not None and desc_el.text else ''
                
                # 清洗HTML
                import html
                desc = html.unescape(desc)
                desc = re.sub(r'<[^>]+>', '', desc)
                desc = re.sub(r'https?://\S+', '', desc)
                
                combined = f"{title} {desc}"
                kws = extract_meme_signals(combined)
                
                for kw, score in kws.items():
                    if score < 5:  # 只取 meme 强度 ≥5 的关键词
                        continue
                    if kw not in HOT_TWITTER_KEYWORDS:
                        HOT_TWITTER_KEYWORDS[kw] = {'expire': now + HOT_KEYWORDS_TTL, 'category': category}
                        new_count += 1
                    elif score > HOT_TWITTER_KEYWORDS[kw].get('score', 5):
                        # 如果新推文的词分数更高，更新时间
                        HOT_TWITTER_KEYWORDS[kw] = {'expire': now + HOT_KEYWORDS_TTL, 'category': category, 'score': score}
        except Exception as e:
            log(f"[推特] {account} 拉取失败: {e}")
    
    if new_count > 0:
        log(f"[推特] 本轮新增 {new_count} 个热点关键词，总计 {len(HOT_TWITTER_KEYWORDS)} 个")

def reverse_twitter_check(name, symbol, chain, description=''):
    """
    🔄 反向检测 v2：链上动量触发时，立刻查推特。

    两个层面：
    A — 全局推特搜索：用 Nitter 搜索 token name/symbol，不限账号
    B — 模糊语义匹配：不光精确匹配，还做部分词匹配 + description 补充

    返回：{'category': 'musk_trump'|'binance_cz', 'keywords': [...], 'account': str, 'snippet': str, 'source': 'search'|'monitored'} 或 None
    """
    import xml.etree.ElementTree as ET
    import html as html_mod
    headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
    
    # 从 token 名 + symbol 提取搜索关键词
    text = f"{name} {symbol}".lower()
    # 噪音词（只用于分词时跳过，不影响精确匹配）
    noise_words = {'token', 'coin', 'inu', 'swap', 'finance', 'protocol',
                   'dao', 'defi', 'nft', 'meta', 'verse', 'fi', 'ai',
                   'pepe', 'wojak', 'chad', 'based', 'the', 'and', 'for',
                   'cat', 'dog', 'moon', 'sun', 'king', 'meme'}
    words = re.findall(r'[a-z]{2,}', text)
    token_keywords = [w for w in words if w not in noise_words]

    if not token_keywords:
        return None

    # 构建搜索关键词：用 token 名中最重要的词
    # 优先用 symbol（通常是小写 ticker），其次是 name 中的非噪音词
    search_terms = list(set(token_keywords[:5]))
    # 如果 name 本身有多个词，也搜索完整 name
    orig_name_lower = name.lower().strip()
    if len(orig_name_lower.split()) >= 2 and orig_name_lower not in noise_words:
        search_terms.append(orig_name_lower)

    log(f"[反向检测] {name}({symbol}) on {chain} 链上异动，推特搜索: {search_terms[:3]}...")

    def _search_twitter(query, max_results=15):
        """Nitter 搜索，返回原始推文列表"""
        tweets = []
        try:
            # Nitter 搜索：q= 参数
            url = f'https://nitter.net/search?f=tweets&q={requests.utils.quote(query)}'
            resp = requests.get(url, headers={**headers, 'Accept': 'text/html'}, timeout=15)
            if resp.status_code != 200:
                return tweets
            
            # Nitter 搜索返回 HTML，用简单 regex 提取推文
            html_text = resp.text
            
            # 提取推文卡片 — Nitter HTML 结构
            # 每个推文在 <div class="timeline-item"> 中
            tweet_blocks = re.findall(
                r'<div class="timeline-item[^"]*".*?</div>\s*</div>\s*</div>\s*</div>',
                html_text, re.DOTALL
            )
            
            for block in tweet_blocks[:max_results]:
                # 提取用户名
                user_match = re.search(r'<a[^>]*class="username"[^>]*>([^<]+)</a>', block)
                user = html_mod.unescape(user_match.group(1).strip()) if user_match else ''
                
                # 提取推文内容（tweet-content）
                content_match = re.search(
                    r'<div class="tweet-content[^"]*"[^>]*>(.*?)</div>',
                    block, re.DOTALL
                )
                if not content_match:
                    continue
                tweet_text_raw = content_match.group(1)
                # 清洗 HTML
                tweet_text = html_mod.unescape(tweet_text_raw)
                tweet_text = re.sub(r'<br\s*/?>', ' ', tweet_text)
                tweet_text = re.sub(r'<[^>]+>', '', tweet_text)
                tweet_text = re.sub(r'https?://\S+', '', tweet_text)
                tweet_text = re.sub(r'\s+', ' ', tweet_text).strip()
                
                if not tweet_text or len(tweet_text) < 10:
                    continue
                
                tweets.append({
                    'user': user,
                    'text': tweet_text,
                    'text_lower': tweet_text.lower(),
                })
        except Exception as e:
            log(f"[反向检测] 搜索 '{query}' 失败: {e}")
        
        return tweets

    def _score_tweet(tweet_text, search_terms, token_keywords, description):
        """
        给一条推文的"相关性 + meme 潜力"打分。

        核心原则：**没有精确匹配 = 不相关 = 0分**
        MEME信号、description匹配等只能作为精确匹配之上的加分项。

        匹配方式（从强到弱）：
        1. 精确匹配 token name 完整词（必要前置条件）
        2. 精确匹配 symbol
        3. token 名作为子串出现在推文关键词中（"doge" in "doge department"）
        4. description 中的词出现在推文中（加分项）
        5. 推文高信号词（加分项）
        """
        tl = tweet_text.lower()
        matched = set()
        score = 0
        
        # 1. 精确匹配 token 关键词（必要前置条件）
        for kw in token_keywords:
            if len(kw) >= 3 and re.search(r'\b' + re.escape(kw) + r'\b', tl):
                matched.add(kw)
                score += 15  # 精确匹配高分
        
        # 2. 精确匹配完整 name（多词 token 名）
        orig_name = ' '.join(token_keywords)
        if orig_name in tl:
            matched.add(orig_name)
            score += 20
        
        # ❗ 没有精确匹配 → 直接返回 0
        # 避免完全不相关的 token 靠 MEME 信号词误判
        if score < 15:
            return 0, set()
        
        # 3. 子串匹配 — token 关键词是推文某词的一部分
        # 比如 token 叫 "doge" 而推文说 "doge department"
        for kw in token_keywords:
            if len(kw) >= 4 and kw not in matched:
                if re.search(r'\b[a-z]*' + re.escape(kw) + r'[a-z]*\b', tl):
                    matched.add(f"{kw}*")
                    score += 10
        
        # 4. description 补充匹配（加分项）
        if description:
            desc_words = set(re.findall(r'[a-z]{4,}', description.lower()))
            # 去噪音
            desc_words -= {'with', 'this', 'that', 'from', 'they', 'have',
                          'been', 'will', 'what', 'when', 'where', 'which',
                          'token', 'coin', 'meme', 'project', 'community'}
            for dw in desc_words:
                if dw in tl:
                    matched.add(f"desc:{dw}")
                    score += 5
        
        # 5. MEME 信号评分 — 推文本身体现的 meme 潜力（加分项）
        meme_sigs = extract_meme_signals(tweet_text)
        for sig, sig_score in meme_sigs.items():
            if sig_score >= 10:  # 高信号短语
                matched.add(f"sig:{sig}")
                score += sig_score // 3  # 降低 MEME 信号权重
        
        return score, matched

    # ================================================================
    # A: 全局推特搜索（不限账号）
    # ================================================================
    best_result = None  # (score, matched_keywords, user, snippet, source)
    
    for term in search_terms[:3]:  # 最多搜3个关键词
        tweets = _search_twitter(term)
        for tw in tweets:
            s, matched = _score_tweet(tw['text'], search_terms, token_keywords, description)
            if s > 0:
                if best_result is None or s > best_result[0]:
                    best_result = (
                        s, list(matched)[:6], tw['user'],
                        tw['text'][:100], 'search'
                    )
    
    # ================================================================
    # B: 也查监控账号（原有逻辑的增强版）
    # ================================================================
    for account, category in MONITORED_ACCOUNTS.items():
        try:
            url = f'https://nitter.net/{account}/rss'
            resp = requests.get(url, headers=headers, timeout=12)
            if resp.status_code != 200:
                continue
            
            root = ET.fromstring(resp.text)
            items = root.findall('.//item')[:8]  # 放宽到8条
            
            for item in items:
                title_el = item.find('title')
                desc_el = item.find('description')
                title = title_el.text if title_el is not None and title_el.text else ''
                desc = desc_el.text if desc_el is not None and desc_el.text else ''
                
                desc = html_mod.unescape(desc)
                desc = re.sub(r'<[^>]+>', '', desc)
                desc = re.sub(r'https?://\S+', '', desc)
                
                tweet_text = f"{title} {desc}"
                s, matched = _score_tweet(tweet_text, search_terms, token_keywords, description)
                
                if s > 0:
                    # 监控账号命中加成
                    s += 25  # 大V推文权重更高
                    if best_result is None or s > best_result[0]:
                        best_result = (
                            s, list(matched)[:6], f"@{account}",
                            (title or desc)[:100], 'monitored'
                        )
        except Exception as e:
            log(f"[反向检测] {account} 查询失败: {e}")
    
    if best_result:
        score, keywords, user, snippet, source = best_result
        log(f"[反向检测] ✅ 命中！来源={source} 用户={user} 分数={score}")
        
        # 判断分类
        category = 'musk_trump'  # 默认
        if source == 'monitored':
            category = MONITORED_ACCOUNTS.get(user.replace('@', ''), 'musk_trump')
        else:
            # 全局搜索命中：从推文内容判断
            tl = snippet.lower()
            if any(kw in tl for kw in ['bnb', 'binance', 'cz', 'heyibinance']):
                category = 'binance_cz'
            elif any(kw in tl for kw in ['musk', 'elon', 'trump', 'donald']):
                category = 'musk_trump'
        
        return {
            'category': category,
            'keywords': keywords,
            'account': user,
            'snippet': snippet,
            'source': source,
        }
    
    log(f"[反向检测] ❌ 未命中任何推文")
    return None

# ============================================================
# 叙事分类引擎
# ============================================================
def classify_narrative(name, symbol, chain, description=''):
    """
    分类代币叙事
    返回：('musk_trump', matched_keywords) / ('binance_cz', matched_keywords) / ('novel', None) / ('common', None)
    """
    text = f"{name} {symbol}".lower()
    text_with_desc = f"{name} {symbol} {description}".lower()
    
    # 1. 检查是否是垃圾币
    for pat in SPAM_PATTERNS:
        if re.search(pat, text_with_desc, re.IGNORECASE):
            return ('spam', None)
    
    # 0. 先检查推特热点关键词（优先级最高）
    matched_hot_mt = []
    matched_hot_bc = []
    for kw, info in list(HOT_TWITTER_KEYWORDS.items()):
        if info['expire'] <= time.time():
            continue
        if kw.lower() in text_with_desc:
            if info['category'] == 'musk_trump':
                matched_hot_mt.append(kw)
            elif info['category'] == 'binance_cz':
                matched_hot_bc.append(kw)
    
    if matched_hot_mt:
        chain_lower = chain.lower()
        if chain_lower in ('eth', 'ethereum', 'sol', 'solana', 'bsc', 'base'):
            return ('musk_trump', matched_hot_mt[:5])
    if matched_hot_bc:
        chain_lower = chain.lower()
        if chain_lower in ('bsc',):
            return ('binance_cz', matched_hot_bc[:5])
        else:
            return ('binance_cz_wrong_chain', matched_hot_bc[:5])
    
    # 2. 马斯克/川普检测（关键词库 + 描述）
    matched_mt = []
    for kw in MUSK_TRUMP_KEYWORDS:
        if kw.lower() in text_with_desc:
            matched_mt.append(kw)
    if not matched_mt:
        for pat in MUSK_TRUMP_PATTERNS:
            m = re.search(pat, text_with_desc, re.IGNORECASE)
            if m:
                matched_mt.append(m.group())
    
    if matched_mt:
        # 马斯克/川普：重点ETH+SOL，BSC也可以
        chain_lower = chain.lower()
        if chain_lower in ('eth', 'ethereum', 'sol', 'solana', 'bsc', 'base'):
            return ('musk_trump', matched_mt)
    
    # 3. 币安/CZ检测 — 只在BSC上推
    matched_bc = []
    for kw in BINANCE_CZ_KEYWORDS:
        if kw.lower() in text_with_desc:
            matched_bc.append(kw)
    if not matched_bc:
        for pat in BINANCE_CZ_PATTERNS:
            m = re.search(pat, text_with_desc, re.IGNORECASE)
            if m:
                matched_bc.append(m.group())
    
    if matched_bc:
        chain_lower = chain.lower()
        if chain_lower in ('bsc',):
            return ('binance_cz', matched_bc)
        else:
            return ('binance_cz_wrong_chain', matched_bc)
    
    # 4. 名人/推特热点检测（★★级别）
    matched_cv = []
    for kw in CELEBRITY_VIRAL_KEYWORDS:
        if kw.lower() in text_with_desc:
            matched_cv.append(kw)
    if not matched_cv:
        for pat in CELEBRITY_VIRAL_PATTERNS:
            m = re.search(pat, text_with_desc, re.IGNORECASE)
            if m:
                matched_cv.append(m.group())
    
    if matched_cv:
        return ('celebrity_viral', matched_cv)
    
    # 5. 都不匹配 → 需要进一步检查是否全新叙事
    return ('check_novelty', None)

# ============================================================
# 安全检查（复用现有逻辑）
# ============================================================
def check_token_safety(chain, address):
    """快速安全检查 — 只拦硬伤（蜜罐/可增发），卖税不作为否决条件"""
    if chain in ('sol', 'solana'):
        try:
            r = requests.get(f'https://api.rugcheck.xyz/v1/tokens/{address}/report', timeout=10)
            if r.status_code == 200:
                data = r.json()
                score = data.get('score', 999)
                mint = data.get('mintAuthority')
                freeze = data.get('freezeAuthority')
                return {
                    'safe': not mint and not freeze,
                    'score': score, 'mint': mint is not None,
                    'freeze': freeze is not None
                }
        except:
            pass
    else:
        chain_map = {'ethereum': '1', 'eth': '1', 'bsc': '56', 'base': '8453'}
        cid = chain_map.get(chain, '1')
        try:
            r = requests.get(f'https://api.gopluslabs.io/api/v1/token_security/{cid}?contract_addresses={address}', timeout=10)
            if r.status_code == 200:
                result = r.json().get('result', {})
                data = result.get(address.lower(), {})
                if data:
                    honeypot = data.get('is_honeypot', '0') == '1'
                    mintable = data.get('is_mintable', '0') == '1'
                    sell_tax = float(data.get('sell_tax', '0') or '0')
                    buy_tax = float(data.get('buy_tax', '0') or '0')
                    return {
                        'safe': not honeypot and not mintable,  # 卖税不作为否决
                        'honeypot': honeypot, 'mintable': mintable,
                        'sell_tax': sell_tax, 'buy_tax': buy_tax
                    }
        except:
            pass
    return {'safe': False, 'reason': '无法检查'}  # 无法检查时不推，宁可错过不踩坑

# ============================================================
# GMGN数据获取
# ============================================================
def gmgn_get(url):
    try:
        resp = requests.get(url, headers=GMGN_HEADERS, timeout=15)
        if resp.status_code == 200:
            return resp.json().get('data', {})
    except:
        pass
    return {}

def fetch_token_description(chain, address):
    """获取代币描述/故事 — 叙事雷达核心信息"""
    desc = ''
    
    # SOL链：Pump.fun有最完整的description
    if chain in ('sol', 'solana'):
        try:
            r = requests.get(f'https://frontend-api-v3.pump.fun/coins/{address}', timeout=8)
            if r.status_code == 200:
                data = r.json()
                desc = data.get('description', '') or ''
                twitter = data.get('twitter', '') or ''
                telegram = data.get('telegram', '') or ''
                website = data.get('website', '') or ''
                return {
                    'description': desc.strip(),
                    'twitter': twitter,
                    'telegram': telegram,
                    'website': website,
                }
        except:
            pass
    
    # 所有链：DEXScreener info字段（网站+社交链接）
    try:
        chain_dex = {'sol': 'solana', 'eth': 'ethereum', 'bsc': 'bsc', 'base': 'base',
                     'solana': 'solana', 'ethereum': 'ethereum'}.get(chain, chain)
        r = requests.get(f'https://api.dexscreener.com/latest/dex/tokens/{address}', timeout=8)
        if r.status_code == 200:
            pairs = r.json().get('pairs', [])
            if pairs:
                info = pairs[0].get('info', {})
                websites = info.get('websites', [])
                socials = info.get('socials', [])
                twitter = ''
                telegram = ''
                website = ''
                for s in socials:
                    if s.get('type') == 'twitter':
                        twitter = s.get('url', '')
                    elif s.get('type') == 'telegram':
                        telegram = s.get('url', '')
                for w in websites:
                    if w.get('label', '').lower() == 'website':
                        website = w.get('url', '')
                if not desc:
                    # DEXScreener没有description但有社交信息
                    return {
                        'description': desc,
                        'twitter': twitter,
                        'telegram': telegram,
                        'website': website,
                    }
    except:
        pass
    
    return {'description': desc, 'twitter': '', 'telegram': '', 'website': ''}

def fetch_new_tokens():
    """从GMGN获取各链新币 + 多维度覆盖"""
    all_tokens = []
    seen_addrs = set()
    
    for chain in ['eth', 'bsc', 'base', 'sol']:
        # 多维度拉数据，避免漏掉
        urls = [
            # 按创建时间 — 最新的币
            f'https://gmgn.ai/defi/quotation/v1/rank/{chain}/swaps/1h?orderby=open_timestamp&direction=desc&limit=100',
            # 按交易量 — 最活跃的币
            f'https://gmgn.ai/defi/quotation/v1/rank/{chain}/swaps/1h?orderby=swaps&direction=desc&limit=50',
        ]
        
        for url in urls:
            data = gmgn_get(url)
            tokens = data.get('rank', [])
            
            for t in tokens:
                addr = t.get('address', '')
                if not addr or addr in seen_addrs:
                    continue
                
                mc = t.get('market_cap', 0) or t.get('fdv', 0) or 0
                liq = t.get('liquidity', 0) or 0
                
                # 基本过滤：太小的不看
                if mc < 1000 or liq < 500 or mc > 10000000:
                    continue
                
                age_ts = t.get('open_timestamp', 0)
                age_h = (time.time() - age_ts) / 3600 if age_ts > 0 else 999
                
                # 不限年龄 — 动量追踪核心逻辑：涨就推，不管新旧
                
                seen_addrs.add(addr)
                all_tokens.append({
                    'address': addr,
                    'chain': chain,
                    'name': t.get('name', '?'),
                    'symbol': t.get('symbol', '?'),
                    'mc': mc,
                    'liq': liq,
                    'volume': t.get('volume', 0) or 0,
                    'holders': t.get('holder_count', 0) or 0,
                    'sm': t.get('smart_degen_count', 0) or 0,
                    'chg_1h': t.get('price_change_percent1h', 0) or 0,
                    'chg_24h': t.get('price_change_percent', 0) or 0,
                    'age_h': age_h,
                    'price': t.get('price', 0),
                    'buys_1h': t.get('buys', 0) or 0,
                    'sells_1h': t.get('sells', 0) or 0,
                })
            
            time.sleep(0.3)
    
    return all_tokens

def fetch_flap_tokens():
    """
    FLAP平台扫描 — BSC社区驱动型发射台
    找形态：跌下来但有底部支撑（有庄在低位推）
    特征：24h跌了，但1h企稳/反弹，买入>卖出，holders在涨
    """
    data = gmgn_get(
        'https://gmgn.ai/defi/quotation/v1/rank/bsc/swaps/24h?launchpad=flap&orderby=volume&direction=desc&limit=30'
    )
    tokens = data.get('rank', [])
    
    candidates = []
    for t in tokens:
        addr = t.get('address', '')
        if not addr:
            continue
        
        mc = t.get('market_cap', 0) or 0
        liq = t.get('liquidity', 0) or 0
        vol = t.get('volume', 0) or 0
        holders = t.get('holder_count', 0) or 0
        buys = t.get('buys', 0) or 0
        sells = t.get('sells', 0) or 0
        chg_1h = t.get('price_change_percent1h', 0) or 0
        chg_24h = t.get('price_change_percent', 0) or 0
        age_ts = t.get('open_timestamp', 0)
        age_h = (time.time() - age_ts) / 3600 if age_ts > 0 else 0
        
        # 基本门槛
        if mc < 1000 or liq < 500:
            continue
        if holders < 5:
            continue
        
        # 底部支撑形态判断：
        # 条件1: 24h跌了（或者涨幅有限），说明不是刚拉的
        # 条件2: 1h跌幅小于24h跌幅，说明在企稳
        # 条件3: 买入 > 卖出，有人在接
        buy_ratio = buys / max(sells, 1)
        
        is_support = False
        reason = ''
        
        # 形态A: 24h跌了，1h在企稳/反弹
        if chg_24h < -10 and chg_1h > chg_24h * 0.3:
            is_support = True
            reason = f'24h跌{chg_24h:.0f}%但1h企稳{chg_1h:+.0f}%'
        
        # 形态B: 24h微跌或横盘，1h微涨，买卖比健康
        if -10 <= chg_24h <= 30 and chg_1h > -5 and buy_ratio > 1.1:
            is_support = True
            reason = f'底部横盘 买卖比{buy_ratio:.2f}'
        
        # 形态C: 大跌后强反弹
        if chg_24h < -30 and chg_1h > 10:
            is_support = True
            reason = f'大跌{chg_24h:.0f}%后反弹{chg_1h:+.0f}%'
        
        if is_support and buy_ratio >= 1.0:
            candidates.append({
                'address': addr,
                'chain': 'bsc',
                'name': t.get('name', '?'),
                'symbol': t.get('symbol', '?'),
                'mc': mc,
                'liq': liq,
                'volume': vol,
                'holders': holders,
                'sm': 0,
                'chg_1h': chg_1h,
                'chg_24h': chg_24h,
                'age_h': age_h,
                'price': t.get('price', 0),
                'buys': buys,
                'sells': sells,
                'buy_ratio': buy_ratio,
                'support_reason': reason,
                'launchpad': 'flap',
            })
    
    # 按市值排序
    candidates.sort(key=lambda x: x['mc'], reverse=True)
    return candidates

def format_flap_alert(token, desc_info=None):
    """FLAP低吸信号推送"""
    msg = f"链上雷达 — FLAP低吸信号\
"
    msg += f"链: BSC | 平台: FLAP\
\
"
    msg += f"{token['name']} ({token['symbol']})\
"
    msg += f"`{token['address']}`\
\
"
    
    # 故事描述
    desc = (desc_info or {}).get('description', '')
    if desc:
        if len(desc) > 200:
            desc = desc[:200] + '...'
        msg += f"故事: {desc}\
\
"
    
    msg += f"形态: {token['support_reason']}\
\
"
    msg += f"```\
"
    msg += f"市值     ${token['mc']:>12,.0f}\
"
    msg += f"流动性   ${token['liq']:>12,.0f}\
"
    msg += f"24h量    ${token['volume']:>12,.0f}\
"
    msg += f"持有人   {token['holders']:>12,d}\
"
    msg += f"买/卖    {token['buys']:>6,d}/{token['sells']:>6,d}\
"
    msg += f"买卖比   {token['buy_ratio']:>12.2f}\
"
    msg += f"1h涨幅   {token['chg_1h']:>+11.1f}%\
"
    msg += f"24h涨幅  {token['chg_24h']:>+11.1f}%\
"
    msg += f"```\
"
    msg += "\
FLAP社区币 — 低吸进场信号"
    
    # 社交链接
    links = []
    if (desc_info or {}).get('twitter'):
        links.append(f"\
Twitter: {desc_info['twitter']}")
    if (desc_info or {}).get('telegram'):
        links.append(f"TG: {desc_info['telegram']}")
    if (desc_info or {}).get('website'):
        links.append(f"Web: {desc_info['website']}")
    if links:
        msg += '\
'.join(links)
    
    return msg

# ============================================================
# 推送格式
# ============================================================
def format_musk_trump_alert(token, matched_kw, desc_info=None):
    """马斯克/川普叙事推送"""
    chain_map = {'sol': 'SOL', 'eth': 'ETH', 'bsc': 'BSC', 'base': 'BASE'}
    ch = chain_map.get(token['chain'], token['chain'].upper())
    
    msg = f"链上雷达 — 马斯克/川普概念\
"
    msg += f"链: {ch}\
\
"
    msg += f"{token['name']} ({token['symbol']})\
"
    msg += f"`{token['address']}`\
\
"
    
    # 叙事故事（核心！）
    desc = (desc_info or {}).get('description', '')
    if desc:
        # 截取前200字符，避免太长
        if len(desc) > 200:
            desc = desc[:200] + '...'
        msg += f"故事: {desc}\
\
"
    
    msg += f"命中关键词: {', '.join(matched_kw[:5])}\
\
"
    msg += f"```\
"
    msg += f"市值     ${token['mc']:>12,.0f}\
"
    msg += f"流动性   ${token['liq']:>12,.0f}\
"
    msg += f"1h涨幅   {token['chg_1h']:>+11.1f}%\
"
    if token.get('sm', 0) > 0:
        msg += f"聪明钱   {token['sm']:>12d}\
"
    msg += f"币龄     {token['age_h']:>10.1f}h\
"
    msg += f"```\
"
    
    # 社交链接
    links = []
    if (desc_info or {}).get('twitter'):
        links.append(f"Twitter: {desc_info['twitter']}")
    if (desc_info or {}).get('telegram'):
        links.append(f"TG: {desc_info['telegram']}")
    if (desc_info or {}).get('website'):
        links.append(f"Web: {desc_info['website']}")
    if links:
        msg += '\
' + '\
'.join(links)
    
    return msg

def format_binance_cz_alert(token, matched_kw, desc_info=None):
    """币安/CZ叙事推送"""
    msg = f"链上雷达 — 币安/CZ概念\
"
    msg += f"链: BSC\
\
"
    msg += f"{token['name']} ({token['symbol']})\
"
    msg += f"`{token['address']}`\
\
"
    
    # 叙事故事
    desc = (desc_info or {}).get('description', '')
    if desc:
        if len(desc) > 200:
            desc = desc[:200] + '...'
        msg += f"故事: {desc}\
\
"
    
    msg += f"命中关键词: {', '.join(matched_kw[:5])}\
\
"
    msg += f"```\
"
    msg += f"市值     ${token['mc']:>12,.0f}\
"
    msg += f"流动性   ${token['liq']:>12,.0f}\
"
    msg += f"1h涨幅   {token['chg_1h']:>+11.1f}%\
"
    msg += f"币龄     {token['age_h']:>10.1f}h\
"
    msg += f"```\
"
    
    # 社交链接
    links = []
    if (desc_info or {}).get('twitter'):
        links.append(f"Twitter: {desc_info['twitter']}")
    if (desc_info or {}).get('telegram'):
        links.append(f"TG: {desc_info['telegram']}")
    if (desc_info or {}).get('website'):
        links.append(f"Web: {desc_info['website']}")
    if links:
        msg += '\
' + '\
'.join(links)
    
    return msg

def format_novel_narrative_alert(token, theme, desc_info=None):
    """全新叙事推送 — 保留备用"""
    return format_heating_narrative_alert(token, theme, 1, desc_info)

def format_heating_narrative_alert(token, theme, count, desc_info=None):
    """叙事热点推送 — 同主题持续冒新币"""
    chain_map = {'sol': 'SOL', 'eth': 'ETH', 'bsc': 'BSC', 'base': 'BASE'}
    ch = chain_map.get(token['chain'], token['chain'].upper())
    
    msg = f"链上雷达 — 叙事热点\
"
    msg += f"链: {ch}\
\
"
    msg += f"{token['name']} ({token['symbol']})\
"
    msg += f"`{token['address']}`\
\
"
    
    # 叙事故事
    desc = (desc_info or {}).get('description', '')
    if desc:
        if len(desc) > 300:
            desc = desc[:300] + '...'
        msg += f"故事: {desc}\
\
"
    else:
        msg += f"叙事主题: {theme}\
\
"
    
    msg += f"同类概念已出现{count}个币 — 持续有人做\
\
"
    msg += f"```\
"
    msg += f"市值     ${token['mc']:>12,.0f}\
"
    msg += f"流动性   ${token['liq']:>12,.0f}\
"
    msg += f"1h涨幅   {token['chg_1h']:>+11.1f}%\
"
    if token.get('sm', 0) > 0:
        msg += f"聪明钱   {token['sm']:>12d}\
"
    msg += f"持有人   {token['holders']:>12d}\
"
    msg += f"币龄     {token['age_h']:>10.1f}h\
"
    msg += f"```"
    
    # 社交链接
    links = []
    if (desc_info or {}).get('twitter'):
        links.append(f"\
Twitter: {desc_info['twitter']}")
    if (desc_info or {}).get('telegram'):
        links.append(f"TG: {desc_info['telegram']}")
    if (desc_info or {}).get('website'):
        links.append(f"Web: {desc_info['website']}")
    if links:
        msg += '\
'.join(links)
    
    return msg

# ============================================================
# 动量追踪器 — 持续上涨+放量检测
# ============================================================
def track_momentum(tokens):
    """
    每轮扫描更新币的快照。
    连续多轮市值上涨+成交量增加 = 动量信号，直接推。
    """
    global MOMENTUM_TRACKER, MOMENTUM_PUSHED
    now = time.time()
    alerts = []
    
    # 当前轮所有地址
    current_addrs = set()
    
    for token in tokens:
        addr = token['address']
        mc = token['mc']
        vol = token.get('volume', 0) or 0
        price = token.get('price', 0) or 0
        buys = token.get('buys_1h', 0) or token.get('buys', 0) or 0
        
        current_addrs.add(addr)
        
        # 基本门槛
        if mc < 1000 or token.get('liq', 0) < 500 or mc > 10000000:
            continue
        
        # 记录快照 — 只有数据真正变化时才记录（GMGN有缓存）
        if addr not in MOMENTUM_TRACKER:
            MOMENTUM_TRACKER[addr] = []
        
        snapshots = MOMENTUM_TRACKER[addr]
        
        # 跳过重复数据（跟上一次完全一样就不记录）
        if snapshots and snapshots[-1]['mc'] == mc and snapshots[-1]['vol'] == vol:
            continue  # 数据没变，跳过
        
        snapshots.append({
            'ts': now,
            'mc': mc,
            'vol': vol,
            'price': price,
            'buys': buys,
        })
        
        # 只保留最近20个快照（约200秒）
        if len(snapshots) > 20:
            snapshots[:] = snapshots[-20:]
        
        # 至少需要3个快照才能判断
        if len(snapshots) < MOMENTUM_CONSECUTIVE_UP:
            continue
        
        # 检测最近N轮是否持续涨
        recent = snapshots[-MOMENTUM_CONSECUTIVE_UP:]
        consecutive_up = True
        total_gain = 0
        
        for i in range(1, len(recent)):
            prev_mc = recent[i-1]['mc']
            curr_mc = recent[i]['mc']
            if prev_mc <= 0:
                consecutive_up = False
                break
            gain = (curr_mc - prev_mc) / prev_mc
            if gain <= 0:  # 任何一轮没涨就不算
                consecutive_up = False
                break
            total_gain += gain
        
        if not consecutive_up:
            continue
        
        # 连续涨了！检查放量（成交量在增）
        vol_increasing = True
        for i in range(1, len(recent)):
            if recent[i]['buys'] < recent[i-1]['buys'] * 0.8:  # 允许小幅波动
                vol_increasing = False
                break
        
        # 计算总涨幅
        first_mc = recent[0]['mc']
        last_mc = recent[-1]['mc']
        pct_gain = ((last_mc - first_mc) / first_mc * 100) if first_mc > 0 else 0
        
        # 推送条件：连续涨 + 涨幅（SOL链meme太多，大幅提高门槛）
        chain_min_gain = 25 if token['chain'] in ('sol', 'solana') else 5
        if pct_gain < chain_min_gain:
            continue

        # SOL链额外要求：市值 > $5000 才推（过滤纯土狗）
        if token['chain'] in ('sol', 'solana') and mc < 5000:
            continue

        # 信号计数
        push_info = MOMENTUM_PUSHED.get(addr, {'count': 0, 'last_ts': 0, 'last_mc': 0})
        
        # 必须比上次推送时市值还高才推（真的还在涨）
        if push_info['count'] > 0 and last_mc <= push_info['last_mc']:
            continue
        
        push_info['count'] += 1
        push_info['last_ts'] = now
        push_info['last_mc'] = last_mc
        signal_count = push_info['count']
        
        # 安全检查
        safety = check_token_safety(token['chain'], addr)
        if not safety.get('safe'):
            continue
        
        # 拉取描述（用于分类 + 推送格式）
        desc_info = fetch_token_description(token['chain'], addr)
        description = desc_info.get('description', '')
        
        # 叙事分类 → 星级评分（含描述匹配 + 推特热点关键词）
        category, matched_kw = classify_narrative(token['name'], token['symbol'], token['chain'], description)
        
        # 🎯 只推送★★★级叙事（马斯克/川普、币安/CZ/何一）
        reverse_detected = False
        if category not in ('musk_trump', 'binance_cz'):
            # 🔄 反向检测：链上异动 → 立刻查推特
            reverse_result = reverse_twitter_check(token['name'], token['symbol'], token['chain'], description)
            if reverse_result:
                category = reverse_result['category']
                matched_kw = reverse_result['keywords']
                reverse_detected = True
                # 将匹配词注入推特热词池，供后续正向匹配复用
                now_rt = time.time()
                for kw in reverse_result['keywords']:
                    if kw not in HOT_TWITTER_KEYWORDS:
                        HOT_TWITTER_KEYWORDS[kw] = {'expire': now_rt + HOT_KEYWORDS_TTL, 'category': category}
            else:
                continue
        
        is_flap = token.get('launchpad') == 'flap'
        
        if category == 'musk_trump':
            stars = 3
            prefix = "🔁 " if reverse_detected else ""
            narrative_tag = f"{prefix}马斯克/川普概念 ({', '.join(matched_kw[:3])})"
        elif category == 'binance_cz':
            stars = 3
            prefix = "🔁 " if reverse_detected else ""
            narrative_tag = f"{prefix}币安/CZ概念 ({', '.join(matched_kw[:3])})"
        elif category == 'celebrity_viral':
            stars = 2
            narrative_tag = f"名人/热点 ({', '.join(matched_kw[:3])})"
        elif is_flap:
            stars = 2
            narrative_tag = "FLAP社区币"
        else:
            # 检查是否全新叙事
            theme = normalize_theme(token['name'], token['symbol'])
            theme_words = [w for w in theme.split() if w not in COMMON_NOISE_WORDS and len(w) > 2]
            if len(theme_words) >= 2:
                stars = 2
                narrative_tag = f"叙事: {theme}"
            else:
                stars = 1
                narrative_tag = "无明确叙事"
        
        # FLAP币额外标注社区/CTO信息
        if is_flap:
            has_twitter = bool(desc_info.get('twitter'))
            has_tg = bool(desc_info.get('telegram'))
            has_web = bool(desc_info.get('website'))
            community_tags = []
            if has_twitter:
                community_tags.append("有推特")
            if has_tg:
                community_tags.append("有TG群")
            if has_web:
                community_tags.append("有官网")
            if community_tags:
                narrative_tag += f" | {' '.join(community_tags)}"
                stars = min(3, stars + 1)  # 有社区加一星
            else:
                narrative_tag += " | 无社区链接"
        
        msg = format_momentum_alert(token, pct_gain, len(recent), vol_increasing, stars, narrative_tag, desc_info, signal_count)
        alerts.append({'msg': msg, 'token': token})
        MOMENTUM_PUSHED[addr] = push_info
        
        log(f"[动量信号{signal_count}] {token['name']} ({token['symbol']}) on {token['chain']} — 连涨{len(recent)}轮 +{pct_gain:.1f}%")
    
    # 清理不再出现的币
    stale = [a for a in MOMENTUM_TRACKER if a not in current_addrs]
    for a in stale:
        if now - MOMENTUM_TRACKER[a][-1]['ts'] > 600:  # 10分钟没出现就清理
            del MOMENTUM_TRACKER[a]
    
    # 清理推送记录 — 1小时没出现的清掉
    MOMENTUM_PUSHED = {k: v for k, v in MOMENTUM_PUSHED.items() if now - v.get('last_ts', 0) < 3600}
    
    return alerts

def format_momentum_alert(token, pct_gain, rounds, vol_up, stars, narrative_tag, desc_info=None, seen_count=0):
    """持续上涨动量推送 — 简化直观格式"""
    chain_map = {'sol': '🔵 SOL', 'eth': '🟣 ETH', 'bsc': '🟡 BSC', 'base': '🔵 BASE'}
    ch = chain_map.get(token['chain'], token['chain'].upper())
    vol_tag = "📈放量" if vol_up else "📊"
    star_str = "★" * stars + "☆" * (3 - stars)
    chain_icon = {'sol': '🔵', 'eth': '🟣', 'bsc': '🟡', 'base': '🔵'}.get(token['chain'], '⚪')

    msg = (
        f"{chain_icon} {ch} | {narrative_tag}\n"
        f"\n"
        f"**{token['name']}** `${token['symbol']}`\n"
        f"`{token['address'][:8]}...{token['address'][-6:]}`\n"
    )

    desc = (desc_info or {}).get('description', '')
    if desc:
        if len(desc) > 120:
            desc = desc[:120] + '...'
        msg += f"\n💬 {desc}\n"

    msg += (
        f"\n{vol_tag} 连涨{rounds}轮 `+{pct_gain:.1f}%`\n"
        f"\n"
        f"```\n"
        f"市值 ${token['mc']:>9,.0f}\n"
        f"池子 ${token['liq']:>9,.0f}\n"
        f"1h   {token['chg_1h']:>+8.1f}%\n"
    )
    if token.get('sm', 0) > 0:
        msg += f"聪明  {token['sm']:>8d}\n"
    msg += (
        f"年龄  {token['age_h']:>7.1f}h\n"
        f"```\n"
        f"{star_str}  # {seen_count}"
    )

    links = []
    if (desc_info or {}).get('twitter'):
        links.append(f"𝕏 {desc_info['twitter']}")
    if (desc_info or {}).get('telegram'):
        links.append(f"📱 {desc_info['telegram']}")
    if (desc_info or {}).get('website'):
        links.append(f"🌐 {desc_info['website']}")
    if links:
        msg += '\n' + '\n'.join(links)

    return msg

def format_celebrity_alert(token, matched_kw, desc_info=None):
    """名人/推特热点推送 ★★"""
    chain_map = {'sol': 'SOL', 'eth': 'ETH', 'bsc': 'BSC', 'base': 'BASE'}
    ch = chain_map.get(token['chain'], token['chain'].upper())
    
    msg = f"链上雷达 — 名人/热点 ★★\
"
    msg += f"链: {ch}\
\
"
    msg += f"{token['name']} ({token['symbol']})\
"
    msg += f"`{token['address']}`\
\
"
    
    desc = (desc_info or {}).get('description', '')
    if desc:
        if len(desc) > 200:
            desc = desc[:200] + '...'
        msg += f"故事: {desc}\
\
"
    
    msg += f"命中关键词: {', '.join(matched_kw[:5])}\
\
"
    msg += f"```\
"
    msg += f"市值     ${token['mc']:>12,.0f}\
"
    msg += f"流动性   ${token['liq']:>12,.0f}\
"
    msg += f"1h涨幅   {token['chg_1h']:>+11.1f}%\
"
    if token.get('sm', 0) > 0:
        msg += f"聪明钱   {token['sm']:>12d}\
"
    msg += f"币龄     {token['age_h']:>10.1f}h\
"
    msg += f"```"
    
    links = []
    if (desc_info or {}).get('twitter'):
        links.append(f"\
Twitter: {desc_info['twitter']}")
    if (desc_info or {}).get('telegram'):
        links.append(f"TG: {desc_info['telegram']}")
    if links:
        msg += '\
'.join(links)
    
    return msg

# ============================================================
# 核心扫描逻辑
# ============================================================
def scan_narratives():
    """主扫描函数"""
    conn = init_db()
    tokens = fetch_new_tokens()
    
    log(f"扫描 {len(tokens)} 个新币...")
    
    # === 动量追踪 — 每轮更新所有币的快照，检测持续上涨 ===
    # 拉FLAP币一起喂进动量追踪器
    flap_tokens = []
    try:
        flap_tokens = fetch_flap_tokens()
    except:
        pass
    all_momentum_tokens = tokens + flap_tokens
    momentum_alerts = track_momentum(all_momentum_tokens)
    
    for token in tokens:
        addr = token['address']
        chain = token['chain']
        name = token['name']
        symbol = token['symbol']
        
        # 已扫描过的 — 更新seen_count和narratives的token_count，但不重复推
        if is_token_seen(conn, addr):
            # 更新seen_count
            c = conn.cursor()
            c.execute('UPDATE tokens_seen SET seen_count = seen_count + 1, market_cap = ? WHERE address = ?', (token['mc'], addr))
            # 更新narratives表的token_count（按主题）
            theme_tmp = normalize_theme(name, symbol)
            if theme_tmp:
                c.execute('UPDATE narratives SET token_count = token_count + 1, last_seen_at = ? WHERE theme = ?', (int(time.time()), theme_tmp))
            conn.commit()
            continue
        
        # 分类叙事
        category, matched_kw = classify_narrative(name, symbol, chain)
        
        if category == 'spam':
            record_token(conn, addr, chain, name, symbol, '', 'spam', token['mc'])
            continue
        
        # 基本质量门槛（防止推太多垃圾）
        min_mc = 1000
        min_liq = 500
        if token['mc'] < min_mc or token['liq'] < min_liq:
            record_token(conn, addr, chain, name, symbol, '', 'too_small', token['mc'])
            continue
        
        theme = normalize_theme(name, symbol)
        
        # 所有分类只记录，不直接推送 — 推送统一走动量引擎
        record_token(conn, addr, chain, name, symbol, theme, category, token['mc'])
        check_narrative_novelty(conn, theme, name, symbol, addr, chain)
    
    conn.close()
    
    # === 推送动量信号 ===
    pushed = 0
    for ma in momentum_alerts[:8]:  # 单轮最多推8个
        if tg_send(ma['msg']):
            pushed += 1
            time.sleep(1)  # 避免TG限流
    
    return pushed, len(momentum_alerts)

# ============================================================
# 主循环
# ============================================================
def main():
    log("=" * 50)
    log("链上雷达 v1 启动")
    log(f"扫描间隔: {SCAN_INTERVAL}s")
    log(f"推送逻辑: 动量优先 — 连涨才推，仅推★★★级叙事（马斯克/川普/币安/CZ/何一）")
    log("=" * 50)
    
    # 初始化DB
    init_db()
    
    # 启动通知
    tg_send(
        "链上雷达 v1 已启动\n"
        "\n"
        "核心逻辑: 动量优先\n"
        "连涨3轮+涨幅>5% + 仅★★★叙事才推送\n"
        "推送范围:\n"
        "✅ 马斯克/川普概念\n"
        "✅ 币安/CZ/何一概念\n"
        "❌ 名人热点 / FLAP / 普通叙事（不推）\n"
        "\n"
        f"扫描频率: 每{SCAN_INTERVAL}秒"
    )
    
    scan_count = 0
    total_pushed = 0
    
    while True:
        try:
            scan_count += 1
            # 每2分钟拉取一次推特关键词
            fetch_twitter_keywords()
            pushed, found = scan_narratives()
            total_pushed += pushed
            
            if pushed > 0:
                log(f"第{scan_count}轮: 发现{found}个, 推送{pushed}个 (累计推送{total_pushed})")
            else:
                if scan_count % 20 == 0:  # 每20轮报一次无信号
                    log(f"第{scan_count}轮: 无新信号 (累计推送{total_pushed})")
            
        except Exception as e:
            log(f"扫描异常: {e}")
        
        time.sleep(SCAN_INTERVAL)

if __name__ == '__main__':
    main()

