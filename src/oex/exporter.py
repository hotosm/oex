"""Per-category export loop, shared by Overture and OSM sources."""

import concurrent.futures
import itertools
import os
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pystac
from hdx.data.hdxobject import HDXError

from oex.boundary import resolve_boundary
from oex.catalog import S3CatalogStore, link_collections
from oex.catalog import layout as catalog_layout
from oex.config.schema import (
    CategoryConfig,
    PcodesSourceConfig,
    RootConfig,
    dataset_identity,
    license_label,
)
from oex.duckdb_session import connect
from oex.hdx_publisher import (
    HDX_SHORT_SOURCE,
    SOURCE_RANK,
    CombinedCategory,
    ExtraResource,
    HdxPublisher,
    PublishContext,
    combined_title,
    country_name,
    dataset_title,
    hdx_dataset_url,
    links_related_datasets,
    related_dataset_links,
)
from oex.logging_setup import get_logger
from oex.metadata import compute_metadata
from oex.naming import (
    COMBINED_SLUG,
    category_dataset_name,
    category_label,
    combined_dataset_name,
    layer_dataset_name,
    resource_prefix,
    slugify,
)
from oex.palette import DEFAULT_PALETTE
from oex.pcodes import (
    PcodeCacheEntry,
    ensure_admin_parquets,
    resolve_pcodes_config,
    tag_table,
)
from oex.pcodes.tagger import parse_boundary_resolution
from oex.preflight import check_tile_builder, check_writable_paths
from oex.report import SourceMetadata
from oex.report.page import PageLayer, downloads_from_assets, render_dataset_page
from oex.s3 import artifact_key, build_layer_key, list_layer_urls
from oex.s3 import resolve as s3_resolve
from oex.s3 import upload as s3_upload
from oex.sources.base import CategorySkippedError, SourceQuery, SourceRunner
from oex.sql import build_select_clause, build_where_clause, materialise
from oex.stac import (
    LayerAssets,
    asset_href,
    asset_key,
    build_collection,
    build_item,
    collection_filename,
    href_or_filename,
    item_filename,
    item_href,
    license_href,
    to_payload,
    write_json,
)
from oex.state import StateStore
from oex.system import adaptive_parallel_resources, cpu_count
from oex.translit import transliterate_table
from oex.writers import (
    GEOMETRY_LABELS,
    TileLayer,
    build_combined_pmtiles,
    geometry_labels,
    write_format,
    write_geoparquet,
    write_pmtiles,
)
from oex.zip_bundle import make_zip

logger = get_logger(__name__)

_PROJECT_URL = "https://github.com/osgeonepal/oex"
# State slug for the single combined dataset's upload marker.
_COMBINED_SLUG = "__combined__"
_FORMAT_LABELS = {
    "gpkg": "GeoPackage (gpkg)",
    "shp": "ESRI Shapefile (shp)",
    "geojson": "GeoJSON (geojson)",
    "kml": "Keyhole Markup Language (kml)",
}


def _oex_version() -> str:
    try:
        return version("oex")
    except PackageNotFoundError:
        return "0.2.0+source"


@dataclass
class CategoryResult:
    name: str
    status: str
    feature_count: int = 0
    duration_s: float = 0.0
    zip_paths: list[Path] = field(default_factory=list)
    hdx_dataset: str | None = None
    error: str | None = None


@dataclass
class BuiltCategory:
    """A processed category plus the artifacts phase-B combined publishing needs."""

    result: CategoryResult
    category: CategoryConfig
    zip_paths: list[Path] = field(default_factory=list)
    metadata_json_path: Path | None = None
    item_path: Path | None = None
    metadata_obj: object | None = None
    source_metadata: SourceMetadata | None = None
    geoparquet_path: Path | None = None
    publish_geoparquet: bool = False
    geoparquet_url: str | None = None
    query: SourceQuery | None = None


@dataclass
class ExportResult:
    iso3: str
    source_name: str
    categories: dict[str, CategoryResult] = field(default_factory=dict)
    total_duration_s: float = 0.0

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.categories.values() if r.status == "ok")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.categories.values() if r.status == "failed")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.categories.values() if r.status == "skipped")

    @property
    def empty(self) -> int:
        return sum(1 for r in self.categories.values() if r.status == "empty")


