"""Typed run configuration."""

from dataclasses import dataclass, field
from typing import Any

from oex.palette import DEFAULT_PALETTE

OsmTagFilter = dict[str, Any]


@dataclass
class CombinedHdx:
    """Metadata for the one dataset `hdx.combine` publishes every category onto. Every value
    is optional; an empty one falls back to what oex derives from the categories."""

    # Dataset slug. Empty falls back to "{key}_{iso3}".
    name: str = ""
    # Supports {country} and {iso3}.
    title: str = ""
    notes: str = ""
    caveats: str = ""
    source: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class HdxConfig:
    push: bool = False
    site: str = "demo"
    api_key: str | None = None
    owner_org: str | None = None
    maintainer: str | None = None
    user_agent: str = "oex"
    methodology: str = "Other"
    methodology_other: str = "Open Source Geographic information"
    # Supports {country}, {category}, {iso3}. Empty falls back to
    # "<category> of <iso3>".
    title_template: str = ""
    # Destructive: deletes every existing resource on the dataset before upload.
    purge_existing_resources: bool = False
    # YYYY-MM-DD the dataset first covered; the HDX time period then runs to each snapshot,
    # unless the data's own dates from a category's temporal.column are known.
    time_period_start: str | None = None
    # Publish every category as resources on ONE HDX dataset instead of one
    # dataset per category.
    combine: bool = False
    combined: CombinedHdx = field(default_factory=CombinedHdx)


@dataclass
class DuckdbConfig:
    # 8 retries / 500 ms initial / 2x backoff and a 120 s timeout absorb
    # transient S3 blips so a 200-country batch doesn't abort on one shard.
    http_retries: int = 8
    http_retry_wait_ms: int = 500
    http_retry_backoff: float = 2.0
    http_timeout_s: int = 120
    temp_dir: str = "/tmp/duckdb_temp"


@dataclass
class MapAssetsConfig:
    """Where the page map gets its basemap and JavaScript. Fetched at view time, so a version
    can be pinned or served from your own host."""

    basemap_tiles: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    basemap_attribution: str = "(c) OpenStreetMap contributors"
    maplibre_css: str = "https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css"
    maplibre_js: str = "https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"
    pmtiles_js: str = "https://unpkg.com/pmtiles@3.2.1/dist/pmtiles.js"


@dataclass
class ThemeConfig:
    """Colours the pages are built from. Everything else derives from these."""

    accent_color: str = "#2563eb"
    map_popup_color: str = "#2563eb"
    boundary_outline_color: str = "#e11d48"


@dataclass
class ReportConfig:
    enabled: bool = False
    # Layer colours, assigned in layer order and cycled when there are more layers.
    palette: list[str] = field(default_factory=lambda: list(DEFAULT_PALETTE))
    theme: ThemeConfig = field(default_factory=ThemeConfig)
    map_assets: MapAssetsConfig = field(default_factory=MapAssetsConfig)
    related_datasets: bool = False


@dataclass
class PmtilesConfig:
    enabled: bool = False
    min_zoom: int = 0
    # tippecanoe may add up to max_extra_zooms past max_zoom before it drops the densest features.
    max_zoom: int = 12
    max_extra_zooms: int = 4


@dataclass
class S3Config:
    enabled: bool = False
    bucket: str = ""
    prefix: str = ""
    region: str = ""
    acl: str = "public-read"
    endpoint_url: str | None = None
    # Mid-path folder and filename id segment. Empty falls back to iso3.
    folder: str = ""
    nest_by_category: bool = True
    name_include_source: bool = True
    # Requires output.split_by_geometry.
    nest_by_geometry: bool = False


@dataclass
class StacCatalogConfig:
    # Every run links its Collections into a root and per-area catalog on S3.
    catalog: bool = False
    id: str = "oex-exports"
    title: str = "oex exports"
    description: str = "Vector layers published by oex."
    group: str | None = None
    group_title: str | None = None


