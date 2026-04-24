import streamlit as st
import anthropic
import json
import os
import base64
import requests
import random
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

# ── Config ────────────────────────────────────────────────────────────────────

_APP_DIR = os.path.dirname(os.path.abspath(__file__))

RECIPES_FILE  = os.path.join(_APP_DIR, "recipes.json")
PLAN_FILE     = os.path.join(_APP_DIR, "meal_plan.json")
LIST_FILE     = os.path.join(_APP_DIR, "shopping_list.json")
HISTORY_FILE  = os.path.join(_APP_DIR, "meal_history.json")
COOKED_FILE   = os.path.join(_APP_DIR, "cooked_history.json")

CONFIG_FILE = os.path.join(_APP_DIR, "config.json")
_config = json.load(open(CONFIG_FILE)) if os.path.exists(CONFIG_FILE) else {}

def get_secret(key):
    try:
        return st.secrets[key]
    except Exception:
        return _config.get(key, "")

ANTHROPIC_KEY   = get_secret("anthropic_key")
SPOONACULAR_KEY = get_secret("spoonacular_key")

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

st.set_page_config(page_title="Weekly Meal Planner", page_icon="🥗", layout="wide")

# ── Browser localStorage (survives server restarts on Streamlit Cloud) ────────

try:
    from streamlit_local_storage import LocalStorage
    _ls = LocalStorage()
except Exception:
    _ls = None

_LS_KEYS = {
    RECIPES_FILE:  "recipes",
    PLAN_FILE:     "meal_plan",
    LIST_FILE:     "shopping_list",
    HISTORY_FILE:  "meal_history",
    COOKED_FILE:   "cooked_history",
}

# ── Persistence ───────────────────────────────────────────────────────────────

def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default

def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass
    if _ls and path in _LS_KEYS:
        try:
            _ls.setItem(_LS_KEYS[path], json.dumps(data, default=str))
        except Exception:
            pass

# ── Session state ─────────────────────────────────────────────────────────────

def ss(key, default):
    if key not in st.session_state:
        st.session_state[key] = default

ss("recipes",        load_json(RECIPES_FILE, []))
ss("meal_plan",      load_json(PLAN_FILE, {}))
ss("shopping_list",  load_json(LIST_FILE, []))
ss("meal_history",   load_json(HISTORY_FILE, []))
ss("cooked_history", load_json(COOKED_FILE, []))
ss("include_sil",    False)
ss("list_view",      "By Aisle")

# Restore from browser localStorage if local files are missing (Streamlit Cloud)
if _ls and not st.session_state.get("_ls_loaded"):
    _needs_restore = {
        "recipes":       not st.session_state.recipes,
        "meal_plan":     not st.session_state.meal_plan,
        "shopping_list": not st.session_state.shopping_list,
        "meal_history":  not st.session_state.meal_history,
        "cooked_history":not st.session_state.cooked_history,
    }
    _restored = False
    for ls_key, needs in _needs_restore.items():
        if needs:
            try:
                val = _ls.getItem(ls_key)
                if val:
                    parsed = json.loads(val) if isinstance(val, str) else val
                    if parsed:
                        st.session_state[ls_key] = parsed
                        _restored = True
            except Exception:
                pass
    if _restored:
        st.session_state["_ls_loaded"] = True

# ── Family & preference strings ───────────────────────────────────────────────

FAMILY_BASE = "2 adults and 3 kids (elementary school age)"

PREFS = """- Healthy lean proteins: chicken thighs/breast, salmon, shrimp, white fish, turkey, eggs, legumes, tofu
- Fresh, high-quality ingredients; fresh herbs always (basil, cilantro, parsley, mint, dill, tarragon)
- Nuanced layered flavors — not bland; often a sauce, pan sauce, herb oil, or tahini drizzle
- Middle Eastern touches welcome: dukkah, labneh, za'atar, sumac, tahini, harissa (mild), preserved lemon
- Mexican flavors welcome: fresh salsas, lime, corn, black beans, cotija, cilantro
- Kid-friendly, NOT spicy (bold but gentle seasoning; hot sauce on the side for adults)
- 30-45 min weeknights; longer OK on weekends
- Equipment: grill, sous vide, wok, oven/sheet pan, pan fry
- Occasional grain bowls or lentil sides welcome but not every week
- Sources: NYT Cooking, Serious Eats, The Modern Proper, Food52, Love and Lemons, Bon Appétit, Epicurious"""

PREFS_SHORT = """- Healthy lean protein, fresh herbs, nuanced flavors, sauce or drizzle
- Kid-friendly not spicy; 30-45 min; vegan sides option
- Middle Eastern / Mexican / Mediterranean / Asian welcome
- Equipment: grill, sous vide, wok, oven, pan fry"""

# ── Ingredient categoriser (no API call) ──────────────────────────────────────

