### CPG usage patterns — TypeScript

TypeScript compiles to JavaScript before CPG ingestion — all JavaScript
sink/source patterns in `worker_js_cpg.md` apply. TypeScript-specific notes:

**Type erasure:** Joern sees only the post-transpile shape. Interfaces,
generics, and type guards don't constrain dataflow. Don't treat
`strictNullChecks`/`noImplicitAny` as a defense — compile-time only.

**`reachable` sources_hint by framework:**
- Express + TS types: `req\.body|req\.params|req\.query`
- NestJS: `@Body|@Param|@Query|@Headers` (decorator-injected)
- tRPC: `ctx\.input|input`

**Validation guards:** `bodySchema.parse(req.body)` (Zod) or
`@Body() dto: CreateUserDto` (class-validator) sanitizes validated fields.
Verify the schema covers the exact field flowing into the sink — partial
validation is common.

**Decorators:** `@Controller`, `@Get`, `@Post`, `@Middleware`, `@UseGuards`
(NestJS) bind handlers in ways the CPG call graph doesn't model. If
`callers` is empty on a handler, assume external route binding and treat
the decorator as an entry point.
