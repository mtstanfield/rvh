## Bash/shell-specific review

Shell has extreme footguns: word-splitting, unquoted expansions, globbing, signal handling. Most real bugs come from quoting mistakes crossing trust boundaries.

### Common bug classes

- **Unquoted expansion injection** (CWE-78) — `rm $user` splits on spaces/newlines; `; cmd` runs second command. Defense: `rm "$user"` or use `--` terminator: `rm -- "$user"`.
- **Flag injection** (CWE-88) — `rm "$user"` is safe if `user="-rf /"`, still passed as flag. Use `rm -- "$user"`.
- **`eval` RCE** (CWE-95) — `eval "$anything_user_controlled"` is automatic RCE. Never use `eval`.
- **`bash -c` with user input** (CWE-78) — `bash -c "$1"` or `sh -c "$user"` is shell-in-shell RCE. Even `bash -c 'echo $1' _ "$user"` re-expands in child shell.
- **Command substitution globbing** — `cmd $(other_cmd)` splits output on word-splitting; `x=$(cmd)` stores output, later unquoted usage applies word-splitting.
- **Tempfile races** (CWE-377, CWE-367) — `foo > /tmp/file.$$` is predictable and symlink-replaceable. Use `mktemp` with strict template.
- **`set -e` / `pipefail` omission** (CWE-252, CWE-755) — scripts without `set -euo pipefail` ignore errors; failed security checks continue silently.
- **IFS injection** — attacker controlling `IFS` forces word-splitting. Defense: `IFS=$' \t\n'` at script top or `unset IFS`.
- **`$@` vs `$*`** — `"$@"` preserves argv; `$*` without quotes splits. Wrong usage forwarding args creates injection paths.
- **Path traversal in targets** — `cp "$source" "$user_dest"` with `..` or absolute paths. Defense: `realpath()` + prefix check on allowed root.
- **Glob expansion on user input** — `for f in $user_input` if `user_input=*` expands to every file. Safe: always quote or `set -f` (noglob).
- **Lock file TOCTOU** — `[ -e lock ] || touch lock` is vulnerable. Use `flock` or atomic `mkdir`.
- **Umask** — scripts writing keys should `umask 077` or `install -m 600`.
- **Heredoc expansion** (CWE-94) — `<<EOF` expands `$vars` / `$(cmd)`; `<<'EOF'` is literal. Unquoted heredoc with user variables piped to shell is injection.
- **Signal handler leaks** — `trap 'echo failed: $SECRET' ERR` logs secrets on failure.
- **`source` / `.` with user-derived paths** (CWE-94) — inclusion injection if path from user input.

### Defenses (don't re-report as missing)

- **Quoting every `$var`** — well-written shell quotes all expansions. Unquoted internal vars like `$PWD` / `$HOME` are stylistically suspect but not exploitable unless value crosses trust boundary.
- **`set -euo pipefail` at top** — indicates error propagation care.
- **`mktemp` / `mktemp -d`** — correct temp file pattern. Not vulnerable to symlink races.
- **`declare -r` / `readonly`** — prevents constant override.
- **`getopts` / `getopt`** — suggests careful argument handling.
- **Explicit `--` terminator** — `rm -- "$file"` blocks flag injection.
- **`printf '%s'` vs `echo`** — `echo` misbehaves on `-n`, `-e`, backslashes; `printf '%s'` is safe.

### Red flags

- `eval` on user-reachable input (CWE-95)
- `bash -c` / `sh -c` with user input (CWE-78)
- Unquoted variables from `$1`-`$9`, `$*`, `$@`, `$ENV`, stdin, `$(curl ...)` (CWE-78)
- Tempfiles via `$$`, `$RANDOM`, or hand-rolled predictable names (CWE-377)
- `source` / `.` with user-derived paths (CWE-94)
- `eval "$(cat /file)"` where file writable (CWE-95)
- Piping user data to `sh` / `bash` stdin (CWE-78)
- `chmod 777` or `o+w` on scripts / keys / configs (CWE-377 adjacent)
- `curl | sh` / `wget | bash` patterns (CWE-494)
- `[ "$secret" = "$input" ]` for constant-time comparison (CWE-208)
- Missing `set -e` in multi-command security flow (CWE-252)
