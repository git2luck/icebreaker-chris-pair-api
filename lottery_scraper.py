"""
LotteryUSA Scraper

Fetches Pick 3 / Pick 4 results from lotteryusa.com.
Numbers are ALWAYS strings to preserve leading zeros.
"""

import re
import time
import subprocess
import tempfile
from datetime import datetime
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass

# ============================================================
# CONFIGURATION
# ============================================================

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

STATES = [
    ("Arizona", "arizona", "AZ"), ("Arkansas", "arkansas", "AR"),
    ("California", "california", "CA"), ("Colorado", "colorado", "CO"),
    ("Connecticut", "connecticut", "CT"), ("Delaware", "delaware", "DE"),
    ("Florida", "florida", "FL"), ("Georgia", "georgia", "GA"),
    ("Idaho", "idaho", "ID"), ("Illinois", "illinois", "IL"),
    ("Indiana", "indiana", "IN"), ("Iowa", "iowa", "IA"),
    ("Kansas", "kansas", "KS"), ("Kentucky", "kentucky", "KY"),
    ("Louisiana", "louisiana", "LA"), ("Maine", "maine", "ME"),
    ("Maryland", "maryland", "MD"), ("Massachusetts", "massachusetts", "MA"),
    ("Michigan", "michigan", "MI"), ("Minnesota", "minnesota", "MN"),
    ("Mississippi", "mississippi", "MS"), ("Missouri", "missouri", "MO"),
    ("Montana", "montana", "MT"), ("Nebraska", "nebraska", "NE"),
    ("New Hampshire", "new-hampshire", "NH"), ("New Jersey", "new-jersey", "NJ"),
    ("New Mexico", "new-mexico", "NM"), ("New York", "new-york", "NY"),
    ("North Carolina", "north-carolina", "NC"), ("North Dakota", "north-dakota", "ND"),
    ("Ohio", "ohio", "OH"), ("Oklahoma", "oklahoma", "OK"),
    ("Oregon", "oregon", "OR"), ("Pennsylvania", "pennsylvania", "PA"),
    ("Puerto Rico", "puerto-rico", "PR"), ("Rhode Island", "rhode-island", "RI"),
    ("South Carolina", "south-carolina", "SC"), ("South Dakota", "south-dakota", "SD"),
    ("Tennessee", "tennessee", "TN"), ("Texas", "texas", "TX"),
    ("Vermont", "vermont", "VT"), ("Virginia", "virginia", "VA"),
    ("Washington", "washington", "WA"), ("Washington, D.C.", "district-of-columbia", "DC"),
    ("West Virginia", "west-virginia", "WV"), ("Wisconsin", "wisconsin", "WI"),
    ("Wyoming", "wyoming", "WY"),
]

# Draw normalization
DRAW_MAP = {
    "New Mexico": {"midday": "Day"},
    "North Carolina": {"midday": "Daytime"},
    "Oklahoma": {"evening": "Daily"},
    "Louisiana": {"evening": "Daily"},
    "Washington, D.C.": {"mid-day": "1:50pm", "evening": "7:50pm", "night": "11:30pm"},
    "Puerto Rico": {"día": "Dia", "dia": "Dia", "noche": "Noche"},
    "Pennsylvania": {"midday": "Day"},
}

# Game tokens
GAME_TOKENS = [
    "pick 3 plus", "pick 4 plus",
    "pick 3", "pick 4", "pick-3", "pick-4", "pick3", "pick4",
    "cash 3", "cash 4", "cash3", "cash4",
    "daily 3", "daily 4", "daily3", "daily4",
    "play 3", "play 4", "play3", "play4",
    "pega 3", "pega 4", "pega3", "pega4",
    "dc 3", "dc 4", "dc3", "dc4", "dc-3", "dc-4",
    "the numbers game", "the numbers", "daily game",
]

# Titles to exclude
EXCLUDE_TITLES = (
    "myday", "cash ball", "cash 5", "cash5", "win for life",
    "texas two step", "two step", "match 4", "match4",
    "cash4life", "lucky for life", "fantasy 5", "take 5", "fireball"
)

