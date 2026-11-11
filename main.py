#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A deterministic, scripted Werewolf agent for make.fun's werewolf-agent-01.

The program plays one seat in each of the trial's 30 games of 8-player
Werewolf (2 werewolves, 1 seer, 1 doctor, 4 villagers) through the env at
MAKEFUN_ENV_URL. It makes no model calls: every decision is a pure function
of the brief, the views the env serves, and MAKEFUN_TRIAL_SEED, so a trial
replays byte for byte. No wall clock and no unseeded randomness ever reach
a request; the seeded hash below is the only source of variation, and it is
keyed by the trial seed and the game number alone.

Strategy, in short:

* One inference engine reads the public log the same way whatever seat the
  agent holds: revealed roles, votes for and against players whose roles
  are now known, accusations and defences in statements, and seer claims
  (parsed from statements, with their claimed inspection results). It keeps
  a suspicion score per living seat and a trust level per seer claimant.
* On the village side it follows a trusted seer's named werewolf, votes
  its most suspicious seat (joining a forming plurality instead of
  splitting the vote), protects a claimed seer as the doctor, and as the
  seer inspects unresolved seats and reveals a found werewolf at once,
  with every result, so the village can act on it.
* As a werewolf it kills the claimed seer first, then the village's most
  trusted seats, never its partner; it talks and votes like a villager,
  defends its partner only while that is cheap, buses a partner the seer
  has named once the vote is lost, and counterclaims seer when it or its
  partner is named by the real seer.
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

MAX_CHARS = 500
SAFE_CHARS = 480


# --------------------------------------------------------------------------
# Deterministic hashing: the only "randomness", keyed by seed and game.
# --------------------------------------------------------------------------

def keyed(*parts):
    raw = "||".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")


class Agent:
    def __init__(self, env_url, trial_seed):
        self.env_url = env_url.rstrip("/")
        self.seed = trial_seed

    # ------------------------------------------------------------------
    # Env transport: one request at a time, in the order they are made.
    # ------------------------------------------------------------------

    def post(self, path, body):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.env_url + path,
            data=data,
            method="POST",
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def run(self):
        failures = 0
        while True:
            try:
                status, obs = self.post("/observe", {})
            except Exception:
                failures += 1
                if failures > 120:
                    return
                time.sleep(2)
                continue
            failures = 0
            if status != 200:
                time.sleep(1)
                continue
            if obs.get("done"):
                return
            for pending in obs.get("pending", []):
                self.answer(pending)

    def answer(self, pending):
        game = pending.get("game")
        kind = pending.get("decision")
        view = pending.get("view")
        if game is None or kind is None or view is None:
            return
        try:
            action = decide(self.seed, game, view, kind, pending)
        except Exception:
            action = fallback_action(kind)
        body = {"game": game, "decision": kind}
        body.update(action)
        try:
            status, _ = self.post("/act", body)
        except Exception:
            return
        if status != 200:
            # A refusal changes nothing: answer with the safe fallback once.
            body = {"game": game, "decision": kind}
            body.update(fallback_action(kind))
            try:
                self.post("/act", body)
            except Exception:
                pass


def fallback_action(kind):
    if kind == "speak":
        return {"pass": True}
    return {"target": None}


# --------------------------------------------------------------------------
# View model: everything derived from one game's view alone.
# --------------------------------------------------------------------------

