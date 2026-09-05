# Review and Implement AI Suggestions from Your Terminal

**Qodo Merge CLI** utilizes [Qodo Command](https://docs.qodo.ai/qodo-documentation/qodo-command) to bring AI-powered code suggestions directly to your terminal.
Review, implement, and manage Qodo Merge suggestions without leaving your development environment.

![Qodo Merge CLI Main Interface](https://www.qodo.ai/images/pr_agent/qm_cli_main_table_fix_all.png){width=768}

## Mission

The CLI can bridge the gap between Qodo Merge feedback and code implementation in your local enviroment:

- **Seamlessly generate and manage PR suggestions** without context switching
    - Remote Suggestions: Fetches Qodo Merge suggestions from your Git Environment
    - Local Suggestions: Get real-time suggestions against your local changes
- **Interactive review and implementation** of AI feedback directly in your terminal
- **Track implementation status** of each suggestion (pending/implemented/declined)

## Remote Suggestions Flow 
1. Open a Pull Request on your Git environment and receive Qodo Merge feedback
2. Pull the remote suggestions into your terminal with Qodo Merge CLI
3. Explore, Review, and implement suggestions interactively
4. Commit changes back to your branch seamlessly

## Local Suggestions Flow
Work in progress - coming soon!

## Quick Start

1. **[Install](installation.md)** Qodo Merge CLI
2. **[Usage](usage.md)** - Navigate, explore, and implement suggestions

---

*Part of the Qodo Merge ecosystem - closing the loop between AI feedback and code implementation.*