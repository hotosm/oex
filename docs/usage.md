# Usage

## CLI

```bash
oex-cli overture <iso3> [theme]
oex-cli overture --configs-dir <dir>
oex-cli osm <iso3> [theme]
oex-cli osm --configs-dir <dir>
oex-cli file --config <path>
oex-cli osm-build-cache --config <path>
oex-cli metadata --iso3 <iso3>
oex-cli metadata --configs-dir <dir>
```

`metadata` pushes config text to HDX without re-running the export. It
rewrites the dataset title, description, caveats, source and tags, and every
resource's name and description, leaving the data files alone, so a wording
change takes seconds rather than a full export. `--check` reports what would
change and writes nothing. `--prune` deletes resources whose category has
left the config, which is destructive and off by default.

Common options:

- `--config <path>`: explicit YAML
- `--configs-dir <dir>`: run every YAML in the directory (200-country fan-out)
- `--engine geofabrik|planet|postpass|rawdata`: OSM engine override
- `--output-dir <dir>`: override output dir
- `--hdx-push / --no-hdx-push`: HDX toggle

## Output formats

Set `output.formats` (or per-category `formats`) to any combination of
`gpkg`, `shp`, `geojson`, `kml`. Default is `[gpkg, shp]`. Each format
produces its own zip:

- `gpkg`: GeoPackage. Single file. Holds all geometry types in one layer.
  Recommended for QGIS, ArcGIS, GDAL/OGR.
- `shp`: ESRI Shapefile. Split by geometry type into
  `<name>_polygons.shp`, `<name>_lines.shp`, `<name>_points.shp`. Field
  names truncated to 10 characters (shapefile format limit).
- `geojson`: single-file text. Easy to inspect, can be very large for big
  layers.
- `kml`: opens directly in Google Earth and most desktop GIS. Single XML
  file; prefer gpkg above ~1M features.

## Library

```python
from oex import Exporter
from oex.config import load_config, select_categories
from oex.osm.runner import OsmRunner
from oex.overture.runner import OvertureRunner

cfg = load_config("configs/nepal.yaml", ["iso3=NPL", "hdx.push=false"])

result = Exporter(cfg, OvertureRunner()).run()

buildings_only = select_categories(cfg, "buildings")
result = Exporter(buildings_only, OsmRunner()).run()

print(result.succeeded, result.empty, result.skipped, result.failed)
for name, cat in result.categories.items():
    print(name, cat.status, cat.feature_count, cat.zip_paths)
```

## Output layout

```text
output/
└── <iso3>/
    ├── osm/
    │   ├── <key>_<iso3>_<category>_gpkg.zip
    │   └── <key>_<iso3>_<category>_shp.zip
    └── overture/
        └── ...
```

Each zip contains:

- the GIS file(s)
- `README.txt`: country, source, snapshot, bbox
- `config.yaml`: snapshot of the category that produced this dataset
- `metadata.json` (when `output.metadata` or `output.report.enabled` is on): the
  layer as a STAC Item, carrying feature count, geometry types, its footprint
  (the simplified export boundary, or the data's bbox), and per-column null
  share, distinct count and top values
