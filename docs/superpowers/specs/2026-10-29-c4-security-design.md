# C4 views and security overlays

Approved design, 29 October 2026. Implementation details below are proposed engineering choices.

## Intent

Keep detailed Azure infrastructure diagrams and add more abstract application and context
views. Let users independently choose C4 presentation and security overlays at runtime.
Application diagrams retain APIs, workers and data stores inside application boundaries.

## Goals and interfaces

- `render --view detailed|summary|application|context|all`; default `all` retains its existing
  meaning: detailed and summary. Users request new views explicitly.
- `render --style azure|c4`; default `azure` retains the existing output.
- `render --security-overlay none|identity|network|all`; default `none`.
- `extract --security` adds read-only authorization extraction to the normal extraction.
  It can accompany `--azure`; reject `--bicep --security` as contradictory.
- New variants receive distinct output filenames; existing default names stay unchanged.
- Rendering remains offline from caches.

## Discovery and abstraction

Preserve tags and top-level identity metadata from Azure and Bicep what-if. Default application
membership is subscription + environment + application. Tag key matching ignores case;
values retain their spelling, with grouping values compared without case. Missing application
tags produce unclassified infrastructure; missing environment is explicitly unspecified.
YAML overrides support name/type/subscription selectors, explicit application membership,
component roles/labels, environment, shared services and an explicit cross-subscription group.
First matching override wins. Grouping across subscriptions only happens in the new views,
using the selected subscriptions together; existing reports and views stay per subscription.

Application views retain individual components unless an explicit component grouping rule
combines them. Use known service types and configured name patterns to identify APIs,
workers and stores; ambiguous components retain their Azure technology description.
Context views collapse application internals. Shared services and out-of-scope dependencies
remain visible. Do not infer people, business systems or protocols from infrastructure alone.
Optional annotation selection exposes department, owner and dataClassification; conflicting
values at an aggregate are listed as mixed rather than choosing the first resource's value.

## Security

Identity overlays distinguish workload identity attachment, observed role grants and
configured use. Preserve identity names, role names/IDs, assignment scope, inheritance,
conditions, and evidence source. A shared identity can have several workload attachments.
An endpoint reference alone is not evidence that a particular identity authenticates the call.
Resources with multiple identities must not get an arbitrarily selected identity.

Read role assignments and definitions from AuthorizationResources into a separate cache.
Do not feed subscription or management-group scoped authorization records through the
resource-group-only ARM ID parser. Resolve principals by principal ID and resource identity
metadata; unresolved principals retain an identifier and unknown label.
Include resource, resource-group and subscription grants and available ancestor grants;
state incomplete coverage when ancestor discovery or permissions are insufficient.
Keep Bicep-declared grants separate from Azure-observed grants rather than treating the
union as the deployed access model. Unresolved what-if expressions remain unresolved.

Network overlays show actual VNet/subnet boundaries, NSG associations, private endpoint
connections and explicit public-access settings. A private endpoint does not establish that
public access is disabled, or that a workload uses that endpoint. Subnet references such as
VNet integration are associations, not necessarily physical workload placement.

Higher views aggregate security relationships with identity attribution and scope intact.
Do not reparent a resource into several overlapping identity boundaries: permissions are
labelled relationships; network/security zones are containers or explicit annotations.
Respect explicit out-of-scope filters even when overlay mode exposes normally hidden
security evidence. Add a legend and coverage note. Unknown never means no access.

## Approaches

1. Add C4 deployment presentation to D2 and extend renderer-independent views (chosen).
   Reuses the existing renderer, SVG bundling and graph pipeline.
2. Introduce a separate C4 renderer/model. More export flexibility but duplicates rendering
   and adds dependencies; not needed for this runtime style feature.

Application discovery uses the recommended hybrid approach: existing tags plus YAML
overrides. Pure automatic inference cannot resolve shared resources reliably; entirely
manual mappings would duplicate existing tags.

## Constraints and verification

Keep Python 3.12+, D2 with ELK, and existing dependencies. Existing commands, caches and
drift/cost behavior stay compatible. Old caches permit existing views; new modes visibly
report absent tags/security coverage and how to refresh extraction.

Tests cover grouping, scope inheritance, ambiguous identity use, aggregation and provenance,
offline CLI combinations, and unchanged default output. Render representative C4/overlay
fixtures with real D2 and inspect SVGs before claiming visual verification.

## Non-goals

This is not an effective-access audit, traffic observation, full firewall evaluation, automatic
business architecture discovery, or proof that one identity has exclusive access. SQL users,
group expansion, Key Vault access policies and other service-specific authorization require
explicit future support; show their coverage as outside the initial RBAC overlay.
