"""Country boundary resolution: user-supplied geom or geoBoundaries ADM0."""

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any

import antimeridian
import requests
import shapely
from pyproj import CRS, Geod, Transformer
from shapely.geometry import Point, mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform

from oex.config.schema import BoundaryConfig
from oex.logging_setup import get_logger

logger = get_logger(__name__)

_GEOBOUNDARIES_RELEASE = "gbOpen"
_GEOBOUNDARIES_TPL = (
    f"https://www.geoboundaries.org/api/current/{_GEOBOUNDARIES_RELEASE}/{{iso3}}/{{level}}/"
)

WORLD_GEOJSON: dict[str, Any] = {
    "type": "Polygon",
    "coordinates": [[[-180, -90], [180, -90], [180, 90], [-180, 90], [-180, -90]]],
}


@dataclass(frozen=True)
class Boundary:
    iso3: str
    bbox: tuple[float, float, float, float]
    geojson: str
    source: str


def area_sq_km(geometry: dict[str, Any]) -> float:
    """Geodesic area on the WGS84 ellipsoid, holes excluded."""
    # pyproj signs each ring by its winding, and drawn boundaries do not always follow the
    # right-hand rule, so an unoriented hole would add area.
    area, _perimeter = Geod(ellps="WGS84").geometry_area_perimeter(
        shapely.orient_polygons(shape(geometry))
    )
    return abs(area) / 1_000_000


_lock = threading.Lock()
_cache: dict[tuple[str, str, str, str, str, str], Boundary] = {}


def _bbox_from_geometry(geometry: dict[str, Any]) -> tuple[float, float, float, float]:
    coords: list[float] = []

    def walk(node: Any) -> None:
        if (
            isinstance(node, list)
            and len(node) == 2
            and all(isinstance(v, (int, float)) for v in node)
        ):
            coords.extend(node)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(geometry.get("coordinates", []))
    if not coords:
        raise ValueError("No coordinates found in geometry")
    xs = coords[0::2]
    ys = coords[1::2]
    return (min(xs), min(ys), max(xs), max(ys))


def _featurecollection_to_geometry(fc: dict[str, Any]) -> dict[str, Any]:
    # ST_GeomFromGeoJSON accepts a single geometry or a GeometryCollection,
    # not a FeatureCollection.
    features = fc.get("features", [])
    geometries = [f["geometry"] for f in features if f.get("geometry")]
    if len(geometries) == 1:
        return geometries[0]
    return {"type": "GeometryCollection", "geometries": geometries}


def _fetch_geoboundaries(iso3: str, level: str) -> Boundary:
    url = _GEOBOUNDARIES_TPL.format(iso3=iso3.upper(), level=level)
    logger.info("Fetching boundary metadata: %s", url)
    meta = requests.get(url, timeout=60)
    meta.raise_for_status()
    payload = meta.json()
    geojson_url = payload.get("gjDownloadURL") or payload.get("simplifiedGeometryGeoJSON")
    if not geojson_url:
        raise RuntimeError(f"geoBoundaries response missing GeoJSON URL for {iso3}")

    logger.info("Downloading boundary geometry: %s", geojson_url)
    resp = requests.get(geojson_url, timeout=180)
    resp.raise_for_status()
    fc = resp.json()
    geometry = _featurecollection_to_geometry(fc) if fc.get("type") == "FeatureCollection" else fc
    bbox = _bbox_from_geometry(geometry)
    return Boundary(
        iso3=iso3.upper(),
        bbox=bbox,
        geojson=json.dumps(geometry),
        source=f"geoBoundaries {_GEOBOUNDARIES_RELEASE} {level}",
    )


def _world_boundary(iso3: str) -> Boundary:
    return Boundary(
        iso3=iso3.upper(),
        bbox=(-180.0, -90.0, 180.0, 90.0),
        geojson=json.dumps(WORLD_GEOJSON),
        source="whole planet (boundary.geom: world)",
    )


def _from_user_geom(iso3: str, geom_str: str) -> Boundary:
    fc = json.loads(geom_str)
    geometry = _featurecollection_to_geometry(fc) if fc.get("type") == "FeatureCollection" else fc
    bbox = _bbox_from_geometry(geometry)
    return Boundary(
        iso3=iso3.upper(),
        bbox=bbox,
        geojson=json.dumps(geometry),
        source="user-provided",
    )


