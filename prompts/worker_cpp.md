## C/C++-specific review

Memory-safety bugs are the threat model. Look for unsafe string/buffer operations, integer wraps in allocation, format strings, command injection, path traversal, and concurrency races.

### Common C/C++ bug patterns

**Memory corruption:**
- Unbounded writes: `strcpy`, `strcat`, `sprintf`, `gets`, `scanf("%s")` on user input (CWE-120, CWE-787).
- `memcpy(dst, src, len)` where `len` from input not bounded vs `sizeof(dst)` (CWE-787).
- Off-by-one on loop bounds (`<=` vs `<`) on fixed buffers (CWE-193).
- Return stack pointers (CWE-562). Use-after-free: `string_view` outliving owner, `unique_ptr::get()` held past `reset()` (CWE-416).

**Integer bugs:**
- Size wrap in allocation: `int len = user; malloc(len + header)` where addition overflows (CWE-190).
- `size_t` truncated to `int` (CWE-197). Negative size as unsigned (CWE-681).

**Other high-value issues:**
- Format strings: `printf(user_input)` not `printf("%s", user_input)` (CWE-134).
- Command exec: `system(cmd)`, `popen(cmd)` with constructed strings (CWE-78). `execv`/`execve` safer.
- Path traversal: `fopen(user_path)`, `ifstream(user_path)` need `realpath()` + prefix check (CWE-22).
- Deserialization: custom binary parsers reading sizes then allocating without bounds (CWE-502).
- Race conditions: unsynchronized access to shared globals, no `mutex`/atomics (CWE-362). TOCTOU: `stat()` then `open()` on user path (CWE-367).
- Iterator invalidation: mutate container while iterating, erase during for-each (CWE-416).
- Exception in destructor → `std::terminate` (DOS).
- OpenSSL: unchecked `EVP_*` returns, static IVs, raw ECB mode (CWE-327, CWE-330).
- Scientific libraries (ROOT, HepMC, MPI, CUDA, HDF5): dimension/size reads from input used to allocate unchecked.

### Skip reporting

- Modern C++ ownership in use (`unique_ptr`, `shared_ptr`, `vector`) — real bugs need specific bad constructs.
- `std::string`/`string_view` in use — focus on C APIs crossing boundaries.
- AddressSanitizer in CI/CMake (`-fsanitize=address`) — real bugs would be caught.
- `std::filesystem::path` — safer but NOT traversal-proof without `canonical()` + prefix check.
- Fuzzing coverage — un-fuzzed parsers higher-value than fuzzed surface.
- Missing `noexcept`, style choices, `reinterpret_cast` between compatible types (e.g. `char*` ↔ `uint8_t*`), C++ version suggestions.

### Red flags

- C-string APIs on user data (CWE-120).
- `reinterpret_cast` of network bytes to struct (CWE-843, CWE-125).
- `malloc(user_size * user_count)` or `new T[user_n]` (CWE-190 → CWE-787).
- Manual resource cleanup without exception safety (CWE-404).
- `#pragma pack` + network deserialization (CWE-125).
- Use-after-move: `std::move(x); use(x);` (implementation-defined bug).
- Custom crypto vs OpenSSL/libsodium.
- `system()`/`popen()` in handler paths.
- Missing bounds on array indices tied to user input.
