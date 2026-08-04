"""
Classifies scraped Reddit comments against the Question Architecture rubric,
using the Anthropic Message Batches API for cost efficiency at scale.

Context-aware classification:
  - Each comment's row can carry conversational context columns (added
    upstream by the scraper): thread_body (the original post's text, not
    just its title), parent_body (the comment it's directly replying to),
    context_text (the top-of-branch comment it all traces back to, as
    "author: body"), root_comment_id, and ancestor_ids. All of these are
    OPTIONAL — if a CSV doesn't have them (older exports), the script falls
    back to title-only context exactly like before, so this is backward
    compatible with older input files.
  - Context is included only when it adds information: a depth-0 comment has
    no parent/root beyond the thread itself, so nothing extra is shown for
    it. A depth-1 reply's parent IS the thread's root comment, so showing
    both parent_body and context_text would just be the same text twice —
    only one is shown. Depth-2+ comments get both, since by then they
    genuinely differ (root-of-branch topic vs. immediate reply target).
  - Context fields are length-capped (config: context_char_limit, default
    400) since they're supporting material, not the text being classified —
    the comment's own body is never truncated.

Cost-efficiency design:
  - Comments are grouped by thread into calls of up to `max_comments_per_call`
    (config.yaml). The rubric (system prompt) is large and otherwise gets billed
    on every single call — grouping amortizes that cost across ~40 comments
    instead of paying it per comment.
  - All calls for a run are submitted as ONE Batches API job, which is
    processed asynchronously at a 50% cost discount vs. live/sequential calls.
  - Output is forced into a structured JSON schema via tool-use, so there's no
    brittle free-text parsing.
  - Batch submissions are checkpointed to state/batch_jobs.json, so an
    interrupted run can be resumed with --resume instead of resubmitting
    (and double-paying for) the same comments.

Usage:
    # Submit a new classification run
    python classify_comments.py --input output/reddit_comments_20260718.csv --model sonnet
    python classify_comments.py --input output/reddit_comments_20260718.csv --model haiku

    # Calibration: sample N comments and run through both models for comparison
    python classify_comments.py --input output/reddit_comments_20260718.csv --model sonnet --sample 500
    python classify_comments.py --input output/reddit_comments_20260718.csv --model haiku --sample 500
    # (use the same --sample N with the same --seed so both runs see the identical subset)

    # Resume polling/fetching an already-submitted batch (e.g. script was interrupted)
    python classify_comments.py --resume <batch_id>

Requires:
    ANTHROPIC_API_KEY environment variable (or a .env file, see .env.example)

Output:
    <input_stem>_classified_<model>.csv   — original columns + relevance_tag,
                                             icp_likelihood, confidence, rationale
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import yaml

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import anthropic
except ImportError:
    print("ERROR: pip install anthropic (see requirements.txt)")
    sys.exit(1)

STATE_DIR = os.path.join(os.path.dirname(__file__), "state")
BATCH_JOBS_PATH = os.path.join(STATE_DIR, "batch_jobs.json")

MODEL_ALIASES = {
    "sonnet": "model_sonnet",
    "haiku": "model_haiku",
}

# One tag per Question Architecture strategic question (see rubric.md), plus "none".
# Kept in sync manually with rubric.md's Tag column — if you edit one, edit the other.
RELEVANCE_TAGS = [
    "own-language", "existing-workaround", "trust-objection", "trigger-moment",
    "usdt-mep-fluency", "payroll-sentiment",
    "parent-dependence", "stress-humor", "recurring-small-costs",
    "stipend-vs-inflation",
    "ambition-plan", "leaving-gig-work", "skill-investment",
    "working-capital-gap", "afip-compliance", "client-payment-timing",
    "none",
]

# Batch API pricing, per million tokens (USD). Source: platform.claude.com/docs/en/about-claude/pricing
# Sonnet 5 rate shown is introductory pricing (through Aug 31, 2026) — check the docs page if running
# this after that date, since it steps up to $1.50 / $7.50 batch afterward.
BATCH_PRICING_PER_MTOK = {
    "claude-sonnet-5": {"input": 1.00, "output": 5.00},
    "claude-haiku-4-5-20251001": {"input": 0.50, "output": 2.50},
}

TOOL_SCHEMA = {
    "name": "classify_comments",
    "description": "Classify a batch of Reddit comments against the Question Architecture rubric.",
    "input_schema": {
        "type": "object",
        "properties": {
            "classifications": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "comment_id": {"type": "string"},
                        "relevance_tag": {
                            "type": "string",
                            "enum": RELEVANCE_TAGS,
                            "description": "Which Question Architecture strategic question this comment answers (see the rubric for what each tag means), or 'none' if it doesn't answer any of them.",
                        },
                        "icp_likelihood": {
                            "type": "string",
                            "enum": ["1", "2A", "2B", "3", "4", "none"],
                            "description": "Which ICP this commenter most likely belongs to, or 'none'.",
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                        },
                        "rationale": {
                            "type": "string",
                            "description": "One-line rationale for the tags above, under 25 words.",
                        },
                    },
                    "required": ["comment_id", "relevance_tag", "icp_likelihood", "confidence", "rationale"],
                },
            }
        },
        "required": ["classifications"],
    },
}


# ---------- config / state helpers ----------

def load_config(path="config.yaml"):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_rubric(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def read_csv_robust(path, **kwargs):
    """Reads a CSV, auto-detecting encoding when it isn't UTF-8. Scraper
    output is UTF-8, but files that pass through Excel/other tools sometimes
    end up in cp1252 or Mac OS Roman instead.

    Doesn't just try encodings in a fixed order and catch exceptions: cp1252
    is a near-total mapping over all byte values, so it "succeeds" even on
    the wrong encoding, just producing wrong characters (mangled accented
    Spanish) instead of raising an error to catch. Real content-based
    detection (chardet) is needed to actually pick the right one.

    keep_default_na=False is set unless overridden: without it, pandas turns
    empty cells into float NaN even with dtype=str, which crashes the
    string-handling helpers below on legitimately-empty context columns."""
    kwargs.setdefault("keep_default_na", False)

    try:
        return pd.read_csv(path, encoding="utf-8", **kwargs)
    except UnicodeDecodeError:
        pass

    try:
        import chardet
    except ImportError:
        print("  [warn] non-UTF-8 file and chardet isn't installed (pip install chardet) — trying cp1252 blind.")
        return pd.read_csv(path, encoding="cp1252", **kwargs)

    with open(path, "rb") as f:
        raw = f.read()
    detected = chardet.detect(raw)
    encoding = detected.get("encoding") or "cp1252"
    print(f"  [info] {path} isn't UTF-8 — detected {encoding} (confidence {detected.get('confidence', 0):.2f})")
    return pd.read_csv(path, encoding=encoding, **kwargs)


def load_batch_jobs():
    if not os.path.exists(BATCH_JOBS_PATH):
        return {}
    with open(BATCH_JOBS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_batch_job(batch_id, record):
    os.makedirs(STATE_DIR, exist_ok=True)
    jobs = load_batch_jobs()
    jobs[batch_id] = record
    with open(BATCH_JOBS_PATH, "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2)


# ---------- prompt building ----------

def build_system_prompt(rubric_text):
    return f"""You are classifying Reddit comments for Rivet FI's market research operation.