class Exporter:
    def __init__(self, cfg: RootConfig, runner: SourceRunner):
        self._cfg = cfg
        self._runner = runner
        self._pcodes_cfg: PcodesSourceConfig = resolve_pcodes_config(cfg.source)
        self._pcode_cache: dict[int, PcodeCacheEntry] | None = None
        self._state: StateStore | None = None
        # Spatial join peaks per session; serialise across workers to
        # prevent concurrent peaks from exhausting machine RAM.
        self._pcode_tag_semaphore = threading.Semaphore(1)
        self._adaptive_workers, self._adaptive_mem_gb = adaptive_parallel_resources()
        self._staged_collections: list[tuple[str, str]] = []

    def run(self) -> ExportResult:
        if not dataset_identity(self._cfg):
            raise ValueError("config.iso3 or config.output.s3.folder is required")
        if not self._cfg.categories:
            raise ValueError("config.categories is empty")
        if self._want_metadata():
            for category in self._cfg.categories:
                license_href(category.hdx.license, category.hdx.license_url)
        if self._cfg.hdx.push and self._want_metadata() and not self._cfg.output.s3.enabled:
            raise ValueError(
                "hdx.push is true and layer metadata is on, but output.s3.enabled is false: "
                "the Collection links each layer's Item by URL and the Items are published "
                "to the bucket, so without S3 every link would be a 404"
            )

        check_writable_paths(self._cfg)
        check_tile_builder(self._cfg)

        iso = self._cfg.iso3.upper() or dataset_identity(self._cfg)
        cat_names = [c.name for c in self._cfg.categories]
        logger.info(
            "[%s/%s] run starting: %d categor%s, formats=%s, parallel=%s, hdx_push=%s",
            iso,
            self._runner.name,
            len(cat_names),
            "y" if len(cat_names) == 1 else "ies",
            self._cfg.output.formats,
            self._cfg.parallel.enabled,
            self._cfg.hdx.push,
        )
        logger.info("[%s/%s] categories: %s", iso, self._runner.name, ", ".join(cat_names))
        _workers = self._cfg.parallel.threads or self._adaptive_workers
        _mem = self._cfg.parallel.memory_gb or self._adaptive_mem_gb
        logger.info(
            "[%s/%s] resources: workers=%d (cfg=%s adaptive=%d) memory_gb=%d (cfg=%s adaptive=%d)",
            iso,
            self._runner.name,
            _workers,
            self._cfg.parallel.threads,
            self._adaptive_workers,
            _mem,
            self._cfg.parallel.memory_gb,
            self._adaptive_mem_gb,
        )

        out_root = Path(self._cfg.output.dir) / dataset_identity(self._cfg) / self._runner.name
        out_root.mkdir(parents=True, exist_ok=True)
        self._state = StateStore(
            path=out_root / ".state.json",
            iso3=self._cfg.iso3,
            source=self._runner.name,
        )

        peeked: str | None = None
        if self._cfg.output.resume:
            peeked = self._runner.peek_snapshot_label(self._cfg)
            if self._cfg.hdx.combine:
                already_done = bool(peeked) and self._state.is_uploaded(
                    _COMBINED_SLUG, snapshot_label=peeked
                )
            else:
                already_done = bool(peeked) and all(
                    self._state.is_uploaded(slugify(c.name), snapshot_label=peeked)
                    for c in self._cfg.categories
                )
            if already_done:
                logger.info(
                    "[%s/%s] resume: already uploaded for snapshot %s; "
                    "skipping boundary fetch, pcode cache, and per-category work",
                    iso,
                    self._runner.name,
                    peeked,
                )
                return ExportResult(iso3=iso, source_name=self._runner.name)

        boundary = resolve_boundary(self._cfg.iso3, self._cfg.boundary)
        bbox = boundary.bbox
        logger.info(
            "[%s/%s] boundary: %s bbox=(%.4f, %.4f, %.4f, %.4f)",
            iso,
            self._runner.name,
            boundary.source,
            bbox[0],
            bbox[1],
            bbox[2],
            bbox[3],
        )

        self._runner.prepare(self._cfg)

        # Serialise the download here so parallel-category threads share one cache.
        if self._pcodes_cfg.enabled:
            logger.info(
                "[%s/%s] pcodes: preparing fieldmaps cache (levels=%s, dir=%s)",
                iso,
                self._runner.name,
                self._pcodes_cfg.levels,
                self._pcodes_cfg.cache_dir,
            )
            self._pcode_cache = ensure_admin_parquets(
                cache_dir=Path(self._pcodes_cfg.cache_dir),
                levels=self._pcodes_cfg.levels,
                manifest_url=self._pcodes_cfg.manifest_url,
                parquet_url_template=self._pcodes_cfg.parquet_url_template,
                manifest_group=self._pcodes_cfg.manifest_group,
            )

        publisher: HdxPublisher | None = None
        if self._cfg.hdx.push:
            publisher = HdxPublisher(self._cfg.hdx)

        # Layer staging uploads on output.s3.enabled alone, independently of hdx.push.
        if self._cfg.output.s3.enabled:
            from oex.s3 import preflight as s3_preflight

            logger.info("[%s/%s] s3: preflight check", iso, self._runner.name)
            s3_preflight(self._cfg.output.s3)

        result = ExportResult(iso3=iso, source_name=self._runner.name)
        start = time.time()

        built_list: list[BuiltCategory] = []
        _resolved_workers = self._cfg.parallel.threads or self._adaptive_workers
        if self._cfg.parallel.enabled and len(self._cfg.categories) > 1 and _resolved_workers > 1:
            workers = min(_resolved_workers, len(self._cfg.categories))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(
                        self._process_category, category, boundary, out_root, publisher
                    ): category
                    for category in self._cfg.categories
                }
                for fut in concurrent.futures.as_completed(futures):
                    built = fut.result()
                    built_list.append(built)
                    result.categories[built.result.name] = built.result
        else:
            for category in self._cfg.categories:
                built = self._process_category(category, boundary, out_root, publisher)
                built_list.append(built)
                result.categories[built.result.name] = built.result

        if self._cfg.hdx.combine:
            self._finish_combined(
                built_list,
                out_root,
                publisher,
                peeked_label=peeked,
                boundary_bbox=bbox,
                boundary_geojson=boundary.geojson,
            )

        if self._staged_collections and (where := catalog_layout(self._cfg)):
            link_collections(
                where,
                self._cfg.output.stac,
                S3CatalogStore(self._cfg),
                self._staged_collections,
                area_title=self._area_title(),
            )

        result.total_duration_s = time.time() - start
        logger.info(
            "[%s] %s done in %.1fs: %d ok, %d empty, %d skipped, %d failed",
            result.iso3,
            self._runner.name,
            result.total_duration_s,
            result.succeeded,
            result.empty,
            result.skipped,
            result.failed,
        )
        return result

    def _process_category(
        self,
        category: CategoryConfig,
        boundary: object,
        out_root: Path,
        publisher: HdxPublisher | None,
    ) -> BuiltCategory:
        # Combined mode defers publishing to a single phase-B call, so no
        # category publishes on its own and artifacts are kept until then.
        combine = self._cfg.hdx.combine
        cat_start = time.time()
        slug = slugify(category.name)
        cat_tag = f"[{category.name}/{self._runner.name}]"
        logger.info("%s starting", cat_tag)

        def _early(result: CategoryResult) -> BuiltCategory:
            return BuiltCategory(result=result, category=category)

        try:
            query = self._runner.query_for(self._cfg, category)
        except CategorySkippedError as skip:
            logger.info("%s skipped: %s", cat_tag, skip)
            return _early(
                CategoryResult(
                    name=category.name,
                    status="skipped",
                    duration_s=time.time() - cat_start,
                    error=str(skip),
                )
            )

        formats = category.formats or self._cfg.output.formats
        if not formats:
            logger.info("%s skipped: no output formats configured", cat_tag)
            return _early(
                CategoryResult(
                    name=category.name,
                    status="skipped",
                    duration_s=time.time() - cat_start,
                    error="no output formats configured",
                )
            )
        # Never zipped, so a client reads it in place over HTTP; it also feeds the
        # combined tiles, so it can be wanted without being requested.
        publish_geoparquet = "geoparquet" in formats
        want_geoparquet = publish_geoparquet or (combine and self._cfg.output.pmtiles.enabled)
        hdx_formats = [f for f in formats if f != "geoparquet"]

        if self._cfg.output.resume and self._state is not None and not combine:
            entry = self._state.get(slug)
            snapshot_label = query.snapshot_label or "unknown"
            if entry and self._state.is_uploaded(slug, snapshot_label=snapshot_label):
                logger.info(
                    "%s resume: already built and uploaded (%s); skipping",
                    cat_tag,
                    entry.uploaded_utc,
                )
                return _early(
                    CategoryResult(
                        name=category.name,
                        status="ok",
                        feature_count=0,
                        duration_s=time.time() - cat_start,
                        zip_paths=[Path(p) for p in entry.zip_paths],
                        hdx_dataset=entry.hdx_dataset,
                    )
                )

        logger.info(
            "%s source: %s | snapshot: %s",
            cat_tag,
            query.dataset_source,
            query.snapshot_label,
        )

        from typing import cast

        from oex.boundary import Boundary

        boundary_obj = cast(Boundary, boundary)

        d = self._cfg.duckdb
        _parallel_workers = max(1, self._cfg.parallel.threads or self._adaptive_workers)
        _duckdb_threads = max(2, cpu_count() // _parallel_workers)
        table = f"{slug}_{os.getpid()}_{int(time.time() * 1000)}"
        db_path = Path(d.temp_dir) / f"{table}.duckdb"
        conn = connect(
            path=db_path,
            threads=_duckdb_threads,
            memory_gb=self._cfg.parallel.memory_gb or self._adaptive_mem_gb,
            s3_region=getattr(
                self._cfg.source.get("overture"),
                "s3_region",
                "us-west-2",
            ),
            temp_dir=d.temp_dir,
            http_retries=d.http_retries,
            http_retry_wait_ms=d.http_retry_wait_ms,
            http_retry_backoff=d.http_retry_backoff,
            http_timeout_s=d.http_timeout_s,
            anonymous_s3_bucket=getattr(
                self._cfg.source.get("overture"), "s3_bucket", "overturemaps-us-west-2"
            ),
        )
        try:
            select_clause = build_select_clause(query.select_fields)
            where_clause = build_where_clause(boundary_obj, query.where_conditions, query.bbox_cols)
            logger.info("%s querying source...", cat_tag)
            mat_start = time.time()
            count = materialise(conn, table, query.source_expr, select_clause, where_clause)
            logger.info(
                "%s queried %s features in %.1fs",
                cat_tag,
                f"{count:,}",
                time.time() - mat_start,
            )
            if count == 0:
                logger.info("%s empty: no features within boundary", cat_tag)
                if publisher is not None and not self._cfg.hdx.combine:
                    self._refresh_empty_category(category, query, publisher, cat_tag)
                return _early(
                    CategoryResult(
                        name=category.name,
                        status="empty",
                        feature_count=0,
                        duration_s=time.time() - cat_start,
                    )
                )

            if self._pcode_cache is not None and not category.skip_pcodes:
                tag_start = time.time()
                logger.info(
                    "%s tagging with pcodes (levels=%s)...",
                    cat_tag,
                    self._pcodes_cfg.levels,
                )
                boundary_resolution = parse_boundary_resolution(
                    category.boundary_resolution or self._pcodes_cfg.boundary_resolution
                )
                with self._pcode_tag_semaphore:
                    tag_table(
                        conn,
                        table=table,
                        iso3=self._cfg.iso3,
                        cache_entries=self._pcode_cache,
                        levels=self._pcodes_cfg.levels,
                        geom_column="geom",
                        boundary_resolution=boundary_resolution,
                    )
                logger.info("%s pcodes tagged in %.1fs", cat_tag, time.time() - tag_start)
            elif self._pcode_cache is not None and category.skip_pcodes:
                logger.info("%s pcodes skipped (skip_pcodes=true)", cat_tag)

            if category.transliterate:
                translit_start = time.time()
                logger.info(
                    "%s transliterating %d column(s)...",
                    cat_tag,
                    len(category.transliterate),
                )
                transliterate_table(conn, table=table, rules=category.transliterate)
                logger.info(
                    "%s transliteration done in %.1fs",
                    cat_tag,
                    time.time() - translit_start,
                )

            need_metadata = self._want_metadata()
            metadata_obj = None
            metadata_dict = None
            if need_metadata:
                logger.info("%s computing metadata...", cat_tag)
                metadata_obj = compute_metadata(
                    conn,
                    table,
                    temporal_column=category.temporal.column,
                )

            dt_name = layer_dataset_name(self._cfg, category)
            metadata_json_path: Path | None = None
            item_path: Path | None = None
            want_item = False
            source_metadata: SourceMetadata | None = None
            if metadata_obj is not None:
                source_metadata = SourceMetadata(
                    source_name=self._runner.name,
                    snapshot_label=query.snapshot_label,
                    dataset_source=query.dataset_source,
                    generated_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    oex_version=_oex_version(),
                    license_label=license_label(category.hdx.license),
                    license_url=category.hdx.license_url,
                    pcode_source_date=(
                        next(iter(self._pcode_cache.values())).upstream_date
                        if self._pcode_cache
                        else None
                    ),
                    boundary=boundary_obj.source,
                    metadata=metadata_obj,
                )
                want_item = True
                if not combine:
                    # A combined dataset is covered by one Collection over every layer.
                    metadata_json_path = out_root / collection_filename(dt_name, self._runner.name)
                # Named before the artifacts exist, so the archived copy lists none.
                metadata_dict = to_payload(
                    build_item(
                        LayerAssets(
                            category=category,
                            title=dataset_title(self._cfg, category),
                            source_metadata=source_metadata,
                            dataset_name=dt_name,
                            footprint=self._item_footprint(boundary_obj.geojson),
                        ),
                        self._cfg,
                    )
                )

            logger.info("%s writing %d format(s): %s", cat_tag, len(hdx_formats), hdx_formats)
            zip_paths = self._materialise_outputs(
                conn=conn,
                table=table,
                slug=slug,
                category=category,
                query=query,
                formats=hdx_formats,
                out_root=out_root,
                metadata_report=metadata_dict,
                boundary=boundary_obj,
                feature_count=count,
            )

            if not zip_paths and not want_geoparquet:
                # Every format failed; skipping rather than failing keeps one bad category
                # from turning the whole country red.
                logger.warning(
                    "%s no formats succeeded; skipping HDX upload for this category",
                    cat_tag,
                )
                return _early(
                    CategoryResult(
                        name=category.name,
                        status="skipped",
                        feature_count=count,
                        duration_s=time.time() - cat_start,
                        error="no formats succeeded",
                    )
                )

            total_mb = sum(p.stat().st_size for p in zip_paths) / (1024 * 1024)

            geoparquet_path: Path | None = None
            geoparquet_url: str | None = None
            if want_geoparquet:
                geoparquet_path = write_geoparquet(
                    conn, table, out_root / "_layers" / f"{slug}.parquet"
                )

            pmtiles_extra: list[ExtraResource] = []
            pmtiles_path: Path | None = None
            if self._cfg.output.pmtiles.enabled and not combine:
                pm = self._cfg.output.pmtiles
                pmtiles_path = out_root / f"{dt_name}_{self._runner.name}.pmtiles"
                # Named for the category, so one map style serves every country's tiles.
                write_pmtiles(
                    conn,
                    table,
                    pmtiles_path,
                    layer=slug,
                    min_zoom=pm.min_zoom,
                    max_zoom=pm.max_zoom,
                    max_extra_zooms=pm.max_extra_zooms,
                )
                pmtiles_extra.append(
                    ExtraResource(
                        path=pmtiles_path,
                        fmt="pmtiles",
                        description=(
                            f"Vector tiles of {category_label(category)} for web maps (PMTiles)."
                        ),
                    )
                )

            # Taken before staging so the S3 upload stays out of the build time.
            export_seconds = time.time() - cat_start
            if geoparquet_path is not None:
                geoparquet_url = self._stage_layer_parquet(geoparquet_path, slug, cat_tag)
                if publish_geoparquet and self._cfg.hdx.push and geoparquet_url is None:
                    raise ValueError(
                        "output.formats includes 'geoparquet' and hdx.push is true, but "
                        "output.s3.enabled is false: the HDX resource points at the staged "
                        "object, so there is nothing to publish"
                    )

            if want_item and source_metadata is not None:
                # After the artifacts exist, so the layer can name every one as an asset.
                layer = LayerAssets(
                    category=category,
                    title=dataset_title(self._cfg, category),
                    source_metadata=source_metadata,
                    dataset_name=dt_name,
                    artifacts=zip_paths,
                    geoparquet_url=geoparquet_url,
                    geoparquet_path=geoparquet_path,
                    pmtiles_url=self._artifact_url(pmtiles_path, slug),
                    pmtiles_path=pmtiles_path,
                    export_seconds=export_seconds,
                    footprint=self._item_footprint(boundary_obj.geojson),
                )
                item_path = self._write_layer_metadata(layer, out_root, metadata_json_path)

            if source_metadata is not None and self._cfg.output.report.enabled:
                self._write_local_report(
                    category=category,
                    source_metadata=source_metadata,
                    report_path=out_root / f"{dt_name}_{self._runner.name}_report.html",
                    zip_paths=zip_paths,
                    pmtiles_path=pmtiles_path,
                    boundary_bbox=boundary_obj.bbox,
                    boundary_geojson=boundary_obj.geojson,
                    cat_tag=cat_tag,
                )

            if self._state is not None:
                self._state.mark_built(
                    slug,
                    snapshot_label=query.snapshot_label or "unknown",
                    zip_paths=zip_paths,
                    metadata_json_path=metadata_json_path,
                )

            dataset_name: str | None = None
            if publisher is not None and not combine:
                logger.info("%s uploading %d artifact(s) to HDX...", cat_tag, len(zip_paths))
                t_min, t_max = _temporal_bounds_for_hdx(metadata_obj)
                ctx = PublishContext(
                    dataset_source=query.dataset_source,
                    snapshot_date=query.snapshot_date,
                    source_name=self._runner.name,
                    metadata_json_path=metadata_json_path,
                    combined_report_enabled=self._cfg.output.report.enabled,
                    output_dir=out_root,
                    s3=self._cfg.output.s3,
                    temporal_min=t_min,
                    temporal_max=t_max,
                    boundary_bbox=boundary_obj.bbox,
                )
                dataset_name = publisher.publish(
                    self._cfg,
                    category,
                    zip_paths,
                    ctx,
                    extra_resources=pmtiles_extra,
                    geoparquet_url=geoparquet_url if publish_geoparquet else None,
                    geoparquet_path=geoparquet_path,
                )
                if self._state is not None:
                    self._state.mark_uploaded(slug, hdx_dataset=dataset_name)
                if self._cfg.output.remove_after_upload:
                    extra_paths = [e.path for e in pmtiles_extra]
                    if item_path is not None:
                        extra_paths.append(item_path)
                    _remove_uploaded_outputs(zip_paths, metadata_json_path, extra_paths)
                    logger.info(
                        "%s removed %d local output(s) after upload", cat_tag, len(zip_paths)
                    )
            elif publisher is None and not combine and self._cfg.output.s3.enabled:
                self._upload_category_to_s3(zip_paths, slug, cat_tag)

            logger.info(
                "%s done: %s features, %d artifact(s), %.0f MB total in %.1fs",
                cat_tag,
                f"{count:,}",
                len(zip_paths),
                total_mb,
                time.time() - cat_start,
            )
            return BuiltCategory(
                result=CategoryResult(
                    name=category.name,
                    status="ok",
                    feature_count=count,
                    duration_s=time.time() - cat_start,
                    zip_paths=zip_paths,
                    hdx_dataset=dataset_name,
                ),
                category=category,
                zip_paths=zip_paths,
                metadata_json_path=metadata_json_path,
                item_path=item_path,
                metadata_obj=metadata_obj,
                source_metadata=source_metadata,
                geoparquet_path=geoparquet_path,
                publish_geoparquet=publish_geoparquet,
                geoparquet_url=geoparquet_url,
                query=query,
            )
        except Exception as exc:  # noqa: BLE001  per-category boundary; logged + reported
            logger.exception("%s failed", cat_tag)
            return _early(
                CategoryResult(
                    name=category.name,
                    status="failed",
                    duration_s=time.time() - cat_start,
                    error=str(exc),
                )
            )
        finally:
            conn.close()
            db_path.unlink(missing_ok=True)

    def _page_layer(
        self,
        category: CategoryConfig,
        source_metadata: SourceMetadata,
        zip_paths: list[Path],
        pmtiles_path: Path | None,
        *,
        tileset: tuple[str, str] | None = None,
    ) -> PageLayer:
        """A layer as the page shows it. Hrefs are filenames, so the page works under
        any host serving the output directory."""
        slug = slugify(category.name)
        assets = {
            asset_key(path.name, slug): {"href": path.name, "file:size": path.stat().st_size}
            for path in zip_paths
        }
        geoparquet = Path("_layers") / f"{slug}.parquet"
        if zip_paths and (zip_paths[0].parent / geoparquet).exists():
            size = (zip_paths[0].parent / geoparquet).stat().st_size
            assets["geoparquet"] = {"href": str(geoparquet), "file:size": size}
        item = item_filename(layer_dataset_name(self._cfg, category), self._runner.name)
        downloads = downloads_from_assets(assets)
        own_tiles = (pmtiles_path.name, slug) if pmtiles_path else None
        index = [slugify(c.name) for c in self._cfg.categories].index(slug)
        palette = self._cfg.output.report.palette or DEFAULT_PALETTE
        return PageLayer(
            key=f"{slug}-{self._runner.name}",
            label=category_label(category),
            color=palette[index % len(palette)],
            source=source_metadata,
            downloads=downloads,
            tileset=tileset or own_tiles,
            match_key=f"{slug}|{self._runner.name}" if tileset else None,
            metadata_href=item if self._want_metadata() else None,
        )

    def _write_local_report(
        self,
        *,
        category: CategoryConfig,
        source_metadata: SourceMetadata,
        report_path: Path,
        zip_paths: list[Path],
        pmtiles_path: Path | None,
        boundary_bbox: tuple[float, float, float, float],
        cat_tag: str,
        boundary_geojson: str | None = None,
    ) -> None:
        """Write this source's report next to its outputs, mapping the local tileset."""
        layer = self._page_layer(category, source_metadata, zip_paths, pmtiles_path)
        report_path.write_text(
            render_dataset_page(
                title=dataset_title(self._cfg, category),
                layers=[layer],
                theme=self._cfg.output.report.theme,
                boundary_bbox=boundary_bbox,
                boundary_geojson=boundary_geojson,
                map_assets=self._cfg.output.report.map_assets,
                dataset_url=self._hdx_url(self._published_dataset_name(category)),
                related=related_dataset_links(self._cfg, category),
            ),
            encoding="utf-8",
        )
        logger.info("%s wrote local report -> %s", cat_tag, report_path)

    def _finish_combined(
        self,
        built_list: list[BuiltCategory],
        out_root: Path,
        publisher: HdxPublisher | None,
        *,
        peeked_label: str | None,
        boundary_bbox: tuple[float, float, float, float],
        boundary_geojson: str | None = None,
    ) -> None:
        """Build the combined artifacts, then publish them when HDX push is on."""
        iso = self._cfg.iso3.upper()
        built_ok = [b for b in built_list if b.result.status == "ok" and b.zip_paths]
        if not built_ok:
            logger.warning("[%s/%s] combine: no successful categories", iso, self._runner.name)
            return

        order = {c.name: i for i, c in enumerate(self._cfg.categories)}
        built_ok.sort(key=lambda b: order.get(b.category.name, len(order)))

        dt_name = combined_dataset_name(self._cfg)
        pmtiles_path = self._build_combined_tiles(built_ok, out_root, dt_name)

        if publisher is None:
            # Written locally too, so the output directory reads as a catalog on its own.
            self._write_combined_metadata(
                built_ok, out_root, dt_name, pmtiles_path, boundary_geojson
            )
            self._write_local_page(
                built_ok,
                out_root,
                dt_name=dt_name,
                pmtiles_path=pmtiles_path,
                boundary_bbox=boundary_bbox,
                boundary_geojson=boundary_geojson,
            )
            return

        self._publish_combined(
            built_ok,
            out_root,
            publisher,
            dt_name=dt_name,
            pmtiles_path=pmtiles_path,
            peeked_label=peeked_label,
            boundary_bbox=boundary_bbox,
            boundary_geojson=boundary_geojson,
        )

    def _write_local_page(
        self,
        built_ok: list[BuiltCategory],
        out_root: Path,
        *,
        dt_name: str,
        pmtiles_path: Path | None,
        boundary_bbox: tuple[float, float, float, float],
        boundary_geojson: str | None = None,
    ) -> None:
        """The dataset page from this run's own layers. The published page also covers layers an
        earlier run of another source left on the dataset, which only HDX knows about."""
        if not self._cfg.output.report.enabled:
            return
        tileset = (pmtiles_path.name, pmtiles_path.stem) if pmtiles_path else None
        layers = [
            self._page_layer(b.category, b.source_metadata, b.zip_paths, None, tileset=tileset)
            for b in built_ok
            if b.source_metadata is not None
        ]
        if not layers:
            return

        place = country_name(self._cfg.iso3, dataset_name=self._cfg.dataset_name)
        q0 = built_ok[0].query
        hdx_source = HDX_SHORT_SOURCE.get(self._runner.name) or (q0.dataset_source if q0 else "")
        html = render_dataset_page(
            title=combined_title(self._cfg, place, hdx_source),
            layers=layers,
            theme=self._cfg.output.report.theme,
            boundary_bbox=boundary_bbox,
            boundary_geojson=boundary_geojson,
            map_assets=self._cfg.output.report.map_assets,
            dataset_url=self._hdx_url(dt_name),
        )
        page_path = out_root / f"{dt_name}_overview.html"
        page_path.write_text(html, encoding="utf-8")
        logger.info(
            "[%s/%s] combine: wrote local overview (%d layers) -> %s",
            self._cfg.iso3.upper(),
            self._runner.name,
            len(layers),
            page_path,
        )

    def _publish_combined(
        self,
        built_ok: list[BuiltCategory],
        out_root: Path,
        publisher: HdxPublisher,
        *,
        dt_name: str,
        pmtiles_path: Path | None,
        peeked_label: str | None,
        boundary_bbox: tuple[float, float, float, float],
        boundary_geojson: str | None = None,
    ) -> None:
        iso = self._cfg.iso3.upper()
        entries = [
            CombinedCategory(
                category=b.category,
                zip_paths=b.zip_paths,
                metadata_json_path=b.metadata_json_path,
                geoparquet_url=b.geoparquet_url if b.publish_geoparquet else None,
                geoparquet_path=b.geoparquet_path,
            )
            for b in built_ok
        ]
        metadata_path = self._write_combined_metadata(
            built_ok, out_root, dt_name, pmtiles_path, boundary_geojson
        )
        t_min, t_max = _combined_temporal_bounds([b.metadata_obj for b in built_ok])
        q0 = built_ok[0].query
        assert q0 is not None
        ctx = PublishContext(
            dataset_source=q0.dataset_source,
            snapshot_date=q0.snapshot_date,
            source_name=self._runner.name,
            output_dir=out_root,
            s3=self._cfg.output.s3,
            temporal_min=t_min,
            temporal_max=t_max,
            boundary_bbox=boundary_bbox,
            boundary_geojson=boundary_geojson,
        )
        logger.info(
            "[%s/%s] combine: publishing %d categories as single dataset %s",
            iso,
            self._runner.name,
            len(entries),
            dt_name,
        )
        dataset_name = publisher.publish_combined(
            self._cfg,
            entries,
            ctx,
            pmtiles_path=pmtiles_path,
            metadata_path=metadata_path,
            page_enabled=self._cfg.output.report.enabled,
        )

        if self._state is not None:
            snap = peeked_label or (q0.snapshot_label or "unknown")
            self._state.mark_built(
                _COMBINED_SLUG, snapshot_label=snap, zip_paths=[], metadata_json_path=None
            )
            self._state.mark_uploaded(_COMBINED_SLUG, hdx_dataset=dataset_name)

        for b in built_ok:
            b.result.hdx_dataset = dataset_name

        if self._cfg.output.remove_after_upload:
            # Keep the per-layer GeoParquets: they are a deliverable and the
            # source of truth for future combined-tile rebuilds.
            for b in built_ok:
                items = [b.item_path] if b.item_path else None
                _remove_uploaded_outputs(b.zip_paths, b.metadata_json_path, items)
            if pmtiles_path is not None:
                pmtiles_path.unlink(missing_ok=True)
            if metadata_path is not None:
                metadata_path.unlink(missing_ok=True)
            logger.info(
                "[%s/%s] combine: removed local outputs after upload", iso, self._runner.name
            )

    def _item_footprint(self, boundary_geojson: str | None) -> str | None:
        """Only a simplified outline is small enough to embed in every Item."""
        return boundary_geojson if self._cfg.boundary.simplify_meters else None

    def _write_combined_metadata(
        self,
        built_ok: list[BuiltCategory],
        out_root: Path,
        dt_name: str,
        pmtiles_path: Path | None = None,
        boundary_geojson: str | None = None,
    ) -> Path | None:
        """One Collection over every layer this source built, named per source so a second
        source's run does not overwrite the first's."""
        layers = [
            LayerAssets(
                category=b.category,
                title=dataset_title(self._cfg, b.category),
                source_metadata=metadata,
                dataset_name=layer_dataset_name(self._cfg, b.category),
                artifacts=b.zip_paths,
                geoparquet_url=b.geoparquet_url,
                footprint=self._item_footprint(boundary_geojson),
            )
            for b in built_ok
            if (metadata := b.source_metadata) is not None
        ]
        if not layers or not self._want_metadata():
            return None
        path = out_root / collection_filename(dt_name, self._runner.name)
        write_json(
            self._collection_for(layers, dt_name, COMBINED_SLUG, pmtiles_path=pmtiles_path), path
        )
        self._stage_metadata(path, COMBINED_SLUG)
        self._staged_collections.append(
            (self._collection_title(layers), href_or_filename(self._cfg, COMBINED_SLUG, path.name))
        )
        return path

    def _artifact_url(self, path: Path | None, category_slug: str) -> str | None:
        return asset_href(self._cfg, category_slug, path.name) if path else None

    def _want_metadata(self) -> bool:
        """The report is rendered from the layer metadata, so asking for it asks for both."""
        return self._cfg.output.metadata or self._cfg.output.report.enabled

    def _write_layer_metadata(
        self, layer: LayerAssets, out_root: Path, collection_path: Path | None
    ) -> Path:
        """The layer's Item file, and its Collection when the layer is its own dataset. The
        filename is derived here so the staged key and the `rel="item"` href cannot drift."""
        item_path = out_root / item_filename(layer.dataset_name, self._runner.name)
        combined = self._cfg.hdx.combine
        collection_id = combined_dataset_name(self._cfg) if combined else layer.dataset_name
        collection_slug = COMBINED_SLUG if combined else slugify(layer.category.name)
        collection_href = href_or_filename(
            self._cfg, collection_slug, collection_filename(collection_id, self._runner.name)
        )
        item = build_item(
            layer,
            self._cfg,
            self_href=item_href(self._cfg, layer),
            collection_id=collection_id,
            collection_href=collection_href,
            hdx_url=self._hdx_url(self._published_dataset_name(layer.category)),
            page_url=self._page_url(layer.category),
        )
        write_json(item, item_path)
        self._stage_metadata(item_path, slugify(layer.category.name))
        if collection_path is not None:
            write_json(
                self._collection_for([layer], collection_id, collection_slug), collection_path
            )
            self._stage_metadata(collection_path, collection_slug)
            self._staged_collections.append((self._collection_title([layer]), collection_href))
        return item_path

    def _area_title(self) -> str:
        cfg = self._cfg
        if not cfg.iso3:
            return cfg.dataset_name or cfg.output.s3.folder
        place = country_name(cfg.iso3, dataset_name=cfg.dataset_name)
        return (
            combined_title(cfg, place, "") if cfg.hdx.combine and cfg.hdx.combined.title else place
        )

    def _published_dataset_name(self, category: CategoryConfig) -> str:
        """The HDX dataset a category's layer is published to, which an S3 folder does not
        change, unlike the names of the files themselves."""
        if self._cfg.hdx.combine:
            return combined_dataset_name(self._cfg)
        return category_dataset_name(self._cfg, category)

    def _hdx_url(self, dataset_name: str) -> str | None:
        """The HDX page a published dataset lives on; None when nothing goes to HDX."""
        return hdx_dataset_url(self._cfg.hdx.site, dataset_name) if self._cfg.hdx.push else None

    def _collection_for(
        self,
        layers: list[LayerAssets],
        dataset_name: str,
        category_slug: str,
        *,
        pmtiles_path: Path | None = None,
    ) -> pystac.Collection:
        filename = collection_filename(dataset_name, self._runner.name)
        return build_collection(
            dataset_name=dataset_name,
            title=self._collection_title(layers),
            description=self._collection_description(layers),
            layers=layers,
            cfg=self._cfg,
            self_href=href_or_filename(self._cfg, category_slug, filename),
            pmtiles_url=self._artifact_url(pmtiles_path, category_slug),
            pmtiles_path=pmtiles_path,
            hdx_url=self._hdx_url(self._published_dataset_name(layers[0].category)),
            related=self._related_collections(layers[0].category),
            page_url=self._page_url(layers[0].category),
        )

    def _page_url(self, category: CategoryConfig) -> str | None:
        """Where the publisher puts the dataset's page, which only a run publishing to HDX builds."""
        cfg = self._cfg
        if not (cfg.hdx.push and cfg.output.report.enabled):
            return None
        name = self._published_dataset_name(category)
        if cfg.hdx.combine:
            return asset_href(cfg, "overview", f"{name}_overview.html")
        return asset_href(cfg, slugify(category.name), f"{name}_report.html")

    def _related_collections(self, category: CategoryConfig) -> list[tuple[str, str]]:
        """The area's other category Collections, which a run publishing to HDX uploads."""
        if not links_related_datasets(self._cfg):
            return []
        return [
            (
                category_label(other),
                href_or_filename(
                    self._cfg,
                    slugify(other.name),
                    collection_filename(layer_dataset_name(self._cfg, other), self._runner.name),
                ),
            )
            for other in self._cfg.categories
            if other.name != category.name
        ]

    def _collection_title(self, layers: list[LayerAssets]) -> str:
        """The same title the dataset carries on HDX, so the two do not diverge."""
        if not self._cfg.hdx.combine:
            return dataset_title(self._cfg, layers[0].category)
        place = country_name(self._cfg.iso3, dataset_name=self._cfg.dataset_name)
        return combined_title(
            self._cfg, place, HDX_SHORT_SOURCE.get(self._runner.name, self._runner.name)
        )

    def _collection_description(self, layers: list[LayerAssets]) -> str:
        if not self._cfg.hdx.combine:
            return layers[0].category.hdx.notes
        return self._cfg.hdx.combined.notes or self._collection_title(layers)

    def _stage_metadata(self, path: Path, category_slug: str) -> None:
        """Items are linked from the Collection rather than listed on HDX, so they go
        straight to the bucket the Collection points at."""
        s3cfg = self._cfg.output.s3
        if not s3cfg.enabled:
            return
        bucket, prefix, region, endpoint_url, acl = s3_resolve(s3cfg)
        if not bucket:
            raise ValueError(
                "output.s3.enabled is true but no bucket given via output.s3.bucket or OEX_S3_BUCKET"
            )
        key = artifact_key(
            prefix,
            self._cfg.iso3,
            category_slug,
            path.name,
            folder=s3cfg.folder,
            nest_by_category=s3cfg.nest_by_category,
        )
        s3_upload(path, bucket=bucket, key=key, region=region, endpoint_url=endpoint_url, acl=acl)

    def _build_combined_tiles(
        self, built_ok: list[BuiltCategory], out_root: Path, dt_name: str
    ) -> Path | None:
        """Merge this run's GeoParquets plus other sources' staged GeoParquets into one tileset."""
        if not self._cfg.output.pmtiles.enabled:
            return None
        order = {slugify(c.name): i for i, c in enumerate(self._cfg.categories)}
        by_slug = {slugify(c.name): c for c in self._cfg.categories}

        def tiles_on(slug: str, source: str) -> bool:
            block = getattr(by_slug.get(slug), source, None)
            return getattr(block, "tiles", True) if block is not None else True

        layers = [
            TileLayer(
                path=str(b.geoparquet_path),
                category=slugify(b.category.name),
                source=self._runner.name,
            )
            for b in built_ok
            if b.geoparquet_path is not None
            and tiles_on(slugify(b.category.name), self._runner.name)
        ]
        if self._cfg.output.s3.enabled:
            for source, slug, url in list_layer_urls(
                self._cfg.output.s3, self._cfg.iso3, exclude_source=self._runner.name
            ):
                # The staging area is never pruned, so it outlives categories this
                # config dropped. Merging them bloats the tileset with unlabelled data.
                if slug not in order:
                    logger.info(
                        "[%s/%s] combine: skipping staged layer %s/%s, not in this config",
                        self._cfg.iso3.upper(),
                        self._runner.name,
                        source,
                        slug,
                    )
                    continue
                if not tiles_on(slug, source):
                    logger.info(
                        "[%s/%s] combine: skipping %s/%s, tiles disabled for category",
                        self._cfg.iso3.upper(),
                        self._runner.name,
                        source,
                        slug,
                    )
                    continue
                layers.append(TileLayer(path=url, category=slug, source=source))
        if not layers:
            return None
        layers.sort(
            key=lambda ly: (SOURCE_RANK.get(ly.source, 99), order.get(ly.category, len(order)))
        )

        pm = self._cfg.output.pmtiles
        pmtiles_path = out_root / f"{dt_name}.pmtiles"
        d = self._cfg.duckdb
        tiles_db = Path(d.temp_dir) / f"{dt_name}_tiles_{int(time.time() * 1000)}.duckdb"
        conn = connect(
            path=tiles_db,
            threads=max(2, cpu_count()),
            memory_gb=self._cfg.parallel.memory_gb or self._adaptive_mem_gb,
            s3_region=getattr(self._cfg.source.get("overture"), "s3_region", "us-west-2"),
            temp_dir=d.temp_dir,
            http_retries=d.http_retries,
            http_retry_wait_ms=d.http_retry_wait_ms,
            http_retry_backoff=d.http_retry_backoff,
            http_timeout_s=d.http_timeout_s,
            anonymous_s3_bucket=getattr(
                self._cfg.source.get("overture"), "s3_bucket", "overturemaps-us-west-2"
            ),
        )
        try:
            build_combined_pmtiles(
                conn,
                layers,
                pmtiles_path,
                min_zoom=pm.min_zoom,
                max_zoom=pm.max_zoom,
                max_extra_zooms=pm.max_extra_zooms,
            )
            sources = sorted({ly.source for ly in layers})
            logger.info(
                "[%s/%s] combine: built tileset from %d layers across %s -> %s",
                self._cfg.iso3.upper(),
                self._runner.name,
                len(layers),
                ", ".join(sources),
                pmtiles_path,
            )
        finally:
            conn.close()
            tiles_db.unlink(missing_ok=True)
        return pmtiles_path

    def _resource_prefix(self) -> str:
        return resource_prefix(self._cfg)

    def _artifact_geometry(self, zip_path: Path) -> str:
        """The geometry label this artifact holds, read back off the name we wrote."""
        if not self._cfg.output.split_by_geometry:
            raise ValueError(
                "output.s3.nest_by_geometry needs output.split_by_geometry; without it "
                "one artifact holds every geometry type and cannot sit under one segment"
            )
        tokens = zip_path.stem.split("_")
        for label in GEOMETRY_LABELS:
            if label in tokens:
                return label
        raise ValueError(f"no geometry label in artifact name {zip_path.name}")

    def _upload_category_to_s3(self, zip_paths: list[Path], slug: str, cat_tag: str) -> None:
        """Upload a category's zips straight to S3 when s3 is enabled and HDX is off."""
        s3cfg = self._cfg.output.s3
        bucket, prefix, region, endpoint_url, acl = s3_resolve(s3cfg)
        logger.info("%s uploading %d artifact(s) to S3...", cat_tag, len(zip_paths))
        for zp in zip_paths:
            key = artifact_key(
                prefix,
                self._cfg.iso3,
                slug,
                zp.name,
                folder=s3cfg.folder,
                nest_by_category=s3cfg.nest_by_category,
                geometry=self._artifact_geometry(zp) if s3cfg.nest_by_geometry else "",
            )
            s3_upload(zp, bucket=bucket, key=key, region=region, endpoint_url=endpoint_url, acl=acl)
        if self._state is not None:
            self._state.mark_uploaded(slug, hdx_dataset=None)
        if self._cfg.output.remove_after_upload:
            _remove_uploaded_outputs(zip_paths, None)
            logger.info("%s removed %d local output(s) after upload", cat_tag, len(zip_paths))

    def _stage_layer_parquet(self, path: Path, slug: str, cat_tag: str) -> str | None:
        """Upload a layer's GeoParquet to its stable key, which every source's runs share and
        the HDX GeoParquet resource links to rather than copying."""
        s3cfg = self._cfg.output.s3
        if not s3cfg.enabled:
            return None
        bucket, prefix, region, endpoint_url, acl = s3_resolve(s3cfg)
        if not bucket:
            raise ValueError(
                "output.s3.enabled is true but no bucket given via output.s3.bucket or OEX_S3_BUCKET"
            )
        key = build_layer_key(
            prefix, self._cfg.iso3, self._runner.name, slug, self._cfg.output.s3.folder
        )
        url = s3_upload(
            path, bucket=bucket, key=key, region=region, endpoint_url=endpoint_url, acl=acl
        )
        logger.info("%s staged layer geoparquet -> %s", cat_tag, url)
        return url

    def _refresh_empty_category(
        self,
        category: CategoryConfig,
        query: SourceQuery,
        publisher: HdxPublisher,
        cat_tag: str,
    ) -> None:
        """Restate the snapshot date on an empty category's dataset. An HDXError only leaves the
        older date in place, so it is logged rather than failing the export."""
        try:
            publisher.refresh_metadata(
                self._cfg,
                category,
                PublishContext(
                    dataset_source=query.dataset_source,
                    snapshot_date=query.snapshot_date,
                    source_name=self._runner.name,
                    s3=self._cfg.output.s3,
                ),
            )
        except HDXError:
            logger.exception("%s metadata refresh failed", cat_tag)

    def _materialise_outputs(
        self,
        *,
        conn,
        table: str,
        slug: str,
        category: CategoryConfig,
        query: SourceQuery,
        formats: list[str],
        out_root: Path,
        metadata_report: dict | None,
        boundary,
        feature_count: int,
    ) -> list[Path]:
        zip_paths: list[Path] = []
        # An empty label means one artifact per format, covering every geometry.
        if self._cfg.output.split_by_geometry:
            partitions = [
                (label, self._geometry_view(conn, table, label, types))
                for label, types in sorted(geometry_labels(conn, table).items())
            ]
        else:
            partitions = [("", table)]

        for fmt, (geometry, source_table) in itertools.product(formats, partitions):
            geom_seg = f"_{geometry}" if geometry else ""
            stage_dir = out_root / f"_stage_{slug}{geom_seg}_{fmt}"
            if stage_dir.exists():
                shutil.rmtree(stage_dir)
            stage_dir.mkdir(parents=True)
            try:
                try:
                    files = write_format(conn, source_table, slug, fmt, stage_dir)
                except Exception as exc:  # noqa: BLE001 - per-format boundary
                    # GDAL can fail mid-write on corrupt OSM coordinates or run out of memory,
                    # and the category's other formats may still succeed.
                    logger.warning(
                        "%s [%s] format write failed (%s); skipping this format",
                        f"[{category.name}/{self._runner.name}]",
                        fmt,
                        exc,
                    )
                    continue
                if not files:
                    continue
                # HDX's filestore does not recognise FlatGeobuf, so it is neither zipped nor
                # published.
                if fmt == "fgb":
                    continue
                s3cfg = self._cfg.output.s3
                source_seg = f"_{self._runner.name}" if s3cfg.name_include_source else ""
                stem = f"{self._resource_prefix()}_{slug}{geom_seg}{source_seg}"
                if not self._should_zip(category, fmt):
                    zip_paths.append(
                        _publish_unzipped(
                            files[0],
                            out_root / f"{stem}.{fmt}",
                            f"[{category.name}/{self._runner.name}]",
                        )
                    )
                    continue
                zip_path = out_root / f"{stem}_{fmt}.zip"
                readme_lines = self._build_readme(
                    fmt=fmt,
                    category=category,
                    query=query,
                    boundary=boundary,
                    feature_count=feature_count,
                )
                make_zip(
                    stage_dir,
                    zip_path,
                    readme_lines=readme_lines,
                    config_snapshot={
                        "iso3": self._cfg.iso3,
                        "category": asdict(category),
                        "source": self._runner.name,
                    },
                    metadata_report=metadata_report,
                )
                zip_paths.append(zip_path)
            finally:
                if stage_dir.exists():
                    shutil.rmtree(stage_dir, ignore_errors=True)
        return zip_paths

    def _should_zip(self, category: CategoryConfig, fmt: str) -> bool:
        """A shapefile is a set of sidecar files, so it is zipped whatever the config says."""
        if fmt == "shp":
            return True
        chosen = category.zip_formats
        if chosen is None:
            chosen = self._cfg.output.zip_formats
        return chosen is None or fmt in chosen

    def _geometry_view(self, conn, table: str, label: str, types: list[str]) -> str:
        """A view over one geometry label, so each artifact holds a single type."""
        view = f"{table}_{label}"
        in_list = ", ".join(f"'{t}'" for t in types)
        conn.execute(
            f"CREATE OR REPLACE TEMP VIEW {view} AS "
            f"SELECT * FROM {table} WHERE ST_GeometryType(geom) IN ({in_list})"
        )
        return view

    def _build_readme(
        self,
        *,
        fmt: str,
        category: CategoryConfig,
        query: SourceQuery,
        boundary,
        feature_count: int,
    ) -> list[str]:
        bbox = ", ".join(f"{x:.4f}" for x in boundary.bbox)
        license_url = category.hdx.license_url or "(not specified)"
        wrapped_desc = _wrap_paragraph(query.source_description, indent="  ", width=78)
        format_notes = _format_notes(fmt)
        return [
            "oex export",
            "==========",
            "",
            f"Generated:        {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"oex version:      {_oex_version()}",
            f"Project:          {_PROJECT_URL}",
            "",
            f"Country (ISO3):   {self._cfg.iso3.upper()}",
            f"Boundary:         {boundary.source}",
            f"Bounding box:     ({bbox})",
            "",
            f"Dataset:          {category.name}",
            f"Format:           {_FORMAT_LABELS.get(fmt, fmt)}",
            f"Features:         {feature_count:,}",
            "",
            f"Source:           {category.hdx.dataset_source or query.dataset_source}",
            f"Source URL:       {query.source_url}",
            f"Snapshot:         {query.snapshot_label}",
            f"License:          {license_label(category.hdx.license)}",
            f"License URL:      {license_url}",
            "",
            "About the source",
            *wrapped_desc,
            "",
            *(["Notes", *format_notes, ""] if format_notes else []),
            f"Feedback:         {_PROJECT_URL}/issues",
        ] + ([line for line in query.extra_readme_lines] if query.extra_readme_lines else [])


def _publish_unzipped(staged: Path, target: Path, cat_tag: str) -> Path:
    """Move a single-file format out of the stage dir, which is deleted straight after."""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staged), target)
    logger.info(
        "%s published %s unzipped (%.2f MB)",
        cat_tag,
        target.name,
        target.stat().st_size / (1024**2),
    )
    return target


