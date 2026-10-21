### Detected framework: Spring (Spring Boot / Spring Security)

Gate via Spring Security — declared globally in `SecurityFilterChain` /
`WebSecurityConfigurerAdapter` and refined per-method via annotations.

**Method-level annotations**:
- `@PreAuthorize("hasRole('USER')")` / `@PreAuthorize("isAuthenticated()")`
- `@PostAuthorize("returnObject.owner == authentication.name")`
- `@Secured("ROLE_USER")`
- `@RolesAllowed("USER")` (JSR-250)
- Class-level versions on the controller class apply to every method

**Global URL-pattern config**:
- `@Configuration class SecurityConfig` defining a `SecurityFilterChain`
  bean:
  ```
  http.authorizeHttpRequests(auth -> auth
      .requestMatchers("/api/admin/**").hasRole("ADMIN")
      .requestMatchers("/api/**").authenticated()
      .anyRequest().permitAll()
  );
  ```
- Older `WebSecurityConfigurerAdapter` (deprecated but common):
  `http.authorizeRequests().antMatchers("/api/**").authenticated()`
- Read `*SecurityConfig.java` / `*SecurityConfiguration.java` to verify
  (find them via `xref(SecurityFilterChain)` / `xref(WebSecurityConfigurerAdapter)`)
  — a controller is unguarded only if no method-level annotation,
  class-level annotation, URL-pattern rule, or in-method check covers the
  action.

**Activation gotcha**: `@PreAuthorize` / `@PostAuthorize` / `@Secured` are
**no-ops** without `@EnableMethodSecurity` (or older
`@EnableGlobalMethodSecurity(prePostEnabled = true, securedEnabled = true)`).
Check for the activation annotation on a `@Configuration` class
(`xref(EnableMethodSecurity)` / `xref(EnableGlobalMethodSecurity)`).
If activation is missing, the annotations are decoration only and the
URL-pattern config is the only real gate.

**Anti-rules**:
- `@CrossOrigin` is CORS, not auth.
- `@Validated` / `@Valid` validate input shape, not identity.
- Reading `SecurityContextHolder.getContext().getAuthentication()` in a
  method body counts as a gate ONLY if it short-circuits on missing/null
  authentication.

**State-change idioms** (gate-required): JPA `entityManager.persist/merge/remove`,
Spring Data `repository.save/saveAll/delete/deleteById/deleteAll`,
`JdbcTemplate.update/execute(<write SQL>)`, `Files.write`/
`Files.createFile`, `Runtime.exec`/`ProcessBuilder.start`, `RestTemplate`
or `WebClient` mutations.

**Controller locations**: classes annotated `@RestController` /
`@Controller`, methods annotated `@GetMapping`/`@PostMapping`/
`@PutMapping`/`@DeleteMapping`/`@PatchMapping` (or `@RequestMapping`).
The gate may live on the method annotation, the class annotation, the
URL-pattern config, or in-method.
