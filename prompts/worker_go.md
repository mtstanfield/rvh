## Go-specific review notes

Focus on bug classes that actually manifest in idiomatic Go code. Be skeptical of defenses the language provides by default.

### Common bug classes in Go

- **Concurrency bugs** — `sync.Map` / unlocked map writes / atomic-pointer races / `sync.Once` misuse / `TOCTOU` check-then-act on shared state. CWE-362, CWE-367.
- **Path traversal** — `filepath.Join` skips sanitization; `filepath.Clean` resolves `..` but doesn't block escape. Verify: untrusted paths flow through `filepath.Clean` AND boundary check (`strings.HasPrefix(cleanPath, allowedRoot)`). CWE-22.
- **Command injection** — `exec.Command(name, args...)` with untrusted `name` or `exec.Command("sh", "-c", untrusted)`. Fixed argv in `exec.Command("cmd", userArg)` is safe (args not shell-interpreted). CWE-78.
- **SQL injection** — direct `fmt.Sprintf("... %s ...", val)` into query string. Parameterized queries (`db.Where("col = ?", val)`, `db.Exec("...?", val)`, GORM placeholders, `database/sql` `?`) are safe. CWE-89.
- **JWT / crypto misuse** — `jwt.Parse(token, jwt.WithVerify(false))` dangerous only if trusted downstream. Check: unverified parse extracts `iss` → re-parse with `jwt.WithKeySet(...)` AND issuer allowlist between parses. CWE-287, CWE-295.
- **SSRF** — `http.Get`, `http.Client.Do`, custom transports. Block URL construction from user input (JWKS endpoints, webhooks, peers) without allowlist of permitted hosts/schemes. CWE-918.
- **Unsafe deserialization** — `gob.Decode`, `json.Unmarshal` to `interface{{}}`, YAML via `yaml.v2` (unsafe by default). CWE-502.
- **Timing side-channels** — `x == y` on secrets (API keys, HMAC, tokens). Use `crypto/subtle.ConstantTimeCompare`. CWE-208.
- **Resource exhaustion** — unbounded channels, goroutines-per-request without limit, `io.ReadAll` on request bodies without `http.MaxBytesReader`. CWE-400.

### Defenses to expect

- **Authorization middleware** — `gin` `group.Use(auth)` / `chi` `r.Use(Authz)` / stdlib `mux` wrapped handlers. Assume auth runs before handler unless code explicitly bypasses it.
- **Context cancellation** — `ctx.Done()` in long-running goroutines. Missing context is a leak, rarely a vulnerability unless combined with resource exhaustion.
- **JSON struct tags** — `json:"field"` skips unknown fields by default; not a vulnerability.
- **Router parameter escaping** — `gorilla/mux`, `gin`, `chi`: `c.Param("id")` returns URL-decoded value, not shell-injected.
- **Linting** — assume `.golangci.yaml` or equivalent passes; skip linter-style complaints unless they hide security logic.

### Red flags

- `unsafe.Pointer` (escape hatch to memory unsafety, CWE-787).
- `os/exec` with user input anywhere in argv.
- `text/template` rendering HTML (XSS by default, CWE-79); should be `html/template`.
- Custom crypto instead of `crypto/*` or `golang.org/x/crypto/*`.
- `reflect` in auth/serialization paths (often hides logic errors).
- `net/http/httputil.ReverseProxy` with attacker-controlled Director (CWE-918).

### No need to re-report

- Missing context timeouts without exploitable consequence.
- "Could add rate limiting" — not a bug unless you demonstrate which resource exhausts.
- Linter complaints (unused vars, shadowed names) unless they hide a security check.

### Authorization gates — by framework

In Go web frameworks (net/http, gin, echo, chi, fiber), routed handlers
follow the gate-on-handler authorization model: a state-changing route
without an auth gate is an `authorization`-category sink. The framework
patterns below describe what counts as a gate.

- **net/http (stdlib)**: handlers wrapped via middleware adapter functions
  — `http.Handle("/x", authMw(handler))`, or composed at server setup
  (`mux.Handle("/x", authMw(http.HandlerFunc(handler)))`). Look for
  middleware naming conventions: `RequireAuth`, `WithAuth`, `Authenticated`,
  `MustAuth`. In-handler gate: parse `r.Header.Get("Authorization")` /
  cookie session and short-circuit on miss.
- **Gin** (`github.com/gin-gonic/gin`): `r.Use(authMiddleware)` for global,
  `group := r.Group("/api"); group.Use(authMw)` for prefixed, or per-route
  inline (`r.POST("/x", authMw, handler)`). Look for `c.MustGet("user")` in
  the handler body — it's a downstream signal that an upstream middleware
  set `c.Set("user", ...)`.
- **Echo** (`github.com/labstack/echo/v4`): `e.Use(authMw)` global,
  `g := e.Group("/api", authMw)` group-scoped, or
  `e.POST("/x", handler, authMw)` per-route. The `echo-jwt` middleware is
  common.
- **Chi** (`github.com/go-chi/chi`): `r.Use(authMw)`, or
  `r.Group(func(r chi.Router) {{ r.Use(authMw); ... }})` for scoped, or
  `r.With(authMw).Post("/x", h)` per-route.
- **Fiber** (`github.com/gofiber/fiber/v2`): `app.Use(authMw)`, or
  `api := app.Group("/api", authMw)`, or per-route. Often paired with
  `fiber/middleware/jwt`.

State-change idioms (the calls whose handlers need gating; pure-read GETs
only count when they expose cross-tenant data): DB writes via
`database/sql` `db.Exec` (write-SQL), GORM `.Create/.Update/.Delete`,
`os.WriteFile`, `os.Create`, `exec.Command` invocation, network mutation
to other services.

Gates may be inline (`r.POST("/x", authMw, h)`), group-level
(`group.Use(authMw)`), or global at server setup (`r.Use(authMw)` in
`main.go` / `server.go` / `cmd/*/main.go`).
