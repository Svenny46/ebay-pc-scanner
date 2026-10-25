"""eBay gaming PC / GPU deal scanner.

Runs on GitHub Actions every 30 minutes. Pulls newly listed Buy It Now items
from eBay's official Browse API, estimates resale from data/values.json, and
opens one GitHub issue per possible deal. GitHub emails the issue to Sven's
Gmail, where his hourly Claude deal filter re-checks it against live sold
comps before anything reaches his phone.

Usage:
  python scanner.py            # live run (needs EBAY_CLIENT_ID / EBAY_CLIENT_SECRET)
  python scanner.py --mock     # offline test using data/mock_listings.json
"""
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SEEN_FILE = DATA / "seen.json"
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SEEN_KEEP_SECONDS = 14 * 24 * 3600

# Titles containing any of these are skipped outright.
REJECT_WORDS = [
    "box only", "empty box", "no gpu", "no graphics", "without gpu", "no video card",
    "mining", "miner", "rig lot", "lot of", "bundle of", "read description", "read desc",
    "laptop", "notebook", "backplate only", "fan only", "fans only", "shroud", "cooler only",
    "bracket", "water block", "waterblock", "dead", "no power", "no display", "artifacting",
    "case only", "picture of", "photo of", "image of", "parts only",
    # Stripped boards: the GPU chip and/or memory have been removed, so nothing to fix.
    "no core", "no vram", "no gpu core", "no die", "no chip", "core removed", "vram removed",
    "pcb only", "board only", "bare pcb", "donor board", "no memory chips",
]
PARTS_WORDS = ["for parts", "not working", "as is", "as-is", "untested"]
LEARNED_FILE = DATA / "learned_filters.json"


def flat(text):
    """Lowercase, punctuation to spaces: 'NO CORE/NO VRAM' -> ' no core no vram '."""
    return " " + re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip() + " "


def learned_rejects():
    """Phrases the hourly Claude reviewer added after rating bad picks (see README)."""
    data = load_json(LEARNED_FILE, {})
    return [p["phrase"] for p in data.get("reject_phrases", []) if p.get("phrase")]


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def normalize(title):
    t = title.lower()
    t = t.replace("geforce", " ").replace("nvidia", " ").replace("radeon", " ").replace("amd", " ")
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    # "rtx3060ti" -> "rtx 3060 ti", "rx6700xt" -> "rx 6700 xt"
    t = re.sub(r"\b(rtx|gtx|rx)(\d{3,4})", r"\1 \2", t)
    t = re.sub(r"(\d{3,4})(ti|xt|xtx|gre|super)\b", r"\1 \2", t)
    t = re.sub(r"\s+", " ", t)
    return f" {t.strip()} "


def find_gpu(title, gpu_values, allow_bare=True):
    """Return (gpu_name, value) for the most specific GPU named in the title."""
    t = normalize(title)
    # Graphics-card titles sometimes drop the rtx/gtx/rx prefix ("3060 ti 12gb").
    # Desktop titles may not: "Dell OptiPlex 3080" is a model number, not a GPU.
    best = None
    for name, value in gpu_values.items():
        bare = name.split(" ", 1)[1]
        for variant in ((name, bare) if allow_bare else (name,)):
            if f" {variant} " in t:
                if best is None or len(name) > len(best[0]):
                    best = (name, value)
    if best:
        # Multiple different GPU models in one title usually means a vague/keyword-stuffed ad.
        models = set(re.findall(r"\b(?:rtx|gtx|rx) (\d{3,4})\b", t))
        if len(models) > 1:
            return None
    return best


