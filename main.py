#!/usr/bin/env python3
"""Deterministic Werewolf agent for make.fun."""
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fold_text(value):
    value = re.sub(r"[\r\n\t]+", " ", str(value))
    value = "".join(ch for ch in value if ord(ch) >= 32 and ord(ch) != 127)
    return value.strip()


def stable_number(seed, game, counter):
    raw = f"{seed}|{game}|{counter}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest(), "big")


def post(url, body, opener, timeout=30):
    request = urllib.request.Request(
        url,
        data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=timeout) as response:
        payload = response.read()
    return json.loads(payload.decode("utf-8"))


def alive_players(view):
    players = view.get("players") or []
    return sorted(
        [p for p in players if p.get("alive") is True and isinstance(p.get("seat"), int)],
        key=lambda p: p["seat"],
    )


def player_map(view):
    return {p["seat"]: p for p in view.get("players", []) if isinstance(p.get("seat"), int)}


def public_roles(view):
    return {
        p["seat"]: p.get("role")
        for p in view.get("players", [])
        if isinstance(p.get("seat"), int) and p.get("role")
    }


def statement_claims(text, seat_numbers):
    lowered = text.lower()
    seer = bool(re.search(r"\b(i am|i'm|im|as) (the )?seer\b|\bseer here\b|\bclaim(?:ing)? seer\b", lowered))
    claims = []
    for seat in seat_numbers:
        names = (rf"(?:seat\s*|player\s*|#)\s*{seat}\b", rf"\b{seat}\b")
        matched = any(re.search(pattern, lowered) for pattern in names)
        if not matched:
            continue
        if re.search(rf"(?:seat\s*|player\s*|#)?\s*{seat}\b[^.]*\b(?:is|looks|seems)\s+(?:a\s+)?wolf\b", lowered) or re.search(
            rf"\b(?:wolf|werewolf)\b[^.]*\b(?:seat\s*|player\s*|#)?\s*{seat}\b", lowered
        ):
            claims.append((seat, True))
        elif re.search(rf"(?:seat\s*|player\s*|#)?\s*{seat}\b[^.]*\b(?:is|checked as|came back)\s+(?:not a |non-)?wolf\b", lowered) or re.search(
            rf"\b(?:clean|innocent|not wolf)\b[^.]*\b{seat}\b", lowered
        ):
            claims.append((seat, False))
    return seer, claims


def game_evidence(view):
    players = player_map(view)
    seats = sorted(players)
    roles = public_roles(view)
    alive = {p["seat"] for p in alive_players(view)}
    log = view.get("log") or []
    claims_by = {}
    accusations = []
    votes = []
    for entry in log:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        seat = entry.get("seat")
        if kind == "speech" and isinstance(seat, int):
            seer, claims = statement_claims(entry.get("text", ""), seats)
            record = claims_by.setdefault(seat, {"seer": False, "checks": []})
            record["seer"] = record["seer"] or seer
            record["checks"].extend((target, result) for target, result in claims)
            accusations.extend((seat, target, result, entry.get("day", 0)) for target, result in claims)
        elif kind == "vote" and isinstance(seat, int) and isinstance(entry.get("target"), int):
            votes.append((entry.get("day", 0), seat, entry["target"]))
    score = {seat: 0.0 for seat in seats}
    trusted_checks = {}
    seer_claimants = [seat for seat, claim in claims_by.items() if claim["seer"]]
    for claimant in sorted(seer_claimants):
        contradictions = any(
            roles.get(target) is not None and ((roles[target] == "werewolf") != is_wolf)
            for target, is_wolf in claims_by[claimant]["checks"]
        )
        if contradictions:
            trust = 0.0
        elif len(seer_claimants) == 1:
            trust = 0.85
        else:
            trust = 0.4
        if trust >= 0.4:
            for target, is_wolf in claims_by[claimant]["checks"]:
                trusted_checks[target] = (is_wolf, trust, claimant)
        for target, is_wolf in claims_by[claimant]["checks"]:
            if target in score:
                score[target] += (3.5 if is_wolf else -2.6) * trust
        if trust > 0:
            score[claimant] -= trust * 0.7

    own_seat = (view.get("you") or {}).get("seat")
    for target in private_seer_wolves(view):
        if target in score:
            score[target] += 8.0
            trusted_checks[target] = (True, 1.0, own_seat)
    for target in private_seer_clean(view):
        if target in score:
            score[target] -= 8.0

    for accuser, target, is_wolf, day in accusations:
        if target in score and not claims_by.get(accuser, {}).get("seer"):
            score[target] += 0.42 if is_wolf else -0.12
    for day, voter, target in votes:
        if target not in score:
            continue
        revealed = roles.get(target)
        if revealed == "werewolf":
            score[voter] -= 1.8
        elif revealed and revealed != "werewolf":
            score[voter] += 0.18

    for seat in seats:
        if roles.get(seat) == "werewolf":
            score[seat] = 100.0
        elif seat not in alive:
            score[seat] *= 0.5
    return {
        "scores": score,
        "claims": claims_by,
        "trusted_checks": trusted_checks,
        "seer_claimants": sorted(seer_claimants),
        "roles": roles,
        "alive": alive,
        "votes": votes,
        "accusations": accusations,
        "players": players,
        "log": log,
    }


