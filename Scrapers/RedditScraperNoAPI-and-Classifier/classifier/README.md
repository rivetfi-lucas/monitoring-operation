# `classify_comments.py` — what it does and how

Classifies scraped Reddit comments against the Question Architecture rubric,
using Claude via the Anthropic Message Batches API. Takes a CSV of raw
comments in, produces the same CSV back out with four new columns:
`relevance_tag`, `icp_likelihood`, `confidence`, `rationale`.

## What's new: context-aware classification

The scraper can now attach conversational context to each comment row, and
this script uses it. The relevant columns, if present:

| Column | What it is |
|---|---|
| `thread_body` | The original post's text (not just its title) |
| `parent_body` | The comment this one is directly replying to |
| `context_text` | The top-of-branch comment the whole exchange traces back to, formatted `"author: body"` |
| `root_comment_id` | ID of that top-of-branch comment |
| `ancestor_ids` | Chain of comment IDs from root to immediate parent |
| `depth` | How many replies deep this comment is (0 = top-level) |

**Why this matters:** a lot of real signal is contextual. A bare "sí" or
"depende de tu situación" is meaningless in isolation, but is a genuine
answer once you can see what it's replying to. Before this update, the
script only gave the model a comment's own text plus the thread title —
enough for self-contained comments, not enough for short replies deep in a
conversation.

**All of this is optional.** If your CSV doesn't have these columns (an
older export, or a different source), the script falls back to exactly the
old behavior — thread title only, no per-comment context — without erroring.

### How context gets shown to the model, and why not always all of it

Context is only included when it adds information the model doesn't already
have from something shown a level up:

- **Depth 0** (a top-level comment): no parent, no root — the thread
  title + post body (shown once, above all comments in the batch) is all
  the context that exists. Nothing extra is added per-comment.
- **Depth 1** (a reply to a top-level comment): its parent *is* the thread's
  root comment. Showing both `parent_body` and `context_text` here would be
  the same text twice — only `"Replying to: ..."` is shown.
- **Depth 2+**: by now the root-of-branch comment and the immediate parent
  are genuinely different comments. Both `"Thread root: ..."` and
  `"Replying to: ..."` are shown, since they carry different information —
  the root anchors what the whole sub-conversation is about, the parent is
  specifically what this comment is responding to.

This isn't just a token-cost optimization (though it is one) — showing the
same text under two different labels would actively confuse the "which
comment is this context for" reading, not just waste tokens.

### Length limits on context, not on the comment itself

`thread_body`, `parent_body`, and `context_text` are each capped at
`context_char_limit` characters (config: `classification.context_char_limit`,
default 400) — they're supporting material, not the thing being classified.
The comment's own `body` is never truncated, however long it is.

## The rest of the pipeline (unchanged)

- **Cost efficiency**: comments are grouped by thread into calls of up to
  `max_comments_per_call` (default from config.yaml) so the rubric's token
  cost gets amortized across many comments per call instead of paid on every
  single one. All calls for a run submit as one Batches API job — async
  processing at a 50% discount vs. live sequential calls.
- **Structured output**: results are forced through a tool-use JSON schema
  (`RELEVANCE_TAGS` enum + fixed ICP/confidence values), not parsed from
  free text.
- **Resumable**: batch submissions are checkpointed to
  `state/batch_jobs.json`. If the script gets interrupted mid-poll, rerun
  with `--resume <batch_id>` instead of resubmitting (and double-paying for)
  the same comments.
- **Real cost reporting**: prints actual token usage and dollar cost from
  the batch's own usage data once it completes, not an estimate.

## New: encoding auto-detection

CSV files that pass through Excel or other tools sometimes end up in an
encoding other than UTF-8 — the example file used to build this update was
in Mac OS Roman, not UTF-8, and simple encoding fallback chains don't
actually work reliably here (`cp1252` in particular will "succeed" on
almost any byte sequence, just producing the wrong characters, instead of
raising an error you could catch). The script now reads the raw bytes and
uses `chardet` for real content-based detection when a file isn't UTF-8,
rather than guessing blindly. Install it if you haven't:

```bash
pip install chardet
```

Not a hard requirement — if it's missing, the script falls back to a plain
`cp1252` read with a warning rather than crashing, but accented text may
come out wrong in that fallback case.

## Usage (unchanged)

```bash
python classify_comments.py --input output/reddit_comments_20260718.csv --model sonnet
python classify_comments.py --input output/reddit_comments_20260718.csv --model haiku --sample 500
python classify_comments.py --resume <batch_id>
```

Output: `<input_stem>_classified_<model>.csv` — all original columns
(including the context ones, untouched) plus the four classification
columns.
