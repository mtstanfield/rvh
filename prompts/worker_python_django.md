### Detected framework: Django

Concrete gate symbols and gotchas that override the generic Python guidance.

**Function-based views**:
- `@login_required(login_url=...)` — auth-only gate
- `@permission_required("app.codename")` — auth + role
- `@user_passes_test(predicate)` — auth + custom predicate
- `@staff_member_required` — admin staff only

**Class-based views**:
- `LoginRequiredMixin` (auth) / `PermissionRequiredMixin` (auth + role) /
  `UserPassesTestMixin` (auth + predicate)
- Custom `dispatch()` override that 401/403s before calling super

**DRF (`rest_framework`)**:
- `permission_classes = [IsAuthenticated, ...]` on the viewset/view class
- Project-wide: `REST_FRAMEWORK = { "DEFAULT_PERMISSION_CLASSES": [...] }`
  in settings.py — read settings.py (`xref(REST_FRAMEWORK)` finds the
  block) to confirm before flagging an unannotated DRF view
- `@permission_classes([...])` decorator on `@api_view` function-based DRF

**URL-pattern gates count as gates**: `path("...", login_required(MyView.as_view()))`
in urls.py wraps the view — walk `urls.py` (`xref(login_required)` /
`xref(permission_required)` find every URL-level wrapper) for
wrappers around the view name before returning a verdict.

**Don't conflate with CSRF**: `@csrf_exempt` removes CSRF protection but
does NOT remove auth — they're separate concerns. Flag missing auth as
its own finding; flag `csrf_exempt` on a state-change view as a separate
CSRF finding.

**State-change idioms** (gate-required): `.save()`, `.delete()`,
`.update()`, `bulk_create`, `bulk_update`, `cursor.execute(<write-SQL>)`,
file writes, `subprocess.*`, `send_mail`, `request.session[X] = ...`.

**Anchor-grounding for these findings**: sink_location is `<view file>:<line of `def viewname` or `class ViewName`>`. Snippet:
`"def <name>(...) // missing auth gate"` or `"class <Name>(View) // missing LoginRequiredMixin"`.

**Enumerate then verify (don't grep-and-stop).** Walk every routed view in
the file: every `def <name>(request, ...)` / `class <Name>(View|...)` that
appears in `urls.py`. For each, verify the gate. Don't search only for
"commented-out decorator" or other known-bad patterns — that misses every
pattern not in your list.

**Decorator-call without `@` is a missing gate, not a present one.** Watch
for expression-statement decorator constructions:

```
login_required(login_url="login")     ← no `@` → no-op expression
def sensor_import(request):           ← unguarded view
```

`login_required(login_url="login")` without a leading `@` constructs the
decorator and immediately discards the result — it does NOT decorate the
following function. The view is unguarded. Same trap with
`permission_required(...)`, `user_passes_test(...)`, `staff_member_required(...)`,
`@method_decorator(login_required)` written without the `@`, and any
custom auth decorator. Grep `^@?(login_required|permission_required|user_passes_test|staff_member_required)` and
flag every match where the leading `@` is absent.
