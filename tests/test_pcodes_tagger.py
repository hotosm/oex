"""Pcode tagging SQL against hand-built admin parquets shaped like fieldmaps.io's
edge-matched schema, with no network.
"""

from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest

from oex.pcodes.cache import PcodeCacheEntry
from oex.pcodes.tagger import _load_admin_table, tag_table, write_country_admin_parquets


def _write_admin_parquet(
    conn: duckdb.DuckDBPyConnection,
    *,
    target: Path,
    level: int,
    rows: list[dict[str, object]],
) -> Path:
    """A minimal fieldmaps-shaped admin parquet holding only what the tagger reads: iso_3,
    adm{level}_src and _name, adm0_src and _name, and geometry."""
    table = f"_tmp_adm{level}"
    conn.execute(
        f"""
        CREATE OR REPLACE TABLE {table} (
            iso_3 VARCHAR,
            adm{level}_src VARCHAR,
            adm{level}_name VARCHAR,
            adm0_src VARCHAR,
            adm0_name VARCHAR,
            geometry GEOMETRY
        )
        """
    )
    for row in rows:
        conn.execute(
            f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ST_GeomFromText(?))",
            [
                row["iso_3"],
                row[f"adm{level}_src"],
                row[f"adm{level}_name"],
                row["adm0_src"],
                row["adm0_name"],
                row["wkt"],
            ],
        )
    conn.execute(f"COPY {table} TO '{target}' (FORMAT PARQUET)")
    conn.execute(f"DROP TABLE {table}")
    return target


@pytest.fixture
def conn() -> Iterator[duckdb.DuckDBPyConnection]:
    c = duckdb.connect(":memory:")
    c.execute("INSTALL spatial; LOAD spatial")
    c.execute("INSTALL h3 FROM community; LOAD h3")
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def admin_cache(tmp_path: Path, conn: duckdb.DuckDBPyConnection) -> dict[int, PcodeCacheEntry]:
    """Square country: adm1 NP01 is the NW quarter (x 0-5, y 5-10), split into NP0101 (y 7-10)
    and NP0102 (y 5-7); adm1 NP02 is the SE quarter (x 5-10, y 0-5), all of it NP0201."""
    adm1_path = tmp_path / "adm1_polygons.parquet"
    _write_admin_parquet(
        conn,
        target=adm1_path,
        level=1,
        rows=[
            {
                "iso_3": "NPL",
                "adm1_src": "NP01",
                "adm1_name": "Province NW",
                "adm0_src": "NPL",
                "adm0_name": "Nepal",
                "wkt": "POLYGON((0 5, 5 5, 5 10, 0 10, 0 5))",
            },
            {
                "iso_3": "NPL",
                "adm1_src": "NP02",
                "adm1_name": "Province SE",
                "adm0_src": "NPL",
                "adm0_name": "Nepal",
                "wkt": "POLYGON((5 0, 10 0, 10 5, 5 5, 5 0))",
            },
            # Decoy row for a different country: tagger must ignore.
            {
                "iso_3": "IND",
                "adm1_src": "INXX",
                "adm1_name": "Decoy",
                "adm0_src": "IND",
                "adm0_name": "India",
                "wkt": "POLYGON((0 5, 5 5, 5 10, 0 10, 0 5))",
            },
        ],
    )
    adm2_path = tmp_path / "adm2_polygons.parquet"
    _write_admin_parquet(
        conn,
        target=adm2_path,
        level=2,
        rows=[
            {
                "iso_3": "NPL",
                "adm2_src": "NP0101",
                "adm2_name": "NW Upper",
                "adm0_src": "NPL",
                "adm0_name": "Nepal",
                "wkt": "POLYGON((0 7, 5 7, 5 10, 0 10, 0 7))",
            },
            {
                "iso_3": "NPL",
                "adm2_src": "NP0102",
                "adm2_name": "NW Lower",
                "adm0_src": "NPL",
                "adm0_name": "Nepal",
                "wkt": "POLYGON((0 5, 5 5, 5 7, 0 7, 0 5))",
            },
            {
                "iso_3": "NPL",
                "adm2_src": "NP0201",
                "adm2_name": "SE",
                "adm0_src": "NPL",
                "adm0_name": "Nepal",
                "wkt": "POLYGON((5 0, 10 0, 10 5, 5 5, 5 0))",
            },
        ],
    )
    return {
        1: PcodeCacheEntry(level=1, path=adm1_path, upstream_date="test", upstream_url=""),
        2: PcodeCacheEntry(level=2, path=adm2_path, upstream_date="test", upstream_url=""),
    }


