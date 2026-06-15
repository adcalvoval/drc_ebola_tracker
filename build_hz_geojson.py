#!/usr/bin/env python3
"""
Download DRC health zone boundaries from HDX (April 2022 DHIS2 dataset),
filter to the three affected provinces (Ituri, North Kivu, South Kivu),
and write data/drc_affected_health_zones.geojson.

Source: https://data.humdata.org/dataset/drc-health-data
        RDC_Zones de santé.zip (GRID3/OSM/DSNIS/WHO, April 2022)

Run once (or whenever the upstream shapefile changes):
    python build_hz_geojson.py
"""

import io
import json
import os
import sys
import zipfile
from urllib.request import urlopen, Request

import shapefile  # pyshp

ZIP_URL = (
    "https://data.humdata.org/dataset/cecb0d2f-331f-45be-a613-58aa506844f9"
    "/resource/32e9c52a-2be0-45a1-9372-33d0dab2c08a/download/rdc_zones-de-sante.zip"
)

OUT_PATH = os.path.join(os.path.dirname(__file__), "data", "drc_affected_health_zones.geojson")

# Only Ituri: all named topHealthZones (bunia, rwampara, mongbwalu/mongbalu, nyankunde)
# are in Ituri. Nord/Sud-Kivu zone-level data is not yet available in the feed.
AFFECTED_PROVINCES = {"ituri"}

def normalise(s):
    return (s or "").lower().strip()

def province_is_affected(props, fields):
    """Return True if the PROVINCE field matches an affected province."""
    val = normalise(props.get("PROVINCE", ""))
    return val in AFFECTED_PROVINCES

COORD_PRECISION = 4  # ~11 m — sufficient for province-scale display

def round_ring(points):
    return [[round(p[0], COORD_PRECISION), round(p[1], COORD_PRECISION)] for p in points]

def shapefile_to_geojson_feature(shape, record, fields):
    """Convert a pyshp shape+record pair to a GeoJSON feature dict."""
    props = {fields[i]: record[i] for i in range(len(fields))}

    if shape.shapeType == 5:  # Polygon
        parts = list(shape.parts) + [len(shape.points)]
        rings = [round_ring(shape.points[parts[i]:parts[i + 1]])
                 for i in range(len(parts) - 1)]
        geometry = {"type": "Polygon", "coordinates": rings}
    else:
        return None

    return {"type": "Feature", "geometry": geometry, "properties": props}

def main():
    print(f"Downloading {ZIP_URL} …")
    req = Request(ZIP_URL, headers={"User-Agent": "ebola-map/1.0"})
    with urlopen(req, timeout=120) as resp:
        data = resp.read()
    print(f"  Downloaded {len(data):,} bytes.")

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        print("  ZIP contents:", names)
        shp_name = next((n for n in names if n.lower().endswith(".shp")), None)
        dbf_name = next((n for n in names if n.lower().endswith(".dbf")), None)
        shx_name = next((n for n in names if n.lower().endswith(".shx")), None)
        if not shp_name:
            sys.exit("No .shp file found in ZIP.")

        shp_bytes = io.BytesIO(zf.read(shp_name))
        dbf_bytes = io.BytesIO(zf.read(dbf_name))
        shx_bytes = io.BytesIO(zf.read(shx_name)) if shx_name else None

    sf = shapefile.Reader(shp=shp_bytes, dbf=dbf_bytes, shx=shx_bytes)
    fields = [f[0] for f in sf.fields[1:]]  # skip deletion flag
    print(f"  Fields: {fields}")
    print(f"  Total features: {len(sf)}")

    features = []
    skipped = 0
    for shape_rec in sf.iterShapeRecords():
        props = {fields[i]: shape_rec.record[i] for i in range(len(fields))}
        if province_is_affected(props, fields):
            feat = shapefile_to_geojson_feature(shape_rec.shape, shape_rec.record, fields)
            if feat:
                features.append(feat)
        else:
            skipped += 1

    print(f"  Kept {len(features)} features, skipped {skipped}.")

    if not features:
        # Fallback: print unique province values to help diagnose field name / value mismatch
        print("\nNo features matched. Sample property sets:")
        for i, shape_rec in enumerate(sf.iterShapeRecords()):
            if i >= 5:
                break
            props = {fields[j]: shape_rec.record[j] for j in range(len(fields))}
            print(" ", props)
        sys.exit("Adjust AFFECTED_PROVINCES or province field name above.")

    geojson = {
        "type": "FeatureCollection",
        "features": features,
        "_source": "HDX / GRID3-M4H / OSM / DSNIS / WHO (April 2022)",
        "_filtered": "Ituri, Nord-Kivu, Sud-Kivu provinces",
    }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(geojson, f, separators=(",", ":"), ensure_ascii=False)

    size_kb = os.path.getsize(OUT_PATH) / 1024
    print(f"Written: {OUT_PATH}  ({size_kb:.0f} KB, {len(features)} zones)")

if __name__ == "__main__":
    main()