def evaluate(item, search, cfg):
    rules = cfg["rules"]
    title = item.get("title", "")
    low = title.lower()
    ft = flat(title)
    if any(w in low or flat(w) in ft for w in REJECT_WORDS + cfg.get("learned", [])):
        return None
    gpu = find_gpu(title, cfg["gpus"], allow_bare=search["kind"] == "gpu")
    if not gpu:
        return None
    gpu_name, gpu_value = gpu

    price = float(item.get("price", {}).get("value", 0) or 0)
    ship = 0.0
    opts = item.get("shippingOptions") or []
    if opts and opts[0].get("shippingCost"):
        ship = float(opts[0]["shippingCost"].get("value", 0) or 0)
    elif opts and opts[0].get("shippingCostType") == "CALCULATED":
        ship = 35.0 if search["kind"] == "pc" else 12.0  # rough guess for calculated shipping
    buy = price + ship
    if buy <= 0 or price < search.get("min_price", 0):
        return None

    condition = (item.get("condition") or "").lower()
    is_parts = "parts" in condition or any(w in low for w in PARTS_WORDS)

    if search["kind"] == "pc":
        resale = gpu_value + cfg["pc_base_value"]
        fees = resale * rules["pc_resale_fee_rate"]
        fix = rules["parts_fix_cost"] if is_parts else rules["pc_fix_cost"]
    else:
        resale = gpu_value
        fees = resale * rules["gpu_resale_fee_rate"] + rules["gpu_resale_fee_fixed"] + rules["gpu_outbound_shipping"]
        fix = rules["parts_fix_cost"] if is_parts else 0
        if is_parts:
            # A broken card often can't be fixed at all; budget for that risk
            # instead of pricing it as a working card minus a small repair.
            fix += round(resale * rules.get("parts_gpu_risk_rate", 0.35))

    # Far below market is usually a scam, a deposit, or a listing for something else.
    # Broken "for parts" items legitimately go cheaper, so they get a lower floor.
    floor = rules.get("min_price_vs_resale_parts", 0.15) if is_parts else rules.get("min_price_vs_resale", 0.3)
    if buy < floor * resale:
        return None

    profit = resale - buy - fees - fix
    if profit < rules["min_profit"] or profit < rules["min_margin"] * buy:
        return None

    # Highest buy price that still clears both bars.
    max_buy = min(resale - fees - fix - rules["min_profit"],
                  (resale - fees - fix) / (1 + rules["min_margin"]))
    seller = item.get("seller") or {}
    return {
        "id": item.get("itemId"),
        "title": title,
        "url": item.get("itemWebUrl"),
        "search": search["name"],
        "kind": search["kind"],
        "gpu": gpu_name.upper(),
        "condition": item.get("condition", "unknown"),
        "is_parts": is_parts,
        "price": round(price, 2),
        "shipping": round(ship, 2),
        "buy": round(buy, 2),
        "resale_est": round(resale),
        "fees_est": round(fees),
        "fix_est": fix,
        "profit_est": round(profit),
        "max_buy": int(max_buy),
        "location": (item.get("itemLocation") or {}).get("postalCode", "")
                    + " " + (item.get("itemLocation") or {}).get("country", ""),
        "seller": f"{seller.get('username', '?')} ({seller.get('feedbackPercentage', '?')}%, {seller.get('feedbackScore', '?')} ratings)",
        "best_offer": "BEST_OFFER" in (item.get("buyingOptions") or []),
        "listed": item.get("itemCreationDate", ""),
    }


def get_token():
    cid = (os.environ.get("EBAY_CLIENT_ID") or "").strip()
    secret = (os.environ.get("EBAY_CLIENT_SECRET") or "").strip()
    if not cid or not secret:
        print("eBay keys not added yet (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET). Skipping this run.")
        sys.exit(0)
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    r = requests.post(TOKEN_URL, timeout=30,
                      headers={"Authorization": f"Basic {auth}",
                               "Content-Type": "application/x-www-form-urlencoded"},
                      data={"grant_type": "client_credentials",
                            "scope": "https://api.ebay.com/oauth/api_scope"})
    if r.status_code >= 300:
        # eBay's error text never contains the keys; surface it as a GitHub annotation
        print(f"::error::eBay sign-in failed ({r.status_code}): {r.text[:300]}")
        sys.exit(1)
    return r.json()["access_token"]


