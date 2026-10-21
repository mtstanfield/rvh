## PHP-specific review notes

Focus on bugs endemic to PHP idioms. Don't overclaim — frameworks like Laravel, Symfony, WordPress, Doctrine, Eloquent provide defenses you should assume are present unless explicitly bypassed.

### Common bug classes in PHP

- **SQL injection** (CWE-89) — string-built `mysqli_query`, `mysql_query`, `PDO::query`, `$pdo->exec` on user input. Safe: `$pdo->prepare("... ?") + execute([$val])`, `mysqli_prepare + bind_param`, Eloquent `Model::where('col', $val)`, Doctrine `QueryBuilder->setParameter`. Watch `DB::raw($user)`, `whereRaw($user)`, `selectRaw($user)` — these bypass parameterization.
- **Command injection** (CWE-78) — `exec`, `system`, `passthru`, `shell_exec`, `` `$cmd` `` with user input. `escapeshellarg()` quotes one arg; `escapeshellcmd()` escapes metacharacters but allows adjacent args—neither is sufficient. Use argv arrays (where available in language).
- **Code injection / RCE** (CWE-94, CWE-95) — `eval($str)`, `assert($str)` (PHP 7.x), `create_function()`, `preg_replace("..."/e)` on attacker input = RCE.
- **Insecure deserialization** (CWE-502) — `unserialize($user)` triggers `__wakeup`, `__destruct`, `__toString` magic methods → POP-chain RCE. Safe: `unserialize(..., ['allowed_classes' => false])` or use JSON. Also `phar://` streams in file-operations (`file_exists`, `is_file`, `filesize`) trigger deserialization on attacker-uploaded `.phar`.
- **LFI/RFI** (CWE-98) — `include`, `require`, `include_once`, `require_once` with user-controlled path fragment. RFI needs `allow_url_include=On` (off by default since 5.2); LFI always works.
- **XXE** (CWE-611) — `simplexml_load_string`, `DOMDocument::loadXML`, `SoapClient` on external XML. PHP ≥ 8.0 disables external entities by default; older versions need `libxml_disable_entity_loader(true)`. Flags `LIBXML_NOENT`, `LIBXML_DTDLOAD` re-enable vulnerability.
- **Path traversal** (CWE-22) — `file_get_contents($user)`, `fopen($user)`, `copy($user, ...)`, `move_uploaded_file($_FILES['f']['tmp_name'], $userDest)`. Defend: `realpath() + str_starts_with($abs, $root)` prefix check.
- **SSRF** (CWE-918) — `curl_exec(CURLOPT_URL => $user)`, `file_get_contents($url)`, `fopen($url)` with `allow_url_fopen=On`. Needs host+scheme allowlist.
- **XSS** (CWE-79) — `echo $user`, `<?= $user ?>` without `htmlspecialchars($user, ENT_QUOTES, 'UTF-8')`. Twig `{{{{ var }}}}`, Blade `{{{{ $var }}}}`, Smarty autoescape by default. `{{{{{ $var }}}}}` (Blade raw) / `{{{{ var|raw }}}}` (Twig) turn it off.
- **Type juggling** (CWE-697) — `==` coerces: `"0e123" == "0e456"` = TRUE (both = 0), `"abc" == 0` = TRUE. Safe: `===`, `hash_equals()` for HMAC, `password_verify()` for passwords.
- **CSRF** (CWE-352) — missing token check. Laravel `VerifyCsrfToken` middleware, Symfony CSRF form component, WordPress `wp_verify_nonce` are default-on IF middleware groups are correct.
- **Mass assignment** (CWE-915) — Laravel Eloquent `$model->fill($request->all())` without `$fillable`/`$guarded`; Symfony Form without `allow_extra_fields => false`. Attacker sets `is_admin=1` via POST.
- **Open redirect** (CWE-601) — `header("Location: $user")` without allowlist.
- **Session fixation** (CWE-384) — missing `session_regenerate_id(true)` after login.
- **Weak crypto** (CWE-327, CWE-330) — `md5`, `sha1` for passwords (use `password_hash(PASSWORD_BCRYPT)`), `mcrypt_*` (removed in 7.2), `mt_rand()` for security tokens (use `random_bytes`/`random_int`).
- **WordPress-specific** — `$_GET`/`$_POST` without `sanitize_text_field`; `esc_sql` alone is insufficient (use `$wpdb->prepare`); `update_option($user_key)` without key validation.
- **Laravel-specific** — `DB::raw($user)`, `whereRaw($user)`, `selectRaw($user)` bypass parameterization; `Model::forceCreate` skips `$guarded`; `{{{{{ $var }}}}}` Blade unescape; `Route::any()` allowing unintended methods.