SECTION_MAP = {
    "Meat & Seafood": ["chicken","beef","salmon","fish","shrimp","prawn","turkey","lamb","pork",
                       "tuna","cod","halibut","tilapia","sausage","bacon","ground","steak","fillet","thigh","breast"],
    "Produce":        ["onion","garlic","tomato","bell pepper","zucchini","lemon","lime","orange",
                       "carrot","celery","spinach","kale","lettuce","cucumber","avocado","basil",
                       "parsley","cilantro","mint","dill","potato","broccoli","cauliflower","mushroom",
                       "corn","bok choy","eggplant","squash","asparagus","green bean","arugula","beet",
                       "fennel","leek","shallot","scallion","ginger","jalapeño","serrano","poblano",
                       "mango","apple","berry","peach","cherry tomato","snap pea","fresh herb"],
    "Dairy & Eggs":   ["milk","heavy cream","half and half","butter","cheese","yogurt","egg",
                       "parmesan","mozzarella","cheddar","feta","labneh","crème fraîche",
                       "ricotta","sour cream","greek yogurt","cream cheese"],
    "Bread & Bakery": ["bread","tortilla","pita","naan","baguette","roll","wrap"],
    "Pantry & Spices":["olive oil","vegetable oil","canola oil","sesame oil","coconut oil",
                       "coconut milk","coconut cream","oat milk","almond milk","soy milk",
                       "black pepper","white pepper","red pepper flake","cumin","paprika","turmeric",
                       "coriander","cinnamon","oregano","za'atar","sumac","harissa","dukkah",
                       "salt","sugar","flour","cornstarch","soy sauce","fish sauce","vinegar",
                       "tahini","honey","maple syrup","mustard","hot sauce","worcestershire",
                       "rice","pasta","lentil","chickpea","can","broth","stock","tomato paste"],
    "Frozen":         ["frozen"],
}

def categorize(ingredient: str) -> str:
    low = ingredient.lower()
    # Check Pantry & Spices before Produce to avoid "pepper" false matches
    for section in ["Pantry & Spices","Meat & Seafood","Produce","Dairy & Eggs","Bread & Bakery","Frozen"]:
        if any(k in low for k in SECTION_MAP[section]):
            return section
    return "Pantry & Spices"

# ── Spoonacular ───────────────────────────────────────────────────────────────

def search_spoonacular(query: str, cuisine: str = "", max_time: int = 60) -> dict | None:
    if not SPOONACULAR_KEY:
        return None
    params = {
        "query":                 query,
        "maxReadyTime":          max_time,
        "number":                5,
        "addRecipeInformation":  True,
        "instructionsRequired":  True,
        "fillIngredients":       True,
        "sort":                  "popularity",
        "offset":                random.randint(0, 30),
        "apiKey":                SPOONACULAR_KEY,
    }
    if cuisine:
        params["cuisine"] = cuisine

    try:
        resp = requests.get(
            "https://api.spoonacular.com/recipes/complexSearch",
            params=params, timeout=10
        )
        data = resp.json()
        results = [r for r in data.get("results", []) if r.get("analyzedInstructions")]
        if not results:
            return None
        recipe = results[0]

        ingredients = [i.get("original", "") for i in recipe.get("extendedIngredients", [])]
        instructions = []
        for group in recipe.get("analyzedInstructions", []):
            for step in group.get("steps", []):
                instructions.append(step.get("step", "").strip())
        instructions = [s for s in instructions if s]

        score      = recipe.get("spoonacularScore", 0)
        likes      = recipe.get("aggregateLikes", 0)
        source     = recipe.get("creditsText") or recipe.get("sourceName") or "Spoonacular"
        source_url = recipe.get("sourceUrl", "")
        stars      = "★" * min(5, round(score / 20)) if score else ""
        note       = f"{stars} {score:.0f}/100 Spoonacular score · {likes:,} saves — {source}"

        return {
            "meal_name":      recipe.get("title", ""),
            "description":    recipe.get("summary", "").replace("<b>","").replace("</b>","")[:120] + "…" if recipe.get("summary") else "",
            "protein":        "",
            "sides":          [],
            "prep_time":      f"{recipe.get('preparationMinutes') or ''} min".strip(),
            "cook_time":      f"{recipe.get('cookingMinutes') or recipe.get('readyInMinutes','?')} min",
            "servings":       f"serves {recipe.get('servings','?')} — scale for your family",
            "cooking_method": "",
            "source_site":    source,
            "source_note":    note,
            "search_url":     source_url,
            "vegan_note":     "",
            "ingredients":    ingredients,
            "instructions":   instructions,
        }
    except Exception:
        return None

# ── Claude recipe fallback ────────────────────────────────────────────────────

RECIPE_TEMPLATE = """MEAL_NAME: ...
DESCRIPTION: one enticing sentence
PROTEIN: e.g. chicken thighs
SIDES: side 1 | side 2
PREP_TIME: 15 min
COOK_TIME: 30 min
SERVINGS: serves 5 (2 adults, 3 kids)
COOKING_METHOD: grill
SOURCE_SITE: inspired by NYT Cooking / Serious Eats / etc.
SOURCE_NOTE: why this style of dish is beloved
SEARCH_URL: https://www.google.com/search?q=recipe+name+site
VEGAN_NOTE: which sides are vegan

INGREDIENTS:
- 2 lbs chicken thighs
- 3 cloves garlic, minced

INSTRUCTIONS:
1. First step.
2. Second step."""


