"""The Collection is what a catalog serves, so it has to satisfy the spec, not resemble it."""

import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pystac
import pystac.validation
import pytest

from oex.config.schema import HDX_LICENSE_LABELS, BoundaryConfig, CategoryConfig, RootConfig
from oex.metadata import ColumnReport, MetadataReport, TemporalReport
from oex.naming import slugify
from oex.report import SourceMetadata
from oex.stac import (
    LayerAssets,
    asset_key,
    build_collection,
    build_item,
    item_id,
    spdx_license,
    to_payload,
)

NPL_BBOX = (84.5549134, 27.7933974, 85.3805573, 28.2818703)
ODBL_URL = "https://opendatacommons.org/licenses/odbl/1-0/"


def _cfg(*, s3: bool = True, nest: bool = True) -> RootConfig:
    cfg = RootConfig(iso3="NPL", key="hot_flood", boundary=BoundaryConfig())
    cfg.output.s3.enabled = s3
    cfg.output.s3.bucket = "example-bucket"
    cfg.output.s3.prefix = "ISO3"
    cfg.output.s3.region = "us-east-1"
    cfg.output.s3.nest_by_category = nest
    return cfg


def _layer(
    name: str = "buildings",
    *,
    source: str = "osm",
    dataset_name: str | None = None,
    snapshot_label: str = "2026-09-18T03:55:55Z",
    license_url: str | None = ODBL_URL,
    pmtiles_url: str | None = None,
    feature_count: int = 20403,
    bbox: tuple[float, float, float, float] | None = NPL_BBOX,
    artifacts: list[str] | None = None,
    geoparquet_url: str | None = None,
    export_seconds: float | None = None,
    footprint: str | None = None,
) -> LayerAssets:
    slug = slugify(name)
    report = MetadataReport(
        feature_count=feature_count,
        geometry_types={"POLYGON": feature_count},
        bbox=bbox,
        columns=[
            ColumnReport(
                name="id",
                type="VARCHAR",
                null_count=0,
                null_percent=0.0,
                distinct_count=feature_count,
            ),
            ColumnReport(
                name="geom",
                type="GEOMETRY",
                null_count=0,
                null_percent=0.0,
                distinct_count=feature_count,
            ),
        ],
        summary=f"{feature_count} features.",
    )
    meta = SourceMetadata(
        source_name=source,
        snapshot_label=snapshot_label,
        dataset_source="OpenStreetMap (Postpass 2026-09-18T03:55:55Z)",
        generated_utc="2026-09-18T04:00:34Z",
        oex_version="0.4.16",
        license_label="Open Database License (ODC-ODbL)",
        license_url=license_url,
        pcode_source_date="2025-07-29",
        boundary="user-provided (buffered +200m)",
        metadata=report,
    )
    category = CategoryConfig(name=name)
    category.hdx.license_url = license_url
    names = (
        artifacts
        if artifacts is not None
        else [f"hot_flood_npl_{slug}_{source}_gpkg.zip", f"hot_flood_npl_{slug}_{source}.geojson"]
    )
    return LayerAssets(
        category=category,
        source_metadata=meta,
        dataset_name=dataset_name or f"hot_flood_npl_{slug}",
        artifacts=[Path(n) for n in names],
        geoparquet_url=geoparquet_url,
        export_seconds=export_seconds,
        pmtiles_url=pmtiles_url,
        footprint=footprint,
    )


def _collection(*layers: LayerAssets, cfg: RootConfig | None = None, **kwargs) -> pystac.Collection:
    return build_collection(
        dataset_name=kwargs.pop("dataset_name", "hot_flood_npl"),
        title=kwargs.pop("title", "Nepal flood affected area data"),
        description=kwargs.pop("description", "Vector data for the flood affected area."),
        layers=list(layers) or [_layer()],
        cfg=cfg or _cfg(),
        **kwargs,
    )


@pytest.mark.network
def test_a_built_item_satisfies_the_stac_spec():
    build_item(_layer(), _cfg()).validate()