class Game:
    def __init__(self, view):
        self.view = view
        self.day = view.get("day", 1)
        self.phase = view.get("phase", "night")
        you = view.get("you", {})
        self.me = you.get("seat")
        self.my_role = you.get("role")
        partner = you.get("partner") or {}
        self.partner = partner.get("seat")
        self.players = {}
        for p in view.get("players", []):
            self.players[p["seat"]] = {
                "name": p.get("name", "?"),
                "alive": bool(p.get("alive")),
                "role": p.get("role"),
            }
        self.view_seed = ""
        self.log = view.get("log", []) or []
        self.seer_results = view.get("seerResults")  # mine, when I am the seer
        self.last_protect = view.get("lastProtect")  # mine, when I am the doctor
        self.wolf_votes = view.get("wolfVotes")      # mine, when I am a wolf

        self.speeches = []       # (day, round, seat, text)
        self.votes = []          # (day, seat, target)
        self.dawns = []          # (day, died seat or None, role)
        self.exiles = []         # (day, seat, role)
        self.my_turns_today = 0
        self.votes_today = []    # votes cast so far in today's vote, in order
        seen_voting = False
        for e in self.log:
            kind = e.get("kind")
            if kind == "speech":
                self.speeches.append((e.get("day"), e.get("round"), e.get("seat"), e.get("text", "")))
                if e.get("seat") == self.me and e.get("day") == self.day:
                    self.my_turns_today += 1
            elif kind == "pass":
                if e.get("seat") == self.me and e.get("day") == self.day:
                    self.my_turns_today += 1
            elif kind == "vote":
                self.votes.append((e.get("day"), e.get("seat"), e.get("target")))
                if seen_voting and e.get("day") == self.day:
                    self.votes_today.append((e.get("seat"), e.get("target")))
            elif kind == "voting":
                if e.get("day") == self.day:
                    seen_voting = True
                    self.votes_today = []
            elif kind == "dawn":
                self.dawns.append((e.get("day"), e.get("died"), e.get("role")))
            elif kind == "exile":
                self.exiles.append((e.get("day"), e.get("seat"), e.get("role")))

    def name(self, seat):
        p = self.players.get(seat)
        return p["name"] if p else "?"

    def who(self, seat):
        return "seat %d (%s)" % (seat, self.name(seat))

    def alive_seats(self):
        return sorted(s for s, p in self.players.items() if p["alive"])

    def revealed_role(self, seat):
        p = self.players.get(seat)
        return p["role"] if p else None

    def death_day(self, seat):
        """The day on which a seat died: a dawn death on day d, an exile on
        day d. None while alive or unknown."""
        for (d, died, _r) in self.dawns:
            if died == seat:
                return d
        for (d, s, _r) in self.exiles:
            if s == seat:
                return d
        return None


# --------------------------------------------------------------------------
# Statement parsing: seer claims, claimed results, accusations, defences.
# --------------------------------------------------------------------------

SEER_CLAIM_RES = [
    r"\bi am the seer\b", r"\bi'm the seer\b", r"\bi am a seer\b",
    r"\bi claim seer\b", r"\bclaiming seer\b", r"\bi am seer\b",
    r"\bthe seer here\b", r"\bi'?m seer\b",
]
DOC_CLAIM_RES = [
    r"\bi am the doctor\b", r"\bi'm the doctor\b", r"\bi claim doctor\b",
    r"\bi am doctor\b",
]
CHECK_VERB_RE = r"\b(checked|checking|inspect\w*|scanned|peeked|verified)\b"
WOLF_NEG_RE = (r"\b(not|never|no)\s+(a\s+)?(werewolf|wolf)\b"
               r"|\b(isn'?t|is\s+not)\s+(a\s+)?(werewolf|wolf)\b")
WOLF_POS_RE = r"\b(werewolf|werewolves|wolf|wolves)\b"
CLEAR_POS_RE = (r"\b(villager|village|innocent|clean|human|town)\b"
                r"|\bnot\s+(a\s+)?(werewolf|wolf)\b")


def normalize(text, game):
    t = " " + text.lower() + " "
    # Longest names first so a short name cannot eat a longer one.
    for seat in sorted(game.players, key=lambda s: -len(game.players[s]["name"])):
        name = game.players[seat]["name"].lower()
        t = re.sub(r"\b" + re.escape(name) + r"\b", " seat%d " % seat, t)
    return t


def clauses(text):
    return [c for c in re.split(r"[.!?;\n]", text) if c.strip()]


def parse_speech(game, seat, text):
    """Return (claims_seer, claims_doctor, results, opinions) for one speech.
    results/opinions: lists of (seat, is_wolf). Results are assertions by a
    seer claimant (or carrying a check verb); opinions are everyone else's."""
    t = normalize(text, game)
    claims_seer = any(re.search(r, t) for r in SEER_CLAIM_RES)
    claims_doc = any(re.search(r, t) for r in DOC_CLAIM_RES)
    results, opinions = [], []
    for clause in clauses(t):
        seats = [int(x) for x in re.findall(r"seat\s*(\d)\b", clause)]
        if not seats:
            continue
        neg = re.search(WOLF_NEG_RE, clause)
        wolf = re.search(WOLF_POS_RE, clause) and not neg
        clear = bool(neg) or re.search(CLEAR_POS_RE, clause)
        if wolf and not clear:
            verdict = True
        elif clear and not wolf:
            verdict = False
        else:
            continue
        bucket = results if (claims_seer or re.search(CHECK_VERB_RE, clause)) else opinions
        for s in seats:
            if s != seat:
                bucket.append((s, verdict))
    return claims_seer, claims_doc, results, opinions


