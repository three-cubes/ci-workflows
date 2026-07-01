# Verifying signed images

When a caller of the `docker-build-publish.yml` reusable workflow sets
`sign: true`, the pushed image is hardened three ways, all keyless (no
long-lived signing key exists anywhere):

- **SLSA provenance** — `docker/build-push-action` attaches a provenance
  attestation (`provenance: true`) to the pushed image index.
- **SBOM** — the same step attaches an SBOM attestation (`sbom: true`).
- **Keyless signature** — a `cosign sign` step signs the pushed **digest**
  using a short-lived GitHub OIDC token (Fulcio issues the certificate, Rekor
  logs it). Because the signature is over the image-*index* digest, it
  transitively covers the provenance/SBOM manifests the index references.

This page is the consumer-side recipe for verifying that bundle. It assumes
`ghcr.io/three-cubes/kairix` (the pilot caller); substitute your own image.

## Prerequisites

Install [`cosign`](https://docs.sigstore.dev/cosign/system_config/installation/)
(v2+). No key file and no registry secret are needed to *verify* a public
image — verification reads the signature and the public Rekor transparency log.

Resolve the **digest** you want to verify (verify a digest, never a mutable
tag). Either use the digest printed by the publishing run, or resolve a tag:

```bash
IMAGE=ghcr.io/three-cubes/kairix
DIGEST=$(crane digest "$IMAGE:latest")   # or: docker buildx imagetools inspect "$IMAGE:latest" --format '{{ .Manifest.Digest }}'
REF="$IMAGE@$DIGEST"
```

## Verify the keyless signature

The signing identity is this reusable workflow — keyless certs minted inside a
*reusable* workflow carry the `job_workflow_ref` of the workflow that ran the
signing step, i.e.
`three-cubes/tc-pipelines/.github/workflows/docker-build-publish.yml`, **not**
the caller repo. The OIDC issuer is GitHub Actions:

```bash
cosign verify "$REF" \
  --certificate-identity-regexp '^https://github\.com/three-cubes/tc-pipelines/\.github/workflows/docker-build-publish\.yml@.+$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

A `0` exit code with a printed certificate bundle means the image was built and
signed by our pipeline. Pin the regexp tighter (e.g. anchor the trailing
`@refs/tags/v1.2.3`) once you publish from a tagged ref you trust.

## Inspect the provenance and SBOM

The SLSA provenance and SBOM are in-toto attestations BuildKit attaches to the
image index. The signature step above signs the index digest, so verifying the
signature already vouches for these manifests. Read their contents with buildx:

```bash
docker buildx imagetools inspect "$REF" --format '{{ json .Provenance }}'
docker buildx imagetools inspect "$REF" --format '{{ json .SBOM }}'
```

If a caller additionally cosign-attests the predicates (keyless, same identity),
verify them independently with `cosign verify-attestation`, which checks each
attestation's own Fulcio cert + Rekor entry before returning the predicate:

```bash
cosign verify-attestation "$REF" \
  --type slsaprovenance \
  --certificate-identity-regexp '^https://github\.com/three-cubes/tc-pipelines/\.github/workflows/docker-build-publish\.yml@.+$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## Notes

- **Release path is HITL.** Signing is opt-in (`sign: false` by default) and
  changes nothing on the human-in-the-loop release path until a caller turns it
  on. The pilot caller is `kairix`'s `docker-publish.yml`.
- **Secret-free by construction.** Signing is keyless: the workflow needs
  `id-token: write` (to mint the OIDC token) plus the registry token the caller
  already uses to push the image (to push the signature). No cosign key is ever
  created, stored, or referenced.
