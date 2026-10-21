### Detected framework: Rails

Gate via `before_action` filters — declared at controller class level,
inheritable through `ApplicationController`.

**Common gates**:
- `before_action :authenticate_user!` — Devise; redirects/401s on miss
- `before_action :require_login` — custom equivalent
- `before_action :authorize!` — Pundit/CanCanCan
- `before_action :authenticate_admin!` — namespaced auth scopes

**Per-action scoping**:
- `before_action :authenticate_user!, only: [:create, :update, :destroy]` —
  protects only those actions
- `before_action :authenticate_user!, except: [:index, :show]` — excludes reads

**Skip signals (red flags)**:
- `skip_before_action :authenticate_user!, only: [:create]` — explicit
  removal. If the action performs writes, this is suspicious.
- `protect_from_forgery with: :null_session` is CSRF, not auth — separate
  concern.

**In-body alternatives that count as gates**:
- Pundit: `authorize @resource` (raises NotAuthorizedError on miss)
- CanCanCan: `authorize! :update, @resource`
- Devise: `redirect_to new_user_session_path unless current_user`

**Inheritance matters**: a controller's effective gate set is its own
`before_action` calls PLUS those on every parent up to
`ApplicationController`. Read `app/controllers/application_controller.rb`
and any namespaced base (`Admin::BaseController`,
`Api::V1::BaseController`) — `xref(before_action)` finds every filter
declaration across the ancestry — when verifying. A controller is
unguarded only if no ancestor declares a covering filter that isn't
`skip_before_action`-ed for the action.

**State-change idioms** (gate-required): ActiveRecord `.save`, `.update`,
`.destroy`, `.create`, `.update_all`, `.delete_all`,
`ActiveRecord::Base.connection.execute(<write SQL>)`, `File.write`,
`system`/`` ` ``/`exec`, `Net::HTTP.post/put/delete`, `Rails.cache.write`
to shared cache.

**Action locations**: controller class methods are actions; `routes.rb`
maps URL → `controller#action`. The action method's enclosing class +
ancestors define the gate set.
