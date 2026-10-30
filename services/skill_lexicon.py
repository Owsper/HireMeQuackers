"""Cross-industry skill vocabulary used to tag job descriptions.

The list covers every field the app searches (not only IT), so no industry
gets an artificial head start in matching.  Only terms that literally
appear in the text are returned — nothing is inferred.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

SKILLS_BY_FIELD: Dict[str, Tuple[str, ...]] = {
    "it": ("python", "sql", "javascript", "typescript", "java", "react", "aws", "azure",
           "docker", "kubernetes", "git", "django", "fastapi", "pandas", "machine learning",
           "data analysis", "tableau", "power bi", "api", "linux"),
    "office": ("excel", "microsoft office", "ms office", "powerpoint", "outlook",
               "sap", "salesforce", "crm", "erp", "data entry", "scheduling",
               "office administration", "correspondence"),
    "finance": ("accounting", "bookkeeping", "financial reporting", "budgeting", "payroll",
                "tax", "audit", "accounts payable", "accounts receivable", "ifrs", "hgb",
                "datev", "invoicing", "reconciliation", "controlling"),
    "health": ("patient care", "nursing", "first aid", "medication administration",
               "wound care", "vital signs", "elderly care", "hygiene", "triage",
               "medical documentation", "cpr", "bls"),
    "engineering": ("cad", "autocad", "solidworks", "quality control", "lean",
                    "six sigma", "maintenance", "plc", "manufacturing", "technical drawing",
                    "cnc", "welding"),
    "marketing": ("social media", "seo", "sem", "content creation", "copywriting",
                  "google analytics", "email marketing", "market research", "branding",
                  "public relations", "event management", "sales", "negotiation",
                  "lead generation", "account management", "cold calling"),
    "design": ("photoshop", "illustrator", "indesign", "figma", "adobe creative suite",
               "typography", "ui design", "ux design", "video editing", "photography"),
    "education": ("teaching", "curriculum development", "tutoring", "lesson planning",
                  "research", "academic writing", "classroom management"),
    "hospitality": ("customer service", "food safety", "haccp", "cooking", "barista",
                    "front desk", "housekeeping", "reservations", "cash handling", "pos"),
    "retail": ("merchandising", "inventory management", "stock management",
               "visual merchandising", "point of sale"),
    "logistics": ("forklift", "forklift license", "picking", "packing", "shipping",
                  "warehouse management", "supply chain", "logistics", "driving license",
                  "route planning", "dispatching", "procurement"),
    "trades": ("carpentry", "plumbing", "electrical installation", "painting", "masonry",
               "hvac", "construction", "occupational safety", "blueprint reading"),
    "legal_public": ("contract law", "legal research", "compliance", "gdpr",
                     "case management", "public administration", "drafting"),
    "general": ("communication", "teamwork", "problem solving", "time management",
                "project management", "driving licence"),
}

ALL_SKILLS: Tuple[str, ...] = tuple(dict.fromkeys(
    s for skills in SKILLS_BY_FIELD.values() for s in skills))
_PATTERNS = [(s, re.compile(r"(?<!\w)" + re.escape(s) + r"(?!\w)")) for s in ALL_SKILLS]


def extract_skills(text: str) -> List[str]:
    """Skills from :data:`ALL_SKILLS` that literally occur in ``text``."""
    low = (text or "").lower()
    return [s for s, rx in _PATTERNS if rx.search(low)]


__all__ = ["SKILLS_BY_FIELD", "ALL_SKILLS", "extract_skills"]
