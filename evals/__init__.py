"""Offline evals: run the real agent over a dataset of cases and score its behaviour.

Not part of the service package: evals are a separate entry point (``python -m evals``) that
reuses the composition root, the way the HTTP API does. See evals/README.md.
"""