def choose_ranked(candidates, scores, seed, game, counter, high=True):
    candidates = sorted(set(candidates))
    if not candidates:
        return None
    best = max(scores.get(s, 0.0) for s in candidates) if high else min(scores.get(s, 0.0) for s in candidates)
    tied = [s for s in candidates if scores.get(s, 0.0) == best]
    return min(tied, key=lambda s: (stable_number(seed, game, counter + s), s))


def plurality_target(evidence, candidates, seed, game):
    vote_counts = {}
    for _day, _voter, target in evidence["votes"]:
        vote_counts[target] = vote_counts.get(target, 0) + 1
    if vote_counts:
        peak = max(vote_counts.values())
        leaders = sorted(s for s in candidates if vote_counts.get(s, 0) == peak and peak > 0)
        if leaders:
            return leaders[0]
    return choose_ranked(candidates, evidence["scores"], seed, game, 310, True)


def format_seat(view, seat):
    player = player_map(view).get(seat, {})
    name = fold_text(player.get("name", "Unknown"))
    return f"seat {seat} ({name})"


def speak_action(view, evidence, seed, game, role):
    me = view["you"]["seat"]
    alive = sorted(evidence["alive"] - {me})
    if role == "werewolf":
        named_by_seer = any(
            claimant in evidence["seer_claimants"]
            and any(target == me for target, _result in record.get("checks", []))
            for claimant, record in evidence["claims"].items()
        )
        if named_by_seer:
            fabricated = wolf_counterclaim(view, evidence, seed, game)
            if fabricated:
                return {"text": fabricated[:480]}
    for target, (is_wolf, trust, claimant) in sorted(evidence["trusted_checks"].items()):
        if is_wolf and target in evidence["alive"]:
            text = f"{format_seat(view, claimant)} claimed a seer check: {format_seat(view, target)} is a wolf. Vote {format_seat(view, target)}."
            return {"text": text[:480]}
    known_wolves = sorted(s for s, r in evidence["roles"].items() if r == "werewolf" and s in evidence["alive"])
    if known_wolves and role != "werewolf":
        target = known_wolves[0]
        return {"text": f"{format_seat(view, target)} was revealed as a werewolf. I will vote {format_seat(view, target)}."}
    if role == "werewolf":
        partner = (view.get("you", {}).get("partner") or {}).get("seat")
        candidates = [s for s in alive if s != partner and s not in evidence["roles"]]
        target = choose_ranked(candidates, evidence["scores"], seed, game, 420, True)
        if target is None:
            target = choose_ranked(alive, evidence["scores"], seed, game, 421, True)
        return {"text": f"We should focus on {format_seat(view, target)}; the current evidence points there."} if target else {"pass": True}
    target = choose_ranked(alive, evidence["scores"], seed, game, 430, True)
    if target is None or evidence["scores"].get(target, 0) < 0.5:
        return {"text": "Let's compare the vote history and specific claims before settling on a target."}
    return {"text": f"I suspect {format_seat(view, target)}: the claim and vote pattern deserve scrutiny."}


