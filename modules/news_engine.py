"""
╔══════════════════════════════════════════════════════════════════════════╗
║   ZAR ULTIMATE BOT v6 — news_engine.py  (v3 — DIRECTIONAL)            ║
║                                                                          ║
║   FILOSOFÍA v3:                                                          ║
║   Las noticias son CONTEXTO para Gemini, NO un filtro de trading.      ║
║   Solo breaking news inesperadas bloquean (el calendario ya maneja     ║
║   los eventos programados con precisión de 1 minuto).                  ║
║                                                                          ║
║   NUEVO EN v3 — ANÁLISIS DIRECCIONAL POR DIVISA:                        ║
║   "Fed hawkish"  → USD_sentiment = +0.80  (USD sube)                  ║
║   "BOJ dovish"   → JPY_sentiment = -0.75  (JPY baja)                  ║
║   "ECB rate cut" → EUR_sentiment = -0.80  (EUR baja)                  ║
║   Resultado llega a Gemini como contexto rico para su decisión.        ║
╚══════════════════════════════════════════════════════════════════════════╝
"""

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from email.utils import parsedate_to_datetime

import requests
import config as cfg

log          = logging.getLogger(__name__)
HTTP_TIMEOUT = 12

# ════════════════════════════════════════════════════════════════
#  MAPA DIRECCIONAL — Keyword → impacto por divisa
# ════════════════════════════════════════════════════════════════
DIRECTIONAL_MAP = [
    # USD
    {"kw": ["fed hawkish","rate hike","hike rates","tighter monetary","fed raises"],
     "impact": {"USD": +0.80}},
    {"kw": ["fed dovish","rate cut","cuts rates","easier monetary","pivot","fed eases"],
     "impact": {"USD": -0.80}},
    {"kw": ["nfp beat","payrolls beat","strong jobs","unemployment fell","labor strong"],
     "impact": {"USD": +0.70}},
    {"kw": ["nfp miss","payrolls miss","weak jobs","unemployment rose","labor weak"],
     "impact": {"USD": -0.70}},
    {"kw": ["cpi hot","inflation surged","prices soared","inflation above"],
     "impact": {"USD": +0.55}},
    {"kw": ["cpi cool","inflation fell","deflation","prices dropped"],
     "impact": {"USD": -0.50}},
    {"kw": ["dollar surge","dollar rally","dollar strength","dxy rises"],
     "impact": {"USD": +0.60}},
    {"kw": ["dollar falls","dollar weakness","dollar drop","dxy falls"],
     "impact": {"USD": -0.60}},
    {"kw": ["us gdp beat","us growth strong","us expansion"],
     "impact": {"USD": +0.50}},
    {"kw": ["us recession","us gdp miss","us slowdown"],
     "impact": {"USD": -0.55}},
    # EUR
    {"kw": ["ecb hawkish","ecb hikes","lagarde hawkish","ecb raises"],
     "impact": {"EUR": +0.80}},
    {"kw": ["ecb dovish","ecb cuts","lagarde dovish","ecb eases","ecb rate cut"],
     "impact": {"EUR": -0.80}},
    {"kw": ["eurozone gdp beat","eu economy strong","germany growth"],
     "impact": {"EUR": +0.55}},
    {"kw": ["eurozone recession","eu slowdown","germany recession"],
     "impact": {"EUR": -0.60}},
    {"kw": ["euro surges","euro rally","euro strength"],
     "impact": {"EUR": +0.60}},
    {"kw": ["euro falls","euro weakness","euro drops","euro tumbles"],
     "impact": {"EUR": -0.60}},
    # GBP
    {"kw": ["boe hawkish","boe hikes","boe raises rates","bailey hawkish"],
     "impact": {"GBP": +0.80}},
    {"kw": ["boe dovish","boe cuts","boe eases","bailey dovish"],
     "impact": {"GBP": -0.80}},
    {"kw": ["uk gdp beat","uk economy strong","uk expansion"],
     "impact": {"GBP": +0.55}},
    {"kw": ["uk recession","uk gdp miss","uk slowdown"],
     "impact": {"GBP": -0.60}},
    {"kw": ["pound surges","sterling rally","cable rally","pound strength"],
     "impact": {"GBP": +0.60}},
    {"kw": ["pound falls","sterling drops","cable falls","pound weakness"],
     "impact": {"GBP": -0.60}},
    {"kw": ["brexit","uk political crisis","uk instability"],
     "impact": {"GBP": -0.50}},
    # JPY
    {"kw": ["boj hawkish","boj hikes","boj raises","ueda hawkish","boj tightening"],
     "impact": {"JPY": +0.80}},
    {"kw": ["boj dovish","boj eases","yield curve control","ueda dovish","boj ultra-loose"],
     "impact": {"JPY": -0.75}},
    {"kw": ["japan gdp beat","japan growth","japan expansion"],
     "impact": {"JPY": +0.50}},
    {"kw": ["japan recession","japan gdp miss","japan deflation"],
     "impact": {"JPY": -0.55}},
    {"kw": ["yen surge","yen rally","yen strength","yen safe haven"],
     "impact": {"JPY": +0.65}},
    {"kw": ["yen falls","yen weakness","yen drop"],
     "impact": {"JPY": -0.60}},
    {"kw": ["risk off","flight to safety","safe haven demand","global risk off"],
     "impact": {"JPY": +0.55, "USD": +0.30, "XAU": +0.40}},
    {"kw": ["risk on","risk appetite","risk rally","global risk on"],
     "impact": {"JPY": -0.40, "USD": -0.20}},
    # XAU
    {"kw": ["gold surges","gold rally","gold hits record","gold safe haven"],
     "impact": {"XAU": +0.75}},
    {"kw": ["gold drops","gold falls","gold selloff","gold weakness"],
     "impact": {"XAU": -0.70}},
    {"kw": ["geopolit","war","conflict escalat","military","nuclear threat","crisis"],
     "impact": {"XAU": +0.60, "JPY": +0.35, "USD": +0.20}},
    {"kw": ["real yields rise","treasury yields surge","10y yield up"],
     "impact": {"XAU": -0.50, "USD": +0.40}},
    # XAG
    {"kw": ["silver surges","silver rally","silver demand"],
     "impact": {"XAG": +0.65}},
    {"kw": ["silver drops","silver falls","silver selloff"],
     "impact": {"XAG": -0.60}},
    {"kw": ["industrial demand","solar demand","manufacturing boom"],
     "impact": {"XAG": +0.45}},
    # OIL
    {"kw": ["opec cut","opec+ cut","oil supply cut","production cut"],
     "impact": {"OIL": +0.80}},
    {"kw": ["opec hike","oil supply surge","production increase"],
     "impact": {"OIL": -0.70}},
    {"kw": ["oil surges","crude rally","oil hits","crude up"],
     "impact": {"OIL": +0.65}},
    {"kw": ["oil drops","crude falls","oil plunges","crude selloff"],
     "impact": {"OIL": -0.65}},
    {"kw": ["eia crude draw","inventory draw"],
     "impact": {"OIL": +0.55}},
    {"kw": ["eia crude build","inventory build"],
     "impact": {"OIL": -0.55}},
    {"kw": ["middle east tension","strait of hormuz"],
     "impact": {"OIL": +0.50}},
    # BTC
    {"kw": ["bitcoin etf approved","crypto etf","bitcoin institutional"],
     "impact": {"BTC": +0.80}},
    {"kw": ["bitcoin banned","crypto banned","sec rejects","crypto crackdown"],
     "impact": {"BTC": -0.80}},
    {"kw": ["bitcoin surges","crypto rally","btc up","bitcoin hits"],
     "impact": {"BTC": +0.65}},
    {"kw": ["bitcoin crashes","crypto selloff","btc drops","crypto winter"],
     "impact": {"BTC": -0.70}},
    {"kw": ["exchange hack","exchange collapse","crypto fraud"],
     "impact": {"BTC": -0.75}},
    {"kw": ["bitcoin halving"],
     "impact": {"BTC": +0.50}},
]

