# VeriCompute Docs Home

Role-based reading paths for faster review.

## Reviewer Path (Scholarship / Technical Review)

Start here if you need to evaluate thesis, implementation scope, and validation readiness.

1. `../README.md` (thesis, status, checklist)
2. `CONSENSUS.md` (protocol and economics)
3. `SECURITY_ARCHITECTURE.md` (security model and modes)
4. `CONTRACT_SYSTEM.md` (task lifecycle, settlement, arbitration)
5. `PRODUCTION_READINESS_REPORT.md` (readiness and gaps)
6. `SECURITY_AUDIT.md` and `SECURITY_AUDIT_2025.md` (risk and remediation evidence)

## Investor Path (Market / Moat / Monetization)

Start here if you focus on market fit, defensibility, and commercialization potential.

1. `../README.md` (privacy-first verifiable compute settlement thesis)
2. `CONSENSUS.md` section on system advantages/comparison
3. `FEE_MECHANISM.md` (token flow and fee structure)
4. `DYNAMIC_PRICING_IMPLEMENTATION.md` (market response model)
5. `DECENTRALIZATION_ROADMAP.md` (growth and governance trajectory)

## Developer Path (Build / Operate / Extend)

Start here if you need to run, debug, or extend the system.

1. `QUICKSTART.md` (fast bootstrapping)
2. `USER_GUIDE.md` (RPC workflow and common operations)
3. `API.md` (method reference)
4. `OPERATIONS.md` and `DEPLOYMENT.md` (ops/deployment)
5. `SECURITY_HARDENING.md` (production hardening checklist)
6. `SECURITY_BASELINE_CHECKLIST.md` (release gate and minimum controls)
7. `RPC_PERMISSION_BASELINE.md` (sensitive RPC exposure baseline)
8. `CODEBASE_REVIEW_2026-04-10.md` (integrity/rationality review and refactor plan)

Quick regression command:

`python -m pytest tests/test_security_regression_access.py -q`

## Suggested Review Sequence (60-90 minutes)

- 15 min: `../README.md` + `CONSENSUS.md` summary
- 20 min: `SECURITY_ARCHITECTURE.md` + audit documents
- 20 min: `CONTRACT_SYSTEM.md` + `API.md`
- 10 min: `PRODUCTION_READINESS_REPORT.md`

## Notes

- Some audit reports are historical snapshots, so old line references may appear by design.
- For current implementation truth, prioritize `../README.md` status sections and direct code references.
