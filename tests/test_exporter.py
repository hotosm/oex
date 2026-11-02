"""Exporter helpers."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from oex.config.schema import RootConfig
from oex.exporter import _remove_uploaded_outputs


def test_remove_uploaded_outputs_deletes_and_tolerates_missing(tmp_path: Path) -> None:
    zip1 = tmp_path / "buildings_osm_gpkg.zip"
    zip1.write_bytes(b"x")
    meta = tmp_path / "buildings_osm_metadata.json"
    meta.write_text("{}", encoding="utf-8")
    already_gone = tmp_path / "buildings_osm_shp.zip"

    _remove_uploaded_outputs([zip1, already_gone], meta)

    assert not zip1.exists()
    assert not meta.exists()


def test_remove_uploaded_outputs_without_metadata(tmp_path: Path) -> None:
    zip1 = tmp_path / "roads_osm_gpkg.zip"
    zip1.write_bytes(b"x")

    _remove_uploaded_outputs([zip1], None)

    assert not zip1.exists()


def test_publish_combined_builds_tileset_orders_and_cleans_up(tmp_path: Path) -> None:
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    import duckdb

    from oex.config.schema import (
        CategoryConfig,
        DuckdbConfig,
        HdxConfig,
        OutputConfig,
        PmtilesConfig,
        RootConfig,
    )
    from oex.exporter import _COMBINED_SLUG, BuiltCategory, CategoryResult, Exporter
    from oex.sources.base import SourceQuery
    from oex.state import StateStore
    from oex.writers import write_geoparquet

    cfg = RootConfig(
        iso3="NPL",
        key="hotosm",
        hdx=HdxConfig(push=True, combine=True),
        output=OutputConfig(
            dir=str(tmp_path),
            pmtiles=PmtilesConfig(enabled=True, min_zoom=0, max_zoom=8),
            remove_after_upload=True,
        ),
        duckdb=DuckdbConfig(temp_dir=str(tmp_path / "ddb")),
        categories=[CategoryConfig(name="buildings"), CategoryConfig(name="roads")],
    )

    runner = MagicMock()
    runner.name = "osm"
    exporter = Exporter(cfg, runner)
    out_root = tmp_path / "npl" / "osm"
    (out_root / "_layers").mkdir(parents=True)
    exporter._state = StateStore(path=out_root / ".state.json", iso3="NPL", source="osm")

    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial")
    conn.execute("LOAD spatial")
    conn.execute("CREATE TABLE t AS SELECT 'x' AS name, ST_Point(85.3, 27.7) AS geom")

    query = SourceQuery(
        source_expr="t",
        select_fields=[],
        where_conditions=[],
        bbox_cols="geom",
        dataset_source="OpenStreetMap",
        source_url="",
        source_description="",
        snapshot_date=datetime.now(UTC),
        snapshot_label="2026-04-15.0",
        extra_readme_lines=[],
    )

    def _built(name: str) -> BuiltCategory:
        cat = next(c for c in cfg.categories if c.name == name)
        zip_path = out_root / f"hotosm_npl_{name}_osm_gpkg.zip"
        zip_path.write_bytes(b"z" * 128)
        parquet = write_geoparquet(conn, "t", out_root / "_layers" / f"{name}.parquet")
        return BuiltCategory(
            result=CategoryResult(name=name, status="ok", feature_count=1, zip_paths=[zip_path]),
            category=cat,
            zip_paths=[zip_path],
            metadata_json_path=None,
            metadata_obj=None,
            geoparquet_path=parquet,
            query=query,
        )

    # Feed reversed to prove the combine phase re-sorts to config order.
    built_list = [_built("roads"), _built("buildings")]

    publisher = MagicMock()
    publisher.publish_combined.return_value = "hotosm_npl"

    exporter._finish_combined(
        built_list,
        out_root,
        publisher,
        peeked_label="2026-04-15.0",
        boundary_bbox=(85.30, 27.68, 85.36, 27.73),
    )

    publisher.publish_combined.assert_called_once()
    call = publisher.publish_combined.call_args
    entries = call.args[1]
    assert [e.category.name for e in entries] == ["buildings", "roads"]
    pmtiles_path = call.kwargs["pmtiles_path"]
    # Tileset built, then removed after upload; zips removed; GeoParquets kept as deliverables.
    assert pmtiles_path.name == "hotosm_npl.pmtiles"
    assert not pmtiles_path.exists()
    assert all(not b.zip_paths[0].exists() for b in built_list)
    assert all(b.geoparquet_path is not None and b.geoparquet_path.exists() for b in built_list)
    assert exporter._state.is_uploaded(_COMBINED_SLUG, snapshot_label="2026-04-15.0")
    assert all(b.result.hdx_dataset == "hotosm_npl" for b in built_list)


def test_combined_tileset_skips_staged_layers_not_in_this_config(tmp_path: Path) -> None:
    """Staging is never pruned, so it outlives dropped categories; merging them would bloat the
    tileset with features no legend entry draws."""
    from unittest.mock import MagicMock, patch

    import duckdb

    from oex.config.schema import (
        CategoryConfig,
        DuckdbConfig,
        HdxConfig,
        OutputConfig,
        PmtilesConfig,
        RootConfig,
        S3Config,
    )
    from oex.exporter import BuiltCategory, CategoryResult, Exporter
    from oex.writers import write_geoparquet

    cfg = RootConfig(
        iso3="NPL",
        key="hotosm",
        hdx=HdxConfig(push=True, combine=True),
        output=OutputConfig(
            dir=str(tmp_path),
            pmtiles=PmtilesConfig(enabled=True, min_zoom=0, max_zoom=8),
            s3=S3Config(enabled=True, bucket="b", prefix="p"),
        ),
        duckdb=DuckdbConfig(temp_dir=str(tmp_path / "ddb")),
        categories=[CategoryConfig(name="buildings")],
    )

    runner = MagicMock()
    runner.name = "osm"
    exporter = Exporter(cfg, runner)
    out_root = tmp_path / "npl" / "osm"
    (out_root / "_layers").mkdir(parents=True)

    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial")
    conn.execute("LOAD spatial")
    conn.execute("CREATE TABLE t AS SELECT 'x' AS name, ST_Point(85.3, 27.7) AS geom")
    parquet = write_geoparquet(conn, "t", out_root / "_layers" / "buildings.parquet")

    built_ok = [
        BuiltCategory(
            result=CategoryResult(name="buildings", status="ok"),
            category=cfg.categories[0],
            geoparquet_path=parquet,
        )
    ]

    staged = [
        ("overture", "buildings", "https://s3/p/NPL/_layers/overture/buildings.parquet"),
        # Left behind by an earlier config that still had a Rivers category.
        ("overture", "rivers", "https://s3/p/NPL/_layers/overture/rivers.parquet"),
    ]
    captured: dict = {}

    def fake_build(conn, layers, out_path, **kwargs):  # noqa: ANN001, ANN202
        captured["layers"] = layers
        out_path.write_bytes(b"PMTiles")
        return out_path

    with (
        patch("oex.exporter.list_layer_urls", return_value=staged),
        patch("oex.exporter.build_combined_pmtiles", side_effect=fake_build),
        patch("oex.exporter.connect", return_value=conn),
    ):
        exporter._build_combined_tiles(built_ok, out_root, "hotosm_npl")

    merged = {(layer.source, layer.category) for layer in captured["layers"]}
    assert ("overture", "buildings") in merged
    assert ("osm", "buildings") in merged
    assert ("overture", "rivers") not in merged, "stale staged layer must not be merged"


def test_resource_prefix_prefers_dataset_name_in_combine_mode() -> None:
    from unittest.mock import MagicMock

    from oex.config.schema import CombinedHdx, HdxConfig, OutputConfig, RootConfig, S3Config
    from oex.exporter import Exporter

    def prefix(cfg: RootConfig) -> str:
        return Exporter(cfg, MagicMock())._resource_prefix()

    # Combine + a dataset name -> the dataset name.
    assert (
        prefix(
            RootConfig(
                iso3="COD",
                key="hot_cod",
                hdx=HdxConfig(combine=True, combined=CombinedHdx(name="hot_cod_humanitarian_data")),
            )
        )
        == "hot_cod_humanitarian_data"
    )
    # Combine but no dataset name -> falls back to {key}_{iso3}.
    assert (
        prefix(RootConfig(iso3="COD", key="hot_cod", hdx=HdxConfig(combine=True))) == "hot_cod_cod"
    )
    # Per-category (no combine) -> {key}_{iso3}.
    assert prefix(RootConfig(iso3="COD", key="hot_cod")) == "hot_cod_cod"
    # Per-category with an S3 folder id -> {key}_{folder}.
    assert (
        prefix(
            RootConfig(
                iso3="MCO", key="hotosm_project", output=OutputConfig(s3=S3Config(folder="11731"))
            )
        )
        == "hotosm_project_11731"
    )


def test_build_combined_tiles_excludes_a_source_with_tiles_false(tmp_path: Path) -> None:
    """A category's source can opt out of the map while staying a download."""
    from unittest.mock import MagicMock, patch

    import duckdb

    from oex.config.schema import (
        CategoryConfig,
        CategoryOverture,
        DuckdbConfig,
        HdxConfig,
        OutputConfig,
        PmtilesConfig,
        RootConfig,
        S3Config,
    )
    from oex.exporter import BuiltCategory, CategoryResult, Exporter
    from oex.writers import write_geoparquet

    cfg = RootConfig(
        iso3="NPL",
        key="hotosm",
        hdx=HdxConfig(push=True, combine=True),
        output=OutputConfig(
            dir=str(tmp_path),
            pmtiles=PmtilesConfig(enabled=True, min_zoom=0, max_zoom=8),
            s3=S3Config(enabled=True, bucket="b", prefix="p"),
        ),
        duckdb=DuckdbConfig(temp_dir=str(tmp_path / "ddb")),
        categories=[CategoryConfig(name="buildings", overture=CategoryOverture(tiles=False))],
    )
    runner = MagicMock()
    runner.name = "osm"
    exporter = Exporter(cfg, runner)
    out_root = tmp_path / "npl" / "osm"
    (out_root / "_layers").mkdir(parents=True)

    conn = duckdb.connect(":memory:")
    conn.execute("INSTALL spatial")
    conn.execute("LOAD spatial")
    conn.execute("CREATE TABLE t AS SELECT 'x' AS name, ST_Point(85.3, 27.7) AS geom")
    parquet = write_geoparquet(conn, "t", out_root / "_layers" / "buildings.parquet")

    built_ok = [
        BuiltCategory(
            result=CategoryResult(name="buildings", status="ok"),
            category=cfg.categories[0],
            geoparquet_path=parquet,
        )
    ]
    staged = [("overture", "buildings", "https://s3/p/NPL/_layers/overture/buildings.parquet")]
    captured: dict = {}

    def fake_build(conn, layers, out_path, **kwargs):  # noqa: ANN001, ANN202
        captured["layers"] = layers
        out_path.write_bytes(b"PMTiles")
        return out_path

    with (
        patch("oex.exporter.list_layer_urls", return_value=staged),
        patch("oex.exporter.build_combined_pmtiles", side_effect=fake_build),
        patch("oex.exporter.connect", return_value=conn),
    ):
        exporter._build_combined_tiles(built_ok, out_root, "hotosm_npl")

    merged = {(layer.source, layer.category) for layer in captured["layers"]}
    assert ("osm", "buildings") in merged, "osm buildings still tiled"
    assert ("overture", "buildings") not in merged, (
        "tiles: false keeps overture buildings off the map"
    )


