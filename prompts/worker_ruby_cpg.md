### CPG usage patterns — Ruby

**Starting investigation:** `xref(name)` for each dangerous call: `eval`,
`instance_eval`, `class_eval`, `send`, `public_send`, `system`, `exec`,
`spawn`, backticks, `%x[…]`, `Open3.popen3`, `IO.popen`,
`find_by_sql`, `.where` (string arg), `.pluck` (string),
`Marshal.load`, `YAML.load`, `ERB.new(...).result`, `render_inline`.

**`reachable` sources_hint:**
- Rails: `params|request\.(query_string|body|params|headers)|session`
- Sinatra: same `params`
- Hanami: `params|req\.`

**Key sinks:**
- Command injection — `` `cmd #{x}` ``, `system(cmd)`, `exec(cmd)`, `%x[cmd #{x}]` where any part is attacker-controlled; `reachable` with `params` sources_hint.
- SQL injection — `.where("foo = #{x}")` / `.find_by_sql("... #{x}")` = SQLi; safe: `.where("foo = ?", x)` or `where(foo: x)`.
- Mass assignment — `Model.new(params[:model])` without `params.require(:model).permit(...)` exposes protected attributes.
- Deserialization — `Marshal.load(x)` / `YAML.load(x)` (not `safe_load`) → RCE.

**Blind spots:** `send(:method_name, ...)` is dynamic dispatch — `callers`
may miss the edge; `xref` the target. Rails `before_action`/`after_save`
and ActiveSupport concerns hide call chains from Joern.