### Defenses you should expect (don't re-report as missing)

- **Framework CSRF** — Laravel, Symfony CSRF are default-on. Don't flag unless you point to `$except` in config or route outside middleware group.
- **ORM parameterization** — Eloquent, Doctrine parameterize by default. "SQL injection in `User::where('email', $email)`" is false positive.
- **Autoescape in templates** — Twig, Blade, Smarty escape `{{{{ }}}}` by default. Flag only `{{{{{ }}}}}`, `|raw`, or raw `echo`.
- **`password_hash`/`password_verify`** — bcrypt/argon2 with CSPRNG salts. Safe.
- **PDO prepared statements** — `prepare("... ?") + execute([$val])` is safe.
- **`htmlspecialchars` at echo sites** — covers common XSS surface.
- **PHP 8+ unserialize** — still unsafe by default (`allowed_classes=true`).

### Red flags

- `eval`, `assert($stringArg)` (PHP 7.x), `create_function` on user strings (CWE-94/95).
- `unserialize($_GET|$_POST|$_COOKIE|$_SESSION)` without `allowed_classes => false` (CWE-502).
- `include`/`require` with `$_*` fragment in path (CWE-98).
- `"...{$var}..."` to `mysqli_query`, `$pdo->query` (CWE-89).
- `$user_token == $known_token` with `==` not `===`/`hash_equals` (CWE-697).
- `md5($password)` / `sha1($password)` in users table (CWE-327).
- Direct `$_REQUEST` use without typing (combines GET+POST+COOKIE).
- `DB::raw($user)`, `whereRaw($user)`, `selectRaw($user)` (CWE-89).
- `{{{{{ $var }}}}}` Blade or `{{{{ var|raw }}}}` Twig with user value (CWE-79).
- `preg_replace("..."/e, ...)` in PHP 5.x (CWE-94).
- `move_uploaded_file($_FILES['f']['tmp_name'], $_POST['dest'])` without realpath (CWE-22).
- `.htaccess`/`php.ini`: `allow_url_include=On`, `register_globals=On`, `magic_quotes_gpc=On` (legacy).

### No need to re-report

- Missing `declare(strict_types=1)` — style only.
- `??` vs `isset()` — style.
- PHP version deprecation without CVE chain.
- Missing return type hints.
- `error_reporting(E_ALL)` — prod config, not vuln.
- Global vs namespaced functions unless it hides security.

### Authorization gates — by framework

In PHP web frameworks (Laravel, Symfony, Slim, plain PHP), routed
handlers follow the gate-on-handler authorization model: a state-changing
route without an auth gate is an `authorization`-category sink. The
framework patterns below describe what counts as a gate.

- **Laravel**: `Route::middleware('auth')->...` chain, route-group
  `Route::group(['middleware' => 'auth'], ...)`, controller constructor
  `$this->middleware('auth')`, or in-method `$this->authorize(...)` (Gate
  policy) / `Gate::allows(...)` checks. The `auth` middleware is registered
  by default. Custom middleware: anything implementing `handle($request, Closure $next)`
  that 401/403s.
- **Symfony**: `#[IsGranted('ROLE_USER')]` attribute on the action method,
  or `security.yaml` `access_control` rules covering the URL pattern. Older
  syntax: `@IsGranted` annotation. In-method: `$this->denyAccessUnlessGranted(...)`.
- **Slim**: middleware via `$app->add($authMw)` global, or
  `$app->group('/api', ...)->add($authMw)`, or per-route
  `$app->post('/x', $h)->add($authMw)`. In-handler: check
  `$request->getAttribute('user')` short-circuit.
- **Plain PHP**: explicit session check at top of script —
  `if (!isset($_SESSION['user'])) {{ http_response_code(401); exit; }}`.
  An entry script with no session check that performs writes → flag.

State-change idioms (the calls whose handlers need gating; pure-read GETs
only count when they expose cross-tenant data): Eloquent `->save()`,
`->delete()`, `->update()`, `Model::create()`, Doctrine
`$em->persist/remove/flush`, raw `PDO::exec`/`->query()` write-SQL,
`file_put_contents`, `fopen($f, 'w')`, `exec`/`shell_exec`/`system`/
`passthru`, `mail()`.

Gates may sit on the route definition, in route-group middleware, in the
controller constructor, or in project-wide config (`RouteServiceProvider`
for Laravel, `config/packages/security.yaml` `access_control` for
Symfony).
