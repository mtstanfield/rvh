## Python-specific review notes

Analyze Python code. Distinguish between idiomatic-language bugs and defenses built into frameworks (Django, Flask, FastAPI, SQLAlchemy).

### Common bug classes in Python

- **Deserialization** (CWE-502) — `pickle.loads`, `marshal.loads`, `shelve.open`, `dill.loads` on untrusted input are near-automatic RCE. `yaml.load` with any loader other than `SafeLoader` / `yaml.safe_load` (including `FullLoader`) is a deserialization candidate worth flagging — but its exploitability and severity depend on the loader and the installed PyYAML version, so surface it and let the reviewer judge the version-specific impact rather than assuming RCE.
- **Command injection** (CWE-78) — `os.system(user)`, `subprocess.run(cmd, shell=True)` with user-controlled substrings, `os.popen`, `eval`/`exec` on user input. Safe: `subprocess.run([cmd, arg1])` with default `shell=False`.
- **SQL injection** (CWE-89) — string-interpolated queries (`f"SELECT {{user}}"`, `.format()`, `%`) to `cursor.execute()`. Safe: `cursor.execute("... %s ...", (user,))`, SQLAlchemy `.filter(Model.col == user)`, Django ORM `.filter(col=user)`.
- **Path traversal** (CWE-22) — `open(user_path)`, `pathlib.Path(user_path).read_text()`, Flask `send_file(user_path)`. Defend: `os.path.realpath()` + prefix-check.
- **SSTI** (CWE-1336 / CWE-94) — `jinja2.Template(user).render()`. Safe: `render_template("fixed.html", context=user)`.
- **XXE** (CWE-611) — `xml.etree.ElementTree.fromstring()`, `lxml.etree.parse()` without `resolve_entities=False`. Unsafe by default.
- **SSRF** (CWE-918) — `requests.get(user_url)`, `urllib.request.urlopen(user_url)`. Needs URL allowlist.
- **Timing side-channels** (CWE-208) — `secret == user_hmac`. Safe: `hmac.compare_digest()`.
- **Flask debug mode / Werkzeug console** (CWE-489) — `app.run(debug=True)` in production. RCE-by-design.
- **Django** (CWE-89 / CWE-352 / CWE-79) — `raw()` / `extra()` bypass ORM; `mark_safe()` undoes autoescape; `csrf_exempt` on state-change.
- **Flask** (CWE-22 / CWE-1336 / CWE-352) — `send_file()` path traversal (v2+ fixed), `render_template_string()` SSTI, missing Flask-WTF CSRF.
- **FastAPI** (CWE-918) — Pydantic validates type, not intent. A `HttpUrl` is still attacker-controlled (SSRF surface remains).
- **Unsafe identity binding** (CWE-287 / CWE-345) — account takeover by construction. Pattern: a routed handler receives an `email` or `username` from POST/form data, then calls `User.objects.get_or_create(username=<that value>)` or `update_or_create(...)`, then mutates the returned user (`set_password`, assignments to `is_staff` / `is_active` / `groups`, `set_unusable_password`) without first verifying the caller is the existing account's owner. Whether the account existed or was just created, the attacker now controls it. The "feature" is anonymous account registration; the bug is letting that same path overwrite existing accounts. An auth gate on the handler is necessary but not sufficient — gated identity-binding can still let user A take over user B's account if the lookup key isn't tied to A's session.
- **Mass assignment from request data** (CWE-915) — `for k, v in form.cleaned_data.items(): setattr(obj, k, v)` or `Model.objects.create(**request.POST)` lets the client set ANY field on the model, including `is_staff`, `is_superuser`, `is_active`, `groups`, `user_permissions`. Safe: ModelForm `Meta.fields` allowlist and explicit per-field assignment.
- **Privilege escalation via field assignment** (CWE-269) — any routed handler writing to `is_staff`, `is_superuser`, `is_active`, `user.groups`, `user.user_permissions`, or `request.user = ...` with a value sourced from form/POST data. Even with an auth gate on the handler, the gate doesn't restrict what the gated user can set on themselves or others.

### Defenses you should expect to find (don't re-report as missing)

- **Framework CSRF** — Django `CsrfViewMiddleware`, Flask-WTF `CSRFProtect` are default-on. Flag only explicit `csrf_exempt` or missing registration.
- **ORM parameterization** — SQLAlchemy, Django ORM parameterize by default. "SQL injection in `Model.filter()`" is almost always false.
- **Framework autoescape** — Jinja2 `autoescape=True` (Flask default), Django templates autoescape-on. Flag only with `|safe`, `mark_safe`, `autoescape off`.
- **subprocess argv list** — `subprocess.run([cmd, arg1])` without `shell=True` is safe by default.
- **Pydantic validation** — typed fields reject wrong types. Don't flag "type confusion" on validated input.

