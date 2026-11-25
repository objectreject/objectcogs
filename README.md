# objectcogs

Finds the obscure, wanted and underpriced records in a Discogs seller's shop.

## Web app (`index.html`)

Open the page, paste a seller link (CD, Vinyl, Cassette or any other format from the shop's filter), and press **Scan**. Everything runs in your browser, which talks straight to the Discogs API. There's no server and no install.

A scan runs in two parts:

1. **Quick view:** the whole shop is downloaded and the table appears with Year, Price, Haves, Wants, Want/Have and Gem, plus the Unowned, Hide singles and Hide promos toggles and the price and year filters.
2. **Full check:** every record in the format is checked for only seller, scarce, format only, widely pressed, genre and country, with a progress bar. Best gems go first, so the top of the table fills in early. The other filters appear when it's done.

- **Stop** keeps everything fetched so far. **Finish the full check** picks up where it stopped, and so does scanning the same link again.
- **Saved:** everything goes into the browser's storage (IndexedDB). Re-scanning a shop is instant, and records already checked in another format aren't checked again. For-sale counts refresh after a week.
- **Leaving mid-scan:** the browser asks first. An interrupted shop download resumes where it left off.
- **Shareable links:** the link to the page includes the shop link (e.g. `…/#https://www.discogs.com/seller/x/profile?format=Vinyl`), and opening it starts the scan right away.
- **Token:** Settings has a field for your optional Discogs token. It's stored only in that browser and makes scans about 2.5× faster.

### Hosting it on GitHub Pages

1. Push this folder to a GitHub repo.
2. Go to the repo's **Settings → Pages**, choose **Deploy from a branch**, pick `main`, folder `/ (root)`, and save.
3. After a minute the app is at `https://<your-username>.github.io/<repo-name>/`.

Each visitor's browser makes its own Discogs requests under its own rate limit, so the site costs nothing to run however many people use it.

To try it locally first, run `python3 -m http.server 8000` in this folder and open http://localhost:8000. Opening the file directly also works in most browsers.

## Command-line version (`objectcogs.py`)

The same scan as a Python script that writes an HTML report and CSV to `output/`. It's handy for scripting or very long runs.

```bash
python3 objectcogs.py "https://www.discogs.com/seller/armageddonshop/profile?q=&format=CD"
```

Requires Python 3.9+ and nothing else (standard library only). Paste any seller URL. The `format=` and `q=` values in the URL are used as filters. Results go to:

- `output/<seller>_<format>.html`: the report (see below).
- `output/<seller>_<format>.csv`: every column, for a spreadsheet.
- The terminal: the top 25.

## The report

The table shows Artist, Title, Year, Price, Haves, Wants, Want/Have and Gem. Click a column header to sort, and click a row to see the rest: format, label, condition, for sale, versions, format-only, genre and country.

Filters, all clickable chips:

- **Show:**
  - **Unowned:** nobody on Discogs owns it yet.
  - **Hide singles / Hide promos:** based on the listing's format.
  - **Only seller:** nobody else on Discogs is selling this release.
  - **Format only:** every version of the album is in this format, e.g. a CD-only release.
  - **Scarce:** 3 or fewer copies for sale on all of Discogs.
  - **Hide widely pressed:** hides albums with 10 or more versions (reissues, represses, other formats), e.g. an overlooked reissue of a famous record.
- **Genre:** pick one or more. The styles inside them appear underneath.
- **Country:** the release country, most common first.
- **Price:** pills for under $5, $5–10, $10–20, $20–50 and $50+ (in the seller's currency), or type any min and max. Clicking a second pill stretches the range to cover both, and clicking the only selected pill clears it.
- **Year:** the same, with a pill per decade, or type any from and to year. (The command-line report still uses decade and year chips.)

Chip counts update as you filter. In the web app, Only seller, Format only, Scarce, Hide widely pressed, Genre and Country appear once the full check is done. In the command-line report they only see items that were looked up (see the `--deep` and `--info` options).

## What the columns mean

| column | meaning |
|---|---|
| haves | Discogs users with this pressing in their collection |
| wants | users with it on their wantlist |
| **total** | **haves + wants for this pressing. Lower = more obscure** |
| want/have | wants ÷ haves. Above 1 means more people want it than own it. The bar is red for wants, green for haves. "unowned" = nobody owns it but someone wants it |
| **gem** | **obscure, wanted *and* cheap, in one number. Higher = better find** (see below) |
| for sale | copies of this release for sale on all of Discogs, plus the lowest price. **ONLY HERE** = no other seller has it |
| versions | how many versions the album (its Discogs "master") has. **Widely pressed** = 10 or more (change with `--wide`). "single version" = this is the only one |
| only | **CD ONLY** (or VINYL ONLY, etc.) = every physical version of the album is in this format. Digital versions are ignored |
| genre / style | Discogs genres, with styles underneath |
| country | country the release came out in |

Haves, wants and total come with the shop download. The other columns come from the full check (or, on the command line, `--deep` and `--info`).

### Gem score

```
gem = wants / (haves + 1)
      / log10(activity + 10)
      / (log10(price + 10) / log10(20))
```

1. **Demand per owner:** wants ÷ (haves + 1).
2. **Obscurity:** `activity` is haves + wants for this pressing. A quiet reissue of a famous record can still score well, so use **Hide widely pressed** to drop those.
3. **Price:** a $10 copy is neutral. Cheaper copies get a gentle boost and pricier ones a gentle penalty: $2 ≈ ×1.21, $25 ≈ ×0.84, $50 ≈ ×0.73, $100 ≈ ×0.63. The scale uses the seller's currency.

Anything with 0 wants scores 0. Examples at $10, single-version releases:

| haves | wants | gem | |
|---|---|---|---|
| 0 | 3 | 2.69 | nobody owns it, three people want it |
| 5 | 10 | 1.19 | scarce and in demand |
| 200 | 400 | 0.71 | wanted, but well known |
| 3 | 1 | 0.22 | obscure, little demand |
| 50 | 5 | 0.05 | common, nobody cares |

## Options

```
--sort obscure|gem|ratio|wants
                    obscure = lowest total first (default), gem = highest gem first,
                    ratio = highest want/have first, wants = most wanted first
--deep N|all        check the top N items: for sale / ONLY HERE, versions / widely pressed,
                    format-only, genre, country (default 100, 0 = skip, all = everything)
--info N|all        look up genre, style and country for the top N items (cheaper than --deep)
--genre Rock,Jazz   only keep these genres
--style Noise       only keep these styles
--country US,Japan  only keep releases from these countries
--wide N            versions needed to count as widely pressed (default 10)
--max-total N       only keep items with haves + wants ≤ N
--max-haves N       only keep items owned by ≤ N people
--min-wants N       only keep items with ≥ N wants
--format CDr        override the URL's format (CD, CDr, Vinyl, Cassette, …)
--top N             rows to print in the terminal
--refresh           re-download the inventory instead of using the 6-hour cache
```

Examples:

```bash
python3 objectcogs.py "https://www.discogs.com/seller/armageddonshop/profile?format=CD" --sort gem
```

```bash
python3 objectcogs.py armageddonshop --format Cassette --sort gem --style "Black Metal,Noise" --info 1000
```

```bash
python3 objectcogs.py armageddonshop --format CD --country Japan --info all --deep 200
```

## Speed / token

The script uses the official Discogs API, so it never hits Cloudflare blocks. The cost depends on what you ask for:

| step | requests | anonymous (25/min) | with token (60/min) |
|---|---|---|---|
| download a 15k-item shop | 1 per 100 listings (~150) | ~6 min | ~3 min |
| `--info N` (genre/country) | 1 per item | N × 2.5 s | N × 1 s |
| full check / `--deep N` | 1 per item + 1 per album (~1.6 per item) | 1,000 items ≈ 65 min | 1,000 items ≈ 27 min |

Everything is cached in `cache/`: the inventory for 6 hours, release lookups (for sale, genre, country) for a week, album versions for 30 days. Later runs only fetch what's new, so raising `--deep` or `--info` costs only the new items.

To get a free token:

1. Go to https://www.discogs.com/settings/developers and click **Generate new token**.
2. Run `export DISCOGS_TOKEN=yourtoken` (add it to `~/.zshrc` to keep it).

## Limits

- Discogs only lets you page through the first 10,000 results of another seller's inventory. The script gets around this by walking the list from both ends, and in more sort orders for bigger shops. For huge shops, add a `q=` search to the URL to narrow it.
- The inventory data has no genre or country. Those need one lookup per release, so genre/country filters only see the items that were looked up. Use `--info all` once to cover the whole shop. It's slow the first time (8,000 CDs ≈ 2 h with a token) but cached permanently.
- "ONLY HERE" means every copy on Discogs belongs to this seller, so it still counts if they hold two copies.
- The API has no format filter, so the whole shop is downloaded once and filtered locally. `CD` matches `CD` and `2xCD` but not `CDr`. Use `--format CDr` for those.