SYMBOL_TO_CURRENCIES = {
    "XAUUSDm": ["XAU","USD"], "US500m":  ["USD"],
    "EURUSDm": ["EUR","USD"], "GBPUSDm": ["GBP","USD"],
    "USDJPYm": ["USD","JPY"], "GBPJPYm": ["GBP","JPY"],
    "XAGUSDm": ["XAG","USD"], "USOILm":  ["OIL","USD"],
    "NAS100m": ["USD"],        "GER40m":  ["EUR"],
    "EURJPYm": ["EUR","JPY"],  "BTCUSDm": ["BTC","USD"],
}

BREAKING_PAUSE_KEYWORDS = [
    "emergency rate","emergency meeting","unscheduled meeting",
    "surprise rate cut","surprise rate hike",
    "nuclear","war declared","invasion begins",
    "market circuit breaker","trading halt","exchange halted",
    "bank collapse","bank run","bank failure",
    "flash crash","market crash",
    "default declared","sovereign default",
    "circuit breaker triggered",
]

RSS_FEEDS = [
    "https://feeds.reuters.com/reuters/businessNews",
    "https://rss.marketwatch.com/rss/topstories",
    "https://finance.yahoo.com/rss/2.0/headline",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
]

POSITIVE_WORDS = ["rally","surge","gain","rise","bullish","strong","growth",
                  "beat","record","exceed","recover","boost","soar","jump"]
