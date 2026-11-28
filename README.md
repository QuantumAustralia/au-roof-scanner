# AU Roof Scanner

Pilot tooling for an Australian roofing-lead system. It pulls a satellite tile for each dwelling, asks Gemini to grade the roof in two passes, and writes a mail-out list with an indicative AUD range.

The first pilot is **Mermaid Waters and the rest of postcode 4218**, on the Gold Coast in South East Queensland. Addresses outside 4218 are refused unless you pass `--allow-any-postcode`.

The scanner does not identify asbestos, and it does not produce a quotation.

## Layout

| Path | Purpose |
| --- | --- |
| `scanner/fetch_tiles.py` | Downloads a 600×600 satellite tile (`zoom=21`, `scale=2`, `maptype=satellite`), then keeps the centre 85% |
| `scanner/grade_roofs.py` | Two-pass Gemini triage, yellow target box, mail-out CSV, optional Supabase push |
| `scanner/pricing.py` | Indicative GST-inclusive ranges from roof area, material, and wear |
| `input_addresses.csv` | Sample leads: five Mermaid Waters addresses |
| `data/tiles/` | `{lead_id}.png` working tiles: the centre 85% of the zoom-21 download |
| `data/annotated/` | `{lead_id}.png` with a yellow defect box, only when pass-2 confidence is at least 0.92 |
| `data/enhanced/` | `{lead_id}_enhanced.png`, a local Real-ESRGAN upscale of the yellow-box image. The tile and the annotated file are left unchanged |
| `data/grades/` | Full JSON for every graded lead |
| `data/cleared_for_mailout.csv` | Leads that survived the false-positive check |
| `data/logs/static_maps_usage.jsonl` | One line per Static Maps attempt. The API key is not logged |

`scale=2` asks Google for a 600×600 map at zoom 21, rendered at double resolution (about 1200×1200). The working tile keeps the centre 85% of that frame, and that cropped image is what both passes see.

## Setup

Python 3.11 or newer.

```powershell
cd $HOME\au-roof-scanner
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env`:

- `GOOGLE_MAPS_API_KEY` — Maps Static API enabled, billing on, key restricted.
- `GEMINI_API_KEY` — Gemini API key. `GEMINI_MODEL` defaults to `gemini-2.5-flash`.
- Supabase variables are only needed for `--push-supabase`.

Each successful Static Maps response is a billable request. Tiles already in `data/tiles/` are skipped unless you pass `--force`. Google’s terms limit how long map images may be stored; check the current Maps Platform terms before you keep a campaign archive.

Roof photos are sent to Gemini. Keep the lead file and the keys on a machine you trust.

## Running the scanner

Run both commands from the project directory, after the virtual environment is active and `.env` is filled in.

**1. Fetch the satellite tiles**

```powershell
python -m scanner.fetch_tiles --input input_addresses.csv
```

Useful flags:

- `--force` — download again and overwrite `data/tiles/{lead_id}.png`
- `--delay 0.5` — pause between requests
- `--allow-any-postcode` — accept leads outside 4218

The command logs each attempt to `data/logs/static_maps_usage.jsonl` with `billed` set to true or false, then prints how many tiles were saved, skipped, or failed.

Optional columns `roof_centroid_lat` and `roof_centroid_lon` centre the tile on the roof. For production runs, write the Geoscape building-footprint centroid into those two columns. If they are missing or blank, the fetcher keeps using `lat` and `lon`.

**2. Grade the roofs**

```powershell
python -m scanner.grade_roofs --input input_addresses.csv
```

Pass 1 returns structured JSON:

- `material` — `concrete_tile`, `terracotta_tile`, `colorbond_metal`, `corrugated_iron`, or `other`. Older grades may still say `metal` or `unclear`.
- `wear_score` — 1 (sound) to 5 (replacement likely)
- `repair_likelihood` — 1 to 5, separate from wear
- `cleaning_score` — 1 (no clean needed) to 5 (surface largely covered)
- `primary_cleaning_defect` — `heavy_moss_lichen`, `algae_staining`, `dirt_grime_soiling`, `organic_growth`, or `none`
- `primary_defect`
- `confidence` — 0 to 1

