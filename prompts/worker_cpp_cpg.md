### CPG usage patterns — C/C++

**Starting investigation:** for each sink class (`curl_easy_setopt`,
`memcpy`, `strcpy`, `open`, `fopen`, `system`, `popen`, `sprintf`, etc.),
`xref(name)` to locate all occurrences, then investigate case by case.

**Key patterns:**
- SSRF via parsed response — `paths(src="curl_easy_perform", dst="curl_easy_setopt")` to see if a parsed-response field flows into a second URL.
- Buffer overflow — `reachable(sink="file.cpp:LINE")` on each `memcpy`/`strcpy` site; if argv/env/fread reaches the copy with no size-check hop, the finding is real.
- Large function body — call `func(name)` first; if `numCallees` is small, read directly; if large, use `callees` to narrow to the dangerous-sink subtree.
- Defense verification — `paths(src=<plausible_source>, dst=<sink>)` before marking `present-and-effective`; empty = defense likely works; non-empty = read hops and identify what breaks the attack.
