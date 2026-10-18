# Upstream drafts await human verification

No issues or pull requests have been submitted. No statement that the owner
manually verified a finding will be written until the corresponding human label
is `true` and the owner supplies their name.

After completing `labels_todo.md`, run:

```sh
python -m benchmark.compute --run 2026-10-18-r1 --labeler "Your name" --final --drafts
```

This prepares respectful texts and `docs-drift fix` planner patches for up to
three human-verified projects, without modifying or submitting upstream code.
An empty patch means no safe automatic edit was available; review the finding
manually. The owner decides whether to open any issue/PR by hand.