Cleaning is scored separately from structural wear. Moss, algae, dirt, and organic growth raise `cleaning_score`. They raise wear or repair likelihood only when the growth is damaging the roof. `needs_cleaning` is true when `cleaning_score` is 3 or higher.

Pass 2 runs only when `wear_score` is 3 or higher. It filters false positives from trees, solar panels, skylights, and rainwater tanks. A lead is cleared for mail-out only when the second pass accepts a real defect. The row then includes:

- `verified_defect`
- `pass2_confidence`
- `defect_box` — `ymin,xmin,ymax,xmax` on a 0–1000 grid
- `summary` — plain English for a homeowner
- `price_low_aud` and `price_high_aud`
- `occupancy_type` — `owner_occupier`, `rental`, or `unknown`. New leads are `unknown`
- `occupancy_source` — `default` until someone sets the value, then `manual`
- `high_res_requested` — `false` until you flag the lead for paid imagery

When `pass2_confidence` is at least **0.92** and the box is large enough to draw, the tile is copied to `data/annotated/{lead_id}.png` with a yellow rectangle on the defect. The original tile is not altered. That annotated image is then upscaled locally with Real-ESRGAN to `data/enhanced/{lead_id}_enhanced.png`. Edit `upscale_factor` in `enhance_config.json` to change the scale. If Real-ESRGAN cannot run, the older Pillow sharpen is used instead and a warning is logged.

**3. Optional Supabase push**

Create the table (SQL editor in Supabase), then:

```powershell
python -m scanner.grade_roofs --input input_addresses.csv --push-supabase
```

```sql
create table if not exists cleared_mailout (
  lead_id text primary key,
  street_address text,
  suburb text,
  state text,
  postcode text,
  lat double precision,
  lon double precision,
  roof_area_m2 numeric,
  material text,
  wear_score integer,
  primary_defect text,
  verified_defect text,
  pass2_confidence numeric,
  summary text,
  defect_box text,
  price_low_aud integer,
  price_high_aud integer,
  pricing_basis text,
  tile_path text,
  annotated_path text,
  occupancy_type text,
  occupancy_source text,
  high_res_requested boolean
);
```

The service-role key bypasses row-level security. Use it only for this batch job. Reruns upsert on `lead_id`.

## Sample file

`input_addresses.csv` has five Mermaid Waters addresses in 4218. Coordinates are OpenStreetMap geocodes for those street addresses. Roof areas are illustrative square metres so the pricing step has something to multiply; replace them with measured areas before a live campaign.

Required columns: `lead_id`, `street_address`, `suburb`, `state`, `postcode`, `lat`, `lon`, `roof_area_m2`.

Optional columns: `roof_centroid_lat`, `roof_centroid_lon`. For a production list, put the Geoscape building-footprint centroid in that pair so the satellite image is centred on the roof. The sample file does not include them, and existing files with only `lat`/`lon` still run.

## Indicative pricing

`scanner/pricing.py` multiplies roof area by a per-square-metre band for the material and wear score, then rounds to the nearest $50. Wear 1 and 2 do not get a price, because those roofs are not cleared. The basis string on each row states that the figure includes GST and is not a quotation.

Change the rate table when you have Gold Coast contractor prices. The published bands are a starting point for the pilot, not a price list.

## Repair quotes

Quotes are minor-repair estimates, not full roof replacements. A quote starts from the configured call-out (default $500) and adds the material rate for only the worn portion of the roof: 10% at wear 3, 18% at wear 4, and 30% at wear 5. The result is never below the configured minimum.

