"""Pair equivalent binary markets across Kalshi and Polymarket.

Two passes:

1. Games (structural). Kalshi game tickers look like KXNBAGAME-26NOV07BKNATL-ATL
   and Polymarket game slugs like nba-bkn-atl-2026-11-07. We pair games by date
   plus team abbreviations and/or team names, then pair the individual
   moneyline, spread and total markets inside each game.

2. Everything else (text). An IDF-weighted token overlap between the questions,
   plus hard checks that the outcomes (candidate / team names) and any
   numbers (strikes, thresholds) agree, and a soft check on close dates. Only
   mutual-best pairs are kept.

A match is (kalshi market, polymarket market, inverted). `inverted` means
Kalshi YES is equivalent to Polymarket NO (e.g. Kalshi "Atlanta wins" vs
Polymarket "Nets vs. Hawks", whose YES is the Nets).

Matching is heuristic. Always read both markets' resolution rules before
trading: same headline != same settlement.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class Match:
    kalshi_id: str
    poly_id: str
    inverted: bool
    confidence: float  # 0..1
    method: str        # "game" | "spread" | "total" | "text"
    reason: str


TIERS = [("high", 0.80), ("medium", 0.60), ("low", 0.40)]


def tier(conf: float) -> str:
    for name, cut in TIERS:
        if conf >= cut:
            return name
    return "none"


# ---------------------------------------------------------------- text utils

STOP = set("""a an the of in on at to by for and or be is are will would does do did
with from as it its this that who what which than then into after before during
next any following""".split())

SYNONYMS = {
    "hike": "increase", "raise": "increase", "raises": "increase", "increases": "increase",
    "cut": "decrease", "cuts": "decrease", "lower": "decrease", "decreases": "decrease",
    "democrats": "democrat", "democratic": "democrat", "democratics": "democrat", "dem": "democrat",
    "republicans": "republican", "gop": "republican", "rep": "republican",
    "presidential": "president", "presidency": "president",
    "nomination": "nominee", "nominated": "nominee", "nominate": "nominee",
    "wins": "win", "winner": "win", "won": "win",
    "st": "state", "saint": "state",
    "january": "jan", "february": "feb", "march": "mar", "april": "apr", "june": "jun",
    "july": "jul", "august": "aug", "september": "sep", "sept": "sep", "october": "oct",
    "november": "nov", "december": "dec",
    "u.s": "us", "usa": "us",
}
# If exactly one side contains a word from a group, the questions ask different things.
CONFLICT_GROUPS = [
    {"last", "bottom", "relegated", "relegation", "worst", "lose", "loser"},
    {"round"},                       # "first round" vs. the overall winner
    {"nominee", "primary"},          # nomination vs. general election
    {"runner", "second", "2nd", "podium", "top"},
    {"qualify", "advance", "reach"},
    {"resign", "out", "leave", "removed", "fired", "impeached"},
    {"minister"}, {"governor"}, {"senate", "senator"}, {"house"}, {"mayor"},
    {"group"}, {"stage"}, {"semifinal", "quarterfinal"},
    {"not", "no", "never", "without"},
    {"above", "more", "over", "greater", "least", "higher", "exceed"},
    {"below", "less", "under", "fewer", "lower", "decrease"},
    {"between", "range", "exactly"},
    {"reach", "hit", "dip", "touch", "ath"},  # path-dependent vs. a reading at a point in time
    {"arrested"}, {"indicted"}, {"convicted"}, {"sentenced"},
    {"administration", "cabinet"},  # leaving a specific role != leaving the administration
    {"announce", "announced", "announcement", "confirm", "confirmed"},  # announcing X != X happening
]
# Club/party noise that doesn't identify a team or person.
NAME_NOISE = {"fc", "cf", "sc", "ac", "afc", "club", "de", "the", "cd", "sv", "ca", "ec", "fk", "bk",
              "team", "esports", "gaming", "university", "univ", "d", "r", "i"}

_num_re = re.compile(r"\d+(?:\.\d+)?")


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def tokens(s: str) -> list[str]:
    s = _ascii(s or "").lower().replace("&", " and ").replace("'", "")
    # Comparison symbols carry meaning (">25bps" != "25 bps"); spell them out.
    s = s.replace(">=", " at least ").replace("<=", " at most ").replace(">", " above ").replace("<", " below ")
    s = re.sub(r"(\d)\s*\+", r"\1 or more ", s)        # "50+ bps" -> "50 or more bps"
    s = re.sub(r"(?<=\d),(?=\d{3})", "", s)              # 92,000 -> 92000
    s = re.sub(r"(\d+(?:\.\d+)?)k\b", lambda m: str(int(float(m.group(1)) * 1000)), s)
    s = re.sub(r"(\d)([a-z])", r"\1 \2", s)              # 25bps -> 25 bps
    s = re.sub(r"([a-z])(\d)", r"\1 \2", s)
    out = []
    for t in re.findall(r"[a-z]+|\d+(?:\.\d+)?", s):
        t = SYNONYMS.get(t, t)
        if t in STOP:
            continue
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss") and not t[0].isdigit():
            t = t[:-1]
        out.append(t)
    return out


def numbers(toks: list[str]) -> set[str]:
    """Numbers that aren't years (years are compared separately)."""
    out = set()
    for t in toks:
        if _num_re.fullmatch(t):
            v = float(t)
            if not (1990 <= v <= 2100 and v == int(v)):
                out.add(f"{v:g}")
    return out