class Intel:
    """The public record read one way, whatever seat the agent holds."""

    def __init__(self, game):
        self.game = game
        self.seer_claimants = {}    # seat -> first day claimed
        self.doc_claimants = set()
        self.claim_results = {}     # claimant -> [(seat, is_wolf, day)]
        self.opinions = []          # (speaker, seat, is_wolf, day)
        for (day, _rnd, seat, text) in game.speeches:
            cs, cd, results, opinions = parse_speech(game, seat, text)
            if cs and seat not in self.seer_claimants:
                self.seer_claimants[seat] = day
            if cd:
                self.doc_claimants.add(seat)
            if cs or seat in self.seer_claimants:
                self.claim_results.setdefault(seat, []).extend(
                    (s, w, day) for (s, w) in results)
            for (s, w) in opinions:
                self.opinions.append((seat, s, w, day))
        self.trust = self._trust_levels()
        self.suspicion = self._suspicion()

    # -- seer-claim trust ------------------------------------------------

    def _claim_contradicted(self, claimant):
        """A claimant is contradicted when a claimed result clashes with a
        role since revealed by a death."""
        g = self.game
        for (s, w, _d) in self.claim_results.get(claimant, []):
            role = g.revealed_role(s)
            if role is None:
                continue
            if w and role != "werewolf":
                return True
            if not w and role == "werewolf":
                return True
        return False

    def _trust_levels(self):
        """claimant -> weight in [0, 1] to give their claimed results."""
        g = self.game
        out = {}
        claimants = sorted(self.seer_claimants)
        for c in claimants:
            role = g.revealed_role(c)
            if role == "seer":
                out[c] = 1.0
            elif role is not None:
                out[c] = 0.0            # a revealed non-seer who claimed seer
            elif self._claim_contradicted(c):
                out[c] = 0.0
            elif len(claimants) == 1:
                out[c] = 0.85
            else:
                out[c] = 0.4            # contested: partial credit
        # When I am the seer, every other claimant is lying.
        if g.my_role == "seer":
            for c in claimants:
                if c != g.me:
                    out[c] = 0.0
        return out

    def trusted_seer(self):
        """The claimant (not me) whose results carry real weight, if any."""
        best, best_w = None, 0.0
        for c in sorted(self.trust):
            if c != self.game.me and self.trust[c] > best_w:
                best, best_w = c, self.trust[c]
        return best if best_w >= 0.4 else None

    def claimed(self, seat, by=None):
        """The strongest claim verdict on a seat: +1 wolf, -1 clear, 0 none,
        weighted by claimant trust."""
        score = 0.0
        for c, results in self.claim_results.items():
            if by is not None and c != by:
                continue
            w = self.trust.get(c, 0.0)
            if w <= 0:
                continue
            for (s, is_wolf, _d) in results:
                if s == seat:
                    score += w if is_wolf else -w
        return score

    # -- suspicion ---------------------------------------------------------

    def _suspicion(self):
        g = self.game
        sus = {s: 0.0 for s in g.players}
        # Claimed seer results, weighted by trust.
        for c, results in self.claim_results.items():
            w = self.trust.get(c, 0.0)
            for (s, is_wolf, _d) in results:
                if w >= 0.8:
                    sus[s] += 100.0 if is_wolf else -100.0
                elif w > 0:
                    sus[s] += 12.0 * w if is_wolf else -12.0 * w
                elif self.seer_claimants.get(c) is not None and g.revealed_role(c) == "werewolf":
                    # A revealed wolf's claims point the other way.
                    sus[s] += -1.0 if is_wolf else 0.8
        # Votes against players whose roles are now known.
        for (_d, voter, target) in g.votes:
            if target is None or voter not in sus:
                continue
            role = g.revealed_role(target)
            if role == "werewolf":
                sus[voter] -= 2.0
            elif role is not None:
                sus[voter] += 2.0
        # Opinions about players whose roles are now known.
        for (speaker, s, is_wolf, _d) in self.opinions:
            role = g.revealed_role(s)
            if role is None or speaker not in sus:
                continue
            if g.revealed_role(speaker) == "werewolf":
                sus[s] += -0.8 if is_wolf else 1.2
            elif is_wolf and role == "werewolf":
                sus[speaker] -= 1.0
            elif is_wolf:
                sus[speaker] += 0.8
            elif role == "werewolf":
                sus[speaker] += 1.5     # defended a werewolf
            else:
                sus[speaker] -= 0.5
        # A dawn victim's accusations carry a little weight.
        for (_d, died, _r) in g.dawns:
            if died is None:
                continue
            for (speaker, s, is_wolf, _dd) in self.opinions:
                if speaker == died and is_wolf and s in sus:
                    sus[s] += 0.4
        # The near-silent drift suspiciously.
        if g.day >= 2:
            spoke = {seat for (_d, _r, seat, _t) in g.speeches}
            for s in sus:
                if s != g.me and s not in spoke and g.players[s]["alive"]:
                    sus[s] += 0.5
        return sus

    def top_suspect(self, candidates, exclude=()):
        g = self.game
        best, best_key = None, None
        for s in sorted(candidates):
            if s in exclude or s == g.me:
                continue
            key = (self.suspicion.get(s, 0.0), -keyed(g.view_seed, s))
            if best_key is None or key > best_key:
                best, best_key = s, key
        return best