def parse_recipe_text(text: str, day: str) -> dict:
    def field(key):
        for line in text.splitlines():
            if line.strip().upper().startswith(f"{key}:"):
                return line[len(key)+1:].strip()
        return ""

    def section(header):
        lines, inside = [], False
        for line in text.splitlines():
            tag = line.strip().rstrip(":").upper()
            if tag == header:
                inside = True
                continue
            if inside:
                if tag in ("INGREDIENTS","INSTRUCTIONS","") and tag != header:
                    if line.strip() == "":
                        continue
                    if any(line.strip().rstrip(":").upper() == h
                           for h in ("INGREDIENTS","INSTRUCTIONS")):
                        break
                stripped = line.lstrip("-•0123456789. ").strip()
                if stripped:
                    lines.append(stripped)
        return lines

    return {
        "day":            day,
        "meal_name":      field("MEAL_NAME"),
        "description":    field("DESCRIPTION"),
        "protein":        field("PROTEIN"),
        "sides":          [s.strip() for s in field("SIDES").split("|") if s.strip()],
        "prep_time":      field("PREP_TIME"),
        "cook_time":      field("COOK_TIME"),
        "servings":       field("SERVINGS"),
        "cooking_method": field("COOKING_METHOD"),
        "source_site":    field("SOURCE_SITE"),
        "source_note":    field("SOURCE_NOTE"),
        "search_url":     field("SEARCH_URL"),
        "vegan_note":     field("VEGAN_NOTE"),
        "ingredients":    section("INGREDIENTS"),
        "instructions":   section("INSTRUCTIONS"),
    }


def claude_generate_night(day: str, include_sil: bool, avoid_proteins: list,
                           extra_notes: str, direction: str = "") -> dict:
    family = FAMILY_BASE + (", plus 1 vegan adult (sister-in-law)" if include_sil else "")
    avoid  = f"Avoid these proteins (already on the plan): {', '.join(avoid_proteins)}." if avoid_proteins else ""
    prompt = f"""Write a complete dinner recipe for {day} for {family}.

{PREFS_SHORT}
{avoid}
{f"This week: {extra_notes}" if extra_notes else ""}
{f"Specifically make: {direction}" if direction.strip() else ""}

Use EXACTLY this plain-text format with no JSON or markdown:

{RECIPE_TEMPLATE}"""

    for attempt in range(2):
        resp = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            night = parse_recipe_text(resp.content[0].text, day)
            if night["meal_name"]:
                return night
        except Exception:
            if attempt == 1:
                raise
    raise RuntimeError("Could not generate recipe")

# ── Meal plan outline ─────────────────────────────────────────────────────────

def plan_outline(nights: int, include_sil: bool, extra_notes: str) -> list:
    days   = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"][:nights]
    family = FAMILY_BASE + (", plus 1 vegan adult" if include_sil else "")
    history = ""
    # Generated plan history
    gen_lines = []
    for w in st.session_state.meal_history[-4:]:
        meals = ", ".join(n["meal_name"] for n in w.get("nights", []))
        gen_lines.append(f"  Week of {w['week_of']}: {meals}")
    # Actually cooked history (more reliable for variety)
    cooked_lines = []
    for c in st.session_state.cooked_history[-20:]:
        cooked_lines.append(f"  {c['date']}: {c['meal_name']}")
    if gen_lines or cooked_lines:
        history = "MEALS ALREADY MADE — do not repeat these:\n"
        if cooked_lines:
            history += "Actually cooked:\n" + "\n".join(cooked_lines) + "\n"
        if gen_lines:
            history += "Recently planned:\n" + "\n".join(gen_lines)

    saved = ""
    if st.session_state.recipes:
        saved = "Family saved favorites: " + ", ".join(r["name"] for r in st.session_state.recipes[:15])

    prompt = f"""Plan {nights} dinners for {family}.

{PREFS}
{f"THIS WEEK: {extra_notes}" if extra_notes else ""}
{history}
{saved}

Rules: no repeated proteins; red meat at most every other week; vary cooking methods.

One line per night using | as delimiter:
DAY | Meal Name | protein | cooking method

Days in order: {', '.join(days)}"""

    resp = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    outline = []
    for line in resp.content[0].text.splitlines():
        line = line.strip().lstrip("0123456789.-) ")  # strip any numbering Claude adds
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        day = parts[0] if parts else ""
        # Only accept lines where first part looks like a day name
        if not any(d in day for d in ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]):
            continue
        outline.append({
            "day":            day,
            "meal_name":      parts[1] if len(parts) > 1 else "",
            "protein":        parts[2] if len(parts) > 2 else "",
            "cooking_method": parts[3] if len(parts) > 3 else "",
        })
    # Fallback: if parsing failed, build a bare outline so generation still runs
    if not outline:
        for d in days:
            outline.append({"day": d, "meal_name": "", "protein": "", "cooking_method": ""})
    return outline[:nights]

# ── Single night: Spoonacular first, Claude fallback ─────────────────────────

