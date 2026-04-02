"""
TL;DRSS – FastAPI Backend
Endpoints:
  GET  /api/feed          – pobiera artykuły ze wszystkich aktywnych RSS
  POST /api/summarize     – streszcza artykuł przez AI
  GET  /api/sources       – lista źródeł
  POST /api/sources       – dodaj źródło (auto-detect RSS)
  DELETE /api/sources/{id} – usuń źródło
  PUT  /api/sources/{id}  – włącz/wyłącz źródło
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import sqlite3, feedparser, requests, os, re
from bs4 import BeautifulSoup
from datetime import datetime
from functools import lru_cache
import time

# ── Groq / Gemini ─────────────────────────────────────────────────────
try:
    from groq import Groq
    GROQ_OK = True
except ImportError:
    GROQ_OK = False

try:
    import google.generativeai as genai
    GEMINI_OK = True
except ImportError:
    GEMINI_OK = False

app = FastAPI(title="TL;DRSS API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── DB ────────────────────────────────────────────────────────────────
DB = "tldrss.db"

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                rss_url TEXT NOT NULL,
                active INTEGER DEFAULT 1
            )
        """)
        defaults = [
            ("Onet",    "https://onet.pl",    "https://wiadomosci.onet.pl/.feed"),
            ("WP",      "https://wp.pl",      "https://wiadomosci.wp.pl/rss.xml"),
            ("Gazeta",  "https://gazeta.pl",  "https://rss.gazeta.pl/pub/rss/wiadomosci.xml"),
            ("Pudelek", "https://pudelek.pl", "https://www.pudelek.pl/feed/"),
        ]
        for name, url, rss in defaults:
            if not conn.execute("SELECT id FROM sources WHERE name=?", (name,)).fetchone():
                conn.execute("INSERT INTO sources (name,url,rss_url) VALUES (?,?,?)", (name,url,rss))
        conn.commit()

init_db()

# ── Models ────────────────────────────────────────────────────────────
class SummarizeRequest(BaseModel):
    title: str
    url: str
    rss_summary: Optional[str] = ""
    provider: Optional[str] = "groq"
    groq_key: Optional[str] = ""
    gemini_key: Optional[str] = ""

class AddSourceRequest(BaseModel):
    url: str

class ToggleSourceRequest(BaseModel):
    active: bool

# ── Headers ──────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
    "Accept-Language": "pl-PL,pl;q=0.9",
}

# ── RSS cache (simple in-memory, 5 min TTL) ───────────────────────────
_rss_cache: dict = {}
RSS_TTL = 300  # seconds

def fetch_rss_cached(rss_url: str, source_name: str):
    now = time.time()
    if rss_url in _rss_cache:
        cached_at, data = _rss_cache[rss_url]
        if now - cached_at < RSS_TTL:
            return data, None
    try:
        resp = requests.get(rss_url, headers=HEADERS, timeout=8)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        items = []
        for entry in feed.entries[:15]:
            pub = entry.get("published_parsed") or entry.get("updated_parsed")
            if pub:
                pub_dt = datetime(*pub[:6])
                age_h = (datetime.utcnow() - pub_dt).total_seconds() / 3600
                time_str = f"{int(age_h)}h temu" if age_h < 24 else pub_dt.strftime("%d.%m")
                timestamp = pub_dt.isoformat()
            else:
                time_str = ""
                timestamp = ""
            summary_raw = entry.get("summary", "")
            items.append({
                "id": entry.get("id") or entry.get("link", ""),
                "title": entry.get("title", "").strip(),
                "url": entry.get("link", ""),
                "source": source_name,
                "time": time_str,
                "timestamp": timestamp,
                "rss_summary": BeautifulSoup(summary_raw, "html.parser").get_text()[:400],
                "image": _extract_image(entry),
            })
        _rss_cache[rss_url] = (now, items)
        return items, None
    except Exception as e:
        return [], str(e)

def _extract_image(entry) -> str:
    """Try to get a thumbnail/image from RSS entry."""
    # media:thumbnail
    media = entry.get("media_thumbnail") or entry.get("media_content")
    if media and isinstance(media, list) and media[0].get("url"):
        return media[0]["url"]
    # enclosure
    for enc in entry.get("enclosures", []):
        if enc.get("type", "").startswith("image"):
            return enc.get("url", "")
    return ""

# ── Scrape ────────────────────────────────────────────────────────────
def scrape_article(url: str) -> tuple[str, bool]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        if resp.status_code in (403, 429, 451):
            return "", True
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script","style","nav","header","footer","aside","form","iframe","noscript"]):
            tag.decompose()
        candidates = soup.find_all(
            ["article","main","div"],
            class_=lambda c: c and any(kw in c.lower() for kw in ["article","content","body","text","story","post"])
        )
        text = max(candidates, key=lambda t: len(t.get_text()), default=soup).get_text(separator=" ", strip=True)
        text = " ".join(text.split())[:4000]
        block_signals = ["dostęp tylko dla","zaloguj się","wykup dostęp","subskrypcja","paywall","captcha"]
        if any(s in text.lower() for s in block_signals) and len(text) < 600:
            return text, True
        return text, False
    except Exception:
        return "", True

# ── AI ────────────────────────────────────────────────────────────────
def build_prompt(title: str, text: str, note: str) -> str:
    return f"""Streść ten artykuł w 2-3 zdaniach po polsku. Tylko fakty – kto, co, kiedy, wynik. Żadnych wstępów.

Tytuł: {title}
Treść: {text[:3000]}
{note}"""

