### Detected framework: Next.js

Three coexisting routing surfaces — each has its own gate idiom.

**App-router route handlers** (`app/api/*/route.ts`, `app/api/*/route.js`):
- `auth()` from NextAuth at top of handler — `const session = await auth(); if (!session) return new Response("Unauthorized", { status: 401 });`
- Wrapped via `withAuth(handler)` (NextAuth middleware helper) or a custom
  HOF
- `getServerSession(authOptions)` for older NextAuth — same pattern

**Pages-router API routes** (`pages/api/**/*.ts`):
- `getServerSession(req, res, authOptions)` at top of handler
- Custom HOF wrapping: `export default withAuth(async (req, res) => {...})`
- In-handler `req.headers.authorization` parse + 401

**Middleware** (`middleware.ts` / `middleware.js` at app root or
`src/middleware.ts`):
- `export const config = { matcher: ['/api/:path*'] }` defines which paths
  the middleware runs on
- The middleware function itself returns `NextResponse.next()` to continue
  or `NextResponse.redirect(new URL('/login', req.url))` to gate
- Read the matcher CONFIG (top of `middleware.ts`) to verify — a
  permissive matcher with conditional in-body checks is the common gate

**Server actions** (`'use server'` functions in app router):
- `auth()` / `getServerSession()` at top of action body — server actions
  run on POST and ARE state-changing surfaces
- Action without an auth check + DB write → flag

**Anti-rules**:
- Pages-router `_middleware.ts` was deprecated; check `middleware.ts` at
  root.
- A page route (`app/x/page.tsx`) is rendering, not a state-changing API.
  Server components read but rarely write — focus on `route.ts` files and
  server actions.

**State-change idioms** (gate-required): Prisma `.create/update/delete`,
TypeORM/Drizzle writes, raw DB writes, server-side `fetch` mutations to
other services, `cookies().set(...)` / `headers().append(...)` writes,
`fs.writeFile`.

**Route locations**: routes live under `app/` (app router) or
`pages/api/` (pages router); middleware lives at the project root in
`middleware.ts`. Read root-level `middleware.ts` when verifying —
middleware can gate routes file-by-file through the matcher config.
