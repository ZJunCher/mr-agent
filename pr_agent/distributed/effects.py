import hashlib
import inspect
import json
from dataclasses import dataclass
from typing import Any, Callable

from pr_agent.distributed.broker import EffectRecord
from pr_agent.distributed.runtime import ExecutionRuntime, get_execution_runtime


@dataclass(frozen=True)
class EffectResult:
    value: Any
    reconciled: bool = False


def _effect_key(runtime: ExecutionRuntime, effect_name: str) -> str:
    return f"{runtime.task_id}:{effect_name}"


def _stable_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def effect_marker(task_id: str, effect_name: str) -> str:
    return f"<!-- pr-agent-task:{task_id}:effect:{_stable_digest(effect_name)} -->"


class EffectGuard:
    def __init__(self, runtime: ExecutionRuntime | None = None) -> None:
        self.runtime = runtime or get_execution_runtime(required=True)

    async def run(self, effect_name: str, reconcile: Callable, action: Callable) -> Any:
        await self.runtime.raise_if_canceled_async()
        key = _effect_key(self.runtime, effect_name)
        claim = await self.runtime.broker.claim_effect(key, self.runtime.lease)
        if claim.status == "completed":
            return claim.result
        reconciled = reconcile(claim.metadata)
        if inspect.isawaitable(reconciled):
            reconciled = await reconciled
        if reconciled is not None:
            await self.runtime.broker.complete_effect(key, self.runtime.lease, reconciled)
            return reconciled
        result = action()
        if inspect.isawaitable(result):
            result = await result
        await self.runtime.broker.complete_effect(key, self.runtime.lease, result)
        return result


class SyncEffectGuard:
    def __init__(self, runtime: ExecutionRuntime | None = None) -> None:
        self.runtime = runtime or get_execution_runtime(required=True)

    def claim(self, effect_name: str, metadata: dict[str, Any] | None = None) -> EffectRecord:
        self.runtime.raise_if_canceled()
        return self.runtime.sync_broker.claim_effect(
            _effect_key(self.runtime, effect_name),
            self.runtime.lease,
            metadata,
        )

    def record_started(self, effect_name: str, metadata: dict[str, Any]) -> EffectRecord:
        claim = self.claim(effect_name, metadata)
        if claim.status == "started" and claim.metadata != metadata:
            self.record_metadata(effect_name, metadata)
            return EffectRecord("started", metadata)
        return claim

    def record_metadata(self, effect_name: str, metadata: dict[str, Any]) -> None:
        self.runtime.raise_if_canceled()
        updated = self.runtime.sync_broker.update_effect_metadata(
            _effect_key(self.runtime, effect_name),
            self.runtime.lease,
            metadata,
        )
        if not updated:
            raise RuntimeError(f"effect is not writable: {effect_name}")

    def complete(self, effect_name: str, result: Any) -> None:
        completed = self.runtime.sync_broker.complete_effect(
            _effect_key(self.runtime, effect_name),
            self.runtime.lease,
            result,
        )
        if not completed:
            raise RuntimeError(f"effect does not exist: {effect_name}")

    def run(self, effect_name: str, reconcile: Callable[[dict[str, Any]], Any], action: Callable[[], Any]) -> Any:
        claim = self.claim(effect_name)
        if claim.status == "completed":
            return claim.result
        reconciled = reconcile(claim.metadata)
        if reconciled is not None:
            self.complete(effect_name, reconciled)
            return reconciled
        result = action()
        self.complete(effect_name, result)
        return result


class _StoredComment:
    def __init__(self, comment_id: Any, body: str = "") -> None:
        self.id = comment_id
        self.body = body


