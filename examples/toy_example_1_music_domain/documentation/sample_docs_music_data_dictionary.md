# Music Store — Data Dictionary

Field-level definitions for each table. Units and encodings are called out so
they can be captured as ontology annotations.

## Table: artist
| Column | Meaning | Notes |
|---|---|---|
| artist_id | Unique identifier of the artist | Primary key, integer surrogate |
| name | Display name of the artist or band | Free text |

## Table: album
| Column | Meaning | Notes |
|---|---|---|
| album_id | Unique identifier of the album | Primary key, integer surrogate |
| title | Title of the album | Free text |
| release_date | Date the album was released | Calendar date (year-month-day) |
| artist_id | The artist that released this album | Foreign key to artist.artist_id |

## Table: track
| Column | Meaning | Notes |
|---|---|---|
| track_id | Unique identifier of the track | Primary key, integer surrogate |
| name | Title of the track | Free text |
| milliseconds | Playback duration of the track | Unit: **milliseconds** (integer). Divide by 1000 for seconds. |
| unit_price | Retail price of the track | Currency amount in **US dollars**, decimal with two fractional digits. |
| album_id | The album this track appears on | Foreign key to album.album_id |

## Encoding and unit conventions

- `milliseconds` is a duration measured in milliseconds, not seconds. A typical
  three-minute track is about 180000.
- `unit_price` is a monetary amount; treat it as a decimal currency value rather
  than a plain number.
- `release_date` follows ISO 8601 calendar-date formatting.