@pytest.mark.network
def test_a_built_collection_satisfies_the_stac_spec():
    collection = _collection(
        _layer(),
        _layer("roads", feature_count=812),
        pmtiles_url="https://example-bucket.s3.amazonaws.com/ISO3/NPL/combined/hot_flood_npl.pmtiles",
        self_href="https://example-bucket.s3.amazonaws.com/ISO3/NPL/combined/c.json",
    )
    pystac.validation.validate_dict(to_payload(collection))


def test_the_item_id_is_derived_not_incidental():
    """Two datasets can only be compared layer by layer if the ids line up, so the id
    is the layer's own dataset name and its source and nothing else."""
    assert item_id("hotosm_npl_points_of_interest", "osm") == "hotosm_npl_points_of_interest-osm"
    built = build_item(_layer("Points of Interest"), _cfg())
    assert built.id == "hot_flood_npl_points_of_interest-osm"


def test_every_artifact_becomes_an_addressable_asset():
    item = build_item(_layer(geoparquet_url="https://x/buildings.parquet"), _cfg())
    assert set(item.assets) == {"gpkg", "geojson", "geoparquet"}
    assert item.assets["gpkg"].href == (
        "https://example-bucket.s3.amazonaws.com/ISO3/NPL/buildings/hot_flood_npl_buildings_osm_gpkg.zip"
    )
    assert item.assets["geoparquet"].media_type == "application/vnd.apache.parquet"


def test_a_flat_layout_moves_every_asset_href():
    """nest_by_category drops the category segment, which the hrefs must follow."""
    item = build_item(_layer(), _cfg(nest=False))
    assert "/NPL/hot_flood_npl_buildings_osm_gpkg.zip" in item.assets["gpkg"].href


def test_nest_by_geometry_moves_the_asset_href_with_the_upload():
    """The uploader adds a geometry segment; an href that skips it is a 404."""
    cfg = _cfg()
    cfg.output.s3.nest_by_geometry = True
    item = build_item(_layer(artifacts=["d_npl_buildings_polygons_osm_shp.zip"]), cfg)
    assert "/buildings/polygons/" in item.assets["polygons_shp"].href


def test_without_s3_every_href_is_relative_so_the_output_directory_is_a_catalog():
    """A local run writes the Collection and its Items side by side, and an absolute
    href would point into a bucket that was never written."""
    item = build_item(_layer(), _cfg(s3=False))
    assert item.assets["gpkg"].href == "hot_flood_npl_buildings_osm_gpkg.zip"
    collection = _collection(_layer(), cfg=_cfg(s3=False))
    assert [link.href for link in collection.get_links("item")] == [
        "hot_flood_npl_buildings_osm_item.json"
    ]


def test_the_row_count_and_schema_travel_with_the_item():
    item = build_item(_layer(), _cfg())
    assert item.properties["table:row_count"] == 20403
    assert [c["name"] for c in item.properties["table:columns"]] == ["id", "geom"]
    # The extension wants the native type, which is what the parquet assets carry.
    assert [c["type"] for c in item.properties["table:columns"]] == ["VARCHAR", "GEOMETRY"]


def test_the_item_names_the_geometry_column_of_its_geoparquet():
    """Readers use primary_geometry to find the geometry without guessing a name."""
    from oex.metadata import GEOMETRY_COL

    assert build_item(_layer(), _cfg()).properties["table:primary_geometry"] == GEOMETRY_COL


@pytest.mark.network
def test_an_empty_layer_still_describes_itself():
    """A layer with no features has no bbox; the Item must stay valid without one."""
    item = build_item(_layer(feature_count=0, bbox=None), _cfg())
    item.validate()
    assert item.bbox is None
    assert item.properties["table:row_count"] == 0


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("d_npl_buildings_osm_gpkg.zip", "gpkg"),
        ("d_npl_buildings_osm_shp.zip", "shp"),
        ("d_npl_buildings_osm.geojson", "geojson"),
        ("buildings.parquet", "geoparquet"),
        ("d_npl_buildings_polygons_osm_shp.zip", "polygons_shp"),
        ("d_npl_buildings_lines_osm_shp.zip", "lines_shp"),
    ],
)
def test_the_asset_key_separates_formats_and_geometry_partitions(filename, expected):
    """These are exporter-shaped names: the dataset prefix is still on the front."""
    assert asset_key(filename, "buildings") == expected


