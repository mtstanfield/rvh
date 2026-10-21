## Perl security review

Analyze Perl code for vulnerability classes. Perl's permissiveness (eval, backticks, runtime type coercion) creates dense footgun surfaces; modern code (`use strict; use warnings;`) is safer than legacy CGI.pm / Perl 4.

### Critical patterns

**Code injection (`eval` STRING form):**
- `eval "... $user ..."` executes string as code (CWE-95). `eval { BLOCK }` is exception handling, safe.

**Command injection:**
- Backticks `` `$cmd` ``, `qx/$cmd/` are shell-interpreted (CWE-78).
- `system($cmd)` single-arg → shell via `/bin/sh -c`. `system($cmd, @args)` list-form → direct exec (safe).
- `open(FH, "| $cmd")` or `open(FH, "$cmd |")` pipes shell. 3-arg `open(FH, "<", $file)` blocks `|` and `>` injection (CWE-78).

**Taint-mode bypass (CWE-20):**
- `-T` flag or `PERL_TAINT` env var activate taint tracking; untrust user data unless regex-captured: `$data =~ /^(\w+)$/; $clean = $1`. Catch-all untaint (`/(.*)/s`) is useless. Code without `-T` gets no protection.

**SQL injection (CWE-89):**
- `$dbh->do("SELECT ... '$user' ...")` string interpolation is unsafe. Use placeholders: `$dbh->prepare("... ?")` + `$sth->execute($user)` or `$dbh->selectrow_array("... ?", undef, $user)`. DBIx::Class parameterizes correctly.

**Path traversal (CWE-22):**
- `open(FH, "<$file")` 2-arg form allows `|`, `>`, `../` injection. Use 3-arg form + realpath + prefix check.

**Deserialization RCE (CWE-502):**
- `Storable::thaw`, `Storable::retrieve` can RCE on crafted data. `YAML::XS`/`YAML::Syck` unsafely parse type tags. Avoid on untrusted input.

**XSS in CGI output (CWE-79):**
- `print "Hello $user"` needs HTML escaping: `CGI::escapeHTML($user)` or `HTML::Entities::encode_entities`. Template::Toolkit requires `FILTER html` config; default is unsafe.

**HTTP response splitting (CWE-93/CWE-113) and open redirect (CWE-601):**
- `print "Location: $url\n\n"` with user-controlled `$url` allows `\r\n` injection (response splitting) or unvalidated redirect (open redirect). Validate URL, strip newlines.

**Regex DoS (CWE-1333):**
- `qr/$user/` (user-controlled pattern) or catastrophic backtracking (`(.*)+`). Validate and anchor user patterns.

**Other concerns:**
- **Symlink races (CWE-367):** `-e $file` then `open($file)` is TOCTOU. Use `File::Temp->new`.
- **Weak crypto (CWE-327, CWE-338):** `rand()` not CSPRNG for tokens; use `Crypt::URandom` or `/dev/urandom`. `Digest::MD5` / `Digest::SHA1` for passwords → use `Digest::BCrypt` or `Crypt::Argon2`.
- **CGI.pm anti-pattern:** `use CGI qw/:standard/; param()` values directly in `system`/`print`/`open` without sanitization.
- **Apache `mod_perl` handler reuse:** request globals leak across requests; reset state explicitly.
- **`local *` aliasing:** can cause unexpected scoping bugs; verify intent.
- **`do $file` / `require $file` with attacker paths:** evaluates file; requires path hardening.

### Safe patterns (don't re-flag)

- `use strict; use warnings;` — baseline hygiene.
- `#!/usr/bin/perl -T` or `-wT` — taint mode enabled; flag bypasses, not missing taint.
- 3-arg `open(FH, "<", $file)` — safe form.
- `DBI` placeholders (`prepare(...?...)` + `execute($val)`).
- `CGI::escapeHTML` or `HTML::Entities::encode_entities` in output.
- `File::Temp->new` — race-safe tempfiles.
- `Digest::BCrypt` / `Crypt::Argon2` — correct password hashing.

### Flagging checklist

- `eval "..."` with interpolated user-reachable variables (CWE-95).
- Backticks / `qx/.../` with `$user` or unvetted vars (CWE-78).
- 2-arg `open` with `$var` that could start with `|`, `>`, `<`, `>>`, `+` (CWE-22/CWE-78).
- `system("cmd $user")` single-arg (CWE-78).
- `$dbh->do("... $user ...")` or raw SQL without placeholders (CWE-89).
- `Storable::thaw($user_bytes)` on network input (CWE-502).
- `YAML::XS::Load($user)` without safe-mode (CWE-502).
- Missing `-T` on web request handlers.
- Over-permissive untaint regex (`/(.*)/s`, etc.).
- CGI/Plack/Mojolicious handlers: `param()` values in `print`, `system`, `open` without sanitization.
- `rand()` for session IDs or tokens (CWE-338).
- `Digest::MD5` or `Digest::SHA1` for password hashing (CWE-327).
