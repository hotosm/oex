# Configuration

Configuration is layered:

1. Bundled defaults at `src/oex/defaults/base.yaml`.
2. A user YAML (passed via `--config` or auto-located at `configs/<iso3>.yaml`).
3. CLI overrides (`--iso3`, `--hdx-push`, `--output-dir`, etc).

Layers merge with [OmegaConf](https://omegaconf.readthedocs.io/), with one
deviation: when a user YAML provides a `categories` list, it **replaces** the
default list rather than element-wise merging.

## Mental model: `filter`, `where`, `select`

- **`select`** lists the columns to keep in the output. Same on both sources.
  Pure SQL expressions, e.g. `names.primary AS name` (Overture) or
  `tags['name'] AS name` (OSM).
- **`where`** is an optional SQL filter on the rows that come out of the
  source. Combined with the implicit country bbox and boundary clip.
- **`filter`** (OSM only) is the OSM tag filter passed to quackosm at
  **parquet build time**. It decides which OSM features ever land in the
  cache. This is the OSM equivalent of a row filter, but it runs during PBF
  to GeoParquet conversion, not at DuckDB query time. Most categories only
  need `filter`; `osm.where` stays empty.

For Overture there is no `filter`: the upstream (theme, feature_type) pair
already names a partitioned subset, so `where` is the only filter.

## Boundary

```yaml
boundary:
  geom: null                       # optional inline GeoJSON (string)
  geoboundaries_level: ADM0
  buffer_meters: 0                 # outward buffer in metres (0 = off)
  simplify_meters: 0               # simplification before and after the buffer (0 = off)
```

If `geom` is set (a GeoJSON string), it overrides the geoBoundaries lookup;
otherwise the boundary comes from geoBoundaries gbOpen ADM0 for the ISO3.

`buffer_meters` is an outward buffer applied to whatever boundary you end up
with, measured in metres on the ground: the geometry is buffered in an
azimuthal equidistant projection centred on it. 0 disables it. Use this for
coastal countries or for cross-border features whose centroid sits a few
hundred metres outside the legal boundary (jetties, bridges, airfields).
A boundary with land on both sides of the 180th meridian is centred where the
land lies and split at the meridian again after the buffer.

`simplify_meters` simplifies the outline with that tolerance before and after
the buffer, in the same projection. It must be below half of `buffer_meters`,
which keeps the whole original boundary inside the outline. Simplifying first keeps
the buffer of a detailed coastline, such as Norway's, fast and small in
memory, and the published outline is a much smaller polygon.

When `simplify_meters` is set, the simplified outline is also the geometry of
each layer's STAC Item, so a GIS that maps a catalog shows the area each export
covers. Otherwise the Item geometry is the bounding box of the data.

A note on engines: the buffer drives the polygon used to extract the
country PBF. For `source.osm.engine: geofabrik` the country PBF comes
pre-cut from Geofabrik (a small overlap beyond the legal border is
included, but it's bounded). Switch to `engine: planet` if you need a
larger buffer than Geofabrik's slice; the buffered polygon is passed
straight to `osmium extract`. The `postpass` and `rawdata` engines take the
buffered polygon as the query area itself, so the buffer applies exactly.
Overture is not affected; it reads from the global S3 bucket.

## Output formats

`output.formats` (or per-category `formats`) accepts any subset of:

| Format    | Notes                                                                                              |
| --------- | -------------------------------------------------------------------------------------------------- |
| `gpkg`    | GeoPackage. Single file, all geometry types together. Recommended default.                         |
| `shp`     | ESRI Shapefile. Split by geometry type. Field names truncated to 10 chars.                         |
| `geojson` | Single-file text. Easy to inspect, can be large.                                                   |
| `kml`     | Opens in Google Earth and most desktop GIS. Single XML file; prefer gpkg above ~1M features.       |

Default is `[gpkg, shp]`.

## Top-level keys

| Key            | Type                | Notes                                         |
| -------------- | ------------------- | --------------------------------------------- |
| `iso3`         | string              | Required. ISO3 country code.                  |
| `key`          | string              | Required. Slug for HDX dataset names.         |
| `dataset_name` | string \| null      | Pretty country/region name in HDX titles.     |
| `subnational`  | bool                | Sets HDX `subnational` flag.                  |
| `frequency`    | string              | HDX expected update frequency.                |
| `boundary`     | block               | See `BoundaryConfig`.                         |
| `output`       | block               | Output directory and format list.             |
| `parallel`     | block               | DuckDB threads + memory + thread pool toggle. |
| `duckdb`       | block               | http retry/timeout, temp dir, object cache.   |
| `logging`      | block               | level, format string.                         |
| `hdx`          | block               | HDX site, push toggle, credentials.           |
| `source`       | `overture` + `osm`  | Per-source settings (release, cache dir).     |
| `categories`   | list                | Per-theme `name`, `hdx`, `overture`, `osm`.   |

## Categories

Each category carries:

- `name`: human label (also used as the HDX dataset suffix and as the OSM
  cache parquet filename).
- `formats`: optional override for `output.formats`.
- `hdx`: title, notes, tags, license, license_url, caveats.
- `overture`: `enabled`, `theme`, `feature_type`, `select`, `where`.
- `osm`: `enabled`, `filter`, `select`, `where`.

Both `overture.select` and `osm.select` are pure SQL fragments. The geometry
column is appended automatically.

## OSM source schema (what your SELECT runs against)

The OSM cache produced by quackosm has three columns:

| Column       | Type                       | What it is                                    |
| ------------ | -------------------------- | --------------------------------------------- |
| `feature_id` | `VARCHAR`                  | OSM type + id, e.g. `node/12345`              |
| `tags`       | `MAP<VARCHAR, VARCHAR>`    | All retained OSM tags as key->value           |
| `geometry`   | geometry                   | POINT, LINESTRING, POLYGON, MULTIPOLYGON, ... |

`tags` is `MAP<VARCHAR, VARCHAR>`. Access a key with `tags['name'] AS name`; returns NULL when the key is absent. Refer to the
[OSM tag wiki](https://wiki.openstreetmap.org/wiki/Map_features) for what
keys exist; common ones include `building`, `highway`, `amenity`,
`waterway`, `landuse`, `place`, `aeroway`, `railway`, `name`, `name:en`,
`addr:*`, `source`.

`osm.filter` accepts the quackosm tag-filter shape:

```yaml
osm:
  filter:
    building: true                          # any value of `building`
    highway: ["primary", "secondary"]       # only these values
    amenity: ["hospital", "clinic"]
```

## Overture source schema (what your SELECT runs against)

Overture publishes parquet at `s3://overturemaps-us-west-2/release/<release>/theme=<theme>/type=<feature_type>/`.
Each (theme, feature_type) has a documented column set. The current release
exposes:

| Theme            | Feature type        | Notable columns                                                          |
| ---------------- | ------------------- | ------------------------------------------------------------------------ |
| `addresses`      | `address`           | `id`, `country`, `postcode`, `street`, `number`, `unit`                  |
| `base`           | `bathymetry`        | `id`, `depth`                                                            |
| `base`           | `infrastructure`    | `id`, `names`, `subtype`, `class`                                        |
| `base`           | `land`              | `id`, `names`, `subtype`, `class`                                        |
| `base`           | `land_cover`        | `id`, `subtype`, `cartography.{min,max}_zoom`                            |
| `base`           | `land_use`          | `id`, `names`, `subtype`, `class`, `surface`                             |
| `base`           | `water`             | `id`, `names`, `subtype`, `class`, `is_salt`, `wikidata`                 |
| `buildings`      | `building`          | `id`, `names`, `class`, `subtype`, `height`, `num_floors`, `roof_*`      |
| `buildings`      | `building_part`     | `id`, `height`, `num_floors`                                             |
| `divisions`      | `division`          | `id`, `names`, `subtype`, `country`, `region`, `population`, `wikidata`  |
| `divisions`      | `division_area`     | `id`, `names`, `subtype`, `country`, `region`                            |
| `divisions`      | `division_boundary` | `id`, `subtype`, `class`                                                 |
| `places`         | `place`             | `id`, `names`, `categories`, `addresses`, `phones`, `websites`           |
| `transportation` | `connector`         | `id`                                                                     |
| `transportation` | `segment`           | `id`, `names`, `class`, `subclass`, `subtype`, `road_surface`            |

For the authoritative schema (including types and nested struct shapes),
see the [Overture Maps schema reference](https://docs.overturemaps.org/schema/).
Note that types are renamed across releases (e.g. `boundary` became
`division_boundary` in 2026-04-15.0), so pin `source.overture.release` if
your config relies on a specific schema.

## Parallel and memory

```yaml
parallel:
  enabled: true
  threads: null      # null = adaptive: always 1 worker
  memory_gb: null    # null = adaptive: 60% of total RAM as DuckDB memory limit
```

`threads: null` and `memory_gb: null` both use adaptive defaults. The adaptive
logic always runs one DuckDB worker (DuckDB already parallelises every
operation internally) and allocates 60% of total RAM to the DuckDB memory
limit. This leaves headroom for GDAL writes and the GEOS boundary fallback
during pcode tagging.

Inside Docker, set `OEX_MEMORY_GB` to the container's `--memory` value so the
adaptive calculation uses the container limit rather than host RAM:

```bash
docker run --memory=20g -e OEX_MEMORY_GB=20 ...
```

## Pcode tagging

```yaml
source:
  pcodes:
    enabled: true                       # default false; true in the HOT schema
    levels: [1, 2, 3, 4]
    cache_dir: data/pcodes
    boundary_resolution: geos           # or 'h3_neighbor'
```

When enabled, every feature gets six extra columns:

| Column        | Example value     | What it is                          |
| ------------- | ----------------- | ----------------------------------- |
| `adm0_pcode`  | `NPL`             | ISO3 country code                   |
| `adm0_name`   | `Nepal`           | Country name                        |
| `adm1_pcode`  | `NP-BA`           | First subdivision pcode             |
| `adm1_name`   | `Bagmati`         | First subdivision name              |
| `adm2_pcode`  | `NP-BA-KA`        | Second subdivision pcode            |
| `adm2_name`   | `Kathmandu`       | Second subdivision name             |

`adm3` and `adm4` columns are added at the same levels; many countries have
null values at those levels.

Pcode data comes from [fieldmaps.io](https://fieldmaps.io/) edge-matched
humanitarian boundaries, downloaded once and cached locally.

### Boundary resolution

The H3 hash join covers 95-99% of features. For the remainder, whose H3 centroid
cell isn't owned by any admin (they sit on the seam between two adjacent admin
polygons), `boundary_resolution` picks the strategy:

| Value         | What it does                                                                           | Trade-off                                                                                       |
| ------------- | -------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `h3_neighbor` | Look up the 6 neighbour H3 cells; assign the admin that the most neighbours belong to. | Pure hash join, memory-bounded. Up to ~5 km of slack at admin borders.                          |
| `geos`        | `ST_Contains(admin_geom, centroid)` against the admin polygon.                         | Correct to the metre. Spatial nested-loop join; can OOM on large countries (e.g. CHN at 20 GB). |

Default is `geos`. The bundled defaults and the HOT schema set
`boundary_resolution: h3_neighbor` on high-cardinality categories (buildings,
roads, waterways, land use, rivers) so big countries don't OOM; smaller
categories inherit `geos` for precise borders.

```yaml
categories:
  - name: Buildings
    boundary_resolution: h3_neighbor   # memory-safe for millions of features
  - name: Schools
    # inherits geos
```

## Custom schemas

Three ways to plug in your own category set:

1. Inline `categories:` in your country YAML (replaces defaults wholesale).
2. `categories_file: path/to/schema.yaml` on the country YAML.
3. Both: `categories_file` loads the base set, then an inline
   `categories:` block overrides for that country.

Each category needs `name`, plus any of:

- `formats` (list): override the global `output.formats` for this category.
- `hdx`: HDX metadata (title, notes, tags, license, license_url, caveats).
- `overture`: `theme`, `feature_type`, `select` (SQL), `where` (SQL).
- `osm`: `filter` (quackosm tag filter), `select` (SQL), `where` (SQL).

`output.formats` and a category's own `formats` accept `gpkg`, `shp`, `geojson`,
`kml`, `fgb` and `geoparquet`. An unrecognised name fails at config load rather
than part way through a run.

### Zipping

Every format is zipped by default. `output.zip_formats` names the formats to zip, and
the rest publish as the bare file:

```yaml
output:
  formats: [gpkg, geojson, shp]
  zip_formats: [gpkg]      # geojson publishes as .geojson, gpkg and shp as .zip
```

A category may override it with its own `zip_formats`, so a large layer can stay
compressed while a small one publishes raw.

Two formats ignore the setting. Shapefiles are always zipped, because a shapefile is a
set of sidecar files rather than one file. GeoParquet is never zipped, because it is
published to be read in place over HTTP, and naming it in `zip_formats` is rejected at
config load rather than quietly doing nothing.

A bare file can be read straight from its URL, which is what a web client or a
`read_parquet` style query wants. Two things to weigh against that: the `README.txt`
and the config snapshot ride inside the zip and do not travel with a bare file (the
layer metadata is published as its own JSON resource either way), and text formats
compress well, so a GeoJSON is several times larger unzipped.

## File source

A category can come from a spatial data file rather than a mapped source. Anything
GDAL opens works (shapefile, GeoPackage, GeoJSON, FlatGeobuf), from a local path,
an `https://` URL or `s3://`.

```yaml
source:
  file:
    enabled: true
    snapshot: ""        # empty takes the file's own modification time
categories:
  - name: bridge_damage
    file:
      enabled: true
      path: data/bridges.shp
      crs: EPSG:32645   # overrides the file's own CRS; required when it declares none
      layer: bridges    # required when the file holds more than one layer
      select:
        name: Bridge_Nm
        status: Condition
      where: ["Condition IS NOT NULL"]
    hdx:
      title: Bridge Damage
      summary: Road bridges and their condition after the flood.
      license: hdx-odc-odbl
```

Run it with `oex-cli file --config <path>`.

`select` maps an output column to a column in the file, and is required. Naming the
columns explicitly means a rename upstream fails loudly instead of publishing a layer
with a missing field.

Coordinates are reprojected to `OGC:CRS84` when the file declares a different CRS,
because the boundary clip and the bbox filter work in longitude and latitude. The
transform ignores the authority's axis order, so a latitude-first CRS such as
`EPSG:4258` is read the way the file stores it rather than coming out swapped. A file
that declares no CRS at all is refused unless `crs` says what its coordinates are in:
guessing would place the data somewhere else in the world.

A file holding more than one layer is refused unless `layer` names the one to export,
since reading only the first would silently drop the rest.

The snapshot date becomes the HDX time period. It comes from the file's modification
time for a local path and from `Last-Modified` for an URL. S3 objects and files whose
server sends no `Last-Modified` need `source.file.snapshot` set explicitly.

Everything after reading is the same as any other source: the boundary clip, pcode
tagging, every configured output format, the zip and its README, S3 upload, and the
HDX title, description, licence and tags the config carries.

Pcode tagging needs `iso3`, since it looks up one country's admin boundaries. A config
with `source.pcodes.enabled` and no `iso3` is refused.

## OSM source: engines

```yaml
source:
  osm:
    engine: geofabrik          # geofabrik | planet | postpass | rawdata
    cache_dir: data/osm
    snapshot: latest
    fallback_engine: ""        # engine to retry with when the primary is unreachable
    geofabrik_clip_to_boundary: true
    planet_clip_to_boundary: true  # planet only; false = whole planet, no clip
    pbf_path: null             # required for engine: planet or planet_fallback
    planet_fallback: false     # try geofabrik, fall back to planet when it has no extract or stays unreachable
    auto_download_planet: false  # when true, download the planet PBF if pbf_path is missing
    postpass_endpoint: https://postpass.geofabrik.de/api/interpreter
    postpass_max_area_sq_km: 2000
    postpass_timeout_s: 600
    rawdata_endpoint: https://api-prod.raw-data.hotosm.org/v1
    rawdata_timeout_s: 1800   # the API queues the job, so allow for the wait
```

`geofabrik` and `planet` read a PBF, so they see OSM as of that file's
snapshot. `postpass` and `rawdata` query a live database instead, which is
what you want during an active response where edits made this morning have
to reach today's export. All four write the same
`feature_id` / `tags` / `geometry` parquet, so switching engines does not
change the output schema.

`geofabrik` (default): no pre-build. First run per country downloads the
country PBF from Geofabrik and runs quackosm once with the union of all category
tag filters. Per-category extraction is a tag predicate at query time. Cache
layout: `<cache_dir>/geofabrik/<iso3>/<snapshot>/country-<fingerprint>.parquet`,
where the fingerprint covers the boundary and the filter set.

`planet`: clips a country PBF out of a local planet PBF using
`osmium extract --strategy=complete_ways`, then runs quackosm once with
the union of all category tag filters and `keep_all_tags=True`. Per-category
extraction at query time is a tag-predicate WHERE on the resulting
`<cache_dir>/planet/<iso3>/<snapshot>/country-<fingerprint>.parquet`. Requires the
`osmium-tool` binary on PATH (one-time `dnf install osmium-tool` /
`apt install osmium-tool` / `brew install osmium-tool`).

`planet_fallback: true`: keeps `engine: geofabrik` as primary and switches to
the planet path when Geofabrik does not publish the country (e.g. some small
territories) or stays unreachable after its requests are retried.

`postpass`: sends one SQL query per OSM table, three per run, to Geofabrik's Postpass API,
which serves a minutely-updated OSM database, and writes the rows to
`<cache_dir>/postpass/<iso3>/country-<fingerprint>.parquet`. No download and no
local PBF. Postpass caps how much area one query may cover, so
`postpass_max_area_sq_km` (default 2000) fails the run before submitting a
boundary that is too large rather than waiting for the server to refuse it.
Suited to event-sized areas, not to whole countries.

`rawdata`: submits one job per run to the HOT Raw Data API, polls it to
completion, and reads the returned GeoJSON into
`<cache_dir>/rawdata/<iso3>/country-<fingerprint>.parquet`. The snapshot label
is the mirror's own `lastUpdated` time, so it records when the data was
current rather than when the run happened. Slower than Postpass because the
work is queued, but it takes the same filters and has no area cap.

`fallback_engine`: names a second engine to try when the first cannot be
reached. It covers an outage, not a defect: a transport error or a rejected
job triggers it, while an engine that answers with data oex cannot read
fails the run where the problem is. Set it to an engine of the same kind as
the primary, for example `engine: postpass` with `fallback_engine: rawdata`,
so a fallback does not quietly change how fresh the output is.

Download the planet PBF once with `oex-cli osm-build-cache`; the result
lives at `<cache_dir>/_pbf/planet-latest.osm.pbf` and you point
`pbf_path` at it.

`planet_clip_to_boundary: false`: whole-planet mode. The osmium clip is
skipped and quackosm processes the entire planet PBF in one pass, so
`osmium-tool` is not required and the source PBF is never deleted. Pair it
with `boundary.geom: world`, which resolves to a full-globe boundary
(`-180/-90..180/90`) without a geoBoundaries lookup, making the query-time
bbox filter a no-op. `iso3` is still required (it names the cache and output
paths, e.g. `WLD`). This is heavy: quackosm reads the full planet, so expect
a long run and ample disk.

```yaml
iso3: WLD
boundary:
  geom: world
source:
  osm:
    engine: planet
    planet_clip_to_boundary: false
    pbf_path: /data/osm/_pbf/planet-latest.osm.pbf
```

## Pinning a release / snapshot

Both sources resolve to the latest data by default. Pin a specific version
in the country YAML when you need a reproducible run:

```yaml
source:
  overture:
    release: 2026-04-15.0          # default 'latest' -> resolved from S3
  osm:
    snapshot: 2026-05-01           # default 'latest'
```

Resolution rules:

- **Overture `release`**: any literal release like `2026-04-15.0` is used
  verbatim with no lookup. `latest` lists the public S3 bucket and picks the
  highest `YYYY-MM-DD.N`.
- **OSM `snapshot` for `planet`**: defaults to the planet PBF's mtime as
  an ISO date, and a PBF older than the config's `frequency` is refused, so a stale
  planet fails the run instead of publishing data that is already due. An explicit value pins the cache directory name; subsequent
  runs reuse `<cache>/planet/<iso3>/<snapshot>/country-<fingerprint>.parquet` without
  reclipping the planet.
- **OSM `snapshot` for `geofabrik`**: this is a label for the per-country
  cache dir. With `latest`, the label is the date Geofabrik last rebuilt the
  extract, read from the `Last-Modified` header on the PBF URL; a run that
  cannot read that header falls over to `planet_fallback` or
  `fallback_engine`. Geofabrik only publishes `*-latest.osm.pbf` URLs (no
  historical archive), so a fresh build always pulls today's PBF regardless
  of the label. To truly pin an OSM date, use `engine: planet` with a planet
  PBF downloaded on the date you want.

The resolved version is logged before any per-category work, e.g.:

```text
Overture source: release=2026-04-15.0 bucket=overturemaps-us-west-2
OSM source: geofabrik IND, snapshot=2026-05-07, cache=...
```

It also lands inside every zip's `README.txt` (Source, Snapshot fields).

## HDX publication

HDX push is **off by default**. Enable per run:

```yaml
hdx:
  push: true
  site: prod                     # or 'demo'
  api_key: ${oc.env:HDX_API_KEY}
  owner_org: your-org
  maintainer: your-username
  user_agent: my-pipeline/1.0    # optional, defaults to oex
```

Each category supplies its own HDX metadata block:

```yaml
- name: Buildings
  hdx:
    title: Buildings of Nepal
    notes: |
      Building footprints from Overture (OSM + Microsoft + Google + Esri)
      and OpenStreetMap.
    tags: [buildings, geodata]
    license: hdx-odc-odbl                                     # or a free-form license string
    license_url: https://opendatacommons.org/licenses/odbl/1-0/
    caveats: Verified at the community level only.
    dataset_source: OpenStreetMap contributors                # optional override
```

`dataset_source` is the value HDX displays under "Source" on the dataset
page. When unset, the runner supplies a default like
`OpenStreetMap (Geofabrik IND 2026-05-07)` for OSM exports or
`Overture Maps Foundation 2026-04-15.0` for Overture. Override it to match
your organisation's standard (HOT-OSM uses the verbatim string
`OpenStreetMap contributors`).

When both `overture` and `osm` are enabled for a category, both sources
contribute resources to the same HDX dataset (one zip per source per format).
