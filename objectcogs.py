#!/usr/bin/env python3
"""
objectcogs — dig through a Discogs seller's inventory for the obscure stuff.

Give it a seller URL (e.g. https://www.discogs.com/seller/armageddonshop/profile?q=&format=CD)
and it will:
  * pull every listing via the official Discogs API (no HTML scraping, no Cloudflare fights)
  * filter to the format in the URL (CD, Vinyl, Cassette, CDr, ...)
  * score each item by community activity: haves + wants (lower = more obscure)
  * compute the want/have ratio
  * write a CSV and a sortable/filterable HTML report to ./output/

Only uses the Python standard library.

Rate limits: Discogs allows 25 requests/min anonymously, 60/min with a personal token.
Get a token at https://www.discogs.com/settings/developers and either
  export DISCOGS_TOKEN=xxxx      or      pass --token xxxx
The inventory is cached in ./cache/ so re-runs are fast.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

API = "https://api.discogs.com"
USER_AGENT = "objectcogs/0.1"
PAGE_CAP = 100          # Discogs refuses page > 100 for inventories you don't own
PER_PAGE = 100
INVENTORY_TTL = 6 * 3600   # re-use cached inventory for 6 hours
HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- HTTP

class Client:
    def __init__(self, token: str | None):
        self.token = token
        self.calls = 0
        # stay just under the per-minute limit instead of bouncing off 429s
        self.min_interval = 60 / (60 if token else 25) + 0.1
        self._last = 0.0

    def get(self, path: str, params: dict | None = None) -> dict:
        url = path if path.startswith("http") else API + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Discogs token={self.token}"

        for attempt in range(6):
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    self.calls += 1
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = 15 * (attempt + 1)
                    log(f"  rate limited, waiting {wait}s…")
                    time.sleep(wait)
                    continue
                if e.code in (500, 502, 503, 504):
                    time.sleep(5 * (attempt + 1))
                    continue
                try:
                    msg = json.load(e).get("message", "")
                except Exception:
                    msg = ""
                raise RuntimeError(f"HTTP {e.code} for {url}: {msg}") from None
            except (urllib.error.URLError, TimeoutError) as e:
                log(f"  network error ({e}), retrying…")
                time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"giving up on {url}")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- URL parsing

def parse_seller_url(url: str) -> tuple[str, str, str]:
    """Return (seller, format, query) from a discogs seller URL or bare username."""
    if "/" not in url:
        return url, "", ""
    parsed = urllib.parse.urlparse(url)
    m = re.search(r"/(?:seller|user)/([^/?#]+)", parsed.path)
    if not m:
        raise SystemExit(f"Couldn't find a seller name in {url!r}")
    qs = urllib.parse.parse_qs(parsed.query)
    return m.group(1), qs.get("format", [""])[0], qs.get("q", [""])[0]


# --------------------------------------------------------------------------- inventory

def fetch_inventory(client: Client, seller: str, query: str, refresh: bool) -> list[dict]:
    cache = HERE / "cache" / f"inventory_{seller}_{slug(query) or 'all'}.json"
    if cache.exists() and not refresh and time.time() - cache.stat().st_mtime < INVENTORY_TTL:
        log(f"Using cached inventory ({cache.name}). Pass --refresh to re-download.")
        return json.loads(cache.read_text())

    base = {"per_page": PER_PAGE, "status": "For Sale"}
    if query:
        base["q"] = query

    first = client.get(f"/users/{seller}/inventory", {**base, "page": 1, "sort": "listed", "sort_order": "asc"})
    total = first["pagination"]["items"]
    log(f"{seller} has {total:,} listings for sale" + (f" matching {query!r}" if query else ""))
    est_calls = -(-total // PER_PAGE)
    log(f"  ~{est_calls} requests, about {est_calls * client.min_interval / 60:.0f} min")

    reachable = PAGE_CAP * PER_PAGE
    # Discogs only lets non-owners page through the first 10,000 results of any one
    # sort order. Walking several sort orders from both ends covers bigger shops.
    passes = [("listed", "asc"), ("listed", "desc")]
    if total > 2 * reachable:
        passes += [("artist", "asc"), ("artist", "desc"), ("price", "asc"),
                   ("price", "desc"), ("item", "asc"), ("item", "desc")]
    if total > reachable:
        log(f"  more than {reachable:,} items: walking the list from several directions to reach them all")

    listings: dict[int, dict] = {}
    for sort, order in passes:
        pages = min(PAGE_CAP, -(-total // PER_PAGE))
        for page in range(1, pages + 1):
            if (sort, order, page) == ("listed", "asc", 1):
                data = first
            else:
                data = client.get(f"/users/{seller}/inventory",
                                  {**base, "page": page, "sort": sort, "sort_order": order})
            before = len(listings)
            for l in data.get("listings", []):
                listings[l["id"]] = slim_listing(l)
            log(f"  [{sort} {order}] page {page}/{pages} — {len(listings):,}/{total:,} unique")
            if len(listings) >= total:
                break
            # walking from the other end: once a page adds nothing new we've met the first pass
            if order == "desc" and page > 1 and len(listings) == before:
                break
        if len(listings) >= total:
            break

    if len(listings) < total:
        log(f"  note: reached {len(listings):,} of {total:,} listings "
            f"(Discogs' 10k paging cap). Try narrowing with a q= search in the URL.")

    out = list(listings.values())
    cache.parent.mkdir(exist_ok=True)
    cache.write_text(json.dumps(out))
    return out


def slim_listing(l: dict) -> dict:
    r = l.get("release", {})
    comm = (r.get("stats") or {}).get("community") or {}
    return {
        "listing_id": l["id"],
        "listing_url": l.get("uri", ""),
        "condition": l.get("condition", ""),
        "sleeve": l.get("sleeve_condition", ""),
        "comments": l.get("comments", ""),
        "price": (l.get("price") or {}).get("value"),
        "currency": (l.get("price") or {}).get("currency", ""),
        "posted": (l.get("posted") or "")[:10],
        "release_id": r.get("id"),
        "artist": r.get("artist", ""),
        "title": r.get("title", ""),
        "format": r.get("format", ""),
        "year": r.get("year") or "",
        "label": r.get("label", ""),
        "catno": r.get("catalog_number", ""),
        "thumb": r.get("thumbnail", ""),
        "haves": comm.get("in_collection", 0) or 0,
        "wants": comm.get("in_wantlist", 0) or 0,
    }


FORMAT_ALIASES = {"cass": "cassette", "lp": "vinyl", '7"': "vinyl", '10"': "vinyl", '12"': "vinyl",
                  "hdcd": "cd", "dvd-v": "dvd", "box": "box set"}


def canon_format(token: str) -> str:
    """'2xLP' -> 'vinyl', 'Cass' -> 'cassette', 'CD' -> 'cd' (Discogs abbreviates in listings)."""
    token = re.sub(r"^\s*\d+\s*x\s*", "", token.strip().lower())
    return FORMAT_ALIASES.get(token, token)


def matches_format(fmt_string: str, wanted: str) -> bool:
    """'2xCD, Album, RE' matches 'CD'; 'CDr, Album' does not (it matches 'CDr')."""
    if not wanted:
        return True
    return canon_format(wanted) in [canon_format(p) for p in re.split(r"[,+]", fmt_string)]


# --------------------------------------------------------------------------- output

COLUMNS = ["artist", "title", "format", "year", "label", "catno", "price", "currency",
           "condition", "haves", "wants", "total", "want_have_ratio", "gem",
           "for_sale_on_discogs", "only_seller", "market_low",
           "versions", "widely_pressed", "format_only",
           "genres", "styles", "country", "listing_url", "release_url"]

PRICE_NEUTRAL = 10  # a copy at this price gets no price boost or penalty


def gem_score(haves: int, wants: int, price: float | None = None) -> float:
    """Obscure AND wanted AND cheap.
    Demand per owner (wants / (haves+1)), divided by log10(haves+wants+10) so widely
    known releases sink, then scaled by price on a log curve: $10 is neutral, cheaper
    copies get a gentle boost and pricier ones a gentle penalty.
    0 wants -> 0. Examples at $10: 0h/3w 2.69 · 5h/10w 1.19 · 200h/400w 0.71 · 50h/5w 0.05"""
    score = wants / (haves + 1) / math.log10(haves + wants + 10)
    if price is not None and price >= 0:
        score /= math.log10(price + 10) / math.log10(PRICE_NEUTRAL + 10)
    return round(score, 3)


def market_text(it: dict) -> str:
    if it["for_sale_on_discogs"] == "":
        return ""
    if it["only_seller"]:
        return "ONLY HERE"
    return str(it["for_sale_on_discogs"])


def flags_text(it: dict) -> str:
    out = []
    if it["only_seller"] is True:
        out.append("ONLY HERE")
    if it["format_only"] is True:
        out.append(f'{it["format_only_name"]} ONLY')
    if it["widely_pressed"] is True:
        out.append("WIDELY PRESSED")
    return ", ".join(out)


# --------------------------------------------------------------------------- albums (masters)

MASTER_TTL = 30 * 24 * 3600
RELEASE_TTL = 7 * 24 * 3600   # for-sale counts go stale; genre/country don't


def fetch_release_info(client: Client, release_ids: list[int], fresh: bool = False) -> dict[int, dict]:
    """One request per release: genres, styles, country, master (album) id, plus copies
    for sale on all of Discogs and the lowest price. With fresh=True, entries older than
    a week (or from before for-sale counts were stored) are fetched again."""
    rel_file = HERE / "cache" / "releases.json"
    cache: dict[str, dict] = json.loads(rel_file.read_text()) if rel_file.exists() else {}
    now = time.time()
    todo = [rid for rid in release_ids if str(rid) not in cache
            or (fresh and now - cache[str(rid)].get("fetched", 0) > RELEASE_TTL)]
    if todo:
        log(f"Looking up {len(todo)} releases (about {len(todo) * client.min_interval / 60:.0f} min)…")
    for i, rid in enumerate(todo, 1):
        try:
            r = client.get(f"/releases/{rid}")
        except RuntimeError as e:
            log(f"  {e}")
            continue
        cache[str(rid)] = {"master_id": r.get("master_id") or None,
                           "genres": r.get("genres") or [], "styles": r.get("styles") or [],
                           "country": r.get("country") or "", "num_for_sale": r.get("num_for_sale"),
                           "lowest": r.get("lowest_price"), "fetched": now}
        if i % 25 == 0 or i == len(todo):
            log(f"  releases {i}/{len(todo)}")
            rel_file.parent.mkdir(exist_ok=True)
            rel_file.write_text(json.dumps(cache))
    return {rid: cache[str(rid)] for rid in release_ids if str(rid) in cache}


def fetch_albums(client: Client, info: dict[int, dict]) -> dict[int, dict]:
    """For each release: how many versions its master (album) has, and how many come
    in each format. One request per album, shared by all its pressings."""
    mas_file = HERE / "cache" / "masters.json"
    mas_cache: dict[str, dict] = json.loads(mas_file.read_text()) if mas_file.exists() else {}
    now = time.time()
    masters = {v["master_id"] for v in info.values() if v["master_id"]}
    todo_m = [m for m in masters
              if str(m) not in mas_cache or now - mas_cache[str(m)].get("fetched", 0) > MASTER_TTL]
    if todo_m:
        log(f"Counting versions for {len(todo_m)} albums…")
    for i, mid in enumerate(todo_m, 1):
        try:
            data = client.get(f"/masters/{mid}/versions", {"per_page": 1})
        except RuntimeError as e:
            log(f"  {e}")
            continue
        formats = {}
        for facet in data.get("filter_facets", []):
            if facet.get("id") == "format":
                formats = {v["title"].lower(): v["count"] for v in facet["values"]}
        mas_cache[str(mid)] = {"versions": data["pagination"]["items"], "formats": formats, "fetched": now}
        if i % 25 == 0 or i == len(todo_m):
            log(f"  albums {i}/{len(todo_m)}")
            mas_file.parent.mkdir(exist_ok=True)
            mas_file.write_text(json.dumps(mas_cache))

    out = {}
    for rid, v in info.items():
        mid = v["master_id"]
        if not mid:
            out[rid] = {"versions": 1, "formats": None}
        elif str(mid) in mas_cache:
            out[rid] = mas_cache[str(mid)]
    return out


def primary_format(fmt_string: str) -> str:
    return canon_format(re.split(r"[,+]", fmt_string)[0])


def is_format_only(album: dict, target: str) -> bool:
    """True when every physical version of the album is in the target format."""
    if album["formats"] is None:          # no master: this is the only version
        return True
    physical = album["versions"] - album["formats"].get("file", 0)
    return physical > 0 and album["formats"].get(target, 0) >= physical


def ratio_text(it: dict) -> str:
    if it["haves"] == 0:
        return "unowned" if it["wants"] else "–"
    return f'{it["want_have_ratio"]:.2f}'


def ratio_sort_value(it: dict) -> float:
    if it["haves"] == 0:
        return 1e9 if it["wants"] else -1
    return it["want_have_ratio"]


def write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_html(rows: list[dict], path: Path, title: str, wide: int) -> None:
    """Minimal report: a few columns, chip filters, click a row for details.
    Rows are embedded as JSON and drawn by JS so big shops stay fast."""
    def split(s: str) -> list[str]:   # genre names can contain commas, so ";" separates
        return [x.strip() for x in s.split(";") if x.strip()]

    data = []
    for r in rows:
        data.append({
            "a": r["artist"], "t": r["title"], "f": r["format"], "y": r["year"] or 0,
            "l": r["label"], "c": r["catno"], "p": r["price"], "cur": r["currency"],
            "cond": r["condition"], "h": r["haves"], "w": r["wants"], "tot": r["total"],
            "r": r["want_have_ratio"] if r["haves"] else None, "g": r["gem"],
            "fs": None if r["for_sale_on_discogs"] == "" else r["for_sale_on_discogs"],
            "only": r["only_seller"] if r["only_seller"] != "" else None, "low": r["market_low"],
            "ver": None if r["versions"] == "" else r["versions"],
            "wide": r["widely_pressed"] if r["widely_pressed"] != "" else None,
            "fo": r["format_only"] if r["format_only"] != "" else None, "fon": r["format_only_name"],
            "gen": split(r["genres"]), "sty": split(r["styles"]), "ct": r["country"], "inf": bool(r.get("_info")),
            "lu": r["listing_url"], "ru": r["release_url"],
        })
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")

    page = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#fff;--fg:#000;--dim:#767676;--line:#e6e6e6;--chip:#f2f2f2;--acc:#000;--on:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#000;--fg:#fff;--dim:#8c8c8c;--line:#262626;--chip:#1a1a1a;--acc:#fff;--on:#000}}
.wh{display:inline-flex;align-items:center;gap:8px}
.wh b{display:block;width:56px;height:6px;border-radius:3px;background:#16a34a;overflow:hidden}
.wh b i{display:block;height:100%;background:#dc2626}
.wh span{display:inline-block;width:56px;text-align:right;font-variant-numeric:tabular-nums}
.wh span.u{color:var(--dim);font-size:12px}
@media (prefers-color-scheme:dark){.wh b{background:#22c55e}.wh b i{background:#ef4444}}
*{box-sizing:border-box} [hidden]{display:none!important}
body{background:var(--bg);color:var(--fg);font:14px/1.45 -apple-system,system-ui,sans-serif;margin:0;padding:20px 16px;max-width:1100px;margin:auto}
h1{font-size:20px;margin:0}
.count{color:var(--dim);margin:2px 0 18px}
.group{display:flex;gap:10px;align-items:flex-start;margin:0 0 10px}
.group>span{width:64px;flex:none;color:var(--dim);font-size:12px;padding-top:5px;text-transform:uppercase;letter-spacing:.04em}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.sub{margin:-4px 0 10px 74px}
button.chip{font:inherit;font-size:13px;color:var(--fg);background:var(--chip);border:1px solid transparent;border-radius:999px;padding:3px 10px;cursor:pointer}
button.chip:hover{border-color:var(--line)}
button.chip.on{background:var(--acc);color:var(--on)}
button.chip i{font-style:normal;opacity:.55;margin-left:4px;font-size:12px}
button.chip.more{background:transparent;color:var(--dim)}
table{border-collapse:collapse;width:100%;margin-top:14px}
th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:500;font-size:12px;cursor:pointer;user-select:none;white-space:nowrap}
th:hover{color:var(--fg)} th.n,td.n{text-align:right;font-variant-numeric:tabular-nums}
tr.m{cursor:pointer} tr.m:hover td{background:var(--chip)}
td.car{width:18px;color:var(--dim);padding-right:0}
a{color:var(--acc);text-decoration:none} a:hover{text-decoration:underline}
tr.d td{background:var(--chip);padding:10px 8px 14px}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;margin:0}
dt{color:var(--dim)} dd{margin:0}
#more{display:block;margin:16px auto;font:inherit;color:var(--fg);background:var(--chip);border:0;border-radius:8px;padding:8px 16px;cursor:pointer}
@media (max-width:640px){.hide-s{display:none}.group>span{width:auto}.group{flex-direction:column;gap:4px}.sub{margin-left:0}}
</style></head><body>
<h1>__TITLE__</h1>
<div class="count" id="count"></div>
<div class="group"><span>Show</span><div class="chips" id="toggles"></div></div>
<div class="group"><span>Genre</span><div class="chips" id="genre"></div></div>
<div class="chips sub" id="style"></div>
<div class="group"><span>Country</span><div class="chips" id="country"></div></div>
<div class="group"><span>Price</span><div class="chips" id="price"></div></div>
<div class="group"><span>Decade</span><div class="chips" id="decade"></div></div>
<div class="chips sub" id="year"></div>
<table><thead><tr><th></th><th data-k="a">Artist</th><th data-k="t">Title</th><th class="n" data-k="y">Year</th><th class="n" data-k="p">Price</th>
<th class="n" data-k="h">Haves</th><th class="n" data-k="w">Wants</th>
<th class="n hide-s" data-k="rs">Want/Have</th><th class="n" data-k="g">Gem</th></tr></thead>
<tbody id="tb"></tbody></table>
<button id="more" hidden></button>
<script>
const D=__DATA__, WIDE=__WIDE__, NL='Not looked up';
D.forEach((d,i)=>{d.i=i;d.rs=d.h?d.r:(d.w?1e9:-1);d.dec=d.y?Math.floor(d.y/10)*10+'s':'Unknown';d.ys=d.y?String(d.y):'Unknown';d.pk=priceBucket(d.p,d.cur);
 d.gk=d.inf?(d.gen.length?d.gen:['Unknown']):[NL];d.ck=d.inf?(d.ct||'Unknown'):NL});
const sel={genre:new Set(),style:new Set(),country:new Set(),decade:new Set(),year:new Set(),price:new Set()};
const PRICE_EDGES = [5, 10, 20, 50];
const SYM = {USD: "$", EUR: "€", GBP: "£", JPY: "¥", CAD: "CA$", AUD: "A$"};
const priceLabels = cur => { const m = v => SYM[cur] ? SYM[cur] + v : v + " " + cur, E = PRICE_EDGES;
  return [`under ${m(E[0])}`, ...E.slice(1).map((v, i) => `${m(E[i])}–${m(v)}`), `${m(E[E.length - 1])}+`]; };
const priceBucket = (p, cur) => p == null ? "Unknown" : priceLabels(cur)[PRICE_EDGES.filter(e => p >= e).length];

const TOG=[
 ['unowned','Unowned','Nobody on Discogs owns this release yet',d=>d.h===0],
 ['only','Only seller','Nobody else on Discogs is selling this release',d=>d.only===true],
 ['fo','Format only','Every version of this album is in this format (e.g. a CD-only release)',d=>d.fo===true],
 ['scarce','Scarce','3 or fewer copies for sale on all of Discogs',d=>d.fs!=null&&d.fs<=3],
 ['hidewide','Hide widely pressed','Hide albums with '+WIDE+'+ versions (reissues, represses, other formats)',d=>d.wide!==true],
 ['nosingle','Hide singles','Hide singles and maxi-singles',d=>!/\b(Single|Maxi)\b/.test(d.f)],
 ['nopromo','Hide promos','Hide promos, samplers and advance copies',d=>!/\b(Promo|Smplr|Advance)\b/.test(d.f)],
];
const tog=new Set(), open=new Set(), showAll={};
let sortK=null,sortD=1,limit=100;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const $=id=>document.getElementById(id);

function pass(d,skip){
  for(const [k,,,fn] of TOG) if(tog.has(k)&&!fn(d)) return false;
  if(skip!=='genre'&&sel.genre.size&&!d.gk.some(x=>sel.genre.has(x))) return false;
  if(skip!=='style'&&sel.style.size&&!d.sty.some(x=>sel.style.has(x))) return false;
  if(skip!=='country'&&sel.country.size&&!sel.country.has(d.ck)) return false;
  if(skip!=='price'&&sel.price.size&&!sel.price.has(d.pk)) return false;
  if(skip!=='year'&&sel.year.size&&!sel.year.has(d.ys)) return false;
  if(skip!=='decade'&&!sel.year.size&&sel.decade.size&&!sel.decade.has(d.dec)) return false;
  return true;
}
function counts(key,skip,filter){
  const m=new Map();
  for(const d of D){ if(!pass(d,skip)||(filter&&!filter(d))) continue;
    for(const v of [].concat(d[key])) if(v) m.set(v,(m.get(v)||0)+1); }
  return m;
}
function chips(id,group,m,{order='count',cap=0}={}){
  let e=[...m.entries()];
  for(const v of sel[group]) if(!m.has(v)) e.push([v,0]);
  e.sort(Array.isArray(order)?(a,b)=>order.indexOf(a[0])-order.indexOf(b[0]):order==='name'?(a,b)=>a[0].localeCompare(b[0]):(a,b)=>b[1]-a[1]||a[0].localeCompare(b[0]));
  const last=v=>v===NL||v==='Unknown';
  let main=e.filter(([v])=>!last(v));const tail=e.filter(([v])=>last(v));
  const over=cap?main.length-cap:0;
  if(over>0&&!showAll[group]) main=main.slice(0,cap);
  $(id).innerHTML=main.concat(tail).map(([v,n])=>`<button class="chip${sel[group].has(v)?' on':''}" data-g="${group}" data-v="${esc(v)}">${esc(v)}<i>${n.toLocaleString()}</i></button>`).join('')
    +(over>0?`<button class="chip more" data-more="${group}">${showAll[group]?'fewer':'+'+over+' more'}</button>`:'');
}
function render(){
  $('toggles').innerHTML=TOG.map(([k,l,tip])=>`<button class="chip${tog.has(k)?' on':''}" data-t="${k}" title="${esc(tip)}">${l}</button>`).join('');
  chips('genre','genre',counts('gk','genre'));
  if(sel.genre.size){chips('style','style',counts('sty','style',d=>d.gk.some(x=>sel.genre.has(x))),{cap:20});$('style').hidden=false}
  else{sel.style.clear();$('style').hidden=true}
  chips('country','country',counts('ck','country'),{cap:10});
  chips('price','price',counts('pk','price'),{order:priceLabels(D[0]?D[0].cur:'USD')});
  chips('decade','decade',counts('dec','decade'),{order:'name'});
  if(sel.decade.size){chips('year','year',counts('ys','year',d=>sel.decade.has(d.dec)),{order:'name'});$('year').hidden=false}
  else{sel.year.clear();$('year').hidden=true}
  for(const id of ['genre','country']) $(id).parentElement.hidden=!$(id).children.length;

  let rows=D.filter(d=>pass(d));
  if(sortK) rows.sort((a,b)=>{const x=a[sortK],y=b[sortK];return (typeof x==='string'?x.localeCompare(y):x-y)*sortD});
  $('count').textContent=rows.length.toLocaleString()+' of '+D.length.toLocaleString()+' items';
  $('tb').innerHTML=rows.slice(0,limit).map(row).join('');
  $('more').hidden=rows.length<=limit; $('more').textContent='Show more ('+(rows.length-limit).toLocaleString()+' left)';
  document.querySelectorAll('th[data-k]').forEach(th=>th.textContent=th.textContent.replace(/ [↑↓]$/,'')+(th.dataset.k===sortK?(sortD>0?' ↑':' ↓'):''));
}
function money(v,cur){return v==null?'–':cur==='USD'?'$'+Number(v).toFixed(2):Number(v).toFixed(2)+' '+cur}
function whBar(d){return d.tot?`<span class="wh" title="${d.w} want · ${d.h} have"><b><i style="width:${(100*d.w/d.tot).toFixed(0)}%"></i></b>${d.h?'<span>'+d.r.toFixed(2)+'</span>':'<span class="u">unowned</span>'}</span>`:'–'}
function ratio(d){return d.h?d.r.toFixed(2):d.w?'∞':'–'}
function row(d){
  const o=open.has(d.i);
  let h=`<tr class="m" data-i="${d.i}"><td class="car">${o?'▾':'▸'}</td><td>${esc(d.a)}</td><td><a href="${esc(d.lu)}" target="_blank">${esc(d.t)}</a></td>
<td class="n">${d.y||'–'}</td><td class="n">${money(d.p,d.cur)}</td><td class="n">${d.h}</td><td class="n">${d.w}</td><td class="n hide-s">${whBar(d)}</td><td class="n">${d.g.toFixed(2)}</td></tr>`;
  if(o){
    const f=[['Format',d.f],['Label',d.l+(d.c?' · '+d.c:'')],
      ['Condition',d.cond],['Haves + wants',d.tot]];
    if(d.fs!=null) f.push(['For sale on Discogs',d.only?'only here':d.fs+(d.low!=null?' · lowest '+d.low+' '+d.cur:'')]);
    if(d.ver!=null) f.push(['Album',d.ver===1?'single version':d.ver+' versions'+(d.wide?' (widely pressed)':'')]);
    if(d.fo!=null) f.push(['Format only',d.fo?'yes, '+d.fon+' only':'no']);
    if(d.gen.length) f.push(['Genre',d.gen.join(', ')+(d.sty.length?' · '+d.sty.join(', '):'')]);
    if(d.ct) f.push(['Country',d.ct]);
    h+=`<tr class="d"><td></td><td colspan="8"><dl>${f.filter(x=>x[1]!=='').map(([k,v])=>`<dt>${k}</dt><dd>${esc(v)}</dd>`).join('')}
<dt>Links</dt><dd><a href="${esc(d.lu)}" target="_blank">listing</a> · <a href="${esc(d.ru)}" target="_blank">release</a></dd></dl></td></tr>`;
  }
  return h;
}
document.addEventListener('click',e=>{
  const c=e.target.closest('button.chip,th[data-k],tr.m,#more'); if(!c||e.target.closest('a')) return;
  if(c.id==='more'){limit+=100}
  else if(c.dataset.t){tog.has(c.dataset.t)?tog.delete(c.dataset.t):tog.add(c.dataset.t);limit=100}
  else if(c.dataset.more){showAll[c.dataset.more]=!showAll[c.dataset.more]}
  else if(c.dataset.g){const s=sel[c.dataset.g],v=c.dataset.v;s.has(v)?s.delete(v):s.add(v);limit=100}
  else if(c.dataset.k){const k=c.dataset.k;if(sortK===k)sortD=-sortD;else{sortK=k;sortD=(k==='a'||k==='t')?1:-1}}
  else if(c.classList.contains('m')){const i=+c.dataset.i;open.has(i)?open.delete(i):open.add(i)}
  render();
});
render();
</script></body></html>"""
    page = (page.replace("__TITLE__", html.escape(title)).replace("__WIDE__", str(wide))
            .replace("__DATA__", payload))
    path.write_text(page, encoding="utf-8")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# --------------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description="Find the obscure stuff in a Discogs shop.")
    ap.add_argument("url", help="Discogs seller URL (or just the seller's username)")
    ap.add_argument("--format", help="override format filter, e.g. CD, CDr, Vinyl, Cassette")
    ap.add_argument("--max-total", type=int, help="only keep items with haves + wants ≤ this")
    ap.add_argument("--max-haves", type=int, help="only keep items with at most this many haves")
    ap.add_argument("--min-wants", type=int, default=0, help="only keep items with at least this many wants")
    ap.add_argument("--sort", choices=["obscure", "gem", "ratio", "wants"], default="obscure",
                    help="obscure = lowest haves + wants first (default); gem = obscure-but-wanted first; "
                         "ratio = highest want/have first; wants = most wanted first")
    ap.add_argument("--deep", "--market", dest="deep", default="100", metavar="N|all",
                    help="check the top N items in the chosen sort: copies for sale on Discogs, "
                         "only-seller, versions / widely pressed, format-only, genre, country "
                         "(default 100, 0 = skip, all = everything; 1 request per item + 1 per album, cached)")
    ap.add_argument("--info", metavar="N|all",
                    help="look up genre, style and release country for the top N items (1 request each, "
                         "cached for good). Deep-checked items always get it. Default: 0, or 500 when "
                         "filtering by --genre/--style/--country")
    ap.add_argument("--genre", help="only keep these genres, comma separated (e.g. 'Rock,Electronic')")
    ap.add_argument("--style", help="only keep these styles, comma separated (e.g. 'Noise,Black Metal')")
    ap.add_argument("--country", help="only keep releases from these countries (e.g. 'US,Japan')")
    ap.add_argument("--wide", type=int, default=10, metavar="N",
                    help="call an album widely pressed when it has N or more versions (default 10)")
    ap.add_argument("--top", type=int, default=25, help="how many to print in the terminal")
    ap.add_argument("--token", default=os.environ.get("DISCOGS_TOKEN"), help="Discogs personal token")
    ap.add_argument("--refresh", action="store_true", help="ignore cached inventory")
    args = ap.parse_args()

    seller, url_format, query = parse_seller_url(args.url)
    fmt = args.format if args.format is not None else url_format
    client = Client(args.token)
    if not args.token:
        log("No DISCOGS_TOKEN set — running at the anonymous 25 req/min limit.")

    listings = fetch_inventory(client, seller, query, args.refresh)
    items = [dict(l) for l in listings if matches_format(l["format"], fmt)]
    log(f"{len(items):,} of {len(listings):,} listings match format {fmt or '(any)'}")
    # copies of each release this seller has (across all formats), for the only-seller check
    copies_here = Counter(l["release_id"] for l in listings)

    for it in items:
        it["total"] = it["haves"] + it["wants"]
        it["want_have_ratio"] = round(it["wants"] / it["haves"], 3) if it["haves"] else ""
        it["gem"] = gem_score(it["haves"], it["wants"], it["price"])
        it["release_url"] = f"https://www.discogs.com/release/{it['release_id']}"
        it["for_sale_on_discogs"] = ""
        it["only_seller"] = ""
        it["market_low"] = None
        it["versions"] = ""
        it["widely_pressed"] = ""
        it["format_only"] = ""
        it["format_only_name"] = ""
        it["genres"] = ""
        it["styles"] = ""
        it["country"] = ""

    if args.max_total is not None:
        items = [i for i in items if i["total"] <= args.max_total]
    if args.max_haves is not None:
        items = [i for i in items if i["haves"] <= args.max_haves]
    items = [i for i in items if i["wants"] >= args.min_wants]

    sort_keys = {
        "obscure": lambda i: (i["total"], i["haves"]),
        "gem": lambda i: (-i["gem"], i["total"]),
        "ratio": lambda i: (-ratio_sort_value(i), i["total"]),
        "wants": lambda i: (-i["wants"], i["haves"]),
    }
    items.sort(key=sort_keys[args.sort])

    def apply_info(info: dict[int, dict]) -> None:
        for it in items:
            r = info.get(it["release_id"])
            if r:
                it["_info"] = True
                it["genres"] = "; ".join(r["genres"])
                it["styles"] = "; ".join(r["styles"])
                it["country"] = r["country"]

    # Genre / style / country: one request per release, so only the top --info N get it.
    # With a genre/style/country filter, look up more so the filter has something to bite on.
    filtering = bool(args.genre or args.style or args.country)
    if args.info == "all":
        info_n = len(items)
    elif args.info is not None:
        info_n = int(args.info)
    else:
        info_n = 500 if filtering else 0
    rel_file = HERE / "cache" / "releases.json"
    if rel_file.exists():   # anything looked up on earlier runs is free
        apply_info({int(k): v for k, v in json.loads(rel_file.read_text()).items()})
    if info_n and items:
        apply_info(fetch_release_info(client, list(dict.fromkeys(
            i["release_id"] for i in items[:info_n]))))

    if filtering:
        def has(field: str, wanted: str | None, it: dict) -> bool:
            if not wanted:
                return True
            values = [v.strip().lower() for v in it[field].split(";") if v.strip()]
            terms = [w.strip().lower() for w in wanted.split(",") if w.strip()]
            if field == "country":   # exact, so "US" doesn't match "Russia"
                return any(t in values for t in terms)
            # partial, so "Folk" finds "Folk, World, & Country" and "Metal" finds "Black Metal"
            return any(t in v for t in terms for v in values)
        known = [i for i in items if i.get("_info")]
        unknown = len(items) - len(known)
        items = [i for i in known if has("genres", args.genre, i) and has("styles", args.style, i)
                 and has("country", args.country, i)]
        log(f"{len(items):,} items match the genre/style/country filter"
            + (f" ({unknown:,} items further down weren't looked up — raise --info or use --info all)"
               if unknown else ""))

    # Deep check: one release lookup (for sale, genre, country, album) plus one lookup per album.
    deep_n = len(items) if args.deep == "all" else int(args.deep)
    checked = 0
    if deep_n and items:
        ids = list(dict.fromkeys(i["release_id"] for i in items[:deep_n]))
        log(f"Checking the top {deep_n:,} (first run up to ~{len(ids) * 1.6 * client.min_interval / 60:.0f} min; cached afterwards)")
        info = fetch_release_info(client, ids, fresh=True)
        apply_info(info)
        albums = fetch_albums(client, info)
        for it in items:
            rid = it["release_id"]
            r = info.get(rid)
            if r and r.get("num_for_sale") is not None:
                it["for_sale_on_discogs"] = r["num_for_sale"]
                it["only_seller"] = r["num_for_sale"] <= copies_here[rid]
                it["market_low"] = r["lowest"]
            if rid in albums:
                a = albums[rid]
                target = canon_format(fmt) if fmt else primary_format(it["format"])
                it["versions"] = a["versions"]
                it["widely_pressed"] = a["versions"] >= args.wide
                it["format_only"] = is_format_only(a, target)
                it["format_only_name"] = {"cd": "CD", "cdr": "CDr", "vinyl": "Vinyl", "cassette": "Cassette"}.get(target, target)
                checked += 1

    out_dir = HERE / "output"
    out_dir.mkdir(exist_ok=True)
    stem = f"{seller}_{slug(fmt) or 'all'}" + (f"_{slug(query)}" if query else "")
    csv_path, html_path = out_dir / f"{stem}.csv", out_dir / f"{stem}.html"
    write_csv(items, csv_path)
    write_html(items, html_path, f"{seller} · {fmt or 'all formats'}", args.wide)

    print(f"\nTop {min(args.top, len(items))} ({args.sort}):")
    print(f"{'total':>5} {'haves':>5} {'wants':>5} {'w/h':>7} {'gem':>5} {'sale':>4} {'vers':>4}  "
          f"{'price':>7}  item")
    for it in items[: args.top]:
        sale = "" if it["for_sale_on_discogs"] == "" else str(it["for_sale_on_discogs"])
        album = str(it["versions"])
        flags = flags_text(it)
        print(f'{it["total"]:>5} {it["haves"]:>5} {it["wants"]:>5} {ratio_text(it):>7} {it["gem"]:>5.2f} '
              f'{sale:>4} {album:>4}  {it["price"] or "":>7}  {it["artist"]} – {it["title"]} '
              f'[{it["format"]}]'
              + "".join(f" · {x}" for x in (it["genres"].split(";")[0], it["country"]) if x)
              + (f"  ◆ {flags}" if flags else ""))
    if checked:
        only = sum(1 for i in items if i["only_seller"] is True)
        fo = sum(1 for i in items if i["format_only"] is True)
        wide = sum(1 for i in items if i["widely_pressed"] is True)
        print(f"\nOf {checked} checked items: {only} sold only by {seller}, "
              f"{fo} format-only releases, {wide} widely pressed.")
    print(f"\nWrote {csv_path}\n      {html_path}\n({client.calls} API calls)")


if __name__ == "__main__":
    main()
