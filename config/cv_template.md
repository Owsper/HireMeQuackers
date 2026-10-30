# Filling in the CV template

Use this when your PDF CV cannot be read reliably (scanned, image-only, or
garbled text). Fill in `cv_template.json`, then load it with:

    quackers cv-check path/to/cv.json   # validate + preview, saves nothing
    quackers cv path/to/cv.json         # validate, preview, confirm, save

JSON has no comments — keep this file open next to the template.

| Field | Type | Example |
|---|---|---|
| `name` | text, **required** | `"Ada Lovelace"` |
| `email` | text, must be a valid address | `"ada@example.com"` |
| `phone` | text | `"+49 151 2345678"` |
| `location` | text | `"Berlin, Germany"` |
| `linkedin` | text (URL) | `"https://linkedin.com/in/ada"` |
| `website` | text (URL: portfolio / GitHub) | `"https://github.com/ada"` |
| `job_titles` | list of text — roles you are targeting | `["Data Analyst Intern", "Werkstudent BI"]` |
| `summary` | text, 2–4 sentences | |
| `skills` | list of text | `["Python", "SQL", "Power BI"]` |
| `languages` | list of text | `["German C1", "English C2"]` |
| `experience` | list of text, one entry per job | `"Data Intern — Acme GmbH, Berlin (06/2025–09/2025): built KPI dashboards"` |
| `education` | list of text | `"B.Sc. Computer Science — TU Berlin (2023–2026)"` |
| `projects` | list of text | `"Job-scraper CLI — Python, SQLite"` |
| `certifications` | list of text | `["AWS Cloud Practitioner"]` |
| `achievements` | list of text | |

Rules:

- Leave a field empty (`""` or `[]`) rather than guessing — nothing is invented for you.
- Accepted aliases: `full_name` → `name`, `professional_summary` → `summary`.
- Unknown fields are reported as warnings and ignored.
- Loading never silently replaces your active profile: you are shown a
  preview and asked first. Older profiles are kept (`quackers profile list`).
