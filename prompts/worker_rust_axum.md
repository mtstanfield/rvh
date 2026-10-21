### Detected framework: Axum

Gate via tower middleware layer or a custom `FromRequestParts` extractor
in the handler signature.

**Tower middleware layer**:
- `Router::new().route("/x", post(handler)).layer(from_fn(auth_mw))` —
  applies to the route
- `.route_layer(...)` (only the routes defined before this call) vs
  `.layer(...)` (all routes including nested) — semantics matter
- Group-scoped: `Router::new().nest("/api", api_router.layer(auth_mw))`
- Common helper: `axum::middleware::from_extractor::<AuthUser>()`

**Extractor-based**:
- A custom `AuthUser` / `Authenticated<User>` extractor implementing
  `FromRequestParts` whose `from_request_parts` returns
  `Err((StatusCode::UNAUTHORIZED, ...))` on missing/invalid creds:
  ```
  async fn handler(user: AuthUser, Json(body): Json<NewItem>) -> Result<...>
  ```
- The handler signature is what gates. A handler with no auth-typed
  parameter and no upstream `.layer(auth_mw)` → flag.

**Common patterns**:
- `tower-http::auth::RequireAuthorizationLayer` for basic header schemes
- `axum-login` crate's `AuthLayer` / `RequireAuthorizationLayer`
- JWT crates expose `jwt_auth_middleware` typically composed via `from_fn`

**Anti-rules**:
- `Json<T>`, `Path<T>`, `Query<T>` extractors validate shape, not identity.
- `State<AppState>` is dependency injection, not auth.
- `tracing` / `cors` / `compression` layers are not auth.

**State-change idioms** (gate-required):
- `sqlx::query!("INSERT/UPDATE/DELETE ...")`, `sqlx::query_as!` for writes,
- Diesel `insert_into/update/delete` execution,
- SeaORM `Entity::insert/update/delete` execution,
- `tokio::fs::write` / `OpenOptions::new().write(true)`,
- `Command::new(...).spawn()`,
- `reqwest::Client::post/put/delete` to other services,
- session/cookie writes via `tower-sessions` or similar.

**Route registration locations**: `main.rs` / `lib.rs` / `routes.rs`
carry `Router::new().route(...)` chains, with `.layer(...)` /
`.route_layer(...)` applied to scope middleware. The two gate surfaces
are (a) the layer chain wrapping the route and (b) the handler signature
itself (auth-typed extractor). A handler is unguarded only when neither
surface covers it — trace both (`xref(layer)` / `xref(route_layer)`
find the wrapping chains) before verdicting.
