<div align="center">

# 🛍️ → 🎬 &nbsp;Catalog → Campaign

### Turn any Shopify catalog into platform-ready product videos — with one script and the [Runway API](https://dev.runwayml.com).

<br/>

<img src="assets/demo.gif" alt="Product videos generated from a live Shopify catalog" width="760"/>

<br/>
<sub><i>Real output — three SKUs pulled straight from a live store's <code>products.json</code>, animated with <code>gen4.5</code>.</i></sub>

<br/><br/>

[![Python](https://img.shields.io/badge/python-3.11+-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Runway API](https://img.shields.io/badge/Runway-gen4.5-F062B4?style=flat-square)](https://docs.dev.runwayml.com)
[![License: MIT](https://img.shields.io/badge/license-MIT-7C5CFF?style=flat-square)](LICENSE)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-FF7A59?style=flat-square)](#contributing)

</div>

---

Point it at a store. It pulls each product's photo, animates it with `gen4.5` — **your catalog shot is the anchor frame, so the product stays true, no hallucinated variants** — and exports a Reels-ready 9:16 video per SKU.

**Your first run:** about ten minutes, and free starter credits cover it. A 5-second clip costs on the order of ~$0.25. <!-- pricing approximate; a Runway 400 response enumerates current values -->

## ⚡ Quickstart

```bash
git clone https://github.com/fabriciocarraro/runway-shopify-pipeline && cd runway-shopify-pipeline
pip install -r requirements.txt
cp .env.example .env     # add your API key → get one at https://dev.runwayml.com

python catalog_to_video.py --catalog sample_catalog.json          # proof it works (sample data)
python catalog_to_video.py --store yourstore.myshopify.com --skus 5   # now: YOUR products, moving
```

That second command is the whole point: most Shopify stores expose `products.json` publicly, so this runs against **your real catalog** with zero app installs and zero auth dances.

> **Tip:** start with `--dry-run` to see exactly what would be generated — it spends nothing.

## 🧩 How it works

<div align="center">
<img src="assets/pipeline.svg" alt="Pipeline: catalog → submit all → poll → bounded retry → videos" width="880"/>
</div>

1. **Catalog in.** `products.json` (or a local JSON file) → product title + first image, size-normalized through Shopify's CDN.
2. **Submit everything up front.** Every SKU's image→video task is submitted *before* any polling begins — generation runs in parallel on Runway's side, so 10 videos take barely longer than one.
3. **Poll with backoff.** One round-robin loop checks every pending task, backing off with jitter between sweeps (Runway's recommended ≥5s cadence).
4. **Bounded retry on real failures.** Terminal failures are often transient, so each *genuinely failed* task retries exactly once — bounded on purpose, because a truly bad request shouldn't re-bill forever. Tasks that are merely slow (still rendering at timeout) are **never** resubmitted, so nothing gets double-billed.
5. **Idempotent re-runs.** Already-rendered SKUs are skipped, so a crash or a tweak never re-spends credits on finished work.

## ⚙️ Options

| Flag | Default | What it does |
|------|---------|--------------|
| `--store` / `--catalog` | — | Source: a live Shopify domain **or** a local catalog JSON (one required) |
| `--skus N` | `5` | How many products to animate |
| `--duration N` | `5` | Seconds per video (2–10) |
| `--style X` | `studio` | Motion preset: `studio` · `lifestyle` · `dramatic` |
| `--ratio W:H` | `720:1280` | Output aspect ratio (9:16 by default) |
| `--out DIR` | `./output` | Where the MP4s land |
| `--timeout N` | *auto* | Max seconds to wait per poll batch (auto-scales with SKU count) |
| `--square` | off | Also export a 1:1 center-crop for feed (needs `ffmpeg`) |
| `--dry-run` | off | List what would be generated; spend nothing |

## 🎨 Style presets

| `studio` | `lifestyle` | `dramatic` |
|:--------:|:-----------:|:----------:|
| Slow push-in, soft studio light, clean background | Natural light, shallow depth of field, warm handheld drift | Dark background, sweeping rim light, slow orbit |

## 🛟 Troubleshooting

<details>
<summary><b><code>products.json</code> returns 403 / 404</b></summary>

Some stores disable it. Use `--catalog your_file.json` — see [`sample_catalog.json`](sample_catalog.json) for the shape: `{"items": [{"sku", "title", "image"}]}` with public image URLs.
</details>

<details>
<summary><b>400 on ratio or parameters</b></summary>

Read the response body, not just the status code — Runway's validation errors enumerate every accepted value inline. For `gen4.5` image→video the ratios are: `1280:720 1584:672 1104:832` (landscape), `720:1280 832:1104 672:1584` (portrait), `960:960` (square). That body is the source of truth.
</details>

<details>
<summary><b>Image rejected</b></summary>

`prompt_image` must be a publicly reachable HTTPS image. The script requests `width=1280` from Shopify's CDN; adjust if your source images are unusually sized.
</details>

<details>
<summary><b>429 · "daily task limit reached"</b></summary>

That's an account-tier cap, not a bug — the script stops cleanly when it hits one instead of hammering. Generate in smaller `--skus` batches, or check your [usage tier](https://docs.dev.runwayml.com/usage/tiers/).
</details>

## 💡 Why this exists

DTC creative is a volume game now — paid social wants fresh assets every week, and the traditional pipeline (brief → shoot/agency → revisions) runs days-to-weeks per asset. But if your products live in a catalog, the brief already exists. This repo turns it into video.

## 🤝 Contributing

PRs welcome — especially new style presets. Fork, add your preset to the `STYLES` dict in [`catalog_to_video.py`](catalog_to_video.py), and open a PR.

## 📄 License

MIT — see [LICENSE](LICENSE).

<div align="center"><br/><sub>Built with the <a href="https://docs.dev.runwayml.com">Runway API</a>.</sub></div>