# --------------------------------------------------------------------------
# Decisions.
# --------------------------------------------------------------------------

def decide(seed, game_no, view, kind, pending):
    g = Game(view)
    g.view_seed = "%s|%d" % (seed, game_no)
    intel = Intel(g)
    options = pending.get("options") or []
    if kind == "kill":
        return {"target": wolf_kill(g, intel, options)}
    if kind == "protect":
        return {"target": doctor_protect(g, intel, options)}
    if kind == "inspect":
        return {"target": seer_inspect(g, intel, options)}
    if kind == "vote":
        if g.my_role == "werewolf":
            return {"target": wolf_vote(g, intel, options)}
        return {"target": village_vote(g, intel, options)}
    if kind == "speak":
        text = speak(g, intel)
        if text is None:
            return {"pass": True}
        return {"text": text}
    return fallback_action(kind)


def pick_by_score(g, intel, candidates, scores=None):
    """Highest suspicion wins; the seeded hash breaks ties, deterministically."""
    best, best_key = None, None
    for s in sorted(candidates):
        v = (scores or intel.suspicion).get(s, 0.0)
        key = (v, -keyed(g.view_seed, "tie", s))
        if best_key is None or key > best_key:
            best, best_key = s, key
    return best


# -- night: the wolf's kill -------------------------------------------------

def wolf_kill(g, intel, options):
    opts = [s for s in options if s != g.partner]
    if not opts:
        return None
    # Second wolf: match the partner's pick and the kill is certain.
    if g.wolf_votes:
        for v in g.wolf_votes:
            if v.get("seat") == g.partner and v.get("night") == g.day and v.get("target") in opts:
                return v["target"]
    # The claimed seer dies first, above all when trusted.
    claimants = [c for c in sorted(intel.seer_claimants)
                 if c in opts and c != g.me]
    if claimants:
        return max(claimants, key=lambda c: intel.trust.get(c, 0.0))
    # Then the village's most trusted living seats (its clears and leaders),
    # never the seats the village itself suspects: those are tomorrow's exile.
    worst = pick_by_score(g, intel, opts,
                          scores={s: -intel.suspicion.get(s, 0.0) for s in opts})
    return worst


# -- night: the doctor's protection ------------------------------------------

def doctor_protect(g, intel, options):
    if not options:
        return None
    last = (g.last_protect or {}).get("seat")
    seer = intel.trusted_seer()
    if seer in options and seer != last:
        return seer
    # Protect the most trusted living seat the village has.
    return pick_by_score(g, intel, options,
                         scores={s: -intel.suspicion.get(s, 0.0) for s in options})


# -- night: the seer's inspection ---------------------------------------------

def seer_inspect(g, intel, options):
    if not options:
        return None
    known = {r["seat"] for r in (g.seer_results or [])}
    fresh = [s for s in options if s not in known]
    if not fresh:
        return None
    # A rival claimant first: learning whether they are a wolf settles it.
    rivals = [c for c in sorted(intel.seer_claimants) if c in fresh and c != g.me]
    if rivals:
        return rivals[0]
    # Then the living seat suspicion leans on hardest but has not resolved.
    return pick_by_score(g, intel, fresh)