def _make_features_table(conn: duckdb.DuckDBPyConnection, name: str) -> None:
    conn.execute(
        f"""
        CREATE TABLE {name} AS SELECT * FROM (VALUES
            (1, 'NW upper point',  ST_Point(2.5, 8.0)),
            (2, 'NW lower point',  ST_Point(2.5, 6.0)),
            (3, 'SE point',        ST_Point(7.5, 2.5)),
            (4, 'Outside country', ST_Point(20.0, 20.0)),
            (5, 'NW polygon',
                ST_GeomFromText('POLYGON((1 8, 2 8, 2 9, 1 9, 1 8))'))
        ) AS t(id, label, geom)
        """
    )


def test_tag_table_assigns_pcodes_by_centroid(
    conn: duckdb.DuckDBPyConnection,
    admin_cache: dict[int, PcodeCacheEntry],
) -> None:
    _make_features_table(conn, "features")

    report = tag_table(
        conn,
        table="features",
        iso3="NPL",
        cache_entries=admin_cache,
        levels=[1, 2],
    )

    assert report.iso3 == "NPL"
    assert report.levels_tagged == [1, 2]
    assert report.levels_empty == []
    assert report.adm0_pcode == "NPL"
    assert report.adm0_name == "Nepal"

    rows = conn.execute(
        "SELECT id, adm0_pcode, adm0_name, adm1_pcode, adm1_name, adm2_pcode, adm2_name "
        "FROM features ORDER BY id"
    ).fetchall()

    by_id = {r[0]: r for r in rows}
    assert by_id[1] == (1, "NPL", "Nepal", "NP01", "Province NW", "NP0101", "NW Upper")
    assert by_id[2] == (2, "NPL", "Nepal", "NP01", "Province NW", "NP0102", "NW Lower")
    assert by_id[3] == (3, "NPL", "Nepal", "NP02", "Province SE", "NP0201", "SE")
    assert by_id[4] == (4, "NPL", "Nepal", None, None, None, None)
    # Polygon centroid (1.5, 8.5) lands in NW Upper.
    assert by_id[5] == (5, "NPL", "Nepal", "NP01", "Province NW", "NP0101", "NW Upper")


def test_tag_table_emits_nulls_for_missing_levels(
    conn: duckdb.DuckDBPyConnection,
    admin_cache: dict[int, PcodeCacheEntry],
) -> None:
    """Levels not in the cache are emitted as NULL columns, not skipped."""
    _make_features_table(conn, "features")

    report = tag_table(
        conn,
        table="features",
        iso3="NPL",
        cache_entries=admin_cache,
        levels=[1, 2, 3, 4],
    )
    assert sorted(report.levels_tagged) == [1, 2]
    assert sorted(report.levels_empty) == [3, 4]

    cols = [r[0] for r in conn.execute("DESCRIBE features").fetchall()]
    for n in (0, 1, 2, 3, 4):
        assert f"adm{n}_pcode" in cols
        assert f"adm{n}_name" in cols

    nulls = conn.execute(
        "SELECT adm3_pcode, adm3_name, adm4_pcode, adm4_name FROM features LIMIT 1"
    ).fetchone()
    assert nulls == (None, None, None, None)


