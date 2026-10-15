<!--
  This file is the filter. Claude reads every new headline against it.
  Write it the way you'd brief an assistant: plain English, no keywords or Boolean logic.
  Saving a change re-checks the last 48 hours of articles automatically.
  The bold category and tag keys must match the ones in config.yml.
  (Notes inside these comment marks are for you; Claude never sees them.)
-->

# What I want in my feed

Keep an article only when its **main news** is one of these:

- **ordered**: A network or streamer orders a new show to series (including
  straight-to-series orders and pilots picked up to series), orders a pilot or gives a
  pilot production commitment, or a film or show is officially greenlit or set to go
  into production at a studio, network or streamer.
- **casting**: An actor is cast in, joins, boards, is set to star in, or is in talks
  for a role in a film or series: lead, series regular, recurring, guest, supporting or
  voice. Includes recastings and actors promoted to series regular.
- **creative**: A director, writer, showrunner or creator attaches to, is hired for,
  or is in talks for a specific film or series.
- **adaptation**: Someone acquires, options or wins the screen rights to a book, comic,
  manga, video game, podcast, article, play, life story or other existing material,
  including remakes and reboots; or a new adaptation of existing material is
  announced as being in development.
- **development**: A new original (non-adaptation) film or series, sequel or spinoff
  is set up, in development, or being shopped at a studio, network or streamer, before
  any order or greenlight. Adaptations in development go under adaptation.
- **renewal**: An existing series is renewed for another season.
- **deals**: A finished or nearly finished film or series is acquired, picked up for
  distribution or streaming, or boarded by a sales company; or a spec script or
  pitch sells to a studio, network or streamer.
- **release**: A film or series gets a release or premiere date, or its first trailer,
  teaser, first-look photos or footage.
- **cancelled**: A series is cancelled or ends early, a film or series in development
  is dropped, scrapped, shelved or "not moving forward", or a series regular or lead
  leaves a show or project.
- **representation**: An actor, director, writer or creator signs with or moves to a
  talent agency or management company.

If an article fits more than one, pick the category for its biggest news (an order
beats casting, a greenlight beats development).

# What I don't want

Everything else, including: reviews, box office, ratings and viewership, interviews and
profiles, recaps, obituaries, executive hires and moves, overall and first-look deals,
business news (mergers, earnings, layoffs), "everything we know" explainers, and
opinion or analysis pieces.

Festival and awards coverage doesn't count either: lineups and selections, premieres,
reviews, red carpets, juries, awards and buzz pieces. But when an article's main news
is one of the kinds above, keep it even if it's tied to a festival: a film "acquired
ahead of its TIFF premiere" is a deal.

# When in doubt

Keep it.

<!-- Feed too noisy? Change "Keep it." to "Leave it out." -->

# Extra tags

Add these to a kept article whenever they apply, on top of its category:

- **asian**: The news involves Asian or Asian-diaspora people, productions or
  companies: an Asian or Asian-American actor, director, writer or creator; a
  production from or set in an Asian country (K-dramas, anime, Indian, Chinese or
  Japanese films and series); an adaptation of Asian source material such as manga,
  webtoons or Asian novels; or an Asian-focused company or organization. Tag it only
  when the headline or summary says so or you're confident who someone is; don't
  guess from a name.

<!-- To stop tagging, delete the "asian" entry here and under tags: in config.yml. -->