class IdempotentGitProvider:
    """Guard queue-mode Git provider writes with fencing and deterministic reconciliation markers."""

    def __init__(self, original_provider, runtime: ExecutionRuntime | None = None) -> None:
        self.original_provider = original_provider
        self.runtime = runtime or get_execution_runtime(required=True)
        self.effects = SyncEffectGuard(self.runtime)

    def __getattr__(self, name):
        return getattr(self.original_provider, name)

    def _effect_name(self, operation: str, payload: Any) -> str:
        return f"{operation}:{_stable_digest(payload)}"

    def _marked_body(self, body: str, effect_name: str) -> tuple[str, str]:
        marker = effect_marker(self.runtime.task_id, effect_name)
        max_chars = int(getattr(self.original_provider, "max_comment_chars", 0) or 0)
        if max_chars and len(body) + len(marker) + 2 > max_chars:
            body = body[: max(0, max_chars - len(marker) - 5)] + "..."
        return f"{body}\n\n{marker}", marker

    def _find_comment(self, marker: str):
        try:
            comments = self.original_provider.get_issue_comments()
        except Exception:
            try:
                comments = self.original_provider.mr.notes.list(get_all=True)
            except TypeError:
                comments = self.original_provider.mr.notes.list(all=True)
            except Exception:
                return None
        for comment in comments or []:
            if marker in str(getattr(comment, "body", "") or ""):
                return comment
        return None

    def _publish_comment_effect(self, effect_name: str, body: str, action: Callable[[str], Any]):
        marked_body, marker = self._marked_body(body, effect_name)
        claim = self.effects.claim(effect_name)
        existing = self._find_comment(marker)
        if existing is not None:
            if claim.status != "completed":
                self.effects.complete(effect_name, {"comment_id": str(getattr(existing, "id", ""))})
            return existing
        if claim.status == "completed":
            result = claim.result if isinstance(claim.result, dict) else {}
            return _StoredComment(result.get("comment_id", ""), marked_body)
        comment = action(marked_body)
        self.effects.complete(effect_name, {"comment_id": str(getattr(comment, "id", ""))})
        return comment

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        effect_name = self._effect_name("publish_comment", {"body": pr_comment, "temporary": is_temporary})
        return self._publish_comment_effect(
            effect_name,
            pr_comment,
            lambda body: self.original_provider.publish_comment(body, is_temporary=is_temporary),
        )

    def publish_persistent_comment(
        self,
        pr_comment: str,
        initial_header: str,
        update_header: bool = True,
        name: str = "review",
        final_update_message: bool = True,
    ):
        payload = {
            "body": pr_comment,
            "initial_header": initial_header,
            "update_header": update_header,
            "name": name,
            "final_update_message": final_update_message,
        }
        effect_name = self._effect_name("publish_persistent_comment", payload)
        return self._publish_comment_effect(
            effect_name,
            pr_comment,
            lambda body: self.original_provider.publish_persistent_comment(
                body,
                initial_header,
                update_header,
                name,
                final_update_message,
            ),
        )

    def publish_inline_comment(
        self,
        body: str,
        relevant_file: str,
        relevant_line_in_file: str,
        original_suggestion=None,
    ):
        payload = {
            "body": body,
            "file": relevant_file,
            "line": relevant_line_in_file,
            "suggestion": original_suggestion,
        }
        effect_name = self._effect_name("publish_inline_comment", payload)
        return self._publish_comment_effect(
            effect_name,
            body,
            lambda marked: self.original_provider.publish_inline_comment(
                marked,
                relevant_file,
                relevant_line_in_file,
                original_suggestion,
            ),
        )

    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        results = []
        for suggestion in code_suggestions:
            suggestion = dict(suggestion)
            effect_name = self._effect_name("publish_code_suggestion", suggestion)
            marked, marker = self._marked_body(str(suggestion.get("body") or ""), effect_name)
            claim = self.effects.claim(effect_name)
            if self._find_comment(marker) is not None or claim.status == "completed":
                if claim.status != "completed":
                    self.effects.complete(effect_name, True)
                results.append(True)
                continue
            suggestion["body"] = marked
            result = bool(self.original_provider.publish_code_suggestions([suggestion]))
            self.effects.complete(effect_name, result)
            results.append(result)
        return all(results)

    def publish_inline_suggestions(self, code_suggestions: list) -> list:
        results = []
        for suggestion in code_suggestions:
            suggestion = dict(suggestion)
            effect_name = self._effect_name("publish_inline_suggestion", suggestion)
            marked, marker = self._marked_body(str(suggestion.get("body") or ""), effect_name)
            claim = self.effects.claim(effect_name)
            existing = self._find_comment(marker)
            if existing is not None:
                result = {
                    "suggestion_id": suggestion.get("suggestion_id"),
                    "discussion_id": getattr(existing, "discussion_id", None),
                    "note_id": getattr(existing, "id", None),
                    "publish_status": "published",
                    "skip_reason": "",
                }
                if claim.status != "completed":
                    self.effects.complete(effect_name, result)
                results.append(result)
                continue
            if claim.status == "completed":
                results.append(claim.result)
                continue
            suggestion["body"] = marked
            published = self.original_provider.publish_inline_suggestions([suggestion]) or []
            result = published[0] if published else {
                "suggestion_id": suggestion.get("suggestion_id"),
                "discussion_id": None,
                "note_id": None,
                "publish_status": "failed",
                "skip_reason": "no_result",
            }
            self.effects.complete(effect_name, result)
            results.append(result)
        return results

    def publish_description(self, pr_title: str, pr_body: str):
        effect_name = self._effect_name("publish_description", {"title": pr_title, "body": pr_body})

        def reconcile(_metadata):
            mr = getattr(self.original_provider, "mr", None)
            if (
                mr is not None
                and getattr(mr, "title", None) == pr_title
                and getattr(mr, "description", None) == pr_body
            ):
                return True
            return None

        def publish():
            self.original_provider.publish_description(pr_title, pr_body)
            if reconcile({}) is None:
                raise RuntimeError("Git provider did not persist the requested description")
            return True

        return self.effects.run(
            effect_name,
            reconcile,
            publish,
        )

    def publish_labels(self, labels: list):
        normalized = sorted(set(labels))
        effect_name = self._effect_name("publish_labels", normalized)

        def reconcile(_metadata):
            current = self.original_provider.get_pr_labels(update=True)
            return True if set(current or []) == set(normalized) else None

        def publish():
            self.original_provider.publish_labels(normalized)
            if reconcile({}) is None:
                raise RuntimeError("Git provider did not persist the requested labels")
            return True

        return self.effects.run(
            effect_name,
            reconcile,
            publish,
        )
