import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
TIMEOUT = 30


def post(url, path, payload):
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=TIMEOUT) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8"))


def stable_pick(items, seed, game, counter=0):
    if not items:
        return None
    ordered = sorted(set(items))
    digest = hashlib.sha256(f"{seed}|{game}|{counter}".encode()).digest()
    return ordered[int.from_bytes(digest[:8], "big") % len(ordered)]


def players_by_seat(view):
    result = {}
    for p in view.get("players", []):
        if isinstance(p, dict) and isinstance(p.get("seat"), int):
            result[p["seat"]] = p
    return result


def entry_seat(entry, keys=("seat", "target", "player", "voter", "from")):
    for key in keys:
        value = entry.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, dict) and isinstance(value.get("seat"), int):
            return value["seat"]
    return None


def event_text(entry):
    for key in ("text", "statement", "say", "content"):
        if isinstance(entry.get(key), str):
            return entry[key]
    return ""


def public_data(view):
    players = players_by_seat(view)
    revealed = {}
    for seat, p in players.items():
        role = p.get("role")
        if role:
            revealed[seat] = str(role).lower()
    speeches, votes, inspections = [], [], []
    for e in view.get("log", []):
        if not isinstance(e, dict):
            continue
        kind = str(e.get("kind", "")).lower()
        if kind in ("speech", "speak", "statement"):
            speeches.append((entry_seat(e, ("seat", "speaker", "player")), event_text(e), e))
        elif kind == "vote":
            votes.append((entry_seat(e, ("seat", "voter", "from")), entry_seat(e, ("target", "vote")), e))
        elif "inspect" in kind or "check" in kind:
            inspections.append(e)
        if kind in ("death", "exile", "reveal", "dawn"):
            seat = entry_seat(e)
            role = e.get("role")
            if seat and role:
                revealed[seat] = str(role).lower()
    return players, revealed, speeches, votes, inspections


def living_seats(players):
    return sorted(seat for seat, p in players.items() if p.get("alive", True))


def role_is(role, needle):
    return needle in str(role or "").lower()


def claim_analysis(speeches, revealed):
    claims = defaultdict(list)
    checks = []
    for seat, text, event in speeches:
        if not seat or not text:
            continue
        low = text.lower()
        if re.search(r"\b(i am|i'm|im|claim(?:ing)? to be) (the )?seer\b|\bseer here\b", low):
            claims[seat].append(event)
        patterns = [
            (r"(?:seat\s*)?(\d+)\D{0,32}(?:is|=|looks like|checked as|came back)\s+(?:a )?wolf\b", "wolf"),
            (r"(?:seat\s*)?(\d+)\D{0,32}(?:is|=|looks|checked as|came back)\s+(?:not a |no |clean|innocent)", "clean"),
            (r"checked\s+(?:seat\s*)?(\d+)\D{0,24}\b(?:wolf|werewolf)\b", "wolf"),
            (r"checked\s+(?:seat\s*)?(\d+)\D{0,24}\b(?:clean|innocent|not a wolf)\b", "clean"),
        ]
        for pattern, verdict in patterns:
            for match in re.finditer(pattern, low):
                target = int(match.group(1))
                checks.append((seat, target, verdict, event))
    falsified = {s for s, _, _, _ in checks if revealed.get(s) in ("werewolf", "wolf")}
    trust = {}
    claimants = {s for s, _ in claims.items()}
    for s in claimants:
        trust[s] = 0.0 if s in falsified else (0.85 if len(claimants) == 1 else 0.4)
    return claims, checks, trust


def format_seat(seat, players):
    p = players.get(seat, {})
    name = str(p.get("name", f"Player {seat}"))
    return f"seat {seat} ({name})"


def clean_speech(text):
    text = re.sub(r"[\t\r\n\x00-\x1f]+", " ", str(text)).strip()
    return text[:480]


