# eBay Gaming PC & GPU Deal Scanner

An automated scanner that watches eBay for underpriced gaming PCs and graphics cards and flags the ones worth buying to resell. I built it for my used PC and GPU reselling side business. Good deals often sell within an hour, so checking listings by hand wasn't fast enough.

## What it does

Every 30 minutes, a GitHub Actions workflow:

1. **Pulls new listings** from eBay's official Browse API (Buy It Now gaming desktops and graphics cards, within set price ranges).
2. **Identifies the GPU** in each title (RTX, GTX, and Radeon RX models from the GTX 10-series through RTX 50-series).
3. **Estimates resale value** from a table of conservative used-market prices.
4. **Models the real profit** by subtracting the buy price, shipping, eBay selling fees, outbound shipping, and an expected repair cost.
5. **Opens a GitHub issue** for any listing that clears both bars: **$100+ profit and a 20%+ margin**. Each issue includes the maximum price worth paying.

## Filtering out bad listings

Most of the engineering went into rejecting listings that look like deals but aren't:

- **Model-number collisions:** office PCs like the Dell "OptiPlex 3080" were being read as RTX 3080s. For desktops, the GPU matcher now requires the full model name (like "RTX 3080"). It also rejects keyword-stuffed titles that name several different GPUs.
- **Scam pricing:** listings priced far below market (for example, a $35 "RTX 4060 PC") are almost always scams, deposits, or a listing for something else. Anything under a set fraction of resale value gets dropped.
- **Stripped cards:** "for parts" cards with the GPU core or memory removed can't be repaired. They are rejected by phrase.
- **Repair risk:** broken but complete cards are priced with a risk discount, not as a working card minus a small repair.
- **Duplicate checks:** listings already evaluated are tracked in `data/seen.json` for 14 days and skipped.

## How it learns

A scheduled AI reviewer (Claude) re-checks every pick against live sold listings, comments a verdict on the issue, and closes the bad ones. Every verdict is logged to `data/feedback.jsonl`. When the same title phrase causes two or more bad picks and never shows up in a good one, it's added to `data/learned_filters.json` and the scanner skips it from then on. Repeat overestimates lower the matching price in `data/values.json`.

The first round of feedback rated all 9 early picks as bad. Those results drove the fixes listed above.

## Tech

- Python 3.12 with `requests`
- eBay Browse API (OAuth client-credentials flow)
- GitHub Actions for scheduling, and GitHub Issues as the alert feed
- JSON config files, so search terms, price ranges, and profit rules can be tuned without touching code

## Project layout

```
scanner.py                 # fetch, evaluate, and open issues
.github/workflows/scan.yml # runs every 30 minutes, saves seen listings
data/searches.json         # search terms, price ranges, profit and fee rules
data/values.json           # used GPU resale estimates
data/learned_filters.json  # reject phrases learned from bad picks
data/feedback.jsonl        # verdict log for every pick
data/mock_listings.json    # sample listings for offline testing
```

## Running it

Offline test with sample listings (no API keys needed):

```
pip install requests
python scanner.py --mock
```

Live run on GitHub Actions:

1. Create a Production keyset at developer.ebay.com.
2. In the repo, go to **Settings → Secrets and variables → Actions** and add `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET`.
3. Open the **Actions** tab, then **eBay deal scan → Run workflow**.

API keys live only in GitHub Secrets and are never committed to the repo.