# Regex patterns
CARD_RE = re.compile(
    r'c-game-result-card__title">(.*?)</span>\s*<time>(.*?)</time>'
    r'.*?<ul[^>]*class="c-result[^"]*"[^>]*>(.*?)</ul>', re.S
)
BALL_RE = re.compile(r"<li([^>]*)>(.*?)</li>", re.S)


# ============================================================
# DATA MODELS
# ============================================================

@dataclass
class LotteryResult:
    id: str
    state: str
    state_code: str
    game: str  # "pick3" or "pick4"
    draw: str
    number: str  # STRING - preserves leading zeros
    draw_date: str  # ISO date
    fetched_at: str
    source: str
    fingerprint: str


def generate_fingerprint(state_code: str, game: str, draw: str, draw_date: str, number: str) -> str:
    """Generate deterministic fingerprint for deduplication."""
    return f"{state_code}|{game}|{draw}|{draw_date}|{number}"


# ============================================================
# SCRAPER
# ============================================================

def get_state_info(identifier: str) -> Optional[Tuple[str, str, str]]:
    """Get (name, slug, code) for a state."""
    identifier_lower = identifier.lower().strip()
    for name, slug, code in STATES:
        if slug == identifier_lower or code.lower() == identifier_lower or name.lower() == identifier_lower:
            return (name, slug, code)
    return None


def normalize_draw(state: str, raw_draw: str) -> str:
    """Normalize draw label for a state."""
    trimmed = raw_draw.strip()
    lowered = trimmed.lower()
    
    # Check state-specific mapping
    if state in DRAW_MAP and lowered in DRAW_MAP[state]:
        return DRAW_MAP[state][lowered]
    
    # Default: capitalize first letter
    return trimmed.title() if trimmed else "Daily"


def determine_game(title: str, ball_count: int) -> Optional[str]:
    """Determine game type from title and ball count."""
    t = title.lower()
    
    # Special case
    if "the numbers game" in t or "numbers game" in t:
        return "pick4"
    
    # Pick 3 indicators
    pick3_tokens = [
        "pick 3", "pick-3", "pick3", "cash 3", "cash3",
        "daily 3", "daily3", "play 3", "play3", "pega 3", "pega3",
        "dc 3", "dc3", "dc-3", "daily game"
    ]
    for tok in pick3_tokens:
        if tok in t:
            return "pick3"
    
    # Pick 4 indicators
    pick4_tokens = [
        "pick 4", "pick-4", "pick4", "cash 4", "cash4",
        "daily 4", "daily4", "play 4", "play4", "pega 4", "pega4",
        "dc 4", "dc4", "dc-4"
    ]
    for tok in pick4_tokens:
        if tok in t:
            return "pick4"
    
    # NY "Numbers" is Pick 3
    if "numbers" in t and "game" not in t:
        return "pick3"
    
    # Fall back to ball count
    if ball_count == 3:
        return "pick3"
    if ball_count >= 4:
        return "pick4"
    
    return None


def extract_draw(state: str, title: str) -> str:
    """Extract draw time from title."""
    draw = title
    for tok in GAME_TOKENS:
        draw = re.sub(re.escape(tok), "", draw, flags=re.I)
    draw = re.sub(r"\s+", " ", draw).strip(" -–—")
    return normalize_draw(state, draw) if draw else "Daily"


def parse_date(date_str: str) -> Optional[str]:
    """Parse date string to ISO format."""
    months = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "may": "05", "jun": "06", "jul": "07", "aug": "08",
        "sep": "09", "oct": "10", "nov": "11", "dec": "12"
    }
    
    match = re.search(r"(\w+),\s+(\w+)\s+(\d+),\s+(\d+)", date_str)
    if not match:
        return None
    
    month_abbr = match.group(2).lower()[:3]
    month = months.get(month_abbr)
    if not month:
        return None
    
    day = match.group(3).zfill(2)
    year = match.group(4)
    return f"{year}-{month}-{day}"


def parse_balls(html: str) -> List[str]:
    """Parse ball elements, excluding Fireball."""
    balls = []
    for match in BALL_RE.finditer(html):
        cls = match.group(1).lower()
        text = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        
        # Skip Fireball
        if "fire" in cls:
            continue
        
        if text.isdigit():
            balls.append(text)  # Keep as string!
    return balls


