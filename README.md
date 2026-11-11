# werewolf-bot

A deterministic, scripted Werewolf agent for make.fun's
[werewolf-agent-01](https://make.fun) competition. It plays one seat in each
of a trial's 30 games of 8-player Werewolf (2 werewolves, 1 seer, 1 doctor,
4 villagers) against seven house players, 15 games as a werewolf and 15 on
the village side, and its score is the mean of its two win rates.

## How it works

`main.py` (Python 3.13, standard library only) is the whole entry. It makes
**no model calls**: every decision is a pure function of the brief, the game
views the env serves, and `MAKEFUN_TRIAL_SEED`, so a trial replays byte for
byte. It polls the env at `MAKEFUN_ENV_URL` (`/observe`, then `/act` for
each decision waiting, one request at a time) and exits when every game is
over.

One inference engine reads the public log the same way whatever seat the
agent holds:

- revealed roles, and votes for or against players whose roles are now known;
- accusations and defences in statements;
- seer claims parsed from statements, with their claimed inspection results
  and a trust level per claimant (a claimant contradicted by a revealed role
  is worth nothing; a lone claimant is believed; a contest is split).

It keeps a suspicion score for every living seat and plays from it:

- **Village side** — follows a trusted seer's named werewolf, otherwise
  votes its most suspicious seat, joining a forming plurality instead of
  splitting the vote.
- **Seer** — inspects unresolved seats (a rival claimant first), reveals a
  found werewolf immediately with every check so far, and counterclaims a
  false seer with its real results.
- **Doctor** — protects the trusted claimed seer first, then the village's
  most trusted seats.
- **Werewolf** — kills the claimed seer first, then the village's most
  trusted seats, never its partner; the second wolf matches the first
  wolf's pick so the kill lands. By day it talks and votes like a villager,
  defends its partner only while that is cheap, buses a partner the seer
  has named once the vote is lost, and counterclaims seer — naming the real
  seer as its checked werewolf — when it or its partner is named.

Statements are one line, under the 500-character limit, and name seats the
way the table does (`seat 4 (Tariq)`), with the evidence drawn from the
public record.

## Layout

- `main.py` — the entry, run by the grader as `python3 -P -s /bot/main.py`.
- `.makefun/owner.json` — the claim file naming the entrant's wallet.
- `LICENSE` — MIT.

## License

MIT — see `LICENSE`.