def fabricated_wolf_check(view, evidence, seed, game):
    me = view["you"]["seat"]
    choices = sorted(s for s in evidence["alive"] if s != me and s not in evidence["roles"])
    target = choose_ranked(choices, evidence["scores"], seed, game, 510, True)
    return target


def decision_action(pending, seed):
    game = pending.get("game", 0)
    kind = pending.get("decision")
    view = pending.get("view") or {}
    you = view.get("you") or {}
    role = you.get("role", "villager")
    options = pending.get("options") or []
    options = sorted(s for s in options if isinstance(s, int))
    evidence = game_evidence(view)
    if kind == "speak":
        action = speak_action(view, evidence, seed, game, role)
        text = fold_text(action.get("text", ""))
        if not text:
            return {"game": game, "decision": kind, "pass": True}
        max_chars = min(500, max(1, int(pending.get("max_chars", 500))))
        if len(text) > max_chars:
            text = text[:max_chars]
        return {"game": game, "decision": kind, "text": text}
    if not options:
        return {"game": game, "decision": kind, "target": None}
    me = you.get("seat")
    candidates = [s for s in options if s != me]
    if not candidates:
        candidates = options[:]
    target = None
    if kind == "inspect":
        rival_claims = [s for s in evidence["seer_claimants"] if s in candidates and s != me]
        target = choose_ranked(rival_claims, evidence["scores"], seed, game, 610, True)
        if target is None:
            unknown = [s for s in candidates if s not in evidence["roles"]]
            target = choose_ranked(unknown or candidates, evidence["scores"], seed, game, 611, True)
    elif kind == "protect":
        trusted_seers = [
            s for s in evidence["seer_claimants"]
            if s in candidates and any(claimant == s for _, (_, _, claimant) in evidence["trusted_checks"].items())
        ]
        target = choose_ranked(trusted_seers, evidence["scores"], seed, game, 620, False)
        if target is None:
            target = choose_ranked(candidates, evidence["scores"], seed, game, 621, False)
    elif kind == "kill":
        partner = (you.get("partner") or {}).get("seat")
        safe = [s for s in candidates if s != partner and evidence["roles"].get(s) != "werewolf"]
        trusted_claimants = [
            s for s in evidence["seer_claimants"]
            if s in safe and evidence["claims"].get(s, {}).get("seer")
        ]
        target = choose_ranked(trusted_claimants, evidence["scores"], seed, game, 630, False)
        if target is None:
            least_suspected = [s for s in safe if evidence["scores"].get(s, 0.0) <= 0.2]
            target = choose_ranked(least_suspected or safe, evidence["scores"], seed, game, 631, False)
        if target is None:
            target = choose_ranked(candidates, evidence["scores"], seed, game, 632, False)
    elif kind == "vote":
        if role == "seer":
            personally_confirmed = [s for s in private_seer_wolves(view) if s in candidates]
            if personally_confirmed:
                target = min(personally_confirmed)
        for seat, (is_wolf, trust, claimant) in sorted(evidence["trusted_checks"].items()):
            if target is None and is_wolf and seat in candidates:
                target = seat
                break
        if target is None:
            revealed_wolves = [s for s in candidates if evidence["roles"].get(s) == "werewolf"]
            target = min(revealed_wolves) if revealed_wolves else None
        if target is None and role == "werewolf":
            partner = (you.get("partner") or {}).get("seat")
            named_partner = any(
                candidate == partner and trust >= 0.4
                for candidate, (_is_wolf, trust, _claimant) in evidence["trusted_checks"].items()
            )
            if named_partner:
                voted = [s for s in candidates if s != partner]
                tally = {}
                for _day, _voter, seat in evidence["votes"]:
                    tally[seat] = tally.get(seat, 0) + 1
                partner_votes = tally.get(partner, 0)
                top_other = max((tally.get(s, 0) for s in voted), default=0)
                if partner_votes > top_other:
                    target = choose_ranked(voted, evidence["scores"], seed, game, 640, True)
        if target is None:
            target = plurality_target(evidence, candidates, seed, game)
        if target is None:
            target = choose_ranked(candidates, evidence["scores"], seed, game, 641, True)
    else:
        return {"game": game, "decision": kind, "target": None}
    if target not in options:
        target = None
    return {"game": game, "decision": kind, "target": target}


