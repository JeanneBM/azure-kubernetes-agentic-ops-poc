# Changelog

Notable changes are recorded here, newest first.

## [Unreleased]

### Fixed

- Environment cleanup resolves one native Azure CLI executable, avoiding
  recursion through an `Az` function and duplicate executable PATH matches.
- Delete eligible unused regional Network Watchers with `az resource delete`
  using their discovered resource group, name and resource type, verify removal,
  and remove `NetworkWatcherRG` only when empty. Preserve watchers serving other
  VNets or containing diagnostic children and report them explicitly.
- Continue watcher checks when AKS node groups remain, then report incomplete
  cleanup as an error. Print the saved inventory path for retries after deletion.

### Added

- Cleanup examples for `rg-agentic-ops-poc-lab`, including `-WhatIf`.
- This changelog. Earlier history has not been reconstructed.