Below is the Question Architecture — the rubric that defines what counts as a
relevant signal, and how to identify which Ideal Customer Profile (ICP) a
commenter likely belongs to.

{rubric_text}

Each comment may come with conversational context: the original post's title
and body, and — for replies — the comment it's directly responding to
("Replying to") and/or the top-of-branch comment the whole exchange traces
back to ("Thread root"). Use this context to correctly read comments whose
meaning depends on it (a bare "sí" or "depende de tu situación" means nothing
alone, but is a real signal once you can see what it's answering). Classify
each comment individually — context informs the reading, but every comment
still gets its own relevance_tag / icp_likelihood / confidence / rationale.

For every comment given to you, return:
- relevance_tag: which rubric question/tag it answers, or "none"
- icp_likelihood: "1", "2A", "2B", "3", "4", or "none"
- confidence: "high", "medium", or "low" — how confident you are in the tags above
- rationale: one line, under 25 words, explaining the call

Call the classify_comments tool exactly once with all comments in this batch."""


def _truncate(text, limit):
    text = (text or "").strip()
    if not text or not limit:
        return text
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def build_user_content(thread_title, thread_body, comments_chunk, context_char_limit=400):
    lines = [f"Thread: {thread_title}"]
    thread_body = _truncate(thread_body, context_char_limit)
    if thread_body:
        lines.append(f"Post body: {thread_body}")
    lines.append("")
    lines.append("Comments:")

    for row in comments_chunk:
        depth = int(row.get("depth", 0) or 0)
        parent_body = _truncate(row.get("parent_body", ""), context_char_limit)
        context_text = _truncate(row.get("context_text", ""), context_char_limit)

        context_lines = []
        if depth >= 2 and context_text:
            context_lines.append(f"    Thread root: {context_text}")
        if depth >= 1 and parent_body:
            context_lines.append(f"    Replying to: {parent_body}")

        lines.append(f"[{row['comment_id']}] (depth {depth})")
        lines.extend(context_lines)
        lines.append(f"    Comment: {row['body']}")

    return "\n".join(lines)


# ---------- chunking ----------

def chunk_by_thread(df, max_per_call):
    """Groups rows by thread_id (preserving first-seen order), then splits each
    thread's comments into chunks of at most max_per_call."""
    chunks = []  # list of (thread_id, thread_title, thread_body, [row_dicts])
    has_thread_body = "thread_body" in df.columns
    for thread_id, group in df.groupby("thread_id", sort=False):
        thread_title = group.iloc[0].get("thread_title", "")
        thread_body = group.iloc[0].get("thread_body", "") if has_thread_body else ""
        rows = group.to_dict("records")
        for i in range(0, len(rows), max_per_call):
            chunks.append((thread_id, thread_title, thread_body, rows[i:i + max_per_call]))
    return chunks


# ---------- batch submission ----------

def submit_batch(client, chunks, model, system_prompt, max_tokens, context_char_limit=400):
    requests = []
    manifest = []  # custom_id -> comment_ids in that chunk, for verifying completeness
    for idx, (thread_id, thread_title, thread_body, rows) in enumerate(chunks):
        custom_id = f"chunk_{idx}_{thread_id}"
        user_content = build_user_content(thread_title, thread_body, rows, context_char_limit)
        requests.append({
            "custom_id": custom_id,
            "params": {
                "model": model,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_content}],
                "tools": [TOOL_SCHEMA],
                "tool_choice": {"type": "tool", "name": "classify_comments"},
            },
        })
        manifest.append({"custom_id": custom_id, "comment_ids": [r["comment_id"] for r in rows]})

    batch = client.messages.batches.create(requests=requests)
    return batch.id, manifest