def test_a_category_named_after_a_geometry_label_is_not_a_partition():
    """`points_of_interest` starts with a geometry label and must not be split on it."""
    assert asset_key("d_npl_points_of_interest_osm_shp.zip", "points_of_interest") == "shp"


def test_split_by_geometry_keeps_every_artifact_addressable():
    """One key per format would let the partitions overwrite each other."""
    names: list[str] = [
        f"d_npl_buildings_{geometry}_osm_{fmt}.zip"
        for geometry in ("points", "lines", "polygons")
        for fmt in ("gpkg", "shp")
    ]
    item = build_item(_layer(artifacts=names), _cfg())
    assert len(item.assets) == len(names)
    assert "polygons_shp" in item.assets and "lines_gpkg" in item.assets


@pytest.mark.network
def test_an_asset_carries_the_size_of_the_file_it_points_at(tmp_path):
    """A download list that cannot state a size makes the reader fetch to find out."""
    artifact = tmp_path / "d_npl_buildings_osm_gpkg.zip"
    artifact.write_bytes(b"x" * 4096)
    layer = _layer()
    layer.artifacts = [artifact]
    item = build_item(layer, _cfg())
    item.validate()
    assert item.assets["gpkg"].extra_fields["file:size"] == 4096


def test_the_item_states_the_crs_of_its_assets():
    """Every writer is given EPSG:4326; saying so saves opening a file to find out."""
    assert build_item(_layer(), _cfg()).properties["proj:code"] == "EPSG:4326"


def test_the_layer_title_is_the_label_people_read_not_a_blank():
    assert build_item(_layer("points of interest"), _cfg()).properties["title"] == (
        "Points of Interest"
    )


def _rich_layer() -> LayerAssets:
    layer = _layer()
    layer.source_metadata.metadata.columns = [
        ColumnReport(
            name="building",
            type="VARCHAR",
            null_count=47,
            null_percent=0.23,
            distinct_count=12,
            top_values=[{"value": "yes", "count": 18_000}],
        )
    ]
    layer.source_metadata.metadata.temporal = TemporalReport(
        column="update_time", min="2026-08-01", max="2026-09-18", non_null_count=20403
    )
    return layer


def test_an_item_round_trips_back_into_the_report_without_loss():
    """The page reads these back, so a lossy Item degrades it. Built with a published Item's
    links, so the licence lookup has to pick the right one rather than the first."""
    layer = _rich_layer()
    item = build_item(
        layer,
        _cfg(),
        self_href="https://example-bucket.s3.amazonaws.com/ISO3/NPL/buildings/i.json",
        collection_id="hot_flood_npl",
        collection_href="https://example-bucket.s3.amazonaws.com/ISO3/NPL/combined/c.json",
    )
    assert len(to_payload(item)["links"]) == 5
    restored = SourceMetadata.from_payload(to_payload(item))
    assert restored == layer.source_metadata


def test_the_data_time_range_is_a_stac_range_not_only_an_oex_field():
    """A client filtering by time reads start_datetime; oex:temporal names the column."""
    item = build_item(_rich_layer(), _cfg())
    assert item.properties["start_datetime"] == "2026-08-01T00:00:00Z"
    assert item.properties["end_datetime"] == "2026-09-18T00:00:00Z"
    assert item.properties["oex:temporal"]["column"] == "update_time"


@pytest.mark.network
def test_a_time_column_that_holds_no_timestamps_leaves_the_range_off():
    """Free text in the configured column must not invent a range or fail the export."""
    layer = _rich_layer()
    layer.source_metadata.metadata.temporal = TemporalReport(
        column="update_time", min="last tuesday", max="recently", non_null_count=3
    )
    item = build_item(layer, _cfg())
    item.validate()
    assert "start_datetime" not in item.properties
    assert item.properties["oex:temporal"]["min"] == "last tuesday"


@pytest.mark.parametrize("zone", ["UTC", "Asia/Kathmandu", "America/Los_Angeles"])
@pytest.mark.network
def test_a_snapshot_label_that_is_not_a_time_still_dates_the_item(zone, monkeypatch):
    """Overture labels like `2026-06-17.0` are releases, not timestamps. Run in three zones,
    since a bare date read as local time shifts the Item in a way UTC alone never shows."""
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    item = build_item(_layer(snapshot_label="2026-06-17.0"), _cfg())
    item.validate()
    assert item.datetime == datetime(2026, 6, 17, tzinfo=UTC)
    assert item.properties["oex:snapshot"] == "2026-06-17.0"


