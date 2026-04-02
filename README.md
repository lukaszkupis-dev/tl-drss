# TL;DRSS

Czytnik RSS z AI TL;DR. Streszcza artykuły zanim klikniesz.
Made by **vibe ops**.

---

## Struktura

```
tldrss/
├── backend/
│   ├── main.py           ← FastAPI backend (RSS + AI)
│   └── requirements.txt
├── frontend/
│   ├── index.html        ← cała apka (PWA)
│   ├── manifest.json     ← PWA manifest
│   ├── sw.js             ← Service Worker
│   └── icons/            ← ikony (wygeneruj z SVG)
└── render.yaml           ← deploy config dla Render.com
```

---

## Deploy na Render.com (za darmo)

### 1. Wrzuć na GitHub
Stwórz repo `tldrss` i wgraj cały folder.

### 2. Render.com
1. Wejdź na **render.com** → New → Web Service
2. Połącz z repo `tldrss`
3. Ustaw:
   - **Root Directory**: `backend`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Kliknij **Create Web Service**
5. Dostaniesz link np. `https://tldrss-api.onrender.com`

### 3. Frontend – podmień API URL
W `frontend/index.html` zmień linię:
```js
const API = '';
```
na:
```js
const API = 'https://tldrss-api.onrender.com';
```

### 4. Frontend – GitHub Pages
Stwórz drugie repo `tldrss-pwa` → wgraj folder `frontend/` → włącz GitHub Pages.

### 5. Ikony
Wejdź na **svgtopng.com**, wgraj ikonę SVG (z poprzedniego projektu), wygeneruj rozmiary:
512, 192, 180, 167, 152 px → wgraj do `frontend/icons/`.

---

## Lokalnie (development)

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Otwórz `http://localhost:8000` – serwuje też frontend.

---

## Jak działa

1. **GET /api/feed** – pobiera RSS ze wszystkich aktywnych źródeł, przeplata je round-robin
2. **POST /api/summarize** – scrape artykułu + Groq/Gemini → 2-3 zdania
3. **Klucze API** – wpisujesz w ustawieniach apki, trzymane w localStorage, wysyłane przy każdym zapytaniu do backendu

Render.com darmowy plan: aplikacja "zasypia" po 15 min bezczynności, pierwsze zapytanie trwa ~30s.
Aby temu zapobiec – użyj **cron-job.org** i pinguj `https://twoj-backend.onrender.com/api/feed` co 10 minut.
