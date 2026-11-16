"""Curl Check: a hair-routine app for wavy, curly, coily and loc'd hair.

People create an account, take a 1-minute quiz and get a personal routine
(3 steps, 2-3 products for their budget, one habit to stop). Optional profile
details (water, climate, pillow, heat, medication) add extra tips. Feedback
(usefulness 1-10 and usual search time) feeds the team dashboard used for
the project's Traction and Impact numbers.

Storage: Supabase when SUPABASE_URL and SUPABASE_KEY are set in Streamlit
secrets, otherwise a local SQLite file (for testing only).
"""

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Curl Check", page_icon="🌀", layout="centered", initial_sidebar_state="collapsed")

# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

JSON_FIELDS = ("quiz", "details", "routine", "feedback")


def _secret(name, default=None):
    """Read a Streamlit secret or environment variable, without crashing if none exist."""
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name, default)


class SupabaseStore:
    """Stores members in a Supabase table called `members` through its REST API."""

    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/rest/v1/members"
        self.entries = url.rstrip("/") + "/rest/v1/entries"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def get(self, username):
        """Return one member as a dict, or None if the username doesn't exist."""
        r = requests.get(self.base, headers=self.headers,
                         params={"username": f"eq.{username}", "select": "*"}, timeout=15)
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None

    def create(self, row):
        """Insert a new member row."""
        r = requests.post(self.base, headers={**self.headers, "Prefer": "return=minimal"},
                          data=json.dumps(row), timeout=15)
        r.raise_for_status()

    def update(self, username, fields):
        """Update some fields of one member."""
        r = requests.patch(self.base, headers={**self.headers, "Prefer": "return=minimal"},
                           params={"username": f"eq.{username}"}, data=json.dumps(fields), timeout=15)
        r.raise_for_status()

    def add_entry(self, kind, username, data):
        """Add a review, post, reply or tracker log to the `entries` table."""
        r = requests.post(self.entries, headers={**self.headers, "Prefer": "return=minimal"},
                          data=json.dumps({"kind": kind, "username": username, "data": data}), timeout=15)
        r.raise_for_status()

    def list_entries(self, kind, username=None, limit=300):
        """Newest entries of one kind, optionally for one member."""
        params = {"kind": f"eq.{kind}", "select": "*", "order": "created_at.desc", "limit": str(limit)}
        if username:
            params["username"] = f"eq.{username}"
        r = requests.get(self.entries, headers=self.headers, params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def all(self):
        """Return every member (without password hashes)."""
        r = requests.get(self.base, headers=self.headers, params={
            "select": "username,first_name,created_at,quiz,details,routine,feedback,updated_at"}, timeout=15)
        r.raise_for_status()
        return r.json()


class SQLiteStore:
    """Local fallback with the same interface as SupabaseStore. Data resets on Streamlit Cloud."""

    def __init__(self, path="curlcheck_local.db"):
        self.path = path
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS members (
                username TEXT PRIMARY KEY, first_name TEXT, pw_hash TEXT,
                created_at TEXT, updated_at TEXT,
                quiz TEXT, details TEXT, routine TEXT, feedback TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, username TEXT,
                created_at TEXT, data TEXT)""")

    def _conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

    @staticmethod
    def _decode(row):
        d = dict(row)
        for f in JSON_FIELDS:
            d[f] = json.loads(d[f]) if d.get(f) else None
        return d

    def get(self, username):
        """Return one member as a dict, or None."""
        with self._conn() as c:
            row = c.execute("SELECT * FROM members WHERE username=?", (username,)).fetchone()
        return self._decode(row) if row else None

    def create(self, row):
        """Insert a new member row."""
        row = {k: (json.dumps(v) if k in JSON_FIELDS and v is not None else v) for k, v in row.items()}
        cols = ",".join(row)
        with self._conn() as c:
            c.execute(f"INSERT INTO members ({cols}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))

    def update(self, username, fields):
        """Update some fields of one member."""
        fields = {k: (json.dumps(v) if k in JSON_FIELDS and v is not None else v) for k, v in fields.items()}
        sets = ",".join(f"{k}=?" for k in fields)
        with self._conn() as c:
            c.execute(f"UPDATE members SET {sets} WHERE username=?", (*fields.values(), username))

    def add_entry(self, kind, username, data):
        """Add a review, post, reply or tracker log."""
        with self._conn() as c:
            c.execute("INSERT INTO entries (kind, username, created_at, data) VALUES (?,?,?,?)",
                      (kind, username, now_iso(), json.dumps(data)))

    def list_entries(self, kind, username=None, limit=300):
        """Newest entries of one kind, optionally for one member."""
        q, args = "SELECT * FROM entries WHERE kind=?", [kind]
        if username:
            q += " AND username=?"
            args.append(username)
        with self._conn() as c:
            rows = c.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def all(self):
        """Return every member (without password hashes)."""
        with self._conn() as c:
            rows = c.execute("SELECT username,first_name,created_at,quiz,details,routine,feedback,updated_at "
                             "FROM members").fetchall()
        return [self._decode(r) for r in rows]


@st.cache_resource
def get_store():
    """Pick Supabase if configured, otherwise local SQLite."""
    url, key = _secret("SUPABASE_URL"), _secret("SUPABASE_KEY")
    if url and key:
        return SupabaseStore(url, key), "supabase"
    return SQLiteStore(), "local"


STORE, STORE_KIND = get_store()

# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------


def hash_password(password):
    """Hash a password with PBKDF2-SHA256 and a random salt. Returns 'salt$hash'."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${digest}"


def check_password(password, stored):
    """Return True if the password matches the stored 'salt$hash'."""
    if isinstance(stored, str) and stored.startswith("temp:"):
        stored = stored[5:]
    try:
        salt, digest = stored.split("$", 1)
    except (AttributeError, ValueError):
        return False
    test = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return hmac.compare_digest(test, digest)


def is_temp(member):
    """True if the member still has the temporary password the team gave them."""
    return str(member.get("pw_hash", "")).startswith("temp:")


def temp_password():
    """A short, readable temporary password like 'curl-4821'."""
    return "curl-" + "".join(secrets.choice("0123456789") for _ in range(4))


def now_iso():
    """Current UTC time as an ISO string."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

# --------------------------------------------------------------------------
# Quiz options and routine rules
# --------------------------------------------------------------------------


HAIR_TYPES = {"wavy": "Wavy (2A–2C): loose S-shapes", "curly": "Curly (3A–3C): loops or spirals",
              "coily": "Coily (4A–4C): tight coils or zig-zags", "locs": "Locs", "unsure": "Not sure"}
POROSITY = {"low": "It floats (low porosity)", "medium": "It floats in the middle (medium porosity)",
            "high": "It sinks (high porosity)", "unsure": "I didn't do the test"}
PROBLEMS = {"frizz": "Frizz", "dry": "Dryness", "break": "Breakage or split ends", "lost": "I don't know what to buy",
            "define": "Curls not defined", "flat": "Flat, no volume", "scalp": "Itchy or flaky scalp"}
WASH = {"daily": "Every day", "few": "2–3 times a week", "weekly": "Once a week", "rare": "Every 2 weeks or less"}
BUDGET = {"low": "Under €10", "mid": "€10–€25", "high": "Over €25"}

WATER = {"unknown": "I don't know", "hard": "Hard (white marks on the tap or kettle)", "soft": "Soft"}
CLIMATE = {"unknown": "Prefer not to say", "humid": "Hot and humid", "dry_hot": "Hot and dry",
           "cold": "Cold or very windy", "mixed": "Mild or it changes a lot"}
PILLOW = {"unknown": "Prefer not to say", "cotton": "Cotton pillowcase", "satin": "Silk or satin pillowcase",
          "bonnet": "I sleep with a bonnet or scarf"}
HEAT = {"never": "Never", "sometimes": "Sometimes (once a month or less)", "often": "Often (every week)"}
CHEMICAL = {"no": "No", "colour": "Coloured or bleached", "relaxed": "Relaxed or straightened chemically"}
MEDICATION = {"skip": "Prefer not to say", "no": "No", "yes": "Yes"}

PRODUCTS = {
    "shampoo": {"low": ("Sulfate-free shampoo, e.g. Garnier Fructis Hair Food", 6),
                "mid": ("Gentle curl cleanser, e.g. Shea Moisture Curl & Shine", 14),
                "high": ("Curl cleanser, e.g. Bouclème Curl Cleanser", 27)},
    "clarify": {"low": ("Clarifying shampoo, e.g. Cantu Sulfate-Free Cleansing Cream", 8),
                "mid": ("Clarifying shampoo, e.g. Mielle Rosemary Mint", 15),
                "high": ("Detox shampoo, e.g. Bouclème Root Revitaliser", 28)},
    "conditioner": {"low": ("Rich conditioner, e.g. Aussie 3 Minute Miracle", 5),
                    "mid": ("Moisture mask, e.g. Les Secrets de Loly Pink Paradise", 19),
                    "high": ("Repair mask, e.g. Bouclème Intensive Moisture Treatment", 30)},
    "light": {"low": ("Light curl gel, e.g. Garnier Fructis Curl Gel", 5),
              "mid": ("Light gel, e.g. Mielle Pomegranate & Honey Curl Jelly", 16),
              "high": ("Curl gel, e.g. Bouclème Curl Defining Gel", 26)},
    "cream": {"low": ("Leave-in cream, e.g. Cantu Shea Butter Leave-In", 8),
              "mid": ("Curl cream, e.g. Shea Moisture Curl Enhancing Smoothie", 15),
              "high": ("Curl cream, e.g. Bouclème Curl Cream", 27)},
    "butter": {"low": ("Shea butter cream to seal, e.g. Cantu Coconut Curling Cream", 8),
               "mid": ("Butter to seal, e.g. Les Secrets de Loly Curly Custard", 18),
               "high": ("Sealing oil, e.g. Mielle Rosemary Mint Oil", 26)},
    "scalp": {"low": ("Light scalp oil (a few drops of jojoba oil)", 7),
              "mid": ("Scalp serum, e.g. Mielle Rosemary Mint Scalp Oil", 12),
              "high": ("Scalp treatment, e.g. Bouclème Scalp Serum", 26)},
}

STOP = {
    "lost": "Stop buying a new product every time you see a trend. Use this routine for 4 weeks before changing anything.",
    "frizz": "Stop rubbing your hair with a bath towel. Squeeze it gently with a cotton T-shirt instead.",
    "dry": "Stop washing with hot water. Use lukewarm water and finish with a cool rinse.",
    "break": "Stop brushing dry hair. Detangle only when wet, with conditioner in, from the ends up.",
    "define": "Stop touching your hair while it dries. Let it dry fully, then scrunch out the crunch.",
    "flat": "Stop putting heavy butters or oils on your roots. Keep them on the lengths only.",
    "scalp": "Stop putting heavy oils and butters on your scalp. Keep it clean and use only a few drops of light oil.",
}


def build_routine(quiz, details=None):
    """Turn quiz answers (and optional profile details) into a routine dict.

    Returns {"steps": [(title, text)], "products": [(name, price)], "stop": str, "extra_tips": [str]}.
    """
    details = details or {}
    t, p, b, prob = quiz["type"], quiz["poro"], quiz["budget"], quiz["problem"]
    steps, prods = [], []

    # Step 1: wash
    if t == "locs":
        wash = ("Wash with a residue-free shampoo, focusing on your scalp. Squeeze the water out and let "
                "your locs dry fully so they don't smell.")
    elif quiz["wash"] == "daily":
        wash = "Shampoo only 1–2 times a week. On other days, just rinse with water or a little conditioner (co-wash)."
    elif quiz["wash"] == "rare":
        wash = "Wash at least once a week if you can, massaging your scalp with your fingertips, not your nails."
    else:
        wash = "Shampoo your scalp only, with your fingertips. Let the foam rinse through your lengths."
    if prob == "scalp":
        wash += " Massage your scalp for 1 minute while you wash."
    steps.append(("Wash", wash))
    hard_water = details.get("water") == "hard"
    prods.append(PRODUCTS["clarify" if (t == "locs" or p == "low" or hard_water) else "shampoo"][b])

    # Step 2: condition
    if t == "locs":
        cond = "Skip heavy conditioner. Spray a little water mixed with a few drops of light oil on your locs after washing."
    elif p == "low":
        cond = "Put conditioner on and cover your hair with a shower cap for 10 minutes. The warmth helps it soak in."
    elif p == "high":
        cond = ("Use a rich conditioner or mask every wash, leave it 5–10 minutes, and rinse with cool water "
                "to close the hair.")
    else:
        cond = "Condition every wash and detangle with your fingers while the conditioner is in."
    steps.append(("Condition", cond))
    if t != "locs":
        prods.append(PRODUCTS["conditioner"][b])

    # Step 3: style
    if t == "locs":
        style = "Oil your scalp lightly between washes and retwist only every 4–6 weeks to avoid thinning."
        prods.append(PRODUCTS["scalp"][b])
    elif t == "wavy" or prob == "flat":
        style = ("On soaking-wet hair, scrunch in a light gel or mousse. Avoid creams on the roots so your "
                 "waves don't fall flat.")
        prods.append(PRODUCTS["light"][b])
    elif t == "coily" or p == "high":
        style = "Use the LOC method on wet hair: Liquid (water), then Oil, then Cream. This seals moisture in for days."
        prods.append(PRODUCTS["butter"][b])
    else:
        style = "On wet hair, smooth in a curl cream, then a little gel to hold the curls. Scrunch and let it dry."
        prods.append(PRODUCTS["cream"][b])
    if prob == "scalp" and t != "locs":
        prods.append(PRODUCTS["scalp"][b])
    steps.append(("Style", style))

    # Extra tips from optional profile details
    tips = []
    if hard_water:
        tips.append("Hard water leaves minerals on your hair. Use a clarifying shampoo once a month, "
                    "or try a shower filter.")
    if details.get("climate") == "humid":
        tips.append("Humid weather causes frizz. Finish with a gel that holds well and avoid styling on damp, "
                    "humid days without it.")
    elif details.get("climate") == "dry_hot":
        tips.append("Dry heat pulls water out of your hair. Add a leave-in after every wash and cover your hair in strong sun.")
    elif details.get("climate") == "cold":
        tips.append("Cold wind and indoor heating dry your hair. Seal with a cream or oil and line your hat "
                    "with satin to avoid friction.")
    if details.get("pillow") == "cotton":
        tips.append("Cotton pillowcases pull moisture and cause frizz. Switch to a satin pillowcase or sleep in a bonnet.")
    if details.get("heat") == "often":
        tips.append("Heat every week weakens curls. Always use a heat protectant and keep the temperature under 180°C.")
    if details.get("chemical") in ("colour", "relaxed"):
        tips.append("Treated hair is more fragile. Do a deep conditioning mask once a week and be extra gentle when detangling.")
    if details.get("medication") == "yes":
        tips.append("Some medicines can change your hair (shedding, dryness). If you notice a change, "
                    "ask your doctor or pharmacist. Don't stop a treatment because of your hair.")

    return {"steps": steps, "products": prods[:3], "stop": STOP[prob], "extra_tips": tips}

# --------------------------------------------------------------------------
# Look and feel
# --------------------------------------------------------------------------


st.markdown("""
<style>
  h1, h2, h3 { letter-spacing: -0.01em; }
  .cc-brand { font-weight: 800; font-size: 1.9rem; color: #4a1f47; line-height: 1; }
  .cc-brand span { color: #d98e1f; }
  .cc-step { display: flex; gap: .75rem; margin: .5rem 0 .9rem; }
  .cc-num { flex: 0 0 2rem; height: 2rem; border-radius: 50%; background: #4a1f47; color: #fff;
            display: grid; place-items: center; font-weight: 800; }
  .cc-stop { border-left: 4px solid #d98e1f; padding: .6rem .9rem; background: rgba(217,142,31,.08);
             border-radius: 6px; }
  header[data-testid="stHeader"], [data-testid="stToolbar"], [data-testid="stDecoration"],
  [data-testid="stSidebarCollapsedControl"], #MainMenu, footer { display: none !important; }
  .block-container { padding-top: 1.5rem; max-width: 520px; }
  .cc-tag { display: inline-block; font-size: .72rem; font-weight: 700; letter-spacing: .06em;
            text-transform: uppercase; padding: .2rem .5rem; border: 1px dashed #888; border-radius: 6px;
            color: #888; }
</style>
""", unsafe_allow_html=True)


def brand():
    """Show the app name."""
    st.markdown('<div class="cc-brand">Curl<span>Check</span></div>', unsafe_allow_html=True)
    st.caption("Your hair, your routine")

# --------------------------------------------------------------------------
# Account screens
# --------------------------------------------------------------------------

USERNAME_RE = re.compile(r"^[a-z0-9_.]{3,20}$")


def invite_quiz():
    """Read quiz answers passed in a personal invite link (?name=&type=&poro=&problem=&wash=&budget=)."""
    p = st.query_params
    keys = {"type": HAIR_TYPES, "poro": POROSITY, "problem": PROBLEMS, "wash": WASH, "budget": BUDGET}
    if not all(p.get(k) in opts for k, opts in keys.items()):
        return None, p.get("name", "")
    quiz = {k: p.get(k) for k in keys}
    quiz["at"] = now_iso()
    quiz["source"] = "invite"
    return quiz, p.get("name", "")


def auth_screen():
    """Log in or sign up."""
    brand()
    invited_quiz, invited_name = invite_quiz()
    if invited_quiz:
        st.success(f"Hi {invited_name or 'there'}! Your quiz answers are saved. Create your account below to see your routine.")
    st.write("Find your hair routine in 1 minute: 3 steps, 2–3 products for your budget, and one habit to stop.")
    if invited_quiz:
        signup_tab, login_tab = st.tabs(["Create an account", "Log in"])
    else:
        login_tab, signup_tab = st.tabs(["Log in", "Create an account"])

    with login_tab:
        with st.form("login"):
            u = st.text_input("Username", autocomplete="username").strip().lower()
            pw = st.text_input("Password", type="password", autocomplete="current-password")
            if st.form_submit_button("Log in", type="primary", use_container_width=True):
                member = STORE.get(u) if u else None
                if member and check_password(pw, member.get("pw_hash")):
                    st.session_state.user = u
                    st.rerun()
                else:
                    st.error("Wrong username or password.")

    with signup_tab:
        with st.form("signup"):
            first = st.text_input("First name", value=invited_name, autocomplete="given-name").strip()
            u = st.text_input("Choose a username", help="3–20 characters: lowercase letters, numbers, _ or .", autocomplete="username").strip().lower()
            pw = st.text_input("Choose a password", type="password", help="At least 6 characters.",
                               autocomplete="new-password")
            agree = st.checkbox("I agree that Curl Check stores my hair answers to make my routine. "
                                "I can ask the team to delete my account at any time.")
            if st.form_submit_button("Create my account", type="primary", use_container_width=True):
                if not first:
                    st.error("Add your first name.")
                elif not USERNAME_RE.match(u):
                    st.error("Username must be 3–20 characters: lowercase letters, numbers, _ or .")
                elif len(pw) < 6:
                    st.error("Password must be at least 6 characters.")
                elif not agree:
                    st.error("Tick the box to agree before creating your account.")
                elif STORE.get(u):
                    st.error("That username is taken. Try another one.")
                else:
                    t = now_iso()
                    row = {"username": u, "first_name": first, "pw_hash": hash_password(pw),
                           "created_at": t, "updated_at": t}
                    if invited_quiz:
                        row["quiz"] = invited_quiz
                        row["routine"] = build_routine(invited_quiz)
                    STORE.create(row)
                    if invited_quiz:
                        st.session_state.flash = "Your routine is ready in the My routine tab."
                    st.session_state.user = u
                    st.rerun()

# --------------------------------------------------------------------------
# Member screens
# --------------------------------------------------------------------------


def radio(label, options, current=None, key=None, help=None):
    """A radio over a {value: label} dict that returns the value."""
    keys = list(options)
    idx = keys.index(current) if current in keys else 0
    return st.radio(label, keys, index=idx, format_func=options.get, key=key, help=help)


def quiz_tab(member):
    """The 1-minute quiz."""
    q = member.get("quiz") or {}
    st.subheader("Your 1-minute hair quiz")
    with st.form("quiz"):
        t = radio("What does your hair look like when it dries on its own, with no product?", HAIR_TYPES, q.get("type"))
        p = radio("The water test: drop a clean hair in a glass of water. After 2 minutes…", POROSITY, q.get("poro"),
                  help="This shows your porosity: how easily your hair takes in water.")
        prob = radio("What's your biggest problem right now?", PROBLEMS, q.get("problem"))
        w = radio("How often do you wash your hair?", WASH, q.get("wash"))
        b = radio("What's your budget per product?", BUDGET, q.get("budget"))
        if st.form_submit_button("See my routine", type="primary", use_container_width=True):
            quiz = {"type": t, "poro": p, "problem": prob, "wash": w, "budget": b, "at": now_iso()}
            routine = build_routine(quiz, member.get("details"))
            STORE.update(member["username"], {"quiz": quiz, "routine": routine, "updated_at": now_iso()})
            st.session_state.flash = "Your routine is ready below."
            st.rerun()


def routine_tab(member):
    """Show the routine and collect feedback."""
    r, q = member.get("routine"), member.get("quiz")
    if not r:
        st.info("Take the quiz first to get your routine.")
        return
    st.subheader(f"{member['first_name']}, here is your routine")
    st.caption(f"{HAIR_TYPES[q['type']].split(':')[0]} · {POROSITY[q['poro']].split('(')[-1].rstrip(')')}"
               f" · {BUDGET[q['budget']]}")

    st.markdown("#### 3 steps")
    for i, (title, text) in enumerate(r["steps"], 1):
        st.markdown(f'<div class="cc-step"><div class="cc-num">{i}</div><div><b>{title}</b><br>{text}</div></div>',
                    unsafe_allow_html=True)

    st.markdown("#### Your products")
    total = 0
    for name, price in r["products"]:
        c1, c2 = st.columns([5, 1])
        c1.write(name)
        c2.write(f"€{price}")
        total += price
    st.caption(f"About €{total} in total. Prices are approximate. Check the ingredients before you buy.")

    st.markdown("#### One thing to stop")
    st.markdown(f'<div class="cc-stop">{r["stop"]}</div>', unsafe_allow_html=True)

    if r.get("extra_tips"):
        st.markdown("#### Tips from your profile")
        for tip in r["extra_tips"]:
            st.write("• " + tip)
    else:
        st.caption("Fill in **My details** for extra tips about your water, climate, pillow and more.")

    st.divider()
    fb = member.get("feedback")
    if fb:
        st.markdown("#### Your feedback")
        c1, c2 = st.columns(2)
        c1.metric("Usefulness", f"{fb['score']}/10")
        c2.metric("Time saved", f"{max(0, fb['minutes'] - 1)} min",
                  help="Your usual search time minus the 1-minute quiz.")
        if fb.get("avoided") is not None:
            st.caption("Avoided buying a wrong product: " + ("yes" if fb["avoided"] else "not yet"))
        if st.button("Update my feedback"):
            STORE.update(member["username"], {"feedback": None})
            st.rerun()
    else:
        st.markdown("#### Help us improve")
        with st.form("feedback"):
            score = st.slider("How useful is this routine? (1 = not at all, 10 = very)", 1, 10, 7)
            minutes = st.number_input("How many minutes do you usually spend looking for hair tips or products "
                                      "before buying?", min_value=0, max_value=600, value=30, step=5)
            avoided = st.radio("Did this routine stop you from buying a product that wouldn't suit you?",
                               ["Not yet", "Yes"], horizontal=True)
            if st.form_submit_button("Save my feedback", type="primary"):
                STORE.update(member["username"], {"feedback": {
                    "score": int(score), "minutes": int(minutes), "avoided": avoided == "Yes", "at": now_iso()},
                    "updated_at": now_iso()})
                st.rerun()


def details_tab(member):
    """Optional profile details that add tips to the routine."""
    d = member.get("details") or {}
    st.subheader("My details")
    st.caption("Optional. Each answer adds a tip to your routine.")
    with st.form("details"):
        water = radio("What's the water like where you live?", WATER, d.get("water"))
        climate = radio("What's the weather like where you live?", CLIMATE, d.get("climate"))
        country = st.text_input("Country or city (optional)", value=d.get("country", ""))
        pillow = radio("What do you sleep on?", PILLOW, d.get("pillow"))
        heat = radio("How often do you use heat (straightener, blow-dryer without diffuser)?", HEAT, d.get("heat"))
        chemical = radio("Is your hair chemically treated?", CHEMICAL, d.get("chemical"))
        medication = radio("Are you taking a medicine that you think affects your hair?", MEDICATION, d.get("medication"),
                           help="We only store yes, no or prefer not to say. Never write the name of a medicine.")
        if st.form_submit_button("Save my details", type="primary"):
            details = {"water": water, "climate": climate, "country": country.strip()[:60], "pillow": pillow,
                       "heat": heat, "chemical": chemical, "medication": medication}
            fields = {"details": details, "updated_at": now_iso()}
            if member.get("quiz"):
                fields["routine"] = build_routine(member["quiz"], details)
            STORE.update(member["username"], fields)
            st.session_state.flash = "Saved. Your routine now includes tips from your details."
            st.rerun()


def preview_tab(title, text, cards):
    """A 'coming soon' screen for future features."""
    st.markdown('<span class="cc-tag">Coming soon · Preview</span>', unsafe_allow_html=True)
    st.subheader(title)
    st.write(text)
    for heading, body in cards:
        with st.container(border=True):
            st.caption("Example layout, not real content")
            st.markdown(f"**{heading}**")
            st.write(body)


# --------------------------------------------------------------------------
# Community features: products, community board, tracker
# --------------------------------------------------------------------------

CATEGORIES = ["Shampoo", "Conditioner or mask", "Leave-in", "Curl cream", "Gel or mousse", "Oil or butter", "Scalp care", "Other"]
TOPICS = ["Question", "Wash day", "Product tip", "Before / after", "Other"]
MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~])")


def safe(text):
    """Escape user text so it shows as plain text in Markdown."""
    return MD_SPECIAL.sub(r"\\\1", str(text))


def hair_label(member_or_quiz):
    """Short hair-type label for a member or quiz dict."""
    q = member_or_quiz.get("quiz") if "quiz" in member_or_quiz else member_or_quiz
    if not isinstance(q, dict):
        return "Hair type not set"
    return HAIR_TYPES[q["type"]].split(":")[0].split(" (")[0]


def ago(iso):
    """'3 h ago' style time label."""
    try:
        t = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    mins = int((dt.datetime.now(dt.timezone.utc) - t).total_seconds() // 60)
    if mins < 60:
        return f"{max(mins, 1)} min ago"
    if mins < 1440:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} d ago"


def home_tab(member):
    """Startup home: what Curl Check does, live community numbers, next steps."""
    st.subheader(f"Hi {member['first_name']} 👋")
    st.write("Curl Check helps people with wavy, curly, coily hair and locs stop wasting money on products "
             "that don't suit them. Get a routine made for your hair, see what works for people like you, "
             "and track your results.")
    members = STORE.all()
    reviews = STORE.list_entries("review")
    posts = STORE.list_entries("post")
    c1, c2, c3 = st.columns(3)
    c1.metric("Members", len(members))
    c2.metric("Product reviews", len(reviews))
    c3.metric("Community posts", len(posts))

    st.markdown("#### What you can do")
    steps = [
        ("My routine", "Take the 1-minute quiz and get 3 steps, products for your budget and one habit to stop.",
         bool(member.get("routine"))),
        ("Products", "Rate the products you use and see ratings from people with your hair type.",
         any(r["username"] == member["username"] for r in reviews)),
        ("Community", "Ask a question or share what worked, with no sponsored posts.",
         any(p["username"] == member["username"] for p in posts)),
        ("Tracker", "Log each wash day to see which products give you the best results.", False),
    ]
    for title, text, done in steps:
        with st.container(border=True):
            st.markdown(f"{'✅' if done else '⬜'} **{title}**")
            st.caption(text)


def routine_page(member):
    """Quiz if needed, otherwise the routine with an option to retake."""
    if member.get("routine"):
        routine_tab(member)
        with st.expander("Retake the quiz"):
            quiz_tab(member)
    else:
        quiz_tab(member)


def products_tab(member):
    """Members rate products; ratings can be filtered to people with the same hair type."""
    st.subheader("Products rated by people like you")
    st.caption("Real reviews from Curl Check members. No sponsored ratings.")

    with st.expander("➕ Rate a product you've used", expanded=False):
        with st.form("review", clear_on_submit=True):
            name = st.text_input("Product name", placeholder="e.g. Cantu Shea Butter Leave-In")
            cat = st.selectbox("Type", CATEGORIES)
            stars = st.slider("Your rating", 1, 5, 4)
            again = st.radio("Would you buy it again?", ["Yes", "No"], horizontal=True)
            note = st.text_area("What happened? (optional)", max_chars=300)
            if st.form_submit_button("Post my review", type="primary"):
                if len(name.strip()) < 3:
                    st.error("Add the product name.")
                else:
                    q = member.get("quiz") or {}
                    STORE.add_entry("review", member["username"], {
                        "product": name.strip()[:80], "category": cat, "stars": int(stars),
                        "again": again == "Yes", "note": note.strip()[:300], "first_name": member["first_name"],
                        "hair": q.get("type"), "poro": q.get("poro")})
                    st.session_state.flash = "Thanks! Your review is posted."
                    st.rerun()

    reviews = STORE.list_entries("review")
    if not reviews:
        st.info("No reviews yet. Be the first to rate a product you use.")
        return
    my_type = (member.get("quiz") or {}).get("type")
    only_mine = st.toggle(f"Only people with my hair type ({hair_label(member)})", value=bool(my_type),
                          disabled=not my_type)
    if only_mine and my_type:
        reviews = [r for r in reviews if r["data"].get("hair") == my_type]
    if not reviews:
        st.info("No reviews from people with your hair type yet. Turn the filter off to see all.")
        return

    # Group by product name
    groups = {}
    for r in reviews:
        groups.setdefault(r["data"]["product"].strip().lower(), []).append(r)
    ranked = sorted(groups.values(), key=lambda g: (-sum(x["data"]["stars"] for x in g) / len(g), -len(g)))
    for g in ranked:
        d0 = g[0]["data"]
        avg = sum(x["data"]["stars"] for x in g) / len(g)
        again = sum(1 for x in g if x["data"].get("again"))
        with st.container(border=True):
            st.markdown(f"**{safe(d0['product'])}** · {d0['category']}")
            st.markdown(f"{'★' * round(avg)}{'☆' * (5 - round(avg))} **{avg:.1f}** from {len(g)} "
                        f"review{'s' if len(g) > 1 else ''} · {again} would buy again")
            for x in g[:3]:
                d = x["data"]
                who = f"{safe(d.get('first_name', 'Member'))} · {HAIR_TYPES.get(d.get('hair'), 'Hair type not set').split(':')[0]}"
                st.caption(f"{who} · {'★' * d['stars']} · {ago(x.get('created_at'))}")
                if d.get("note"):
                    st.markdown(f"> {safe(d['note'])}")


def community_tab(member):
    """A simple board: posts with replies."""
    st.subheader("Community")
    st.caption("Ask questions and share what really worked. Be kind: no ads, no body-shaming.")

    with st.expander("✏️ Write a post", expanded=False):
        with st.form("post", clear_on_submit=True):
            topic = st.selectbox("Topic", TOPICS)
            text = st.text_area("Your post", max_chars=600, placeholder="e.g. What do you use to stop frizz in humid weather?")
            if st.form_submit_button("Post", type="primary"):
                if len(text.strip()) < 5:
                    st.error("Write a bit more before posting.")
                else:
                    STORE.add_entry("post", member["username"], {
                        "topic": topic, "text": text.strip()[:600], "first_name": member["first_name"],
                        "hair": (member.get("quiz") or {}).get("type")})
                    st.session_state.flash = "Posted!"
                    st.rerun()

    posts = STORE.list_entries("post")
    if not posts:
        st.info("No posts yet. Start the first conversation.")
        return
    replies = STORE.list_entries("reply")
    by_post = {}
    for r in replies:
        by_post.setdefault(str(r["data"].get("post_id")), []).append(r)

    topic_filter = st.selectbox("Show", ["All topics"] + TOPICS)
    for p in posts:
        d = p["data"]
        if topic_filter != "All topics" and d.get("topic") != topic_filter:
            continue
        pid = str(p["id"])
        with st.container(border=True):
            hair = HAIR_TYPES.get(d.get("hair"), "Hair type not set").split(":")[0]
            st.caption(f"**{safe(d.get('first_name', 'Member'))}** · {hair} · {d.get('topic')} · {ago(p.get('created_at'))}")
            st.markdown(safe(d["text"]))
            reps = sorted(by_post.get(pid, []), key=lambda r: str(r.get("created_at")))
            for r in reps:
                rd = r["data"]
                st.markdown(f"↳ **{safe(rd.get('first_name', 'Member'))}:** {safe(rd['text'])}")
            with st.form(f"reply-{pid}", clear_on_submit=True):
                rep = st.text_input("Reply", key=f"rep-{pid}", label_visibility="collapsed", placeholder="Write a reply…")
                if st.form_submit_button("Reply"):
                    if rep.strip():
                        STORE.add_entry("reply", member["username"], {
                            "post_id": pid, "text": rep.strip()[:300], "first_name": member["first_name"]})
                        st.rerun()


def tracker_tab(member):
    """Log wash days and see results over time."""
    st.subheader("Wash-day tracker")
    st.caption("Log each wash day. After a few weeks you'll see which routine and products work best.")
    with st.form("log", clear_on_submit=True):
        day = st.date_input("Date", value=dt.date.today(), max_value=dt.date.today())
        used = st.text_input("Products used", placeholder="e.g. Cantu leave-in + gel")
        result = st.slider("How did your hair turn out? (1 = bad, 5 = great)", 1, 5, 3)
        note = st.text_input("Note (optional)", placeholder="e.g. less frizz, more definition")
        if st.form_submit_button("Save wash day", type="primary"):
            STORE.add_entry("log", member["username"], {
                "day": day.isoformat(), "used": used.strip()[:120], "result": int(result), "note": note.strip()[:200]})
            st.session_state.flash = "Wash day saved."
            st.rerun()

    logs = STORE.list_entries("log", username=member["username"])
    if not logs:
        st.info("No wash days logged yet.")
        return
    df = pd.DataFrame([{**l["data"]} for l in logs])
    df["day"] = pd.to_datetime(df["day"])
    df = df.sort_values("day")
    c1, c2 = st.columns(2)
    c1.metric("Wash days logged", len(df))
    c2.metric("Average result", f"{df['result'].mean():.1f}/5")
    if len(df) > 1:
        st.line_chart(df.set_index("day")["result"], color="#4a1f47")
    best = df.sort_values("result", ascending=False).iloc[0]
    if best["used"]:
        st.success(f"Your best result so far: **{safe(best['used'])}** ({best['result']}/5).")
    st.dataframe(df.sort_values("day", ascending=False).rename(columns={
        "day": "Date", "used": "Products", "result": "Result", "note": "Note"}),
        hide_index=True, use_container_width=True)


def change_password(member):
    """Let a member change their password."""
    with st.expander("Change my password"):
        with st.form("change_pw", clear_on_submit=True):
            old = st.text_input("Current password", type="password", autocomplete="current-password")
            new = st.text_input("New password", type="password", autocomplete="new-password")
            if st.form_submit_button("Change password"):
                if not check_password(old, member.get("pw_hash")):
                    st.error("Current password is wrong.")
                elif len(new) < 6:
                    st.error("New password must be at least 6 characters.")
                else:
                    STORE.update(member["username"], {"pw_hash": hash_password(new), "updated_at": now_iso()})
                    st.success("Password changed.")


def profile_tab(member):
    """Hair profile summary and optional details."""
    q = member.get("quiz")
    if q:
        st.subheader("My hair profile")
        st.write(f"**{HAIR_TYPES[q['type']].split(':')[0]}** · {POROSITY[q['poro']].split('(')[-1].rstrip(')')} · "
                 f"main problem: {PROBLEMS[q['problem']].lower()} · budget {BUDGET[q['budget']]}")
    details_tab(member)
    change_password(member)

# --------------------------------------------------------------------------
# Team dashboard (Traction and Impact)
# --------------------------------------------------------------------------


def add_members():
    """Team tool: create accounts for people who took the Google Forms quiz, with temporary passwords."""
    with st.expander("➕ Create accounts for quiz respondents"):
        st.caption("One person per line: first name, hair type, porosity, problem, wash, budget. "
                   "Hair type: wavy/curly/coily/locs/unsure. Porosity: low/medium/high/unsure. "
                   "Problem: frizz/dry/break/define/flat/scalp/lost. Wash: daily/few/weekly/rare. Budget: low/mid/high.")
        text = st.text_area("Paste lines", height=180, placeholder="Ines, wavy, unsure, define, few, mid")
        if st.button("Create accounts", type="primary"):
            made, problems = [], []
            for line in text.strip().splitlines():
                parts = [x.strip().lower() for x in line.split(",")]
                if len(parts) != 6:
                    problems.append(f"{line} (needs 6 values)")
                    continue
                name, t, p, prob, w, b = parts
                if t not in HAIR_TYPES or p not in POROSITY or prob not in PROBLEMS or w not in WASH or b not in BUDGET:
                    problems.append(f"{line} (a value isn't in the list)")
                    continue
                base = re.sub(r"[^a-z0-9]", "", name)[:16] or "member"
                username, n = base, 2
                while STORE.get(username):
                    username, n = f"{base}{n}", n + 1
                pw = temp_password()
                quiz = {"type": t, "poro": p, "problem": prob, "wash": w, "budget": b, "at": now_iso(), "source": "google_form"}
                now = now_iso()
                STORE.create({"username": username, "first_name": name.title(), "pw_hash": "temp:" + hash_password(pw),
                              "created_at": now, "updated_at": now, "quiz": quiz, "routine": build_routine(quiz)})
                made.append({"First name": name.title(), "Username": username, "Temporary password": pw})
            if made:
                st.success(f"Created {len(made)} account(s). Copy this table now: the passwords are not shown again.")
                st.table(pd.DataFrame(made))
            for pr in problems:
                st.error("Skipped: " + pr)


def dashboard():
    """Numbers for the project report: members, routines and feedback."""
    st.subheader("Team dashboard")
    add_members()
    rows = STORE.all()
    if not rows:
        st.info("No members yet.")
        return
    df = pd.DataFrame(rows)
    df["joined"] = pd.to_datetime(df["created_at"]).dt.date
    has_routine = df["routine"].notna()
    fb = df["feedback"].dropna().apply(pd.Series) if df["feedback"].notna().any() else pd.DataFrame()

    c1, c2, c3 = st.columns(3)
    c1.metric("Accounts", len(df))
    c2.metric("Routines given", int(has_routine.sum()))
    c3.metric("Feedback received", len(fb))
    if len(fb):
        c4, c5, c6 = st.columns(3)
        c4.metric("Average usefulness", f"{fb['score'].mean():.1f}/10")
        c5.metric("Time saved (total)", f"{int((fb['minutes'] - 1).clip(lower=0).sum())} min")
        c6.metric("Avoided a wrong buy", int(fb["avoided"].sum()) if "avoided" in fb else 0)

    c7, c8, c9 = st.columns(3)
    c7.metric("Product reviews", len(STORE.list_entries("review")))
    c8.metric("Community posts", len(STORE.list_entries("post")))
    c9.metric("Wash days logged", len(STORE.list_entries("log")))

    st.markdown("**Traction: new accounts per day**")
    per_day = df.groupby("joined").size().rename("Accounts").to_frame()
    per_day["Total"] = per_day["Accounts"].cumsum()
    st.bar_chart(per_day["Accounts"], color="#4a1f47")
    st.markdown("**Total accounts over time**")
    st.line_chart(per_day["Total"], color="#d98e1f")

    if len(fb):
        st.markdown("**Impact: usefulness scores (1–10)**")
        st.bar_chart(fb["score"].value_counts().sort_index().rename("People"), color="#d98e1f")

    st.markdown("**Members**")
    table = pd.DataFrame({
        "First name": df["first_name"],
        "Joined": df["joined"],
        "Hair type": df["quiz"].apply(lambda q: HAIR_TYPES[q["type"]].split(":")[0] if isinstance(q, dict) else "–"),
        "Problem": df["quiz"].apply(lambda q: PROBLEMS[q["problem"]] if isinstance(q, dict) else "–"),
        "Usefulness": df["feedback"].apply(lambda f: f["score"] if isinstance(f, dict) else None),
        "Logged in": df["username"].apply(lambda u: "yes" if not is_temp(STORE.get(u) or {}) else "not yet"),
    })
    st.dataframe(table, hide_index=True, use_container_width=True)

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main():
    """Route between the login screen, the member app and the team dashboard."""
    if "team" in st.query_params:
        admin_pw = _secret("ADMIN_PASSWORD")
        st.markdown("**Team dashboard**")
        if not admin_pw:
            st.error("Set ADMIN_PASSWORD in the app secrets.")
            return
        entered = st.text_input("Team password", type="password")
        if entered and hmac.compare_digest(entered, admin_pw):
            dashboard()
        elif entered:
            st.error("Wrong password.")
        return

    user = st.session_state.get("user")
    member = STORE.get(user) if user else None
    if not member:
        st.session_state.pop("user", None)
        auth_screen()
        return

    if is_temp(member):
        brand()
        st.subheader(f"Welcome {member['first_name']}!")
        st.write("Your routine is ready. First, choose your own password.")
        with st.form("new_pw"):
            pw1 = st.text_input("New password", type="password", autocomplete="new-password", help="At least 6 characters.")
            pw2 = st.text_input("Type it again", type="password", autocomplete="new-password")
            agree = st.checkbox("I agree that Curl Check stores my hair answers to make my routine. "
                                "I can ask the team to delete my account at any time.")
            if st.form_submit_button("Save and see my routine", type="primary", use_container_width=True):
                if len(pw1) < 6:
                    st.error("Password must be at least 6 characters.")
                elif pw1 != pw2:
                    st.error("The two passwords are different.")
                elif not agree:
                    st.error("Tick the box to continue.")
                else:
                    STORE.update(member["username"], {"pw_hash": hash_password(pw1), "updated_at": now_iso()})
                    st.session_state.flash = "Password saved. Your routine is in the My routine tab."
                    st.rerun()
        if st.button("Log out"):
            st.session_state.clear()
            st.rerun()
        return

    top1, top2 = st.columns([4, 1])
    with top1:
        brand()
    with top2:
        if st.button("Log out"):
            st.session_state.clear()
            st.rerun()
    if st.session_state.get("flash"):
        st.success(st.session_state.pop("flash"))

    tabs = st.tabs(["Home", "My routine", "Products", "Community", "Tracker", "Profile"])
    with tabs[0]:
        home_tab(member)
    with tabs[1]:
        routine_page(member)
    with tabs[2]:
        products_tab(member)
    with tabs[3]:
        community_tab(member)
    with tabs[4]:
        tracker_tab(member)
    with tabs[5]:
        profile_tab(member)

main()
st.markdown('<p style="text-align:center;color:#6e5a6c;font-size:.8rem;margin-top:2.5rem">Created by Sarah</p>', unsafe_allow_html=True)