def _projection_centre(geom: BaseGeometry) -> Point:
    """The centroid with longitudes read over [0, 360) when that makes the shape narrower,
    so land spread across 180 degrees is centred where it lies."""
    shifted = shapely.transform(geom, lambda xy: xy + [360.0, 0.0] * (xy[:, :1] < 0))
    if _width(shifted) >= _width(geom):
        return geom.centroid
    centre = shifted.centroid
    return Point((centre.x + 180.0) % 360.0 - 180.0, centre.y)


def _width(geom: BaseGeometry) -> float:
    return geom.bounds[2] - geom.bounds[0]


def _buffered(boundary: Boundary, buffer_meters: float, simplify_meters: float) -> Boundary:
    # A metre in EPSG:3857 is inflated by 1/cos(latitude); an azimuthal equidistant
    # projection centred on the boundary keeps the buffer true at any latitude.
    geom = shape(json.loads(boundary.geojson))
    centre = _projection_centre(geom)
    local = CRS.from_proj4(
        f"+proj=aeqd +lat_0={centre.y} +lon_0={centre.x} +datum=WGS84 +units=m +no_defs"
    )
    to_local = Transformer.from_crs(4326, local, always_xy=True).transform
    to_wgs84 = Transformer.from_crs(local, 4326, always_xy=True).transform
    # GEOS needs tens of GB to buffer a fjord coast at full detail. Each simplify moves edges
    # by up to its tolerance, so two of them stay inside the buffer below half of it.
    local_geom = transform(to_local, geom).simplify(simplify_meters)
    widened = local_geom.buffer(buffer_meters).simplify(simplify_meters)
    # Land on both sides of 180 degrees comes back as rings that jump across the globe;
    # antimeridian splits them at the meridian again.
    back = shape(antimeridian.fix_shape(mapping(transform(to_wgs84, widened)), fix_winding=True))
    simplified = f", simplified {simplify_meters:g}m" if simplify_meters else ""
    return Boundary(
        iso3=boundary.iso3,
        bbox=back.bounds,
        geojson=json.dumps(mapping(back)),
        source=f"{boundary.source} (buffered +{buffer_meters:g}m{simplified})",
    )


def resolve_boundary(iso3: str, cfg: BoundaryConfig) -> Boundary:
    if not iso3 and not cfg.geom:
        raise ValueError("iso3 must be set on the config, or boundary.geom provided")
    if cfg.buffer_meters < 0:
        raise ValueError(
            f"boundary.buffer_meters must be >= 0; got {cfg.buffer_meters}. "
            "Inward buffers are not supported (would shrink the export area)."
        )
    if cfg.simplify_meters < 0 or (
        cfg.simplify_meters and cfg.simplify_meters * 2 >= cfg.buffer_meters
    ):
        raise ValueError(
            f"boundary.simplify_meters must be >= 0 and below half of buffer_meters; "
            f"got simplify_meters={cfg.simplify_meters}, buffer_meters={cfg.buffer_meters}"
        )
    # Without cfg.geom in the key, two configs with different user geometries for
    # one country share an entry and the second exports the first one's area.
    key = (
        iso3.upper(),
        _GEOBOUNDARIES_RELEASE,
        cfg.geoboundaries_level,
        f"buf{cfg.buffer_meters:g}",
        f"simplify{cfg.simplify_meters:g}",
        hashlib.sha256((cfg.geom or "").encode()).hexdigest(),
    )
    with _lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached

    if cfg.geom:
        if cfg.geom.strip().lower() == "world":
            boundary = _world_boundary(iso3)
        else:
            boundary = _from_user_geom(iso3, cfg.geom)
    else:
        boundary = _fetch_geoboundaries(iso3, cfg.geoboundaries_level)

    if cfg.buffer_meters > 0:
        boundary = _buffered(boundary, cfg.buffer_meters, cfg.simplify_meters)

    with _lock:
        _cache[key] = boundary
    logger.info(
        "Resolved boundary for %s from %s; bbox=%s",
        boundary.iso3,
        boundary.source,
        boundary.bbox,
    )
    return boundary
