---
applyTo: "04-foundry-agent-delivery/agent/**,04-foundry-agent-delivery/evals/**,04-foundry-agent-delivery/tests/**"
---

When reviewing agent definitions and evaluation assets:

- Look for contradictory, ambiguous, or untestable instructions and behavior.
- Check that enabled tools and their authority match the documented use case.
- Check that portable configuration contains no model deployment names,
  endpoints, connection names, resource IDs, credentials, or permissions.
- Reject unsupported settings rather than silently omitting them.
- Check that cases are representative, privacy-safe, and cover expected
  failures as well as successful behavior.
- Treat deterministic assertions as smoke checks, not proof of factual
  correctness. Do not infer groundedness from citations, keywords, or rubric
  scores without the required source context.
- Note evaluator limitations, uncalibrated judge scores, preview status, and
  missing result evidence. Do not recommend a release threshold that isn't
  approved in the workshop configuration.
- Runtime evaluation and Copilot review complement, but do not replace, human
  review or production approval.
