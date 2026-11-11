#!/usr/bin/env python3
"""Deterministic Werewolf player for make.fun agent/v1."""
import hashlib
import json
import os
import re
import sys
import time
import urllib.request

LIMIT = 500
SAFE = 480


def digest(seed, game, label):
    raw = f"{seed}|{game}|{label}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")


def safe_action(kind):
    return {"pass": True} if kind == "speak" else {"target": None}


class Record:
    def __init__(self, view):
        self.view = view
        self.day = view.get("day", 1)
        you = view.get("you") or {}
        self.me = you.get("seat")
        self.role = you.get("role")
        self.partner = (you.get("partner") or {}).get("seat")
        self.players = {p.get("seat"): {"name": p.get("name", "?"), "alive": bool(p.get("alive")), "role": p.get("role")}
                        for p in view.get("players", []) if p.get("seat") is not None}
        self.results = view.get("seerResults") or []
        self.protection = view.get("lastProtect") or {}
        self.wolf_votes = view.get("wolfVotes") or []
        self.log = view.get("log") or []
        self.speeches, self.votes, self.dawns, self.exiles = [], [], [], []
        self.today_votes = []
        self.mine_turns = 0
        voting = False
        for event in self.log:
            kind, day = event.get("kind"), event.get("day")
            if kind == "speech":
                self.speeches.append((day, event.get("round", 0), event.get("seat"), event.get("text", "")))
                if day == self.day and event.get("seat") == self.me:
                    self.mine_turns += 1
            elif kind == "pass" and day == self.day and event.get("seat") == self.me:
                self.mine_turns += 1
            elif kind == "vote":
                voter, target = event.get("seat"), event.get("target")
                self.votes.append((day, voter, target))
                if voting and day == self.day:
                    self.today_votes.append((voter, target))
            elif kind == "voting":
                if day == self.day:
                    voting, self.today_votes = True, []
            elif kind == "dawn":
                self.dawns.append((day, event.get("died"), event.get("role")))
            elif kind == "exile":
                self.exiles.append((day, event.get("seat"), event.get("role")))

    def alive(self):
        return sorted(s for s, p in self.players.items() if p["alive"])

    def role_of(self, seat):
        return self.players.get(seat, {}).get("role")

    def who(self, seat):
        p = self.players.get(seat, {})
        return f"seat {seat} ({p.get('name', '?')})"

    def died_day(self, seat):
        for day, dead, _ in self.dawns:
            if dead == seat:
                return day
        for day, dead, _ in self.exiles:
            if dead == seat:
                return day
        return None


SEER_CLAIM = re.compile(r"\b(?:i am (?:the )?seer|i'm (?:the )?seer|i claim seer|claiming seer|the seer here)\b", re.I)
DOCTOR_CLAIM = re.compile(r"\b(?:i am (?:the )?doctor|i'm (?:the )?doctor|i claim doctor)\b", re.I)
CHECK = re.compile(r"\b(?:checked|checking|inspected|inspect|scanned|peeked|verified)\b", re.I)
WOLF = re.compile(r"\b(?:werewolf|werewolves|wolf|wolves)\b", re.I)
NEG = re.compile(r"\b(?:not|never|no)\s+(?:a\s+)?(?:werewolf|wolf)\b|\b(?:isn't|is not)\s+(?:a\s+)?(?:werewolf|wolf)\b", re.I)
CLEAR = re.compile(r"\b(?:villager|village|innocent|clean|human|town)\b", re.I)


def parse_text(rec, speaker, text):
    normalized = " " + (text or "").lower() + " "
    # Name replacement makes both seat references and names usable in evidence.
    for seat in sorted(rec.players, key=lambda x: (-len(rec.players[x]["name"]), x)):
        name = rec.players[seat]["name"].lower()
        if name:
            normalized = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", f" seat{seat} ", normalized)
    claimed = bool(SEER_CLAIM.search(normalized))
    doc = bool(DOCTOR_CLAIM.search(normalized))
    result, opinion = [], []
    for sentence in re.split(r"[.!?;\n]+", normalized):
        refs = [int(n) for n in re.findall(r"\bseat\s*(\d+)\b", sentence)]
        if not refs:
            continue
        neg = bool(NEG.search(sentence))
        haswolf = bool(WOLF.search(sentence)) and not neg
        hasclear = neg or bool(CLEAR.search(sentence))
        if haswolf == hasclear:
            continue
        verdict = haswolf
        bucket = result if claimed or CHECK.search(sentence) else opinion
        bucket.extend((seat, verdict) for seat in refs if seat != speaker and seat in rec.players)
    return claimed, doc, result, opinion