def years(toks: list[str]) -> set[str]:
    return {t for t in toks if re.fullmatch(r"(199\d|20\d\d)", t)}


def name_tokens(s: str) -> set[str]:
    s = re.sub(r"\((?:[DRI]|[A-Z]{2})\)", " ", s or "")   # "(D)", "(FL)" keep? -> handled below
    return {t for t in tokens(s) if t not in NAME_NOISE}


def name_sim(a: str, b: str) -> float:
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def same_name(a: str, b: str) -> bool:
    """Outcome labels refer to the same entity: one contains the other ("Salt Lake" /
    "Real Salt Lake") or they nearly coincide. Club suffixes are kept here because they
    can be the only difference ("Los Angeles G" vs "Los Angeles FC"). Empty = no constraint."""
    strip = lambda s: {t for t in tokens(re.sub(r"\((?:D|R|I)\)|\bSen\.|\bRep\.", " ", s or "")) if t != "the"}
    ta, tb = strip(a), strip(b)
    if not ta or not tb:
        return True
    return ta <= tb or tb <= ta or len(ta & tb) / len(ta | tb) >= 0.67


def _dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T")).astimezone(timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------- pass 1: games

_K_GAME = re.compile(r"^KX([A-Z0-9]+?)(GAME|MATCH|FIGHT|SPREAD|TOTAL)-(\d{2})([A-Z]{3})(\d{2})(?:\d{4})?([A-Z0-9]+)-([A-Z0-9.]+)$")
_P_GAME = re.compile(r"^([a-z0-9]+)-([a-z0-9]+)-([a-z0-9]+)-(\d{4}-\d{2}-\d{2})(?:-(.+))?$")
MONTHS = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}
# Kalshi league code -> Polymarket slug prefix, where we know both.
# Learned from matched games. A Polymarket league listed here only pairs with its Kalshi
# league (stops e.g. women's Liga F matching men's LaLiga on the same abbreviations).
LEAGUES = {"NBA": "nba", "NFL": "nfl", "NHL": "nhl", "MLB": "mlb", "WNBA": "wnba",
           "NCAAF": "cfb", "NCAAMB": "cbb", "NCAAWB": "cwbb",
           "EPL": "epl", "LALIGA": "lal", "LALIGA2": "es2", "BUNDESLIGA": "bun", "BUNDESLIGA2": "bl2",
           "SERIEA": "sea", "SERIEB": "itsb", "LIGUE1": "fl1", "EREDIVISIE": "ere", "LIGAPORTUGAL": "por",
           "SCOTTISHPREM": "scop", "SUPERLIG": "tur", "SAUDIPL": "spl", "SLGREECE": "gre1", "UKRPL": "ukr1",
           "ARGPREMDIV": "arg", "EFLL1": "el1", "LIGAMX": "mex", "MLS": "mls", "BRASILEIROB": "bra2",
           "UCL": "ucl", "UEFANL": "unl", "INTLFRIENDLY": "fif", "LVAVIR": "lva1",
           "CS2": "cs2", "LOL": "lol", "DOTA2": "dota2", "UFC": "ufc", "WTA": "wta", "ATP": "atp",
           "TEST": "crint", "ODI": "crint"}




