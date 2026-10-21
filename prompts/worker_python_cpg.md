### CPG usage patterns — Python

**Starting investigation:** `xref(name)` for each dangerous call: `eval`,
`exec`, `Popen`, `call`, `run` (`subprocess.*`), `os.system`,
`pickle.loads`, `yaml.load`, `yaml.unsafe_load`, `cursor.execute`,
`Manager.raw`, `Manager.extra`, `render_template_string`, `ET.fromstring`,
`ElementTree.parse`. Joern's Python frontend stores call nodes by method
name only (short name, not module-qualified).

**`reachable` sources_hint by framework:**
- Django: `request\.GET|request\.POST|request\.META|request\.body`
- Flask: `request\.args|request\.form|request\.json|request\.values`
- FastAPI: handler parameters and `Request\.`
- Fallback: `input|sys\.argv|os\.environ`

**Key sinks:**
- `subprocess.*` with `shell=True` — `args` is an injection sink; `reachable(sink=<file:line>, sources_hint=<fw>)`.
- Django ORM raw SQL (`cursor.execute`, `Manager.raw()`, `Manager.extra()`) — SQLi if sql uses f-strings/format over user input.
- Deserialization: `pickle.loads`, `yaml.load` (anything but `SafeLoader` / `yaml.safe_load`, including `FullLoader`), `marshal.loads`, `dill.loads` — always sinks; `reachable` from network/file-read sources. `FullLoader` does not flip the verdict to safe.

**Blind spots:** decorators (`@login_required`), middleware, and signal
handlers are invisible to CPG — fall back to `xref` + reading.