class Inference:
    def __init__(self, rec, seed, game):
        self.rec = rec
        self.seed = seed
        self.game = game
        self.claimants = {}
        self.doctors = set()
        self.claim_results = {}
        self.opinions = []
        for day, _round, speaker, text in rec.speeches:
            if speaker not in rec.players:
                continue
            seer, doctor, results, opinions = parse_text(rec, speaker, text)
            if seer:
                self.claimants.setdefault(speaker, day)
            if doctor:
                self.doctors.add(speaker)
            if seer or speaker in self.claimants:
                self.claim_results.setdefault(speaker, []).extend((s, w, day) for s, w in results)
            self.opinions.extend((speaker, s, w, day) for s, w in opinions)
        self.trust = self._trust()
        self.suspicion = self._scores()

    def _trust(self):
        out = {}
        for claimant in sorted(self.claimants):
            role = self.rec.role_of(claimant)
            if role is not None:
                out[claimant] = 1.0 if role == "seer" else 0.0
                continue
            contradicted = any((wolf and self.rec.role_of(s) not in (None, "werewolf")) or
                               (not wolf and self.rec.role_of(s) == "werewolf")
                               for s, wolf, _ in self.claim_results.get(claimant, []))
            out[claimant] = 0.0 if contradicted else (0.85 if len(self.claimants) == 1 else 0.4)
        if self.rec.role == "seer":
            for claimant in out:
                if claimant != self.rec.me:
                    out[claimant] = 0.0
        return out

    def best_claim(self):
        vals = [(weight, -seat, seat) for seat, weight in self.trust.items()
                if seat != self.rec.me and weight >= 0.4]
        return max(vals)[2] if vals else None

    def claimed_score(self, seat):
        score = 0.0
        for claimant, results in self.claim_results.items():
            weight = self.trust.get(claimant, 0.0)
            for target, wolf, _ in results:
                if target == seat:
                    score += weight if wolf else -weight
        return score

    def _scores(self):
        scores = {s: 0.0 for s in self.rec.players}
        for claimant, results in self.claim_results.items():
            weight = self.trust.get(claimant, 0.0)
            for seat, wolf, _ in results:
                if seat in scores:
                    scores[seat] += weight * (3.0 if wolf else -2.0)
        # Public voting behavior is evaluated once roles become known.
        for day, voter, target in self.rec.votes:
            if target is None or voter not in scores:
                continue
            revealed = self.rec.role_of(target)
            if revealed == "werewolf":
                scores[voter] -= 1.5
            elif revealed is not None:
                scores[voter] += 0.8
            if revealed is not None and revealed != "werewolf" and self.rec.role_of(voter) == "werewolf":
                scores[voter] -= 0.8
        for speaker, target, accused, _day in self.opinions:
            role = self.rec.role_of(target)
            if role is None:
                continue
            if accused and role == "werewolf":
                scores[speaker] -= 1.0
            elif accused:
                scores[speaker] += 0.6
            elif role == "werewolf":
                scores[speaker] += 1.4
            else:
                scores[speaker] -= 0.4
        spoke = {speaker for _, _, speaker, _ in self.rec.speeches}
        if self.rec.day >= 2:
            for seat in scores:
                if seat != self.rec.me and seat not in spoke and self.rec.players[seat]["alive"]:
                    scores[seat] += 0.4
        return scores

    def pick(self, candidates, scores=None, exclude=(), label="choice"):
        eligible = [s for s in candidates if s != self.rec.me and s not in exclude]
        if not eligible:
            return None
        values = self.suspicion if scores is None else scores
        return max(eligible, key=lambda s: (values.get(s, 0.0), -digest(self.seed, self.game, f"{label}:{s}"), -s))