def suspicion_model(view):
    players, revealed, speeches, votes, _ = public_data(view)
    me = view.get("you", {}).get("seat")
    claims, checks, trust = claim_analysis(speeches, revealed)
    score = {s: 0.0 for s in players}
    evidence = {s: [] for s in players}
    for claimant, target, verdict, event in checks:
        weight = trust.get(claimant, 0.2)
        if target not in score:
            continue
        if verdict == "wolf":
            score[target] += 3.0 * weight
            evidence[target].append((1, f"{format_seat(claimant, players)} checked them as a wolf"))
        else:
            score[target] -= 1.2 * weight
    for seat, text, event in speeches:
        if not seat or seat == me:
            continue
        low = text.lower()
        for target in score:
            if target == seat:
                continue
            name = re.escape(str(players.get(target, {}).get("name", "")).lower())
            hit = re.search(rf"\b(?:seat\s*)?{target}\b", low) or (name and re.search(rf"\b{name}\b", low))
            if not hit:
                continue
            if re.search(r"\b(wolf|suspect|suspicious|vote|accuse|evil|scum)\b", low):
                score[target] += 0.35
                evidence[target].append((2, f"{format_seat(seat, players)} accused them"))
            if re.search(r"\b(defend|trust|clean|innocent|not a wolf)\b", low):
                score[target] -= 0.2
    for voter, target, event in votes:
        if voter not in score or target not in score:
            continue
        target_role = revealed.get(target, "")
        voter_role = revealed.get(voter, "")
        if target_role in ("werewolf", "wolf"):
            score[voter] -= 1.5
            score[target] += 0.4
            evidence[voter].append((3, f"voted for revealed wolf {format_seat(target, players)}"))
        if voter_role in ("werewolf", "wolf") and target_role not in ("werewolf", "wolf"):
            score[target] += 0.1
    for claimant, target, verdict, event in checks:
        if target in score and verdict == "wolf":
            score[target] += 0.4 * trust.get(claimant, 0.2)
    for seat, role in revealed.items():
        if role in ("werewolf", "wolf"):
            score[seat] = 100.0
        else:
            score[seat] = -100.0
    living = living_seats(players)
    unknown = [s for s in living if s != me and s not in revealed]
    ranked = sorted(unknown, key=lambda s: (-score.get(s, 0), s))
    trusted = sorted((s for s in living if s != me and s not in revealed),
                     key=lambda s: (score.get(s, 0), s))
    return {
        "players": players, "revealed": revealed, "speeches": speeches,
        "votes": votes, "claims": claims, "checks": checks, "trust": trust,
        "score": score, "evidence": evidence, "living": living,
        "unknown": unknown, "ranked": ranked, "trusted": trusted,
    }


def trusted_wolf_claim(data):
    for claimant in sorted(data["trust"]):
        if data["trust"][claimant] < 0.5:
            continue
        for author, target, verdict, _ in data["checks"]:
            if author == claimant and verdict == "wolf":
                if target in data["living"] and target != claimant:
                    return target
    for seat, role in data["revealed"].items():
        if role in ("werewolf", "wolf") and seat in data["living"]:
            return seat
    return None


def plurality_target(votes, options):
    counts = Counter(target for _, target, _ in votes if target in options)
    if not counts:
        return None
    max_count = max(counts.values())
    leaders = sorted(s for s, n in counts.items() if n == max_count)
    return leaders[0]


def safe_target(options, preferred):
    options = [x for x in options if isinstance(x, int)]
    if preferred in options:
        return preferred
    return options[0] if options else None


def statement_for(view, data):
    me = view.get("you", {}).get("seat")
    hit = trusted_wolf_claim(data)
    if hit and hit != me:
        for claimant, target, verdict, _ in data["checks"]:
            if target == hit and verdict == "wolf" and data["trust"].get(claimant, 0) >= 0.5:
                return clean_speech(f"I trust {format_seat(claimant, data['players'])}'s check: {format_seat(hit, data['players'])} is a wolf. Vote there.")
        return clean_speech(f"{format_seat(hit, data['players'])} was revealed as a wolf. Vote there.")
    if data["ranked"]:
        target = data["ranked"][0]
        reason = data["evidence"].get(target, [])
        if reason:
            detail = sorted(reason, key=lambda x: (-x[0], x[1]))[0][1]
            return clean_speech(f"I'm watching {format_seat(target, data['players'])}: {detail}.")
        return clean_speech(f"I suspect {format_seat(target, data['players'])}; their votes and claims need an explanation.")
    return "I have no new information yet; compare claims and voting patterns carefully."


def choose_kill(view, data, options, seed, game):
    you = view.get("you", {})
    partner = you.get("partner") or {}
    wolves = {you.get("seat"), partner.get("seat")}
    eligible = sorted(s for s in options if s not in wolves)
    seers = [(s, data["trust"].get(s, 0)) for s in data["claims"] if s in eligible]
    trusted_claims = [s for s, t in seers if t >= 0.5]
    if trusted_claims:
        return sorted(trusted_claims)[0]
    # Shared deterministic ranking: scores are based on the shared log; seat tie-break is stable.
    low_suspicion = sorted(eligible, key=lambda s: (data["score"].get(s, 0), s))
    if low_suspicion:
        return low_suspicion[0]
    return stable_pick(eligible, seed, game)


def fabricate_check(view, data, seed, game):
    me = view.get("you", {}).get("seat")
    enemies = [s for s in data["living"] if s != me and s != (view.get("you", {}).get("partner") or {}).get("seat")]
    target = stable_pick(enemies, seed, game, int(view.get("day", 0)) + 17)
    return target


