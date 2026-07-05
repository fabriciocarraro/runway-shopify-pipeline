#!/usr/bin/env python3
"""
Catalog → Campaign
Turn a Shopify catalog into platform-ready product videos with the Runway API.

Quickstart:
    pip install -r requirements.txt
    cp .env.example .env        # add your RUNWAYML_API_SECRET (get one: https://dev.runwayml.com)
    python catalog_to_video.py --store yourstore.myshopify.com --skus 5

No store handy? Use a local catalog file instead:
    python catalog_to_video.py --catalog sample_catalog.json

Docs: https://docs.dev.runwayml.com
"""

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from runwayml import RunwayML

from styles import STYLES  # motion presets live in their own file — add new ones there

load_dotenv()

# Keep emoji status output alive when stdout is redirected on Windows (cp1252):
# without this, `python catalog_to_video.py ... > run.log` dies on the first 🛍.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TERMINAL_SUCCESS = {"SUCCEEDED"}
# The API returns "CANCELED" (one L, per docs.dev.runwayml.com); "CANCELLED" is
# kept as a defensive alias. Non-terminal states (PENDING/THROTTLED/RUNNING) are
# treated as "still rendering" by falling through in poll_all.
TERMINAL_FAILURE = {"FAILED", "CANCELED", "CANCELLED"}

# Gen-4.5 pricing, confirmed against docs.dev.runwayml.com/guides/pricing:
# 12 credits per second of video; credits cost $0.01 each → 5s clip ≈ $0.60.
# (gen4_turbo is 5 credits/s — don't mix them up when switching models.)
CREDITS_PER_SECOND = 12
USD_PER_CREDIT = 0.01

# gen4.5 image_to_video accepted ratios (docs.dev.runwayml.com/assets/inputs):
#   Landscape 1280:720 1584:672 1104:832 · Portrait 720:1280 832:1104 672:1584 · Square 960:960
DEFAULT_RATIO = "720:1280"  # 9:16 — Reels/TikTok-first (confirmed valid)

# Motion presets (STYLES) live in styles.py so new looks can be added there
# without touching the pipeline. Imported at the top of this file.


# ---------------------------------------------------------------------------
# Catalog loading
# ---------------------------------------------------------------------------

def fetch_shopify_catalog(store: str, limit: int) -> list[dict]:
    """Pull products from a store's public products.json (no auth needed on
    most Shopify stores). Returns [{sku, title, image}]."""
    store = store.replace("https://", "").replace("http://", "").strip("/")
    url = f"https://{store}/products.json?limit={min(limit, 250)}"
    try:
        resp = requests.get(url, headers={"User-Agent": "catalog-to-campaign-demo"}, timeout=30)
        resp.raise_for_status()
        products = resp.json().get("products", [])
    except requests.exceptions.JSONDecodeError:
        sys.exit(f"{url} did not return JSON (bot protection?). Use --catalog instead.")
    except requests.RequestException as e:
        sys.exit(f"Could not fetch {url}: {e}\nIf the store blocks products.json, use --catalog instead.")

    items = []
    for p in products:
        images = p.get("images") or []
        if not images:
            continue  # no image, nothing to animate
        src = images[0].get("src", "")
        if not src:
            continue
        # Normalize size via Shopify's CDN transform so Runway gets a
        # reasonable input (VERIFY promptImage size/format constraints).
        sep = "&" if "?" in src else "?"
        items.append({
            "sku": p.get("handle") or str(p.get("id")),
            "title": p.get("title", "Product"),
            "image": f"{src}{sep}width=1280",
        })
        if len(items) >= limit:
            break
    return items