def decide(seed, game, view, kind, pending):
    rec = Record(view)
    intel = Inference(rec, seed, game)
    opts = [x for x in (pending.get("options") or []) if x in rec.players]
    if kind == "speak":
        statement = speak(rec, intel)
        return {"text": statement} if statement else {"pass": True}
    if kind == "inspect":
        return {"target": inspect(rec, intel, opts)}
    if kind == "protect":
        return {"target": protect(rec, intel, opts)}
    if kind == "kill":
        return {"target": kill(rec, intel, opts)}
    if kind == "vote":
        return {"target": wolf_vote(rec, intel, opts) if rec.role == "werewolf" else village_vote(rec, intel, opts)}
    return safe_action(kind)


def inspect(r, i, options):
    prior = {x.get("seat") for x in r.results}
    fresh = [s for s in options if s not in prior]
    rivals = [s for s in i.claimants if s in fresh and s != r.me]
    return min(rivals) if rivals else i.pick(fresh, label="inspect")


def protect(r, i, options):
    previous = r.protection.get("seat")
    seer = i.best_claim()
    if seer in options and seer != previous:
        return seer
    scores = {s: -i.suspicion.get(s, 0.0) for s in options}
    if previous in options:
        options = [s for s in options if s != previous] or options
    return i.pick(options, scores=scores, label="protect")


def kill(r, i, options):
    options = [s for s in options if s != r.partner]
    if not options:
        return None
    for vote in r.wolf_votes:
        if vote.get("seat") == r.partner and vote.get("night") == r.day and vote.get("target") in options:
            return vote["target"]
    claimants = [s for s in i.claimants if s in options]
    if claimants:
        return max(claimants, key=lambda s: (i.trust.get(s, 0), -s))
    return i.pick(options, scores={s: -i.suspicion.get(s, 0) for s in options}, label="kill")


def village_vote(r, i, options):
    options = [s for s in options if s != r.me]
    if r.role == "seer":
        for found in r.results:
            if found.get("werewolf") and found.get("seat") in options:
                return found["seat"]
    sure = [s for s in options if i.suspicion.get(s, 0) >= 100]
    if sure:
        return min(sure)
    trusted = i.best_claim()
    if trusted is not None:
        named = [s for s, wolf, _ in i.claim_results.get(trusted, []) if wolf and s in options]
        if named:
            return named[-1]
    top = i.pick(options, label="vote")
    tally = {}
    for _, target in r.today_votes:
        if target in options:
            tally[target] = tally.get(target, 0) + 1
    plurality = sorted(tally, key=lambda s: (-tally[s], s))
    if plurality and tally[plurality[0]] >= 2 and top is not None and i.suspicion.get(plurality[0], 0) >= i.suspicion.get(top, 0) - 1.5:
        return plurality[0]
    return top


def wolf_vote(r, i, options):
    options = [s for s in options if s not in (r.me, r.partner)]
    partner_alive = any(p.get("seat") == r.partner and p.get("alive") for p in r.view.get("players", []))
    if partner_alive and r.partner is not None:
        counts = {}
        for _, target in r.today_votes:
            if target is not None:
                counts[target] = counts.get(target, 0) + 1
        partner_votes = counts.get(r.partner, 0)
        largest_other = max((count for seat, count in counts.items() if seat != r.partner), default=0)
        trusted = i.best_claim()
        named_partner = trusted is not None and any(
            seat == r.partner and wolf for seat, wolf, _ in i.claim_results.get(trusted, [])
        )
        if named_partner and partner_votes >= 2 and partner_votes >= largest_other:
            return r.partner
    return i.pick(options, label="wolf-vote")


def evidence(r, i, target):
    for _, voter, voted in r.votes:
        if voter == target and voted is not None:
            role = r.role_of(voted)
            if role is not None and role != "werewolf":
                return f"{r.who(target)} voted out {r.who(voted)}, who was village"
    for speaker, accused, is_wolf, _ in i.opinions:
        if speaker == target and not is_wolf and r.role_of(accused) == "werewolf":
            return f"{r.who(target)} defended {r.who(accused)}, a revealed wolf"
    if r.day >= 2 and not any(s == target for _, _, s, _ in r.speeches):
        return f"{r.who(target)} has barely spoken"
    return f"{r.who(target)}'s record deserves scrutiny"


def my_check_text(r):
    return "; ".join(f"night {x.get('night', 0)} I checked {r.who(x.get('seat'))}: {'werewolf' if x.get('werewolf') else 'not a werewolf'}" for x in r.results)