def test_the_collection_points_at_itself_and_back_at_hdx():
    """A catalog crawling S3 needs the canonical address; a human needs the dataset page."""
    collection = _collection(
        _layer(),
        self_href="https://example-bucket.s3.amazonaws.com/ISO3/NPL/combined/x_metadata.json",
        hdx_url="https://data.humdata.org/dataset/hot_flood_npl",
    )
    rels = {link["rel"]: link["href"] for link in to_payload(collection)["links"]}
    assert rels["self"].endswith("/ISO3/NPL/combined/x_metadata.json")
    assert rels["via"] == "https://data.humdata.org/dataset/hot_flood_npl"


def test_a_stac_client_reads_every_layer_out_of_the_collection():
    """The whole point of the Collection: a generic client must find the Items."""
    payload = to_payload(_collection(_layer(), _layer("roads")))
    seen = pystac.Collection.from_dict(payload).get_links("item")
    assert [link.href for link in seen] == [
        "https://example-bucket.s3.amazonaws.com/ISO3/NPL/buildings/hot_flood_npl_buildings_osm_item.json",
        "https://example-bucket.s3.amazonaws.com/ISO3/NPL/roads/hot_flood_npl_roads_osm_item.json",
    ]


@pytest.mark.network
def test_an_item_names_the_collection_it_belongs_to_and_links_back_to_it():
    """A crawler that reaches an Item first has to be able to walk up to the dataset."""
    href = "https://example-bucket.s3.amazonaws.com/ISO3/NPL/combined/c.json"
    item = build_item(_layer(), _cfg(), collection_id="hot_flood_npl", collection_href=href)
    payload = to_payload(item)
    # validate_dict rather than Item.validate: the latter fetches the root link it finds.
    pystac.validation.validate_dict(payload, pystac.STACObjectType.ITEM)
    assert payload["collection"] == "hot_flood_npl"
    assert {link["rel"] for link in payload["links"]} >= {"collection", "parent", "root"}


def test_the_collection_says_who_made_the_data_and_what_it_covers():
    """Attribution and filtering both live on the Collection; a browser shows them first."""
    payload = to_payload(_collection(_layer(), _layer("roads")))
    assert payload["providers"][0]["roles"] == ["producer", "licensor"]
    assert payload["summaries"]["oex:category"] == ["buildings", "roads"]
    assert payload["keywords"] == ["geodata"]


def test_the_collection_lists_the_formats_its_items_carry():
    """A format picker should not have to fetch every Item to learn the choices."""
    payload = _collection(_layer(geoparquet_url="https://x/b.parquet")).to_dict(
        include_self_link=False
    )
    assert sorted(payload["item_assets"]) == ["geojson", "geoparquet", "gpkg"]
    assert payload["item_assets"]["gpkg"]["title"] == "GeoPackage"


@pytest.mark.parametrize("hdx_license", sorted(HDX_LICENSE_LABELS))
@pytest.mark.network
def test_the_published_payload_validates_for_every_licence_the_config_allows(hdx_license):
    """STAC takes an SPDX expression; the HDX id and its human label are neither."""
    layer = _layer()
    layer.category.hdx.license = hdx_license
    payload = to_payload(_collection(layer, cfg=_cfg(s3=False)))
    # Validity is not enough: an HDX id such as `hdx-odc-odbl` matches the SPDX pattern
    # while naming no licence anyone can resolve.
    assert payload["license"] not in HDX_LICENSE_LABELS
    pystac.Collection.from_dict(payload).validate()


def test_a_licence_with_no_spdx_id_and_no_url_is_refused():
    """The spec: `other` with no licence link grants consumers no right to use the data."""
    layer = _layer(license_url=None)
    layer.category.hdx.license = "in-house-terms"
    with pytest.raises(ValueError, match="no SPDX identifier"):
        build_item(layer, _cfg())