def generate_night(item: dict, include_sil: bool, avoid: list, extra_notes: str,
                   saved_recipes: list = None) -> dict:
    day        = item["day"]
    meal_name  = item.get("meal_name", "")
    protein    = item.get("protein", "")
    method     = item.get("cooking_method", "")

    # Check saved recipes first (passed in to avoid thread session-state access)
    for r in (saved_recipes or []):
        if protein and protein.lower() in r.get("name", "").lower():
            night = saved_recipe_to_night(r, day)
            night["protein"] = protein
            night["cooking_method"] = method
            return night

    # Try Spoonacular
    cuisine = ""
    if extra_notes:
        low = extra_notes.lower()
        if "mexican" in low:   cuisine = "mexican"
        elif "asian" in low:   cuisine = "asian"
        elif "italian" in low: cuisine = "italian"
        elif "middle east" in low or "mediterranean" in low: cuisine = "mediterranean"

    night = search_spoonacular(meal_name, cuisine=cuisine)
    if night:
        night["day"]            = day
        night["protein"]        = night.get("protein") or protein
        night["cooking_method"] = night.get("cooking_method") or method
        return night

    # Claude fallback
    night = claude_generate_night(day, include_sil, avoid, extra_notes, meal_name)
    night["protein"]        = night.get("protein") or protein
    night["cooking_method"] = night.get("cooking_method") or method
    return night


def generate_meal_plan(nights: int, include_sil: bool, extra_notes: str) -> dict:
    progress = st.progress(0, text="Choosing meals for the week…")
    outline  = plan_outline(nights, include_sil, extra_notes)
    if not outline:
        progress.empty()
        raise RuntimeError("Could not build a meal outline — please try again.")
    progress.progress(0.1, text=f"Got the plan — pulling {len(outline)} real recipes…")

    results      = [None] * len(outline)
    proteins     = [o.get("protein","") for o in outline]
    saved_snaps  = list(st.session_state.recipes)

    def fetch(i, item):
        try:
            avoid = [p for j, p in enumerate(proteins) if j != i and p]
            return i, generate_night(item, include_sil, avoid, extra_notes, saved_snaps)
        except Exception as e:
            # Return a minimal placeholder so the rest of the plan still loads
            return i, {
                "day": item["day"], "meal_name": f"(Could not load — try replacing)",
                "description": str(e), "protein": "", "sides": [], "prep_time": "",
                "cook_time": "", "servings": "", "cooking_method": "",
                "source_site": "", "source_note": "", "search_url": "",
                "vegan_note": "", "ingredients": [], "instructions": [],
            }

    with ThreadPoolExecutor(max_workers=7) as pool:
        futures = {pool.submit(fetch, i, item): i for i, item in enumerate(outline)}
        done = 0
        for future in as_completed(futures):
            i, night = future.result()
            results[i] = night
            done += 1
            progress.progress(0.1 + 0.85 * done / len(outline),
                              text=f"Got {done} of {len(outline)} recipes…")

    progress.progress(1.0, text="Done!")
    progress.empty()
    nights_clean = [r for r in results if r is not None]
    plan = {"nights": nights_clean}

    st.session_state.meal_history.append({
        "week_of": date.today().strftime("%b %d, %Y"),
        "nights":  [{"meal_name": n.get("meal_name",""), "protein": n.get("protein","")}
                    for n in nights_clean],
    })
    st.session_state.meal_history = st.session_state.meal_history[-8:]
    save_json(HISTORY_FILE, st.session_state.meal_history)
    return plan

# ── Shopping list ─────────────────────────────────────────────────────────────

