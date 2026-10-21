## Java-specific review notes

Analyze Java code for idioms of the platform and frameworks (Spring, Spring Security, Hibernate/JPA, Jackson) that provide defenses you shouldn't re-report as missing.

### Common bug classes in Java server code

- **Deserialization** — `ObjectInputStream.readObject()` on untrusted bytes is RCE via gadget chains. Jackson with `enableDefaultTyping()` or `@JsonTypeInfo` lets attackers instantiate arbitrary classes. SnakeYAML default constructor calls constructors during parse. Apache Commons `ConvertUtils.convert` with class string. CWE-502.
- **SQL injection** — `Statement.executeQuery("...")` with string concatenation, `String.format` / `+` into raw SQL, Hibernate `createQuery("from User where name = '" + n + "'")`, JPA `createQuery(String)` without `setParameter`. Safe: `PreparedStatement`, `?` placeholders, `setParameter(name, val)`, Hibernate `:name` params, Spring Data repos, QueryDSL. CWE-89.
- **Command injection** — `Runtime.exec(String)` parses whitespace — any space splits args unpredictably. `ProcessBuilder("sh", "-c", userInput)` is shell. Safe: `ProcessBuilder` argv list without shell wrapper. CWE-78.
- **Path traversal** — `new File(userPath)`, `Paths.get(userPath)`, `FileInputStream(userPath)`, `MultipartFile.getOriginalFilename()` as destination. Defense: `Paths.get(base).resolve(user).normalize().startsWith(base)` + `.toRealPath()` for symlinks. CWE-22.
- **SSRF** — `URL.openStream`, `HttpURLConnection`, `HttpClient`, Apache HttpClient, OkHttp, Spring `RestTemplate`/`WebClient`, Feign, webhook callers. Need scheme + host allowlist (blocklists fail: DNS rebinding, IPv6, decimal IPs). CWE-918.
- **XXE / XML parsing** — `DocumentBuilderFactory`, `SAXParserFactory`, `XMLInputFactory`, `SAXBuilder`, `TransformerFactory`, `SchemaFactory`, `XPathFactory`, `Unmarshaller` allow external entities by default in older JDKs. Modern secure: disable doctype decl + external entities + set `XMLConstants.FEATURE_SECURE_PROCESSING`. CWE-611.
- **SAML signature wrapping / JWT** — missing signature check, wrong element verified, accept "none" alg, unverified claims for auth. Jwts.parser without `.setSigningKey(...)` accepts ANY token. Algorithm confusion. CWE-347, CWE-287.
- **LDAP injection** — `DirContext.search(base, "(&(uid=" + user + ")(...))")` with concat. Safe: `LdapQueryBuilder` or `EncoderUtils.encodeFilter(user)`. CWE-90.
- **SpEL / EL / OGNL injection** — `@Value` on user input, `ExpressionParser.parseExpression(userString).getValue()`, Struts OGNL. CWE-917.
- **SSTI** — Velocity `Velocity.evaluate(...user...)`, Freemarker user template text, Thymeleaf `th:utext` (unescaped) with user input. CWE-1336.
- **Template autoescape bypass** — JSP `<%= user %>` without `<c:out value="${{{{{user}}}}}">`, Thymeleaf `th:utext` vs `th:text`, Mustache `{{{{{{{{triple}}}}}}}}` vs `{{{{{{double}}}}}}`. CWE-79.
- **CSRF** — Spring Security CSRF default-on since 4.x. `http.csrf().disable()` turns it off. CWE-352.
- **Authorization** — missing `@PreAuthorize` / `@Secured` / `@RolesAllowed` on endpoints. `@PreAuthorize("permitAll")` is explicit downgrade. Method-level only works with `@EnableMethodSecurity`. CWE-862, CWE-285.
- **Timing side channels** — `String.equals()` on secrets, `Arrays.equals(byte[],byte[])` for HMAC/tokens. Safe: `MessageDigest.isEqual(a,b)`. CWE-208.
- **Weak crypto** — MD5/SHA-1 for integrity. `Cipher.getInstance("AES")` defaults to ECB on many JVMs — specify mode+padding (`AES/GCM/NoPadding`). `new Random()` / `Math.random()` for tokens (use `SecureRandom`). `setSeed(constant)` defeats it. Hardcoded keys/IVs. CWE-327, CWE-328, CWE-338, CWE-798.
- **Race conditions** — shared `HashMap` across threads (use `ConcurrentHashMap`). `SimpleDateFormat` NOT thread-safe — static instances corrupt security parsing. Double-checked locking without `volatile`. CWE-362.
- **Reflection / class loading** — `Class.forName(userString)`, `method.invoke(obj, userString)` loads attacker classes. CWE-470.
- **Insecure TLS** — `TrustManager` with empty `checkServerTrusted`, `HostnameVerifier` returning `true` always, `HttpsURLConnection.setDefaultHostnameVerifier(ALLOW_ALL)`. CWE-295, CWE-297.
- **Regex DoS** — `Pattern.compile(user)` or catastrophic-backtracking regex on long input. CWE-1333.