def test_a_licence_with_no_configured_url_still_resolves_to_its_text():
    """license_url defaults to None, and the report reads the licence name off the link."""
    item = build_item(_layer(license_url=None), _cfg())
    link = next(link for link in item.links if link.rel == "license")
    assert link.href == "https://spdx.org/licenses/ODbL-1.0.html"
    assert link.title == "Open Database License (ODC-ODbL)"


def test_the_merged_tileset_is_a_collection_asset():
    """A viewer wants one tileset for the dataset, not one per layer."""
    payload = to_payload(
        _collection(
            _layer(),
            pmtiles_url="https://example-bucket.s3.amazonaws.com/ISO3/NPL/combined/d.pmtiles",
        )
    )
    assert payload["assets"]["pmtiles"]["href"].endswith("/combined/d.pmtiles")
    assert payload["assets"]["pmtiles"]["roles"] == ["visual", "overview"]


def test_a_per_layer_tileset_is_an_item_asset():
    """Without combine each layer has its own tiles, which belong on the layer."""
    item = build_item(_layer(pmtiles_url="https://x/d_osm.pmtiles"), _cfg())
    assert item.assets["pmtiles"].href == "https://x/d_osm.pmtiles"
    assert item.assets["pmtiles"].roles == ["visual", "overview"]


def test_every_layer_of_a_collection_resolves_back_to_its_category():
    """The dataset page matches layers by filename; an unresolvable name drops a layer."""
    from oex.hdx_publisher import _entry_name, _slug_from_metadata_name

    categories = [CategoryConfig(name="Points of Interest"), CategoryConfig(name="buildings")]
    layers = [_layer("Points of Interest"), _layer("buildings")]
    collection = to_payload(_collection(*layers))
    names = [_entry_name(collection, to_payload(build_item(layer, _cfg()))) for layer in layers]
    assert [_slug_from_metadata_name(n, "hot_flood_npl_", categories) for n in names] == [
        "points_of_interest",
        "buildings",
    ]


def test_relative_item_links_resolve_against_the_collection_that_carries_them():
    """A local catalog served as-is would otherwise have the reader fetching bare names."""
    from oex.hdx_publisher import _item_hrefs

    collection = to_payload(_collection(_layer(), cfg=_cfg(s3=False)))
    assert _item_hrefs(collection, "https://example.org/a/b/c_metadata.json") == [
        "https://example.org/a/b/hot_flood_npl_buildings_osm_item.json"
    ]
    up_one = {"links": [{"rel": "item", "href": "../x/item.json"}]}
    assert _item_hrefs(up_one, "https://example.org/a/b/c_metadata.json") == [
        "https://example.org/a/x/item.json"
    ]


def test_a_local_catalog_carries_no_absolute_filesystem_paths():
    """pystac resolves a relative self href against the working directory, which would
    publish the path of whichever machine happened to run the export."""
    layer = _layer()
    collection = _collection(layer, cfg=_cfg(s3=False), self_href="c_metadata.json")
    item = build_item(
        layer,
        _cfg(s3=False),
        self_href="hot_flood_npl_buildings_osm_item.json",
        collection_id="hot_flood_npl",
        collection_href="c_metadata.json",
    )
    for payload in (to_payload(collection), to_payload(item)):
        hrefs = [link["href"] for link in payload["links"]]
        assert not [h for h in hrefs if h.startswith("/")], hrefs
    # No self link either: the spec asks for one once a catalog is published, and an
    # absolute one is the only kind pystac can express.
    rels = {link["rel"] for link in to_payload(item)["links"]}
    assert rels >= {"root", "collection", "parent"} and "self" not in rels


def test_the_collection_names_one_root_and_it_is_itself():
    """Two root links, or a root pointing elsewhere, and a crawler walks out of the dataset."""
    href = "https://example-bucket.s3.amazonaws.com/ISO3/NPL/combined/c.json"
    payload = to_payload(_collection(_layer(), self_href=href))
    roots = [link["href"] for link in payload["links"] if link["rel"] == "root"]
    assert roots == [href]
    assert [link["href"] for link in payload["links"] if link["rel"] == "self"] == [href]


