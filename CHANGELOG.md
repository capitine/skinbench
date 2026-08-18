# Changelog

All notable changes to this project are documented in this file. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.0.0-beta.1] - 2026-08-18

First public beta.

### Added

- Working Maya tools for moving and mirroring joints, pose handling,
  detach/reattach recovery, bind-photo repair, weight mirror and heal, drag
  solve, anti-bleed cleanup, ROM sweep, and chain curl.
- Source-visible drag installer and Maya module layout.
- Safety, recovery, support, contribution, and development documentation.
- Automated syntax, static-analysis, release-policy, security, and mayapy smoke
  checks.
- Evidence-bounded compatibility reporting for verified and unverified
  environments.

### Fixed

- Selected-vertex nearest mirroring now grades the actual pairs it used and
  verifies changed rows against the pre-write snapshot. Ambiguous nearest pairs
  are refused before any weights change.
- Opening SkinBench now warns when a skinned mesh still carries vertex drags
  that Maya can save or export. Unsupported skinned geometry is named as
  unverified instead of hiding valid polygon-mesh results.

[1.0.0-beta.1]: https://github.com/capitine/skinbench/releases/tag/v1.0.0-beta.1