Edit `pricing_config.json` to change the call-out, minimum, material rates, and repair fractions. `python -m scanner.quote --input data/ashmore_cleared_for_mailout.csv` reads that file at runtime and writes `data/quotes.csv`. Quote rows copy `occupancy_type` and `occupancy_source` from the cleared mail-out.

## Occupancy

`occupancy_type` is `owner_occupier`, `rental`, or `unknown`. New cleared leads are `unknown`, with `occupancy_source` set to `default`. Automatic population will come from Geoscape or other property data later.

Until then, edit either CSV directly, or update a lead in place:

```powershell
python -m scanner.occupancy --lead-id ASH-0001 --occupancy owner_occupier --file data\ashmore_cleared_for_mailout.csv --file data\quotes.csv
```

That writes `occupancy_source` as `manual`. A later grade or quote run keeps a value already saved for that lead.

## High-resolution imagery

`high_res_requested` marks a ranked or cleared lead for a later Nearmap pull, or another paid imagery source. New leads are `false`. This command only sets the flag. It does not call a paid API.

```powershell
python -m scanner.flag_highres --lead-id ASH-0001 --file data\ashmore_cleared_for_mailout.csv
python -m scanner.flag_highres --lead-id ASH-0001 --file data\quotes.csv --unflag
```

Use the cleared mail-out CSV or the ranked quote CSV (`data/quotes.csv`). A later grade or quote keeps a `true` flag already saved for that lead.

## G-NAF addresses

The November 2026 extract lives in `data/gnaf`, under `G-NAF/G-NAF NOVEMBER 2026/Standard`. Principal street addresses for one postcode are written from those pipe-separated files. Roof area is a placeholder of 200 m² because G-NAF does not include it. Nothing is downloaded.

```powershell
python -m scanner.load_gnaf --postcode 4218 --output data\addresses_4218.csv
```

The dashboard has a **Load addresses** button that runs the same command. **Start scan** can then use that CSV with the existing tile fetch and roof grade.

## Dashboard

Start the local page from the project directory:

```powershell
python -m scanner.dashboard
```

Open http://127.0.0.1:5000 in a browser on this machine. There is no login.

**Load addresses** reads the local G-NAF extract only. **Start scan** asks you to confirm how many addresses will be scanned. Leave **Scan entire postcode** unticked and enter a number in **Maximum addresses** to scan only the first addresses that are not locked. Tick the box to scan the whole postcode. Addresses already scanned are skipped for the lockout period.

The default lockout is 12 months. Change `lockout_months` in `scan_config.json`, or set `SCAN_LOCKOUT_MONTHS` in `.env`. Dates are kept in `data/scan_history.json`.

The table starts on **Match: Either**, with both thresholds at 3 and **Roof material** on All. Either keeps a lead that meets the repair/wear threshold or the cleaning threshold. Repair / Wear keeps the repair side only. Cleaning keeps the cleaning side only. Roof material lists All, then concrete tile, terracotta tile, colorbond metal, corrugated iron, and other, plus older stored values `metal` and `unclear`. **Reset filters** returns the thresholds, match, material, occupancy, postcode, and Show ALL to those starting values. **Show ALL** ignores the two thresholds and still applies material, postcode, and occupancy.

**Export Repair Leads** downloads a contractor CSV for the repair threshold and the selected material. **Export Cleaning Leads** does the same for the cleaning threshold. **Export selected for roofer** is unchanged: it zips the rows you tick.

**CLUSTERS** opens `data/clusters`. Repair/wear leads (cleared for mail-out) and cleaning leads (cleaning score 3 or more) are separate walking groups. Each folder is the suburb abbreviation, a number, and the work type, such as `BRO-01-repair` or `BRO-01-cleaning`. The same labels are in each folder's `summary.csv`.

Roofs graded before cleaning scores existed show a dash in the Cleaning column until they are re-graded. This reads the saved tile and does not download imagery:

```powershell
python -m scanner.regrade_saved --limit 2
python -m scanner.regrade_saved --lead GANSW704689557
```

