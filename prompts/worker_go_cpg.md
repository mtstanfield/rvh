### CPG usage patterns — Go

**Starting investigation:** `xref(name)` for each dangerous call:
`Command`, `CommandContext` (`exec.*`), `Exec`, `Query` (`sql.DB.*`),
`template.HTML`, `html/template.HTML`, `os.Open`, `ioutil.ReadFile`,
`http.Get`, `json.Unmarshal`, `yaml.Unmarshal`. Joern's Go frontend uses
the short name (e.g. `Command`, not `exec.Command`).

**`reachable` sources_hint:**
- net/http: `r\.URL\.Query|r\.FormValue|r\.PostFormValue|r\.Body|r\.Header`
- gorilla/mux: add `mux\.Vars`
- gin: `c\.Query|c\.PostForm|c\.Param|c\.GetHeader|c\.Bind|c\.BindJSON`
- echo: `c\.QueryParam|c\.FormValue|c\.Bind|c\.Param`

**Key sinks:**
- Command injection — `exec.Command(cmd, args...)` where `cmd` or any arg is attacker-controlled; `reachable` with HTTP sources_hint.
- SQL injection — `db.Query(sqlStr)` / `db.Exec(sqlStr)` built via `fmt.Sprintf` or concatenation; `paths` from input to DB call.
- SSRF — `http.Get(url)` / `http.NewRequest("GET", url, ...)` with `url` from request.
- Template injection — `template.HTML`/`template.JS` bypasses auto-escaping in `text/template`/`html/template`.

**Blind spots:** interfaces via type assertion and goroutine function values
may not connect in CPG; `xref` implementing methods if `callers` is empty.
