"""Reconciliation subsystem.

Provides a deterministic, money-safe reconciliation service that verifies the
money ledger reconciles to the sum of executed fills (net of fees) and records
each run's outcome. See :mod:`trade_post.reconciliation.service`.
"""
