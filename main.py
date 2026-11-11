#!/usr/bin/env python3
"""Deterministic Werewolf agent for the make.fun agent/v1 protocol.

Decisions are pure functions of the brief, the available views, and
MAKEFUN_TRIAL_SEED, so the same inputs always produce the same choices.
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.request

MAX_TEXT = 480


def rank_hash(seed, game, purpose):
    token = f"{seed}|{game}|{purpose}".encode("utf-8")
    return hashlib.sha256(token).digest()


class Table:
    def __init__(self, view):
        self.view = view if isinstance(view, dict) else {}
        you = self.view.get("you") or {}
        self.me = you.get("seat")
        self.role = you.get("role")
        partner = you.get("partner") or {}
        self.partner = partner.get("seat")
        self.day = self.view.get("day", 1)
        self.people = {}
        for p in self.view.get("players", []):
            seat = p.get("seat")
            if seat is not None:
                self.people[seat] = {"name": p.get("name", "?"), "alive": bool(p.get("alive")), "role": p.get("role")}
        self.events = self.view.get("log") or []
        self.checks = self.view.get("seerResults") or []
        self.last_guard = self.view.get("lastProtect") or {}
        self.night_votes = self.view.get("wolfVotes") or []
        self.talk = []
        self.votes = []
        self.today = []
        self.exits = []
        self.deaths = []
        self.my_speaking_turns = 0
        in_vote = False
        for e in self.events:
            kind, d = e.get("kind"), e.get("day")
            if kind == "speech":
                self.talk.append((d, e.get("round", 0), e.get("seat"), e.get("text", "")))
                if d == self.day and e.get("seat") == self.me:
                    self.my_speaking_turns += 1
            elif kind == "pass" and d == self.day and e.get("seat") == self.me:
                self.my_speaking_turns += 1
            elif kind == "voting" and d == self.day:
                in_vote = True
                self.today = []
            elif kind == "vote":
                row = (d, e.get("seat"), e.get("target"))
                self.votes.append(row)
                if in_vote and d == self.day:
                    self.today.append((row[1], row[2]))
            elif kind == "dawn":
                self.deaths.append((d, e.get("died"), e.get("role")))
            elif kind == "exile":
                self.exits.append((d, e.get("seat"), e.get("role")))

    def alive(self):
        return sorted(s for s, p in self.people.items() if p["alive"])

    def role_at(self, seat):
        return self.people.get(seat, {}).get("role")

    def label(self, seat):
        p = self.people.get(seat, {})
        return f"seat {seat} ({p.get('name', '?')})"

    def dead_on(self, seat):
        for day, target, _ in self.deaths + self.exits:
            if seat == target:
                return day
        return None


SEER = re.compile(r"\b(?:i am (?:the )?seer|i'm (?:the )?seer|claim(?:ing)? seer|seer here)\b", re.I)
DOCTOR = re.compile(r"\b(?:i am (?:the )?doctor|i'm (?:the )?doctor|claim doctor)\b", re.I)
SCAN = re.compile(r"\b(?:checked|checking|inspected|inspect|scanned|peeked|verified)\b", re.I)
EVIL = re.compile(r"\b(?:werewolves?|wolves?)\b", re.I)
NEGATE = re.compile(r"\b(?:not|never|no)\s+(?:a\s+)?(?:werewolf|wolf)\b|\b(?:isn't|is not)\s+(?:a\s+)?(?:werewolf|wolf)\b", re.I)
GOOD = re.compile(r"\b(?:villager|village|innocent|clean|human|town)\b", re.I)


def parse_statement(table, speaker, text):
    text = " " + (text or "").lower() + " "
    for seat in sorted(table.people, key=lambda x: (-len(table.people[x]["name"]), x)):
        name = table.people[seat]["name"].lower()
        if name:
            text = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", f" seat{seat} ", text)
    is_seer, is_doctor = bool(SEER.search(text)), bool(DOCTOR.search(text))
    claims, opinions = [], []
    for sentence in re.split(r"[.!?;\n]+", text):
        refs = [int(x) for x in re.findall(r"\bseat\s*(\d+)\b", sentence)]
        if not refs:
            continue
        neg = bool(NEGATE.search(sentence))
        says_wolf = bool(EVIL.search(sentence)) and not neg
        says_clear = neg or bool(GOOD.search(sentence))
        if says_wolf == says_clear:
            continue
        bucket = claims if is_seer or SCAN.search(sentence) else opinions
        bucket.extend((s, says_wolf) for s in refs if s in table.people and s != speaker)
    return is_seer, is_doctor, claims, opinions


class Beliefs:
    def __init__(self, table, seed, game):
        self.t, self.seed, self.game = table, seed, game
        self.claimants = {}
        self.checks = {}
        self.opinions = []
        for day, _, speaker, words in table.talk:
            if speaker not in table.people:
                continue
            claimed, _, checks, opinions = parse_statement(table, speaker, words)
            if claimed:
                self.claimants.setdefault(speaker, day)
            if claimed or speaker in self.claimants:
                self.checks.setdefault(speaker, []).extend((target, wolf, day) for target, wolf in checks)
            self.opinions.extend((speaker, target, wolf, day) for target, wolf in opinions)
        self.cred = self._credibility()
        self.suspicion = self._suspicion()

    def _credibility(self):
        trust = {}
        for speaker in sorted(self.claimants):
            known = self.t.role_at(speaker)
            if known is not None:
                trust[speaker] = 1.0 if known == "seer" else 0.0
                continue
            contradicted = any((wolf and self.t.role_at(target) not in (None, "werewolf")) or
                               (not wolf and self.t.role_at(target) == "werewolf")
                               for target, wolf, _ in self.checks.get(speaker, []))
            trust[speaker] = 0.0 if contradicted else (0.85 if len(self.claimants) == 1 else 0.4)
        if self.t.role == "seer":
            for speaker in trust:
                if speaker != self.t.me:
                    trust[speaker] = 0.0
        return trust

    def top_claim(self):
        possible = [s for s, v in self.cred.items() if s != self.t.me and v >= 0.4]
        return max(possible, key=lambda s: (self.cred[s], -s)) if possible else None

    def reported_wolf(self, seat):
        return any(target == seat and wolf and self.cred.get(speaker, 0) > 0
                   for speaker, rows in self.checks.items() for target, wolf, _ in rows)

    def _suspicion(self):
        score = {s: 0.0 for s in self.t.people}
        for speaker, rows in self.checks.items():
            for target, wolf, _ in rows:
                score[target] += self.cred.get(speaker, 0) * (3 if wolf else -2)
        for _, voter, target in self.t.votes:
            if target is None or voter not in score:
                continue
            role = self.t.role_at(target)
            if role == "werewolf":
                score[voter] -= 1.5
            elif role is not None:
                score[voter] += 0.8
            if role is not None and role != "werewolf" and self.t.role_at(voter) == "werewolf":
                score[voter] -= 0.8
        for speaker, target, accused, _ in self.opinions:
            role = self.t.role_at(target)
            if role is None:
                continue
            if accused:
                score[speaker] += -1.0 if role == "werewolf" else 0.6
            else:
                score[speaker] += 1.4 if role == "werewolf" else -0.4
        spoken = {s for _, _, s, _ in self.t.talk}
        if self.t.day >= 2:
            for s in score:
                if s != self.t.me and self.t.people[s]["alive"] and s not in spoken:
                    score[s] += 0.4
        return score

    def choose(self, candidates, inverted=False, purpose="pick"):
        rows = [s for s in candidates if s != self.t.me]
        if not rows:
            return None
        def key(s):
            h = rank_hash(self.seed, self.game, f"{purpose}:{s}")
            return ((-1 if inverted else 1) * self.suspicion.get(s, 0), h, -s)
        return max(rows, key=key)


def action(seed, game, view, decision, pending):
    t = Table(view)
    b = Beliefs(t, seed, game)
    opts = [s for s in (pending.get("options") or []) if s in t.people]
    if decision == "speak":
        line = statement(t, b)
        return {"text": line} if line else {"pass": True}
    if decision == "inspect":
        seen = {x.get("seat") for x in t.checks}
        fresh = [s for s in opts if s not in seen]
        rivals = [s for s in b.claimants if s in fresh]
        return {"target": min(rivals) if rivals else b.choose(fresh, purpose="inspect")}
    if decision == "protect":
        last = t.last_guard.get("seat")
        trusted = b.top_claim()
        if trusted in opts and trusted != last:
            return {"target": trusted}
        pool = [s for s in opts if s != last] or opts
        return {"target": b.choose(pool, inverted=True, purpose="protect")}
    if decision == "kill":
        pool = [s for s in opts if s != t.partner]
        if not pool:
            return {"target": None}
        for v in t.night_votes:
            if v.get("seat") == t.partner and v.get("night") == t.day and v.get("target") in pool:
                return {"target": v["target"]}
        claimants = [s for s in b.claimants if s in pool]
        if claimants:
            return {"target": max(claimants, key=lambda s: (b.cred.get(s, 0), -s))}
        return {"target": b.choose(pool, inverted=True, purpose="kill")}
    if decision == "vote":
        pool = [s for s in opts if s != t.me and (t.role != "werewolf" or s != t.partner)]
        if t.role == "werewolf":
            counts = {}
            for _, target in t.today:
                if target is not None:
                    counts[target] = counts.get(target, 0) + 1
            partner_count = counts.get(t.partner, 0)
            rival_max = max((n for s, n in counts.items() if s != t.partner), default=0)
            claimed_partner = t.partner is not None and b.reported_wolf(t.partner)
            if t.partner in opts and claimed_partner and partner_count >= 2 and partner_count >= rival_max:
                return {"target": t.partner}
            return {"target": b.choose(pool, purpose="wolfvote")}
        sure = [s for s in pool if b.suspicion.get(s, 0) >= 100]
        if sure:
            return {"target": min(sure)}
        if t.role == "seer":
            found = [x.get("seat") for x in t.checks if x.get("werewolf") and x.get("seat") in pool]
            if found:
                return {"target": found[0]}
        claimed = b.top_claim()
        if claimed is not None:
            found = [s for s, wolf, _ in b.checks.get(claimed, []) if wolf and s in pool]
            if found:
                return {"target": found[-1]}
        preferred = b.choose(pool, purpose="vote")
        tally = {}
        for _, target in t.today:
            if target in pool:
                tally[target] = tally.get(target, 0) + 1
        if tally and preferred is not None:
            lead = max(tally, key=lambda s: (tally[s], -s))
            if tally[lead] >= 2 and b.suspicion.get(lead, 0) >= b.suspicion.get(preferred, 0) - 1.5:
                preferred = lead
        return {"target": preferred}
    return {"target": None}


def evidence(t, b, target):
    for _, voter, voted in t.votes:
        if voter == target and voted is not None and t.role_at(voted) not in (None, "werewolf"):
            return f"{t.label(target)} voted out {t.label(voted)}, who was village"
    for speaker, accused, wolf, _ in b.opinions:
        if speaker == target and not wolf and t.role_at(accused) == "werewolf":
            return f"{t.label(target)} defended {t.label(accused)}, a revealed wolf"
    if t.day >= 2 and not any(s == target for _, _, s, _ in t.talk):
        return f"{t.label(target)} has barely spoken"
    return f"{t.label(target)}'s record deserves scrutiny"


def statement(t, b):
    turn = t.my_speaking_turns + 1
    alive_wolf_check = next((x.get("seat") for x in t.checks
                             if x.get("werewolf") and t.people.get(x.get("seat"), {}).get("alive")), None) if t.role == "seer" else None
    rivals = [s for s in b.claimants if s != t.me]
    if t.role == "seer":
        checks = "; ".join(f"night {x.get('night', 0)} I checked {t.label(x.get('seat'))}: {'werewolf' if x.get('werewolf') else 'not a werewolf'}" for x in t.checks)
        if alive_wolf_check is not None:
            tail = f" {t.label(rivals[0])} is falsely claiming seer." if rivals else ""
            return fit(f"I am the seer. My checks: {checks}. Vote {t.label(alive_wolf_check)} today.{tail}")
        if rivals:
            return fit(f"I am the real seer; {t.label(rivals[0])} is lying. My checks: {checks}.")
        if t.day >= 3 and checks:
            return fit(f"I am the seer. My checks so far: {checks}.")
    if t.role == "werewolf":
        accused = b.reported_wolf(t.me) or (t.partner is not None and b.reported_wolf(t.partner))
        bluffers = [s for s in b.claimants if s not in (t.me, t.partner)]
        if accused and bluffers and t.me not in b.claimants:
            target = bluffers[0]
            nights = list(range(1, t.day)) or [1]
            fake = []
            for night in nights:
                if night == nights[-1]:
                    fake.append(f"night {night} I checked {t.label(target)}: werewolf")
                else:
                    choices = [s for s in sorted(t.people) if s not in (t.me, t.partner, target) and
                               (t.dead_on(s) is None or t.dead_on(s) >= night)]
                    if choices:
                        selected = choices[int.from_bytes(rank_hash(b.seed, b.game, f"bluff:{night}")[:4], "big") % len(choices)]
                        fake.append(f"night {night} I checked {t.label(selected)}: not a werewolf")
            return fit(f"I am the seer. My checks: {'; '.join(fake)}. Vote {t.label(target)}.")
    trusted = b.top_claim()
    if trusted is not None:
        targets = [s for s, wolf, _ in b.checks.get(trusted, []) if wolf and t.people.get(s, {}).get("alive")]
        if targets:
            return fit(f"{t.label(trusted)}, our seer, named {t.label(targets[-1])} as a wolf. We should vote there.")
    pool = [s for s in t.alive() if s not in (t.me, t.partner)]
    target = b.choose(pool, purpose="speech")
    if target is None:
        return None
    start = "My main suspicion" if turn <= 1 else "I will vote"
    return fit(f"{start} is {t.label(target)}: {evidence(t, b, target)}.")


def fit(text):
    return re.sub(r"\s+", " ", text).strip()[:MAX_TEXT]


def safe(decision):
    return {"pass": True} if decision == "speak" else {"target": None}


class Runner:
    def __init__(self):
        self.url = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
        self.seed = os.environ.get("MAKEFUN_TRIAL_SEED", "")
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(self, route, body):
        req = urllib.request.Request(self.url + route, data=json.dumps(body, separators=(",", ":")).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with self.http.open(req, timeout=60) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def run(self):
        failed_polls = 0
        while True:
            try:
                status, packet = self.post("/observe", {})
            except Exception:
                failed_polls += 1
                if failed_polls >= 5:
                    return
                time.sleep(1)
                continue
            if status != 200:
                failed_polls += 1
                if failed_polls >= 5:
                    return
                time.sleep(1)
                continue
            failed_polls = 0
            if packet.get("done"):
                return
            for item in packet.get("pending", []):
                game, decision, view = item.get("game"), item.get("decision"), item.get("view")
                if game is None or decision is None or view is None:
                    continue
                try:
                    choice = action(self.seed, game, view, decision, item)
                except Exception:
                    choice = safe(decision)
                body = {"game": game, "decision": decision, **choice}
                try:
                    status, _ = self.post("/act", body)
                    if status != 200:
                        self.post("/act", {"game": game, "decision": decision, **safe(decision)})
                except Exception:
                    pass


def main():
    prompt = os.environ.get("MAKEFUN_PROMPT_FILE", "")
    if prompt:
        try:
            with open(prompt, "r", encoding="utf-8") as stream:
                stream.read()
        except OSError:
            pass
    if os.environ.get("MAKEFUN_ENV_URL"):
        Runner().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
