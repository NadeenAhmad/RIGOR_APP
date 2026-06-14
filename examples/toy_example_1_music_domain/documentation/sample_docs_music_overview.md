# Music Store — Domain Overview

This dataset describes a small digital music store. It records the artists whose
work is sold, the albums those artists release, and the individual tracks that
make up each album.

## Entities and how they relate

- **Artist** — a musical act (a solo performer or a band) that creates recorded
  music. Each artist is identified by `artist_id` and has a display `name`.
- **Album** — a released collection of recordings attributed to a single
  artist. Identified by `album_id`, with a `title`, a `release_date`, and an
  `artist_id` linking it to the artist who released it.
- **Track** — a single recorded piece of music that appears on an album.
  Identified by `track_id`, with a `name`, a duration in `milliseconds`, a
  `unit_price`, and an `album_id` linking it to its album.

The relationships form a simple hierarchy: an **artist** releases one or more
**albums**, and an **album** contains one or more **tracks**. A track therefore
belongs to exactly one album, and an album to exactly one artist.

## Notes on identifiers

`artist_id`, `album_id`, and `track_id` are surrogate primary keys. The columns
`album.artist_id` and `track.album_id` are foreign keys that express the
"released by" and "appears on" relationships respectively.
