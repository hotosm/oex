"""OSM source runner: one quackosm pass per country over every category's tags, after
which each category is a WHERE over the resulting country.parquet.
"""

import hashlib
import json
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import requests
from upath import UPath

if TYPE_CHECKING:
    from shapely.geometry.base import BaseGeometry

from oex import http
from oex.boundary import resolve_boundary
from oex.config.schema import (
    CategoryConfig,
    OsmSourceConfig,
    RootConfig,
    dataset_identity,
)
from oex.locale import local_osm_languages
from oex.logging_setup import get_logger
from oex.osm.category_filter import category_where_predicate, union_tag_filter
from oex.osm.country_parquet import LiveSnapshot
from oex.osm.engines import ENGINE_NAMES
from oex.osm.errors import OsmEngineUnavailableError
from oex.osm.extract import osmium_polygon_extract
from oex.osm.fetch_planet import download_pbf
from oex.osm.geofabrik import GeofabrikUnavailableError, lookup_country
from oex.osm.postpass import boundary_area_sq_km, fetch_country_parquet
from oex.osm.rawdata import fetch_country_parquet as fetch_rawdata_parquet
from oex.sources.base import CategorySkippedError, SourceQuery, SourceRunner
from oex.system import cpu_count, default_memory_limit_gb

logger = get_logger(__name__)

_GEOFABRIK_DOWNLOAD_ATTEMPTS = 2
_GEOFABRIK_RETRY_BACKOFF_SECONDS = 5
_GeofabrikFallbackError = (
    GeofabrikUnavailableError,
    OsmEngineUnavailableError,
    requests.RequestException,
)


def _ensure_local_pbf(pbf_path: str, cache_dir: Path) -> Path:
    """Return a local PBF, downloading it first when the path is remote (s3://, ...).

    A cached download is reused when its size matches the remote object.
    """
    remote = UPath(pbf_path)
    if remote.protocol in ("", "file", "local"):
        return Path(pbf_path)
    local = cache_dir / "_pbf" / remote.name
    if local.is_file() and local.stat().st_size == remote.stat().st_size:
        logger.info("Using cached PBF %s", local)
        return local
    local.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading PBF %s -> %s", pbf_path, local)
    with remote.open("rb") as reader, local.open("wb") as writer:
        shutil.copyfileobj(reader, writer)
    return local


def _row_group_size(memory_gb: int) -> int:
    """Parquet row group size, scaled to the memory budget: quackosm holds one row
    group per worker as an Arrow table, outside the DuckDB budget."""
    return 100_000 if memory_gb >= 16 else max(10_000, memory_gb * 5_000)


# quackosm's default drops source, ele, area and other tags the layers select.
_IGNORE_METADATA_TAGS = False


