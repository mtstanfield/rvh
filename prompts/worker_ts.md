## TypeScript-specific review notes

TypeScript compiles to JS; same runtime, same sinks. Read `worker_js.md` for core coverage. This file adds where the type system misleads.

### Where TS helps

- `noImplicitAny` + exhaustive switches catch missing-case bugs before runtime. CWE-1021.
- Branded types (`type SafeHtml = string & { __brand: 'SafeHtml' }`) block untagged strings from unsafe sinks.
- `strictNullChecks` catches unhandled `undefined` that JS would crash on.

### Where TS doesn't help

- `JSON.parse(req.body)` returns `any`; `as User` at a boundary is unfounded — runtime shape is whatever arrived. All JS injection classes apply. CWE-502, CWE-95.
- `as any` / `as unknown as T` silences the checker; sinks downstream need JS-level scrutiny.
- Type predicates without real validators (`function isUser(x): x is User { return !!x; }`) — find the actual ajv/zod/io-ts call. CWE-502.
- `__proto__` / `constructor.prototype` access — TS types don't model it. Prototype pollution applies identically. CWE-1321.
- `eval`, `Function`, `vm.runInContext` bypass types entirely. CWE-95.

### Red flags

- `@ts-ignore` / `@ts-expect-error` adjacent to a data sink (DB query, `spawn`, `fs.read`). CWE-95, CWE-78.
- `JSON.parse(...)` typed as a concrete interface without a runtime validator. CWE-502.
- `Reflect.get(obj, userKey)` or `(obj as any)[userKey]` — property-path-injection. CWE-1021.
- NestJS `@UseGuards` / TypeORM `@Validate*` decorators are compile-time hints; verify actual runtime enforcement. `@ValidateNested()` without `@Type()` leaves nested shapes unvalidated. CWE-502.
- `tsconfig.json` `"strict": false` — type system doing less work than it appears.

### Don't re-report

See `worker_js.md`. Don't flag loose types or missing generics unless they directly enable an exploit.

### Authorization gates

Same gate patterns as JavaScript — see `worker_js.md` for Express, Koa,
Fastify, Hapi, NestJS, and Next.js gate symbols. TS-specific notes:

- **NestJS**: prefer `@UseGuards(AuthGuard)` over manual `request.user`
  checks. A controller with no `@UseGuards` and no `APP_GUARD` provider
  module-side, performing state changes → flag.
- **Decorator stripping**: `// @ts-ignore` immediately above a route handler
  decorator can hide a removed/disabled gate. If the decorator stack looks
  unusual, surface it as a candidate sink and evaluate the active
  decorators with `inspect(file:line)` to confirm what is actually
  applied.
- **Type-only "auth"**: a parameter typed as `User` does NOT mean it's
  authenticated. Pydantic-equivalents (zod, io-ts) validate *shape*, not
  identity. Don't treat `req.body: AuthenticatedUser` as a gate.