# -- day: the village's vote ---------------------------------------------------

def village_vote(g, intel, options):
    opts = [s for s in options if s != g.me]
    if not opts:
        return None
    # A certainly known werewolf is always the vote.
    if g.my_role == "seer":
        for r in (g.seer_results or []):
            if r.get("werewolf") and r["seat"] in opts:
                return r["seat"]
    certain = [s for s in opts if intel.suspicion.get(s, 0.0) >= 100.0]
    if certain:
        return certain[0]
    pool = [s for s in opts if intel.suspicion.get(s, 0.0) > -100.0] or opts
    top = pick_by_score(g, intel, pool)
    # Join a forming plurality on a plausible seat rather than split.
    tally = {}
    for (_voter, target) in g.votes_today:
        if target in pool:
            tally[target] = tally.get(target, 0) + 1
    for s in sorted(tally, key=lambda s: -tally[s]):
        if tally[s] >= 2 and intel.suspicion.get(s, 0.0) >= intel.suspicion.get(top, 0.0) - 1.5:
            return s
    return top


# -- day: the wolf's vote --------------------------------------------------------

def wolf_vote(g, intel, options):
    opts = [s for s in options if s not in (g.me, g.partner)]
    if not opts:
        return None
    seer = intel.trusted_seer()
    # The real seer is the vote when it is not me.
    if seer in opts:
        return seer
    # Bus the partner only when the table has already decided on them.
    if g.partner in options:
        against = sum(1 for (_v, t) in g.votes_today if t == g.partner)
        doomed = intel.claimed(g.partner) >= 0.8 or against >= 2
        if doomed:
            return g.partner
    # Otherwise exile the village's own top suspect among non-wolves.
    return pick_by_score(g, intel, opts)


# --------------------------------------------------------------------------
# Statements.
# --------------------------------------------------------------------------

def fold(text):
    text = re.sub(r"\s+", " ", text).strip()
    return text[:SAFE_CHARS]


def evidence_for(g, intel, target):
    """One short, true reason the record gives against a seat."""
    for (_d, voter, t) in g.votes:
        if voter == target and t is not None and g.revealed_role(t) not in (None, "werewolf"):
            return "%s voted to exile %s, who was village" % (g.who(target), g.who(t))
    for (speaker, s, is_wolf, _d) in intel.opinions:
        if speaker == target and not is_wolf and g.revealed_role(s) == "werewolf":
            return "%s defended %s, who was a werewolf" % (g.who(target), g.who(s))
    for (_d, voter, t) in g.votes:
        if voter == target and t is not None and g.revealed_role(t) is not None and g.revealed_role(t) != "werewolf":
            return "%s pushed on %s, a villager" % (g.who(target), g.who(t))
    spoke = {seat for (_d, _r, seat, _t) in g.speeches}
    if target not in spoke and g.day >= 2:
        return "%s has said almost nothing all game" % g.who(target)
    return "%s's votes and words do not line up with the village" % g.who(target)


def my_results_text(g):
    parts = []
    for r in (g.seer_results or []):
        parts.append("night %d I checked %s: %s" % (
            r["night"], g.who(r["seat"]),
            "werewolf" if r.get("werewolf") else "not a werewolf"))
    return "; ".join(parts)


def found_wolf(g):
    for r in (g.seer_results or []):
        if r.get("werewolf") and g.players.get(r["seat"], {}).get("alive"):
            return r["seat"]
    return None


def speak(g, intel):
    rnd = g.my_turns_today + 1
    if g.my_role == "werewolf":
        return wolf_speak(g, intel, rnd)
    if g.my_role == "seer":
        return seer_speak(g, intel, rnd)
    return village_speak(g, intel, rnd)


def seer_speak(g, intel, rnd):
    wolf = found_wolf(g)
    rivals = [c for c in sorted(intel.seer_claimants) if c != g.me]
    results = my_results_text(g)
    if wolf is not None:
        text = "I am the seer. My checks: %s. Vote %s today." % (results, g.who(wolf))
        if rivals:
            text += " %s's seer claim is false; I am the real seer." % g.who(rivals[0])
        return fold(text)
    if rivals:
        return fold("I am the real seer, and %s's claim is false. My checks so far: %s. "
                    "None is a werewolf yet; I will keep checking." % (g.who(rivals[0]), results))
    # Stay hidden while the checks are all clear, unless the game is late or
    # the village is about to waste a day on me.
    accusations = sum(1 for (_sp, s, w, _d) in intel.opinions if s == g.me and w)
    if g.day >= 3 or accusations >= 2:
        if results:
            return fold("I am the seer, and it is time you knew my checks: %s. "
                        "Those seats are village; our werewolves hide among the rest." % results)
    return village_speak(g, intel, rnd, as_seer=True)


