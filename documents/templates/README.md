Markdown templates for generated documents (CV suggestions, cover letter,
job detail). These templates are referenced by `documents/exporter.py` when
exporting DOCX / PDF / Markdown (text).

If `ENABLE_PDF_EXPORT=true` and `PyMuPDF` is installed, `exporter` will create
`.pdf` variants by rendering the Markdown text via the engine. (Currently text /
.md are always supported.)

## Files

- `cover_letter_template.md` — variable placeholders: `{name}` `{company}` `{title}` `{date}`
- `cv_suggestions_template.md` — variable placeholders: `{profile_id}` `{job_id}`
