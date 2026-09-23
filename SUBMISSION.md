# Publishing nf-biofuse to the Nextflow plugin registry

Everything below is for the repository owner to do. None of it has been done: no GitHub repository exists, nothing
has been pushed, the plugin name is not claimed, no registry token exists, and nothing has been published.

The repository is otherwise ready: `make assemble` builds `build/distributions/nf-biofuse-0.1.0.zip` (manifest
`Plugin-Id: nf-biofuse`, `Plugin-Version: 0.1.0`, `Plugin-Requires: >=25.10.0`, `Plugin-Provider: lyh970817`,
with the generated `spec.json` for the `biofuse` config scope), and `README.md` has the four sections the registry
requires: Summary, Get Started, Examples and License.

## 0. Decisions to take first

- [ ] **Licence.** The repository uses Apache-2.0, the plugin template's default (`LICENSE`, SPDX headers,
      `supervisor/pyproject.toml`). The supervisor is ported from nf-core/gwas code released under MIT. Keep
      Apache-2.0, or relicense to MIT to match nf-core; the registry only requires an OSI licence.
- [ ] **Provider and repository location.** `build.gradle` sets `provider = 'lyh970817'`, and the README's clone
      URL is `https://github.com/lyh970817/nf-biofuse`. The provider must equal the one given when claiming the
      name. If the plugin should live under an organisation, change both before anything is published.
- [ ] **Identity schema string.** The view identity preimage keeps the launcher's schema string
      `nf-core/gwas/biofuse-view-identity/1`, so a store keeps the identity the launcher declared for it. Renaming it
      (for example to `nf-biofuse/view-identity/1`) moves every identity; it is cheap now and a breaking change
      after release. If renamed, update `supervisor/nf_biofuse_supervisor/identity.py`, the golden test in
      `supervisor/tests/test_identity.py` and `docs/operations.md`.

## 1. Verify on the release commit

- [ ] `make test` (plugin unit tests) passes.
- [ ] In the supervisor environment: `cd supervisor && python -m pytest` passes.
- [ ] `make install`, then the local FUSE tier passes on a FUSE host (see `docs/operations.md`, Tests).
- [ ] `make assemble` builds `build/distributions/nf-biofuse-<version>.zip`.
- [ ] `version` in `build.gradle`, `supervisor/pyproject.toml` and `supervisor/nf_biofuse_supervisor/__init__.py`
      agree, and `CHANGELOG.md` has the release date.

## 2. GitHub

- [ ] Create the repository (`lyh970817/nf-biofuse`, or the organisation chosen above) and push `main`.
- [ ] Check that the `nf-biofuse CI` workflow passes (plugin unit tests on Nextflow 25.10 and 26.04, loading the
      plugin in the validation pipeline, and the supervisor's pure tier). It has not run yet.
- [ ] Tag the release commit `v0.1.0` and push the tag. The README's Get Started clones that tag to build the
      supervisor environment.

## 3. Nextflow plugin registry

- [ ] Open <https://registry.nextflow.io> and log in to Seqera (GitHub, Google or e-mail).
- [ ] *My plugins* → *Claim a plugin*: plugin name `nf-biofuse`, provider exactly as in `build.gradle`. Wait until
      an admin approves the claim (shown as *PENDING REVIEW* until then; needed once).
- [ ] *Access tokens*: create a token and store it; it is shown once.
- [ ] Put the token in `$HOME/.gradle/gradle.properties`, never in the repository:

      ```properties
      npr.apiKey=<token>
      ```

      (`NPR_API_KEY` in the environment works too.)

## 4. Release

- [ ] From the tagged commit: `make release` (runs `./gradlew releasePlugin`, which uploads the zip and uses
      `README.md` as the registry description).
- [ ] In a clean plugins directory, check that the registry serves it:
      `NXF_PLUGINS_DIR=$(mktemp -d) nextflow run <repo>/validation -plugins nf-biofuse@0.1.0`.

## 5. Afterwards

- [ ] Post the release and the evidence summary on lyh970817/gwas#8, where the move to a plugin was decided.
- [ ] Run the production validation run from `docs/operations.md` (Evidence checklist) on the Slurm cluster.