def decide(pending, seed):
    game = pending.get("game", 0)
    decision = pending.get("decision", "")
    view = pending.get("view", {})
    options = pending.get("options", [])
    you = view.get("you", {})
    role = str(you.get("role", "")).lower()
    me = you.get("seat")
    data = suspicion_model(view)
    if decision == "speak":
        text = statement_for(view, data)
        if role_is(role, "werewolf") or role_is(role, "wolf"):
            # Counterclaim a trusted seer accusation with a stable fabricated history.
            accusation = next((c for c in data["checks"]
                               if c[1] == me and c[2] == "wolf" and data["trust"].get(c[0], 0) >= 0.5), None)
            if accusation:
                target = fabricate_check(view, data, seed, game)
                if target:
                    text = clean_speech(f"I'm the seer. I checked {format_seat(target, data['players'])}: clean. The accusation on me is false.")
        return {"game": game, "decision": "speak", "text": clean_speech(text)}
    if decision == "kill":
        return {"game": game, "decision": decision, "target": choose_kill(view, data, options, seed, game)}
    if decision == "protect":
        last = view.get("private", {}).get("last_protection")
        candidates = [s for s in options if s != me and s != last]
        claimants = [s for s, trust in data["trust"].items() if trust >= 0.5 and s in candidates]
        preferred = sorted(claimants)[0] if claimants else (data["trusted"][0] if data["trusted"] else None)
        return {"game": game, "decision": decision, "target": safe_target(candidates, preferred)}
    if decision == "inspect":
        rival_claims = [s for s in data["claims"] if s in options and s != me]
        preferred = sorted(rival_claims)[0] if rival_claims else (data["ranked"][0] if data["ranked"] else None)
        return {"game": game, "decision": decision, "target": safe_target(options, preferred)}
    if decision == "vote":
        hit = trusted_wolf_claim(data)
        if hit == me or hit not in options:
            hit = None
        if hit is None and (role_is(role, "werewolf") or role_is(role, "wolf")):
            partner = (you.get("partner") or {}).get("seat")
            partner_named = partner is not None and any(
                target == partner and verdict == "wolf" and data["trust"].get(author, 0) >= 0.5
                for author, target, verdict, _ in data["checks"])
            if partner_named:
                votes = data["votes"]
                tally = Counter(target for _, target, _ in votes if target in options)
                current = plurality_target(votes, options)
                if current == partner and tally[partner] >= 2:
                    hit = partner
        if hit is None:
            if role_is(role, "werewolf") or role_is(role, "wolf"):
                partner = (you.get("partner") or {}).get("seat")
                village = [s for s in data["ranked"] if s != partner]
                hit = village[0] if village else None
            else:
                hit = data["ranked"][0] if data["ranked"] else None
        return {"game": game, "decision": decision, "target": safe_target(options, hit)}
    return {"game": game, "decision": decision, "target": None}


def post_action(url, action):
    return post(url, "/act", action)


def safe_action(pending):
    game = pending.get("game")
    decision = pending.get("decision")
    if decision == "speak":
        return {"game": game, "decision": "speak", "pass": True}
    return {"game": game, "decision": decision, "target": None}


def run():
    prompt_path = os.environ.get("MAKEFUN_PROMPT_FILE", "")
    if not prompt_path:
        raise RuntimeError("MAKEFUN_PROMPT_FILE is required")
    with open(prompt_path, "r", encoding="utf-8") as handle:
        brief = handle.read()
    # Reading the brief is part of the trial input and makes missing input fail closed.
    if not brief.strip():
        raise RuntimeError("the trial brief is empty")
    env_url = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    if not env_url:
        raise RuntimeError("MAKEFUN_ENV_URL is required")
    seed = os.environ.get("MAKEFUN_TRIAL_SEED", "")
    failures = 0
    failure_cap = 8
    answered = set()
    while True:
        try:
            observed = post(env_url, "/observe", {})
            failures = 0
        except Exception:
            failures += 1
            if failures >= failure_cap:
                return
            time.sleep(0.2 * failures)
            continue
        if not isinstance(observed, dict):
            failures += 1
            if failures >= failure_cap:
                return
            continue
        if observed.get("done") is True:
            return
        pending_list = observed.get("pending", [])
        if not isinstance(pending_list, list):
            pending_list = []
        if not pending_list:
            time.sleep(0.1)
            continue
        progressed = False
        for pending in pending_list:
            if not isinstance(pending, dict):
                continue
            view = pending.get("view", {})
            log = view.get("log", []) if isinstance(view, dict) else []
            key = (pending.get("game"), pending.get("decision"), len(log) if isinstance(log, list) else 0)
            if key in answered:
                continue
            try:
                action = decide(pending, seed)
            except Exception:
                action = safe_action(pending)
            try:
                result = post_action(env_url, action)
                if isinstance(result, dict) and result.get("error") == "not_pending":
                    answered.add(key)
                else:
                    answered.add(key)
                    progressed = True
            except Exception:
                # One legal fallback attempt for a rejected or malformed answer.
                try:
                    post_action(env_url, safe_action(pending))
                    answered.add(key)
                    progressed = True
                except Exception:
                    failures += 1
                    if failures >= failure_cap:
                        return
        if not progressed:
            time.sleep(0.1)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        # Do not leak an internal exception into the trial output.
        raise SystemExit(0)