Omit `--limit` and `--lead` to re-grade every saved tile. That calls Gemini once or twice per roof and replaces the cleaning, wear, and repair scores for those leads. Other mail-out rows are kept.

Use the checkboxes or **Select all repairable**, and keep the high-res flag and status controls. **Export selected for roofer** downloads a ZIP. Each image shows the roof and any yellow repair box, with the suburb on the image and in the file name. The street address is not included. Enter an email address as well if you want a copy sent; that needs `SMTP_HOST` and `SMTP_FROM` in `.env`.

## Marketplace

The public page is on the same app as the scanner dashboard: http://127.0.0.1:5000/marketplace. There is no contractor login. Only leads you publish are shown. Each tile shows the rooftop image, the suburb, the price exclusive of GST, the category (repair/wear or cleaning), and the wear score. The street address is omitted until payment.

Default prices for wear/repair scores 2 to 5, cleaning scores 2 to 5, the listing length (30 days), and the minimum order (10 leads) are in `marketplace_config.json`. Edit them in the **Marketplace** panel on the dashboard. A price override on publish, or on a live row, replaces the score default for that lead. Every price, the checkout total, and the email say the amount is exclusive of GST.

Tick one or more rows and choose **Publish selected**. Live rows show the expiry date and the price. When the listing date passes, the lead leaves the public page and appears in **Archive list**. **Re-list selected** starts the timer again.

The cart is stored in the browser. The cart and the checkout page say **Minimum order is 10 leads** when the buyer is short, and the checkout button stays disabled until the minimum is met. The server refuses a payment under that number as well. Change the number in the dashboard; the pages use the saved value.

Checkout uses Stripe when `STRIPE_SECRET_KEY` is set in `.env`. The charge is the listed ex-GST amount in Australian dollars. With the key blank, **Test payment** marks the order paid locally so you can check the email. After payment, the buyer is emailed the street address, suburb, category, wear score, and price. That email uses the same `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, and `SMTP_FROM` settings as the mail-out. If mail is not configured, the addresses are shown on the payment page instead.

The marketplace and the checkout page both carry this notice: there are no warranties or guarantees on image accuracy or roof condition; the pictures are third-party satellite imagery and their age, currency, and accuracy cannot be guaranteed; leads are information only and buyers should inspect the roof themselves.

Local test:

```powershell
python -m scanner.dashboard
```

Open http://127.0.0.1:5000, publish at least the minimum number of leads, then open http://127.0.0.1:5000/marketplace. Add them to the cart, continue to checkout, and use **Test payment** while `STRIPE_SECRET_KEY` is empty. Put a Stripe test secret key in `.env` and restart the dashboard when you want the real Checkout page.

## Address verification

When `python -m scanner.grade_roofs` finishes, it writes `data/verification_report.csv` and `data/verification_report.html` for every lead graded in that run. A lead that is already in the CSV keeps its status. A new lead is `pending_review`.

Open the HTML report in a browser:

```powershell
start data\verification_report.html
```

Each row has a Google Maps link for the graded latitude and longitude. Check the address, then set the status to `approved` or `rejected` before a mail-out. The next grade refreshes the scores and map link and leaves an existing status as you set it.

Start the review page from the project directory:

```powershell
python dashboard/app.py
```

Open http://127.0.0.1:8080 . The port comes from `APP_PORT` in `.env`. The table reads `data/verification_report.csv`. Changing a status saves that file straight away. **Download approved leads** writes `data/approved_for_mailout.csv` and downloads the same file. It contains only rows marked approved. Lightsail steps are in `deploy/README.md`.

## Limits

- Satellite imagery misses underside damage, flashing detail, and anything under trees or shade.
- A high confidence score is still not an inspection.
- Neighbouring roofs, pools, and driveways are out of scope, but the model can still be wrong. Review annotated images before anyone is contacted.
