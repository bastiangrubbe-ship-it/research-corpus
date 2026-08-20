# Decisions

Newest first. One entry per decision that would be expensive to reverse, or that would
look arbitrary to someone arriving later.

The rejected options are the valuable part. They are what stops the same debate from
being had twice, and what tells a future reader whether the alternative was considered
and dismissed or simply never thought of.

Format:

```
## YYYY-MM-DD — <the decision, in a few words>

**Chose:** what was decided
**Rejected:** the alternatives that were seriously considered
**Why:** the reasoning, including what would have to change for this to be revisited
```

---

## 2026-08-20 — Project layout and data separation

**Chose:** Code in `~/Projects/research-corpus`, data in `~/data/research-corpus`,
located at runtime via `PROJECT_DATA_DIR`.

**Rejected:** A `data/` directory inside the repo. Simpler to set up and one less
environment variable to manage.

**Why:** Data inside a repo does not survive a fresh clone, a `git clean`, or a
reset, and it makes the repo grow without bound. Reading the location from the
environment means the eventual move to a Linux server is a config change rather
than a rewrite — the same code runs against `/srv/data/research-corpus` with no
edits. Revisit only if this project stops having data worth keeping.