def summarize_groq(prompt: str, api_key: str) -> str:
    if not api_key:
        return "⚠️ Brak klucza Groq API."
    if not GROQ_OK:
        return "⚠️ Biblioteka groq nie jest zainstalowana."
    try:
        client = Groq(api_key=api_key)
        resp = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role":"user","content":prompt}],
            max_tokens=200, temperature=0.3,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"❌ Groq: {str(e)[:120]}"

def summarize_gemini(prompt: str, api_key: str) -> str:
    if not api_key:
        return "⚠️ Brak klucza Gemini API."
    if not GEMINI_OK:
        return "⚠️ Biblioteka google-generativeai nie jest zainstalowana."
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")
        resp = model.generate_content(
            prompt,
            generation_config=genai.types.GenerationConfig(max_output_tokens=200, temperature=0.3)
        )
        return resp.text.strip()
    except Exception as e:
        return f"❌ Gemini: {str(e)[:120]}"

# ── RSS auto-detect ───────────────────────────────────────────────────
def find_rss(url: str) -> tuple[str, str]:
    if not url.startswith("http"):
        url = "https://" + url
    domain = url.rstrip("/")
    name = domain.replace("https://","").replace("http://","").split("/")[0]

    # Try scraping <link rel="alternate"> first
    try:
        resp = requests.get(url, headers=HEADERS, timeout=6)
        soup = BeautifulSoup(resp.text, "html.parser")
        title_tag = soup.find("title")
        if title_tag:
            name = title_tag.text.strip().split("|")[0].split("-")[0].strip()[:30]
        for link in soup.find_all("link", rel=lambda r: r and "alternate" in r):
            t = link.get("type","")
            if "rss" in t or "atom" in t or "xml" in t:
                href = link.get("href","")
                if href:
                    if href.startswith("/"):
                        href = domain + href
                    return href, name
    except Exception:
        pass

    # Try common paths
    for path in ["/feed","/feed/","/rss","/rss.xml","/atom.xml","/feeds/posts/default","/najnowsze.xml"]:
        try:
            r = requests.get(domain+path, headers=HEADERS, timeout=4)
            ct = r.headers.get("content-type","")
            if r.status_code == 200 and ("xml" in ct or "rss" in ct or "atom" in ct):
                return domain+path, name
            feed = feedparser.parse(r.content)
            if feed.entries:
                return domain+path, name
        except Exception:
            continue

    return "", name

# ── API Routes ────────────────────────────────────────────────────────

@app.get("/api/feed")
def get_feed():
    """Return interleaved articles from all active sources."""
    with get_db() as conn:
        sources = conn.execute("SELECT * FROM sources WHERE active=1").fetchall()

    from collections import defaultdict
    by_source = defaultdict(list)
    errors = []

    for src in sources:
        arts, err = fetch_rss_cached(src["rss_url"], src["name"])
        if err:
            errors.append({"source": src["name"], "error": err})
        for a in arts:
            by_source[src["name"]].append(a)

    # Round-robin interleave
    interleaved = []
    while any(by_source.values()):
        for sname in list(by_source.keys()):
            if by_source[sname]:
                interleaved.append(by_source[sname].pop(0))

    return {"articles": interleaved, "errors": errors, "count": len(interleaved)}


@app.post("/api/summarize")
def summarize(req: SummarizeRequest):
    """Scrape article and summarize with AI."""
    text, blocked = scrape_article(req.url)
    note = ""
    if blocked or len(text) < 200:
        text = req.rss_summary
        note = "(streszczenie na podstawie RSS – portal zablokował pobieranie)"

    if not text:
        raise HTTPException(status_code=422, detail="Nie udało się pobrać treści artykułu.")

    prompt = build_prompt(req.title, text, note)

    if req.provider == "gemini":
        result = summarize_gemini(prompt, req.gemini_key)
        if result.startswith("❌") or result.startswith("⚠️"):
            fallback = summarize_groq(prompt, req.groq_key)
            if not (fallback.startswith("❌") or fallback.startswith("⚠️")):
                return {"summary": fallback, "provider": "groq", "fallback": True}
    else:
        result = summarize_groq(prompt, req.groq_key)
        if result.startswith("❌") or result.startswith("⚠️"):
            fallback = summarize_gemini(prompt, req.gemini_key)
            if not (fallback.startswith("❌") or fallback.startswith("⚠️")):
                return {"summary": fallback, "provider": "gemini", "fallback": True}

    return {"summary": result, "provider": req.provider, "fallback": False}


@app.get("/api/sources")
def get_sources():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM sources ORDER BY name").fetchall()
    return {"sources": [dict(r) for r in rows]}


@app.post("/api/sources")
def add_source(req: AddSourceRequest):
    rss_url, name = find_rss(req.url)
    if not rss_url:
        raise HTTPException(status_code=404, detail=f"Nie znalazłem RSS dla: {req.url}")
    url_clean = req.url if req.url.startswith("http") else "https://" + req.url
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM sources WHERE rss_url=?", (rss_url,)).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="To źródło już istnieje.")
        conn.execute("INSERT INTO sources (name,url,rss_url) VALUES (?,?,?)", (name, url_clean, rss_url))
        conn.commit()
        new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return {"id": new_id, "name": name, "rss_url": rss_url}


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
        conn.commit()
    return {"ok": True}


@app.put("/api/sources/{source_id}")
def toggle_source(source_id: int, req: ToggleSourceRequest):
    with get_db() as conn:
        conn.execute("UPDATE sources SET active=? WHERE id=?", (1 if req.active else 0, source_id))
        conn.commit()
    return {"ok": True}


# ── Health check ─────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "app": "TL;DRSS API", "version": "1.0"}
