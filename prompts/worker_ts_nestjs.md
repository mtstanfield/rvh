### Detected framework: NestJS

Gate via Guards — class-based interceptors on the controller class or the
route method.

**Method-level**:
- `@UseGuards(AuthGuard)` on the method: `@Post("x") @UseGuards(JwtAuthGuard) async create(...) {}`
- Multiple guards: `@UseGuards(JwtAuthGuard, RolesGuard)`

**Controller-level**:
- `@UseGuards(AuthGuard)` on the controller class — applies to every route in it
- `@Controller("admin") @UseGuards(AdminGuard)`

**Global**:
- `APP_GUARD` provider in a module:
  ```
  providers: [{{ provide: APP_GUARD, useClass: JwtAuthGuard }}]
  ```
  This makes the guard apply to every route across the app — read
  module providers (`xref(APP_GUARD)` finds every provider binding)
  when verifying a controller without `@UseGuards`.
- `app.useGlobalGuards(new JwtAuthGuard())` in `main.ts`

**Opt-out signal**: a `@Public()` / `@SkipAuth()` decorator that a global
guard checks via reflection (`Reflector`). Its presence on a route means
the developer is *explicitly* removing auth — escalate to a finding only
if the route changes state and the public exemption is unjustified.

**State-change idioms** (gate-required): TypeORM `.save/remove`,
`repository.save/delete`, Prisma `.create/update/delete`, Mongoose
`.save`, raw connection writes, file writes, child_process, network
mutations, session/cookie writes.

**Controller locations**: `@Controller(...)` class with method decorators
`@Get/@Post/@Put/@Delete/@Patch`. The decorator stack on each method is
where gates live. After enumerating candidate sinks, walk the project's
modules for `APP_GUARD` providers (`xref(APP_GUARD)`) and the controller
ancestry before verdicting.
