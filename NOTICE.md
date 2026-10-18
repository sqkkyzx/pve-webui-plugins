# Source and license notices

This project extracts the PVE WebUI integration from
https://github.com/sqkkyzx/komodo, based on source revision
`520e2aaa61a02e9bd12e94932f0c7ce935c46b13`, plus the local PVE integration
changes present at extraction (node temperatures, additional sites, and UI
refinements). Those local changes were not represented by that commit alone.
The original Komodo project is https://github.com/moghtech/komodo.

The integration implementation and tests retain their GNU GPL v3 license.
The full original license is included in LICENSE. The standalone repository
starts with an extracted snapshot; it does not include Komodo's full Git
history, Core, Periphery, or complete Web UI source.

`tests/fixtures/komodo-operation.rs` is the unmodified `Operation` enum excerpt
from `client/core/rs/src/entities/mod.rs` at the source revision above. That
file's latest modifying commit in the source checkout was
`93cce3fb60ee59673f92b720a24e8ab0d71eff10`. It is retained under GPL v3 as a
local API-contract fixture, so tests do not depend on an adjacent Komodo clone
or fetch a moving upstream schema at test time.

Proxmox VE's ExtJS components and xterm.js are loaded from the user's existing
PVE installation; they are not vendored here. Node exporter is an optional
external data source and is not bundled into the extension image.