def test_tag_table_skips_levels_with_null_pcodes(
    conn: duckdb.DuckDBPyConnection,
    tmp_path: Path,
) -> None:
    """Polygons that exist for the ISO3 but have a NULL pcode column are filtered
    out at load time, so the level is treated as empty (no tessellation, no join)."""
    adm1_path = tmp_path / "adm1_polygons.parquet"
    _write_admin_parquet(
        conn,
        target=adm1_path,
        level=1,
        rows=[
            {
                "iso_3": "NPL",
                "adm1_src": "NP01",
                "adm1_name": "Province",
                "adm0_src": "NPL",
                "adm0_name": "Nepal",
                "wkt": "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))",
            },
        ],
    )
    adm2_path = tmp_path / "adm2_polygons.parquet"
    _write_admin_parquet(
        conn,
        target=adm2_path,
        level=2,
        rows=[
            {
                "iso_3": "NPL",
                "adm2_src": None,
                "adm2_name": "Unmapped",
                "adm0_src": "NPL",
                "adm0_name": "Nepal",
                "wkt": "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))",
            },
        ],
    )
    cache = {
        1: PcodeCacheEntry(level=1, path=adm1_path, upstream_date="t", upstream_url=""),
        2: PcodeCacheEntry(level=2, path=adm2_path, upstream_date="t", upstream_url=""),
    }
    _make_features_table(conn, "features")

    report = tag_table(conn, table="features", iso3="NPL", cache_entries=cache, levels=[1, 2])
    assert report.levels_tagged == [1]
    assert report.levels_empty == [2]

    row = conn.execute("SELECT adm1_pcode, adm2_pcode FROM features WHERE id = 1").fetchone()
    assert row == ("NP01", None)


def test_tag_table_handles_country_with_no_admin_data(
    conn: duckdb.DuckDBPyConnection,
    admin_cache: dict[int, PcodeCacheEntry],
) -> None:
    """A country with no admin data still gets null pcode columns, so every country in a sweep
    writes the same schema."""
    _make_features_table(conn, "features")

    report = tag_table(
        conn,
        table="features",
        iso3="ATA",  # Antarctica, not in our fixture
        cache_entries=admin_cache,
        levels=[1, 2],
    )

    assert report.levels_tagged == []
    assert sorted(report.levels_empty) == [1, 2]
    assert report.adm0_pcode is None

    cols_after = {r[0] for r in conn.execute("DESCRIBE features").fetchall()}
    assert {
        "adm0_pcode",
        "adm0_name",
        "adm1_pcode",
        "adm1_name",
        "adm2_pcode",
        "adm2_name",
    } <= cols_after
    rows = conn.execute(
        "SELECT adm0_pcode, adm0_name, adm1_pcode, adm2_pcode FROM features"
    ).fetchall()
    for r in rows:
        assert r[0] == "ATA"
        assert r[1] is None
        assert r[2] is None
        assert r[3] is None


def test_tag_table_drops_temp_admin_tables(
    conn: duckdb.DuckDBPyConnection,
    admin_cache: dict[int, PcodeCacheEntry],
) -> None:
    _make_features_table(conn, "features")
    tag_table(
        conn,
        table="features",
        iso3="NPL",
        cache_entries=admin_cache,
        levels=[1, 2],
    )
    leftovers = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_name LIKE '_pcodes_%'"
    ).fetchall()
    assert leftovers == []


def test_tag_table_treats_iso3_as_data_not_sql(
    conn: duckdb.DuckDBPyConnection,
    admin_cache: dict[int, PcodeCacheEntry],
) -> None:
    """A malicious iso3 must not execute as SQL: features table survives."""
    _make_features_table(conn, "features")
    row = conn.execute("SELECT COUNT(*) FROM features").fetchone()
    assert row is not None
    feature_count_before = row[0]

    report = tag_table(
        conn,
        table="features",
        iso3="NPL'; DROP TABLE features; --",
        cache_entries=admin_cache,
        levels=[1, 2],
    )
    assert report.levels_tagged == []
    rows = conn.execute("SELECT COUNT(*) FROM features").fetchone()
    assert rows is not None
    assert rows[0] == feature_count_before, "iso3 must be parameterised, not interpolated"


def test_tag_table_quotes_apostrophes_in_adm0_name(
    conn: duckdb.DuckDBPyConnection,
    tmp_path: Path,
) -> None:
    """adm0 values are interpolated as SQL literals; apostrophes must be safe."""
    parquet = tmp_path / "adm1_polygons.parquet"
    _write_admin_parquet(
        conn,
        target=parquet,
        level=1,
        rows=[
            {
                "iso_3": "CIV",
                "adm1_src": "CI01",
                "adm1_name": "Region",
                "adm0_src": "CI'V",  # contrived: apostrophe in pcode
                "adm0_name": "Cote d'Ivoire",
                "wkt": "POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))",
            },
        ],
    )
    cache = {1: PcodeCacheEntry(level=1, path=parquet, upstream_date="t", upstream_url="")}
    conn.execute("CREATE TABLE features AS SELECT 1 AS id, ST_Point(5, 5) AS geom")

    report = tag_table(conn, table="features", iso3="CIV", cache_entries=cache, levels=[1])
    assert report.adm0_name == "Cote d'Ivoire"

    row = conn.execute("SELECT adm0_pcode, adm0_name FROM features").fetchone()
    assert row == ("CI'V", "Cote d'Ivoire")