### Red flags that warrant a real investigation

- ANY `pickle` / `shelve` / `dill` on network input — RCE (CWE-502)
- `eval`, `exec`, `compile` on user strings (CWE-95)
- `subprocess.run(..., shell=True)`, `os.system()` — check command construction (CWE-78)
- `cursor.execute(f"...{{var}}...")` f-string queries — ORM bypass (CWE-89)
- `yaml.load()` with any loader other than `Loader=yaml.SafeLoader` (or `yaml.safe_load(...)`) — deserialization candidate (CWE-502). `FullLoader` is not equivalent to `SafeLoader`, but whether it is actually exploitable is PyYAML-version-dependent — flag it for review, don't assume RCE.
- `requests.get(url, verify=False)` — broken TLS (CWE-295)
- Custom crypto instead of `cryptography` lib — weak implementation
- Hard-coded `SECRET_KEY` — session forgery (CWE-798)
- `User.objects.get_or_create(username=<request-derived value>)` followed by `set_password()` or any field mutation — unsafe identity binding / account takeover (CWE-287). The lookup key (`username` / `email`) MUST come from the authenticated session, not from the request body, when the handler then writes credentials.
- `setattr(obj, key, value)` in a loop over `request.POST` / `form.cleaned_data.items()` / `request.data.items()` — mass assignment (CWE-915). Look for the iteration shape; the loop is the red flag, not any one setattr.
- Direct write to `is_staff`, `is_superuser`, `is_active`, `user.groups`, `user.user_permissions` from form/POST data — privilege escalation (CWE-269). Flag even when the handler has an auth gate; the gate doesn't restrict the *content* the gated user can set.

### CPG enumeration queries (Python)

When CPG is available, these are the canonical query sequences for
the language's most common sink shapes. Run the **full battery** on a
routed-handler anchor **BEFORE any `final_output`** — not just before
no-finding. Surfacing one `eval()` or one `subprocess(shell=True)`
does not tell you whether `get_or_create + set_password`, a `setattr`
loop over `request.POST`, or a write to `is_staff` also exists in a
different handler in the same file. The battery is a checklist;
emit one `final_output` covering everything it surfaced.

- **Command injection (CWE-78):** `xref(Popen)`, `xref(run)`,
  `xref(system)`, `xref(call)`, `xref(popen)`. For each hit,
  `inspect` to check whether `shell=True` is set or the argv is a
  user-controlled string.
- **Eval/exec (CWE-95):** `xref(eval)`, `xref(exec)`, `xref(compile)`.
  Any hit on user-derived input is a finding.
- **SQL injection (CWE-89):** `xref(execute)`, `xref(executemany)`,
  `xref(raw)`, `xref(extra)`. For each hit, `inspect` to confirm the
  query string is f-string / `%` / `.format()` constructed rather
  than parameterized.
- **Deserialization (CWE-502):** `xref(loads)` (pickle), `xref(load)`
  + `xref(load_all)` (yaml/marshal). For yaml hits, `inspect` the
  call args and flag anything not `Loader=SafeLoader` / `yaml.safe_load`
  (including `FullLoader`); its severity is PyYAML-version-dependent, so
  leave the exploitability call to review.
- **Unsafe identity binding (CWE-287):** `xref(get_or_create)` and
  `xref(update_or_create)`. For each hit, `inspect` to see whether
  the lookup-key argument is request-derived (`username=email`,
  `username=request.POST['email']`, etc.). If yes, then `xref(set_password)`
  within the same enclosing function — that pair is the finding.
  Mutation through field assignment or `set_unusable_password` after
  the lookup is equally damning.
- **Mass assignment (CWE-915):** `xref(setattr)`. The red flag is
  `setattr` inside a `for` loop iterating `request.POST`,
  `request.data.items()`, `request.json.items()`, or
  `form.cleaned_data.items()` — `inspect` each hit to check the
  enclosing loop. Also `xref(create)` / `xref(update)` on Django
  ORM with `**request.POST` / `**request.data` argument splat.
- **Privilege escalation (CWE-269):** `xref(is_staff)`,
  `xref(is_superuser)`, `xref(is_active)`. For each hit, `inspect`
  to see whether it's a write (`user.is_staff = ...`) and whether
  the RHS traces to request data — use `paths(<request source>, <write>)`.
  Also check `xref(groups)` and `xref(user_permissions)` for direct
  membership writes.
