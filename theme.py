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
    
    # Strip any empty lines or strange spacing in style block
    st.markdown(f"<style>{css_content}</style>", unsafe_allow_html=True)


def render_announcement_bar(text: str, link_text: str = "Learn more", link_url: str = "#") -> None:
    """Renders top slim announcement bar for notices like DEMO DATA."""
    html = f"""
<div class="oh-announcement-strip">
<span class="tag">NOTICE</span>
<span>{text}</span>
<a href="{link_url}">{link_text} &rarr;</a>
</div>
"""
    render_html(html)


def render_navbar(active_mode: str = "discover", run_count: int = 0) -> None:
    """Renders top sticky job-portal navbar with logo, nav links, and action buttons."""
    disc_active = "active" if "discover" in active_mode.lower() else ""
    rel_active = "active" if "reliab" in active_mode.lower() else ""

    logo_svg = """<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><circle cx="12" cy="12" r="6"></circle><circle cx="12" cy="12" r="2"></circle><path d="M12 2v4M12 18v4M2 12h4M18 12h4"></path></svg>"""

    html = f"""
<div class="oh-navbar">
<div class="oh-logo-group">
<div class="oh-logo-mark">{logo_svg}</div>
<div class="oh-logo-text">OFFER<span>HUNT</span></div>
</div>
<div class="oh-nav-links">
<span class="oh-nav-item {disc_active}">🎯 Internships</span>
<span class="oh-nav-item {rel_active}">🧪 Reliability Lab</span>
<span class="oh-nav-item">📊 Audit Trail <span style="font-size:0.75rem; background:#E2E8F0; padding:1px 6px; border-radius:999px;">{run_count} runs</span></span>
</div>
<div class="oh-nav-actions">
<div class="oh-search-pill">
<span>🔍</span>
<span>Search roles...</span>
</div>
<span class="oh-nav-btn-outlined">Operator</span>
<span class="oh-nav-btn-primary">Autonomous Engine</span>
</div>
</div>
"""
    render_html(html)


def render_hero_header(headline_part1: str = "Find verified", highlight_word: str = "#1 internships", headline_part2: str = "autonomously", subheading: str = "AI-powered discovery, contract term extraction, and cryptographic proof-validation.") -> None:
    """Renders hero header with navy gradient, highlighted yellow keyword, and subheading."""
    html = f"""
<div class="oh-hero">
<h1 class="oh-hero-headline">
{headline_part1} <span class="highlight">{highlight_word}</span> {headline_part2}
</h1>
<div class="oh-hero-subheading">
{subheading}
</div>
</div>
"""
    render_html(html)


def render_stats_strip(key_metric: str = "5 opportunities per run", sources: Optional[List[str]] = None) -> None:
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


def render_filter_chips(chips: List[Dict[str, Any]], active_label: str = "All") -> None:
    """Renders horizontal filter chips row."""
    chips_html = ""
    for c in chips:
        lbl = c.get("label", "")
        count = c.get("count", "")
        is_act = "active" if lbl.lower() == active_label.lower() else ""
        cnt_html = f'<span class="count">{count}</span>' if count else ""
        chips_html += f'<span class="oh-chip {is_act}">{lbl} {cnt_html}</span>'

    html = f"""
<div class="oh-chips-row">
{chips_html}
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
    
    safe_url = source_url if source_url and source_url.startswith("http") else "#"
    link_attr = f'href="{safe_url}" target="_blank"' if safe_url != "#" else ""

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
<a class="oh-view-details-link" {link_attr}>View details &rsaquo;</a>
</div>
</div>
"""
    return clean_html(card_str)


def render_opportunity_grid(cards_html: List[str]) -> None:
    """Renders cards in a responsive CSS grid without markdown code block interpretation."""
    all_cards = "".join(cards_html)
    render_html(f'<div class="oh-card-grid">{all_cards}</div>')


def render_carousel_controls(current_page: int = 1, total_pages: int = 3) -> None:
    """Renders carousel controls pill with progress bar."""
    pct = int((current_page / max(1, total_pages)) * 100)
    html = f"""
<div class="oh-carousel-bar">
<div class="oh-carousel-pill">
<span class="oh-carousel-arrow">&larr;</span>
<div class="oh-carousel-progress">
<div class="oh-carousel-progress-fill" style="width: {pct}%;"></div>
</div>
<span style="font-size:0.8rem; color:#64748B; font-weight:600;">{current_page} / {total_pages}</span>
<span class="oh-carousel-arrow">&rarr;</span>
</div>
</div>
"""
    render_html(html)


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
<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 1.25rem;">
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
