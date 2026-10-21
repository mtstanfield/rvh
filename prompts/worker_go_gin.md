### Detected framework: Gin

Gate via middleware on the router, a route group, or per-route.

**Global**:
- `r := gin.Default(); r.Use(authMiddleware)` — applies to every route
  registered after this call

**Group-level**:
- `api := r.Group("/api"); api.Use(authMw); api.POST("/x", handler)` —
  scoped to the group
- `api := r.Group("/api", authMw)` — equivalent shorthand

**Per-route**:
- `r.POST("/x", authMw, handler)` — middleware chain before the final handler

**In-handler signal**:
- `c.MustGet("user")` in the handler body — implies an upstream middleware
  set `c.Set("user", ...)`. Trace the chain via `xref(Set)` / `xref(Use)`
  to confirm a real upstream gate exists.
- `c.GetString("user_id")` from JWT middleware contexts.

**Common middleware names**: `gin-jwt`, `gin-contrib/sessions` + custom
auth, `casbin`-based authz middleware, hand-written
`func authMiddleware(c *gin.Context) { ... if !ok { c.AbortWithStatus(401); return }; c.Next() }`.

**Anti-rules**:
- `gin.Logger()` / `gin.Recovery()` are not auth.
- `c.Next()` without a 401 short-circuit is not a gate — the middleware
  must `c.AbortWithStatus(...)` or `c.AbortWithStatusJSON(...)` to count.

**State-change idioms** (gate-required): GORM `.Create/.Save/.Update/.Delete`,
raw `db.Exec("INSERT/UPDATE/DELETE ...")`, `os.WriteFile`, `os.Create`,
`exec.Command(...).Run()`, `http.Post`/`http.Put`/`http.Do(req)` to other
services.

**Route registration locations**: `main.go`, `routes/*.go`, or similar —
`r.GET/POST/PUT/DELETE/PATCH("/path", handler)`. Walk back to the
`r := gin.New()` / `gin.Default()` setup (find it via
`xref(gin.Default)` / `xref(gin.New)`) to locate global middleware. A
handler is unguarded only if no global, group, or inline middleware
covers it.
