<!--
  This file is the filter. Claude reads every new headline against it.
  Write it the way you'd brief an assistant: plain English, no keywords or Boolean logic.
  Saving a change re-checks the last 48 hours of articles automatically.
  The bold category keys must match the ones in config.yml.
  (Notes inside these comment marks are for you; Claude never sees them.)
-->

# What I want in my feed

Keep an article only when its **main news** is one of these:

- **series_order**: A network or streamer orders a new show to series, including
  straight-to-series orders and pilots picked up to series. Renewals of existing
  shows go under renewal instead.
- **pilot**: A pilot is ordered, or a project lands a pilot production commitment.
- **greenlight**: A film or show is officially greenlit or set to go into production
  at a studio, network or streamer.
- **adaptation**: Someone acquires, options or wins the screen rights to a book, comic,
  manga, video game, podcast, article, play, life story or other existing material,
  including remakes and reboots; or a new adaptation of existing material is
  announced as being in development.
- **casting**: An actor is cast in, joins, boards, is set to star in, or is in talks
  for a role in a film or series: lead, series regular, recurring, supporting or voice.
- **creative**: A director, writer, showrunner or creator attaches to, is hired for,
  or is in talks for a specific film or series.
- **development**: A new original (non-adaptation) film or series is set up, in
  development, or being shopped at a studio, network or streamer, before any pilot
  order, series order or greenlight. Adaptations in development go under adaptation.
- **renewal**: An existing series is renewed for another season.

If an article fits more than one, pick the category for its biggest news (a series
order beats casting, a greenlight beats development).

# What I don't want

Everything else, including: cancellations, release and premiere dates,
trailers and first looks, reviews, box office, ratings and viewership, interviews and
profiles, recaps, awards and festival coverage, obituaries, executive moves and
business news (mergers, earnings, layoffs), overall deals, agency signings, and
opinion or analysis pieces.

# When in doubt

Keep it.

<!-- Feed too noisy? Change "Keep it." to "Leave it out." -->