def test_an_items_root_is_the_collection_not_the_item():
    """The root is where a crawler starts; an Item that roots at itself hides the dataset."""
    collection_href = "https://example-bucket.s3.amazonaws.com/ISO3/NPL/combined/c.json"
    item_href = "https://example-bucket.s3.amazonaws.com/ISO3/NPL/buildings/i.json"
    payload = to_payload(
        build_item(
            _layer(),
            _cfg(),
            self_href=item_href,
            collection_id="hot_flood_npl",
            collection_href=collection_href,
        )
    )
    rels = {link["rel"]: link["href"] for link in payload["links"]}
    assert rels["root"] == collection_href
    assert rels["self"] == item_href


def test_the_collection_covers_the_time_range_its_items_declare():
    """Items carry start_datetime from the data; an extent built from the snapshot alone
    tells a client filtering by time that eleven years of data are not there."""
    payload = to_payload(_collection(_rich_layer()))
    # The snapshot is the later bound here, so the extent spans both it and the data.
    assert payload["extent"]["temporal"]["interval"] == [
        ["2026-08-01T00:00:00Z", "2026-09-18T03:55:55Z"]
    ]


def test_a_property_with_no_value_is_left_out():
    """A null is a field an intermediary may strip, and the report would then fail to read it."""
    layer = _layer()
    layer.source_metadata = replace(layer.source_metadata, pcode_source_date=None, boundary=None)
    payload = to_payload(build_item(layer, _cfg()))
    assert [key for key, value in payload["properties"].items() if value is None] == []
    assert "oex:pcode_source_date" not in payload["properties"]
    assert SourceMetadata.from_payload(payload).pcode_source_date is None


def test_a_layer_with_no_configured_licence_url_reads_back_with_the_spdx_one():
    """The report renders whatever the link carries, so the round trip must say plainly
    that a layer with no configured URL gains the SPDX one rather than keeping None."""
    layer = _layer(license_url=None)
    restored = SourceMetadata.from_payload(to_payload(build_item(layer, _cfg())))
    assert restored.license_url == "https://spdx.org/licenses/ODbL-1.0.html"
    assert restored.license_label == "Open Database License (ODC-ODbL)"
    assert restored.metadata == layer.source_metadata.metadata


@pytest.mark.network
def test_the_item_footprint_is_the_bbox_as_a_closed_lon_lat_ring():
    """A swapped axis order puts Nepal in the Indian Ocean, and validate() does not notice."""
    footprint = build_item(_layer(), _cfg()).geometry
    assert footprint is not None
    ring = footprint["coordinates"][0]
    west, south, east, north = NPL_BBOX
    assert ring[0] == [west, south]
    assert ring[2] == [east, north]
    assert ring[0] == ring[-1]
    assert len(ring) == 5


def test_the_item_footprint_is_the_export_boundary_and_its_bbox_crosses_180():
    """RFC 7946 section 5.2: a bbox over the antimeridian has its west above its east."""
    islands = {
        "type": "MultiPolygon",
        "coordinates": [
            [[[179.0, -17.0], [180.0, -17.0], [180.0, -16.0], [179.0, -16.0], [179.0, -17.0]]],
            [[[-180.0, -17.0], [-179.0, -17.0], [-179.0, -16.0], [-180.0, -16.0], [-180.0, -17.0]]],
        ],
    }
    item = build_item(_layer(footprint=json.dumps(islands)), _cfg())
    assert item.geometry == islands
    assert item.bbox == [179.0, -17.0, -179.0, -16.0]


def test_a_dataset_with_no_footprint_still_writes_valid_json():
    """pystac returns infinities for an extent with no bbox, and Infinity is not JSON."""
    payload = to_payload(_collection(_layer(feature_count=0, bbox=None)))
    assert payload["extent"]["spatial"]["bbox"] == [[-180.0, -90.0, 180.0, 90.0]]
    json.loads(json.dumps(payload))


def _mixed_licence_collection() -> pystac.Collection:
    """Overture places are CDLA and its buildings ODbL, and one Collection carries both."""
    places = _layer("places", license_url="https://cdla.dev/permissive-2-0/")
    places.category.hdx.license = "CDLA Permissive 2.0"
    return _collection(_layer(), places)


def test_a_collection_of_differently_licensed_layers_names_every_licence():
    collection = _mixed_licence_collection()
    assert collection.license == "other"
    hrefs = sorted(link.href for link in collection.links if link.rel == "license")
    assert hrefs == ["https://cdla.dev/permissive-2-0/", ODBL_URL]