def test_country_admin_parquets_keep_only_the_country(
    tmp_path: Path,
    conn: duckdb.DuckDBPyConnection,
    admin_cache: dict[int, PcodeCacheEntry],
) -> None:
    country = write_country_admin_parquets(
        conn, iso3="npl", cache_entries=admin_cache, target_dir=tmp_path / "country"
    )

    assert set(country) == {1, 2}
    assert country[1].upstream_date == admin_cache[1].upstream_date
    rows = conn.execute(
        "SELECT DISTINCT iso_3, adm1_src FROM read_parquet(?) ORDER BY adm1_src",
        [str(country[1].path)],
    ).fetchall()
    assert rows == [("NPL", "NP01"), ("NPL", "NP02")]


def _write_jagged_admin_parquet(conn: duckdb.DuckDBPyConnection, target: Path) -> Path:
    """Vertices 0.00001 degrees off a straight edge sit inside the 0.0001 tolerance."""
    jagged_edge = ", ".join(f"{x / 10} {0.00001 * (x % 2)}" for x in range(1, 100))
    return _write_admin_parquet(
        conn,
        target=target,
        level=1,
        rows=[
            {
                "iso_3": "NPL",
                "adm1_src": "NP01",
                "adm1_name": "Jagged",
                "adm0_src": "NPL",
                "adm0_name": "Nepal",
                "wkt": f"POLYGON((0 0, {jagged_edge}, 10 0, 10 10, 0 10, 0 0))",
            }
        ],
    )


def test_country_admin_parquets_are_simplified(
    tmp_path: Path, conn: duckdb.DuckDBPyConnection
) -> None:
    adm1 = _write_jagged_admin_parquet(conn, tmp_path / "adm1_polygons.parquet")
    cache = {1: PcodeCacheEntry(level=1, path=adm1, upstream_date="t", upstream_url="")}

    country = write_country_admin_parquets(
        conn, iso3="NPL", cache_entries=cache, target_dir=tmp_path / "country"
    )

    row = conn.execute(
        "SELECT ST_NPoints(CAST(geometry AS GEOMETRY)) FROM read_parquet(?)",
        [str(country[1].path)],
    ).fetchone()
    assert row is not None
    assert row[0] == 5


def test_tagging_from_country_admin_parquets_matches_the_global_files(
    tmp_path: Path,
    conn: duckdb.DuckDBPyConnection,
    admin_cache: dict[int, PcodeCacheEntry],
) -> None:
    country = write_country_admin_parquets(
        conn, iso3="NPL", cache_entries=admin_cache, target_dir=tmp_path / "country"
    )
    _make_features_table(conn, "from_global")
    _make_features_table(conn, "from_country")

    tag_table(conn, table="from_global", iso3="NPL", cache_entries=admin_cache, levels=[1, 2])
    tag_table(conn, table="from_country", iso3="NPL", cache_entries=country, levels=[1, 2])

    columns = "id, adm0_pcode, adm1_pcode, adm1_name, adm2_pcode, adm2_name"
    from_global = conn.execute(f"SELECT {columns} FROM from_global ORDER BY id").fetchall()
    from_country = conn.execute(f"SELECT {columns} FROM from_country ORDER BY id").fetchall()
    assert from_country == from_global
    assert from_global[0][2] == "NP01"


def test_loading_admin_polygons_keeps_their_geometry(
    tmp_path: Path, conn: duckdb.DuckDBPyConnection
) -> None:
    """Simplifying again would move vertices of an already simplified coast."""
    adm1 = _write_jagged_admin_parquet(conn, tmp_path / "adm1_polygons.parquet")

    _load_admin_table(conn, parquet_path=adm1, iso3="NPL", level=1, target_table="loaded")

    row = conn.execute(
        "SELECT (SELECT ST_NPoints(admin_geom) FROM loaded),"
        " (SELECT ST_NPoints(CAST(geometry AS GEOMETRY)) FROM read_parquet(?))",
        [str(adm1)],
    ).fetchone()
    assert row is not None
    assert row[0] == row[1]