### Defenses you should expect to find (don't re-report as missing)

- **Spring Security CSRF / method-security** — default-on with `spring-boot-starter-security`. Don't flag missing CSRF unless you find `csrf().disable()` or custom `SecurityFilterChain` omitting it.
- **`PreparedStatement` + `?` placeholders** — standard JDBC parameterization. Setters are safe.
- **`HttpSecurity.authorizeHttpRequests().anyRequest().authenticated()`** — catches unmatched routes. Read the security config (`xref(SecurityFilterChain)`) when verifying.
- **Modern JAXP secure defaults** — JDK 17+ has XXE mitigations on by default. Still verify in the specific path.

### Red flags that warrant investigation

- `ObjectInputStream.readObject()` on network bytes or user uploads (CWE-502)
- `Runtime.exec(userString)` or `ProcessBuilder("sh","-c", userString)` (CWE-78)
- `Class.forName(userString)` or `method.invoke(obj, userArgs)` (CWE-470)
- `DocumentBuilderFactory.newInstance()` without any `setFeature`/`setProperty` (CWE-611)
- Jwts.parser without `.setSigningKey(...)` or alg verification (CWE-347)
- `ObjectMapper.enableDefaultTyping()` or `@JsonTypeInfo` with CLASS type (CWE-502)
- `@PreAuthorize("permitAll")` or `http.csrf().disable()` on state-changing operations
- `Cipher.getInstance("AES")` without mode+padding (defaults to ECB, CWE-327)
- `TrustManager` / `HostnameVerifier` blanket-accepting all certs (CWE-295)
- `String.equals(hmac1, hmac2)` on signature verification (CWE-208)
- `SimpleDateFormat` as static field used across threads
- Spring Boot Actuator endpoints unprotected in production
- `URL.openStream` or `HTTP.Get` with user-controlled scheme+host (CWE-918)

### No need to re-report

- Missing `final` on fields — style only
- Package-private access (missing public) — not a vulnerability
- Missing Javadoc or style complaints
- `Logger.debug` on non-secret values
- `@Deprecated` APIs without specific CVE chain
- Missing try-with-resources on `Scanner` — leak, not vuln unless DoS-reachable

### Authorization gates — by framework

In Java web frameworks (Spring, Spring Security, Jakarta EE, JAX-RS),
routed handlers follow the gate-on-handler authorization model: a
state-changing endpoint without an auth gate is an `authorization`-category
sink. The framework patterns below describe what counts as a gate.

- **Spring Security** (most common): method annotations `@PreAuthorize("...")`,
  `@PostAuthorize("...")`, `@Secured("ROLE_X")`, `@RolesAllowed("...")`,
  class-level versions of the same. Plus `SecurityFilterChain` /
  `WebSecurityConfigurerAdapter` defining `authorizeHttpRequests` /
  `authorizeRequests` for URL-pattern-level rules — a `requestMatchers("/api/**").authenticated()`
  block gates every method matching the pattern. Gates can live on the
  method, on the controller class, or in a `*SecurityConfig.java` /
  `SecurityFilterChain` bean — walk all three (find configs via
  `xref(SecurityFilterChain)`).
- **Spring MVC (no Security)**: handlers protected by servlet filters
  registered via `FilterRegistrationBean` or `web.xml`. Filter must run
  before the handler.
- **Jakarta EE / Servlet**: `@RolesAllowed("...")` on EJB methods, or
  `web.xml` `<security-constraint>` / `<auth-constraint>` for URL patterns.
  Servlet filters with `doFilter` short-circuiting unauthenticated
  requests.
- **JAX-RS (Jersey/Resteasy)**: `@RolesAllowed`, `@DenyAll`, `@PermitAll`
  on resource methods, or `ContainerRequestFilter` registered via
  `@Provider` enforcing auth.
- **In-method**: `SecurityContextHolder.getContext().getAuthentication()`
  followed by a 401/403 short-circuit counts as a gate.

State-change idioms (the calls whose handlers need gating; pure-read GETs
only count when they expose cross-tenant data): JPA
`entityManager.persist/merge/remove`, Spring Data
`repository.save/delete/deleteAll`, raw JDBC writes, `Files.write`,
`Process.start`/`Runtime.exec`, network mutation.

Gotcha: `@PreAuthorize` is no-op without `@EnableMethodSecurity` (or older
`@EnableGlobalMethodSecurity(prePostEnabled = true)`) somewhere in the
config — check `xref(EnableMethodSecurity)` / `xref(EnableGlobalMethodSecurity)`
when the annotation is the only visible gate. If neither activation
annotation is present anywhere, the `@PreAuthorize` is decoration only.
