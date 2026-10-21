# Evals

`./rvh eval` measures worker quality on a labeled corpus. It runs a suite of
anchors — files or line ranges with known vulnerabilities and known false
positives — through the real pipeline, scores recall, false positives, and
cost, and compares two configurations with paired bootstrap confidence
intervals.

`public/` is the public demo corpus: labels from public advisories only
(`./rvh eval validate --corpus evals/public --public` enforces that). The main
corpus is private, because many of its labels are undisclosed
vulnerabilities; it lives in its own repository with the same layout:

    repos.yaml           repo id → source, pinned commit (or tree hash), framework hints
    families.yaml        CWE → family, for "location + CWE" matching
    labels/<repo>.yaml   ground truth
    suites/<id>.yaml     anchors to run: {kind, repeats, frozen, anchors}
    adjudication/        unmatched findings awaiting (queue/) or given (decided/) a verdict
    results/<eval-id>/   provenance.json, summary.json, summary.md (raw/ is not committed)
    cache/               pinned checkouts and CPGs (not committed)

Typical use:

    ./rvh eval validate --corpus CORPUS
    ./rvh eval --corpus CORPUS --suite quick --endpoint URL --model NAME
    ./rvh eval compare RESULTS_A RESULTS_B
    ./rvh eval rescore RESULTS_DIR --corpus CORPUS   # after adjudication

Run one eval at a time: containers are named per checkout, so two evals of
the same pinned repo would reclaim each other's containers.
