"""OFFERHUNT UI Theme & Component Library.

Provides reusable HTML/CSS components matching the clean, modern job-portal aesthetic
inspired by internshala.com (Inter typography, primary blue #008BDC, hero navy gradients,
responsive opportunity cards, chips, stats bands, and reliability visualizers).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
import streamlit as st

STYLES_PATH = Path(__file__).parent / "styles.css"


def clean_html(html_str: str) -> str:
    """Strips all leading and trailing whitespace from every line.
    Prevents CommonMark from ever misinterpreting 4-space indentation as a code block.
    """
    return " ".join(line.strip() for line in html_str.splitlines() if line.strip())


def render_html(html_str: str) -> None:
    """Renders clean HTML safely into Streamlit without markdown formatting leaks."""
    st.markdown(clean_html(html_str), unsafe_allow_html=True)


def inject_theme() -> None:
    """Injects Google Fonts and custom CSS into the Streamlit app."""
    css_content = ""
    if STYLES_PATH.exists():
        css_content = STYLES_PATH.read_text(encoding="utf-8")
    
    st.markdown(f"<style>{css_content}</style>", unsafe_allow_html=True)


def render_announcement_bar(text: str, link_text: str = "Evaluation Mode Active", link_url: str = "#") -> None:
    """Renders top slim announcement bar for notices like DEMO DATA."""
    html = f"""
<div class="oh-announcement-strip">
<span class="tag">NOTICE</span>
<span>{text}</span>
<a href="{link_url}">{link_text} &rarr;</a>
</div>
"""
    render_html(html)


def render_hero_content(
    headline_part1: str = "Find verified",
    highlight_word: str = "#1 internships",
    headline_part2: str = "autonomously",
    subheading: str = "Deterministic contract extraction, atomic workbook upserts, and cryptographic proof-validation.",
) -> str:
    """Returns HTML for hero text side."""
    return clean_html(f"""
<div class="oh-hero">
<h1 class="oh-hero-headline">
{headline_part1} <span class="highlight">{highlight_word}</span> {headline_part2}
</h1>
<div class="oh-hero-subheading">
{subheading}
</div>
</div>
""")


def render_stats_strip(key_metric: str = "5 verified opportunities per run", sources: Optional[List[str]] = None) -> None:
    """Renders darker navy band immediately below the hero with metrics and partner badges."""
    if sources is None:
        sources = ["Tavily Search", "MCP Protocol v2", "SQLite WAL", "Gemini 2.5", "SHA-256 Checksums"]
    
    badges_html = "".join([
        f'<div class="oh-source-badge"><span>✓</span> {s}</div>' for s in sources
    ])

    html = f"""
<div class="oh-stats-strip">
<div class="oh-stats-key">
<span>⚡ GUARANTEED:</span> {key_metric}
</div>
<div class="oh-stats-sources">
{badges_html}
</div>
</div>
"""
    render_html(html)


def render_section_heading(title: str, subtitle: str = "", icon: str = "") -> None:
    """Renders bold section title with optional icon and secondary subtitle."""
    icon_html = f"<span>{icon}</span> " if icon else ""
    html = f"""
<div class="oh-section-header">
<h2 class="oh-section-title">{icon_html}{title}</h2>
{"<p class='oh-section-subtitle'>" + subtitle + "</p>" if subtitle else ""}
</div>
"""
    render_html(html)


def render_opportunity_card(
    title: str,
    company: str,
    location: str,
    stipend: str,
    duration: str = "2-6 Months",
    tag: str = "Internship",
    verified: bool = True,
    source_url: str = "#",
) -> str:
    """Returns clean HTML string for an opportunity card."""
    badge_html = (
        '<span class="oh-badge-verified">✓ VERIFIED</span>'
        if verified
        else '<span class="oh-badge-review">⚠️ NEEDS REVIEW</span>'
    )
    
    safe_url = source_url if source_url and source_url.startswith("http") else "https://internshala.com"

    card_str = f"""
<div class="oh-job-card">
<div class="oh-job-card-header">
<div class="oh-job-card-top">
<h3 class="oh-job-title">{title}</h3>
{badge_html}
</div>
<div class="oh-company-name">{company}</div>
</div>
<div class="oh-card-divider"></div>
<div class="oh-meta-list">
<div class="oh-meta-item">
<div class="oh-meta-icon">📍</div>
<div>{location}</div>
</div>
<div class="oh-meta-item">
<div class="oh-meta-icon">💵</div>
<div><strong>{stipend}</strong></div>
</div>
<div class="oh-meta-item">
<div class="oh-meta-icon">🗓️</div>
<div>{duration}</div>
</div>
</div>
<div class="oh-job-card-footer">
<span class="oh-job-type-tag">{tag}</span>
<a class="oh-view-details-link" href="{safe_url}" target="_blank" rel="noopener noreferrer">View details &rsaquo;</a>
</div>
</div>
"""
    return clean_html(card_str)


def render_opportunity_grid(cards_html: List[str]) -> None:
    """Renders cards in a responsive CSS grid without markdown code block interpretation."""
    all_cards = "".join(cards_html)
    render_html(f'<div class="oh-card-grid">{all_cards}</div>')


def render_diagnostics_pill(name: str, value: str, status_type: str = "green") -> str:
    """Returns clean HTML string for a diagnostics pill in the sidebar."""
    cls = {
        "green": "oh-status-pill-green",
        "amber": "oh-status-pill-amber",
        "red": "oh-status-pill-red",
    }.get(status_type, "oh-status-pill-green")

    pill_str = f"""
<div class="oh-diag-pill">
<span class="oh-diag-pill-name">{name}</span>
<span class="{cls}">{value}</span>
</div>
"""
    return clean_html(pill_str)


def render_fault_and_recovery_cards(fault_title: str, fault_desc: str, recovery_title: str, recovery_desc: str) -> None:
    """Renders side-by-side soft red and soft green cards for Reliability Lab."""
    html = f"""
<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1.25rem; margin-bottom: 1.25rem;">
<div class="oh-fault-card">
<div class="oh-fault-title">💥 {fault_title}</div>
<div class="oh-fault-desc">{fault_desc}</div>
</div>
<div class="oh-recovery-card">
<div class="oh-recovery-title">🛡️ {recovery_title}</div>
<div class="oh-recovery-desc">{recovery_desc}</div>
</div>
</div>
"""
    render_html(html)


def render_kpi_card(title: str, value: str, subtitle: str = "") -> str:
    """Returns clean HTML string for a single KPI card."""
    card_str = f"""
<div class="oh-kpi-card">
<div class="oh-kpi-title">{title}</div>
<div class="oh-kpi-value">{value}</div>
<div class="oh-kpi-subtitle">{subtitle}</div>
</div>
"""
    return clean_html(card_str)


def render_empty_state(icon: str = "🧪", title: str = "No executions yet", desc: str = "Launch a scenario run or full suite above to gather measured reliability telemetry.") -> None:
    """Renders clean empty state when no runs or metrics exist."""
    html = f"""
<div class="oh-empty-state">
<div class="oh-empty-icon">{icon}</div>
<div class="oh-empty-title">{title}</div>
<div class="oh-empty-desc">{desc}</div>
</div>
"""
    render_html(html)
