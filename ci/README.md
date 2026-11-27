# sweb integration

CI runs Tortillas against the real
[`isec-tugraz/sweb`](https://github.com/isec-tugraz/sweb), not a fork.

## Pinned revision

[`sweb-revision`](sweb-revision) contains the upstream commit used by the
push/pull-request job. Bump it deliberately when you want to move to a newer
sweb:

```sh
git ls-remote https://github.com/isec-tugraz/sweb refs/heads/main
```

The weekly canary and manual workflow runs ignore the pin and test upstream
`main` directly, so a change that would break the next bump is reported before
it lands.

## Patch

[`sweb-tortillas.patch`](sweb-tortillas.patch) is the small delta sweb needs for
Tortillas:

- the two meta syscalls (`sc_tortillas_bootup`, `sc_tortillas_finished`),
- the shell hooks that call them,
- the GRUB menu timeout,
- one smoke test.

[`apply-sweb-patch.sh`](apply-sweb-patch.sh) applies it with
`git apply --3way`, then falls back to `patch --fuzz=3`. If the canary reports
that the patch no longer applies, upstream changed: rebase the patch on a fresh
`main` checkout and commit it.

Only the three small source hunks and `menu.lst` can conflict. The new
`tortillas_smoke.c` test is a pure addition and never conflicts.

## Why a patch and not a fork?

A fork duplicates the whole sweb repository and silently rots when it is not
rebased — the old `PaideiaDilemma/tortillas-sweb` submodule is exactly that
failure mode. A pinned upstream revision plus a reviewed patch keeps the delta
visible in this repository and makes gating deterministic. The ideal long-term
fix is to upstream the hooks into `isec-tugraz/sweb`; once that lands this patch
can be deleted.