def village_speak(g, intel, rnd, as_seer=False):
    seer = intel.trusted_seer()
    if seer is not None:
        # Echo a trusted seer's named werewolf, or back their read.
        named = None
        for (s, w, _d) in intel.claim_results.get(seer, []):
            if w and g.players.get(s, {}).get("alive"):
                named = s
        if named is not None:
            return fold("%s, our seer, has named %s as a werewolf. That is the strongest "
                        "lead we have: vote %s today." % (g.who(seer), g.who(named), g.who(named)))
    target = intel.top_suspect(g.alive_seats())
    if target is None:
        return None
    if rnd <= 1:
        return fold("Reading the record, my main suspicion is %s: %s. "
                    "I want to hear them answer that before we vote."
                    % (g.who(target), evidence_for(g, intel, target)))
    return fold("We should not split the vote. %s is my exile today: %s. Join that vote."
                % (g.who(target), evidence_for(g, intel, target)))


def wolf_speak(g, intel, rnd):
    me_named = intel.claimed(g.me) >= 0.4
    partner_named = g.partner is not None and intel.claimed(g.partner) >= 0.4
    claimants = [c for c in sorted(intel.seer_claimants)
                 if c != g.me and c != g.partner]
    i_claimed = g.me in intel.seer_claimants
    if (me_named or partner_named) and claimants and not i_claimed:
        # Counterclaim: name the real seer as my checked werewolf.
        c = claimants[0]
        fake = fake_results(g, c)
        return fold("I am the real seer, and %s is lying. My checks: %s. "
                    "Vote %s today." % (g.who(c), fake, g.who(c)))
    if i_claimed and claimants:
        c = claimants[0]
        return fold("My checks stand: %s is a werewolf, and their counter-claim "
                    "changes nothing. Vote %s." % (g.who(c), g.who(c)))
    # Otherwise talk like a villager, aimed at a non-wolf.
    pool = [s for s in g.alive_seats() if s not in (g.me, g.partner)]
    target = intel.top_suspect(pool)
    if target is None:
        return None
    if target == g.partner:
        target = intel.top_suspect(pool, exclude=(g.partner,))
    if target is None:
        return None
    if rnd <= 1:
        return fold("I have been tracking the votes. My suspicion is %s: %s. "
                    "What do others make of that?"
                    % (g.who(target), evidence_for(g, intel, target)))
    return fold("One exile, together: mine is %s. %s."
                % (g.who(target), evidence_for(g, intel, target)))


def fake_results(g, seer_claimant):
    """Fabricated checks for a counterclaim: the real seer is my 'werewolf',
    earlier nights cleared seats that were alive then (never my partner)."""
    parts = []
    nights = list(range(1, g.day))
    for n in nights:
        if n == nights[-1]:
            parts.append("night %d I checked %s: werewolf" % (n, g.who(seer_claimant)))
        else:
            alive_then = [s for s in sorted(g.players)
                          if s not in (g.me, g.partner, seer_claimant)
                          and (g.death_day(s) is None or g.death_day(s) >= n)]
            if alive_then:
                pick = alive_then[keyed(g.view_seed, "fake", n) % len(alive_then)]
                parts.append("night %d I checked %s: not a werewolf" % (n, g.who(pick)))
    return "; ".join(parts) if parts else "night 1 I checked %s: werewolf" % g.who(seer_claimant)


# --------------------------------------------------------------------------
# Entry point.
# --------------------------------------------------------------------------

def main():
    # The brief is the one file the trial gives the agent to read.
    prompt_file = os.environ.get("MAKEFUN_PROMPT_FILE", "")
    if prompt_file:
        try:
            with open(prompt_file, "r", encoding="utf-8") as f:
                f.read()
        except OSError:
            pass
    env_url = os.environ.get("MAKEFUN_ENV_URL", "")
    seed = os.environ.get("MAKEFUN_TRIAL_SEED", "")
    if not env_url:
        return 0
    Agent(env_url, seed).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
