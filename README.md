# NASA Task Book (`taskbook.nasaprs.com`) Harvester & Static Mirror

Automated crawler, parser, static site generator, and dual sitemap pipeline for the **NASA Task Book: Biological and Physical Sciences & Human Research** ([https://taskbook.nasaprs.com](https://taskbook.nasaprs.com)).

## Overview

The NASA Task Book is the authoritative database of research projects funded by:
- **Biological & Physical Sciences (BPS) Division** (Space Biology, Physical Sciences)
- **Human Research Program (HRP)**
- **Translational Research Institute for Space Health (TRISH)**
- **National Space Biomedical Research Institute (NSBRI)**
- **NASA EPSCoR**

This repository provides:
1. **Pre-rendered Static HTML Pages**: Every project record is pre-rendered into clean, accessible, semantic HTML under `tasks/<TASKID>.html` and served via GitHub Pages for fast, 100% full-text indexing by enterprise search engines (Onyx) and AI agents without ColdFusion session bottlenecks.
2. **Authoritative Source-of-Truth Citations**: Each pre-rendered page links directly back to the live NASA Task Book record (`https://taskbook.nasaprs.com/tbp/index.cfm?action=public_query_taskbook_content&TASKID=...`) and the official PDF report (`https://taskbook.nasaprs.com/tbp/tbpdf.cfm?id=...`).
3. **Structured Dataset (`taskbook_tasks.jsonl`)**: Machine-readable JSON Lines dataset containing complete metadata, investigator contacts, grant numbers, solicitations, project abstracts, research impacts, annual progress reports, and cumulative publications.
4. **Dual Sitemaps**:
   - `taskbook_sitemap.xml`: Primary XML sitemap referencing GitHub Pages URLs for Onyx Web Connector ingestion.
   - `taskbook_direct_sitemap.xml`: Official XML sitemap pointing directly to live NASA URLs.
   - `taskbook_urls.txt` / `taskbook_direct_urls.txt`: Plain text URL lists.
5. **Interactive Catalog (`index.html`)**: Instant client-side searchable catalog with live filters by division, fiscal year, PI, institution, and keyword.
6. **Automated Weekly Sync (`.github/workflows/update-sitemap.yml`)**: GitHub Actions workflow running weekly on Sundays at 00:00 UTC to discover new research projects, annual progress report updates, and bibliography additions.

## Repository Structure

```
.
├── .github/
│   └── workflows/
│       └── update-sitemap.yml      # Automated weekly crawler & deployment
├── tasks/                          # Pre-rendered HTML project cards (2,918 tasks)
│   ├── 055D2E4355B36F21...html
│   └── ...
├── generate_taskbook_sitemap.py    # Main scraper, parser & static generator
├── index.html                      # Interactive searchable catalog dashboard
├── taskbook_tasks.jsonl            # Complete structured metadata dataset (JSONL)
├── taskbook_sitemap.xml            # GitHub Pages sitemap for Onyx Web Connector
├── taskbook_urls.txt               # Plain text GitHub Pages URL list
├── taskbook_direct_sitemap.xml     # Live NASA Task Book sitemap
├── taskbook_direct_urls.txt        # Plain text live NASA URL list
├── requirements.txt                # Python dependencies (standard library compatible)
└── README.md                       # Documentation
```

## Onyx Search & Agent Integration

To ingest this repository into Onyx:
1. Navigate to **Onyx Admin &rarr; Add Connector &rarr; Web**.
2. Select **Sitemap** mode.
3. Enter the raw sitemap URL:
   ```
   https://raw.githubusercontent.com/Oht8wooWi8yait9n/taskbook/main/taskbook_sitemap.xml
   ```
4. Under **URL Rewrites** (in Advanced Options), add a prefix rewrite rule so citations in Onyx point directly to NASA:
   * **Source Prefix**: `https://oht8woowi8yait9n.github.io/taskbook/tasks/`
   * **Target Prefix**: `https://taskbook.nasaprs.com/tbp/index.cfm?action=public_query_taskbook_content&TASKID=`
   *(Note: Every static card also has `<link rel="canonical">` and automatic human-browser forwarding as a fallback).*
5. Set crawling frequency (e.g., Weekly).
6. Attach the connector to your desired **Document Set** (e.g. `NASA Task Book`) and link it to **Persona 22 (`Taskbook`)**.
7. Trigger an initial sync. Onyx will index all 2,918 static research projects with rich titles, metadata, and direct source-of-truth citations.

## Running Locally

```bash
# Optional: create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Run the complete harvest and site generator
python3 generate_taskbook_sitemap.py
```