def _parquet_fingerprint(cfg: RootConfig, *, clip: bool) -> str:
    """Keyed on the boundary config and tag filters too, or changing either would reuse a stale
    parquet; the config is hashed rather than the geometry so reuse never needs the network."""
    boundary = cfg.boundary
    clip_key: object = "noclip"
    if clip:
        clip_key = {
            "geom": boundary.geom,
            "level": boundary.geoboundaries_level,
            "buffer": boundary.buffer_meters,
            "simplify": boundary.simplify_meters,
        }
    payload = json.dumps(
        # Which tags quackosm keeps shapes the parquet, so it is part of the key.
        {
            "clip": clip_key,
            "filter": union_tag_filter(cfg.categories),
            "ignore_metadata_tags": _IGNORE_METADATA_TAGS,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def _inject_local_name(select_fields: list[str], iso3: str) -> list[str]:
    """Append `tags['name:<lang>'] AS name_<lang>` for each local OSM language.

    Skips a language if its alias is already present (per-category YAML wins).
    """
    languages = local_osm_languages(iso3)
    if not languages:
        return select_fields

    new_fields = list(select_fields)
    insert_at = len(new_fields)
    for index, field in enumerate(new_fields):
        if "AS name_en" in field or field.endswith(" name_en"):
            insert_at = index + 1
            break

    existing = "\n".join(new_fields)
    for lang in languages:
        alias = f"name_{lang}"
        if alias in existing:
            continue
        new_fields.insert(insert_at, f"tags['name:{lang}'] AS {alias}")
        insert_at += 1
    return new_fields


def _refuse_stale_planet(planet_pbf: Path, frequency: str) -> None:
    """Data older than the advertised frequency lands on HDX already past its due date."""
    from hdx.data.dataset import Dataset

    days = Dataset.transform_update_frequency(frequency)
    if days is None or int(days) <= 0:
        return
    age_days = (time.time() - planet_pbf.stat().st_mtime) / 86400
    if age_days > int(days):
        raise RuntimeError(
            f"{planet_pbf} is {age_days:.0f} days old, older than the {frequency} update "
            f"frequency; refresh the planet PBF before exporting"
        )


class OsmRunner(SourceRunner):
    name = "osm"

    def __init__(self) -> None:
        self._engine: str | None = None
        self._snapshot_dir: Path | None = None
        self._snapshot_label: str | None = None
        self._snapshot_date: datetime | None = None
        self._dataset_source: str = "OpenStreetMap"
        self._source_description: str = "Country features are extracted from the source PBF via quackosm with the union of all category tag filters; per-category exports apply tag predicates at query time."
        self._country_parquet: Path | None = None
        self._tags_present: bool | None = None

    def peek_snapshot_label(self, cfg: RootConfig) -> str | None:
        src = cast(OsmSourceConfig, cfg.source["osm"])
        if not src.enabled or not dataset_identity(cfg):
            return None
        engine = (src.engine or "geofabrik").lower()
        if engine == "geofabrik":
            country_root = Path(src.cache_dir) / "geofabrik" / cfg.iso3.lower()
            if not country_root.exists():
                return None
            try:
                extract = lookup_country(cfg.iso3, index_url=src.geofabrik_index_url)
                return self._resolve_geofabrik_snapshot(src.snapshot, extract.pbf_url)
            except _GeofabrikFallbackError as exc:
                logger.debug("Cannot peek the %s snapshot (%s); will run it", cfg.iso3, exc)
                return None
        if engine == "planet":
            if not src.pbf_path:
                return None
            pbf = Path(src.pbf_path)
            if not pbf.exists():
                return None
            return self._planet_snapshot_label(pbf, src.snapshot)
        return None

    def prepare(self, cfg: RootConfig) -> None:
        src = cast(OsmSourceConfig, cfg.source["osm"])
        if not src.enabled:
            raise RuntimeError("OSM source is disabled in config")

        engine = (src.engine or "geofabrik").lower()
        fallback = (src.fallback_engine or "").lower()
        try:
            self._prepare_engine(cfg, src, engine)
        except (OsmEngineUnavailableError, requests.RequestException) as error:
            if not fallback:
                raise
            logger.warning(
                "osm.engine=%s failed (%s); falling back to %s for this run",
                engine,
                error,
                fallback,
            )
            try:
                self._prepare_engine(cfg, src, fallback)
            except Exception as fallback_error:
                raise fallback_error from error

    def _prepare_engine(self, cfg: RootConfig, src: OsmSourceConfig, engine: str) -> None:
        if engine == "geofabrik":
            try:
                self._prepare_geofabrik(cfg, src)
            except _GeofabrikFallbackError as exc:
                if not src.planet_fallback:
                    raise
                logger.warning(
                    "Geofabrik failed for %s (%s); falling back to planet engine",
                    cfg.iso3,
                    exc,
                )
                self._prepare_planet(cfg, src)
        elif engine == "planet":
            self._prepare_planet(cfg, src)
        elif engine == "postpass":
            self._prepare_postpass(cfg, src)
        elif engine == "rawdata":
            self._prepare_rawdata(cfg, src)
        else:
            raise ValueError(
                f"Unknown osm.engine={engine!r}; expected one of {sorted(ENGINE_NAMES)}"
            )

    def _prepare_live_engine(
        self,
        cfg: RootConfig,
        src: OsmSourceConfig,
        *,
        engine: str,
        fetch: Callable[..., LiveSnapshot],
        endpoint: str,
        timeout: int,
        dataset_source: str,
        source_description: str,
    ) -> None:
        """Shared path for the live engines. Always refetches, since freshness is the reason to
        use one."""
        if not cfg.iso3:
            raise ValueError(f"osm.engine={engine} requires `iso3` in the config")

        boundary = resolve_boundary(cfg.iso3, cfg.boundary)
        self._check_area(engine, src, boundary.geojson)
        union_filter = union_tag_filter(cfg.categories)
        snapshot_dir = Path(src.cache_dir) / engine / cfg.iso3.lower()
        country_parquet = snapshot_dir / f"country-{_parquet_fingerprint(cfg, clip=True)}.parquet"

        logger.info("%s fetch for %s: %d filter keys", engine, cfg.iso3.upper(), len(union_filter))
        snapshot = fetch(
            boundary_geojson=boundary.geojson,
            tag_filter=union_filter,
            out_path=country_parquet,
            endpoint=endpoint,
            timeout=timeout,
        )

        self._engine = engine
        self._snapshot_dir = snapshot_dir
        self._snapshot_label = snapshot.label
        self._snapshot_date = snapshot.timestamp
        self._dataset_source = dataset_source.format(label=snapshot.label)
        self._source_description = source_description
        self._country_parquet = country_parquet

    @staticmethod
    def _check_area(engine: str, src: OsmSourceConfig, boundary_geojson: str) -> None:
        """Postpass is a shared Geofabrik service and refuses large areas server-side."""
        if engine != "postpass":
            return
        area_sq_km = boundary_area_sq_km(boundary_geojson)
        if area_sq_km > src.postpass_max_area_sq_km:
            raise ValueError(
                f"Boundary covers {area_sq_km:,.0f} km2, above the "
                f"{src.postpass_max_area_sq_km:,.0f} km2 ceiling for osm.engine=postpass. "
                "Postpass is a shared Geofabrik service; use engine=geofabrik for an area "
                "this size, or raise osm.postpass_max_area_sq_km deliberately."
            )

    def _prepare_rawdata(self, cfg: RootConfig, src: OsmSourceConfig) -> None:
        self._prepare_live_engine(
            cfg,
            src,
            engine="rawdata",
            fetch=fetch_rawdata_parquet,
            endpoint=src.rawdata_endpoint,
            timeout=src.rawdata_timeout_s,
            dataset_source="OpenStreetMap (HOT Raw Data API {label})",
            source_description=(
                "Features are exported from the HOT Raw Data API, which tracks OpenStreetMap "
                "continuously, as one job per run; per-category exports apply tag predicates "
                "at query time."
            ),
        )

    def _prepare_postpass(self, cfg: RootConfig, src: OsmSourceConfig) -> None:
        self._prepare_live_engine(
            cfg,
            src,
            engine="postpass",
            fetch=fetch_country_parquet,
            endpoint=src.postpass_endpoint,
            timeout=src.postpass_timeout_s,
            dataset_source="OpenStreetMap (Postpass {label})",
            source_description=(
                "Features are queried from Geofabrik's Postpass database, which tracks "
                "OpenStreetMap continuously."
            ),
        )

    def _prepare_planet(self, cfg: RootConfig, src: OsmSourceConfig) -> None:
        if not dataset_identity(cfg):
            raise ValueError(
                "osm.engine=planet requires `iso3`, or `output.s3.folder` when the "
                "export has no country code"
            )
        if not src.pbf_path:
            raise ValueError(
                "osm.engine=planet requires source.osm.pbf_path "
                "(a local path or an s3:// URL to a planet PBF)"
            )
        planet_pbf = _ensure_local_pbf(src.pbf_path, Path(src.cache_dir))
        if not planet_pbf.is_file():
            if not src.auto_download_planet:
                raise FileNotFoundError(
                    f"Planet PBF not found at {planet_pbf}. "
                    "Download it with `oex-cli osm-build-cache`, "
                    "set source.osm.auto_download_planet=true, "
                    "or pass --download-if-missing on the CLI."
                )
            logger.warning(
                "Planet PBF missing at %s; auto_download_planet is on, downloading from %s",
                planet_pbf,
                src.pbf_url,
            )
            result = download_pbf(
                src.pbf_url,
                planet_pbf.parent,
                md5_url=src.md5_url,
                filename=planet_pbf.name,
            )
            planet_pbf = result.path

        if not src.snapshot or src.snapshot == "latest":
            _refuse_stale_planet(planet_pbf, cfg.frequency)
        snapshot_label = self._planet_snapshot_label(planet_pbf, src.snapshot)
        country_root = Path(src.cache_dir) / "planet" / dataset_identity(cfg)
        snapshot_dir = country_root / snapshot_label
        snapshot_dir.mkdir(parents=True, exist_ok=True)

        clip = src.planet_clip_to_boundary
        country_pbf = snapshot_dir / "country.osm.pbf"
        country_parquet = snapshot_dir / f"country-{_parquet_fingerprint(cfg, clip=clip)}.parquet"

        if not country_parquet.exists():
            if clip:
                if not country_pbf.exists():
                    boundary = resolve_boundary(cfg.iso3, cfg.boundary)
                    osmium_polygon_extract(planet_pbf, json.loads(boundary.geojson), country_pbf)
                else:
                    logger.info("Reusing existing country PBF %s", country_pbf)
                source_pbf = country_pbf
            else:
                logger.info(
                    "planet_clip_to_boundary=false; building parquet from the whole planet "
                    "%s (no clip)",
                    planet_pbf,
                )
                source_pbf = planet_pbf
            self._build_country_parquet(
                cfg, source_pbf, country_parquet, snapshot_dir, engine="planet"
            )
        else:
            logger.info("Reusing existing country parquet %s", country_parquet)

        if clip and not src.keep_pbf and country_pbf.exists():
            try:
                country_pbf.unlink()
            except OSError as exc:
                logger.warning("Could not remove %s: %s", country_pbf, exc)

        self._engine = "planet"
        self._snapshot_dir = snapshot_dir
        self._snapshot_label = snapshot_label
        self._snapshot_date = self._infer_snapshot_date(snapshot_dir, snapshot_label)
        self._dataset_source = f"OpenStreetMap planet ({snapshot_label})"
        self._country_parquet = country_parquet
        logger.info("OSM source: planet, snapshot=%s, parquet=%s", snapshot_label, country_parquet)

    def _build_country_parquet(
        self,
        cfg: RootConfig,
        country_pbf: Path,
        country_parquet: Path,
        snapshot_dir: Path,
        *,
        engine: str,
        geometry_filter: "BaseGeometry | None" = None,
    ) -> None:
        from quackosm.functions import convert_pbf_to_parquet

        union_filter = union_tag_filter(cfg.categories)
        if not union_filter:
            raise ValueError(
                f"{engine} engine requires at least one enabled category with osm.filter"
            )

        work_dir = snapshot_dir / "_qosm_work"
        if work_dir.exists():
            import shutil

            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True)

        logger.info(
            "Building country.parquet from %s (union of %d category filters: %s)",
            country_pbf.name,
            len(union_filter),
            sorted(union_filter.keys()),
        )
        # quackosm's own DuckDB sizes itself from the host unless told otherwise.
        threads = cpu_count()
        memory_gb = cfg.parallel.memory_gb or default_memory_limit_gb()
        convert_pbf_to_parquet(
            pbf_path=country_pbf,
            tags_filter=union_filter,
            geometry_filter=geometry_filter,
            result_file_path=country_parquet,
            keep_all_tags=True,
            ignore_metadata_tags=_IGNORE_METADATA_TAGS,
            sort_result=True,
            compression="zstd",
            compression_level=3,
            row_group_size=_row_group_size(memory_gb),
            working_directory=work_dir,
            ignore_cache=True,
            verbosity_mode="silent",
            cpu_limit=threads,
            duckdb_conn_kwargs={
                "config_kwargs": {"memory_limit": f"{memory_gb}GB", "threads": threads}
            },
        )
        manifest = {
            "snapshot": snapshot_dir.name,
            "iso3": cfg.iso3.upper(),
            "engine": engine,
            "country_parquet": country_parquet.name,
            "filter_keys": sorted(union_filter.keys()),
        }
        (snapshot_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        import shutil

        shutil.rmtree(work_dir, ignore_errors=True)

    @staticmethod
    def _planet_snapshot_label(planet_pbf: Path, requested: str) -> str:
        """Snapshot label for planet engine: explicit override, else PBF mtime ISO date."""
        if requested and requested != "latest":
            return requested
        ts = datetime.fromtimestamp(planet_pbf.stat().st_mtime, tz=UTC)
        return ts.date().isoformat()

    def _prepare_geofabrik(self, cfg: RootConfig, src: OsmSourceConfig) -> None:
        if not cfg.iso3:
            raise ValueError("osm.engine=geofabrik requires `iso3` in the config")

        country_root = Path(src.cache_dir) / "geofabrik" / cfg.iso3.lower()
        country_root.mkdir(parents=True, exist_ok=True)

        extract = lookup_country(cfg.iso3, index_url=src.geofabrik_index_url)
        snapshot = self._resolve_geofabrik_snapshot(src.snapshot, extract.pbf_url)
        snapshot_dir = country_root / snapshot
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        fingerprint = _parquet_fingerprint(cfg, clip=src.geofabrik_clip_to_boundary)
        country_parquet = snapshot_dir / f"country-{fingerprint}.parquet"

        if not country_parquet.exists():
            pbf_dir = country_root / "_pbf"
            pbf_path = pbf_dir / f"{extract.geofabrik_id}-latest.osm.pbf"

            if not pbf_path.exists():
                logger.info(
                    "Geofabrik extract for %s: %s (%s)",
                    cfg.iso3,
                    extract.geofabrik_id,
                    extract.pbf_url,
                )
                result = self._download_geofabrik_with_retry(
                    extract.pbf_url,
                    pbf_dir,
                    md5_url=extract.md5_url,
                    filename=pbf_path.name,
                )
                pbf_path = result.path
            else:
                logger.info("Reusing already-downloaded PBF: %s", pbf_path)

            geometry_filter = None
            if src.geofabrik_clip_to_boundary:
                from shapely.geometry import shape

                boundary = resolve_boundary(cfg.iso3, cfg.boundary)
                geometry_filter = shape(json.loads(boundary.geojson))

            self._build_country_parquet(
                cfg,
                pbf_path,
                country_parquet,
                snapshot_dir,
                engine="geofabrik",
                geometry_filter=geometry_filter,
            )

            if not src.keep_pbf:
                try:
                    pbf_path.unlink()
                    logger.info("Removed PBF after parquet build: %s", pbf_path)
                except OSError as exc:
                    logger.warning("Could not remove PBF %s: %s", pbf_path, exc)
        else:
            logger.info("Reusing existing country parquet %s", country_parquet)

        self._engine = "geofabrik"
        self._snapshot_dir = snapshot_dir
        self._snapshot_label = snapshot
        self._snapshot_date = self._infer_snapshot_date(snapshot_dir, snapshot)
        self._dataset_source = f"OpenStreetMap (Geofabrik {cfg.iso3.upper()} {snapshot})"
        self._country_parquet = country_parquet
        logger.info(
            "OSM source: geofabrik %s, snapshot=%s, parquet=%s",
            cfg.iso3.upper(),
            snapshot,
            country_parquet,
        )

    @staticmethod
    def _download_geofabrik_with_retry(
        url: str,
        dest_dir: Path,
        *,
        md5_url: str | None,
        filename: str,
    ):  # noqa: ANN202 - returns whatever download_pbf returns
        last_exc: requests.RequestException | None = None
        for attempt in range(1, _GEOFABRIK_DOWNLOAD_ATTEMPTS + 1):
            try:
                return download_pbf(url, dest_dir, md5_url=md5_url, filename=filename)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < _GEOFABRIK_DOWNLOAD_ATTEMPTS:
                    logger.warning(
                        "Geofabrik download attempt %d/%d failed (%s); retrying in %ds",
                        attempt,
                        _GEOFABRIK_DOWNLOAD_ATTEMPTS,
                        exc,
                        _GEOFABRIK_RETRY_BACKOFF_SECONDS,
                    )
                    time.sleep(_GEOFABRIK_RETRY_BACKOFF_SECONDS)
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _resolve_geofabrik_snapshot(requested: str, pbf_url: str) -> str:
        """The date Geofabrik last rebuilt the extract, so `latest` tracks upstream."""
        if requested and requested != "latest":
            return requested
        head = http.session.head(pbf_url, allow_redirects=True, timeout=60)
        head.raise_for_status()
        last_modified = head.headers.get("Last-Modified")
        if not last_modified:
            raise OsmEngineUnavailableError(f"Geofabrik sent no Last-Modified for {pbf_url}")
        return parsedate_to_datetime(last_modified).astimezone(UTC).date().isoformat()

    def _country_source_expr(self) -> str:
        """An empty extract makes quackosm omit the tags column that category selects reference,
        so one is injected when missing."""
        parquet = str(self._country_parquet)
        if self._tags_present is None:
            import duckdb

            con = duckdb.connect()
            try:
                con.execute(f"SELECT * FROM read_parquet('{parquet}') LIMIT 0")
                self._tags_present = any(col[0] == "tags" for col in con.description)
            finally:
                con.close()
        if self._tags_present:
            return f"read_parquet('{parquet}')"
        return (
            "(SELECT *, CAST(NULL AS MAP(VARCHAR, VARCHAR)) AS tags "
            f"FROM read_parquet('{parquet}')) AS src"
        )

    def query_for(self, cfg: RootConfig, category: CategoryConfig) -> SourceQuery:
        if self._country_parquet is None or not self._country_parquet.exists():
            raise CategorySkippedError(
                f"{category.name}: country parquet missing at {self._country_parquet}"
            )
        if not category.osm.enabled:
            raise CategorySkippedError(f"{category.name}: osm disabled for category")

        snapshot_label = self._snapshot_label or "unknown"
        snapshot_date = self._snapshot_date or datetime.now(UTC)
        select_fields = _inject_local_name(list(category.osm.select), cfg.iso3)

        tag_predicate = category_where_predicate(category)
        where = list(category.osm.where)
        if tag_predicate != "TRUE":
            where.append(tag_predicate)

        engine_label = self._engine or "osm"
        return SourceQuery(
            source_expr=self._country_source_expr(),
            select_fields=select_fields,
            where_conditions=where,
            bbox_cols="geom",
            dataset_source=self._dataset_source,
            source_url="https://www.openstreetmap.org/",
            source_description=(
                "OpenStreetMap is a community-edited geographic dataset of the world. "
                f"{self._source_description}"
            ),
            snapshot_date=snapshot_date,
            snapshot_label=snapshot_label,
            extra_readme_lines=[f"Engine: {engine_label}"],
        )

    @staticmethod
    def _infer_snapshot_date(snapshot_dir: Path, snapshot_label: str) -> datetime:
        manifest = snapshot_dir / "manifest.json"
        if manifest.exists():
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                snap = payload.get("snapshot", snapshot_label)
                return datetime.fromisoformat(snap).replace(tzinfo=UTC)
            except (ValueError, KeyError, OSError) as exc:
                logger.warning("manifest.json at %s is malformed: %s", manifest, exc)
        try:
            return datetime.fromisoformat(snapshot_label).replace(tzinfo=UTC)
        except ValueError:
            return datetime.now(UTC)