def test_nest_by_geometry_without_the_split_is_rejected() -> None:
    """One artifact holding every geometry type cannot sit under a single segment."""
    from oex.exporter import Exporter

    cfg = RootConfig(iso3="NPL")
    cfg.output.split_by_geometry = False
    cfg.output.s3.nest_by_geometry = True
    exporter = Exporter(cfg, Mock(name="osm"))
    with pytest.raises(ValueError, match="split_by_geometry"):
        exporter._artifact_geometry(Path("x_buildings_polygons_shp.zip"))


def test_the_geometry_label_is_read_back_off_the_artifact_name() -> None:
    from oex.exporter import Exporter

    cfg = RootConfig(iso3="NPL")
    cfg.output.split_by_geometry = True
    exporter = Exporter(cfg, Mock(name="osm"))
    name = Path("hotosm_project_1_buildings_polygons_shp.zip")
    assert exporter._artifact_geometry(name) == "polygons"
    with pytest.raises(ValueError, match="no geometry label"):
        exporter._artifact_geometry(Path("hotosm_project_1_buildings_shp.zip"))


@pytest.mark.parametrize(("simplify_meters", "embedded"), [(0.0, False), (500.0, True)])
def test_only_a_simplified_outline_becomes_the_item_footprint(
    simplify_meters: float, embedded: bool
) -> None:
    """A raw national outline can be over 100 MB, and every Item would carry a copy."""
    from oex.config.schema import BoundaryConfig
    from oex.exporter import Exporter

    cfg = RootConfig(
        iso3="NPL",
        boundary=BoundaryConfig(buffer_meters=1000.0, simplify_meters=simplify_meters),
    )
    outline = '{"type":"Polygon","coordinates":[[[85,27],[86,27],[86,28],[85,27]]]}'
    footprint = Exporter(cfg, Mock(name="osm"))._item_footprint(outline)
    assert (footprint == outline) is embedded