def load_catalog_file(path: Path, limit: int) -> list[dict]:
    """Load a local catalog: [{"sku": ..., "title": ..., "image": <public URL>}]"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        sys.exit(f"Catalog file not found: {path}")
    except json.JSONDecodeError as e:
        sys.exit(f"{path} is not valid JSON: {e}")
    items = [i for i in data.get("items", []) if i.get("image") and i.get("sku")]
    return items[:limit]


def safe_name(sku: str) -> str:
    """SKU becomes the output filename — keep it filesystem-safe (local catalogs
    can contain anything; Shopify handles pass through unchanged)."""
    return re.sub(r"[^\w.\-]+", "_", sku).strip("._") or "item"


# ---------------------------------------------------------------------------
# Runway task lifecycle: submit all → poll all → bounded retry on transients
# ---------------------------------------------------------------------------

def is_rate_limited(exc: Exception) -> bool:
    """True for 429 / daily-limit / rate-limit errors. These won't clear on an
    immediate retry, so retrying just wastes calls (and can create duplicates)."""
    if getattr(exc, "status_code", None) == 429:
        return True
    s = str(exc).lower()
    return "429" in s or "daily task limit" in s or "rate limit" in s


def submit_video_task(client: RunwayML, item: dict, prompt: str,
                      duration: int, ratio: str) -> str:
    """Submit one image→video task; the catalog photo is the anchor frame."""
    task = client.image_to_video.create(
        model="gen4.5",
        prompt_image=item["image"],
        prompt_text=f"{prompt}. Product: {item['title']}",
        duration=duration,
        ratio=ratio,
    )
    return task.id


def poll_all(client: RunwayML, pending: dict, timeout: float):
    """Round-robin poll every pending task until all reach a terminal state.

    pending: {task_id: item}. Returns (successes {task_id: (item, url)},
    failures {task_id: (item, reason)}, still_pending {task_id: item}).
    Backoff grows per sweep so we don't hammer the API while tasks render.

    On timeout the leftover tasks are returned in still_pending, NOT failures:
    they are (very likely) still rendering, so the caller must not resubmit
    them — that would double-bill and burn daily quota on work already running.
    """
    successes, failures = {}, {}
    delay, started = 5.0, time.monotonic()  # Runway recommends polling ≥5s, with jitter

    while pending:
        for task_id in list(pending):
            try:
                t = client.tasks.retrieve(task_id)
            except Exception as e:
                # A transient retrieve error must not kill the run: the task is
                # still rendering server-side, and losing track of it means a
                # re-run would resubmit and re-bill. Leave it for the next sweep.
                print(f"  ⚠  poll error on {pending[task_id]['sku']} ({e}) — retrying next sweep")
                continue
            status = t.status
            if status in TERMINAL_SUCCESS:
                out = t.output
                url = out[0] if isinstance(out, (list, tuple)) else out
                successes[task_id] = (pending.pop(task_id), url)
            elif status in TERMINAL_FAILURE:
                reason = (getattr(t, "failure", None) or getattr(t, "failure_code", None)
                          or getattr(t, "failureCode", None) or getattr(t, "error", None) or "unknown")
                failures[task_id] = (pending.pop(task_id), str(reason))
        if not pending:
            break
        if time.monotonic() - started > timeout:
            break  # leftover `pending` is returned as still-rendering, not failed
        time.sleep(min(delay, 15.0) + random.uniform(0, 1.0))
        delay *= 1.4

    return successes, failures, pending


def download(url: str, dest: Path) -> Path:
    """Stream to a .part file, then move into place atomically: a crash mid-
    download must never leave a partial {sku}.mp4 for idempotency to skip."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    os.replace(tmp, dest)
    return dest


