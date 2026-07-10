# Main Branch Protection

Apply this after `.github/workflows/ci.yml` has completed successfully once on `main`.

1. Open repository Settings → Rules → Rulesets.
2. Create a branch ruleset targeting the default branch, `main`.
3. Require a pull request before merging.
4. Require these status checks:
   - `Python`
   - `Frontend`
   - `Compose`
5. Require branches to be up to date before merging.
6. Require conversation resolution before merging.
7. Block force pushes and branch deletion for `main`.
8. Under Actions settings for this public repository, require approval for workflows from external contributors.

This ruleset is a CI gate only. It does not authorize deployment, package publication, or production access.
