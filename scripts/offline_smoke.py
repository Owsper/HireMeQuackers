"""Exercise installed command handlers through separate CLI processes without API calls."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    directory = ROOT / "data" / "qa" / ("smoke-" + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True)
    os.environ.update(DATABASE_URL=f"sqlite:///{directory / 'jobs.db'}", DATA_DIR=str(directory), AI_PROVIDER="auto", PYTHONUTF8="1", ENABLE_AI_MATCHING="true", ENABLE_AI_DIRECTION="false")
    for key in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
        os.environ[key] = ""
    from database.connection import init_db, get_session
    from database.models import Job, GeneratedDocument
    from services.ai_diagnostics import SAMPLE_JD
    engine = init_db()
    source = directory / "original.json"
    source.write_text(json.dumps({"name": "Synthetic Candidate", "skills": ["SQL", "Python"], "experience": [{"title": "Developer", "employer": "Synthetic Company", "dates": "2020-2024"}]}), encoding="utf-8")
    def run(*arguments, expected=0):
        result = subprocess.run([sys.executable, "-m", "quackers", *arguments], cwd=ROOT, env=os.environ.copy(), capture_output=True, text=True, encoding="utf-8")
        if result.returncode != expected:
            raise RuntimeError(f"CLI {arguments!r} exited {result.returncode}: {result.stderr}")
        print("PASS", " ".join(arguments[:3]), f"(exit {result.returncode})")
    run("cv", str(source), "--yes")
    with get_session(engine) as session:
        job = Job(title="Python Developer", company="Synthetic Company", description=SAMPLE_JD,
                  required_skills_json='["Python", "SQL"]', original_url="https://example.invalid/test",
                  source="offline-smoke", content_fingerprint="synthetic", extraction_status="COMPLETE", extraction_method="fixture")
        session.add(job)
        session.commit()
        job_id = str(job.id)
    run("match", job_id)
    run("job", job_id)
    run("docs", "cv", job_id, "--tailored")
    with get_session(engine) as session:
        doc_id = str(session.query(GeneratedDocument).filter_by(doc_type="tailored_cv").one().id)
    run("docs", "show", doc_id)
    run("docs", "export", doc_id, str(directory / "unapproved.docx"), expected=1)
    run("docs", "approve", doc_id)
    run("docs", "export", doc_id, str(directory / "approved.docx"))
    run("docs", "export", doc_id, str(directory / "approved.pdf"))
    run("app", "set", job_id, "SHORTLISTED")
    run("status")
    run("ai-check")
    run("ai-test", "extraction", expected=1)
    print("Synthetic data and exports:", directory)


if __name__ == "__main__":
    main()
