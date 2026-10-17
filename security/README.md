# security/

Historical security tooling and audit write-ups. **Nothing here is wired into
CI, the backend, or the deployment.** The live security gates are the Security
Scan job in `.github/workflows/test-pipeline.yml` (Bandit and `safety`) and the
backend authorization and tenancy tests; see
[`docs/engineering/testing.md`](../docs/engineering/testing.md) for the job map.
The project's security policy, threat model and reporting rules are in the root
[`SECURITY.md`](../SECURITY.md), which is the document to read first.

This README previously described a "Knowledge Graph Analytics Dashboard
security framework" with every component marked implemented. That claim was not
supported: no workflow, script, compose file or Makefile target in the
repository references these files, and the files that README told readers to run
(`security/requirements.txt`, `incident_response_system.py`,
`monitoring_system.py`) do not exist. Treat the files below as unreviewed
drafts, not as controls.

## What is actually in this directory

| File | What it is |
| --- | --- |
| `enterprise_security_audit_framework.py`, `automated_vulnerability_scanner.py`, `compliance_validation_system.py` | Large standalone Python audit and scanning scripts, not imported by the backend. |
| `security_hardening_suite.py`, `deploy_security.py`, `final_security_validation.py`, `security_test_suite.py` | Standalone hardening, deployment and validation scripts. Not run by any pipeline. |
| `security_monitoring_threat_detection.py`, `incident_response_automation.py`, `cicd_security_integration.py` | Standalone monitoring, incident-response and CI-integration prototypes. Nothing in `.github/workflows/` calls them. |
| `docker-security.hardened.yml`, `security-compliance.yml`, `security-scanning.yml` | Example hardened Docker Compose, compliance and scanner configuration for an earlier "Multimodal Enterprise RAG System" deployment. Not loaded by the real Compose files or workflows. |
| `AUDIT_REPORT.md`, `COMPREHENSIVE_SECURITY_AUDIT_REPORT.md` | Dated audit reports (January and November 2025). Historical evidence only. |
| `SECURITY_FRAMEWORK_DOCUMENTATION.md`, `SECURITY_HARDENING_IMPLEMENTATION.md` | Design and hardening write-ups for the same earlier framework. Their status claims are unverified. |

## Rules for editing here

- Do not cite a file in this directory as proof that a control exists or runs.
  Prove the control from running code, a workflow, or a test.
- Do not rewrite the dated reports to match current code; add a new dated
  record instead (see [`docs/AGENTS.md`](../docs/AGENTS.md)).
- Do not add credentials, tokens, scan output containing secrets, or customer
  data to any file here.
- If a script here is revived, wire it into a workflow in the same change and
  list it as a CI gate in `docs/engineering/testing.md`; otherwise leave it
  as-is or propose removing it.