def search_ebay(token, s):
    params = {
        "q": s["q"],
        "category_ids": s["category"],
        "filter": (f"price:[{s['min_price']}..{s['max_price']}],priceCurrency:USD,"
                   "buyingOptions:{FIXED_PRICE|BEST_OFFER},itemLocationCountry:US"),
        "sort": "newlyListed",
        "limit": "100",
    }
    r = requests.get(SEARCH_URL, params=params, timeout=30, headers={
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "X-EBAY-C-ENDUSERCTX": "contextualLocation=country=US,zip=28173",
    })
    r.raise_for_status()
    return r.json().get("itemSummaries", [])


def issue_body(d):
    parts = "⚠️ Listed as parts / not working / untested.\n" if d["is_parts"] else ""
    offer = "Accepts offers." if d["best_offer"] else "No offers (fixed price)."
    return f"""@Svenny46 possible deal from the eBay scanner (first-pass estimate, needs verification).

**{d['title']}**
{d['url']}

- Search: {d['search']} · GPU: **{d['gpu']}** · Condition: {d['condition']}
{parts}- Price: ${d['price']} + ${d['shipping']} shipping = **${d['buy']}**
- Est. resale: ${d['resale_est']} · est. fees: ${d['fees_est']} · est. fix: ${d['fix_est']}
- **Est. profit: ${d['profit_est']}** · max buy for $100 / 20%: ${d['max_buy']}
- Seller: {d['seller']} · Location: {d['location']} · {offer}
- Listed: {d['listed']}

<!-- scanner-data {json.dumps(d)} -->
"""


def open_issue(d):
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    title = f"[eBay deal] ${d['profit_est']} est. profit · {d['gpu']} · ${d['buy']} · {d['title'][:60]}"
    if not repo or not token:
        print("--- would open issue ---\n" + title + "\n" + issue_body(d))
        return
    r = requests.post(f"https://api.github.com/repos/{repo}/issues", timeout=30,
                      headers={"Authorization": f"Bearer {token}",
                               "Accept": "application/vnd.github+json"},
                      json={"title": title, "body": issue_body(d), "labels": ["deal"]})
    if r.status_code >= 300:
        print(f"::warning::Issue creation failed ({r.status_code}): {r.text[:300]}")


def main():
    try:
        run()
    except SystemExit:
        raise
    except Exception as e:  # make any crash readable in the run summary
        print(f"::error::Scanner crashed: {type(e).__name__}: {str(e)[:300]}")
        raise


def run():
    mock = "--mock" in sys.argv
    cfg = load_json(DATA / "searches.json", {})
    vals = load_json(DATA / "values.json", {})
    cfg["gpus"], cfg["pc_base_value"] = vals["gpus"], vals["pc_base_value"]
    cfg["learned"] = learned_rejects()

    seen = load_json(SEEN_FILE, {})
    now = int(time.time())
    seen = {k: v for k, v in seen.items() if now - v < SEEN_KEEP_SECONDS}

    if mock:
        mock_data = load_json(DATA / "mock_listings.json", {})
        batches = [(s, mock_data.get(s["name"], [])) for s in cfg["searches"]]
    else:
        token = get_token()
        batches = []
        for s in cfg["searches"]:
            try:
                batches.append((s, search_ebay(token, s)))
            except requests.HTTPError as e:
                print(f"::warning::Search '{s['name']}' failed: {e} {e.response.text[:200] if e.response is not None else ''}")

    checked, deals = 0, []
    for s, items in batches:
        for item in items:
            iid = item.get("itemId")
            if not iid or iid in seen:
                continue
            seen[iid] = now
            checked += 1
            d = evaluate(item, s, cfg)
            if d:
                deals.append(d)

    deals.sort(key=lambda d: d["profit_est"], reverse=True)
    for d in deals[:5]:  # cap per run so a pricing glitch can't flood the inbox
        open_issue(d)

    if not mock:
        SEEN_FILE.write_text(json.dumps(seen))
    print(f"::notice::Checked {checked} new listings, {len(deals)} possible deals, opened {min(len(deals), 5)} issues.")


if __name__ == "__main__":
    main()