- **Path traversal (CWE-22):** `xref(open)`, `xref(read_text)`,
  `xref(send_file)`. For each hit, `inspect` the path argument.
- **SSRF (CWE-918):** `xref(get)`, `xref(post)`, `xref(urlopen)`.
  For each hit, `inspect` the URL argument.
- **Missing authorization (CWE-862):** start with
  `entrypoints(file=<anchor>)`. Every entry without a gate decorator
  AND without an in-body `request.user.is_authenticated` check is a
  candidate. Cross-check with `xref(login_required)` and
  `xref(permission_required)` for decorator-call-without-`@` traps.

Each query is one tool call. Treat the battery as a checklist; the
correct enumeration is to run it, not to eyeball the file for one
shape and stop.

### No need to re-report

- Missing type hints — not a vulnerability
- `requests` without timeout — DoS on caller, not remote exploit
- Deprecated APIs (`datetime.utcnow()`) — not security

### Authorization gates — by framework

In Python web frameworks (Django, Flask, FastAPI, etc.), routed handlers
follow the gate-on-handler authorization model: a state-changing handler
reachable from routing without an auth gate is an `authorization`-category
sink. The framework symbols below describe what counts as a gate.

- **Django (function views)**: `@login_required`, `@permission_required("app.code")`,
  `@user_passes_test(...)`, `@staff_member_required`. Class-based views: mixin
  on the class — `LoginRequiredMixin`, `PermissionRequiredMixin`,
  `UserPassesTestMixin`, or an explicit `dispatch()` override.
- **Django REST Framework**: `permission_classes = [IsAuthenticated, ...]` on
  the viewset/view class; project-wide `DEFAULT_PERMISSION_CLASSES` in
  `REST_FRAMEWORK` settings counts as a baseline gate (verify in settings.py).
- **Flask**: `@login_required` from Flask-Login, `@auth.login_required` from
  Flask-HTTPAuth, custom decorators that compose these, or
  `before_request`/`before_app_request` handlers that 401/403 unauthenticated
  callers.
- **FastAPI**: `Depends(get_current_user)` / `Depends(authenticate)` /
  `Security(...)` parameter on the path operation function, or a
  router-level dependency (`APIRouter(dependencies=[Depends(...)])`) /
  app-level dependency (`FastAPI(dependencies=[...])`).
- **Tornado**: `@authenticated` decorator, or an authenticating `prepare()`
  override on the RequestHandler.
- **In-body checks**: `if not request.user.is_authenticated: return ...`
  early in the body counts as a gate.

State-change idioms (the calls whose handlers need gating; pure-read GETs
only count when they expose cross-tenant data): `.save()`, `.delete()`,
`.update()`, `bulk_create`, `bulk_update`, `cursor.execute(<write SQL>)`,
file writes, `subprocess.*`, `send_mail`, `requests.post/put/delete`,
session/cookie writes.

Gates may live in multiple places: on the handler (`@login_required`,
mixins), on the URL pattern (`path("...", login_required(view))`), or as
a project-wide default (DRF `DEFAULT_PERMISSION_CLASSES`,
`MIDDLEWARE = [...AuthenticationMiddleware...]` paired with a deny-by-default
view base).

**Decorator-call without `@` is NOT a gate.** A line like
`login_required(login_url="login")` directly above `def view(request):`
WITHOUT a leading `@` is a Python expression statement that constructs the
decorator and discards the return value — it does NOT decorate the
following function. Same trap for `permission_required(...)`,
`user_passes_test(...)`, `staff_member_required` (called as `staff_member_required(...)`),
Flask `login_required(...)`, FastAPI dependency wrappers, etc. When you
walk a view file, ripgrep both `^@?(login_required|permission_required|...)\b`
patterns; lines matching without the leading `@` are missing-gate findings,
not gated handlers. Anchor sink_location to the `def` / `class` line, not
the no-op decorator-call line above it.

**Enumerate every routed handler, then check each.** Don't grep for
known-bad patterns (commented-out decorators, etc.) and stop. The full
sweep is: list every `def <name>(request, ...)` / `class <Name>(View|...)`
that is reachable from a URL pattern, then for each one verify a gate
mechanism applies (decorator, mixin, URL-wrapper, project-wide default,
or in-body check). Anything without a gate is a missing-auth finding.
The grep-for-bad-patterns approach misses any pattern not in the list.
