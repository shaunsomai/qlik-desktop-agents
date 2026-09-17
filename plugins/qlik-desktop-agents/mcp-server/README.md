# qlik-desktop MCP server

Exposes a local **Qlik Sense Desktop** install to Claude Code over the Qlik Engine JSON API.

Bundled Python modules and visualization defaults; no third-party runtime dependencies:

| File | What it is |
|---|---|
| `engine.py` | A minimal RFC6455 WebSocket client and a JSON-RPC session for the Engine API. Stdlib only. |
| `qlik_desktop_mcp.py` | The MCP stdio server: 39 tools, named to match the Qlik Cloud MCP connector wherever an equivalent exists. |

## Why stdlib only

This ships inside a plugin. A `pip install` step is one more thing between a user and a working setup, and the one case where hand-rolling a WebSocket client is defensible is exactly this one: `ws://127.0.0.1:4848`, no TLS, no proxy, no auth, one request in flight at a time.

## Requirements

- Qlik Sense Desktop installed **and running** — the engine only listens while the app is open.
- Python 3.8 or later on `PATH` as `python`.

Nothing else. No `qlik-cli`, no Node, no API key — Desktop's engine is unauthenticated on loopback.

## Quick check

```bash
python engine.py          # prints engine version, product version and app count
python qlik_desktop_mcp.py --selftest
python qlik_desktop_mcp.py --list-tools
```

If `python engine.py` reports `Cannot reach the Qlik Sense Desktop engine`, Desktop is not running.

## Configuration

Read from the environment, all optional:

| Variable | Default | Purpose |
|---|---|---|
| `QLIK_DESKTOP_HOST` | `127.0.0.1` | Engine host. Leave it on loopback. |
| `QLIK_DESKTOP_PORT` | `4848` | Engine port. |
| `QLIK_DESKTOP_TIMEOUT` | `60` | Socket timeout in seconds for normal calls. |
| `QLIK_DESKTOP_RELOAD_TIMEOUT` | `1800` | Socket timeout for `qlik_reload_app`, which can legitimately run for many minutes. |

## Registering it by hand

The plugin registers this server automatically via `mcpServers` in `.claude-plugin/plugin.json`. To register it manually instead — or if `python` is not the right interpreter name on your machine:

```bash
claude mcp add qlik-desktop -- python /full/path/to/mcp-server/qlik_desktop_mcp.py
```

On Windows, if `python` is not on `PATH`, use the full interpreter path (for example `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`).

## Tools

Names match the Qlik Cloud MCP connector wherever the concept exists, so agent instructions written against Cloud largely carry over.

**Read** — `qlik_search`, `qlik_describe_app`, `qlik_list_sheets`, `qlik_get_sheet_details`, `qlik_list_measures`, `qlik_list_dimensions`, `qlik_get_fields`, `qlik_get_field_values`, `qlik_search_field_values`, `qlik_get_chart_info`, `qlik_get_chart_data`, `qlik_list_bookmarks`, `qlik_get_current_selections`, `qlik_get_variables`

**Write** — `qlik_create_measure`, `qlik_update_measure`, `qlik_delete_measure`, `qlik_create_dimension`, `qlik_delete_dimension`, `qlik_create_sheet`, `qlik_add_chart`, `qlik_add_filter`, `qlik_create_bookmark`, `qlik_select_bookmark`, `qlik_delete_bookmark`, `qlik_select_values`, `qlik_clear_selections`

**Desktop only** (no Qlik Cloud MCP equivalent) — `qlik_get_script`, `qlik_set_script`, `qlik_check_script_syntax`, `qlik_get_tables_and_keys`, `qlik_reload_app`, `qlik_save_app`, `qlik_create_app`, `qlik_list_connections`, `qlik_get_engine_info`, `qlik_get_reload_log`

`qlik_get_tables_and_keys` is the most valuable of these: it returns the data model the engine actually built, including synthetic keys with the exact fields they are composed of. It turns "your script probably creates a synthetic key" into "it does, `$Syn 1` over `CustomerID` + `Region`".

## Why `viz_defaults.json` exists

A chart is not renderable just because its hypercube evaluates. Every native
Qlik chart is a nebula.js bundle that declares the properties a new instance
needs, and a chart created without them comes back **blank** — a bar chart with
no `dimensionAxis`/`measureAxis` has nothing to lay out, even though
`qlik_get_chart_data` happily returns rows. `qlik_add_chart` merges these
defaults in at creation time.

Equally load-bearing: a sheet cell needs `bounds` (percentages of the 24×12
grid). `col`/`row`/`colspan`/`rowspan` alone is the legacy grid, and the current
client will not position from it — every object on the sheet stays invisible.

`viz_defaults.json` is harvested from the installed client. Regenerate it after
a Qlik Sense upgrade:

```bash
python extract_viz_defaults.py
```

## Behaviour worth knowing

- **One shared connection.** The server holds a single engine connection for its lifetime, because selections, open-doc handles and session objects all belong to the WebSocket. A connection per call would silently discard every selection. The practical consequence: selections made by a tool are visible in the user's own open Desktop window, and agents are instructed to clear them.
- **Writes save immediately.** Every `qlik_create_*` / `qlik_update_*` / `qlik_delete_*` call ends with `DoSave`, so the `.qvf` on disk reflects the change. Qlik has no undo.
- **`qlik_set_script` replaces the whole script.** The agents are instructed to back up the previous script to a file first; if you call the tool yourself, do the same.
- **App references are forgiving.** Any of the app title, the `.qvf` file name, the name without the extension, or the full path will resolve. Ambiguous references fail loudly rather than picking one.
- **Idle sockets reconnect.** If the engine drops the connection, the next tool call rebuilds it and retries once.

## Security note

The Desktop engine on port 4848 is unauthenticated by design — anything that can reach that port can read and rewrite your local apps. This server does not change that, and it binds nothing itself; it is a client. Keep `QLIK_DESKTOP_HOST` on loopback.