# ---------- polling + result retrieval ----------

def poll_until_done(client, batch_id, poll_interval):
    while True:
        batch = client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        print(
            f"  status={batch.processing_status} "
            f"(succeeded={counts.succeeded} errored={counts.errored} "
            f"processing={counts.processing} canceled={counts.canceled} expired={counts.expired})"
        )
        if batch.processing_status == "ended":
            return batch
        time.sleep(poll_interval)


def fetch_results(client, batch_id):
    """Returns (results_by_comment, errors, usage) where usage is the summed
    real input/output token counts across all requests in the batch — used
    to report actual cost rather than an estimate."""
    results_by_comment = {}
    errors = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    for result in client.messages.batches.results(batch_id):
        custom_id = result.custom_id
        if result.result.type != "succeeded":
            errors.append((custom_id, result.result.type))
            continue
        message = result.result.message
        usage["input_tokens"] += message.usage.input_tokens
        usage["output_tokens"] += message.usage.output_tokens
        tool_use_blocks = [b for b in message.content if b.type == "tool_use"]
        if not tool_use_blocks:
            errors.append((custom_id, "no_tool_use_block"))
            continue
        classifications = tool_use_blocks[0].input.get("classifications", [])
        for c in classifications:
            results_by_comment[c["comment_id"]] = c
    return results_by_comment, errors, usage


def report_cost(model, usage):
    pricing = BATCH_PRICING_PER_MTOK.get(model)
    if not pricing:
        print(f"  [info] no pricing table entry for {model} — skipping cost estimate.")
        return
    input_cost = usage["input_tokens"] / 1_000_000 * pricing["input"]
    output_cost = usage["output_tokens"] / 1_000_000 * pricing["output"]
    total = input_cost + output_cost
    print(
        f"\nActual usage this run: {usage['input_tokens']:,} input tokens, "
        f"{usage['output_tokens']:,} output tokens.\n"
        f"Cost @ batch rates for {model}: ${input_cost:.4f} input + ${output_cost:.4f} output "
        f"= ${total:.4f} total.\n"
        f"(Batch API pricing — check platform.claude.com/docs/en/about-claude/pricing "
        f"if it's been a while, rates do change.)"
    )


# ---------- main flows ----------

