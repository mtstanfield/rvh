### Detected framework: Flask

Gate symbols specific to Flask + common extensions.

**Decorators**:
- `@login_required` from Flask-Login — auth only
- `@auth.login_required` from Flask-HTTPAuth — basic/digest/token
- `@jwt_required` from Flask-JWT-Extended — JWT
- `@roles_required("admin")` from Flask-Security
- Custom decorators wrapping these

**App / blueprint level**:
- `before_request` / `before_app_request` handler that 401/403s on miss —
  e.g. `@app.before_request def require_login(): if not session.get('user'): abort(401)`
- Blueprint-level `@bp.before_request` covers all routes in that blueprint

**In-body**: `if not session.get('user_id'): abort(401)` at the top of
the route counts as a gate.

**Anti-rules**:
- Flask-WTF CSRF is a separate concern — `@csrf.exempt` is not an auth bypass.
- `@app.route` itself is NOT a gate. Routes default to public.
- Pydantic / marshmallow validation is not auth — flagging "no auth" on a
  validated handler is still correct if the validator only checks shape.

**State-change idioms** (gate-required): SQLAlchemy
`db.session.add/.delete`, `Model.query.update`, raw
`db.engine.execute(<write-SQL>)`, `open(f, 'w')` writes, `subprocess.*`,
`send_mail` (Flask-Mail).

**View locations**: Flask views are functions decorated with
`@app.route(...)`, `@bp.route(...)`, or registered via
`app.add_url_rule(...)`. The decorator stack is where gates live.

**Decorator-call without `@` is a missing gate.** A line like
`login_required()` directly above `def view():` WITHOUT a leading `@`
constructs the decorator and discards the return value — it does NOT
decorate the function. The view is unguarded. Grep `^@?(login_required|jwt_required|roles_required|auth\.login_required)` and flag every
match where the leading `@` is absent.

**Enumerate-then-verify**: walk every `@app.route` / `@bp.route` /
`add_url_rule` registration, then for each verify a gate decorator,
`before_request` handler, or in-body check applies. Don't grep only for
known-bad patterns and stop.