@dataclass
class OutputConfig:
    dir: str = "output"
    formats: list[str] = field(default_factory=lambda: ["gpkg", "shp"])
    # None zips every format; a list zips only those named. Shapefiles are always
    # zipped, since a shapefile is a set of sidecar files.
    zip_formats: list[str] | None = None
    # Layer metadata as a STAC Item per layer plus one Collection per dataset.
    metadata: bool = False
    report: ReportConfig = field(default_factory=ReportConfig)
    pmtiles: PmtilesConfig = field(default_factory=PmtilesConfig)
    stac: StacCatalogConfig = field(default_factory=StacCatalogConfig)
    s3: S3Config = field(default_factory=S3Config)
    resume: bool = True
    remove_after_upload: bool = True
    split_by_geometry: bool = False


@dataclass
class ParallelConfig:
    enabled: bool = True
    threads: int | None = None
    memory_gb: int | None = None


@dataclass
class BoundaryConfig:
    geom: str | None = None
    geoboundaries_level: str = "ADM0"
    buffer_meters: float = 0.0
    # Simplification tolerance applied after the buffer, to keep the outline small.
    simplify_meters: float = 0.0


@dataclass
class OvertureSourceConfig:
    enabled: bool = True
    release: str = "latest"
    s3_region: str = "us-west-2"
    s3_bucket: str = "overturemaps-us-west-2"


@dataclass
class FileSourceConfig:
    """Defaults for categories read from spatial files."""

    enabled: bool = False
    path: str = ""
    crs: str = ""
    snapshot: str = ""


@dataclass
class OsmSourceConfig:
    enabled: bool = True
    engine: str = "geofabrik"
    cache_dir: str = "data/osm"
    snapshot: str = "latest"
    keep_pbf: bool = False
    pbf_url: str = "https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf"
    md5_url: str = "https://planet.openstreetmap.org/pbf/planet-latest.osm.pbf.md5"
    geofabrik_index_url: str = "https://download.geofabrik.de/index-v1.json"
    geofabrik_clip_to_boundary: bool = True
    planet_clip_to_boundary: bool = True
    pbf_path: str | None = None
    planet_fallback: bool = False
    auto_download_planet: bool = False
    # Postpass is a shared Geofabrik service, so the engine refuses areas above
    # this rather than letting a country-sized boundary through by accident.
    postpass_endpoint: str = "https://postpass.geofabrik.de/api/interpreter"
    postpass_max_area_sq_km: float = 2000.0
    rawdata_endpoint: str = "https://api-prod.raw-data.hotosm.org/v1"
    postpass_timeout_s: int = 600
    rawdata_timeout_s: int = 1800
    fallback_engine: str = ""


@dataclass
class PcodesSourceConfig:
    enabled: bool = False
    cache_dir: str = "data/pcodes"
    levels: list[int] = field(default_factory=lambda: [1, 2, 3, 4])
    manifest_url: str = "https://data.fieldmaps.io/edge-matched.json"
    parquet_url_template: str = (
        "https://data.fieldmaps.io/edge-matched/humanitarian/intl/adm{level}_polygons.parquet"
    )
    manifest_group: str = "humanitarian"
    # 'geos' is exact; 'h3_neighbor' errs by up to 5 km at admin borders but bounds
    # memory, which is what buildings, roads and waterways in large countries need.
    boundary_resolution: str = "geos"


# HDX licence ids, as HDX's license_list API names them; reports show the title instead.
HDX_LICENSE_LABELS = {
    "hdx-odc-odbl": "Open Database License (ODC-ODbL)",
    "hdx-odc-by": "Open Data Commons Attribution License",
    "hdx-pddl": "Open Data Commons Public Domain Dedication and License",
    "cc-by": "Creative Commons Attribution 4.0",
    "cc-by-sa": "Creative Commons Attribution Share-Alike 4.0",
    "cc-by-igo": "Creative Commons Attribution for Intergovernmental Organisations",
    "other-pd-nr": "Public Domain (CC0)",
}


