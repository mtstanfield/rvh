### Detected framework: FastAPI

Gate via dependency injection — fundamentally different from decorator-based
frameworks.

**Per-route dependency**:
- `Depends(get_current_user)` parameter on the path operation function:
  `async def create_item(item: Item, user: User = Depends(get_current_user)):`
- `Security(get_current_user, scopes=[...])` — same plus OAuth2 scopes

**Router-level**:
- `APIRouter(dependencies=[Depends(get_current_user)])` — applies to every
  route registered on this router

**App-level**:
- `FastAPI(dependencies=[Depends(get_current_user)])` — applies to every route

**Common auth dependencies**: `OAuth2PasswordBearer`, `HTTPBearer`, `HTTPBasic`,
custom callables that raise `HTTPException(401)` on missing/invalid creds.

**Gotchas**:
- A `User` *type* on a parameter without `Depends(...)` is just type-hinting,
  not a gate. The dependency call is what gates.
- Pydantic models in the body validate shape, not identity. `body: Token` is
  not auth.
- `@app.get("/...")` decorator is NOT a gate. Routes default to public.

**State-change idioms** (gate-required): SQLAlchemy
`session.add/.delete/.commit`, SQLModel writes, raw
`await db.execute(<write-SQL>)`, file writes, `subprocess.*` /
`asyncio.create_subprocess_*`, network mutations.

**View locations**: path operation functions are decorated with `@app.post`,
`@app.put`, `@app.delete`, `@app.patch`, `@router.post`, etc. The function
*signature* is where gates live (as `Depends(...)` parameters).

**Enumerate-then-verify**: walk every `@app.<method>` / `@router.<method>`
registration in the file, then for each path operation function verify
either a `Depends(...)`/`Security(...)` parameter, a router-level
`dependencies=[...]`, or an app-level `dependencies=[...]` covers it.
Don't stop after grepping for known-bad patterns — list every routed
function and check each. A path operation with `def create_item(item: Item):`
and no `Depends()` parameter is unguarded.