def square_crop(src: Path) -> Path | None:
    """Optional 1:1 center-crop for feed placements (requires ffmpeg)."""
    if not shutil.which("ffmpeg"):
        return None
    dest = src.with_name(src.stem + "_1x1.mp4")
    cmd = ["ffmpeg", "-y", "-i", str(src),
           "-vf", "crop='min(iw,ih)':'min(iw,ih)'",
           "-c:a", "copy", str(dest)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError:
        return None  # match the ffmpeg-absent behavior: skip the crop, keep the run alive
    return dest


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Turn a Shopify catalog into product videos with the Runway API.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--store", help="Shopify store domain, e.g. yourstore.myshopify.com")
    src.add_argument("--catalog", type=Path, help="Local catalog JSON (see sample_catalog.json)")
    ap.add_argument("--skus", type=int, default=5, help="How many products (default 5)")
    ap.add_argument("--duration", type=int, default=5,
                    help="Seconds per video (default 5; if the API rejects a value, "
                         "its 400 response lists the accepted set)")
    ap.add_argument("--ratio", default=DEFAULT_RATIO, help=f"Output ratio (default {DEFAULT_RATIO})")
    ap.add_argument("--style", choices=STYLES, default="studio")
    ap.add_argument("--out", type=Path, default=Path("./output"))
    ap.add_argument("--timeout", type=int, default=0,
                    help="Max seconds to wait for a poll batch (default: auto-scales "
                         "with SKU count; low-concurrency accounts render slowly)")
    ap.add_argument("--square", action="store_true", help="Also export a 1:1 center-crop (needs ffmpeg)")
    ap.add_argument("--dry-run", action="store_true", help="List what would be generated; spend nothing")
    args = ap.parse_args()

    # -- Load catalog
    if args.store:
        print(f"🛍  Fetching catalog from {args.store} …")
        items = fetch_shopify_catalog(args.store, args.skus)
    else:
        print(f"🛍  Loading catalog from {args.catalog} …")
        items = load_catalog_file(args.catalog, args.skus)

    if not items:
        sys.exit("No products with images found. If the store blocks products.json, use --catalog.")

    for it in items:
        it["sku"] = safe_name(str(it["sku"]))
        it.setdefault("title", "Product")

    # -- Idempotency: skip SKUs already rendered (re-runs don't re-bill)
    args.out.mkdir(parents=True, exist_ok=True)
    todo = []
    for it in items:
        if (args.out / f"{it['sku']}.mp4").exists():
            print(f"  ↩  {it['sku']} already rendered — skipping")
        else:
            todo.append(it)

    # Estimate what THIS run will spend (skipped SKUs cost nothing).
    est_credits = len(todo) * args.duration * CREDITS_PER_SECOND
    print(f"   {len(todo)} of {len(items)} products to generate · {args.duration}s each · "
          f"est. ~{est_credits} credits (~${est_credits * USD_PER_CREDIT:.2f})\n")

    if args.dry_run:
        for it in todo:
            print(f"  [dry-run] would generate: {it['sku']}  ←  {it['title']}")
        return
    if not todo:
        print("Nothing to do — all requested SKUs already rendered.")
        return

    if not os.environ.get("RUNWAYML_API_SECRET"):
        sys.exit("RUNWAYML_API_SECRET not set. Copy .env.example to .env and add your key "
                 "(create one at https://dev.runwayml.com).")

    client = RunwayML()
    prompt = STYLES[args.style]
    started = time.monotonic()

    # -- Phase 1: submit everything up front (parallel on the API side).
    # Stop early on a daily-limit 429: every further submit would just 429 too.
    pending, submit_failures = {}, []
    for idx, it in enumerate(todo):
        try:
            tid = submit_video_task(client, it, prompt, args.duration, args.ratio)
            pending[tid] = it
            print(f"  🚀 submitted {it['sku']}  (task {tid[:8]}…)")
        except Exception as e:
            submit_failures.append((it, str(e)))
            print(f"  ⚠  submit failed for {it['sku']}: {e}")
            if is_rate_limited(e):
                print("  ⛔ daily task limit reached — submitting no more this run.")
                for x in todo[idx + 1:]:
                    submit_failures.append((x, "skipped — daily task limit reached"))
                break

    # Poll timeout scales with workload: a big catalog on a low-concurrency
    # account renders far longer than any fixed 15-min window, and a task that's
    # merely slow must never be mistaken for a failure.
    poll_timeout = args.timeout or (300 + 120 * len(pending))

    # -- Phase 2: poll everything; Phase 3: one bounded retry for GENUINE
    # failures only. Rationale: terminal FAILED is often transient, so we retry
    # once (bounded — a truly bad request would otherwise re-bill forever). But
    # still-rendering tasks (poll timeout) are NOT resubmitted: they're likely
    # in-flight, and resubmitting duplicates both double-bills and burns quota.
    successes, failures, still_pending = poll_all(client, pending, poll_timeout)
    if failures:
        print(f"\n  🔁 retrying {len(failures)} failed task(s) once …")
        retry_pending = {}
        for _, (it, reason) in failures.items():
            try:
                tid = submit_video_task(client, it, prompt, args.duration, args.ratio)
                retry_pending[tid] = it
            except Exception as e:
                submit_failures.append((it, f"retry submit failed: {e}"))
                if is_rate_limited(e):
                    print("  ⛔ daily task limit reached — stopping retries.")
                    break
        if retry_pending:
            retr_ok, retr_fail, retr_pending = poll_all(client, retry_pending, poll_timeout)
            successes.update(retr_ok)
            failures = retr_fail
            still_pending.update(retr_pending)
        else:
            failures = {}

    # -- Download
    print()
    outputs = []
    for task_id, (it, url) in successes.items():
        try:
            dest = download(url, args.out / f"{it['sku']}.mp4")
        except Exception as e:
            # The render is done and paid for — don't lose it to a download blip.
            # Surface the URL for manual recovery (note: Runway output URLs expire).
            failures[task_id] = (it, f"download failed: {e} · output URL: {url}")
            continue
        outputs.append(dest)
        line = f"  ✅ {dest.name}"
        if args.square:
            sq = square_crop(dest)
            if sq:
                line += f"  (+ {sq.name})"
        print(line)

    # -- Summary (these are your launch-post numbers)
    elapsed = time.monotonic() - started
    spent_credits = len(outputs) * args.duration * CREDITS_PER_SECOND
    mins, secs = divmod(int(elapsed), 60)
    print(f"\n🎬 {len(outputs)} videos · {mins}m{secs:02d}s wall-clock · ~{spent_credits} credits (~${spent_credits * USD_PER_CREDIT:.2f})")
    for it, reason in submit_failures:
        print(f"  ✗ {it['sku']}: {reason}")
    for _, (it, reason) in failures.items():
        print(f"  ✗ {it['sku']}: {reason}")
    for tid, it in still_pending.items():
        print(f"  ⏳ {it['sku']}: still rendering when we stopped (task {tid}) — not failed, "
              f"not resubmitted. Fetch it later via client.tasks.retrieve, or raise --timeout next run.")
    if outputs:
        src_label = args.store or args.catalog.name
        print(f'\n📣 Tweet-ready: "I turned {src_label}\'s catalog into {len(outputs)} product '
              f'videos in {mins} minutes, for about ${spent_credits * USD_PER_CREDIT:.2f}. '
              f'No shoot, no agency — one script."')
    elif submit_failures or failures or still_pending:
        sys.exit(1)  # nothing produced and something went wrong: fail loudly for scripts/CI


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n⚠  Interrupted. Tasks already submitted keep rendering on Runway's side; "
              "finished downloads are kept, and a re-run skips them.")
        sys.exit(130)