@pytest.mark.network
def test_a_mixed_licence_collection_satisfies_the_stac_spec():
    pystac.validation.validate_dict(to_payload(_mixed_licence_collection()))


def test_one_licence_with_two_texts_links_both():
    """Two layers naming the same licence can still point readers at different texts."""
    other = _layer("roads", license_url="https://example.org/odbl-mirror")
    collection = _collection(_layer(), other)
    hrefs = sorted(link.href for link in collection.links if link.rel == "license")
    assert hrefs == ["https://example.org/odbl-mirror", ODBL_URL]


@pytest.mark.parametrize("hdx_license", sorted(HDX_LICENSE_LABELS))
def test_every_hdx_licence_publishes_with_its_spdx_id(hdx_license):
    """A licence HDX names but oex cannot map would publish as `other` with no text."""
    assert spdx_license(hdx_license) != "other"


def test_metadata_files_carry_names_in_their_own_script(tmp_path):
    """Escaped as \\u0936, a Nepali name is unreadable to anyone opening the file."""
    from oex.stac import write_json

    layer = _layer()
    layer.source_metadata.metadata.columns[0] = ColumnReport(
        "name_ne", "VARCHAR", 0, 0.0, 1, [{"value": "शिव मन्दिर", "count": 18}]
    )
    path = tmp_path / "item.json"
    write_json(build_item(layer, _cfg()), path)
    raw = path.read_text(encoding="utf-8")
    assert "शिव मन्दिर" in raw
    assert "\\u0936" not in raw


def test_an_item_links_to_its_hdx_dataset_when_published_there():
    url = "https://data.humdata.org/dataset/hotosm_npl_buildings"
    via = [link for link in build_item(_layer(), _cfg(), hdx_url=url).links if link.rel == "via"]
    assert [link.href for link in via] == [url]
    assert not [link for link in build_item(_layer(), _cfg()).links if link.rel == "via"]


def _with_related() -> pystac.Collection:
    return _collection(
        _layer(),
        related=[("Roads", "https://example-bucket.s3.amazonaws.com/ISO3/NPL/roads/r.json")],
    )


def test_a_collection_links_the_areas_other_datasets():
    related = [link for link in _with_related().links if link.rel == "related"]
    assert [(link.title, link.href) for link in related] == [
        ("Roads", "https://example-bucket.s3.amazonaws.com/ISO3/NPL/roads/r.json")
    ]
    assert not [link for link in _collection(_layer()).links if link.rel == "related"]


@pytest.mark.network
def test_a_collection_with_related_links_satisfies_the_stac_spec():
    pystac.validation.validate_dict(to_payload(_with_related()))


def _under_grid(cfg: RootConfig) -> RootConfig:
    cfg.output.stac.catalog = True
    return cfg


def test_a_configured_catalog_becomes_the_root_and_parent_a_client_walks_back_to():
    collection = _collection(_layer(), cfg=_under_grid(_cfg()))
    links = {link.rel: link.target for link in collection.links if link.rel in ("root", "parent")}
    assert links == {
        "root": "https://example-bucket.s3.amazonaws.com/ISO3/catalog.json",
        "parent": "https://example-bucket.s3.amazonaws.com/ISO3/NPL/catalog.json",
    }
    item = build_item(_layer(), _under_grid(_cfg()))
    assert [link.target for link in item.links if link.rel == "root"] == [
        "https://example-bucket.s3.amazonaws.com/ISO3/catalog.json"
    ]
    plain = _collection(_layer())
    assert not [link for link in plain.links if link.rel == "parent"]


@pytest.mark.network
def test_a_collection_under_a_catalog_satisfies_the_stac_spec():
    pystac.validation.validate_dict(to_payload(_collection(_layer(), cfg=_under_grid(_cfg()))))


def test_an_item_records_how_long_its_layer_took_to_build():
    item = build_item(_layer(export_seconds=41.27), _cfg())
    assert item.properties["oex:export_seconds"] == 41.3


def test_an_item_without_a_measured_build_carries_no_build_time():
    assert "oex:export_seconds" not in build_item(_layer(), _cfg()).properties
