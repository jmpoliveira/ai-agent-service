# Contributing and releasing

`main` and the release tags are protected by repository rules on GitHub. This file describes
the workflow those rules require, so a push is never rejected halfway through a release.

## Repository rules

| Rule | Effect |
|---|---|
| `main` changes only through a pull request | `git push origin main` is rejected; push a branch and open a PR |
| 4 required status checks | The PR merges only when CI is green: `Lint, types and tests` on Python 3.12, 3.13 and 3.14, and `Docker build` |
| Merge commits disabled | Merge with **squash** or **rebase**; both create new commits on `main` |
| Tags cannot be deleted (or moved) | A pushed tag is permanent: tag only commits that are already on `main` |

## Everyday change

```bash
git switch main && git pull
git switch -c <type>/<short-name>          # e.g. feat/azure-openai, docs/release-workflow
# ... change, then:
make check
git add <files> && git commit -m "<type>(<scope>): <summary>"
git push -u origin <type>/<short-name>
gh pr create --base main --fill
gh pr checks --watch
gh pr merge --squash --delete-branch       # or --rebase to keep every commit
git switch main && git pull
```

Commit messages follow Conventional Commits (`feat`, `fix`, `docs`, `test`, `refactor`,
`chore`), in English. Use `--rebase` when the individual commits matter later, for example
commits referenced by [evals/EXPERIMENTS.md](evals/EXPERIMENTS.md); `--squash` otherwise.

Without the GitHub CLI, push the branch and use the "Create a pull request" link that `git push`
prints; choose the merge method in the dropdown of the merge button.

## Releasing a version

Each roadmap step is a release (`v0.2.0`, `v0.3.0`...). **The tag comes last**, after the merge,
because squash and rebase give the commits new hashes: a tag created on the branch would point
to a commit that never reaches `main`, and the rules do not allow fixing it.

1. On a branch, bump `version` in `pyproject.toml`, update the README (status, roadmap) and any
   ADRs, and merge it through a PR as above.
2. Tag the merged commit on `main` and publish the release:

   ```bash
   git switch main && git pull
   make check
   git tag -a vX.Y.Z -m "vX.Y.Z: <title>"
   git push origin vX.Y.Z
   gh release create vX.Y.Z --verify-tag --title "vX.Y.Z – <title>" --notes "<what changed>"
   ```

Never run `git push --follow-tags` or `git push --tags` from a branch: it publishes tags before
their commits are on `main`.

If a release needs a fix after its tag is pushed, release the fix as the next patch version
(`vX.Y.Z+1`); the old tag cannot be moved.

`v0.2.0` is the exception that taught this: it was tagged before the PR, so it was merged with a
merge commit (enabled for that PR only) to keep the tagged commit on `main`.
