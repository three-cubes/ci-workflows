"""Off-CI local agent tooling for Three Cubes repos.

Currently exposes `agent-token` — mint a short-lived per-agent GitHub App
installation token from Key Vault (the local complement to the CI
`github-app-token` composite action). `--agent builder|shape|consultant|growth`
selects a per-agent App; omit it for the canonical `three-cubes-agent`.
"""
