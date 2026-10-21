### Detected framework: Symfony

Gate via `security.yaml` access_control rules + per-action attributes.

**`security.yaml`** (`config/packages/security.yaml`):
- `access_control` block defines URL-pattern → role rules:
  ```
  access_control:
    - {{ path: ^/admin, roles: ROLE_ADMIN }}
    - {{ path: ^/api, roles: IS_AUTHENTICATED_FULLY }}
  ```
- A path pattern with `roles: PUBLIC_ACCESS` (or absence of any rule) is
  unauthenticated.
- Read `security.yaml` to verify — it gates by URL pattern, so an
  unannotated controller may be covered there.

**Per-action attributes** (modern PHP 8 attribute syntax):
- `#[IsGranted('ROLE_USER')]` on the action method
- `#[IsGranted(new Expression('is_granted("ROLE_USER")'))]`
- Class-level: `#[IsGranted('ROLE_USER')]` on the controller class

**Annotation syntax** (older codebases):
- `@IsGranted("ROLE_USER")` doc-comment annotation — same semantics

**In-method**:
- `$this->denyAccessUnlessGranted('ROLE_USER')` — throws AccessDeniedException
- `if (!$this->isGranted('edit', $post)) {{ throw new AccessDeniedException(); }}`

**Anti-rules**:
- `firewalls:` config in security.yaml defines authentication mechanisms
  (form_login, jwt) but not authorization — even with a firewall configured,
  a route without `access_control` or `#[IsGranted]` is reachable by
  authenticated users with NO role check (read the firewall's
  `provider`/`access_decision_manager` settings in `security.yaml` to
  confirm).
- `@Security("...")` (older Sensio annotation) is similar to `#[IsGranted]`
  but uses ExpressionLanguage — count it as a gate.

**State-change idioms** (gate-required): Doctrine
`$em->persist/remove/flush`, `$em->createQueryBuilder()->update/delete`,
`Connection::executeStatement(<write SQL>)`, `Filesystem::dumpFile`,
`Process::start`, mailer sends, session writes.

**Action locations**: controller methods bound via `#[Route(...)]`
attribute or `routing.yaml`. Walk the chain (`xref(IsGranted)` finds
attribute hits): security.yaml access_control → controller class
attribute → method attribute → in-body check. The action is unguarded
only if none of those covers the path.
