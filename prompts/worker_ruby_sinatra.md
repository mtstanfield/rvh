### Detected framework: Sinatra

Gate via `before` blocks at the top of the app or scoped to route patterns.

**Global before**:
- `before do; halt 401 unless authenticated?; end` at the top of the app
  class — runs before every route

**Pattern-scoped before**:
- `before '/api/*' do; require_token!; end` — runs only for matching paths
- Order matters: `before` blocks declared after a route definition still
  run for that route, but it's idiomatic to declare them at the top

**In-route**:
- `halt 401 unless session[:user_id]` at the top of the route block
- Helpers: `helpers do; def authenticated?; ...; end; end` + an explicit
  `authenticate!` call

**Anti-rules**:
- `enable :sessions` enables session storage but doesn't gate anything;
  the gate is the explicit check.
- `set :erb, escape_html: true` is XSS hardening, not auth.

**State-change idioms** (gate-required): ActiveRecord/Sequel
`.save/.update/.destroy`, raw `DB.execute(<write-SQL>)`, `File.write`,
`system`/`` ` `` /`exec`, `Net::HTTP.post/put/delete`.

**Route locations**: routes are top-level method calls — `get '/x' do ... end`,
`post '/y' do ... end`. The gate is either a global `before`, a
pattern-scoped `before`, or an inline `halt` at the top of the route block.