# Same idea for Polymarket US (learned from its event slugs). A value may be a tuple
# when one Kalshi league spans several Polymarket US divisions.
POLY_US_LEAGUES = {
    "NBA": "nba", "NFL": "nfl", "NHL": "nhl", "MLB": "mlb", "WNBA": "wnba", "AHL": "ahl", "KHL": "khl",
    "NCAAF": "cfb", "NCAAMB": "cbb", "NCAAWB": "wcbb", "NWSL": "nwsl", "USL": "uslc",
    "EPL": "epl", "EWSL": "wsl", "LALIGA": "lal", "LALIGA2": "lal2", "LIGAF": "ligaf",
    "BUNDESLIGA": "bun", "BUNDESLIGA2": "bun2", "BUNDESLIGAW": "fbl", "SERIEA": "sea", "SERIEB": "srb",
    "SERIEC": ("serca", "sercb", "sercc"), "LIGUE1": "lg1", "EREDIVISIE": "ere", "LIGAPORTUGAL": "ligpor",
    "SCOTTISHPREM": "scp", "EFLL1": "efl1", "BELGIANPL": "bel1", "SUPERLIG": "tsl", "TFF1LIG": "tff1",
    "SLGREECE": "grsl", "SWISSLEAGUE": "swsl", "DENSUPERLIGA": "sld", "ALLSVENSKAN": "alsv",
    "ELITESERIEN": "els", "EKSTRAKLASA": "ekst", "HNL": "hnl", "ROML1": "slr", "SVNPL": "svnp",
    "SVK1L": "nls", "CZEFL": "flc", "CZEFNL": "fnl", "HUNNB1": "nb1", "AUTBUNDES": "atbl",
    "LIGAMX": "lmx", "LIGAEXP": "lexp", "DIMAYOR": "lco", "ECULP": "ecu1", "PERLIGA1": "pl1",
    "BOLPDIV": "lpb", "APFDDH": "par1", "URYPD": "uru1", "ARGPREMDIV": "lpa", "BRASILEIROB": "brb",
    "COPADOBRASIL": "cdb", "JLEAGUE": "j1", "J2LEAGUE": "j2", "KLEAGUE": "kl1", "CHNSL": "csl",
    "THAIL1": "thai1", "IDNSL": "idnsl", "UAEPL": "uaepl", "AFCON": "afcq", "UEFANL": "unl",
    "CONCACAFNL": "cnl", "UCLW": "uwcl",
    "ABA": "aba", "ACB": "acb", "BBL": "bbl", "BBSERIEA": "lba", "BSL": "bsl", "EUROCUP": "eurocup",
    "EUROLEAGUE": "eurolg", "VTB": "vtb", "LKL": "lkl", "NBL": "nbl", "KBL": "kbl", "JBLEAGUE": "jpbl",
    "KORISLIIGA": "koris", "LNBELITE": "lnb", "CZENBL": "cznbl", "SLB": "slb", "KBO": "kbo",
    "ATP": "atp", "ATPCHALLENGER": "atp", "WTA": "wta", "WTACHALLENGER": "wta",
    "ITF": "itfme", "ITFW": "itfwo", "UFC": "ufc", "TEST": "testcr",
    "CS2": "cs2", "DOTA2": "dota2", "LOL": "lol", "COD": "cod", "R6": "r6",
}


