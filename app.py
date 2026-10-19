import os, re, requests
from functools import lru_cache
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder="static")

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")  # change in Render if needed
TCG_KEY = os.environ.get("TCG_API_KEY", "")  # optional

SYSTEM = """You are DexGPT, an AI made by an OG Pokémon fan.
Talk like a real Pokémon trainer: fun, hype, and knowledgeable.
Cover games, Pokédex, TCG cards, anime, and the Pokémon channel.
Use the DATA block for stats, moves, and card details. Never invent stats.
If DATA is missing, say you're not sure. Stay on Pokémon topics only.
Keep answers short and easy to read.

MY OWN NOTES:
- Favorite Pokémon: (add your picks)
- Favorite games/generations: (add your picks)
- Opinions, tier lists, tips, nostalgia: (add anything you want)
"""

HEADERS = {"User-Agent": "DexGPT/1.0"}


@lru_cache(maxsize=1)
def all_names():
    try:
        r = requests.get("https://pokeapi.co/api/v2/pokemon?limit=1100",
                         headers=HEADERS, timeout=20)
        return {p["name"] for p in r.json()["results"]}
    except Exception:
        return set()


def find_pokemon(text):
    names = all_names()
    words = re.findall(r"[a-z]+(?:-[a-z]+)*", text.lower())
    seen = []
    for w in words:
        if w in names and w not in seen:
            seen.append(w)
    return seen[:2]


@lru_cache(maxsize=300)
def pokedex_data(name):
    try:
        r = requests.get(f"https://pokeapi.co/api/v2/pokemon/{name}",
                         headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return "", ""
        d = r.json()
    except Exception:
        return "", ""
    stats = {s["stat"]["name"]: s["base_stat"] for s in d["stats"]}
    types = [t["type"]["name"] for t in d["types"]]
    abilities = [a["ability"]["name"] for a in d["abilities"]]
    moves = [m["move"]["name"] for m in d["moves"]][:20]
    text = (f"POKEDEX {name} (#{d['id']}): types={types}, abilities={abilities}, "
            f"base stats={stats}, height={d['height']/10}m, weight={d['weight']/10}kg, "
            f"sample moves={moves}")
    sprite = (f"https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/"
              f"pokemon/other/official-artwork/{d['id']}.png")
    return text, sprite


@lru_cache(maxsize=300)
def card_data(name):
    try:
        h = dict(HEADERS)
        if TCG_KEY:
            h["X-Api-Key"] = TCG_KEY
        r = requests.get("https://api.pokemontcg.io/v2/cards", headers=h,
                         params={"q": f'name:"{name}"', "pageSize": 5,
                                 "orderBy": "-set.releaseDate"}, timeout=25)
        if r.status_code != 200:
            return "", []
        cards = r.json().get("data", [])
    except Exception:
        return "", []
    lines, images = [], []
    for c in cards:
        prices = (c.get("tcgplayer", {}) or {}).get("prices", {}) or {}
        lines.append(f"{c['name']} | {c['set']['name']} | {c.get('rarity')} | "
                     f"HP {c.get('hp')} | prices {prices}")
        img = (c.get("images") or {}).get("small")
        if img:
            images.append(img)
    return "TCG CARDS:\n" + "\n".join(lines), images[:3]


def ask_gemini(history, question, data):
    contents = []
    for m in history[-8:]:
        role = "user" if m.get("role") == "user" else "model"
        contents.append({"role": role, "parts": [{"text": str(m.get("text", ""))[:2000]}]})
    contents.append({"role": "user",
                     "parts": [{"text": f"DATA:\n{data}\n\nQUESTION: {question}"}]})
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM}]},
        "contents": contents,
        "tools": [{"google_search": {}}],  # web search for anime/channel/news
    }
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent",
            params={"key": GEMINI_KEY}, json=body, timeout=60)
        j = r.json()
        parts = j["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except Exception:
        return "Oops, DexGPT couldn't get an answer. Check the Gemini key and try again."


@app.post("/chat")
def chat():
    payload = request.get_json(silent=True) or {}
    q = (payload.get("message") or "").strip()[:1000]
    history = payload.get("history") or []
    if not q:
        return jsonify({"reply": "Ask me something about Pokémon!"})

    data, sprites, cards = [], [], []
    wants_card = re.search(r"\b(card|cards|tcg|booster|holo|rarity|price|worth)\b", q.lower())
    for m in find_pokemon(q):
        text, sprite = pokedex_data(m)
        if text:
            data.append(text)
        if sprite:
            sprites.append(sprite)
        if wants_card:
            ctext, cimgs = card_data(m)
            if ctext:
                data.append(ctext)
            cards += cimgs

    reply = ask_gemini(history, q, "\n".join(data))
    return jsonify({"reply": reply, "sprites": sprites[:2], "cards": cards[:3]})


@app.get("/")
def home():
    return send_from_directory("static", "index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
