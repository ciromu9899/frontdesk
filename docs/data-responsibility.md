# Data responsibility and commercial boundary

Status: product and deployment policy. This is not a legal opinion or a
certification of compliance.

## Default commercial model

Shellie Software Tools sells a packaged customer-operated software delivery and
commercial support benefits. The included source remains under Apache-2.0; the
customer installs and operates Frontdesk in its own environment. The supported
privacy-preserving baseline is:

- Ollama selected locally; no automatic selection of a cloud model;
- the customer owns the database, backups and encryption keys;
- the customer creates and owns any optional LinkedIn OIDC app and each enabled
  Slack, Meta, Teams or other social app;
- the customer stores and rotates its own connector credentials;
- webhook endpoints run in the customer's environment, without a Shellie relay;
- no Shellie telemetry, central conversation log or routine remote access; and
- support uses configuration and redacted diagnostics unless separately agreed.

In this baseline, the customer determines the purposes and means of processing
its end users' conversation data. Shellie does not receive, host, monitor, sell,
advertise against or train models on that conversation content.

## Responsibility map

| Processing | Responsible party in the baseline |
| --- | --- |
| Licence buyer, invoice, support and Shellie sales records | Shellie Software Tools |
| Shellie product-sale checkout outside FrontDesk | PayPal under its terms; Shellie remains responsible for merchant data it receives |
| Customer conversation content and retention | The deploying customer |
| Required customer LinkedIn OIDC app and enabled social accounts | The customer and the relevant platform |
| Customer business tools and channel accounts | The customer |
| Local model inference and local knowledge base | The customer |

Software is not a legal person. Calling Frontdesk “customer operated” does not
override a statutory duty. Actual access, control and use determine the parties'
roles.

## Events that change Shellie's role

Stop and perform a new privacy and contract review before Shellie:

- hosts Frontdesk or its database;
- receives, relays, backs up or can decrypt conversation content;
- receives unredacted support files or diagnostics;
- operates shared social-network apps, tokens or webhooks;
- selects or contracts with a cloud LLM for the customer;
- uses customer content for analytics, product improvement or model training; or
- provides a managed service to a healthcare or financial institution.

These activities may make Shellie a processor, service provider, contractor,
business associate or an independent/joint controller for the relevant
processing. Applicable DPA, BAA, security, subprocessors, transfer, retention,
deletion and incident-response provisions must be in place first.

## Sensitive and regulated data

The baseline product warning tells users not to submit passwords, complete card
numbers, Social Security numbers, medical records or financial-account
credentials. A warning reduces collection; it does not transfer all legal risk.

Each customer must choose and document one of these deployment modes:

1. **Regulated data prohibited.** Contract, onboarding and UI prohibit PHI,
   regulated financial information and other specified sensitive data. Tools and
   knowledge content for those workflows are not enabled.
2. **Regulated deployment.** The customer completes the relevant risk analysis,
   notices, contracts and technical controls. Shellie signs a BAA or equivalent
   only if its actual service and controls support the obligation.

The healthcare and fintech personas make regulated use foreseeable. They must
not be marketed as HIPAA-, GLBA- or otherwise legally compliant merely because
technical safeguards exist.

## Contract wording baseline

The following is a responsibility-allocation starting point, not final legal
text:

> Customer determines the purposes and means of processing Customer Data and is
> responsible for notices, legal bases, permissions, retention and data-subject
> requests. In the customer-operated, self-hosted Ollama configuration,
> Shellie Software Tools does not receive, host, monitor, sell or use Customer
> Data for advertising or model training. If Customer enables an optional hosted
> service, cloud provider, remote support channel or other integration, the
> applicable addendum governs that processing. Nothing in the agreement limits
> obligations that applicable law imposes on either party.

Do not use “Shellie Software Tools has nothing to do with any sensitive data” or
similar absolute language.

## Minimum sales package

Before general availability, the customer should receive:

- licence or subscription terms;
- a privacy notice for Shellie's buyer, billing and support data;
- this deployment responsibility schedule;
- a regulated-data prohibition or regulated-deployment addendum;
- a subprocessor and optional-integration list; and
- an incident and privacy-request contact route.

The deploying customer must replace example contacts, retention periods and
legal bases with its own approved values before production use.
