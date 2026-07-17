# Security policy

## Reporting a vulnerability

Please report security issues **privately** — don't open a public GitHub issue.

- Preferred: use GitHub's **[Report a vulnerability](https://github.com/jaxzin/ha-seestar/security/advisories/new)**
  (Security → Advisories) to open a private advisory.
- Or email **brian@jaxzin.com** with the details.

Please include what you found, how to reproduce it, and the impact you expect.
I'll acknowledge your report as soon as I can, keep you posted on the fix, and
credit you in the release notes if you'd like.

## Scope

This project is a Home Assistant add-on that bridges a ZWO Seestar telescope into
Home Assistant over MQTT. It bundles the third-party
[seestar_alp](https://github.com/smart-underworld/seestar_alp) driver at a pinned
tag; vulnerabilities **in seestar_alp itself** are best reported upstream, though
a heads-up here is welcome so the pin can be bumped.

## Supported versions

Fixes land on `main` and ship in the next add-on release. Please test against the
latest published version before reporting.