def is_section_header(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    upper = t.upper()
    if upper.startswith("FOR THE") or upper.startswith("FOR THE "):
        return True
    if t.endswith(":") and t == upper and len(t.split()) <= 6:
        return True
    if t.endswith(":") and len(t.split()) <= 3:
        return True
    return False


def build_shopping_list(plan: dict) -> list:
    raw = []
    for night in plan.get("nights", []):
        meal = night.get("meal_name", night.get("day", ""))
        for ing in night.get("ingredients", []):
            ing = ing.strip()
            if not ing or is_section_header(ing):
                continue
            raw.append({"item": ing, "meal": meal})
    return consolidate_shopping_list(raw)


def consolidate_shopping_list(raw: list) -> list:
    if not raw:
        return []
    lines = "\n".join(f"- {r['item']}  [from: {r['meal']}]" for r in raw)
    prompt = f"""Consolidate this grocery list from multiple recipes into a clean, logical shopping list.

Rules:
- Merge duplicates and near-duplicates ("minced garlic", "garlic cloves", "3 cloves garlic" → all just "garlic")
- Use real store quantities: parsley → "1–2 bunches", lemons → "3 lemons", garlic → "1 head"
- If the same herb appears in many recipes, estimate whether one bunch is enough or more is needed
- Combine lemon uses: zest + juice + squeeze across recipes → total lemons needed
- Skip vague amounts like "pinch of salt" — just include "kosher salt" once
- Black pepper, salt, olive oil → Pantry & Spices
- Bell peppers, jalapeños → Produce (NOT black pepper)
- Do NOT include "a pinch", "to taste" items as separate lines — just include the ingredient once
- Group by section: Produce, Meat & Seafood, Dairy & Eggs, Bread & Bakery, Pantry & Spices, Frozen

Output format — one item per line, pipe-separated:
SECTION | quantity | item name

Example output:
Produce | 1 head | garlic
Produce | 2 | lemons
Produce | 1 bunch | fresh parsley
Meat & Seafood | 2 lbs | chicken thighs
Pantry & Spices | 1 jar | za'atar
Pantry & Spices | | kosher salt
Pantry & Spices | | black pepper

Raw ingredients:
{lines}"""

    resp = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    items = []
    for line in resp.content[0].text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3:
            items.append({"section": parts[0], "quantity": parts[1],
                          "item": parts[2], "meal": "", "checked": False})
        elif len(parts) == 2:
            items.append({"section": "Pantry & Spices", "quantity": "",
                          "item": parts[1], "meal": "", "checked": False})
    return items

# ── Recipe helpers ────────────────────────────────────────────────────────────

def night_to_recipe(night: dict) -> dict:
    return {
        "name":        night.get("meal_name",""),
        "description": night.get("description",""),
        "servings":    night.get("servings",""),
        "prep_time":   night.get("prep_time",""),
        "cook_time":   night.get("cook_time",""),
        "ingredients": night.get("ingredients",[]),
        "instructions":night.get("instructions",[]),
        "tags":        [t for t in [night.get("protein",""), night.get("cooking_method","")] if t],
        "source_url":  night.get("search_url",""),
        "source_site": night.get("source_site",""),
    }

def saved_recipe_to_night(recipe: dict, day: str) -> dict:
    return {
        "day":            day,
        "meal_name":      recipe.get("name",""),
        "description":    recipe.get("description",""),
        "protein":        "",
        "sides":          [],
        "prep_time":      recipe.get("prep_time",""),
        "cook_time":      recipe.get("cook_time",""),
        "servings":       recipe.get("servings","serves 5"),
        "cooking_method": "",
        "source_site":    recipe.get("source_site","My Recipes"),
        "source_note":    "⭐ Saved family favorite",
        "search_url":     recipe.get("source_url",""),
        "vegan_note":     "",
        "ingredients":    recipe.get("ingredients",[]),
        "instructions":   recipe.get("instructions",[]),
    }

def extract_recipe_from_url(url: str) -> dict | None:
    try:
        soup = BeautifulSoup(
            requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10).text,
            "html.parser")
        for tag in soup(["script","style","nav","footer","header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)[:6000]
    except Exception as e:
        st.error(f"Could not fetch URL: {e}")
        return None
    resp = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1500,
        messages=[{"role":"user","content":
            f"Extract the recipe. Return ONLY valid JSON with keys: name, description, servings, prep_time, cook_time, ingredients (list), instructions (list), tags (list). If none found return {{}}.\n\n{text}"}],
    )
    try:
        text2 = resp.content[0].text.strip().strip("```json").strip("```").strip()
        data  = json.loads(text2)
        if data.get("name"):
            data["source_url"] = url
            return data
    except Exception:
        pass
    return None

def extract_recipe_from_image(image_bytes: bytes, mime: str) -> dict | None:
    b64 = base64.standard_b64encode(image_bytes).decode()
    resp = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1500,
        messages=[{"role":"user","content":[
            {"type":"image","source":{"type":"base64","media_type":mime,"data":b64}},
            {"type":"text","text":"Extract the recipe from this image. Return ONLY valid JSON: name, description, servings, prep_time, cook_time, ingredients (list), instructions (list), tags (list). If no recipe, return {}."},
        ]}],
    )
    try:
        text = resp.content[0].text.strip().strip("```json").strip("```").strip()
        data = json.loads(text)
        if data.get("name"):
            return data
    except Exception:
        pass
    return None

# ── Shopping list helpers ─────────────────────────────────────────────────────

def instacart_link(items: list) -> str:
    query = ", ".join(i["item"] for i in items if not i.get("checked"))[:500]
    return f"https://www.instacart.com/store/search_many_products?query={requests.utils.quote(query)}"

def format_list_for_copy(items: list, view: str) -> str:
    lines = ["🛒 Shopping List", ""]
    if view == "By Aisle":
        sections = {}
        for item in items:
            if not item.get("checked"):
                sections.setdefault(item["section"], []).append(item)
        for sec in ["Produce","Meat & Seafood","Dairy & Eggs","Bread & Bakery","Pantry & Spices","Frozen","Other"]:
            if sec in sections:
                lines.append(f"── {sec} ──")
                for i in sections[sec]:
                    qty = f"{i['quantity']} " if i.get("quantity") else ""
                    lines.append(f"  • {qty}{i['item']}")
                lines.append("")
    else:
        meals = {}
        for item in items:
            if not item.get("checked"):
                meals.setdefault(item.get("meal","Other"), []).append(item)
        for meal, its in meals.items():
            lines.append(f"── {meal} ──")
            for i in its:
                qty = f"{i['quantity']} " if i.get("quantity") else ""
                lines.append(f"  • {qty}{i['item']}")
            lines.append("")
    return "\n".join(lines)

# ═════════════════════════════════════════════════════════════════════════════
# UI
# ═════════════════════════════════════════════════════════════════════════════

st.title("🥗 Weekly Meal Planner")
tab_plan, tab_list, tab_recipes = st.tabs(["📅 Meal Plan", "🛒 Shopping List", "📖 My Recipes"])

