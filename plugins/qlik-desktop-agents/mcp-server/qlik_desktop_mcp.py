"""MCP server exposing Qlik Sense Desktop's Engine JSON API to Claude Code.

Tool names deliberately mirror the Qlik Cloud MCP connector's (qlik_search,
qlik_describe_app, ...) so agent instructions written against Cloud largely
carry over. Tools with no Cloud equivalent -- the script, data model and
reload family -- are marked "Desktop only" in their descriptions.

Stdio JSON-RPC, stdlib only. Run directly for a self-test:
    python qlik_desktop_mcp.py --selftest
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import EngineError, reset_shared_session, shared_session  # noqa: E402

SERVER_NAME = "qlik-desktop"
SERVER_VERSION = "1.0.0"
FALLBACK_PROTOCOL = "2025-06-18"

LOG_DIR = os.path.join(
    os.path.expanduser("~"), "Documents", "Qlik", "Sense", "Log"
)

# Themes are extensions: a folder with a .qext of "type": "theme".
EXTENSION_DIRS = [
    os.path.join("C:\\", "Qlik", "Sense", "Extensions"),
    os.path.join(os.path.expanduser("~"), "Documents", "Qlik", "Sense", "Extensions"),
]
BUILTIN_THEMES_FILE = os.path.join(
    os.path.expanduser("~"), "AppData", "Local", "Programs", "Qlik", "Sense",
    "Client", "assets", "external", "sense-themes-default", "default-themes.json")

# Reload can legitimately run for many minutes; the default socket timeout is
# sized for interactive calls, so these get their own budget.
RELOAD_TIMEOUT = float(os.environ.get("QLIK_DESKTOP_RELOAD_TIMEOUT", "1800"))

# The Sense sheet grid. Objects are positioned as percentages of these.
SHEET_COLUMNS = 24
SHEET_ROWS = 12

# Each native visualisation declares the properties it needs for a new
# instance. A chart created without them evaluates fine but renders blank --
# a bar chart with no dimensionAxis/measureAxis has nothing to lay out.
# Harvested from the installed client's nebula bundles; regenerate with
# extract_viz_defaults.py after a Qlik upgrade.
_VIZ_DEFAULTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "viz_defaults.json")
try:
    with open(_VIZ_DEFAULTS_PATH, encoding="utf-8") as _fh:
        VIZ_DEFAULTS = json.load(_fh)
except (OSError, ValueError):
    VIZ_DEFAULTS = {}

# Content the caller supplies; never overwritten by a default.
_OWNED_PROPS = {"qInfo", "qMetaDef", "qHyperCubeDef", "qListObjectDef", "title",
                "subtitle", "footnote", "visualization", "type", "qChildListDef"}


def _apply_viz_defaults(props, viz_type):
    """Merge in the visualisation's declared defaults, without clobbering
    anything the caller set.

    The qHyperCubeDef scalars matter as much as the top-level properties: a
    treemap needs qMode "K" with qMaxStackedCells/qIndentMode, a pivot table
    needs qMode "P". Left in straight mode both evaluate fine and render
    nothing.
    """
    defaults = VIZ_DEFAULTS.get(viz_type) or {}
    for key, value in defaults.items():
        if key == "qHyperCubeDef":
            cube = props.setdefault("qHyperCubeDef", {})
            for ckey, cvalue in (value or {}).items():
                if ckey in ("qDimensions", "qMeasures") or ckey in cube:
                    continue
                cube[ckey] = json.loads(json.dumps(cvalue))
            continue
        if key in _OWNED_PROPS or key in props:
            continue
        props[key] = json.loads(json.dumps(value))
    return props

TOOLS = []
HANDLERS = {}


def tool(name, description, properties, required=None):
    def decorate(fn):
        TOOLS.append({
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        })
        HANDLERS[name] = fn
        return fn
    return decorate


APP_ARG = {"type": "string", "description": "App title, .qvf file name, or full path."}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

@contextlib.contextmanager
def _session():
    """Hand out the process-wide connection.

    Deliberately does not close on exit: selections and open-doc handles are
    per-connection state, so tearing the socket down between calls would make
    qlik_select_values invisible to qlik_get_current_selections.
    """
    yield shared_session()


def resolve_app(session, app):
    """Match a user-supplied app reference against the local Apps folder.

    Desktop's qDocId is the full .qvf path, which is unwieldy to type, so
    accept a title or file name too and fail loudly on ambiguity rather than
    silently picking one.
    """
    docs = session.doc_list()
    if not docs:
        raise EngineError(
            "No apps found. Qlik Sense Desktop stores apps in its Apps folder "
            "(commonly Documents\\Qlik\\Sense\\Apps, or C:\\Qlik\\Sense\\Apps "
            "if relocated) and this one is empty."
        )
    needle = (app or "").strip().lower()
    if not needle:
        raise EngineError("No app specified.")

    exact, partial = [], []
    for doc in docs:
        doc_id = doc.get("qDocId", "")
        title = doc.get("qTitle", "")
        name = doc.get("qDocName", "")
        base = os.path.basename(doc_id)
        candidates = {
            doc_id.lower(), title.lower(), name.lower(),
            base.lower(), base.lower().replace(".qvf", ""),
        }
        if needle in candidates:
            exact.append(doc)
        elif needle in doc_id.lower() or needle in title.lower():
            partial.append(doc)

    hits = exact or partial
    if not hits:
        listing = ", ".join(d.get("qTitle", "?") for d in docs)
        raise EngineError(
            "No app matching '{0}'. Available: {1}".format(app, listing)
        )
    if len(hits) > 1:
        listing = ", ".join(d.get("qTitle", "?") for d in hits)
        raise EngineError(
            "'{0}' matches more than one app ({1}). Use the exact title or "
            "path.".format(app, listing)
        )
    return hits[0]


def open_app(session, app):
    doc = resolve_app(session, app)
    return doc, session.open_doc(doc["qDocId"])


def _list_layout(session, doc, qtype, def_key, definition):
    payload = {"qInfo": {"qId": qtype + "-" + uuid.uuid4().hex[:8], "qType": qtype}}
    payload[def_key] = definition
    return session.session_object_layout(doc, payload)


def _items(layout, key):
    container = layout.get(key) or {}
    return container.get("qItems", [])


def _ts(value):
    """Engine timestamps are days since 1899-12-30, the OLE automation epoch."""
    if not value:
        return None
    try:
        base = datetime.datetime(1899, 12, 30)
        return (base + datetime.timedelta(days=float(value))).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


def _matrix_values(matrix, column=0):
    out = []
    for row in matrix:
        if column < len(row):
            cell = row[column]
            out.append({
                "value": cell.get("qText"),
                "numeric": cell.get("qNum") if cell.get("qIsNumeric") else None,
                "state": cell.get("qState"),
            })
    return out


def _lib_or_inline_dimension(item):
    if isinstance(item, dict) and item.get("library_id"):
        return {"qLibraryId": item["library_id"], "qType": "dimension",
                "qNullSuppression": True}
    if isinstance(item, dict):
        field = item.get("field")
        label = item.get("label", field)
    else:
        field, label = item, item
    return {
        "qDef": {"qFieldDefs": [field], "qFieldLabels": [label], "qGrouping": "N"},
        "qNullSuppression": True,
    }


def _lib_or_inline_measure(item):
    if isinstance(item, dict) and item.get("library_id"):
        return {"qLibraryId": item["library_id"], "qType": "measure"}
    if isinstance(item, dict):
        expr = item.get("expression")
        label = item.get("label", expr)
        fmt = item.get("format")
    else:
        expr, label, fmt = item, item, None
    definition = {"qDef": expr, "qLabel": label}
    if fmt:
        definition["qNumFormat"] = {"qType": "F", "qFmt": fmt, "qDec": ".", "qThou": ","}
    return {"qDef": definition}


def _number_format(fmt):
    if not fmt:
        return None
    return {"qType": "F", "qFmt": fmt, "qDec": ".", "qThou": ",", "qnDec": 2,
            "qUseThou": 1}


def _add_cell(session, doc, sheet_handle, object_id, obj_type, col, row,
              colspan, rowspan):
    """Place an object on the sheet grid. Qlik stores placement on the sheet,
    not the object, so creating a child is only half the job -- without this
    the chart exists but never renders.

    `bounds` is not optional. col/row/colspan/rowspan is the legacy grid; the
    current Sense client positions objects from `bounds` (percentages of the
    sheet), and a cell without it has no position, so the object is invisible
    even though the engine evaluates its hypercube perfectly well.
    """
    props = session.rpc("GetProperties", sheet_handle).get("qProp", {})
    cells = props.get("cells") or []
    columns = props.get("columns") or SHEET_COLUMNS
    rows = props.get("rows") or SHEET_ROWS
    cells.append({
        "name": object_id,
        "type": obj_type,
        "col": col,
        "row": row,
        "colspan": colspan,
        "rowspan": rowspan,
        "bounds": {
            "x": col / float(columns) * 100,
            "y": row / float(rows) * 100,
            "width": colspan / float(columns) * 100,
            "height": rowspan / float(rows) * 100,
        },
    })
    props["cells"] = cells
    session.rpc("SetProperties", sheet_handle, [props])
    session.save(doc)


def _resolve_sheet(session, doc, sheet):
    """Accept a sheet id or title, refusing ambiguous titles."""
    layout = _list_layout(session, doc, "SheetList", "qAppObjectListDef", {
        "qType": "sheet",
        "qData": {"title": "/qMetaDef/title", "rank": "/rank"},
    })
    needle = (sheet or "").strip().lower()
    hits = []
    for item in _items(layout, "qAppObjectList"):
        info = item.get("qInfo", {})
        title = (item.get("qData", {}) or {}).get("title", "") or \
            item.get("qMeta", {}).get("title", "")
        if info.get("qId", "").lower() == needle:
            return info.get("qId"), title  # an id is unambiguous by definition
        if (title or "").lower() == needle:
            hits.append((info.get("qId"), title))
    if not hits:
        raise EngineError("No sheet matching '{0}' in this app.".format(sheet))
    if len(hits) > 1:
        raise EngineError(
            "'{0}' matches {1} sheets ({2}). Use the sheet id.".format(
                sheet, len(hits), ", ".join(h[0] for h in hits)))
    return hits[0]


# --------------------------------------------------------------------------
# read tools
# --------------------------------------------------------------------------

@tool("qlik_search", "List Qlik Sense Desktop apps in the local Apps folder, "
      "optionally filtered by name. Desktop has no tenant, spaces or owners.",
      {"query": {"type": "string", "description": "Optional name fragment."}})
def t_search(args):
    with _session() as s:
        needle = (args.get("query") or "").strip().lower()
        out = []
        for doc in s.doc_list():
            title = doc.get("qTitle", "")
            doc_id = doc.get("qDocId", "")
            if needle and needle not in title.lower() and needle not in doc_id.lower():
                continue
            out.append({
                "title": title,
                "app_id": doc_id,
                "file": os.path.basename(doc_id),
                "size_bytes": doc.get("qFileSize"),
                "last_reload": doc.get("qLastReloadTime") or None,
                "modified": doc.get("qFileTime") and _ts(doc.get("qFileTime")),
            })
        return {"apps": out, "count": len(out)}


@tool("qlik_describe_app", "App overview: title, path, size, last reload, and "
      "counts of sheets, master items, fields, variables and script tabs.",
      {"app": APP_ARG}, ["app"])
def t_describe_app(args):
    with _session() as s:
        doc, handle = open_app(s, args["app"])
        layout = s.rpc("GetAppLayout", handle).get("qLayout", {})
        sheets = _items(_list_layout(s, handle, "SheetList", "qAppObjectListDef",
                                     {"qType": "sheet", "qData": {"title": "/qMetaDef/title"}}),
                        "qAppObjectList")
        measures = _items(_list_layout(s, handle, "MeasureList", "qMeasureListDef",
                                       {"qType": "measure", "qData": {"title": "/qMetaDef/title"}}),
                          "qMeasureList")
        dimensions = _items(_list_layout(s, handle, "DimensionList", "qDimensionListDef",
                                         {"qType": "dimension", "qData": {"title": "/qMetaDef/title"}}),
                            "qDimensionList")
        fields = _items(_list_layout(s, handle, "FieldList", "qFieldListDef",
                                     {"qShowSystem": False, "qShowHidden": False,
                                      "qShowSrcTables": True}),
                        "qFieldList")
        script = s.rpc("GetScript", handle).get("qScript", "")
        tabs = [line for line in script.splitlines() if line.strip().startswith("///$tab")]
        app_props = s.rpc("GetAppProperties", handle).get("qProp", {})
        return {
            "title": doc.get("qTitle"),
            "theme": app_props.get("theme") or "(none - client default)",
            "app_id": doc.get("qDocId"),
            "file_size_bytes": doc.get("qFileSize"),
            "last_reload": layout.get("qLastReloadTime"),
            "has_data": not layout.get("qIsOpenedWithoutData", False),
            "sheets": len(sheets),
            "master_measures": len(measures),
            "master_dimensions": len(dimensions),
            "fields": len(fields),
            "script_lines": len(script.splitlines()),
            "script_tabs": [t.replace("///$tab", "").strip() for t in tabs],
            "deployment": "Qlik Sense Desktop (local, single user, no tenant)",
        }


@tool("qlik_list_sheets", "Sheets in the app with id, title, description, rank "
      "and object count.", {"app": APP_ARG}, ["app"])
def t_list_sheets(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        layout = _list_layout(s, handle, "SheetList", "qAppObjectListDef", {
            "qType": "sheet",
            "qData": {"title": "/qMetaDef/title", "description": "/qMetaDef/description",
                      "cells": "/cells", "rank": "/rank"},
        })
        out = []
        for item in _items(layout, "qAppObjectList"):
            data = item.get("qData", {}) or {}
            meta = item.get("qMeta", {}) or {}
            cells = data.get("cells") or []
            out.append({
                "sheet_id": item.get("qInfo", {}).get("qId"),
                "title": data.get("title") or meta.get("title"),
                "description": data.get("description") or meta.get("description"),
                "rank": data.get("rank"),
                "object_count": len(cells),
            })
        return {"sheets": out, "count": len(out)}


@tool("qlik_get_sheet_details", "Objects on one sheet with type, id and grid "
      "position (24-column grid).", {"app": APP_ARG, "sheet": {"type": "string"}},
      ["app", "sheet"])
def t_sheet_details(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        sheet_id, title = _resolve_sheet(s, handle, args["sheet"])
        sheet_handle = s.object_handle(handle, sheet_id)
        props = s.rpc("GetProperties", sheet_handle).get("qProp", {})
        cells = []
        for cell in props.get("cells", []) or []:
            cells.append({
                "object_id": cell.get("name"),
                "type": cell.get("type"),
                "col": cell.get("col"), "row": cell.get("row"),
                "colspan": cell.get("colspan"), "rowspan": cell.get("rowspan"),
            })
        return {
            "sheet_id": sheet_id,
            "title": title,
            "columns": props.get("columns", 24),
            "rows": props.get("rows", 12),
            "objects": cells,
        }


@tool("qlik_list_measures", "Master measures with expression, label, format, "
      "tags and description.", {"app": APP_ARG}, ["app"])
def t_list_measures(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        layout = _list_layout(s, handle, "MeasureList", "qMeasureListDef", {
            "qType": "measure",
            "qData": {"title": "/qMetaDef/title", "description": "/qMetaDef/description",
                      "tags": "/qMetaDef/tags", "expression": "/qMeasure/qDef",
                      "label": "/qMeasure/qLabel",
                      "labelExpression": "/qMeasure/qLabelExpression"},
        })
        out = []
        for item in _items(layout, "qMeasureList"):
            data = item.get("qData", {}) or {}
            meta = item.get("qMeta", {}) or {}
            out.append({
                "measure_id": item.get("qInfo", {}).get("qId"),
                "title": data.get("title") or meta.get("title"),
                "expression": data.get("expression"),
                "label": data.get("label"),
                "label_expression": data.get("labelExpression"),
                "description": data.get("description") or meta.get("description"),
                "tags": data.get("tags") or meta.get("tags") or [],
            })
        return {"measures": out, "count": len(out)}


@tool("qlik_list_dimensions", "Master dimensions with field definitions, "
      "grouping (single or drill-down), labels and tags.",
      {"app": APP_ARG}, ["app"])
def t_list_dimensions(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        layout = _list_layout(s, handle, "DimensionList", "qDimensionListDef", {
            "qType": "dimension",
            "qData": {"title": "/qMetaDef/title", "description": "/qMetaDef/description",
                      "tags": "/qMetaDef/tags", "grouping": "/qDim/qGrouping"},
        })
        out = []
        for item in _items(layout, "qDimensionList"):
            data = item.get("qData", {}) or {}
            meta = item.get("qMeta", {}) or {}
            dimension_id = item.get("qInfo", {}).get("qId")
            # qFieldDefs is an array, and array-valued qData paths come back
            # empty from a list object -- fetch the real properties instead.
            fields, labels = [], []
            try:
                obj = s.rpc("GetDimension", handle, [dimension_id])["qReturn"]["qHandle"]
                dim = (s.rpc("GetProperties", obj).get("qProp", {}) or {}).get("qDim", {})
                fields = dim.get("qFieldDefs") or []
                labels = dim.get("qFieldLabels") or []
            except EngineError:
                pass
            out.append({
                "dimension_id": dimension_id,
                "title": data.get("title") or meta.get("title"),
                "fields": fields,
                "labels": labels,
                "grouping": data.get("grouping"),
                "is_drilldown": (data.get("grouping") == "H") or len(fields) > 1,
                "description": data.get("description") or meta.get("description"),
                "tags": data.get("tags") or meta.get("tags") or [],
            })
        return {"dimensions": out, "count": len(out)}


@tool("qlik_get_fields", "Fields in the loaded data model with source tables, "
      "cardinality and tags.",
      {"app": APP_ARG,
       "include_system": {"type": "boolean", "description": "Default false."}},
      ["app"])
def t_get_fields(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        layout = _list_layout(s, handle, "FieldList", "qFieldListDef", {
            "qShowSystem": bool(args.get("include_system", False)),
            "qShowHidden": False,
            "qShowDerivedFields": True,
            "qShowSemantic": True,
            "qShowSrcTables": True,
        })
        out = []
        for item in _items(layout, "qFieldList"):
            out.append({
                "name": item.get("qName"),
                "tables": item.get("qSrcTables") or [],
                "cardinality": item.get("qCardinal"),
                "total_count": item.get("qTotalCount"),
                "tags": item.get("qTags") or [],
                "is_system": item.get("qIsSystem", False),
                "is_hidden": item.get("qIsHidden", False),
            })
        return {"fields": out, "count": len(out)}


@tool("qlik_get_field_values", "Distinct values of a field with selection state.",
      {"app": APP_ARG, "field": {"type": "string"},
       "limit": {"type": "integer", "description": "Default 100, max 1000."}},
      ["app", "field"])
def t_field_values(args):
    limit = min(int(args.get("limit", 100) or 100), 1000)
    with _session() as s:
        _, handle = open_app(s, args["app"])
        layout = _list_layout(s, handle, "ListObject", "qListObjectDef", {
            "qDef": {"qFieldDefs": [args["field"]],
                     "qSortCriterias": [{"qSortByState": 1, "qSortByAscii": 1}]},
            "qInitialDataFetch": [{"qTop": 0, "qLeft": 0, "qHeight": limit, "qWidth": 1}],
        })
        lo = layout.get("qListObject", {})
        pages = lo.get("qDataPages") or [{}]
        dim = lo.get("qDimensionInfo", {})
        return {
            "field": args["field"],
            "cardinality": dim.get("qCardinal"),
            "returned": len(pages[0].get("qMatrix", [])),
            "values": _matrix_values(pages[0].get("qMatrix", [])),
        }


@tool("qlik_search_field_values", "Search within a field's values for a term.",
      {"app": APP_ARG, "field": {"type": "string"}, "term": {"type": "string"},
       "limit": {"type": "integer"}},
      ["app", "field", "term"])
def t_search_field_values(args):
    limit = min(int(args.get("limit", 50) or 50), 500)
    with _session() as s:
        _, handle = open_app(s, args["app"])
        definition = {
            "qInfo": {"qId": "search-" + uuid.uuid4().hex[:8], "qType": "ListObject"},
            "qListObjectDef": {
                "qDef": {"qFieldDefs": [args["field"]]},
                "qInitialDataFetch": [{"qTop": 0, "qLeft": 0, "qHeight": limit, "qWidth": 1}],
            },
        }
        created = s.rpc("CreateSessionObject", handle, [definition])
        obj = created["qReturn"]["qHandle"]
        try:
            s.rpc("SearchListObjectFor", obj, ["/qListObjectDef", args["term"]])
            layout = s.rpc("GetLayout", obj).get("qLayout", {})
            pages = layout.get("qListObject", {}).get("qDataPages") or [{}]
            return {
                "field": args["field"],
                "term": args["term"],
                "matches": _matrix_values(pages[0].get("qMatrix", [])),
            }
        finally:
            try:
                s.rpc("AbortListObjectSearch", obj, ["/qListObjectDef"])
                s.rpc("DestroySessionObject", handle, [definition["qInfo"]["qId"]])
            except EngineError:
                pass


@tool("qlik_get_chart_info", "Full properties of one chart: type, dimensions, "
      "measures, expressions and title.",
      {"app": APP_ARG, "object_id": {"type": "string"}}, ["app", "object_id"])
def t_chart_info(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        obj = s.object_handle(handle, args["object_id"])
        props = s.rpc("GetProperties", obj).get("qProp", {})
        cube = props.get("qHyperCubeDef", {}) or {}
        dims = []
        for d in cube.get("qDimensions", []) or []:
            dims.append({
                "library_id": d.get("qLibraryId"),
                "fields": (d.get("qDef", {}) or {}).get("qFieldDefs"),
                "labels": (d.get("qDef", {}) or {}).get("qFieldLabels"),
            })
        measures = []
        for m in cube.get("qMeasures", []) or []:
            definition = m.get("qDef", {}) or {}
            measures.append({
                "library_id": m.get("qLibraryId"),
                "expression": definition.get("qDef"),
                "label": definition.get("qLabel"),
            })
        return {
            "object_id": args["object_id"],
            "type": props.get("visualization") or props.get("qInfo", {}).get("qType"),
            "title": props.get("title"),
            "dimensions": dims,
            "measures": measures,
        }


@tool("qlik_get_chart_data", "Evaluated data behind a chart, honouring current "
      "selections.",
      {"app": APP_ARG, "object_id": {"type": "string"},
       "rows": {"type": "integer", "description": "Default 50, max 500."}},
      ["app", "object_id"])
def t_chart_data(args):
    rows = min(int(args.get("rows", 50) or 50), 500)
    with _session() as s:
        _, handle = open_app(s, args["app"])
        obj = s.object_handle(handle, args["object_id"])
        layout = s.rpc("GetLayout", obj).get("qLayout", {})
        cube = layout.get("qHyperCube", {}) or {}
        width = cube.get("qSize", {}).get("qcx", 0)
        height = cube.get("qSize", {}).get("qcy", 0)
        if not width:
            return {"object_id": args["object_id"],
                    "note": "Object has no hypercube (it may be a text, container "
                            "or extension object).",
                    "type": layout.get("visualization")}
        pages = s.rpc("GetHyperCubeData", obj, [
            "/qHyperCubeDef",
            [{"qTop": 0, "qLeft": 0, "qHeight": min(rows, height), "qWidth": width}],
        ]).get("qDataPages", [])
        headers = [d.get("qFallbackTitle") for d in cube.get("qDimensionInfo", [])] + \
                  [m.get("qFallbackTitle") for m in cube.get("qMeasureInfo", [])]
        data = []
        for row in (pages[0].get("qMatrix", []) if pages else []):
            data.append([c.get("qText") for c in row])
        return {
            "object_id": args["object_id"],
            "total_rows": height,
            "returned_rows": len(data),
            "columns": headers,
            "rows": data,
        }


@tool("qlik_list_bookmarks", "Bookmarks in the app.", {"app": APP_ARG}, ["app"])
def t_list_bookmarks(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        layout = _list_layout(s, handle, "BookmarkList", "qBookmarkListDef", {
            "qType": "bookmark",
            "qData": {"title": "/qMetaDef/title", "description": "/qMetaDef/description"},
        })
        out = []
        for item in _items(layout, "qBookmarkList"):
            data = item.get("qData", {}) or {}
            meta = item.get("qMeta", {}) or {}
            out.append({
                "bookmark_id": item.get("qInfo", {}).get("qId"),
                "title": data.get("title") or meta.get("title"),
                "description": data.get("description") or meta.get("description"),
            })
        return {"bookmarks": out, "count": len(out)}


@tool("qlik_get_current_selections", "Current selection state of the app session.",
      {"app": APP_ARG}, ["app"])
def t_current_selections(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        layout = _list_layout(s, handle, "CurrentSelection", "qSelectionObjectDef", {})
        selections = (layout.get("qSelectionObject", {}) or {}).get("qSelections", [])
        out = []
        for sel in selections:
            out.append({
                "field": sel.get("qField"),
                "selected_count": sel.get("qSelectedCount"),
                "total": sel.get("qTotal"),
                "selected": sel.get("qSelected"),
            })
        return {"selections": out, "is_clean": not out}


@tool("qlik_get_variables", "Script and front-end variables with definitions "
      "(house convention: vL./vG./vD./vP./vT./vU. prefixes).",
      {"app": APP_ARG}, ["app"])
def t_variables(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        layout = _list_layout(s, handle, "VariableList", "qVariableListDef", {
            "qType": "variable", "qShowReserved": False, "qShowConfig": False,
            "qData": {"tags": "/tags"},
        })
        out = []
        for item in _items(layout, "qVariableList"):
            out.append({
                "name": item.get("qName"),
                "definition": item.get("qDefinition"),
                "is_script_created": item.get("qIsScriptCreated", False),
                "is_reserved": item.get("qIsReserved", False),
            })
        return {"variables": out, "count": len(out)}


# --- Desktop-only read tools ----------------------------------------------

@tool("qlik_get_script", "Desktop only. The app's full load script. No Qlik "
      "Cloud MCP equivalent exists.", {"app": APP_ARG}, ["app"])
def t_get_script(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        script = s.rpc("GetScript", handle).get("qScript", "")
        tabs = []
        current = None
        for index, line in enumerate(script.splitlines(), start=1):
            if line.strip().startswith("///$tab"):
                current = {"name": line.replace("///$tab", "").strip(),
                           "start_line": index}
                tabs.append(current)
        return {"script": script, "line_count": len(script.splitlines()),
                "tabs": tabs}


@tool("qlik_get_tables_and_keys", "Desktop only. The loaded data model: tables, "
      "row counts, fields, key fields and any synthetic tables. Use this to "
      "verify synthetic keys and circular references against the real model "
      "rather than inferring them from script.",
      {"app": APP_ARG}, ["app"])
def t_tables_and_keys(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        result = s.rpc("GetTablesAndKeys", handle, [
            {"qcx": 1000, "qcy": 1000}, {"qcx": 0, "qcy": 0}, 30, True, False,
        ])
        tables = []
        synthetic_tables = []
        synthetic_keys = []
        for table in result.get("qtr", []) or []:
            name = table.get("qName")
            tags = table.get("qTableTags") or []
            fields = []
            for f in table.get("qFields", []) or []:
                field_tags = f.get("qTags") or []
                is_synthetic = "$synthetic" in field_tags
                fields.append({
                    "name": f.get("qName"),
                    "distinct_values": f.get("qnTotalDistinctValues"),
                    "rows": f.get("qnRows"),
                    "is_key": f.get("qKeyType") not in (None, "NOT_KEY"),
                    "key_type": f.get("qKeyType"),
                    "is_synthetic": is_synthetic,
                    # For a $Syn field this names the real culprit: the set of
                    # fields the two tables share.
                    "composed_of": f.get("qOriginalFields") or [],
                    "information_density": f.get("qInformationDensity"),
                    "subset_ratio": f.get("qSubsetRatio"),
                    "tags": field_tags,
                })
                if is_synthetic:
                    synthetic_keys.append({
                        "key_field": f.get("qName"),
                        "shared_fields": f.get("qOriginalFields") or [],
                        "in_table": name,
                    })
            entry = {
                "name": name,
                "rows": table.get("qNoOfRows"),
                "fields": fields,
                "is_synthetic_table": bool(name and name.startswith("$Syn")),
                "is_loose": "$loose" in tags,
                "tags": tags,
            }
            tables.append(entry)
            if entry["is_synthetic_table"]:
                synthetic_tables.append(name)
        keys = []
        for key in result.get("qk", []) or []:
            keys.append({
                "key_fields": key.get("qKeyFields") or [],
                "tables": key.get("qTables") or [],
            })
        return {
            "tables": tables,
            "table_count": len(tables),
            "keys": keys,
            "synthetic_tables": synthetic_tables,
            "synthetic_keys": synthetic_keys,
            "has_synthetic_keys": bool(synthetic_keys or synthetic_tables),
            "loose_tables": [t["name"] for t in tables if t["is_loose"]],
        }


@tool("qlik_check_script_syntax", "Desktop only. Validate the app's current "
      "script and return errors with tab and line numbers. Set the script "
      "first with qlik_set_script to check a new one.",
      {"app": APP_ARG}, ["app"])
def t_check_syntax(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        errors = s.rpc("CheckScriptSyntax", handle).get("qErrors", []) or []
        return {
            "valid": not errors,
            "errors": [{
                "tab_index": e.get("qTabIx"),
                "line_in_tab": e.get("qLineInTab"),
                "column": e.get("qColInLine"),
                "error_length": e.get("qErrLen"),
                "secondary": e.get("qSecondaryFailure"),
            } for e in errors],
        }


@tool("qlik_list_connections", "Desktop only. Data connections defined in the "
      "app (lib:// folder and database connections).", {"app": APP_ARG}, ["app"])
def t_connections(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        conns = s.rpc("GetConnections", handle).get("qConnections", []) or []
        return {"connections": [{
            "id": c.get("qId"),
            "name": c.get("qName"),
            "type": c.get("qType"),
            "connection_string": c.get("qConnectionString"),
        } for c in conns], "count": len(conns)}


@tool("qlik_get_engine_info", "Desktop only. Engine version, product version "
      "and app count -- the Desktop equivalent of a tenant health check.", {})
def t_engine_info(args):
    with _session() as s:
        version = s.engine_version()
        docs = s.doc_list()
        return {
            "engine_version": version.get("qComponentVersion"),
            "product_version": s.product_version(),
            "endpoint": "ws://{0}:{1}/app/engineData".format(
                os.environ.get("QLIK_DESKTOP_HOST", "127.0.0.1"),
                os.environ.get("QLIK_DESKTOP_PORT", "4848")),
            "app_count": len(docs),
            "log_dir": LOG_DIR if os.path.isdir(LOG_DIR) else None,
            "deployment": "Qlik Sense Desktop",
        }


@tool("qlik_list_themes", "Desktop only. Themes available to the client: the "
      "four Qlik built-ins plus any custom theme installed in an Extensions "
      "folder, each with whether the engine has actually registered it. A "
      "custom theme dropped in after startup serves over HTTP but is NOT "
      "registered until Qlik Sense Desktop restarts.",
      {"app": {"type": "string",
               "description": "Optional. Also report which theme this app uses."}})
def t_list_themes(args):
    builtin = []
    try:
        with open(BUILTIN_THEMES_FILE, encoding="utf-8") as handle:
            for entry in (json.load(handle).get("default") or []):
                builtin.append({"id": entry.get("id"), "name": entry.get("name"),
                                "source": "built-in", "registered": True})
    except (OSError, ValueError):
        pass

    custom = []
    for root in EXTENSION_DIRS:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            folder = os.path.join(root, name)
            if not os.path.isdir(folder):
                continue
            qext = os.path.join(folder, name + ".qext")
            if not os.path.isfile(qext):
                continue
            try:
                with open(qext, encoding="utf-8") as handle:
                    meta = json.load(handle)
            except (OSError, ValueError):
                continue
            if (meta.get("type") or "").lower() != "theme":
                continue
            custom.append({
                "id": name,
                "name": meta.get("name", name),
                "source": folder,
                "has_theme_json": os.path.isfile(os.path.join(folder, "theme.json")),
                "registered": None,
            })

    # The Web Extension Service registry is what the client actually reads.
    registry, registry_error = [], None
    try:
        import urllib.request
        url = "http://{0}:{1}/api/wes/v1/extensions".format(
            os.environ.get("QLIK_DESKTOP_HOST", "127.0.0.1"),
            os.environ.get("QLIK_DESKTOP_PORT", "4848"))
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        registry = [e.get("id") or (e.get("attributes") or {}).get("name")
                    for e in (payload.get("data") or [])]
    except Exception as exc:  # noqa: BLE001 - registry is best-effort
        registry_error = str(exc)[:160]

    for entry in custom:
        entry["registered"] = entry["id"] in registry if registry_error is None else None

    result = {
        "builtin_themes": builtin,
        "custom_themes": custom,
        "extension_registry": registry,
        "registry_error": registry_error,
        "extension_dirs_searched": [d for d in EXTENSION_DIRS if os.path.isdir(d)],
    }
    unregistered = [c["id"] for c in custom if c["registered"] is False]
    if unregistered:
        result["warning"] = (
            "Installed but NOT registered: {0}. Qlik Sense Desktop builds its "
            "extension registry when the service starts, so restart Desktop "
            "before the theme will apply.".format(", ".join(unregistered)))

    if args.get("app"):
        with _session() as s:
            doc, handle = open_app(s, args["app"])
            props = s.rpc("GetAppProperties", handle).get("qProp", {})
            result["app"] = doc.get("qTitle")
            result["app_theme"] = props.get("theme") or "(none - client default)"
    return result


@tool("qlik_set_app_theme", "Desktop only. Set the app's theme to a built-in id "
      "(sense, card, breeze, horizon) or an installed custom theme's folder "
      "name. Saves the app. Pass an unknown id only with confirm=true.",
      {"app": APP_ARG,
       "theme": {"type": "string",
                 "description": "Theme id, or empty string to clear back to the default."},
       "confirm": {"type": "boolean",
                   "description": "Set the theme even if it is not installed or not registered."}},
      ["app", "theme"])
def t_set_app_theme(args):
    theme = (args.get("theme") or "").strip()
    known = t_list_themes({})
    ids = [t["id"] for t in known["builtin_themes"]] + \
          [t["id"] for t in known["custom_themes"]]
    notes = []

    if theme and theme not in ids and not args.get("confirm"):
        raise EngineError(
            "Unknown theme '{0}'. Installed: {1}. Pass confirm=true to set it "
            "anyway.".format(theme, ", ".join(ids) or "(none)"))

    for entry in known["custom_themes"]:
        if entry["id"] == theme:
            if not entry["has_theme_json"]:
                notes.append("Theme folder has no theme.json - the client will "
                             "fall back to the default.")
            if entry["registered"] is False:
                notes.append("Theme is installed but NOT registered; restart "
                             "Qlik Sense Desktop before it will apply.")

    with _session() as s:
        doc, handle = open_app(s, args["app"])
        props = s.rpc("GetAppProperties", handle).get("qProp", {})
        previous = props.get("theme")
        if theme:
            props["theme"] = theme
        else:
            props.pop("theme", None)
        s.rpc("SetAppProperties", handle, [props])
        s.save(handle)
        confirmed = s.rpc("GetAppProperties", handle).get("qProp", {}).get("theme")

    return {
        "app": doc.get("qTitle"),
        "previous_theme": previous or "(none)",
        "theme": confirmed or "(none - client default)",
        "applied": (confirmed or "") == theme,
        "notes": notes,
        "next": "Refresh the app in the browser (F5) - the client caches theme "
                "properties.",
    }


@tool("qlik_get_reload_log", "Desktop only. Tail the most recent Qlik Sense "
      "Desktop log file for reload diagnostics.",
      {"lines": {"type": "integer", "description": "Default 80, max 500."},
       "contains": {"type": "string", "description": "Only return lines containing this."}})
def t_reload_log(args):
    limit = min(int(args.get("lines", 80) or 80), 500)
    if not os.path.isdir(LOG_DIR):
        return {"log_dir": LOG_DIR, "available": False,
                "note": "No Desktop log directory found."}
    files = []
    for name in os.listdir(LOG_DIR):
        path = os.path.join(LOG_DIR, name)
        if os.path.isfile(path) and name.lower().endswith((".log", ".txt")):
            files.append((os.path.getmtime(path), path))
    if not files:
        return {"log_dir": LOG_DIR, "available": False, "note": "No log files."}
    files.sort(reverse=True)
    newest = files[0][1]
    with open(newest, "r", encoding="utf-8", errors="replace") as handle:
        content = handle.readlines()
    needle = args.get("contains")
    if needle:
        content = [line for line in content if needle.lower() in line.lower()]
    return {
        "file": newest,
        "modified": datetime.datetime.fromtimestamp(files[0][0]).isoformat(timespec="seconds"),
        "lines": [line.rstrip("\n") for line in content[-limit:]],
    }


# --------------------------------------------------------------------------
# write tools
# --------------------------------------------------------------------------

@tool("qlik_create_measure", "Create a master measure and save the app.",
      {"app": APP_ARG, "title": {"type": "string"},
       "expression": {"type": "string", "description": "Qlik expression, e.g. Sum(Sales)."},
       "description": {"type": "string"}, "label_expression": {"type": "string"},
       "format": {"type": "string", "description": "Number format, e.g. '#,##0.00'."},
       "tags": {"type": "array", "items": {"type": "string"}}},
      ["app", "title", "expression"])
def t_create_measure(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        measure_id = uuid.uuid4().hex
        definition = {
            "qDef": args["expression"],
            "qLabel": args["title"],
            "qGrouping": "N",
            "qExpressions": [],
            "qLabelExpression": args.get("label_expression", ""),
        }
        fmt = _number_format(args.get("format"))
        if fmt:
            definition["qNumFormat"] = fmt
        props = {
            "qInfo": {"qId": measure_id, "qType": "measure"},
            "qMeasure": definition,
            "qMetaDef": {
                "title": args["title"],
                "description": args.get("description", ""),
                "tags": args.get("tags") or [],
            },
        }
        s.rpc("CreateMeasure", handle, [props])
        s.save(handle)
        return {"created": True, "measure_id": measure_id, "title": args["title"],
                "expression": args["expression"], "app_saved": True}


@tool("qlik_update_measure", "Update an existing master measure and save.",
      {"app": APP_ARG, "measure_id": {"type": "string"},
       "title": {"type": "string"}, "expression": {"type": "string"},
       "description": {"type": "string"}, "format": {"type": "string"},
       "tags": {"type": "array", "items": {"type": "string"}}},
      ["app", "measure_id"])
def t_update_measure(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        obj = s.rpc("GetMeasure", handle, [args["measure_id"]])["qReturn"]["qHandle"]
        props = s.rpc("GetProperties", obj).get("qProp", {})
        if args.get("expression"):
            props.setdefault("qMeasure", {})["qDef"] = args["expression"]
        if args.get("title"):
            props.setdefault("qMeasure", {})["qLabel"] = args["title"]
            props.setdefault("qMetaDef", {})["title"] = args["title"]
        if args.get("description") is not None:
            props.setdefault("qMetaDef", {})["description"] = args["description"]
        if args.get("format"):
            props.setdefault("qMeasure", {})["qNumFormat"] = _number_format(args["format"])
        if args.get("tags") is not None:
            props.setdefault("qMetaDef", {})["tags"] = args["tags"]
        s.rpc("SetProperties", obj, [props])
        s.save(handle)
        return {"updated": True, "measure_id": args["measure_id"], "app_saved": True}


@tool("qlik_delete_measure", "Delete a master measure and save.",
      {"app": APP_ARG, "measure_id": {"type": "string"}}, ["app", "measure_id"])
def t_delete_measure(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        s.rpc("DestroyMeasure", handle, [args["measure_id"]])
        s.save(handle)
        return {"deleted": True, "measure_id": args["measure_id"]}


@tool("qlik_create_dimension", "Create a master dimension (single or drill-down) "
      "and save the app.",
      {"app": APP_ARG, "title": {"type": "string"},
       "fields": {"type": "array", "items": {"type": "string"},
                  "description": "One field for a simple dimension, several for a drill-down."},
       "labels": {"type": "array", "items": {"type": "string"}},
       "description": {"type": "string"},
       "tags": {"type": "array", "items": {"type": "string"}}},
      ["app", "title", "fields"])
def t_create_dimension(args):
    fields = args["fields"]
    with _session() as s:
        _, handle = open_app(s, args["app"])
        dimension_id = uuid.uuid4().hex
        props = {
            "qInfo": {"qId": dimension_id, "qType": "dimension"},
            "qDim": {
                "qGrouping": "H" if len(fields) > 1 else "N",
                "qFieldDefs": fields,
                "qFieldLabels": args.get("labels") or fields,
                "qLabelExpression": "",
                "title": args["title"],
            },
            "qMetaDef": {
                "title": args["title"],
                "description": args.get("description", ""),
                "tags": args.get("tags") or [],
            },
        }
        s.rpc("CreateDimension", handle, [props])
        s.save(handle)
        return {"created": True, "dimension_id": dimension_id,
                "title": args["title"], "fields": fields,
                "grouping": props["qDim"]["qGrouping"], "app_saved": True}


@tool("qlik_delete_dimension", "Delete a master dimension and save.",
      {"app": APP_ARG, "dimension_id": {"type": "string"}}, ["app", "dimension_id"])
def t_delete_dimension(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        s.rpc("DestroyDimension", handle, [args["dimension_id"]])
        s.save(handle)
        return {"deleted": True, "dimension_id": args["dimension_id"]}


@tool("qlik_create_sheet", "Create a sheet on the standard 24-column grid.",
      {"app": APP_ARG, "title": {"type": "string"},
       "description": {"type": "string"}, "rank": {"type": "integer"}},
      ["app", "title"])
def t_create_sheet(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        sheet_id = uuid.uuid4().hex
        props = {
            "qInfo": {"qId": sheet_id, "qType": "sheet"},
            "qMetaDef": {"title": args["title"],
                         "description": args.get("description", "")},
            "rank": args.get("rank", 0),
            "thumbnail": {"qStaticContentUrlDef": {"qUrl": ""}},
            "columns": SHEET_COLUMNS,
            "rows": SHEET_ROWS,
            "cells": [],
            "gridResolution": "small",
            "qChildListDef": {"qData": {"title": "/title"}},
        }
        s.rpc("CreateObject", handle, [props])
        s.save(handle)
        return {"created": True, "sheet_id": sheet_id, "title": args["title"],
                "grid": "24 columns x 12 rows", "app_saved": True}


@tool("qlik_add_chart", "Add a chart to a sheet and place it on the grid. "
      "Dimensions and measures may be inline or reference master items by id.",
      {"app": APP_ARG, "sheet": {"type": "string"},
       "type": {"type": "string",
                "description": "barchart, linechart, combochart, kpi, table, pivot-table, "
                               "scatterplot, piechart, treemap, gauge, distributionplot."},
       "title": {"type": "string"},
       "dimensions": {"type": "array", "items": {"type": "object"},
                      "description": "Each: {field, label} or {library_id}."},
       "measures": {"type": "array", "items": {"type": "object"},
                    "description": "Each: {expression, label, format} or {library_id}."},
       "col": {"type": "integer"}, "row": {"type": "integer"},
       "colspan": {"type": "integer"}, "rowspan": {"type": "integer"}},
      ["app", "sheet", "type"])
def t_add_chart(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        sheet_id, _title = _resolve_sheet(s, handle, args["sheet"])
        sheet_handle = s.object_handle(handle, sheet_id)
        object_id = uuid.uuid4().hex
        chart_type = args["type"]
        props = {
            "qInfo": {"qId": object_id, "qType": chart_type},
            "type": chart_type,
            "visualization": chart_type,
            "title": args.get("title", ""),
            "showTitles": True,
            "subtitle": "",
            "footnote": "",
            "qHyperCubeDef": {
                "qDimensions": [_lib_or_inline_dimension(d) for d in args.get("dimensions") or []],
                "qMeasures": [_lib_or_inline_measure(m) for m in args.get("measures") or []],
                "qSuppressZero": False,
                "qSuppressMissing": True,
                "qInitialDataFetch": [{"qTop": 0, "qLeft": 0, "qHeight": 20, "qWidth": 10}],
            },
        }
        _apply_viz_defaults(props, chart_type)
        s.rpc("CreateChild", sheet_handle, [props])
        _add_cell(s, handle, sheet_handle, object_id, chart_type,
                  args.get("col", 0), args.get("row", 0),
                  args.get("colspan", 12), args.get("rowspan", 6))
        return {"created": True, "object_id": object_id, "type": chart_type,
                "sheet_id": sheet_id, "placed_at": {
                    "col": args.get("col", 0), "row": args.get("row", 0),
                    "colspan": args.get("colspan", 12), "rowspan": args.get("rowspan", 6)},
                "app_saved": True}


@tool("qlik_add_filter", "Add a filter pane with one or more field list boxes.",
      {"app": APP_ARG, "sheet": {"type": "string"},
       "fields": {"type": "array", "items": {"type": "string"}},
       "title": {"type": "string"},
       "col": {"type": "integer"}, "row": {"type": "integer"},
       "colspan": {"type": "integer"}, "rowspan": {"type": "integer"}},
      ["app", "sheet", "fields"])
def t_add_filter(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        sheet_id, _title = _resolve_sheet(s, handle, args["sheet"])
        sheet_handle = s.object_handle(handle, sheet_id)
        pane_id = uuid.uuid4().hex
        pane_props = {
            "qInfo": {"qId": pane_id, "qType": "filterpane"},
            "type": "filterpane",
            "visualization": "filterpane",
            "title": args.get("title", ""),
            "showTitles": bool(args.get("title")),
            "qChildListDef": {"qData": {"title": "/title"}},
        }
        _apply_viz_defaults(pane_props, "filterpane")
        s.rpc("CreateChild", sheet_handle, [pane_props])
        pane_handle = s.object_handle(handle, pane_id)
        children = []
        for field in args["fields"]:
            child_id = uuid.uuid4().hex
            s.rpc("CreateChild", pane_handle, [{
                "qInfo": {"qId": child_id, "qType": "listbox"},
                "type": "listbox",
                "visualization": "listbox",
                "title": field,
                "showTitles": True,
                "qListObjectDef": {
                    "qDef": {"qFieldDefs": [field], "qFieldLabels": [field],
                             "qSortCriterias": [{"qSortByState": 1, "qSortByAscii": 1}]},
                    "qInitialDataFetch": [{"qTop": 0, "qLeft": 0, "qHeight": 100, "qWidth": 1}],
                },
            }])
            children.append({"listbox_id": child_id, "field": field})
        _add_cell(s, handle, sheet_handle, pane_id, "filterpane",
                  args.get("col", 0), args.get("row", 0),
                  args.get("colspan", 4), args.get("rowspan", 12))
        return {"created": True, "filterpane_id": pane_id, "sheet_id": sheet_id,
                "listboxes": children, "app_saved": True}


@tool("qlik_create_bookmark", "Create a bookmark capturing the current selections.",
      {"app": APP_ARG, "title": {"type": "string"}, "description": {"type": "string"}},
      ["app", "title"])
def t_create_bookmark(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        bookmark_id = uuid.uuid4().hex
        s.rpc("CreateBookmark", handle, [{
            "qInfo": {"qId": bookmark_id, "qType": "bookmark"},
            "qMetaDef": {"title": args["title"],
                         "description": args.get("description", "")},
            "creationDate": datetime.datetime.now().isoformat(timespec="seconds"),
        }])
        s.save(handle)
        return {"created": True, "bookmark_id": bookmark_id, "title": args["title"]}


@tool("qlik_select_bookmark", "Apply a bookmark's selections to the session.",
      {"app": APP_ARG, "bookmark_id": {"type": "string"}}, ["app", "bookmark_id"])
def t_select_bookmark(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        result = s.rpc("ApplyBookmark", handle, [args["bookmark_id"]])
        # Some engine builds omit qReturn entirely; the call raising nothing is
        # the real success signal, so confirm by reading the state back.
        layout = _list_layout(s, handle, "CurrentSelection", "qSelectionObjectDef", {})
        selections = (layout.get("qSelectionObject", {}) or {}).get("qSelections", [])
        return {
            "applied": result.get("qReturn", True),
            "bookmark_id": args["bookmark_id"],
            "resulting_selections": [
                {"field": x.get("qField"), "selected": x.get("qSelected"),
                 "selected_count": x.get("qSelectedCount")} for x in selections],
            "reminder": "Call qlik_clear_selections before you finish.",
        }


@tool("qlik_delete_bookmark", "Delete a bookmark and save.",
      {"app": APP_ARG, "bookmark_id": {"type": "string"}}, ["app", "bookmark_id"])
def t_delete_bookmark(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        s.rpc("DestroyBookmark", handle, [args["bookmark_id"]])
        s.save(handle)
        return {"deleted": True, "bookmark_id": args["bookmark_id"]}


@tool("qlik_select_values", "Select values in a field. Session state only -- "
      "clear them with qlik_clear_selections when finished.",
      {"app": APP_ARG, "field": {"type": "string"},
       "values": {"type": "array", "items": {"type": "string"}},
       "toggle": {"type": "boolean"}},
      ["app", "field", "values"])
def t_select_values(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        field = s.rpc("GetField", handle, [args["field"], ""])["qReturn"]["qHandle"]
        selected = s.rpc("SelectValues", field, [
            [{"qText": str(v)} for v in args["values"]],
            bool(args.get("toggle", False)),
            False,
        ])
        return {"selected": selected.get("qReturn", False),
                "field": args["field"], "values": args["values"],
                "reminder": "Call qlik_clear_selections before you finish."}


@tool("qlik_clear_selections", "Clear all selections in the app session.",
      {"app": APP_ARG}, ["app"])
def t_clear_selections(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        s.rpc("ClearAll", handle, [False, ""])
        return {"cleared": True}


# --- Desktop-only write tools ---------------------------------------------

@tool("qlik_set_script", "Desktop only. Replace the app's load script. Does NOT "
      "reload -- follow with qlik_check_script_syntax then qlik_reload_app. "
      "Overwrites the existing script entirely.",
      {"app": APP_ARG, "script": {"type": "string"},
       "save": {"type": "boolean", "description": "Save the app afterwards. Default true."}},
      ["app", "script"])
def t_set_script(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        previous = s.rpc("GetScript", handle).get("qScript", "")
        s.rpc("SetScript", handle, [args["script"]])
        saved = False
        if args.get("save", True):
            s.save(handle)
            saved = True
        return {"set": True,
                "previous_line_count": len(previous.splitlines()),
                "new_line_count": len(args["script"].splitlines()),
                "app_saved": saved,
                "next": "Run qlik_check_script_syntax, then qlik_reload_app."}


@tool("qlik_reload_app", "Desktop only. Reload the app and save it. Blocks until "
      "the reload finishes; long reloads are expected.",
      {"app": APP_ARG,
       "partial": {"type": "boolean", "description": "Partial reload. Default false."},
       "save": {"type": "boolean", "description": "Save after a successful reload. Default true."}},
      ["app"])
def t_reload_app(args):
    with _session() as s:
        _, handle = open_app(s, args["app"])
        s.set_timeout(RELOAD_TIMEOUT)
        started = datetime.datetime.now()
        ok = s.rpc("DoReload", handle, [0, bool(args.get("partial", False)), False]) \
              .get("qReturn", False)
        elapsed = (datetime.datetime.now() - started).total_seconds()
        progress = s.rpc("GetProgress", -1, [0])
        messages = []
        for item in (progress.get("qProgressData", {}) or {}).get("qPersistentProgressMessages", []) or []:
            if item.get("qMessageString"):
                messages.append(item["qMessageString"])
        error = (progress.get("qProgressData", {}) or {}).get("qErrorData") or []
        saved = False
        if ok and args.get("save", True):
            s.save(handle)
            saved = True
        s.set_timeout(float(os.environ.get("QLIK_DESKTOP_TIMEOUT", "60")))
        return {
            "reloaded": ok,
            "elapsed_seconds": round(elapsed, 1),
            "app_saved": saved,
            "errors": [e.get("qErrorString") for e in error if e.get("qErrorString")],
            "progress_tail": messages[-20:],
            "next": "Verify the model with qlik_get_tables_and_keys." if ok
                    else "Inspect qlik_get_reload_log for the failing statement.",
        }


@tool("qlik_save_app", "Desktop only. Persist the app to its .qvf file.",
      {"app": APP_ARG}, ["app"])
def t_save_app(args):
    with _session() as s:
        doc, handle = open_app(s, args["app"])
        s.save(handle)
        return {"saved": True, "app_id": doc.get("qDocId")}


@tool("qlik_create_app", "Desktop only. Create a new empty local app.",
      {"name": {"type": "string"}}, ["name"])
def t_create_app(args):
    with _session() as s:
        result = s.create_app(args["name"])
        return {"created": result.get("qSuccess", False),
                "app_id": result.get("qAppId"), "name": args["name"]}


# --------------------------------------------------------------------------
# MCP stdio plumbing
# --------------------------------------------------------------------------

def call_tool(name, arguments):
    handler = HANDLERS.get(name)
    if handler is None:
        return {"content": [{"type": "text", "text": "Unknown tool: " + name}],
                "isError": True}
    try:
        try:
            result = handler(arguments or {})
        except EngineError as exc:
            if "closed" not in str(exc).lower() and "reach" not in str(exc).lower():
                raise
            # The engine drops idle sockets; rebuild and try once more.
            reset_shared_session()
            result = handler(arguments or {})
        return {"content": [{"type": "text",
                             "text": json.dumps(result, indent=2, default=str)}]}
    except EngineError as exc:
        return {"content": [{"type": "text", "text": "Qlik engine error: " + str(exc)}],
                "isError": True}
    except Exception as exc:  # noqa: BLE001 - surface everything to the agent
        return {"content": [{"type": "text",
                             "text": "{0}: {1}".format(type(exc).__name__, exc)}],
                "isError": True}


def handle_message(message):
    method = message.get("method")
    msg_id = message.get("id")

    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        return {
            "protocolVersion": requested or FALLBACK_PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        params = message.get("params") or {}
        return call_tool(params.get("name"), params.get("arguments"))
    if method == "ping":
        return {}
    if method and method.startswith("notifications/"):
        return None
    if msg_id is None:
        return None
    raise ValueError("Unsupported method: " + str(method))


def serve():
    stdin = sys.stdin
    stdout = sys.stdout
    while True:
        line = stdin.readline()
        if not line:
            return
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_id = message.get("id")
        try:
            result = handle_message(message)
        except Exception as exc:  # noqa: BLE001
            if msg_id is not None:
                stdout.write(json.dumps({
                    "jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32603, "message": str(exc)},
                }) + "\n")
                stdout.flush()
            continue

        if result is None or msg_id is None:
            continue
        stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n")
        stdout.flush()


def selftest():
    print("tools registered:", len(TOOLS))
    print(json.dumps(call_tool("qlik_get_engine_info", {}), indent=2)[:800])
    print(json.dumps(call_tool("qlik_search", {}), indent=2)[:800])


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--list-tools" in sys.argv:
        for entry in TOOLS:
            print(entry["name"])
    else:
        serve()
