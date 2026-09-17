"""Regenerate viz_defaults.json from the installed Qlik Sense client.

Every native Qlik chart is a nebula.js bundle that declares the properties a
new instance needs. A chart created without them evaluates perfectly well and
renders blank -- a bar chart with no dimensionAxis/measureAxis has nothing to
lay out -- so `qlik_add_chart` merges these in at creation time.

Run after upgrading Qlik Sense Desktop:

    python extract_viz_defaults.py

It reads the bundles under the client's qmfe/@nebula.js directory and rewrites
viz_defaults.json next to this script.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from jsobj import JsReader  # noqa: E402

DEFAULT_CLIENT = os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "Programs", "Qlik", "Sense",
    "Client", "qmfe", "@nebula.js")

BUNDLES = {
    "barchart": "sn-bar-chart",
    "linechart": "sn-line-chart",
    "combochart": "sn-combo-chart",
    "piechart": "sn-pie-chart",
    "scatterplot": "sn-scatter-plot",
    "treemap": "sn-treemap",
    "kpi": "sn-kpi",
    "table": "sn-table",
    "pivot-table": "sn-pivot-table",
    "gauge": "sn-gauge",
    "filterpane": "sn-filter-pane",
    "boxplot": "sn-boxplot",
    "histogram": "sn-histogram",
    "waterfallchart": "sn-waterfall",
    "bulletchart": "sn-bullet-chart",
    "distributionplot": "sn-distributionplot",
    "funnelchart": "sn-funnel-chart",
    "sankeychart": "sn-sankey-chart",
    "text": "sn-text",
}

# Supplied per chart by the caller; a default must never overwrite these.
OWNED = {"qInfo", "qMetaDef", "qListObjectDef", "title",
         "subtitle", "footnote", "visualization", "type", "qChildListDef"}

# Inside qHyperCubeDef only the column lists belong to the caller. The scalars
# there are viz-specific and load-bearing: a treemap needs qMode "K" plus
# qMaxStackedCells/qIndentMode, a pivot table needs "P". Dropping the whole
# qHyperCubeDef as "caller-owned" is what left both rendering blank.
CUBE_OWNED = {"qDimensions", "qMeasures", "qInterColumnSortOrder",
              "qColumnOrder", "columnOrder", "columnWidths"}


def strip_nulls(value):
    """JS `void 0` means the key is absent, not present-and-null.

    Sending null through matters: tooltip.data.qHyperCubeDef = null is parsed
    by the engine as a hypercube definition and fails with 'JSON parse error'.
    """
    if isinstance(value, dict):
        return {k: strip_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [strip_nulls(v) for v in value if v is not None]
    # 3e3 parses as a float; the engine wants an int for counts like
    # qMaxStackedCells, so normalise whole floats back to integers.
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def enclosing_brace(src, idx):
    depth, i = 0, idx
    while i >= 0:
        if src[i] == "}":
            depth += 1
        elif src[i] == "{":
            if depth == 0:
                return i
            depth -= 1
        i -= 1
    return None


def candidate_offsets(src):
    """Two ways in: an explicit `properties:{`, or the object wrapped around a
    qInitialDataFetch / qListObjectDef. Some bundles only offer the latter."""
    for m in re.finditer(r"properties\s*:\s*\{", src):
        yield src.index("{", m.start())
    for m in re.finditer(r"q(?:InitialDataFetch|ListObjectDef)\s*:", src):
        inner = enclosing_brace(src, m.start())
        if inner is None:
            continue
        outer = enclosing_brace(src, inner - 1)
        if outer is not None:
            yield outer


def extract(src):
    best, seen = None, set()
    for idx in candidate_offsets(src):
        if idx in seen:
            continue
        seen.add(idx)
        try:
            obj = JsReader(src, idx).object()
        except Exception:  # noqa: BLE001 - minified code, skip what won't parse
            continue
        if not isinstance(obj, dict) or "showTitles" not in obj:
            continue
        if not any(k in obj for k in ("qHyperCubeDef", "qListObjectDef",
                                      "dimensionAxis", "color", "legend",
                                      "totals", "showDetails")):
            continue
        if best is None or len(obj) > len(best):
            best = obj
    if not best:
        return None
    cleaned = strip_nulls(best)
    for key in list(cleaned):
        if key in OWNED:
            cleaned.pop(key)
    cube = cleaned.get("qHyperCubeDef")
    if isinstance(cube, dict):
        for key in list(cube):
            if key in CUBE_OWNED:
                cube.pop(key)
        if not cube:
            cleaned.pop("qHyperCubeDef")
    return cleaned


def bundle_source(root, name):
    base = os.path.join(root, name)
    if not os.path.isdir(base):
        return None
    for version in sorted(os.listdir(base), reverse=True):
        path = os.path.join(base, version, "dist", name + ".js")
        if os.path.isfile(path):
            return io.open(path, encoding="utf-8", errors="replace").read()
    return None


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CLIENT
    if not os.path.isdir(root):
        raise SystemExit("client bundles not found: " + root)

    out = {}
    for viz, bundle in sorted(BUNDLES.items()):
        src = bundle_source(root, bundle)
        if src is None:
            print("  {0:18s} bundle not installed".format(viz))
            continue
        props = extract(src)
        if not props:
            print("  {0:18s} no property definition found".format(viz))
            continue
        out[viz] = props
        print("  {0:18s} {1:2} keys".format(viz, len(props)))

    target = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "viz_defaults.json")
    io.open(target, "w", encoding="utf-8").write(json.dumps(out, indent=2))
    print("\nwrote {0} visualisation(s) to {1}".format(len(out), target))


if __name__ == "__main__":
    main()