# ── TAB 1 — MEAL PLAN ────────────────────────────────────────────────────────
with tab_plan:
    st.header("Plan your week")

    col1, col2 = st.columns([1, 1])
    with col1:
        nights = st.number_input("Nights to cook", min_value=1, max_value=7, value=5)
    with col2:
        include_sil = st.checkbox("Sister-in-law joining? (vegan)", value=st.session_state.include_sil)
        st.session_state.include_sil = include_sil

    extra_notes = st.text_area(
        "Anything to work around this week? *(optional)*",
        placeholder=(
            "Tell me what you have, what you're craving, or any constraints — I'll weave it in.\n\n"
            "e.g. I have a whole chicken to use up, leftover dill and basil. "
            "Let's do Mexican one night. My son wants homemade black beans."
        ),
        height=110,
    )

    if st.button("✨ Generate Meal Plan", type="primary", use_container_width=True):
        # Step 1: generate the meal plan — show it immediately on success
        try:
            plan = generate_meal_plan(nights, include_sil, extra_notes)
            if not plan.get("nights"):
                st.error("No meals were generated — please try again.")
                st.stop()
            st.session_state.meal_plan = plan
            save_json(PLAN_FILE, plan)
            st.success(f"✅ {len(plan['nights'])} dinners planned! Scroll down to see them.")
        except Exception as e:
            st.error(f"Meal plan failed: {e}")
            st.stop()

        # Step 2: build shopping list separately so a failure here doesn't hide the plan
        try:
            with st.spinner("Building your shopping list…"):
                shopping = build_shopping_list(plan)
            st.session_state.shopping_list = shopping
            save_json(LIST_FILE, shopping)
            st.info(f"🛒 {len(shopping)} items added to your shopping list.")
        except Exception as e:
            st.warning(f"Shopping list couldn't be built: {e} — your meal plan is still saved.")

    if st.session_state.meal_plan.get("nights"):
        st.divider()
        for i, night in enumerate(st.session_state.meal_plan["nights"]):
            method = night.get("cooking_method","").lower()
            icon   = {"grill":"🔥","sous vide":"🌡️","wok":"🥢","oven":"🫙","pan fry":"🍳"}.get(method,"🍽️")
            header = f"{icon} **{night['day']}** — {night['meal_name']}"
            if night.get("prep_time") or night.get("cook_time"):
                header += f"  ·  {night.get('prep_time','')} prep · {night.get('cook_time','')} cook"

            with st.expander(header):
                if night.get("description"):
                    st.write(f"*{night['description']}*")

                # Source badge
                site  = night.get("source_site","")
                note  = night.get("source_note","")
                url   = night.get("search_url","")
                if site or note:
                    badge = f"**{site}** — {note}" if site else note
                    if url:
                        badge += f"  [→ View original recipe]({url})"
                    st.info(badge)

                st.divider()
                col_l, col_r = st.columns(2)
                with col_l:
                    if night.get("servings"):
                        st.write(f"**Serves:** {night['servings']}")
                    if night.get("protein"):
                        st.write(f"**Protein:** {night['protein']}")
                    if night.get("sides"):
                        st.write(f"**Sides:** {', '.join(night['sides'])}")
                    if include_sil and night.get("vegan_note"):
                        st.success(f"🌱 {night['vegan_note']}")
                    st.write("")
                    if night.get("ingredients"):
                        st.write("**Ingredients:**")
                        for ing in night["ingredients"]:
                            if is_section_header(ing):
                                st.write(f"**{ing.rstrip(':')}**")
                            else:
                                st.write(f"• {ing}")

                with col_r:
                    if night.get("instructions"):
                        st.write("**Instructions:**")
                        for n, step in enumerate(night["instructions"], 1):
                            st.write(f"**{n}.** {step}")
                    else:
                        st.warning("No instructions found. Try replacing this meal.")

                st.divider()

                # Save to recipes + We made this
                btn_col1, btn_col2 = st.columns(2)
                with btn_col1:
                    already = any(r.get("name") == night.get("meal_name") for r in st.session_state.recipes)
                    if already:
                        st.caption("✅ Saved to My Recipes")
                    elif st.button("⭐ Save to My Recipes", key=f"save_{i}"):
                        st.session_state.recipes.append(night_to_recipe(night))
                        save_json(RECIPES_FILE, st.session_state.recipes)
                        st.rerun()
                with btn_col2:
                    already_cooked = any(
                        c.get("meal_name") == night.get("meal_name")
                        for c in st.session_state.cooked_history[-30:]
                    )
                    if already_cooked:
                        st.caption("✅ Marked as cooked")
                    elif st.button("✓ We made this!", key=f"cooked_{i}"):
                        st.session_state.cooked_history.append({
                            "date":       date.today().isoformat(),
                            "meal_name":  night.get("meal_name", ""),
                            "protein":    night.get("protein", ""),
                            "source_url": night.get("search_url", ""),
                        })
                        save_json(COOKED_FILE, st.session_state.cooked_history)
                        st.rerun()

                st.divider()

                # Replace
                st.write("**Not feeling this one?**")
                if st.session_state.recipes:
                    opts = ["— pick a saved recipe —"] + [r["name"] for r in st.session_state.recipes]
                    picked = st.selectbox("Saved recipe", opts, key=f"pick_{i}", label_visibility="collapsed")
                    if picked != "— pick a saved recipe —":
                        if st.button(f'Use "{picked}" for {night["day"]}', key=f"use_{i}"):
                            r = next(r for r in st.session_state.recipes if r["name"] == picked)
                            st.session_state.meal_plan["nights"][i] = saved_recipe_to_night(r, night["day"])
                            save_json(PLAN_FILE, st.session_state.meal_plan)
                            st.session_state.shopping_list = build_shopping_list(st.session_state.meal_plan)
                            save_json(LIST_FILE, st.session_state.shopping_list)
                            st.rerun()

                col_d, col_b = st.columns([3,1])
                with col_d:
                    direction = st.text_input("Or describe what you want",
                        placeholder='e.g. "herby spring pasta" or "something on the grill with salmon"',
                        key=f"dir_{i}", label_visibility="collapsed")
                with col_b:
                    if st.button("Find new meal", key=f"rep_{i}"):
                        with st.spinner(f"Finding a new {night['day']} dinner…"):
                            try:
                                avoid  = [n.get("protein","") for n in st.session_state.meal_plan["nights"] if n.get("day") != night["day"]]
                                outline_item = {"day": night["day"], "meal_name": direction or "", "protein":"", "cooking_method":""}
                                new = generate_night(outline_item, include_sil, avoid, direction)
                                st.session_state.meal_plan["nights"][i] = new
                                save_json(PLAN_FILE, st.session_state.meal_plan)
                                st.session_state.shopping_list = build_shopping_list(st.session_state.meal_plan)
                                save_json(LIST_FILE, st.session_state.shopping_list)
                                st.rerun()
                            except Exception as e:
                                st.error(f"Could not replace: {e}")