NEGATIVE_WORDS = ["fall","drop","decline","bearish","weak","concern","fear",
                  "miss","disappoint","recession","crisis","selloff","plunge","crash"]

MAX_AGE_BREAKING_HOURS = 2


# ════════════════════════════════════════════════════════════════
#  DATACLASSES
# ════════════════════════════════════════════════════════════════

@dataclass
class NewsItem:
    title:            str
    summary:          str
    sentiment:        float
    impact:           int
    source:           str
    published:        str
    published_dt:     object   # datetime | None
    is_breaking:      bool = False
    currency_impacts: dict = field(default_factory=dict)


@dataclass
class NewsContext:
    items:              List[NewsItem] = field(default_factory=list)
    avg_sentiment:      float = 0.0
    high_impact_count:  int   = 0
    breaking_count:     int   = 0
    should_pause:       bool  = False
    pause_reason:       str   = ""
    fetched_at:         str   = ""
    summary:            str   = ""
    currency_sentiment: Dict[str, float] = field(default_factory=dict)

    def get_directional_bias(self, currencies: List[str]) -> str:
        """Descripción del sesgo direccional para las divisas del símbolo."""
        if not currencies or not self.currency_sentiment:
            return "  Sin datos direccionales disponibles."
        lines = []
        for cur in currencies:
            score = self.currency_sentiment.get(cur, None)
            if score is None:
                lines.append(f"  {cur}: sin noticias recientes específicas")
            elif score > 0.15:
                lines.append(f"  {cur}: 📈 ALCISTA ({score:+.2f}) → favorece compra")
            elif score < -0.15:
                lines.append(f"  {cur}: 📉 BAJISTA ({score:+.2f}) → favorece venta")
            else:
                lines.append(f"  {cur}: ➡ NEUTRAL ({score:+.2f})")
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════════

def _parse_pub_date(pub_str: str):
    if not pub_str:
        return None
    try:
        return parsedate_to_datetime(pub_str).astimezone(timezone.utc)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(
            pub_str.replace("Z","+00:00")).astimezone(timezone.utc)
    except Exception:
        pass
    return None


def _age_hours(pub_dt) -> float:
    if pub_dt is None:
        return 24.0
    return (datetime.now(timezone.utc) - pub_dt).total_seconds() / 3600


def _basic_sentiment(text: str) -> float:
    tl  = text.lower()
    pos = sum(1 for w in POSITIVE_WORDS if w in tl)
    neg = sum(1 for w in NEGATIVE_WORDS if w in tl)
    return round((pos - neg) / (pos + neg), 3) if (pos + neg) else 0.0


