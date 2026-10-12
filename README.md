# GroknMok

GroknMok is a small command-line program that searches the online stock of
independent Canadian and US hobby stores for Games Workshop products.

It works from a published dataset: snapshots of the Games Workshop
catalogues of about 1,300 independent stores (about 1,200 websites), taken
by a separate crawler several times a day. GroknMok downloads that
dataset, keeps it current, and searches it on your own machine. It can
also:

- re-check matches on the stores' own sites (`--live`);
- format results for Discord or Reddit (`--share`);
- send you restock alerts through ntfy (`groknmok watch`).

It uses no AI and no account, and it sends nothing about your searches
anywhere.

Not affiliated with Games Workshop.

## Install (Linux)

You need Python 3.10 or newer as `python3`. Most current distributions
have it; check with `python3 --version`. Nothing else needs installing:
the program is a single file that carries its own dependencies.

1. Download `groknmok.pyz` and `groknmok.pyz.minisig` from the
   [latest release](https://github.com/JustinCreates/GroknMok/releases/latest).
2. Optional: check the signature with
   [minisign](https://jedisct1.github.io/minisign/):

   ```bash
   minisign -Vm groknmok.pyz -P RWT1F5LqVaBmswTuy5Ja33wp9+Em8yaWmW/GVrsmqRzwSaWOPnIIfN9z
   ```

3. Put it on your PATH as `groknmok`:

   ```bash
   mkdir -p ~/.local/bin
   install -m 755 groknmok.pyz ~/.local/bin/groknmok
   groknmok version
   ```

   If `groknmok` is not found, add `~/.local/bin` to your PATH.

The first search downloads the dataset, about 63 MB, and unpacks it to
about 545 MB in `~/.local/share/groknmok/`. Later updates download only
the parts that changed: usually about 17 MB, and the larger catalogue
part about once a day at most.

On a slow connection or a small disk, run `groknmok config core-only on`.
This skips the catalogue part (see "What is in the dataset") and keeps
the download to about 17 MB, unpacked to about 140 MB.

Nothing is installed outside your home directory. To uninstall:

```bash
groknmok timer remove        # only if you installed the timer
rm ~/.local/bin/groknmok
rm -r ~/.config/groknmok ~/.local/share/groknmok
```

## Search

```bash
groknmok "telemon"
```

All words of the query must appear in the product title. Results are
grouped by store, cheapest first: in-stock listings, then a separate
"Preorders" section with release dates where the store gives one. The
locations sharing each store's website are listed beneath it.

Example (abbreviated; store names replaced):

```
  $140.00 CAD  Adeptus Custodes: Telemon Heavy Dreadnought  IN STOCK!  [title match, low confidence]
               example-hobbies.ca  not confirmed live, snapshot 19h old
               - Example Hobbies, Windsor ON

Preorders

  $119.00 CAD  Adeptus Custodes: Telemon Heavy Dreadnought [Oct 17]  PRE-ORDER (release 2026-10-17)  [title match, low confidence]
               example-games.com  not confirmed live, snapshot 19h old
               - Example Games, Toronto ON

Dataset 2026-10-11 18:43 PDT (86 min ago); snapshot data, not live-confirmed (--live checks now).
```

Useful options:

| Option | Effect |
|--------|--------|
| `--country CA` or `--country US` | one country only (default: both) |
| `--province ON,QC` / `--state WA,OR` | only stores there; under `--state`, `CA` is California |
| `--min 50 --max 120` | price range, in each store's own currency |
| `--barcode EAN` | marks a listing whose barcode equals it as a confirmed match |
| `--all` | every status, out of stock and backorder included |
| `--used` | include used stock |
| `--live` | re-check the matches on the stores' sites first (see below) |
| `--csv` | also write every match to `./results/<date>-<time>-<query>.csv` |

Examples:

```bash
groknmok "kill team" --country CA --max 100
groknmok "combat patrol" --state WA,OR
groknmok 99123043012          # a GW catalogue number works as a query
groknmok "telemon" --all --csv
```

US prices also show an approximate Canadian price, at the Bank of Canada
daily rate included in the dataset.

## Share results

```bash
groknmok "telemon" --share            # Discord: messages of at most 2,000 characters
groknmok "telemon" --share reddit     # Reddit: posts of at most 10,000 characters
groknmok "telemon" --share --top 5    # every in-stock result, only the 5 cheapest preorders
```

`--share` prints only a Markdown list, ready to paste: product link,
store, location, price and status. Its heading says whether the results
come from the snapshot or were live-confirmed. It ends with "Always check
the store page before buying."

## How fresh is the data

- **The dataset.** The crawler takes a snapshot of each store's
  Games Workshop catalogue. Most stores are refreshed every night, and
  their newest listings and preorders again every 3 hours. Stores whose
  sites can only be read through a sitemap are refreshed weekly. A new
  dataset is published after each crawler run.
- **Your copy.** GroknMok checks for a new dataset at most every 15
  minutes, when you search or when the timer runs. `groknmok update`
  checks now. `groknmok status` shows the build you have and its age.
- **Each store.** Every result line gives the age of that store's
  snapshot, for example "snapshot 19h old".

**"Not confirmed live"** means the status and price are what the store's
site showed when its snapshot was taken. Stock may have sold out since,
or the price may have changed. Use `--live` to check.

**`--live`** re-checks the matched products on the stores' own sites
before showing them: at most 25 products a search and 2 per store. It is
polite to the stores:

- it obeys each site's robots.txt (user-agent token `groknmok`) and
  Crawl-delay;
- it waits at least 2 seconds between requests to the same site;
- it stops at the first refusal (HTTP 403 or 429);
- it identifies itself as `groknmok/<version>` with a link to this page.

Please use it for the products you mean to buy, not for every search.

**If an update fails** (no network, GitHub unavailable, a bad signature,
a corrupt download), GroknMok keeps the copy you have, says so in one
line, and tries again at the next check. Your searches keep working.

**Title matches are low confidence.** Most matches are by product title,
and a title can name a different product or bundle. Results are marked
"[title match, low confidence]" unless a barcode or catalogue number
confirms them. Always check the store page before buying.

## Restock alerts (watches)

A watch runs a saved search after each new dataset and tells you, through
[ntfy](https://ntfy.sh), when a listing turns in stock, a new preorder
appears, or a price drops into your range.

1. Pick an ntfy topic name that is hard to guess, and subscribe to it in
   the ntfy phone or desktop app.
2. Create `~/.config/groknmok/ntfy.conf` (mode 0600):

   ```bash
   mkdir -p ~/.config/groknmok
   printf '{"server": "https://ntfy.sh", "topic": "%s"}\n' "your-hard-to-guess-topic" > ~/.config/groknmok/ntfy.conf
   chmod 600 ~/.config/groknmok/ntfy.conf
   groknmok watch test
   ```

   A self-hosted ntfy server works too. Add `"token": "..."` if it
   needs one. GroknMok never prints the topic or the token.

3. Add watches:

   ```bash
   groknmok watch add "telemon" --country CA --max 130
   groknmok watch add "combat patrol" --state WA --note "for the club"
   groknmok watch add "telemon" --code 99123043012   # barcode or GW catalogue number
   groknmok watch list
   groknmok watch remove w2           # or --pause to keep it but stop alerts
   groknmok watch run --dry-run       # print what would be sent, send nothing
   ```

How alerts behave:

- A new watch's first run sets a silent baseline: it records what is
  already in stock without alerting. Use `--alert-existing` to be told
  about those too.
- After that, the same listing in the same status never alerts twice in
  a row. If it sells out and comes back, you hear about it again.
- Alerts are judged on the dataset alone. `groknmok config watch-live on`
  re-checks new hits on the stores' sites first, under the same limits
  as `--live`.

### Timer

To run the watches automatically:

```bash
groknmok timer install    # a systemd user timer: every hour
groknmok timer status
groknmok timer remove
```

Each hourly run updates the dataset and runs the watches only when the
dataset changed. Its log is in `journalctl --user -u groknmok-watch.service`.

The timer runs as your user and touches nothing outside your home
directory. It runs only while you are logged in, unless you enable
lingering (`loginctl enable-linger`).

## Updates to the program

```bash
groknmok self-update --check    # is there a newer release?
groknmok self-update            # download, verify, ask, replace
groknmok self-update --rollback # put the previous version back
```

`self-update` installs only a release signed with the GroknMok release
key built into the program. It checks the signature, the checksum and the
version inside. It replaces the program only after you type "y" at a
terminal, and keeps the old version for `--rollback`.

The timer never replaces the program. When a new version comes out, it
sends one ntfy note instead. A search at a terminal also mentions a new
version, at most once a day.

## Other commands

```bash
groknmok update            # check for a new dataset now
groknmok status            # dataset build, age, parts, settings
groknmok config            # show settings
groknmok config core-only on|off
groknmok config watch-live on|off
groknmok version
groknmok -h                # every search option
```

## Files

| Path | What |
|------|------|
| `~/.local/bin/groknmok` | the program |
| `~/.config/groknmok/client.json` | settings |
| `~/.config/groknmok/watch.csv` | your watches |
| `~/.config/groknmok/ntfy.conf` | ntfy settings (0600) |
| `~/.local/share/groknmok/` | the dataset, update state, watch state, the previous program |

`GROKNMOK_CONFIG_DIR` and `GROKNMOK_DATA_DIR` override the two directories.

## Network use

- **GitHub only, by default.** GroknMok contacts github.com for the
  dataset, at most every 15 minutes, and for release checks, at most once
  a day.
- **Store sites, only when you ask.** It contacts stores' sites only for
  `--live` or with `watch-live` on.
- **ntfy, only for watches.** It contacts your ntfy server only to send
  your own alerts.
- No telemetry, no accounts.

## What is in the dataset

For each store:

- the store's name, city, province or state, country and website;
- whether it ships;
- the currency of its site;
- its Games Workshop listings: title, price, stock status, release date
  if listed, barcode or GW catalogue number if the site gives one, and
  link.

There are no addresses, phone numbers, descriptions or images.

The data comes in two parts:

- **core:** stores whose catalogues can be read directly, plus the
  Games Workshop-looking links of sitemap-only stores.
- **catalogue:** the rest of the sitemap-only stores' product links.

The manifest is signed. GroknMok refuses a dataset that does not verify
against the key built into it.

## Dataset notice

Provided for use with GroknMok only; no redistribution. Product facts
belong to the stores. Not affiliated with Games Workshop.

No licence is granted for the dataset beyond that.

## Store owners

[STORES.md](STORES.md) explains what is collected and how, and how to
have your store left out. In short: contact groknmoksupport@gmail.com or open a
["Remove my store" issue](https://github.com/JustinCreates/GroknMok/issues/new?template=remove-my-store.yml).

## Licence

The program's code is under the Apache License 2.0, see [LICENSE](LICENSE).
The dataset is not covered by it: see "Dataset notice" above.

Games Workshop, Warhammer, Citadel and Forge World are trademarks of
Games Workshop Limited. GroknMok is an independent project, not
affiliated with or endorsed by Games Workshop or any store.
