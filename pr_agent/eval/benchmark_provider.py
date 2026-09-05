"""A read-only GitLab provider that replays a review against a *frozen* diff.

``BenchmarkGitProvider`` subclasses :class:`GitLabProvider` so it reuses all of
its authentication, file-content and context plumbing, but serves the diff
between two frozen commit shas (``base_sha``..``head_sha``) obtained from the
GitLab *Compare API* instead of the merge request's current state.

This is the engine behind P1 "API replay": it needs only the existing GitLab
token (no local clone, no ``repos_root``). All publishing methods are no-ops so
a replay never posts anything back to GitLab.

The frozen shas are passed explicitly, or read from settings keys
``eval._replay_base_sha`` / ``eval._replay_head_sha`` when the provider is built
through the registry (which only forwards the PR url).
"""

from typing import Optional

from pr_agent.algo.file_filter import filter_ignored
from pr_agent.algo.git_patch_processing import decode_if_bytes
from pr_agent.algo.language_handler import is_valid_file
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.algo.utils import load_large_diff
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import MAX_FILES_ALLOWED_FULL
from pr_agent.git_providers.gitlab_provider import GitLabProvider
from pr_agent.log import get_logger


class BenchmarkGitProvider(GitLabProvider):
    def __init__(self, merge_request_url: str,
                 base_sha: Optional[str] = None,
                 head_sha: Optional[str] = None,
                 input_snapshot: Optional[dict] = None,
                 incremental=None):
        super().__init__(merge_request_url, incremental)
        self.base_sha = base_sha or get_settings().get("eval._replay_base_sha", None)
        self.head_sha = head_sha or get_settings().get("eval._replay_head_sha", None)
        if not self.base_sha or not self.head_sha:
            raise ValueError(
                "BenchmarkGitProvider requires base_sha and head_sha "
                "(pass explicitly or set eval._replay_base_sha/_replay_head_sha)")
        # force a fresh, compare-based diff computation
        self.diff_files = None
        self.git_files = None
        # frozen non-code inputs (title/description/commit_messages/tickets) so a
        # replay reproduces the exact prompt inputs as of review time.
        self.frozen_input = input_snapshot or get_settings().get("eval._replay_input_json", None)
        if isinstance(self.frozen_input, dict) and self.frozen_input:
            self._apply_frozen_input()

    def _apply_frozen_input(self) -> None:
        """Overlay the frozen review-time inputs onto the current MR object."""
        fi = self.frozen_input
        try:
            if fi.get("title") is not None and getattr(self, "mr", None) is not None:
                self.mr.title = fi["title"]
            if fi.get("description") is not None and getattr(self, "mr", None) is not None:
                self.mr.description = fi["description"]
            tickets = fi.get("related_tickets")
            if tickets:
                get_settings().set("related_tickets", tickets)
        except Exception as e:
            get_logger().warning(f"[benchmark] failed to apply frozen input: {e}")

    def get_commit_messages(self):
        if isinstance(self.frozen_input, dict) and self.frozen_input.get("commit_messages") is not None:
            return self.frozen_input["commit_messages"]
        return super().get_commit_messages()

    def _compare_diffs(self) -> list:
        """Return the per-file diff dicts between the two frozen shas."""
        project = self.gl.projects.get(self.id_project)
        compare = project.repository_compare(self.base_sha, self.head_sha)
        return compare.get("diffs", []) or []

    def get_diff_files(self) -> list[FilePatchInfo]:
        if self.diff_files:
            return self.diff_files

        diffs_original = self._compare_diffs()
        diffs = filter_ignored(diffs_original, 'gitlab')
        if diffs != diffs_original:
            try:
                get_logger().info(
                    f"[benchmark] filtered [ignore] files for MR {self.id_mr}",
                    extra={
                        "original_files": [d.get('new_path') for d in diffs_original],
                        "filtered_files": [d.get('new_path') for d in diffs],
                    })
            except Exception:
                pass

        diff_files = []
        invalid_files_names = []
        counter_valid = 0
        for diff in diffs:
            if not is_valid_file(diff['new_path']):
                invalid_files_names.append(diff['new_path'])
                continue

            counter_valid += 1
            if counter_valid < MAX_FILES_ALLOWED_FULL or not diff['diff']:
                original_file_content_str = self.get_pr_file_content(diff['old_path'], self.base_sha)
                new_file_content_str = self.get_pr_file_content(diff['new_path'], self.head_sha)
            else:
                if counter_valid == MAX_FILES_ALLOWED_FULL:
                    get_logger().info("[benchmark] too many files, skipping full content for the rest")
                original_file_content_str = ''
                new_file_content_str = ''

            original_file_content_str = decode_if_bytes(original_file_content_str)
            new_file_content_str = decode_if_bytes(new_file_content_str)

            edit_type = EDIT_TYPE.MODIFIED
            if diff.get('new_file'):
                edit_type = EDIT_TYPE.ADDED
            elif diff.get('deleted_file'):
                edit_type = EDIT_TYPE.DELETED
            elif diff.get('renamed_file'):
                edit_type = EDIT_TYPE.RENAMED

            filename = diff['new_path']
            patch = diff['diff']
            if not patch:
                patch = load_large_diff(filename, new_file_content_str, original_file_content_str)

            patch_lines = patch.splitlines(keepends=True)
            num_plus_lines = len([line for line in patch_lines if line.startswith('+')])
            num_minus_lines = len([line for line in patch_lines if line.startswith('-')])
            diff_files.append(
                FilePatchInfo(original_file_content_str, new_file_content_str,
                              patch=patch,
                              filename=filename,
                              edit_type=edit_type,
                              old_filename=None if diff['old_path'] == diff['new_path'] else diff['old_path'],
                              num_plus_lines=num_plus_lines,
                              num_minus_lines=num_minus_lines, ))
        if invalid_files_names:
            get_logger().info(f"[benchmark] filtered out invalid extensions: {invalid_files_names}")

        self.diff_files = diff_files
        return diff_files

    def get_files(self) -> list:
        if not self.git_files:
            self.git_files = [d.get('new_path') for d in self._compare_diffs() if d.get('new_path')]
        return self.git_files

    def get_diff_refs(self) -> Optional[dict]:
        return {"base_sha": self.base_sha, "head_sha": self.head_sha,
                "start_sha": self.base_sha}

    # --- publishing is disabled during replay (no side effects) ---
    def publish_comment(self, *args, **kwargs):
        return None

    def publish_persistent_comment(self, *args, **kwargs):
        return None

    def publish_inline_comment(self, *args, **kwargs):
        return None

    def publish_inline_comments(self, *args, **kwargs):
        return None

    def publish_code_suggestions(self, *args, **kwargs) -> bool:
        return True

    def publish_labels(self, *args, **kwargs):
        return None

    def remove_initial_comment(self, *args, **kwargs):
        return None

    def remove_comment(self, *args, **kwargs):
        return None

    def add_eyes_reaction(self, *args, **kwargs):
        return None
