# eBay PC / GPU deal scanner

Every 30 minutes GitHub Actions checks newly listed eBay Buy It Now gaming desktops and graphics cards, estimates resale from `data/values.json`, and opens a GitHub issue for anything that looks like **$100+ profit and 20%+ margin**. GitHub emails each issue to Sven, and his hourly Claude deal filter re-checks it against live sold comps before sending a phone alert.

## Setup (phone is fine)
1. Repo **Settings → Secrets and variables → Actions → New repository secret**:
   - `EBAY_CLIENT_ID`: eBay App ID (Client ID), Production keyset
   - `EBAY_CLIENT_SECRET`: eBay Cert ID (Client Secret)
2. **Actions** tab → enable workflows if asked → **eBay deal scan → Run workflow** to test.
3. Make sure GitHub emails you issues: Settings (your profile) → Notifications → Email for "Participating, @mentions" and "Watching" should be on.

## Tuning
- `data/searches.json`: search terms, price ranges, profit rules.
- `data/values.json`: used resale estimates per GPU (conservative). Update as prices move.
- Close or delete deal issues whenever you like; they're just alerts.