def license_label(value: str) -> str:
    """Human-readable licence name, falling back to whatever the config set."""
    return HDX_LICENSE_LABELS.get(value, value)


@dataclass
class CategoryHdx:
    title: str | None = None
    notes: str = "Vector data export."
    # Each resource's description; falls back to the opening of `notes` when unset.
    summary: str = ""
    tags: list[str] = field(default_factory=lambda: ["geodata"])
    license: str = "hdx-odc-odbl"
    license_url: str | None = None
    caveats: str = (
        "Data may contain errors. Verified at the community level only; "
        "individual features may need correction."
    )
    # HDX dataset_source override. When unset, defaults to "OpenStreetMap" or "Overture".
    dataset_source: str | None = None


@dataclass
class CategoryOverture:
    enabled: bool = True
    theme: str = ""
    feature_type: str = ""
    select: list[str] = field(default_factory=list)
    where: list[str] = field(default_factory=list)
    tiles: bool = True


@dataclass
class CategoryOsm:
    # `filter` is the quackosm tag filter applied at parquet BUILD time.
    # `where` is SQL applied at QUERY time over the already-built parquet.
    enabled: bool = True
    select: list[str] = field(default_factory=list)
    where: list[str] = field(default_factory=list)
    filter: OsmTagFilter = field(default_factory=dict)
    tiles: bool = True


@dataclass
class CategoryFile:
    """A category read from a user-supplied spatial file rather than a mapped source."""

    enabled: bool = True
    # Local path, https:// or s3://. Overrides source.file.path for this category.
    path: str = ""
    # Overrides the CRS the file declares; required when it declares none.
    crs: str = ""
    # Required when the file holds more than one layer, which oex will not pick between.
    layer: str = ""
    # Output column -> source column. Explicit, so a rename upstream fails loud.
    select: dict[str, str] = field(default_factory=dict)
    where: list[str] = field(default_factory=list)
    tiles: bool = True


@dataclass
class TransliterateRule:
    target: str = ""
    source: str = ""
    prefer: str | None = None


@dataclass
class CategoryTemporal:
    # A per-feature timestamp from the source's select; its range dates the HDX
    # dataset and the report's temporal block.
    column: str | None = None


@dataclass
class CategoryConfig:
    name: str = ""
    formats: list[str] | None = None
    # Overrides output.zip_formats for this category. None inherits.
    zip_formats: list[str] | None = None
    skip_pcodes: bool = False
    # Override source.pcodes.boundary_resolution per category. None inherits.
    boundary_resolution: str | None = None
    hdx: CategoryHdx = field(default_factory=CategoryHdx)
    overture: CategoryOverture = field(default_factory=CategoryOverture)
    osm: CategoryOsm = field(default_factory=CategoryOsm)
    file: CategoryFile = field(default_factory=CategoryFile)
    transliterate: list[TransliterateRule] = field(default_factory=list)
    temporal: CategoryTemporal = field(default_factory=CategoryTemporal)


@dataclass
class RootConfig:
    iso3: str = ""
    key: str = ""
    dataset_name: str | None = None
    subnational: bool = False
    frequency: str = "yearly"
    categories_file: str | None = None
    boundary: BoundaryConfig = field(default_factory=BoundaryConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    parallel: ParallelConfig = field(default_factory=ParallelConfig)
    duckdb: DuckdbConfig = field(default_factory=DuckdbConfig)
    hdx: HdxConfig = field(default_factory=HdxConfig)
    source: dict[str, Any] = field(
        default_factory=lambda: {
            "overture": OvertureSourceConfig(),
            "osm": OsmSourceConfig(),
            "file": FileSourceConfig(),
            "pcodes": PcodesSourceConfig(),
        }
    )
    categories: list[CategoryConfig] = field(default_factory=list)


def dataset_identity(cfg: RootConfig) -> str:
    """iso3 when set, else the S3 folder id, which is how a sub-national Tasking Manager
    export identifies itself."""
    return cfg.iso3.lower() or cfg.output.s3.folder