# ── TAB 2 — SHOPPING LIST ────────────────────────────────────────────────────
with tab_list:
    st.header("Shopping List")

    if not st.session_state.shopping_list:
        st.info("Generate a meal plan first — your shopping list will appear here automatically.")
    else:
        # Controls row
        ctl1, ctl2 = st.columns([2, 2])
        with ctl1:
            view = st.radio("Sort by", ["By Aisle", "By Meal"], horizontal=True,
                            index=0 if st.session_state.list_view == "By Aisle" else 1)
            st.session_state.list_view = view
        with ctl2:
            unchecked = sum(1 for i in st.session_state.shopping_list if not i.get("checked"))
            st.metric("Items remaining", unchecked)

        # Share / send row
        formatted = format_list_for_copy(st.session_state.shopping_list, st.session_state.list_view)
        email_body = requests.utils.quote(formatted)
        sms_body   = requests.utils.quote(formatted)

        sh1, sh2, sh3, sh4 = st.columns(4)
        with sh1:
            st.link_button("🛒 Instacart", "https://www.instacart.com/store", use_container_width=True)
        with sh2:
            st.link_button("💬 Text to yourself", f"sms:&body={sms_body}", use_container_width=True)
        with sh3:
            st.link_button("📧 Email to yourself", f"mailto:?subject=Shopping%20List&body={email_body}", use_container_width=True)
        with sh4:
            if st.button("🗑 Clear checked", use_container_width=True):
                st.session_state.shopping_list = [i for i in st.session_state.shopping_list if not i.get("checked")]
                save_json(LIST_FILE, st.session_state.shopping_list)
                st.rerun()

        # Add item
        a1, a2, a3 = st.columns([3, 1, 1])
        with a1:
            new_item = st.text_input("Add item", placeholder="e.g. olive oil", label_visibility="collapsed")
        with a2:
            new_section = st.selectbox("Section", ["Produce","Meat & Seafood","Dairy & Eggs",
                                                    "Bread & Bakery","Pantry & Spices","Frozen"], label_visibility="collapsed")
        with a3:
            if st.button("+ Add", use_container_width=True) and new_item.strip():
                st.session_state.shopping_list.append({
                    "item": new_item.strip(), "quantity":"", "section": new_section,
                    "meal":"Manual", "checked": False,
                })
                save_json(LIST_FILE, st.session_state.shopping_list)
                st.rerun()

        st.divider()

        # Render list
        changed = False
        items = st.session_state.shopping_list

        if view == "By Aisle":
            groups = {}
            for idx, item in enumerate(items):
                groups.setdefault(item.get("section","Pantry"), []).append((idx, item))
            order = ["Produce","Meat & Seafood","Dairy & Eggs","Bread & Bakery","Pantry & Spices","Frozen","Other"]
            for sec in order:
                if sec not in groups:
                    continue
                st.subheader(sec)
                for idx, item in groups[sec]:
                    label = f"{item['quantity']} {item['item']}".strip() if item.get("quantity") else item["item"]
                    c1, c2 = st.columns([11, 1])
                    with c1:
                        checked = st.checkbox(label, value=item.get("checked",False),
                                              key=f"chk_{idx}", help=None)
                        if checked != item.get("checked", False):
                            st.session_state.shopping_list[idx]["checked"] = checked
                            changed = True
                    with c2:
                        if st.button("✕", key=f"del_{idx}"):
                            st.session_state.shopping_list.pop(idx)
                            save_json(LIST_FILE, st.session_state.shopping_list)
                            st.rerun()
        else:  # By Meal
            meals = {}
            for idx, item in enumerate(items):
                meals.setdefault(item.get("meal","Other"), []).append((idx, item))
            for meal, its in meals.items():
                st.subheader(meal)
                for idx, item in its:
                    label = f"{item['quantity']} {item['item']}".strip() if item.get("quantity") else item["item"]
                    c1, c2 = st.columns([11, 1])
                    with c1:
                        checked = st.checkbox(label, value=item.get("checked",False), key=f"chk_{idx}")
                        if checked != item.get("checked", False):
                            st.session_state.shopping_list[idx]["checked"] = checked
                            changed = True
                    with c2:
                        if st.button("✕", key=f"del_{idx}"):
                            st.session_state.shopping_list.pop(idx)
                            save_json(LIST_FILE, st.session_state.shopping_list)
                            st.rerun()

        if changed:
            save_json(LIST_FILE, st.session_state.shopping_list)

        # Copy / text to self
        st.divider()
        with st.expander("📋 Copy full list"):
            st.caption("Click the copy icon in the top-right of the box below, then paste into a text or note.")
            st.code(format_list_for_copy(st.session_state.shopping_list, view), language=None)

