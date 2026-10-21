### Detected framework: Express

Gate via middleware — applied inline on the route, on a Router/sub-app, or
globally before route mounts.

**Inline middleware**:
- `app.post("/x", authMw, handler)` — middleware runs before handler
- `router.put("/y", requireUser, validator, handler)` — chain of middlewares

**Router/group level**:
- `const apiRouter = express.Router(); apiRouter.use(authMw); apiRouter.post("/x", h)`
- `app.use("/api", authMw)` mounts auth middleware before the `/api` prefix

**Global**:
- `app.use(authMw)` before any route mounts — covers everything thereafter
- Order matters: `app.use(express.static(...))` before `app.use(authMw)`
  leaves static files unguarded; check the *order* in `app.js`/`server.js`

**Common auth middlewares**:
- `passport.authenticate("jwt", { session: false })`,
  `passport.authenticate("local")`
- `express-jwt` returning 401 on invalid/missing token
- `connect-ensure-login.ensureLoggedIn()`
- Custom `(req, res, next) => { if (!req.user) return res.sendStatus(401); next(); }`

**Anti-rules**:
- Validation middleware (express-validator, joi, zod) is NOT auth.
- CORS middleware is NOT auth.
- `helmet()`, `compression()`, `body-parser` — not auth.
- `csurf` is CSRF, not auth — separate concern.

**State-change idioms** (gate-required): ORM writes
(`Model.create/update/destroy`, Mongoose `.save`, Sequelize `.save/destroy`,
Prisma `.create/update/delete`, TypeORM `.save/remove`), raw
`pool.query("INSERT/UPDATE/DELETE")`, `fs.writeFile`,
`child_process.exec/spawn`, `axios.post/put/delete` to other services,
`res.cookie()`/`req.session` writes.

**Route module locations**: route definitions live in routes modules and
the server-setup file (commonly `app.js`, `server.js`, `index.js`,
`routes/*.js`). The middleware chain wraps inline-unguarded handlers
through `app.use(...)` calls earlier in the setup file — trace this
chain end-to-end (`xref(use)` finds every `.use(...)` call site)
before verdicting a handler as unguarded.
