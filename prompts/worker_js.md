## JavaScript-specific review notes

Analyze JavaScript in its actual runtime: Node.js (server, filesystem, process, network access) or browser (DOM, XSS + prototype pollution risks). Runtime determines threat model.

### Common bug classes in Node.js server code

- **Command injection** — `child_process.exec(cmd)` invokes shell. `execSync` same. Safe: `spawn('cmd', [arg1], {{shell: false}})`. CWE-78.
- **Path traversal** — `fs.readFile(userPath)`, `express.static(userDir)`. Defense: `path.resolve(base, user)` + `startsWith(base)` check. `path.join` does NOT prevent `..`. CWE-22.
- **Server-side template injection** — User-controlled TEMPLATE code (not data): `ejs.render(userTemplate, data)`, `app.render(userString)`. CWE-1336.
- **Prototype pollution** — `Object.assign({{}} , userJson)` or `_.merge(target, userJson)` with `{{"__proto__": {{...}}}}` pollutes `Object.prototype` globally. Defense: `Object.create(null)`, explicit schema, or freeze prototype at startup. CWE-1321.
- **SSRF** — `http.get(userUrl)`, `axios.get(userUrl)`, custom webhooks. Allowlist scheme + host. CWE-918.
- **Code execution via deserialization** — `eval('(' + userJson + ')')`, `Function('return ' + user)()`, `vm.runInContext(user)` are all RCE. CWE-502, CWE-95.
- **Regex DoS (ReDoS)** — `new RegExp(user)` or fixed regex with catastrophic backtracking matched on long input. CWE-1333.
- **NoSQL injection** — `db.find({{user: req.body.user}})` where body is `{{"$ne": null}}`. Defense: type coercion or schema enforcement. CWE-943.
- **Timing attacks** — `secret === userHmac` leaks length/content. Safe: `crypto.timingSafeEqual()`. CWE-208.
- **Weak randomness** — `Math.random()` for tokens. Use `crypto.randomBytes` or `crypto.randomInt`. CWE-338.
- **Unverified JWT** — `jwt.decode()` without `.verify()` returns unverified claims. Using them for auth is RCE-shaped. CWE-347.
- **Express-specific**: `res.redirect(userUrl)` open redirect, `res.sendFile(userPath)` traversal, parsers accepting unbounded payloads. CWE-601, CWE-22.
- **Next.js / API routes** — serverless handlers reading `req.query`/`req.params` and spawning processes without auth = unauth RCE. CWE-78.
- **Mongoose operator injection** — `Model.find(req.body)` with raw body enables `{{$where}}`, `{{$regex}}` operators. CWE-943.

### Common bug classes in browser code

- **DOM XSS** — `element.innerHTML = user`, `document.write(user)`, `eval(user)`, `setTimeout(user, delay)` with string arg. Safe: `textContent`, `text()`, DOMPurify. CWE-79.
- **URL scheme XSS** — `a.href = user`, `window.location = user` execute `javascript:` schemes. Defense: allowlist `http:`, `https:`, `mailto:`. CWE-79.
- **postMessage without origin check** — `window.addEventListener('message', e => ...)` with no `e.origin` check = cross-origin RCE. CWE-1021.
- **Auth tokens in localStorage** — any XSS steals them. Use `HttpOnly` cookies instead.
- **CSRF without SameSite** — cookies without `SameSite=Lax/Strict` + missing CSRF tokens on state-changes = full CSRF. CWE-352.
- **Clickjacking** — missing `X-Frame-Options: DENY` or CSP `frame-ancestors 'none'` = embeddable. CWE-1021.

### Defenses you should expect to find (don't re-report as missing)

- **Express-validator / Zod / Joi / Yup / Ajv** — when input is schema-validated, type-confusion claims need specific bypass evidence.
- **Helmet middleware** — sets CSP, HSTS, X-Frame-Options. If mounted, header claims are closed.
- **bcrypt / argon2** — correct password hashing. Don't flag.
- **CORS allowlist** — `cors({{origin: ['https://app.example.com']}})` correctly configured. Don't flag.
- **React / Vue / Angular default escaping** — `{{{{user}}}}` in JSX/Vue/Angular is HTML-escaped. Only `dangerouslySetInnerHTML`, `v-html`, `[innerHTML]` are dangerous.
- **JSON.parse without reviver** — safe for parsing, produces plain objects. Don't flag by itself.

