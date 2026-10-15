# OFFERHUNT UI Redesign Changelog (Internshala-Inspired Job Portal)

## 🎨 Overview
Transformed OFFERHUNT from a utility developer dashboard into a clean, modern internship discovery portal inspired by the visual language of `internshala.com`. All backend logic, SQLite WAL persistence, verifier checks, MCP tool contracts, and reliability lab runner behaviors are 100% preserved.

---

## 🧩 Component & Visual Upgrades

### 1. Global Design System & Tokens
- **Theme Configuration (`.streamlit/config.toml`):** Configured primary accent (`#008BDC`), light backgrounds (`#FFFFFF`, `#F8FAFC`), and clean typography.
- **Typography & Fonts (`styles.css`):** Integrated Google Font **Inter** (400, 500, 600, 700, 800) with fallback system fonts. Headings set to 700/800 weight, body text to 400/500 with relaxed line heights.
- **Color Palette:**
  - Primary Blue: `#008BDC` with `#0A6DB5` hover state and `#EBF8FF` soft tint.
  - Hero Navy: `#14377D` to `#1E55B3` linear gradient with `#0C2353` stats band.
  - Accent Yellow: `#FFD54F` for high-impact highlighted keywords.
  - Neutral & Borders: Soft `#E2E8F0` / `#E5E7EB` borders, card radius `16px`, button radius `8px`, pill radius `9999px`.
- **Chrome Removal:** Cleanly hid Streamlit's default hamburger menu and footer, streamlined the header bar to a transparent backdrop blur.

### 2. Top Announcement Strip (`render_announcement_bar`)
- Replaced large yellow alert boxes with a slim, professional 28px blue gradient bar.
- Centered white text with uppercase `[NOTICE]` badge and underlined action link for demo catalog notifications.

### 3. Sticky Top Navigation Bar (`render_navbar`)
- Custom brand logo mark with vector target/arrow SVG icon and navy/blue `OFFERHUNT` wordmark.
- Interactive navigation items: **🎯 Internships**, **🧪 Reliability Lab**, and **📊 Audit Trail** with live run counts.
- Rounded search input pill (`Search roles...`).
- Styled operator and engine action buttons.

### 4. Hero Section & Stats Strip (`render_hero_header`, `render_stats_strip`)
- Implemented high-contrast navy gradient banner (`#14377D` &rarr; `#1E55B3`) with ambient radial glow.
- Headline with yellow highlight: `"Find verified #1 internships autonomously"`.
- Underlined subheading detailing autonomous capabilities.
- Darker navy stats band immediately below the hero with key metric (`5 verified opportunities per run`) and real engine source badges (`Tavily Search`, `MCP Protocol v2`, `SQLite WAL`, `Gemini 2.5`, `SHA-256 Checksums`).

### 5. Category Filter Chips (`render_filter_chips`)
- Horizontal pill chips row (`All`, `Machine Learning`, `Backend`, `Remote`, `Bangalore`, `High Stipend (₹40k+)`).
- Active state styled with filled primary blue `#008BDC`, white text, and subtle drop shadow; inactive state styled with white background, soft border, and hover lift.

### 6. Opportunity Cards Grid (`render_opportunity_card`, `render_opportunity_grid`)
- Responsive CSS Grid (4 columns on desktop, 2 on tablet, 1 on mobile).
- Card container with `16px` border-radius, soft card shadow, and interactive `-4px` vertical lift on hover.
- Structure per card:
  - Top header with bold title and verification badge (`✓ VERIFIED` or `⚠️ NEEDS REVIEW`).
  - Company name in muted slate `#64748B`.
  - Subtle divider line.
  - Metadata list with icons: Location (📍), Stipend (💵 in bold), and Duration (🗓️).
  - Footer with job type badge (`Internship with PPO`) and blue `View details ›` link.

### 7. Carousel Pagination Controls (`render_carousel_controls`)
- Centered pill with prev/next arrows, page index indicators (`1 / 3`), and a primary blue progress bar.

### 8. Restyled Clean Tabs
- Modern underlined tab bar replacing default boxed tabs.
- Active tab indicated by `3px` solid `#008BDC` underline and bold text.
- Reusable `.oh-card` containers with consistent padding and typography across all subtabs:
  - **🚀 Launch Discovery Run:** Integrated persona box, parameters, and full-width primary CTA.
  - **📋 Plan & Live Steps:** Step DAG cards with risk levels and SQLite WAL live table.
  - **🔎 Evidence & Verification:** Independent verifier checklist cards, spreadsheet viewer, draft inspector, and audit timeline.
  - **📥 Approval Inbox:** Red-tinted safety invariant card, simulate send test, and boundary approvals.
  - **📑 Final Report:** Distinct status badge, summary, and claim verification cards.

### 9. Reliability Lab Redesign
- Standardized scenario selector housed inside a modern `.oh-card`.
- **💥 Injected Fault Card:** Soft red-tinted background (`#FFF5F5`), red border (`#FECACA`), bold title with hazard icon.
- **🛡️ Expected Recovery Path Card:** Soft green-tinted background (`#F0FDF4`), emerald border (`#BBF7D0`), bold title with shield icon.
- **Three-Button Control Row:** Primary blue `Run Scenario`, secondary `Run Naive Baseline`, and full-width `Run Complete Controlled Suite`.
- **Latest Telemetry KPI Cards:** 5 modular metric cards reporting Final Status, Recovery Succeeded, Duplicate Rows, Approval Bypassed, and Execution Latency.
- **Empty State Illustration:** Replaced blank areas with a styled dotted container, test-tube icon, and helpful onboarding message.

### 10. Sidebar Diagnostics & Run Inspector
- Converted plain text lines to status pill badges:
  - `Active` / `Ready` in emerald green (`#DEF7EC`).
  - `Mock Mode` / `Demo Fixtures` in amber (`#FEF3C7`).
- Collapsed sidebar by default (`initial_sidebar_state="collapsed"`) to maximize workspace real estate for the job-portal UI.

---

## 🔒 Verification & Invariants
- **Pytest Suite:** 33 passed, 1 skipped (100% offline passing rate).
- **Backend Integrity:** Zero modifications to `agent/`, `mcp_tools/`, `reliability/`, or `storage/`.
