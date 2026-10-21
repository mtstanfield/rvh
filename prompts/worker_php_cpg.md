### CPG usage patterns — PHP

**Starting investigation:** `xref(name)` for each dangerous call: `eval`,
`system`, `exec`, `shell_exec`, `passthru`, `popen`, `proc_open`,
`include`, `require`, `include_once`, `require_once`,
`file_get_contents`, `mysql_query`, `mysqli_query`, `unserialize`,
`assert`, `preg_replace`, `create_function`, `readfile`, `fopen`, `fwrite`.

**`reachable` sources_hint by framework:**
- Plain PHP: `\$_GET|\$_POST|\$_REQUEST|\$_COOKIE|\$_FILES|\$_SERVER|file_get_contents`
- Laravel: `request\(\)|Input::get|Request::input|Request::get|Request::all`
- Symfony: `Request::get|Request::request|\$request->request`
- WordPress: `\$_REQUEST|sanitize_text_field|wp_verify_nonce`

**Key sinks:**
- RCE — `eval($x)`, `system($x)`, `exec($x)`, `shell_exec($x)`, `passthru($x)` inject if `$x` is user input.
- LFI/RFI — `include($x)` / `require($x)` with user-controlled path; check `allow_url_include`.
- SQL injection — `mysql_query($sql)`, `mysqli_query($link,$sql)`, raw PDO `->query($sql)` without prepare.
- Deserialization — `unserialize($x)` on untrusted input → object-injection RCE via `__destruct`/`__wakeup`/`__toString`.

**Blind spots:** `$$var`, `call_user_func`, magic methods, traits,
autoloading, Laravel service-container routing — rely on `xref` + reading.
