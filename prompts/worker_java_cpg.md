### CPG usage patterns — Java

**Starting investigation:** `xref(name)` for each dangerous call:
`Runtime.exec`, `ProcessBuilder`, `Statement.execute`,
`Statement.executeQuery`, `PreparedStatement` (concatenated SQL),
`ObjectInputStream.readObject`, `XMLDecoder.readObject`,
`Jackson.readValue` (polymorphic types), `SnakeYAML.load`,
`Method.invoke`, `Class.forName`, `Naming.lookup`, `FileInputStream`.
Use the short method name; qualify only if `xref` returns too many matches.

**`reachable` sources_hint by framework:**
- Servlet: `HttpServletRequest|getParameter|getHeader|getQueryString|getCookies`
- Spring MVC: `@RequestParam|@PathVariable|@RequestBody|@RequestHeader` and `request\.getParameter`
- JAX-RS: `@QueryParam|@PathParam|@FormParam|@HeaderParam|@BodyParam`

**Key sinks:**
- Deserialization — `ObjectInputStream.readObject` without class-whitelist = RCE; `reachable` from request-read.
- Command injection — `Runtime.exec(cmd)` / `ProcessBuilder(cmd).start()` from user input; `paths` from `getParameter` to `exec`.
- SQL injection — `Statement.execute*` with concatenated SQL; `PreparedStatement` safe only with bound params (`setString`/`setInt`), not concatenation.

**Blind spots:** Spring autowiring, AOP proxies, reflection, and
compiled-resource class loading are opaque. If `callers` is empty on
a Spring `@Service` chain, trust `xref` + reading over CPG silence.