# ── TAB 3 — MY RECIPES ───────────────────────────────────────────────────────
with tab_recipes:
    st.header("My Recipe Collection")
    st.subheader("Add a recipe")
    method = st.radio("How?", ["Paste a URL","Upload a photo","Enter manually"], horizontal=True)

    if method == "Paste a URL":
        url = st.text_input("Recipe URL", placeholder="https://cooking.nytimes.com/recipes/…")
        if st.button("Import", type="primary") and url.strip():
            with st.spinner("Fetching…"):
                r = extract_recipe_from_url(url.strip())
            if r:
                st.session_state.recipes.append(r)
                save_json(RECIPES_FILE, st.session_state.recipes)
                st.success(f"Added: {r['name']}")
                st.rerun()
            else:
                st.error("Could not extract recipe. Try entering manually.")

    elif method == "Upload a photo":
        st.caption("Upload multiple photos if the recipe spans several screenshots.")
        uploads = st.file_uploader("Recipe photo(s)", type=["jpg","jpeg","png","webp"],
                                   accept_multiple_files=True)
        if uploads and st.button("Extract Recipe", type="primary"):
            mime_map = {"jpg":"image/jpeg","jpeg":"image/jpeg","png":"image/png","webp":"image/webp"}
            with st.spinner(f"Reading {len(uploads)} photo(s)…"):
                all_ingredients, all_instructions, base = [], [], {}
                for up in uploads:
                    mime = mime_map.get(up.name.split(".")[-1].lower(), "image/jpeg")
                    r = extract_recipe_from_image(up.read(), mime)
                    if r:
                        if not base.get("name"):
                            base = r
                        else:
                            all_ingredients += r.get("ingredients", [])
                            all_instructions += r.get("instructions", [])
                if base.get("name"):
                    base["ingredients"] = base.get("ingredients", []) + all_ingredients
                    base["instructions"] = base.get("instructions", []) + all_instructions
                    st.session_state.recipes.append(base)
                    save_json(RECIPES_FILE, st.session_state.recipes)
                    st.success(f"Added: {base['name']} ({len(base['ingredients'])} ingredients, {len(base['instructions'])} steps)")
                    st.rerun()
                else:
                    st.error("No recipe found in those photos.")

    else:
        with st.form("manual"):
            name = st.text_input("Recipe name *")
            desc = st.text_input("Description")
            c1, c2, c3 = st.columns(3)
            with c1: srv = st.text_input("Servings", placeholder="4-6")
            with c2: pre = st.text_input("Prep time", placeholder="15 min")
            with c3: cok = st.text_input("Cook time", placeholder="30 min")
            ings  = st.text_area("Ingredients (one per line)")
            insts = st.text_area("Instructions (one per line)")
            tags  = st.text_input("Tags", placeholder="kid-friendly, vegan, grill")
            if st.form_submit_button("Save", type="primary") and name.strip():
                st.session_state.recipes.append({
                    "name": name.strip(), "description": desc,
                    "servings": srv, "prep_time": pre, "cook_time": cok,
                    "ingredients": [l.strip() for l in ings.splitlines() if l.strip()],
                    "instructions": [l.strip() for l in insts.splitlines() if l.strip()],
                    "tags": [t.strip() for t in tags.split(",") if t.strip()],
                })
                save_json(RECIPES_FILE, st.session_state.recipes)
                st.success(f"Saved: {name}")
                st.rerun()

    if st.session_state.recipes:
        st.divider()
        st.subheader(f"Saved recipes ({len(st.session_state.recipes)})")
        for i, r in enumerate(st.session_state.recipes):
            label = f"**{r['name']}**"
            if r.get("tags"): label += f"  —  {', '.join(r['tags'])}"
            with st.expander(label):
                if r.get("description"): st.write(f"*{r['description']}*")
                c1, c2 = st.columns(2)
                with c1:
                    for lbl, k in [("Serves","servings"),("Prep","prep_time"),("Cook","cook_time")]:
                        if r.get(k): st.write(f"**{lbl}:** {r[k]}")
                    if r.get("source_url"): st.write(f"**Source:** [link]({r['source_url']})")
                    if r.get("ingredients"):
                        st.write("**Ingredients:**")
                        for ing in r["ingredients"]: st.write(f"• {ing}")
                with c2:
                    if r.get("instructions"):
                        st.write("**Instructions:**")
                        for j, s in enumerate(r["instructions"],1): st.write(f"**{j}.** {s}")
                if st.button("Delete", key=f"del_r_{i}"):
                    st.session_state.recipes.pop(i)
                    save_json(RECIPES_FILE, st.session_state.recipes)
                    st.rerun()
    else:
        st.info("No recipes saved yet. Add some above — they'll be prioritized in future meal plans.")