### Red flags that warrant investigation

- `eval(...)` / `new Function(user)` / `vm.runIn*Context(user)` reachable (CWE-95)
- `child_process.exec` / `execSync` / `spawn(..., {{shell: true}})` with user-derived arg (CWE-78)
- `_.merge(target, userObj)` / `_.set(target, userPath, val)` with old lodash < 4.17.21 (CWE-1321)
- `Object.assign({{}} , userParsedJson)` for recursive merge into privileged object (CWE-1321)
- `innerHTML = user`, `document.write(user)`, `$.html(user)` in browser (CWE-79)
- `element.outerHTML = user` (CWE-79)
- `href = user` / `location = user` without scheme check (CWE-79)
- `window.postMessage` handler with no `event.origin` check (CWE-1021)
- JWT without `.verify()` or `jwt.decode` result fed into authz (CWE-347)
- `Math.random()` for tokens (CWE-338)
- `new RegExp(user)` (CWE-1333)
- Request body spread directly into Mongoose/similar query builder (CWE-943)
- `fs.readFile(req.query.file)` or `express.static` over attacker path (CWE-22)
- `res.redirect(req.query.next)` without allowlist (CWE-601)

### No need to re-report

- Missing TypeScript types / `any` usage — not a security control
- `var` vs `let`/`const` — style
- Missing `'use strict'` — modern modules are strict by default
- `==` vs `===` outside security-sensitive contexts — style
- Missing error handling on `await` without specific exploit
- Bundle size or performance

### Authorization gates — by framework

In JS server frameworks (Express, Koa, Fastify, Hapi, NestJS, Next.js),
routed handlers follow the gate-on-handler authorization model: a
state-changing route without an auth gate is an `authorization`-category
sink. The framework patterns below describe what counts as a gate.

- **Express**: middleware applied before the handler — either inline on the
  route (`app.post("/x", auth, handler)`, `router.put("/y", requireUser, h)`)
  or globally before the mount (`app.use(authMiddleware)`,
  `app.use("/api", apiAuthMiddleware)`). Common middleware: `passport.authenticate(...)`,
  `express-jwt`, `connect-ensure-login`, custom `(req,res,next)` that checks
  `req.user`/`req.session`.
- **Koa**: `app.use(authMiddleware)` global, or per-route via `router.put(...)`
  middleware chain. Check `ctx.state.user` presence in the handler body as
  an in-body gate.
- **Fastify**: `preHandler` hook — `fastify.addHook('preHandler', auth)`,
  `fastify.route({{ preHandler, handler }})`, or `app.register(authPlugin)`.
- **Hapi**: `route.options.auth` set to a registered strategy
  (`server.auth.strategy(...)`); `auth: false` explicitly disables.
- **NestJS**: `@UseGuards(AuthGuard)` on the controller class or the route
  method, or `APP_GUARD` provider in the module — also annotation-based
  decorators like `@Roles(...)`/`@Public()` that participate with a global
  guard. `@Public()` is a way to *opt out* — its presence is significant.
- **Next.js (API routes / app router)**: middleware via `middleware.ts` /
  `middleware.js` matching the route, `getServerSession()` /
  `auth()` (NextAuth) called early in the handler, or route handler wrapped
  in `withAuth(handler)`. App-router route handlers (`app/api/*/route.ts`)
  with no session check + state change → flag.

State-change idioms (the calls whose handlers need gating; pure-read GETs
only count when they expose cross-tenant data): DB writes
(`Model.create/update/delete`, Mongoose `.save()`, Prisma
`.create/update/delete`, raw SQL writes), filesystem writes,
`child_process.exec/spawn`, network mutations to other services,
`res.cookie()`/session writes.

Gates may be applied per-route, per-group, or globally at app entry
(`app.use(...)`, `middleware.ts` matcher, `APP_GUARD` provider).
