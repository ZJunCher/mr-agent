# Recent Updates and Future Roadmap


This page summarizes recent enhancements to Qodo Merge.

It also outlines our development roadmap for the upcoming three months. Please note that the roadmap is subject to change, and features may be adjusted, added, or reprioritized.

=== "Recent Updates"
    | Date | Feature | Description |
    |---|---|---|
    | 2025-09-17 | **Qodo Merge CLI** | A new command-line interface for Qodo Merge, enabling developers to implement code suggestions directly in your terminal. ([Learn more](https://qodo-merge-docs.qodo.ai/qodo-merge-cli/)) |
    | 2025-09-12 | **Repo Metadata** | You can now add metadata from files like `AGENTS.md`, which will be applied to all PRs in that repository. ([Learn more](https://qodo-merge-docs.qodo.ai/usage-guide/additional_configurations/#bringing-additional-repository-metadata-to-qodo-merge)) |
    | 2025-08-11 | **RAG support for GitLab** | All Qodo Merge RAG features are now available in GitLab. ([Learn more](https://qodo-merge-docs.qodo.ai/core-abilities/rag_context_enrichment/)) |
    | 2025-07-29 | **High-level Suggestions** | Qodo Merge now also provides high-level code suggestion for PRs. ([Learn more](https://qodo-merge-docs.qodo.ai/core-abilities/high_level_suggestions/)) |
    | 2025-07-20 | **PR to Ticket** | Generate tickets in your tracking systems based on PR content. ([Learn more](https://qodo-merge-docs.qodo.ai/tools/pr_to_ticket/)) |
    | 2025-07-17 | **Compliance** | Comprehensive compliance checks for security, ticket requirements, and custom organizational rules. ([Learn more](https://qodo-merge-docs.qodo.ai/tools/compliance/)) |
    | 2025-06-21 | **Mermaid Diagrams** | Qodo Merge now generates by default Mermaid diagrams for PRs, providing a visual representation of code changes. ([Learn more](https://qodo-merge-docs.qodo.ai/tools/describe/#sequence-diagram-support)) |
    | 2025-06-11 | **Best Practices Hierarchy** | Introducing support for structured best practices, such as for folders in monorepos or a unified best practice file for a group of repositories. ([Learn more](https://qodo-merge-docs.qodo.ai/tools/improve/#global-hierarchical-best-practices)) |
    | 2025-06-01 | **CLI Endpoint** | A new Qodo Merge endpoint that accepts a lists of before/after code changes, executes Qodo Merge commands, and return the results. Currently available for enterprise customers. Contact [Qodo](https://www.qodo.ai/contact/) for more information. |

=== "Future Roadmap"
    - **`Compliance` tool to replace `review` as default**: Planning to make the `compliance` tool the default automatic command instead of the current `review` tool.
    - **Smarter context retrieval**: Leverage AST and LSP analysis to gather relevant context from across the entire repository.
    - **Enhanced portal experience**: Improved user experience in the Qodo Merge portal with new options and capabilities.
