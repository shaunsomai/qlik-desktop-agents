# qlik-desktop-agents

Five Claude Code subagents for **Qlik Sense Desktop**, plus a bundled MCP server that speaks the Qlik Engine JSON API to the local engine on `ws://localhost:4848`.

Working against a Qlik Cloud tenant instead? Install [`qlik-cloud-agents`](https://github.com/shaunsomai/qlik-cloud-agents) — the platforms differ enough that the agent sets are not interchangeable.

## The agents

| Agent | Model | What it does |
|---|---|---|
| `qlik-desktop-app-inspector` | sonnet | Read-only reconnaissance of a local `.qvf` — sheets, master items, fields, chart data, variables, bookmarks, **the load script**, and **the real loaded data model with verified synthetic keys**. Never writes. |
| `qlik-desktop-script-writer` | inherit | Designs the model and writes the `.qvs`. Under an `APPROVED:` line it closes the loop: set the script, syntax-check it, reload, and verify the model the engine actually built. |
| `qlik-desktop-script-reviewer` | sonnet | The 16-point house checklist, with findings **verified against the loaded model** rather than inferred from script text. Read-only. |
| `qlik-desktop-frontend-builder` | inherit | Builds sheets, charts, master items and filter panes at real grid coordinates, with formats and tags set at creation, then reads each chart back to confirm it returns data. Writes under `APPROVED:`. |
| `qlik-desktop-administrator` | inherit | Deliberately small: engine health, local app inventory, reload execution and log triage, local app creation. Honest that Desktop has no tenant to administer. |

## Prerequisites

- **Qlik Sense Desktop installed and running.** The engine only listens while the app is open.
- **Python 3.8+** on `PATH` as `python`.

That's all. No qlik-cli, no Node, no API key — Desktop's engine is unauthenticated on loopback. The plugin registers the MCP server itself; see [`mcp-server/README.md`](mcp-server/README.md) for manual registration and the environment variables.

Verify the bridge:

```bash
python mcp-server/engine.py     # engine version, product version, app count
```

## What Desktop changes

**Gone.** Spaces, owners, published state, app permissions, the data catalogue, dataset freshness/profile/trust/lineage, glossary, data products, automations, licences, users, groups, reload tasks, Data Gateways. The agents name these as unavailable rather than improvising a local equivalent.

**Better.** Four things the Cloud connector has no equivalent for:

- `qlik_get_script` / `qlik_set_script` — the Cloud inspector has to say "the connected tool set has no load-script getter". Here it does.
- `qlik_check_script_syntax` — validate before claiming a script is good.
- `qlik_reload_app` — closes the write → reload → verify loop.
- `qlik_get_tables_and_keys` — the model the engine actually built, with synthetic keys and the exact fields composing them. The reviewer states them as **fact**, not inference.
- `qlik_list_themes` / `qlik_set_app_theme` — enumerate built-in and custom themes with their registration state, and point an app at one.

Front-end building is also better served: `qlik_add_chart` takes real 24-column grid coordinates, and master items get their format and tags at creation rather than as a UI follow-up.

**Different, and worth knowing:**

- **Section access cannot be validated here.** Desktop does not authenticate users, so a clean reload proves nothing about a section access block. Both script agents run the structural checks, file the whole check under *Unverifiable*, and never tell you it works because the app reloaded. Validate on Cloud or QSEoW before trusting it.
- **Port 4848 is unauthenticated by design.** Anything that can reach it can read and rewrite your local apps. The server keeps to loopback and binds nothing itself, but that is Desktop's security model, not something this plugin improves.
- **One shared engine connection.** Selections an agent makes are visible in your own open Desktop window. The agents record the starting state and clear up after themselves.
- **Writes save immediately and Qlik has no undo.** `qlik_set_script` replaces the entire script; the script writer must back up the previous one to `scripts/<app>/<app>.previous.qvs` first.

## Bundled skill

`qlik-theming` ships with this plugin. It carries the `theme.json` schema, the data-palette and colour-scale sections, the per-chart chrome keys, where theme folders go on Desktop and Cloud, and the registration step that silently stops a theme applying. Any agent can load it; `qlik-desktop-frontend-builder` is told to load it before choosing chart colours.

Palette guidance is bundled; external validators are optional. No dataviz or Node dependency is required.

A worked example lives in [`themes/`](assets/themes/) at the repo root, with the validator output recorded in `THEME-NOTES.md`.

## The permission model

No agent writes to a `.qvf` unless the task it was dispatched with contains a line beginning `APPROVED:` naming the exact objects. Without it the agent returns what it *would* do and stops.

Hard stops that hold **even under an approval**:

- `qlik-desktop-administrator` never deletes anything (it gives you the file path), never sets a load script, and never acts on an app the approval doesn't name.
- `qlik-desktop-script-writer` never sets a script that fails a syntax check, never reloads an app it didn't just write to, and always backs up the previous script first.
- `qlik-desktop-frontend-builder` never touches scripts, reloads, or deletions.

One deliberate difference from the Cloud set: `qlik-script-reviewer` is locked down by its `tools:` frontmatter to `Read, Grep, Glob`. Its Desktop counterpart needs MCP access to read the model, so it cannot be restricted that way — its read-only guarantee rests on its instructions instead, the same basis `qlik-app-inspector` has always used.

## Install

```
/plugin marketplace add https://github.com/shaunsomai/qlik-desktop-agents.git --sparse .claude-plugin plugins
/plugin install qlik-desktop-agents@qlik-desktop-agents-marketplace
```

Copying `agents/*.md` by hand works too, but then you must register the MCP server yourself — see [`mcp-server/README.md`](mcp-server/README.md).

## Two gotchas the hard way

Both are handled by the tooling now; they are recorded because they cost real debugging time and are invisible from the engine's point of view.

**A chart that evaluates is not a chart that renders.** `qlik_get_chart_data` returning rows proves the engine computed a hypercube — nothing more. Three further things must be right, and all three are now applied automatically at creation:

1. the sheet cell needs `bounds` (percentages of the 24×12 grid), or the object has no position and never appears;
2. the object needs the property set its nebula bundle declares — a bar chart with no `dimensionAxis`/`measureAxis` has nothing to lay out;
3. `qHyperCubeDef.qMode` must match the type — `"S"` for most, `"P"` for pivot tables, `"K"` for treemaps.

**Mapping tables are never dropped explicitly.** `DROP TABLE` on a `MAPPING LOAD` table cannot resolve — the table is not in the data model — and the reload aborts with `Request Aborted` and no line number. Qlik discards them itself at script end. The reviewer treats an explicit drop as Critical.

## Licence

[MIT](LICENSE).
