### CPG usage patterns — JavaScript / Node.js

**Starting investigation:** `xref(name)` for each dangerous call: `eval`,
`Function` (constructor), `child_process.exec`, `child_process.execSync`,
`child_process.spawn`, `child_process.spawnSync`, `vm.runInNewContext`,
`vm.runInThisContext`, `fs.readFile`, `fs.writeFile`, `fs.createReadStream`,
`require` (dynamic), `setTimeout`/`setInterval` (string arg),
`Object.assign`, `mongoose.find`, `db.query`, `res.sendFile`.

**`reachable` sources_hint by framework:**
- Express: `req\.query|req\.body|req\.params|req\.headers|req\.cookies`
- Koa: `ctx\.query|ctx\.request\.body|ctx\.params|ctx\.headers`
- Fastify: `request\.query|request\.body|request\.params`

**Key sinks:**
- Command injection — `child_process.exec(cmd)` with attacker-controlled `cmd`; `reachable` with sources_hint.
- Prototype pollution — `Object.assign`/`_.merge`/`Object.setPrototypeOf` from `req.body` JSON sets `__proto__`/`constructor.prototype`; check for stripping middleware.
- SSRF — `axios.get(url)` / `fetch(url)` / `http.get(url)` with `url` from request.
- NoSQL injection — `User.find(req.body)` accepts operator objects (`{$ne: null}`, `{$regex: '.*'}`); `express-mongo-sanitize` mitigates.

**Blind spots:** async chains through promises and `EventEmitter.on` are
imprecise; dynamic `require(path.join(...))` hides the real module.
