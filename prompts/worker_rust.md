## Rust review: focus on escape hatches

Rust prevents buffer overflows, use-after-free, data races by construction. Hunt bugs in `unsafe` blocks, FFI, panic vectors, and implicit trust boundaries.

### High-risk patterns

- **`unsafe` blocks** — turns off borrow checker. Flag: `transmute` between incompatible types, `ptr::read`/`ptr::write` without alignment checks, `slice::from_raw_parts` with wrong length, `CStr::from_ptr` on non-null-terminated, `String::from_utf8_unchecked` on arbitrary bytes. CWE-119, CWE-787, CWE-125.
- **FFI** (`extern "C"`, `bindgen`) — Rust type system has zero knowledge of C behavior. Every FFI call is `unsafe`-equivalent even when not marked `unsafe`.
- **Panic on untrusted input** — `.unwrap()`, `.expect()`, indexing `v[i]`, `a / b`, slicing `s[start..end]` all panic. DoS in long-running services. CWE-248.
- **Integer overflow** — release mode wraps silently; debug panics. `Vec::with_capacity(user_len * 2)` wraps in release. CWE-190.
- **Deserialization** — `serde_json` is safe for parsing; `bincode`/`rmp-serde` with `Vec<T>` or `String` fields can allocate unbounded (DoS). `serde_yaml` default config is large-allocation DoS. CWE-502, CWE-400.
- **Command injection** — `Command::new("sh").arg("-c").arg(user)` or `Command::new(user)` with user binary name. `Command::new("fixed").arg(user)` safer (args not shell-interpreted). CWE-78.
- **Path traversal** — `std::fs::read(user_path)`, `PathBuf::from(user)` + I/O. `Path::canonicalize` resolves symlinks + `..` but needs prefix-check. CWE-22.
- **SSRF** — `reqwest::get(user_url)`, `hyper::Client::get(user_uri)`. Same allowlist requirement. CWE-918.
- **SQL injection** — `sqlx::query(&format!("SELECT ... {}", user))` bypasses bind; `sqlx::query("... $1").bind(user)` safe. Diesel `sql_query("...{user}...")` same. CWE-89.
- **Timing side channels** — `a == b` on secrets. Use `subtle::ConstantTimeEq` or `ring::constant_time::verify_slices_are_equal`. CWE-208.
- **Regex DoS** — `regex` crate is linear-time by design (no backtracking). `fancy-regex` or `regex_syntax` with lookbehind CAN backtrack. CWE-1333.
- **TLS misuse** — `danger_accept_invalid_certs(true)` disables verification. `rustls` defaults safe. CWE-295.
- **Weak crypto** — `rand::SmallRng` is NOT crypto-grade. Custom AES/ChaCha instead of `ring`, `rustcrypto`, `sodiumoxide`. CWE-327.

### Safe by design (don't re-report)

- Borrow checker in safe Rust — no buffer overflows, use-after-free, data races without `unsafe` or FFI.
- `serde` with derived types — field shape enforced at parse time.
- `sqlx::query().bind()` — parameterized.
- `reqwest` + `rustls` defaults — cert verification on.
- `tokio::sync::Mutex` / `RwLock` — proper async locking, no data races.
- `.clone()` overuse, missing `#[must_use]`, `println!` in production, edition suggestions — style, not security.

### Red flags

- `unsafe` block dereferencing pointers, casting, or calling FFI with user data (CWE-119, CWE-787).
- `transmute::<T, U>` between incompatible layouts (CWE-843).
- `Command::new("sh").arg("-c")` or `bash -c` with user input (CWE-78).
- `.unwrap()` / `.expect()` / indexing on user-controlled slices in network paths (CWE-248, DoS).
- `serde_yaml::from_str` / `bincode::deserialize` on unbounded input (CWE-400, CWE-502).
- `from_utf8_unchecked`, `from_raw_parts`, `from_raw_parts_mut` with user-derived lengths (CWE-125, CWE-787).
- `format!("SELECT ...{}...", user)` passed to `sqlx::query` or `diesel::sql_query` (CWE-89).
- `reqwest::get(user_url)` or `hyper::Client::request(user_uri)` without allowlist (CWE-918).
- `danger_accept_invalid_certs(true)` or `danger_accept_invalid_hostnames(true)` (CWE-295).
- Hand-rolled crypto (hashing, AES, HMAC) vs `ring` / `rustcrypto` / `sodiumoxide`.
- `Box::leak` or `mem::forget` with sensitive material (memory disclosure).

### Authorization gates — by framework

In Rust web frameworks (Axum, Actix-web, Rocket, Warp), routed handlers
follow the gate-on-handler authorization model: a state-changing route
without an auth gate is an `authorization`-category sink. The framework
patterns below describe what counts as a gate.

- **Axum** (`axum`): a custom `FromRequestParts` extractor (`AuthUser`,
  `AuthBearer`, etc.) on the handler signature, or a tower middleware
  applied via `.layer(from_fn(auth_mw))` / `.route_layer(...)` on the
  router/group. `axum::middleware::from_extractor::<AuthUser>()` is also
  common. Look at the `Router::new()` chain for `.layer(...)` calls.
- **Actix-web** (`actix-web`): `wrap(HttpAuthentication::bearer(...))` /
  `wrap(authMiddleware)` on the `App` or `Scope`, or a `FromRequest`
  extractor (e.g. `Authenticated<User>`) in the handler signature.
- **Rocket** (`rocket`): `FromRequest` request guards in the handler
  signature (`fn route(user: AuthUser, ...)`) — Rocket short-circuits with
  `Outcome::Forward` / `Outcome::Failure` if the guard fails. A handler
  with no auth-typed guard parameter and no fairing-level gate → flag.
- **Warp** (`warp`): filter combinators — `.and(auth_filter())` chained
  before the handler. If there's no `auth` filter in the chain, flag.

State-change idioms (the calls whose handlers need gating; pure-read GETs
only count when they expose cross-tenant data): DB writes via
`sqlx::query!("INSERT/UPDATE/DELETE ...")`, Diesel
`insert_into/update/delete`, SeaORM `Entity::insert/update/delete`,
`std::fs::write` / `OpenOptions::new().write(true)`,
`Command::new(...).spawn()`, HTTP-mutation calls.

Gates may sit on the handler signature (auth-typed extractor /
`FromRequest` guard) or on the router chain (`.layer(...)`,
`.route_layer(...)`, `wrap(...)` at `main.rs` / `lib.rs` / `routes.rs`).
