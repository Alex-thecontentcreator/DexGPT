import os, re, requests
from functools import lru_cache
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder="static")

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")  # change in Render if needed
TCG_KEY = os.environ.get("TCG_API_KEY", "")  # optional

SYSTEM = """You are DexGPT, an AI made by an OG Pokémon fan.
Talk like a real Pokémon trainer: fun, hype, and knowledgeable.
Cover games, Pokédex, TCG cards, anime, and the Pokémon channel.
Use the DATA block for stats, moves, and card details. Never invent stats.
If DATA is missing, say you're not sure. Stay on Pokémon topics only.
Keep answers short and easy to read.

POKEMON UNITE RULES:
- Unite is rebalanced about once a month. Moves, numbers, builds, tiers and
  counters change every patch, so never answer those from memory alone.
- For any Unite question about a specific Pokémon, build, tier, counter or
  recent change, search the web first (official patch notes at pokemonunite.jp
  and unite-db.com) and say which patch your answer is based on.
- If you cannot confirm a move name, number, build or counter, say so.
  Never invent Unite moves or stats.

POKEMON UNITE BASICS (stable facts):
- 5v5 team game. Matches last 10 minutes. Main map: Theia Sky Ruins.
- Teams score by carrying Aeos energy (from wild Pokémon) to the enemy goal zones.
  Scoring takes a moment and can be interrupted, so score when safe.
- Big wild Pokémon objectives (Regieleki, Regirock, Regice, Registeel and
  others) give team advantages. Rayquaza appears near the end of the match
  and is the biggest late-game fight.
- Roles: Attacker (ranged damage, fragile), Defender (tanky, crowd control),
  Speedster (fast, hunts targets), Supporter (heals, shields, helps allies),
  All-Rounder (balanced, strong in duels).
- Held items and one battle item are chosen before the match. Full Heal clears
  status effects, so it is a common answer to crowd control.

POKEMON UNITE STRATEGY (general advice):
- Early game: farm wild Pokémon safely, level up, and do not dive enemy goals alone.
- Mid game: group up for objectives, rotate to help teammates, and keep levels even.
- Late game: group as a team before Rayquaza, do not start it blindly while the
  enemy team is alive, and win a fight first.
- Track enemy Unite Move readiness. Fight when it is on cooldown.
- Never fight with low HP or an unready Unite Move when you can avoid it.
- To climb in Ranked: learn two or more roles, stay alive, play for objectives
  over kills, and communicate with pings.

MY OWN NOTES:
- Favorite Pokémon: (add your picks)
- Favorite games/generations: (add your picks)
- My Pokémon Unite main, builds I trust, and tips: (add your own)
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
    base = {"system_instruction": {"parts": [{"text": SYSTEM}]}, "contents": contents}

    # try your chosen model first, then backups if Google is busy
    models = []
    for m in [MODEL, "gemini-3.5-flash", "gemini-flash-latest", "gemini-3.5-flash-lite"]:
        if m not in models:
            models.append(m)

    last_error = "unknown error"
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        # if the message has a link, let Gemini read that page too
        if re.search(r"https?://\S+", question):
            variants = [[{"google_search": {}}, {"url_context": {}}],
                        [{"url_context": {}}], None]
        else:
            variants = [[{"google_search": {}}], None]
        for tools in variants:
            body = dict(base)
            if tools:
                body["tools"] = tools
            try:
                r = requests.post(url, params={"key": GEMINI_KEY}, json=body, timeout=40)
                j = r.json()
            except Exception as e:
                last_error = str(e)
                print("GEMINI EXCEPTION:", model, last_error, flush=True)
                break
            if "error" in j:
                last_error = j["error"].get("message", str(j["error"]))
                print("GEMINI ERROR:", model, last_error, flush=True)
                low = last_error.lower()
                if "demand" in low or "overloaded" in low or "quota" in low or "unavailable" in low:
                    break  # busy: go to the next model
                continue  # maybe the search tool is the problem: retry without it
            try:
                parts = j["candidates"][0]["content"]["parts"]
                return "".join(p.get("text", "") for p in parts)
            except Exception as e:
                last_error = f"empty reply ({e})"
                break
    return f"DexGPT error: {last_error[:300]}"


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
