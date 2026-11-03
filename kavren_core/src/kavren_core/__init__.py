"""KAVREN deterministic core.

Everything in this package is pure and deterministic: the same inputs always
produce the same bytes, hashes and results. No network, no clock, no randomness
(randomness for record IDs, canaries and salts is injected by callers).
"""

EVALUATOR_VERSION = "0.1.0"
SPEC_FORMAT = "kavren-spec/1"