def curl_fetch(url: str) -> Tuple[int, Optional[str]]:
    """Fetch URL using curl."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False) as f:
        tmp_path = f.name
    
    cmd = [
        "curl", "-s", "-m", "20", "-L",
        "-A", USER_AGENT,
        "-H", "Accept-Language: en-US,en;q=0.9",
        "-o", tmp_path, "-w", "%{http_code}", url
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        status_code = int(result.stdout.strip()[-3:] if result.stdout else "0")
        
        if status_code == 200:
            with open(tmp_path, 'r', encoding='utf-8', errors='replace') as f:
                return 200, f.read()
        return status_code, None
    except Exception:
        return -1, None
    finally:
        try:
            import os
            os.unlink(tmp_path)
        except:
            pass


def parse_hub_page(state_name: str, state_code: str, html: str) -> List[LotteryResult]:
    """Parse state hub page for lottery results."""
    results = []
    now = datetime.now().isoformat()
    
    for match in CARD_RE.finditer(html):
        title = re.sub(r"\s+", " ", match.group(1)).strip()
        date_str = re.sub(r"\s+", " ", match.group(2)).strip()
        balls_html = match.group(3)
        
        # Skip excluded titles
        if any(x in title.lower() for x in EXCLUDE_TITLES):
            continue
        
        # Parse balls
        balls = parse_balls(balls_html)
        if not balls:
            continue
        
        # Determine game
        game = determine_game(title, len(balls))
        if not game:
            continue
        
        # Ensure correct digit count
        expected = 3 if game == "pick3" else 4
        if len(balls) < expected:
            continue
        
        # Build number STRING (preserves leading zeros!)
        number = "".join(balls[:expected])
        
        # Parse date
        draw_date = parse_date(date_str)
        if not draw_date:
            continue
        
        # Extract draw
        draw = extract_draw(state_name, title)
        
        # Generate fingerprint
        fingerprint = generate_fingerprint(state_code, game, draw, draw_date, number)
        
        results.append(LotteryResult(
            id=f"{state_code}_{game}_{draw}_{draw_date}_{number}",
            state=state_name,
            state_code=state_code,
            game=game,
            draw=draw,
            number=number,  # STRING!
            draw_date=draw_date,
            fetched_at=now,
            source="lotteryusa",
            fingerprint=fingerprint
        ))
    
    return results


def fetch_state(state_identifier: str, rate_limit_ms: int = 5000) -> Tuple[List[LotteryResult], List[str]]:
    """Fetch results for a single state."""
    state_info = get_state_info(state_identifier)
    if not state_info:
        return [], [f"Unknown state: {state_identifier}"]
    
    state_name, slug, state_code = state_info
    url = f"https://www.lotteryusa.com/{slug}/"
    
    status, html = curl_fetch(url)
    
    if status == 429:
        # Rate limited - wait and retry
        time.sleep(60)
        status, html = curl_fetch(url)
    
    if status != 200 or not html:
        return [], [f"{state_name}: HTTP {status}"]
    
    results = parse_hub_page(state_name, state_code, html)
    return results, []


def fetch_all_states(states: List[str] = None, rate_limit_ms: int = 5000) -> Tuple[List[LotteryResult], List[str]]:
    """Fetch results for multiple states."""
    if states is None:
        states = [s[1] for s in STATES]  # All slugs
    
    all_results = []
    all_errors = []
    
    for i, state in enumerate(states):
        results, errors = fetch_state(state)
        all_results.extend(results)
        all_errors.extend(errors)
        
        # Rate limiting
        if i < len(states) - 1:
            time.sleep(rate_limit_ms / 1000)
    
    return all_results, all_errors


# ============================================================
# TEST
# ============================================================

if __name__ == "__main__":
    print("Fetching Florida results...")
    results, errors = fetch_state("florida")
    
    print(f"Found {len(results)} results")
    for r in results[:5]:
        print(f"  {r.state_code} {r.game} {r.draw}: {r.number} ({r.draw_date})")
    
    if errors:
        print(f"Errors: {errors}")