def _league_ok(leagues: dict, k_league: str, p_league: str) -> bool:
    """False when either league is known and they don't correspond."""
    allowed = leagues.get(k_league)
    allowed = set(allowed) if isinstance(allowed, tuple) else {allowed} if allowed else set()
    known_p = any(p_league == v or (isinstance(v, tuple) and p_league in v) for v in leagues.values())
    if allowed or known_p:
        return p_league in allowed
    return True


def _split_vs(title: str) -> tuple[str, str] | None:
    parts = re.split(r"\s+vs\.?\s+", title or "", maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return None
    left = parts[0].split(":")[-1].strip()
    right = re.split(r"[:(]| - ", parts[1])[0].strip()
    return (left, right) if left and right else None


def _kalshi_games(kalshi: list[dict]) -> dict[tuple, dict]:
    games: dict[tuple, dict] = {}
    for m in kalshi:
        g = _K_GAME.match(m["id"])
        if not g:
            continue
        league, kind, yy, mon, dd, code, suffix = g.groups()
        if mon not in MONTHS:
            continue
        date = f"20{yy}-{MONTHS[mon]:02d}-{dd}"
        key = (league, date, code)
        game = games.setdefault(key, {"league": league, "date": date, "code": code.lower(),
                                      "teams": None, "markets": []})
        if kind in ("GAME", "MATCH", "FIGHT"):
            teams = _split_vs(m["event_title"])
            if teams:
                game["teams"] = teams
        game["markets"].append((kind, suffix, m))
    return {k: g for k, g in games.items() if g["teams"]}


def _poly_games(poly: list[dict]) -> dict[tuple, dict]:
    games: dict[tuple, dict] = {}
    for m in poly:
        if not m.get("market_type"):
            continue
        g = _P_GAME.match(m.get("series") or "")  # event slug, e.g. nba-bkn-atl-2026-11-07
        if not g:
            continue
        lg, a, b, date, _ = g.groups()
        teams = _split_vs(m["event_title"])
        if not teams:
            continue
        game = games.setdefault((lg, date, a, b), {"league": lg, "date": date, "abbr": (a, b),
                                                   "teams": teams, "markets": []})
        game["markets"].append(m)
    return games


def _days_apart(a: str, b: str) -> int:
    return abs((datetime.fromisoformat(a) - datetime.fromisoformat(b)).days)


def _pair_games(kg: dict, pg: dict, leagues: dict) -> list[tuple[dict, dict, bool, float, str]]:
    """Return (kalshi_game, poly_game, swapped, confidence, how). swapped: kalshi team order is reversed."""
    by_date = defaultdict(list)
    for g in pg.values():
        by_date[g["date"]].append(g)
    out = []
    for k in kg.values():
        best = None
        for d in (0, -1, 1):
            date = datetime.fromisoformat(k["date"]).date().fromordinal(
                datetime.fromisoformat(k["date"]).date().toordinal() + d).isoformat()
            for p in by_date.get(date, []):
                if not _league_ok(leagues, k["league"], p["league"]):
                    continue
                a, b = p["abbr"]
                code_straight = k["code"] == a + b
                code_swapped = k["code"] == b + a
                (k1, k2), (p1, p2) = k["teams"], p["teams"]
                s_straight = (name_sim(k1, p1) + name_sim(k2, p2)) / 2
                s_swapped = (name_sim(k1, p2) + name_sim(k2, p1)) / 2
                swapped = code_swapped or (not code_straight and s_swapped > s_straight)
                sim = s_swapped if swapped else s_straight
                code = code_straight or code_swapped
                if code and sim >= 0.3:
                    conf, how = 0.97, "abbreviations + team names"
                elif code:
                    conf, how = 0.90, "league, date and team abbreviations"
                elif min(name_sim(k1, p2 if swapped else p1), name_sim(k2, p1 if swapped else p2)) >= 0.5:
                    conf, how = 0.70 + 0.2 * sim, "date and team names"
                else:
                    continue
                if d:
                    conf -= 0.05
                if not best or conf > best[3]:
                    best = (k, p, swapped, conf, how)
        if best:
            out.append(best)
    return out


_K_SPREAD = re.compile(r"wins by over (\d+(?:\.\d+)?)", re.I)
_K_TOTAL = re.compile(r"over (\d+(?:\.\d+)?) (?:points|goals|runs|maps|rounds)", re.I)
_P_SPREAD = re.compile(r"\((-\d+(?:\.\d+)?)\)")
_P_TOTAL = re.compile(r"O/U (\d+(?:\.\d+)?)")
_PARTIAL = re.compile(r"half|quarter|period|inning|\b1h\b|\bmap \d|first|1st|set \d", re.I)


def match_games(kalshi: list[dict], poly: list[dict], leagues: dict | None = None) -> list[Match]:
    kg, pg = _kalshi_games(kalshi), _poly_games(poly)
    out: list[Match] = []
    for k, p, swapped, conf, how in _pair_games(kg, pg, LEAGUES if leagues is None else leagues):
        (k1, k2), (p1, p2) = k["teams"], p["teams"]
        # kalshi team name -> polymarket team name
        team_map = {k1: p2, k2: p1} if swapped else {k1: p1, k2: p2}
        abbr_map = {}
        a, b = p["abbr"]
        for kname, pname in team_map.items():
            abbr_map[kname] = a if pname == p1 else b
        has_tie = any(re.search(r"\b(tie|draw)\b", m["outcome"], re.I) for kind, _, m in k["markets"] if kind != "SPREAD")
        why = f"Same game on {k['date']} ({how}): {k1} vs {k2} ↔ {p1} vs {p2}"

        for kind, suffix, km in k["markets"]:
            kout = km["outcome"]
            if kind in ("GAME", "MATCH", "FIGHT"):
                is_tie = bool(re.search(r"\b(tie|draw)\b", kout, re.I))  # "Tie", "Draw/Tie"
                kteam = None if is_tie else max(team_map, key=lambda t: name_sim(t, kout))
                if kteam and name_sim(kteam, kout) == 0:
                    continue  # outcome isn't recognisably either team
                for pm in p["markets"]:
                    if pm["market_type"] != "moneyline" or _PARTIAL.search(pm["title"]):
                        continue
                    yes_no = pm["yes_label"].lower() == "yes"
                    if yes_no:
                        # 3-way style: "Will X win on DATE?" + "Will A vs. B end in a draw?";
                        # the slug ends with the team abbreviation or "draw".
                        tail = pm["slug"].rsplit("-", 1)[-1]
                        if tail == "draw" or "draw" in pm["title"].lower():
                            if is_tie:
                                out.append(Match(km["id"], pm["id"], False, conf, "game", why + " · draw"))
                            continue
                        if not kteam:
                            continue
                        if tail in (a, b):
                            pteam = p1 if tail == a else p2
                        else:
                            pteam = max((p1, p2), key=lambda t: name_sim(pm["outcome"], t))
                        if pteam == team_map[kteam]:
                            out.append(Match(km["id"], pm["id"], False, conf, "game", why + f" · {kout} wins"))
                    elif kteam and not has_tie:
                        # 2-way market, YES = yes_label team. Skipped when the game can end
                        # in a draw: Kalshi then has a separate "Tie" outcome while two-way
                        # markets typically settle a draw at $0.50 (e.g. Polymarket US Test
                        # cricket), so neither polarity is a hedge.
                        target = team_map[kteam]
                        sy, sn = name_sim(pm["yes_label"], target), name_sim(pm["no_label"], target)
                        if sy == sn:
                            continue  # can't tell which side is which team: never guess
                        if sy > sn:
                            out.append(Match(km["id"], pm["id"], False, conf, "game", why + f" · {kout} wins"))
                        else:  # "B wins" == "not A" (a draw is impossible here)
                            out.append(Match(km["id"], pm["id"], True, round(conf * 0.95, 3), "game",
                                             why + f" · Kalshi '{kout} wins' = Polymarket NO on {pm['yes_label']}"
                                             " (check tie/cancellation rules)"))
            elif kind == "SPREAD":
                ks = _K_SPREAD.search(km["title"])
                if not ks or _PARTIAL.search(km["title"]):
                    continue
                kteam = max(team_map, key=lambda t: name_sim(t, km["title"].split(" wins")[0]))
                for pm in p["markets"]:
                    ps = _P_SPREAD.search(pm["title"])
                    if pm["market_type"] != "spreads" or not ps or _PARTIAL.search(pm["title"]):
                        continue
                    if float(ps.group(1)) != -float(ks.group(1)):
                        continue
                    if name_sim(pm["yes_label"], team_map[kteam]) >= 0.5:
                        out.append(Match(km["id"], pm["id"], False, conf * 0.98, "spread",
                                         why + f" · {kteam} wins by more than {ks.group(1)}"))
            elif kind == "TOTAL":
                kt = _K_TOTAL.search(km["title"])
                if not kt or _PARTIAL.search(km["title"].replace("Full Game", "")):
                    continue
                for pm in p["markets"]:
                    pt = _P_TOTAL.search(pm["title"])
                    if pm["market_type"] != "totals" or not pt or _PARTIAL.search(pm["title"] + pm["slug"]):
                        continue
                    if float(pt.group(1)) == float(kt.group(1)) and pm["yes_label"].lower() == "over":
                        out.append(Match(km["id"], pm["id"], False, conf * 0.98, "total",
                                         why + f" · total over {kt.group(1)}"))
    return out


# ---------------------------------------------------------------- pass 2: text

_DEADLINE = re.compile(r"\b(before|by|until|through|prior to|no later than)\b", re.I)
RARE_IDF = 7.0  # token in fewer than ~0.1% of markets

def _doc(m: dict) -> list[str]:
    title = m["title"]
    # Kalshi titles of multi-outcome events are often generic ("Who will win X?"); add the
    # outcome. The event title carries context such as the city of a weather market.
    extra = m["outcome"] if m["outcome"] and m["outcome"].lower() not in title.lower() else ""
    ev = m["event_title"] if m["event_title"].lower() != title.lower() else ""
    return tokens(f"{title} {extra} {ev}")


def match_text(kalshi: list[dict], poly: list[dict], exclude_k: set, exclude_p: set,
               min_conf: float = 0.40) -> list[Match]:
    K = [m for m in kalshi if m["id"] not in exclude_k and not m["id"].startswith("KXMVE")
         and not _K_GAME.match(m["id"])]
    P = [m for m in poly if m["id"] not in exclude_p and not m.get("market_type")]
    kdocs = [set(_doc(m)) for m in K]
    pdocs = [set(_doc(m)) for m in P]

    df = defaultdict(int)
    for d in kdocs + pdocs:
        for t in d:
            df[t] += 1
    n = len(kdocs) + len(pdocs)
    idf = {t: math.log(n / c) for t, c in df.items()}
    norm = lambda d: math.sqrt(sum(idf[t] ** 2 for t in d)) or 1.0

    index = defaultdict(list)
    for j, d in enumerate(pdocs):
        for t in d:
            index[t].append(j)
    max_post = max(50, len(pdocs) // 20)  # skip very common tokens for candidate generation
    pnorm = [norm(d) for d in pdocs]
    pnums = [numbers(list(d)) for d in pdocs]
    pyears = [years(list(d)) for d in pdocs]
    pout = [name_tokens(m["outcome"]) for m in P]
    pclose = [_dt(m["close_time"]) for m in P]
    now = datetime.now(timezone.utc)

    best_k: dict[int, tuple[float, int, str]] = {}
    for i, d in enumerate(kdocs):
        scores = defaultdict(float)
        for t in d:
            post = index.get(t)
            if post and len(post) <= max_post:
                w = idf[t] ** 2
                for j in post:
                    scores[j] += w
        if not scores:
            continue
        kn = norm(d)
        knums, kyears = numbers(list(d)), years(list(d))
        kout = name_tokens(K[i]["outcome"])
        kclose = _dt(K[i]["close_time"])
        for j, s in sorted(scores.items(), key=lambda x: -x[1])[:15]:
            pd = pdocs[j]
            full = sum(idf[t] ** 2 for t in d & pd) / (kn * pnorm[j])
            if full < min_conf:
                continue
            # Shared names (candidate, team) inflate similarity; the rest of the question
            # (the predicate: "win the election", "finish last", ...) must agree too.
            names = (kout | pout[j]) & d & pd
            kc, pc = d - names, pd - names
            if any(bool(g & kc) != bool(g & pc) for g in CONFLICT_GROUPS):
                continue
            ctx = (sum(idf[t] ** 2 for t in kc & pc) / (norm(kc) * norm(pc))) if kc and pc else (1.0 if not kc and not pc else 0.0)
            if names and ctx < 0.30:
                continue
            cos = 0.4 * full + 0.6 * ctx if names else full
            if cos < min_conf:
                continue
            notes = [] if not names else [f"question wording {ctx:.0%} similar"]
            # Each side names something specific the other doesn't ("SpaceX" vs "elections").
            def distinct(mine, other):  # ignore spelling variants: searched/search, zelenskyy/zelensky
                pre = {t[:5] for t in other}
                return max(((idf[t], t) for t in mine - other if not t[0].isdigit() and t[:5] not in pre),
                           default=(0, ""))
            ku, pu = distinct(kc, pc), distinct(pc, kc)
            if ku[0] >= RARE_IDF and pu[0] >= RARE_IDF:
                cos *= 0.75
                notes.append(f"'{ku[1]}' vs '{pu[1]}'")
            if knums != pnums[j] and (knums or pnums[j]):
                if knums and pnums[j]:
                    continue  # different strikes / thresholds / dates
                cos *= 0.85
                notes.append("number only on one side")
            if kyears and pyears[j] and not (kyears & pyears[j]):
                continue
            if not same_name(K[i]["outcome"], P[j]["outcome"]):
                continue  # different candidate / team ("Chris Olave" vs "Chris Godwin")
            if kclose and pclose[j]:
                gap = abs((kclose - pclose[j]).days)
                short = (min(kclose, pclose[j]) - now).days < 14
                if short and gap > 3:
                    continue  # recurring short-dated markets (daily/weekly) must line up
                # "Will X happen before <date>?" with different dates are different bets:
                # an event between the two deadlines makes both sides lose.
                if gap > 7 and (_DEADLINE.search(K[i]["title"]) or _DEADLINE.search(P[j]["title"])):
                    continue
                cos *= 1.0 if gap <= 3 else 0.95 if gap <= 31 else 0.88 if gap <= 400 else 0.75
                if gap > 31:
                    notes.append(f"close dates {gap}d apart")
            if cos >= min_conf and (i not in best_k or cos > best_k[i][0]):
                best_k[i] = (cos, j, "; ".join(notes))

    # Keep mutual-best pairs only.
    best_p: dict[int, tuple[float, int]] = {}
    for i, (s, j, _) in best_k.items():
        if j not in best_p or s > best_p[j][0]:
            best_p[j] = (s, i)
    out = []
    for i, (s, j, notes) in best_k.items():
        if best_p[j][1] != i:
            continue
        reason = f"Text similarity {s:.0%}" + (f" ({notes})" if notes else "")
        out.append(Match(K[i]["id"], P[j]["id"], False, round(min(s, 0.95), 3), "text", reason))
    return out


def match_all(kalshi: list[dict], poly: list[dict], leagues: dict | None = None) -> list[Match]:
    games = match_games(kalshi, poly, leagues)
    text = match_text(kalshi, poly, {m.kalshi_id for m in games}, {m.poly_id for m in games})
    return games + text
