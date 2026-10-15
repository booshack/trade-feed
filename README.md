# Trade Feed

One feed with only the trade stories you want: adaptations and rights deals, pilot and
series orders, greenlights and casting, from Deadline, Variety, THR, TheWrap and IndieWire.

Instead of a keyword query, every new headline is read by Claude Haiku against a brief
you write in plain English ([`criteria.md`](criteria.md)). It runs on GitHub every 15
minutes and publishes:

- **an RSS feed** (`feed.xml`) for any reader app,
- **a page** for your phone, with category filters, and
- **a searchable archive** of every story it has kept, going back a year from the day you
  set it up.

Also: the same news from several outlets becomes one story ("Also reported by Variety ·
THR"), each story is tagged 📺 🎬 🟢 📚 🎭, and the page lists everything filtered out in
the last 24 hours so you can spot anything it missed.

## Set it up (about 10 minutes)

1. **Get an API key.** At [platform.claude.com](https://platform.claude.com), buy some
   credit (about $15 covers the one-year backfill plus a year of running) and create an
   API key.
2. **Create a new public GitHub repository** and upload all of these files, including the
   `.github` folder. Public keeps GitHub Actions and Pages free.
   *On a Mac, press Cmd+Shift+. in Finder to see the `.github` folder. Or add it on GitHub
   with Add file → Create new file, name it `.github/workflows/update.yml` and paste the
   contents.*
3. **Add the key:** repo **Settings → Secrets and variables → Actions → New repository
   secret**. Name it `ANTHROPIC_API_KEY` and paste the key.
4. **Turn on the page:** **Settings → Pages → Build and deployment → Source: GitHub Actions**.
5. **Run it:** **Actions → Update feed → Run workflow**. A minute or two later:
   - Page: `https://<your-username>.github.io/<repo-name>/`
   - Feed: `https://<your-username>.github.io/<repo-name>/feed.xml`
   - Archive: `https://<your-username>.github.io/<repo-name>/archive.html`

It then updates itself every 15 minutes. Uploading the files starts a first run before
steps 3 and 4 are done; that's fine. It waits for the key and publishes once Pages is on.

## The archive

Every story the filter keeps is saved in your repository, one file per month in
`docs/archive/`, and shown on the archive page with a month picker, search (titles,
people, studios) and the same category filters. It's kept indefinitely; set
`archive_months` in `config.yml` (e.g. `12`) to keep a rolling window instead.

**Filling in the past year.** RSS feeds only list each outlet's latest ~10 articles, so the
past comes from each site's sitemap: the list of every article that sites publish for
search engines. With `backfill_days: 365` (the default), each run spends up to 10 minutes
working back through a year of those, so the archive fills in over a few hours. It reads
each site's robots.txt first, goes at one request a second, and stops for any site that
blocks it. The archive page's "About the archive" section shows how each site went.

Sitemaps list links, not headlines, so filled-in stories get a title rebuilt from the
link's words (marked "title from link") and no summary, and they're sorted from less
information than live ones. Set `backfill_days` to `0` to skip it, or raise it later to
go further back; only the extra time is read.

## Tuning it

Open `criteria.md` on GitHub (the pencil icon works on a phone) and edit it in plain
English, e.g. *"Also keep directors attaching to adaptations"* or *"Skip reality and
unscripted shows."* Saving re-checks the last 48 hours straight away, so you see the
effect within a couple of minutes.

To add or drop sources, or change the categories, edit `config.yml`.

## What it costs

- **GitHub Actions and Pages:** free for public repositories.
- **Claude:** Haiku 5.5 costs $0.10 per million input tokens and $0.50 per million output
  tokens. Sorting every new headline from all five trades comes to roughly a dollar a
  month. The one-year backfill is roughly $2–3, once. The page footer shows the running
  total for the month.
- **Paying:** API credit is bought up front in the amount you choose, so it can't spend
  more than you've loaded (unless you turn on auto-reload). Credit expires a year after
  purchase, so there's no point loading much more than a year's worth.

## If something goes wrong

- **The page shows a yellow banner.** It says what's wrong: a missing or rejected API key,
  no credit left, or a source that isn't loading. For key or credit problems, one run is
  also marked as failed in the Actions tab (GitHub normally emails you about it), once per
  problem rather than every 15 minutes. Articles wait and are sorted once it's fixed.
- **A source says "nothing new since…".** The outlet moved its feed. Find the new address
  (often the site's address plus `/feed/`) and update `config.yml`.
- **A source says it was sent to a "paid bot gateway".** That outlet now blocks automated
  readers like this one. Remove it from `config.yml`, or follow it in your usual reader.
- **The Actions log warns "Page not published yet".** Do step 4.
- **It stopped updating after a long quiet spell.** GitHub pauses schedules in repositories
  with no activity for 60 days (this one commits whenever there's news, so it's unlikely).
  Turn it back on under **Actions → Update feed**.

## How it works

`filter.py` reads each feed, skips articles it has already seen (remembered in
`state.json`), and sends new headlines with their one-line summaries to Claude in batches
of up to 40, together with your brief. Claude returns which ones to keep, each with a
category, the project title and the people involved. Reports with the same project and
people within four days are merged. The script writes `docs/feed.xml`, `docs/index.html`,
`docs/archive.html` and the month files in `docs/archive/`; the workflow commits them and
publishes the site. `backfill.json` tracks how far the backfill has got.

The repository is public, so the feed, page and archive are too. They contain only
headlines and links. Don't put anything private in these files.
