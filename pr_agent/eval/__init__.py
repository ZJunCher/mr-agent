"""Offline evaluation / replay benchmark for PR-Agent reviews.

This package turns online review feedback into a replayable benchmark:

- ``marker``  : build/parse a hidden ``<!-- pr-agent-eval ... -->`` marker that
  freezes the exact code state (base/head/start sha), model and a small config
  snapshot at review time.
- ``store``   : persist captured ``review_runs`` (baseline reviews) and replay
  results, keyed by the existing ``review_id``.
- ``benchmark_provider`` / ``replay`` : re-run reviews against the frozen diff
  using the GitLab Compare API, and compare experiments.

Everything here is additive and opt-in (``[eval] enable_capture``). It never
changes the behavior of the live review/feedback flow.
"""