def _analyze_currency_impact(title: str, summary: str) -> Dict[str, float]:
    """Retorna impacto direccional por divisa según keywords del artículo."""
    combined = (title + " " + summary).lower()
    impacts: Dict[str, float] = {}
    for entry in DIRECTIONAL_MAP:
        if not any(kw in combined for kw in entry["kw"]):
            continue
        for currency, score in entry["impact"].items():
            impacts[currency] = (impacts[currency] + score) / 2 if currency in impacts else score
    return impacts


def _is_breaking(title: str, summary: str, pub_dt) -> bool:
    if _age_hours(pub_dt) > MAX_AGE_BREAKING_HOURS:
        return False
    combined = (title + " " + summary).lower()
    if not any(kw in combined for kw in BREAKING_PAUSE_KEYWORDS):
        return False
    preview = ["guide","preview","outlook","what to watch","analysis",
               "review","how to","why","what is","explainer","history"]
    return not any(w in combined for w in preview)


# ════════════════════════════════════════════════════════════════
#  FETCHERS
# ════════════════════════════════════════════════════════════════

_av_calls_today = 0
_av_reset_day   = -1
AV_DAILY_LIMIT  = 18

def _av_fetch(topics: str) -> List[NewsItem]:
    global _av_calls_today, _av_reset_day
    today = datetime.now(timezone.utc).day
    if today != _av_reset_day:
        _av_calls_today = 0
        _av_reset_day   = today
    if _av_calls_today >= AV_DAILY_LIMIT:
        return []
    url = (f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT"
           f"&topics={topics}&limit=12&apikey={cfg.ALPHA_VANTAGE_KEY}")
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT)
        _av_calls_today += 1
        data = r.json()
        if "feed" not in data:
            return []
        items = []
        for art in data["feed"][:8]:
            title  = art.get("title","")
            summ   = art.get("summary","")[:200]
            score  = float(art.get("overall_sentiment_score", 0))
            pub_s  = art.get("time_published","")
            pub_dt = None
            if pub_s and len(pub_s) >= 15:
                try:
                    pub_dt = datetime.strptime(pub_s[:15],"%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
                except Exception:
                    pass
            cur_i    = _analyze_currency_impact(title, summ)
            breaking = _is_breaking(title, summ, pub_dt)
            items.append(NewsItem(
                title=title, summary=summ, sentiment=score,
                impact=5 if breaking else (4 if cur_i else 2),
                source="AlphaVantage", published=pub_s, published_dt=pub_dt,
                is_breaking=breaking, currency_impacts=cur_i,
            ))
        log.info(f"[news] AV: {len(items)} arts (llamadas: {_av_calls_today})")
        return items
    except Exception as e:
        log.warning(f"[news] AV error: {e}")
        return []


def _rss_fetch() -> List[NewsItem]:
    items = []
    for feed_url in RSS_FEEDS:
        try:
            r = requests.get(feed_url, timeout=HTTP_TIMEOUT,
                             headers={"User-Agent":"ZAR-Bot/6.3 RSS"})
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
            ns   = {"atom":"http://www.w3.org/2005/Atom"}
            rss_items = root.findall(".//item") or root.findall(".//atom:entry",ns)
            for entry in rss_items[:6]:
                title = (entry.findtext("title") or
                         entry.findtext("atom:title",namespaces=ns) or "").strip()
                desc  = (entry.findtext("description") or
                         entry.findtext("atom:summary",namespaces=ns) or "").strip()
                pub   = (entry.findtext("pubDate") or
                         entry.findtext("atom:published",namespaces=ns) or "")
                if not title:
                    continue
                pub_dt   = _parse_pub_date(pub)
                cur_i    = _analyze_currency_impact(title, desc)
                breaking = _is_breaking(title, desc, pub_dt)
                sent     = _basic_sentiment(title + " " + desc)
                items.append(NewsItem(
                    title=title[:150], summary=desc[:200], sentiment=sent,
                    impact=5 if breaking else (4 if cur_i else 2),
                    source=feed_url.split("/")[2], published=pub,
                    published_dt=pub_dt, is_breaking=breaking,
                    currency_impacts=cur_i,
                ))
        except Exception as e:
            log.debug(f"[news] RSS {feed_url}: {e}")
    return items


# ════════════════════════════════════════════════════════════════
#  CONSTRUCTOR PRINCIPAL
# ════════════════════════════════════════════════════════════════

def build_news_context(symbol: str, sym_cfg: dict) -> NewsContext:
    topics    = sym_cfg.get("news_topics","economy_monetary,economy_macro")
    av_items  = _av_fetch(topics)
    rss_items = _rss_fetch()

    seen, all_items = set(), []
    for item in av_items + rss_items:
        key = item.title.lower()[:60]
        if key not in seen:
            seen.add(key)
            all_items.append(item)

    all_items.sort(
        key=lambda x:(x.is_breaking, bool(x.currency_impacts), abs(x.sentiment)),
        reverse=True
    )
    all_items = all_items[:25]

    avg_sent       = sum(i.sentiment for i in all_items)/len(all_items) if all_items else 0.0
    hi_count       = sum(1 for i in all_items if i.currency_impacts)
    breaking_count = sum(1 for i in all_items if i.is_breaking)

    # ── Sentimiento direccional por divisa (ponderado por recencia) ──
    cur_scores: Dict[str, list] = {}
    for item in all_items:
        if not item.currency_impacts:
            continue
        age_h  = _age_hours(item.published_dt)
        weight = max(0.1, 1.0 - age_h / 24)
        for cur, score in item.currency_impacts.items():
            cur_scores.setdefault(cur, []).append(score * weight)

    currency_sentiment = {
        cur: round(sum(scores)/len(scores), 3)
        for cur, scores in cur_scores.items() if scores
    }

    # ── Pausa SOLO por breaking news ─────────────────────────────
    should_pause = breaking_count >= 1
    pause_reason = ""
    if should_pause:
        titles = [i.title[:60] for i in all_items if i.is_breaking][:2]
        pause_reason = f"🚨 BREAKING: {' | '.join(titles)}"
        log.warning(f"[news] PAUSA: {pause_reason}")

    # ── Resumen para prompt Gemini ────────────────────────────────
    sym_currencies = SYMBOL_TO_CURRENCIES.get(symbol, sym_cfg.get("currencies",["USD"]))
    lines = [
        f"📰 {len(all_items)} artículos | Sentimiento: {avg_sent:+.2f} | Breaking: {breaking_count}",
        "",
        "SENTIMIENTO DIRECCIONAL (para este símbolo):",
    ]
    for cur in sym_currencies:
        score = currency_sentiment.get(cur)
        if score is not None:
            bias = "📈 ALCISTA" if score > 0.15 else ("📉 BAJISTA" if score < -0.15 else "➡ NEUTRAL")
            lines.append(f"  {cur}: {score:+.3f} → {bias}")
        else:
            lines.append(f"  {cur}: sin noticias recientes")

    shown = 0
    for item in all_items:
        if item.currency_impacts and shown < 3:
            age_str = f"{_age_hours(item.published_dt):.0f}h"
            cur_str = ", ".join(f"{k}:{v:+.2f}" for k,v in item.currency_impacts.items())
            lines.append(f"  {'🚨' if item.is_breaking else '📰'} [{age_str}] {item.title[:60]}")
            lines.append(f"       → {cur_str}")
            shown += 1

    log.info(f"[news] {symbol}: hi={hi_count} breaking={breaking_count} "
             f"curs={list(currency_sentiment.keys())} pausa={should_pause}")

    return NewsContext(
        items=all_items, avg_sentiment=round(avg_sent,3),
        high_impact_count=hi_count, breaking_count=breaking_count,
        should_pause=should_pause, pause_reason=pause_reason,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        summary="\n".join(lines), currency_sentiment=currency_sentiment,
    )


def format_news_for_prompt(ctx: NewsContext) -> str:
    lines = ["=== NOTICIAS (contexto informativo — no es un bloqueo) ===", ctx.summary]
    if ctx.should_pause:
        lines += ["", f"🚨 BREAKING NEWS: {ctx.pause_reason}", "→ HOLD obligatorio."]
    else:
        lines += ["",
                  "ℹ️ Usa el sentimiento direccional para reforzar o moderar tu señal.",
                  "El calendario económico ya maneja los eventos programados."]
    return "\n".join(lines)
