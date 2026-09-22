# Legacy reference code

`synthetic_field_prototype.py` is the challenge-provided v0.1 prototype, kept here
**unmodified** as reference material.

It is not part of the pipeline and nothing imports it. It exists so a reviewer can read
the original next to the replacement, and so the diff against the baseline commit
(`2a85690`) stays available for the rest of the project's history:

```bash
git diff 2a85690 -- legacy/synthetic_field_prototype.py
```

## Running it

Its import of `synthetic_generator` was written when both files sat side by side. Now
that the generator lives in `tools/`, run it from the repository root:

```bash
python -c "import sys; sys.path.insert(0, 'tools'); exec(open('legacy/synthetic_field_prototype.py').read())"
```

That is deliberate. The prototype is frozen, so patching its import to keep it runnable
would mean editing the very thing we want to preserve as evidence of the starting
point. The replacement entry point is `pitch-pipeline` (see `README.md`).
