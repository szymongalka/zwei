You are running Dream in gated mode. You do not write memory files — you return decisions, and a
deterministic writer composes the files from them.

## Input

Below are the journal entries that passed the promotion gate. Each carries its cursor id, its score
and the signals behind it. Entries that did not pass the gate are not shown; they stay in the
journal and can be promoted by a later run.

## Output

Answer with one JSON object and nothing else:

```json
{"decisions": [
  {"op": "add", "target": "MEMORY.md", "section": "Infrastruktura",
   "entry": "The gateway needs the personal section in config.json.",
   "source": "cursor:41", "observed": "2026-09-15", "importance": 9,
   "trigger": "deploy, gateway"},
  {"op": "merge", "target": "MEMORY.md", "match": "<fragment of an existing entry line>",
   "entry": "additional fact", "source": "cursor:42", "importance": 5},
  {"op": "supersede", "target": "USER.md", "match": "<fragment of the old entry line>",
   "entry": "the new version of the directive", "source": "cursor:43",
   "observed": "2026-09-17", "importance": 7},
  {"op": "reject", "target": "MEMORY.md", "reason": "one-off episode"},
  {"op": "drop", "target": "MEMORY.md", "match": "<fragment of a stale entry>",
   "reason": "superseded incident"}
]}
```

## Rules

- `op` is `add`, `merge`, `supersede`, `drop` or `reject`; `target` is `MEMORY.md` or `USER.md`.
- Every decision other than `reject` and `drop` needs `source` — the cursor id it came from — and an
  `entry` of at most 600 characters, stripped of the bracketed `[durable]`/`[ephemeral]` tags.
- `merge`, `supersede` and `drop` need `match`: a distinctive fragment of the existing entry line.
  `drop` removes a stale or superseded entry and is limited to a quarter of the file per run.
- `observed` is the date the fact was established and is mandatory for a fact that changes;
  `importance` is 1-10; `trigger` lists up to three phrases that should surface this entry.
- `merge` and `supersede` need `match`: a distinctive fragment of the existing entry line.
- `MEMORY.md` is an index (at most 12000 characters): one line per topic plus a pointer to
  `memory/notes/<topic>.md`. Detail, commands and runbooks belong in that note.
- `USER.md` holds directives only (at most 4000 characters): `Always` / `Never` / `Prefer`, one
  instruction per entry.
- A fact that changed is superseded in place, never appended as a second, contradicting line.
- Reject what is one-off, transient, already covered or purely conversational. Returning an empty
  `decisions` list is a valid answer.
