---
doc: scope
status: approved
language: en
---

# Service Requirement Blueprint

Turn vague private messages about website work into requirements, quotes, and delivery records that both parties can understand, confirm, and trace.

## The Unique Kernel

A low headline price rarely explains pages, features, recurring services, maintenance, security responsibilities, or acceptance. This product guides a client through those decisions, lets a contractor respond with an explicit scope and quote, and fixes the version that both parties confirm. Later delivery and acceptance refer to that shared version; a new request becomes a separate proposal.

## Who It Is For

Small-business clients who need a website but do not yet know how to describe it, and independent contractors who need a clear basis for estimating effort, price, exclusions, maintenance, and acceptance.

## The Core Loop

1. The client completes a guided requirement flow and saves Requirement JSON A.
2. The contractor clarifies the work, scope, costs, responsibilities, and acceptance conditions in Specification and Quote JSON B.
3. The system creates one candidate document containing the agreed text, amounts, and diagram. Both parties separately confirm the same fixed version.
4. The contractor delivers against the fixed version. The client accepts it or returns specific items for remediation and reinspection.
5. An out-of-scope request starts a separate proposal and never modifies the original agreement.

## Inspiration

The idea came from public posts that advertise extremely low website prices and from the learner's experience with AI-assisted development. Those examples reveal a recurring problem: the buyer may not know which features, third-party costs, maintenance duties, or security boundaries are excluded. Public posts are used only as problem-discovery material and do not support claims about any provider's conduct.

Mermaid and SVG diagrams present the shared blueprint. Progressive disclosure, checkboxes, clear states, and focused text inputs help users answer one decision at a time without losing the whole project structure.

## What Working Looks Like

One client and one contractor can complete one private case from JSON A through JSON B, a fixed candidate document, separate confirmation, delivery, acceptance, and optional remediation. The system can prove which source versions and exact document bytes were reviewed. Silence or expiry never becomes consent.

## Competition Language Boundary

English is the only enabled UI, planning, documentation, demo, and submission language. `/en/<path>` is the only product route family, and the competition build produces one English-labeled PDF and one English-labeled SVG.

The architecture keeps a catalog-based localization seam, but only `catalog.en.json` ships. No language switch or localized-delivery guarantee is included. Database and document handling remain Unicode-capable, and user-authored text stays exactly as entered without automatic translation. JSON keys, roles, states, and schema identifiers remain English machine codes.

## PoC Boundary

The competition build proves one end-to-end demonstration case. It is a local proof of concept, not a production marketplace, legal-signature service, payment provider, escrow service, or security certification platform.

## Later

- Additional display languages after a market and translation review.
- Production identity, authorization, backup, monitoring, retention, and operational controls.
- Party-controlled encryption and data reconstruction.
- Recommendations, public reviews, bidding, rankings, payments, and marketplace features.

## Explicitly Cut

- Overwriting a formal original or attaching a new request to the original agreement.
- Letting the platform decide quote fairness, work quality, breach, liability, damages, or dispute outcomes.
- Treating silence or an expired deadline as agreement.
- Reusing code from an existing project.
- Automatically translating user content.
- Country-specific law, tax, identity, payment, address, or hosting assumptions in the competition demo.

## Revision

On 2026-10-06, the learner replaced the earlier multi-language competition contract with an English-only, globally neutral delivery target. The product kernel, private-case model, permissions, immutable versions, and future localization seam remain unchanged.
