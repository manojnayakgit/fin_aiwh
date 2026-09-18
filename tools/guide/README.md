# Implementation guide generator

`docs/IMPLEMENTATION_GUIDE.md` is generated, not written. Every code block is
pulled from git by path and, where it matters, by commit, so the guide cannot
drift from the repository.

```
python3 tools/guide/build.py
```

`lib.py` extracts whole files or named top-level defs at a revision.
`part_a.py` to `part_d.py` hold the narrative for each part. `build.py`
assembles them plus the appendices and writes the guide.