def process_pending(pending, seed):
    try:
        return decision_action(pending, seed)
    except Exception:
        game = pending.get("game", 0)
        kind = pending.get("decision", "vote")
        if kind == "speak":
            return {"game": game, "decision": kind, "pass": True}
        return {"game": game, "decision": kind, "target": None}


def run():
    env_url = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    prompt_file = os.environ.get("MAKEFUN_PROMPT_FILE", "")
    if prompt_file:
        try:
            with open(prompt_file, "r", encoding="utf-8") as source:
                source.read()
        except OSError:
            pass
    if not env_url:
        return 0
    seed = os.environ.get("MAKEFUN_TRIAL_SEED", "")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    failures = 0
    while failures < 8:
        try:
            observed = post(env_url + "/observe", {}, opener, timeout=25)
            failures = 0
        except Exception:
            failures += 1
            continue
        if not isinstance(observed, dict):
            failures += 1
            continue
        if observed.get("done") is True:
            return 0
        pending = observed.get("pending") or []
        for item in pending:
            if not isinstance(item, dict):
                continue
            action = process_pending(item, seed)
            try:
                post(env_url + "/act", action, opener, timeout=30)
            except Exception:
                # A transient failure is retried by the next observation if still pending.
                continue
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except Exception:
        raise SystemExit(0)


def wolf_counterclaim(view, evidence, seed, game):
    """A public-state-only fake seer narrative that repeats consistently within a day."""
    me = view.get("you", {}).get("seat")
    day = int(view.get("day", 1) or 1)
    candidates = sorted(
        s for s in evidence["alive"]
        if s != me and evidence["roles"].get(s) != "werewolf"
    )
    if not candidates:
        return None
    results = []
    prior_claim = evidence["claims"].get(me, {})
    for seat, result in prior_claim.get("checks", []):
        if seat in evidence["players"]:
            results.append((seat, result))
    while len(results) <= day:
        pool = [s for s in candidates if all(old != s for old, _ in results)]
        if not pool:
            pool = candidates
        counter = 700 + len(results)
        target = choose_ranked(pool, evidence["scores"], seed, game, counter, True)
        if target is None:
            break
        # Fabricated results are stable from public state and the target seat.
        is_wolf = (stable_number(seed, game, day * 100 + target) % 5 == 0)
        results.append((target, is_wolf))
    if not results:
        return None
    checked, says_wolf = results[-1]
    verdict = "a wolf" if says_wolf else "clean"
    return (
        f"I claim seer: last night I checked {format_seat(view, checked)} "
        f"and they came back {verdict}. We should examine that result."
    )


def private_seer_wolves(view):
    """Return targets personally confirmed as wolves by this seat's seer record."""
    results = view.get("seerResults")
    if not isinstance(results, list):
        return []
    found = []
    for record in results:
        if (
            isinstance(record, dict)
            and isinstance(record.get("seat"), int)
            and record.get("werewolf") is True
        ):
            found.append(record["seat"])
    return sorted(set(found))


def private_seer_clean(view):
    """Return targets personally cleared by this seat's seer record."""
    results = view.get("seerResults")
    if not isinstance(results, list):
        return []
    found = []
    for record in results:
        if (
            isinstance(record, dict)
            and isinstance(record.get("seat"), int)
            and record.get("werewolf") is False
        ):
            found.append(record["seat"])
    return sorted(set(found))


def deduplicate_seats(values):
    result = []
    seen = set()
    for value in values:
        if isinstance(value, int) and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def final_safe_target(target, options):
    """Validate a strategy pick against the env's exact legal option list."""
    if isinstance(target, int) and target in options:
        return target
    return None
