# ADR 0001: Edge host and image pipeline

- **Status:** Accepted
- **Date:** 2026-11-23

## Context

IoT Edge 1.6 LTS needs a Linux host with the Moby engine; Ubuntu Server 24.04 (AMD64) is a
Tier 1 platform. The development machine has neither Docker nor Hyper-V, so there is no local
build or local edge host.

The usual cloud build paths are unavailable on this subscription:

- ACR Tasks (`az acr build`) is refused with `TasksOperationsNotAllowed`; Microsoft has paused
  task runs for free-credit subscriptions.
- GitHub Actions cannot sign in to Azure without an app registration for workload identity
  federation, and the directory does not allow users to create applications.

## Decision

- **The edge device is an Azure VM:** Ubuntu 24.04, `Standard_B2als_v2`, Trusted Launch. Cloud-init
  installs Moby and `aziot-edge` on first boot. A daily auto-shutdown limits cost.
- **The VM is also the image builder.** The deploy script packages the build context (a
  reproducible archive named by its content hash) and uploads it to a `build` container. Through
  Azure Run Command, the VM then:
  - downloads the context with its managed identity (*Storage Blob Data Reader* on that container);
  - builds both images;
  - pushes them to a Basic container registry with the same identity (*AcrPush*), logging in by
    exchanging an Entra token for a registry refresh token;
  - prints the pushed digests.
- **Images are deployed by digest** (`registry/repo@sha256:…`), never by tag, so the device runs
  exactly what was built and a rollout is a manifest change.
- **The device pulls with a repository-scoped token**:
  - Its scope map allows only `content/read` and `metadata/read` on the two module repositories.
  - Each deployment generates a new password that expires in 7 days and passes it to `edgectl`
    in an environment variable. The password is never printed and never written to a file.
  - The registry's admin user stays disabled.
- **No inbound access.** The NSG has no inbound rules and the VM has no usable SSH key (the private
  half is discarded at creation). Run Command, governed by Azure RBAC, is the only management path.
  A Standard public IP gives the VM its explicit outbound path; new subnets have no default
  outbound access.

## Consequences

- Building on the device that runs the modules couples two roles; it is acceptable for one device
  and keeps the pipeline inside Azure with identities only.
- The registry pull password lasts a week. A redeploy or rollout rotates it. Images already pulled
  keep running after it expires.
- Run Command returns only the last 4 KB of output once a script ends, so each script prints a
  single marker line (`THUMBPRINT`, `CONFIGURED`, `IMAGES`, `NETWORK`) that the caller parses, and
  fails loudly without it.