def speak(r, i):
    round_no = r.mine_turns + 1
    found = next((x.get("seat") for x in r.results if x.get("werewolf") and r.players.get(x.get("seat"), {}).get("alive")), None) if r.role == "seer" else None
    rivals = [s for s in i.claimants if s != r.me]
    if r.role == "seer":
        checks = my_check_text(r)
        if found is not None:
            return trim(f"I am the seer. My checks: {checks}. Vote {r.who(found)} today." + (f" {r.who(rivals[0])} is falsely claiming seer." if rivals else ""))
        if rivals:
            return trim(f"I am the real seer; {r.who(rivals[0])} is lying. My checks: {checks}.")
        if r.day >= 3 and checks:
            return trim(f"I am the seer. My checks so far: {checks}.")
    if r.role == "werewolf":
        accused = i.claimed_score(r.me) >= 0.4 or (r.partner is not None and i.claimed_score(r.partner) >= 0.4)
        others = [s for s in i.claimants if s not in (r.me, r.partner)]
        if accused and others and r.me not in i.claimants:
            checks = []
            nights = list(range(1, r.day)) or [1]
            for night in nights:
                if night == nights[-1]:
                    checks.append(f"night {night} I checked {r.who(others[0])}: werewolf")
                else:
                    candidates = [s for s in sorted(r.players) if s not in (r.me, r.partner, others[0])
                                  and (r.died_day(s) is None or r.died_day(s) >= night)]
                    if candidates:
                        chosen = candidates[digest(i.seed, i.game, f"counter-check:{night}") % len(candidates)]
                        checks.append(f"night {night} I checked {r.who(chosen)}: not a werewolf")
            fake = "; ".join(checks)
            return trim(f"I am the seer. My checks: {fake}. Vote {r.who(others[0])}.")
    trusted = i.best_claim()
    if trusted is not None:
        names = [s for s, wolf, _ in i.claim_results.get(trusted, []) if wolf and r.players.get(s, {}).get("alive")]
        if names:
            return trim(f"{r.who(trusted)}, our seer, named {r.who(names[-1])} as a wolf. We should vote there.")
    pool = [s for s in r.alive() if s not in (r.me, r.partner)]
    target = i.pick(pool, label="speech")
    if target is None:
        return None
    lead = "My main suspicion" if round_no <= 1 else "I will vote"
    return trim(f"{lead} is {r.who(target)}: {evidence(r, i, target)}.")


def trim(text):
    # Keep one-line text and within both platform and our conservative cap.
    text = re.sub(r"\s+", " ", text).strip()[:SAFE]
    # Python slices Unicode code points, matching the env's statement counter.
    return text


class Player:
    def __init__(self, url, seed):
        self.url = url.rstrip("/")
        self.seed = seed
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(self, route, body):
        request = urllib.request.Request(self.url + route, data=json.dumps(body, separators=(",", ":")).encode(),
                                         headers={"content-type": "application/json"}, method="POST")
        with self.opener.open(request, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def run(self):
        while True:
            try:
                status, observation = self.post("/observe", {})
            except Exception:
                time.sleep(1)
                continue
            if status != 200:
                time.sleep(1)
                continue
            if observation.get("done"):
                break
            for pending in observation.get("pending", []):
                self.answer(pending)

    def answer(self, pending):
        game, kind, view = pending.get("game"), pending.get("decision"), pending.get("view")
        if game is None or kind is None or view is None:
            return
        try:
            action = decide(self.seed, game, view, kind, pending)
        except Exception:
            action = safe_action(kind)
        body = {"game": game, "decision": kind, **action}
        try:
            status, _ = self.post("/act", body)
            if status != 200:
                self.post("/act", {"game": game, "decision": kind, **safe_action(kind)})
        except Exception:
            # Re-observation will reveal whether the action was accepted.
            return


def main():
    prompt = os.environ.get("MAKEFUN_PROMPT_FILE", "")
    if prompt:
        try:
            with open(prompt, "r", encoding="utf-8") as handle:
                handle.read()
        except OSError:
            pass
    url = os.environ.get("MAKEFUN_ENV_URL", "")
    if url:
        Player(url, os.environ.get("MAKEFUN_TRIAL_SEED", "")).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