def run_new_batch(args, cfg):
    model_key = MODEL_ALIASES[args.model]
    model = cfg["classification"][model_key]
    max_per_call = cfg["classification"]["max_comments_per_call"]
    max_tokens = cfg["classification"]["max_output_tokens"]
    poll_interval = cfg["classification"]["poll_interval_seconds"]
    rubric_path = cfg["classification"]["rubric_path"]
    context_char_limit = cfg["classification"].get("context_char_limit", 400)

    df = read_csv_robust(args.input, dtype=str)
    if args.sample:
        df = df.sample(n=min(args.sample, len(df)), random_state=args.seed).reset_index(drop=True)
        print(f"Sampled {len(df)} comments (seed={args.seed}) for calibration.")

    rubric_text = load_rubric(rubric_path)
    system_prompt = build_system_prompt(rubric_text)

    chunks = chunk_by_thread(df, max_per_call)
    print(f"Grouped {len(df)} comments into {len(chunks)} API calls (model={model}).")

    client = anthropic.Anthropic()
    batch_id, manifest = submit_batch(client, chunks, model, system_prompt, max_tokens, context_char_limit)
    print(f"Submitted batch: {batch_id}")

    save_batch_job(batch_id, {
        "model": model,
        "model_alias": args.model,
        "input_csv": os.path.abspath(args.input),
        "manifest": manifest,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "status": "submitted",
    })

    finish_batch(client, batch_id, poll_interval)


def resume_batch(args, cfg):
    jobs = load_batch_jobs()
    if args.resume not in jobs:
        print(f"ERROR: no local record of batch {args.resume} in {BATCH_JOBS_PATH}")
        sys.exit(1)
    client = anthropic.Anthropic()
    poll_interval = cfg["classification"]["poll_interval_seconds"]
    finish_batch(client, args.resume, poll_interval)


def finish_batch(client, batch_id, poll_interval):
    print(f"Polling batch {batch_id} (Ctrl+C is safe — rerun with --resume {batch_id} to continue)...")
    poll_until_done(client, batch_id, poll_interval)

    results_by_comment, errors, usage = fetch_results(client, batch_id)
    print(f"Retrieved {len(results_by_comment)} classified comments.")
    if errors:
        print(f"  [warn] {len(errors)} chunks had errors, e.g.: {errors[:5]}")

    jobs = load_batch_jobs()
    record = jobs[batch_id]
    report_cost(record["model"], usage)
    input_csv = record["input_csv"]

    df = read_csv_robust(input_csv, dtype=str)
    manifest_ids = {cid for chunk in record["manifest"] for cid in chunk["comment_ids"]}
    if args_sample_active(record, df):
        df = df[df["comment_id"].isin(manifest_ids)].reset_index(drop=True)

    for field in ("relevance_tag", "icp_likelihood", "confidence", "rationale"):
        df[field] = df["comment_id"].map(lambda cid: results_by_comment.get(cid, {}).get(field, ""))

    missing = df[df["relevance_tag"] == ""]
    if len(missing):
        print(f"  [warn] {len(missing)} comments got no classification back (check errors above).")

    stem = os.path.splitext(os.path.basename(input_csv))[0]
    output_dir = os.path.dirname(input_csv) or "."
    output_path = os.path.join(output_dir, f"{stem}_classified_{record['model_alias']}.csv")
    df.to_csv(output_path, index=False)

    record["status"] = "completed"
    record["output_csv"] = output_path
    record["completed_at"] = datetime.now(timezone.utc).isoformat()
    save_batch_job(batch_id, record)

    print(f"\nDone. Classified CSV written to:\n  {output_path}")


def args_sample_active(record, full_df):
    """If the run used --sample, the manifest covers fewer comment_ids than the
    full input file — detect that so we only keep the sampled rows in output."""
    manifest_ids = {cid for chunk in record["manifest"] for cid in chunk["comment_ids"]}
    return len(manifest_ids) < len(full_df)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", help="CSV of raw comments to classify")
    parser.add_argument("--model", choices=["sonnet", "haiku"], help="Which model to use")
    parser.add_argument("--sample", type=int, default=None, help="Randomly sample N comments (calibration runs)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for --sample (keep constant to compare models on the same subset)")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--resume", default=None, help="Batch ID to resume polling/fetching instead of submitting new")
    args = parser.parse_args()

    cfg = load_config(args.config)

    if args.resume:
        resume_batch(args, cfg)
    else:
        if not args.input or not args.model:
            parser.error("--input and --model are required unless using --resume")
        run_new_batch(args, cfg)
