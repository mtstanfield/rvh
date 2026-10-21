### Detected framework: Laravel

Gate via middleware on routes, route groups, or controller constructors.

**Route-level**:
- `Route::post('/x', [Controller::class, 'create'])->middleware('auth');`
- `Route::middleware(['auth', 'verified'])->post('/x', [...]);`

**Route-group**:
- `Route::middleware(['auth'])->group(function () {{ Route::post(...); }});`
- `Route::prefix('admin')->middleware(['auth', 'role:admin'])->group(...)`

**Controller constructor**:
- `public function __construct() {{ $this->middleware('auth'); }}` —
  applies to every method
- `$this->middleware('auth')->only(['store', 'update', 'destroy']);` —
  scoped to specific actions
- `$this->middleware('auth')->except(['index', 'show']);` — excludes reads

**Authorization (in addition to authentication)**:
- `$this->authorize('update', $resource);` — runs the policy class
- `Gate::allows('update-post', $post)` — Gate facade
- `@can('update-post', $post)` Blade directive (template-side, not action gate)

**Form Requests**: a `FormRequest` parameter type-hint runs validation +
authorization automatically — its `authorize()` method MUST return a
boolean check, not just `return true;`. A form request that always returns
true is NOT a gate.

**Anti-rules**:
- `csrf` middleware is CSRF protection, not auth.
- `throttle:60,1` is rate-limit, not auth.
- The default `web` middleware stack (in `app/Http/Kernel.php`) does NOT
  include auth. The `auth` middleware must be added explicitly.

**State-change idioms** (gate-required): Eloquent `->save()`, `->delete()`,
`->update()`, `Model::create()`, `Model::destroy(id)`, raw
`DB::insert/update/delete`, `Storage::put`/`File::put`, `Mail::send`,
`Http::post/put/delete`, `Cookie::queue`/`Session::put`.

**Gate surfaces**: route, route group, controller constructor, or a Form
Request's `authorize()` method. Trace from `routes/web.php` /
`routes/api.php` to the controller method (`xref(middleware)` finds
`->middleware(...)` chains) and read all four surfaces before
verdicting.
