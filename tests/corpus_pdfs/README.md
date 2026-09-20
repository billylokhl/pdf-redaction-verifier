# Real vendor PDF corpus (optional, git-ignored contents)

Drop real producer output here to exercise the verifier against files it
did not generate itself — Acrobat, Word, Ghostscript, scanner output,
redaction-tool output, etc.

For each `foo.pdf`, add a sidecar `foo.pdf.secrets.json` listing the
secrets that file is known to contain, as `[name, value]` pairs:

```json
[["Applicant SSN", "123-45-6789"], ["DOB", "01/01/1970"]]
```

`tests/test_corpus.py::test_real_vendor_corpus` will scan each PDF and
assert every listed secret is detected (exit 1). With no files here the
test skips.

**Do not commit real documents or real PII.** Only the `.pdf` and
`.json` files are git-ignored; this README is tracked so the convention
is discoverable.