def _remove_uploaded_outputs(
    zip_paths: list[Path],
    metadata_json_path: Path | None,
    extra_paths: list[Path] | None = None,
) -> None:
    """Delete local artifacts once HDX confirms their upload."""
    for path in zip_paths:
        path.unlink(missing_ok=True)
    if metadata_json_path is not None:
        metadata_json_path.unlink(missing_ok=True)
    for path in extra_paths or []:
        path.unlink(missing_ok=True)


def _wrap_paragraph(text: str, *, indent: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width=width, initial_indent=indent, subsequent_indent=indent)


def _parse_temporal(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        logger.warning(
            "temporal bound %r is not ISO-8601; HDX period falls back to snapshot", value
        )
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _temporal_bounds_for_hdx(metadata_obj) -> tuple[datetime | None, datetime | None]:
    if metadata_obj is None or metadata_obj.temporal is None:
        return (None, None)
    t = metadata_obj.temporal
    return (_parse_temporal(t.min), _parse_temporal(t.max))


def _combined_temporal_bounds(
    metadata_objs: list[object],
) -> tuple[datetime | None, datetime | None]:
    mins: list[datetime] = []
    maxs: list[datetime] = []
    for obj in metadata_objs:
        lo, hi = _temporal_bounds_for_hdx(obj)
        if lo is not None:
            mins.append(lo)
        if hi is not None:
            maxs.append(hi)
    return (min(mins) if mins else None, max(maxs) if maxs else None)


def _format_notes(fmt: str) -> list[str]:
    if fmt == "shp":
        return [
            "  - Shapefile output is split by geometry type:",
            "    <category>_polygons.shp, <category>_lines.shp, <category>_points.shp.",
            "    This is a shapefile-format limitation, not a data limitation.",
            "  - Field names are truncated to 10 characters in shp; gpkg keeps them full.",
        ]
    if fmt == "gpkg":
        return [
            "  - GeoPackage holds all geometry types in a single .gpkg file.",
            "  - Recommended for QGIS, ArcGIS, GDAL/OGR.",
        ]
    if fmt == "geojson":
        return [
            "  - GeoJSON is a single-file text format. Consider gpkg for very large layers.",
        ]
    if fmt == "kml":
        return [
            "  - KML opens directly in Google Earth and most desktop GIS.",
            "  - Single XML file; can grow large for big layers. Prefer gpkg above ~1M features.",
        ]
    return []
