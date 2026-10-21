## Ruby-specific review notes

Analyze Ruby code. Distinguish between idiomatic-language bugs and defenses built into Rails/Sinatra.

### Common bug classes in Ruby

- **Deserialization** (CWE-502) — `Marshal.load(user)`, `YAML.load(user)` / `unsafe_load`, `JSON.load`. Safe: `YAML.safe_load`, `JSON.parse`.
- **Command injection** (CWE-78) — `system("#{user}")`, backticks `` `#{user}` ``, `exec("#{user}")`, `Kernel.spawn("#{user}")`, `Open3.capture2("#{user}")`. Safe: `system("cmd", arg1, arg2)` (array), `Open3.capture2("cmd", arg)`.
- **Code injection** (CWE-95) — `eval(user)`, `instance_eval(user)`, `class_eval(user)`, `binding.eval(user)`. `send(user_method)` / `public_send(user_method)` arbitrary method invocation. RCE.
- **SQL injection** (CWE-89) — `where("name = '#{user}'")`, `find_by_sql("#{user}")`, `execute("#{user}")`. Safe: `where(name: user)` (hash), `where("name = ?", user)`, `where("name = :n", n: user)`.
- **Mass assignment** (CWE-915) — `Model.new(params[:user])`, `update(params)` without `permit`. Safe: `params.require(:user).permit(:name, :email)`.
- **Regex DoS** (CWE-1333) — `Regexp.new(user_pattern)` with catastrophic backtracking on long input.
- **SSRF** (CWE-918) — `Net::HTTP.get(URI(user_url))`, `HTTParty.get(user_url)`, `open(user)` (requires `open-uri`). Needs URL allowlist.
- **Path traversal** (CWE-22) — `File.read(user)`, `open(user)`, `send_file(user_path)`. Defend: `realpath + prefix-check`.
- **Template injection** (CWE-1336) — `render inline: user`, ERB/Slim/Haml with user TEMPLATE (not data). `<%= %>` data is safe (autoescape on).
- **XSS via html_safe/raw** (CWE-79) — `raw(user)`, `html_safe`, `<%== user %>`. Don't flag `<%= var %>`.
- **Nested attribute bypass** (CWE-915) — `accepts_nested_attributes_for :posts` without specifying `permit(posts_attributes: [...])`.
- **Open redirect** (CWE-601) — `redirect_to params[:next]`. Rails checks format, not host. Safe: `only_path: true` or host allowlist.
- **Weak crypto** (CWE-327, CWE-338) — `Digest::MD5`, `Digest::SHA1`, `rand()` for tokens. Safe: `BCrypt`, `Argon2`, `SecureRandom.hex`.
- **Timing side-channel** (CWE-208) — `token == user_token`. Safe: `ActiveSupport::SecurityUtils.secure_compare`.
- **Rails-specific** — `skip_before_action :authenticate`, `protect_from_forgery with: :null_session`, `dangerous_class_eval`, credentials committed.
- **Sinatra-specific** — no default CSRF (unlike Rails); missing `Rack::Protection` means every POST is CSRF-able.

### Defenses you should expect to find (don't re-report as missing)

- **Rails strong_parameters** — `params.require(:user).permit(:name)` is standard. Don't flag if applied.
- **Rails CSRF** — `protect_from_forgery with: :exception` default. Flag only `:null_session` / `skip_forgery_protection`.
- **ActiveRecord parameterized where** — `where(name: user)` safe. Only string interpolation is SQLi.
- **Rails autoescape ERB** — `<%= var %>` escapes. Don't flag unless `raw`, `html_safe`, `<%== %>`.
- **bcrypt / has_secure_password** — standard password hashing. Safe.
- **ActiveSupport::MessageVerifier / MessageEncryptor** — signed/encrypted payloads. Safe.
- **YAML.safe_load** — safe YAML parsing.

### Red flags that warrant investigation

- `eval(user)`, `instance_eval(user)` (CWE-95)
- `Marshal.load(user)` on network byte source (CWE-502)
- `YAML.load(user)` or `unsafe_load(user)` (CWE-502)
- `send(user_method_name)` / `public_send(user)` (CWE-470)
- `"... #{user} ..."` into `where`, `execute`, `find_by_sql` (CWE-89)
- `Model.new(params[:anything])` / `update(params)` without `permit` (CWE-915)
- `system("cmd #{user}")`, backticks `` `#{user}` ``, `exec("#{user}")` (CWE-78)
- `open(user)` with `open-uri` required (CWE-22 / CWE-918)
- `redirect_to params[:url]` without `only_path` / host check (CWE-601)
- `skip_before_action :authenticate_user!` on state-change
- `protect_from_forgery with: :null_session`
- `raw(user)` / `html_safe` on attacker-influenced data (CWE-79)

### No need to re-report

- Missing `frozen_string_literal: true` — style
- RuboCop warnings — style
- `attr_accessor` vs explicit getters — style
- Ruby's lack of type annotations — not applicable

### Authorization gates — by framework

In Ruby web frameworks (Rails, Sinatra, Grape), routed handlers follow
the gate-on-handler authorization model: a state-changing action without
an auth gate is an `authorization`-category sink. The framework patterns
below describe what counts as a gate.

- **Rails**: `before_action :authenticate_user!` (Devise),
  `before_action :authorize!` (Pundit/CanCanCan), `before_action :require_login`,
  custom before_action filters that 401/403 on miss. Class-level applies to
  all actions; per-action via `before_action :name, only: [:create]`. Skip
  filters via `skip_before_action` are a *removal* — if present and
  non-trivial, scrutinize. CanCanCan: `authorize! :update, @resource` in
  the action body counts as in-body gate. Pundit: `authorize @resource`.
- **Sinatra**: `before do; require_auth!; end` or per-route
  `before '/api/*' do; ...; end`, or in-body `halt 401 unless authenticated?`
  at the top of the route block.
- **Grape**: `before { authenticate! }` or
  `helpers { def authenticate! ...; end }` with an explicit call at the top
  of the resource.

State-change idioms (the calls whose handlers need gating; pure-read GETs
only count when they expose cross-tenant data): ActiveRecord `.save`,
`.update`, `.destroy`, `.create`, `update_all`, `delete_all`,
`connection.execute(<write SQL>)`, `File.write`, `system`/`` ` ``/`exec`,
`Net::HTTP.post/put/delete`.

Gates inherit through the controller hierarchy: a `before_action` on
`ApplicationController` covers every subclass action that isn't
`skip_before_action`-ed; a `before` block at the top of a Sinatra app
covers every route.
