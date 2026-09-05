import hashlib
import json
import math
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, unquote

import redis
import redis.asyncio as async_redis

from pr_agent.distributed.config import DistributedSettings
from pr_agent.distributed.models import (
    TERMINAL_TASK_STATUSES,
    DeliveryKind,
    InboxDelivery,
    IngressDelivery,
    MrKey,
    NotificationEnvelope,
    PipelineEvent,
    PostRepairUTState,
    PostRepairUTStatus,
    RepairCategory,
    RepairItem,
    RepairItemStatus,
    TaskEnvelope,
    TaskKind,
    TaskStatus,
    TriageCardBinding,
    TriageCardState,
)
from pr_agent.triage.final_repair_report import (
    REPORT_TERMINAL_STATUSES,
    FinalRepairReportInput,
    FinalRepairReportState,
    RepairReportStatus,
)
from pr_agent.triage.pipeline_repair import PipelineRepairState
from pr_agent.triage.repair_rollback import (
    RepairCommitEntry,
    RepairCommitManifest,
    RepairRollbackState,
    RepairRollbackStatus,
    cancel_reverts_pushed_commits,
    repair_rollback_enabled,
)


class RepairManifestConflict(RuntimeError):
    """The durable repair commit manifest cannot be changed safely."""


APPEND_REPAIR_COMMIT_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then return {-1, 'task missing'} end
if ARGV[1] ~= '' then
  local owner = redis.call('HGET', KEYS[2], 'worker_id')
  local token = redis.call('HGET', KEYS[2], 'fencing_token')
  if owner ~= ARGV[1] or token ~= ARGV[2] then return {-2, 'lost lease'} end
end
local payload_ok, payload = pcall(cjson.decode, redis.call('HGET', KEYS[1], 'payload') or '')
local entry_ok, entry = pcall(cjson.decode, ARGV[3])
local command = payload_ok and (payload.command or '') or ''
local repair_command = payload.kind == 'post_repair_ut' or string.find(command, '^/triage')
  or string.find(command, '^/fix%-format')
  or string.find(command, '^/fix_format') or string.find(command, '^/repair%-pipeline')
if not payload_ok or not entry_ok or not payload.mr or not repair_command then
  return {-3, 'task is not a repair command'}
end
local raw = redis.call('HGET', KEYS[1], 'repair_commit_manifest') or ''
local manifest
if raw == '' then
  manifest = {
    schema_version = 1,
    repair_task_id = payload.task_id,
    project_id = payload.mr.project_id,
    mr_iid = payload.mr.iid,
    source_branch = ARGV[5],
    base_commit_sha = entry.parent_sha,
    base_tree_sha = ARGV[4],
    authorized_actor_id = ARGV[6],
    entries = {}, frozen = false, frozen_at = ''
  }
else
  local manifest_ok
  manifest_ok, manifest = pcall(cjson.decode, raw)
  if not manifest_ok or type(manifest.entries) ~= 'table' then return {-3, 'stored manifest is invalid'} end
end
if manifest.frozen then return {-4, 'manifest is frozen'} end
if manifest.repair_task_id ~= payload.task_id or manifest.project_id ~= payload.mr.project_id
  or tonumber(manifest.mr_iid) ~= tonumber(payload.mr.iid) or manifest.source_branch ~= ARGV[5]
  or manifest.base_tree_sha ~= ARGV[4] or manifest.authorized_actor_id ~= ARGV[6] then
  return {-5, 'manifest identity changed'}
end
local count = #manifest.entries
if count > 0 then
  local last = manifest.entries[count]
  if tonumber(entry.sequence) == tonumber(last.sequence) and entry.commit_sha == last.commit_sha
    and entry.parent_sha == last.parent_sha and entry.tree_sha == last.tree_sha
    and entry.effect_id == last.effect_id and entry.task_marker == last.task_marker
    and entry.pushed_at == last.pushed_at then
    return {0, raw}
  end
end
if tonumber(entry.sequence) ~= count + 1 then return {-5, 'commit sequence is not continuous'} end
local expected_parent = manifest.base_commit_sha
if count > 0 then expected_parent = manifest.entries[count].commit_sha end
if entry.parent_sha ~= expected_parent then return {-5, 'commit parent does not match'} end
for _, existing in ipairs(manifest.entries) do
  if existing.commit_sha == entry.commit_sha then return {-5, 'duplicate commit SHA'} end
end
table.insert(manifest.entries, entry)
local encoded = cjson.encode(manifest)
redis.call('HSET', KEYS[1], 'repair_commit_manifest', encoded, 'updated_at', ARGV[7])
return {1, encoded}
"""


ADMIT_POST_REPAIR_UT_LUA = """
if redis.call('EXISTS', KEYS[5]) == 0 then return {-1, ''} end
if redis.call('GET', KEYS[8]) ~= ARGV[5] then return {-5, ''} end
local card_state = redis.call('HGET', KEYS[5], 'state') or ''
local card_task_id = redis.call('HGET', KEYS[5], 'task_id') or ''
local message_id = redis.call('HGET', KEYS[5], 'open_message_id') or ''
local receive_id = redis.call('HGET', KEYS[5], 'receive_id') or ''
local revision = tonumber(redis.call('HGET', KEYS[5], 'revision') or '-1')
local pipeline_id = tonumber(redis.call('HGET', KEYS[5], 'current_pipeline_id') or '0')
local pipeline_sha = redis.call('HGET', KEYS[5], 'current_pipeline_sha') or ''
local active_task_id = redis.call('HGET', KEYS[5], 'active_task_id') or ''
if card_state ~= 'repair_succeeded' or card_task_id ~= ARGV[6]
  or message_id ~= ARGV[7] or receive_id ~= ARGV[8]
  or revision ~= tonumber(ARGV[9]) or pipeline_id ~= tonumber(ARGV[10])
  or pipeline_sha ~= ARGV[11] then return {-5, ''} end
local ut_ok, ut = pcall(cjson.decode, redis.call('HGET', KEYS[5], 'post_repair_ut') or '{}')
if not ut_ok or (ut.status or 'idle') ~= 'idle' then return {-5, ''} end
local coverage = tonumber(ARGV[16])
if coverage and coverage >= tonumber(ARGV[12]) then return {-5, ''} end
local items_ok, items = pcall(cjson.decode, redis.call('HGET', KEYS[5], 'repair_items') or '[]')
if not items_ok or #items == 0 then return {-5, ''} end
for _, item in ipairs(items) do
  if item.status ~= 'succeeded' and item.status ~= 'resolved' then return {-5, ''} end
end
local existing = redis.call('GET', KEYS[1])
if existing then return {0, existing, 0, ''} end
local active = redis.call('GET', KEYS[6])
if active_task_id ~= '' or (active and active ~= '') then return {-6, active or active_task_id} end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
redis.call('HSET', KEYS[2],
  'payload', ARGV[3], 'status', 'queued', 'attempt', '0',
  'created_at', ARGV[4], 'updated_at', ARGV[4],
  'admission_state', 'preparing', 'admission_context', ARGV[13])
redis.call('SADD', KEYS[4], ARGV[1])
redis.call('SET', KEYS[6], ARGV[1])
redis.call('ZADD', KEYS[9], ARGV[4], ARGV[1])
local ingress_id = redis.call('XADD', KEYS[3], '*', 'task_id', ARGV[1])
redis.call('HSET', KEYS[2], 'admission_state', 'enqueued', 'ingress_message_id', ingress_id)
redis.call('SET', KEYS[7], ARGV[5], 'EX', ARGV[14])
redis.call('HSET', KEYS[5],
  'active_task_id', ARGV[1], 'active_category', 'unit_test',
  'post_repair_ut', ARGV[15], 'revision', revision + 1,
  'updated_at', ARGV[4])
redis.call('EXPIRE', KEYS[5], ARGV[14])
return {1, ARGV[1], 0, ingress_id}
"""


UPDATE_POST_REPAIR_UT_NOTIFICATION_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then return -1 end
local active_task_id = redis.call('HGET', KEYS[1], 'active_task_id') or ''
local ut_ok, current = pcall(cjson.decode, redis.call('HGET', KEYS[1], 'post_repair_ut') or '{}')
local incoming_ok, incoming = pcall(cjson.decode, ARGV[2])
if not ut_ok or not incoming_ok or (current.task_id or '') ~= ARGV[1]
  or (incoming.task_id or '') ~= ARGV[1] then return -2 end
if active_task_id ~= ARGV[1] and ARGV[3] ~= '1' then return -3 end
local revision = tonumber(redis.call('HGET', KEYS[1], 'revision') or '0') + 1
redis.call('HSET', KEYS[1], 'post_repair_ut', ARGV[2], 'revision', revision, 'updated_at', ARGV[4])
if ARGV[3] == '1' then
  if active_task_id == ARGV[1] then redis.call('HSET', KEYS[1], 'active_task_id', '', 'active_category', '') end
end
if redis.call('SET', KEYS[2], '1', 'NX', 'EX', ARGV[5]) then
  redis.call('HSET', KEYS[4], 'payload', ARGV[6], 'status', 'pending', 'created_at', ARGV[8])
  redis.call('XADD', KEYS[3], '*', 'notification_id', ARGV[7])
end
return 1
"""


FREEZE_REPAIR_MANIFEST_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then return {-1, 'task missing'} end
if ARGV[1] ~= '' then
  local owner = redis.call('HGET', KEYS[2], 'worker_id')
  local token = redis.call('HGET', KEYS[2], 'fencing_token')
  if owner ~= ARGV[1] or token ~= ARGV[2] then return {-2, 'lost lease'} end
end
local raw = redis.call('HGET', KEYS[1], 'repair_commit_manifest') or ''
if raw == '' then return {0, ''} end
local ok, manifest = pcall(cjson.decode, raw)
if not ok or type(manifest.entries) ~= 'table' then return {-3, 'stored manifest is invalid'} end
if manifest.frozen then return {0, raw} end
manifest.frozen = true
manifest.frozen_at = ARGV[3]
local encoded = cjson.encode(manifest)
redis.call('HSET', KEYS[1], 'repair_commit_manifest', encoded, 'updated_at', ARGV[4])
return {1, encoded}
"""


ADMIT_REPAIR_ROLLBACK_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 or redis.call('EXISTS', KEYS[3]) == 0 then return {-1, ''} end
local manifest_raw = redis.call('HGET', KEYS[1], 'repair_commit_manifest') or ''
local manifest_ok, manifest = pcall(cjson.decode, manifest_raw)
if not manifest_ok or not manifest.frozen or type(manifest.entries) ~= 'table' or #manifest.entries == 0 then
  return {-2, ''}
end
if redis.call('HGET', KEYS[3], 'card_id') ~= ARGV[6]
  or redis.call('HGET', KEYS[3], 'open_message_id') ~= ARGV[7]
  or redis.call('HGET', KEYS[3], 'receive_id') ~= ARGV[8]
  or manifest.authorized_actor_id ~= ARGV[8] then
  return {-3, ''}
end
if redis.call('EXISTS', KEYS[2]) == 1 then return {0, ARGV[2]} end
if tonumber(redis.call('HGET', KEYS[3], 'revision') or '-1') ~= tonumber(ARGV[9]) then return {-3, ''} end
local original_status = redis.call('HGET', KEYS[1], 'status') or ''
if ARGV[10] == 'post_repair' then
  if original_status ~= 'completed' and original_status ~= 'failed' then return {-4, ''} end
elseif ARGV[10] == 'cancel' then
  if redis.call('HGET', KEYS[1], 'cancel_requested') ~= '1'
    or original_status == 'completed' or original_status == 'failed' then return {-4, ''} end
elseif ARGV[10] == 'auto_failure' then
  local repair_state_ok, repair_state = pcall(cjson.decode,
    redis.call('HGET', KEYS[1], 'pipeline_repair_state') or '')
  if original_status ~= 'running' or not repair_state_ok
    or repair_state.auto_rollback_required ~= true
    or tonumber(repair_state.verified_selected_success_count or '-1') ~= 0 then
    return {-4, ''}
  end
elseif ARGV[10] == 'post_repair_ut_failure' then
  local ut_ok, ut = pcall(cjson.decode, redis.call('HGET', KEYS[3], 'post_repair_ut') or '{}')
  if original_status ~= 'running' and original_status ~= 'publishing' then return {-4, ''} end
  if not ut_ok or (ut.task_id or '') ~= ARGV[1] then return {-4, ''} end
elseif ARGV[10] == 'post_repair_ut_cancel' then
  local ut_ok, ut = pcall(cjson.decode, redis.call('HGET', KEYS[3], 'post_repair_ut') or '{}')
  if redis.call('HGET', KEYS[1], 'cancel_requested') ~= '1'
    or original_status == 'completed' or original_status == 'failed' then return {-4, ''} end
  if not ut_ok or (ut.task_id or '') ~= ARGV[1] then return {-4, ''} end
else
  return {-4, ''}
end
local active = redis.call('GET', KEYS[4]) or ''
if active ~= '' and active ~= ARGV[1] and active ~= ARGV[2] then return {-5, active} end
if ARGV[10] == 'cancel' then
  local worker_id = redis.call('HGET', KEYS[1], 'worker_id') or ''
  redis.call('HSET', KEYS[1], 'status', 'canceled', 'result', ARGV[12], 'error', '', 'updated_at', ARGV[11])
  if worker_id ~= '' then redis.call('SREM', ARGV[15] .. worker_id .. ':tasks', ARGV[1]) end
  redis.call('ZREM', KEYS[9], ARGV[1])
  local auto_task_id = redis.call('GET', KEYS[10]) or ''
  if auto_task_id ~= '' then
    local auto_task_key = ARGV[16] .. auto_task_id
    if redis.call('HGET', auto_task_key, 'paused_by_triage_task_id') == ARGV[1] then
      redis.call('HSET', auto_task_key, 'paused_by_triage_task_id', ARGV[2], 'updated_at', ARGV[11])
    end
  end
end
if ARGV[10] == 'auto_failure' then
  local worker_id = redis.call('HGET', KEYS[1], 'worker_id') or ''
  redis.call('HSET', KEYS[1], 'status', 'completed', 'result', ARGV[17], 'error', '', 'updated_at', ARGV[11])
  if worker_id ~= '' then redis.call('SREM', ARGV[15] .. worker_id .. ':tasks', ARGV[1]) end
  redis.call('ZREM', KEYS[5], ARGV[1])
  redis.call('ZREM', KEYS[9], ARGV[1])
  local auto_task_id = redis.call('GET', KEYS[10]) or ''
  if auto_task_id ~= '' then
    local auto_task_key = ARGV[16] .. auto_task_id
    if redis.call('HGET', auto_task_key, 'paused_by_triage_task_id') == ARGV[1] then
      redis.call('HSET', auto_task_key, 'paused_by_triage_task_id', ARGV[2], 'updated_at', ARGV[11])
    end
  end
end
if ARGV[10] == 'post_repair_ut_failure' or ARGV[10] == 'post_repair_ut_cancel' then
  local worker_id = redis.call('HGET', KEYS[1], 'worker_id') or ''
  local terminal_status = ARGV[10] == 'post_repair_ut_cancel' and 'canceled' or 'failed'
  redis.call('HSET', KEYS[1], 'status', terminal_status, 'error', '', 'updated_at', ARGV[11])
  if worker_id ~= '' then redis.call('SREM', ARGV[15] .. worker_id .. ':tasks', ARGV[1]) end
  redis.call('ZREM', KEYS[9], ARGV[1])
end
redis.call('HSET', KEYS[1], 'repair_rollback_state', ARGV[5], 'updated_at', ARGV[11])
redis.call('HSET', KEYS[2],
  'payload', ARGV[3], 'status', 'queued', 'attempt', '0',
  'created_at', ARGV[11], 'updated_at', ARGV[11],
  'admission_state', 'enqueued', 'admission_context', ARGV[4],
  'repair_rollback_state', ARGV[5])
local ingress_id = redis.call('XADD', KEYS[6], '*', 'task_id', ARGV[2])
redis.call('HSET', KEYS[2], 'ingress_message_id', ingress_id)
redis.call('SET', KEYS[4], ARGV[2])
redis.call('ZADD', KEYS[5], ARGV[11], ARGV[2])
redis.call('SADD', KEYS[8], ARGV[2])
redis.call('SET', KEYS[7], ARGV[6], 'EX', ARGV[13])
if ARGV[10] == 'post_repair_ut_failure' or ARGV[10] == 'post_repair_ut_cancel' then
  local ut_ok, ut = pcall(cjson.decode, redis.call('HGET', KEYS[3], 'post_repair_ut') or '{}')
  ut.status = ARGV[10] == 'post_repair_ut_cancel' and 'canceling' or 'failed'
  ut.status_markdown = ARGV[12]
  ut.rollback_task_id = ARGV[2]
  ut.rollback_status = 'queued'
  redis.call('HSET', KEYS[3], 'post_repair_ut', cjson.encode(ut),
    'active_task_id', ARGV[2], 'active_category', 'unit_test',
    'revision', tonumber(ARGV[9]) + 1, 'updated_at', ARGV[11])
else
  redis.call('HSET', KEYS[3],
    'state', 'rollback_queued', 'status_markdown', ARGV[12],
    'active_task_id', ARGV[2], 'active_category', '',
    'rollback_repair_task_id', ARGV[1], 'rollback_commit_count', #manifest.entries,
    'rollback_task_id', ARGV[2], 'rollback_status', 'queued', 'rollback_commit_sha', '',
    'rollback_trigger', ARGV[10],
    'revision', tonumber(ARGV[9]) + 1, 'updated_at', ARGV[11])
end
return {1, ARGV[2]}
"""


COMPLETE_REPAIR_ROLLBACK_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 or redis.call('EXISTS', KEYS[2]) == 0
  or redis.call('EXISTS', KEYS[3]) == 0 then return -1 end
if ARGV[1] ~= '' then
  local owner = redis.call('HGET', KEYS[6], 'worker_id') or ''
  local token = redis.call('HGET', KEYS[6], 'fencing_token') or ''
  if owner ~= ARGV[1] or token ~= ARGV[2] then return -2 end
end
local current = redis.call('HGET', KEYS[1], 'status') or ''
local rollback_ok, rollback_state = pcall(cjson.decode, ARGV[4])
if not rollback_ok then return -3 end
local ut_rollback = rollback_state.trigger == 'post_repair_ut_failure'
  or rollback_state.trigger == 'post_repair_ut_cancel'
local ut
if ut_rollback then
  local ut_ok
  ut_ok, ut = pcall(cjson.decode, redis.call('HGET', KEYS[3], 'post_repair_ut') or '{}')
  if not ut_ok or (ut.task_id or '') ~= (rollback_state.repair_task_id or '') then return -3 end
end
if current == 'completed' or current == 'failed' then
  local state_ok, existing_state = pcall(cjson.decode, redis.call('HGET', KEYS[1], 'repair_rollback_state') or '')
  if state_ok and (existing_state.status == 'succeeded' or existing_state.status == 'failed') then return 0 end
end
if current ~= 'running' and current ~= 'publishing' and current ~= 'assigned' and current ~= 'failed' then return -3 end
redis.call('HSET', KEYS[1],
  'status', ARGV[3], 'repair_rollback_state', ARGV[4],
  'result', ARGV[5], 'error', ARGV[6], 'updated_at', ARGV[7])
redis.call('HSET', KEYS[2], 'repair_rollback_state', ARGV[4], 'updated_at', ARGV[7])
if ut_rollback then
  local succeeded = rollback_state.status == 'succeeded'
  if succeeded then
    ut.status = rollback_state.trigger == 'post_repair_ut_cancel' and 'canceled' or 'failed'
  else
    ut.status = 'rollback_failed'
  end
  ut.status_markdown = ARGV[9]
  ut.outcome_reason = ARGV[9]
  ut.rollback_status = rollback_state.status
  ut.rollback_commit_sha = rollback_state.rollback_commit_sha or ''
  redis.call('HSET', KEYS[3], 'post_repair_ut', cjson.encode(ut),
    'active_task_id', '', 'active_category', '',
    'revision', tonumber(redis.call('HGET', KEYS[3], 'revision') or '0') + 1,
    'updated_at', ARGV[7])
else
  redis.call('HSET', KEYS[3],
    'state', ARGV[8], 'status_markdown', ARGV[9],
    'active_task_id', '', 'active_category', '',
    'rollback_status', ARGV[10], 'rollback_commit_sha', ARGV[11],
    'revision', tonumber(redis.call('HGET', KEYS[3], 'revision') or '0') + 1,
    'updated_at', ARGV[7])
end
redis.call('ZREM', KEYS[5], ARGV[12])
local worker_id = redis.call('HGET', KEYS[1], 'worker_id') or ''
if worker_id ~= '' then redis.call('SREM', ARGV[13] .. worker_id .. ':tasks', ARGV[12]) end
return 1
"""

ADMIT_FINAL_REPAIR_REPORT_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then return {-1, ''} end
local status = redis.call('HGET', KEYS[1], 'status') or ''
if status ~= 'completed' and status ~= 'failed' and status ~= 'canceled' then return {-2, ''} end
local manifest_raw = redis.call('HGET', KEYS[1], 'repair_commit_manifest') or ''
local manifest_ok, manifest = pcall(cjson.decode, manifest_raw)
if not manifest_ok or type(manifest.entries) ~= 'table' or #manifest.entries == 0 then return {-3, ''} end
local existing_state_raw = redis.call('HGET', KEYS[1], 'final_repair_report_state') or ''
if existing_state_raw ~= '' then
  local state_ok, existing_state = pcall(cjson.decode, existing_state_raw)
  if state_ok and (existing_state.status == 'model_generated' or existing_state.status == 'fallback'
    or existing_state.status == 'not_applicable') then return {0, existing_state.report_task_id or ARGV[1]} end
end
if redis.call('EXISTS', KEYS[2]) == 1 then return {0, ARGV[1]} end
redis.call('HSET', KEYS[1], 'final_repair_report_state', ARGV[3], 'updated_at', ARGV[4])
redis.call('HSET', KEYS[2],
  'payload', ARGV[2], 'status', 'queued', 'attempt', '0', 'worker_id', '', 'fencing_token', '',
  'result', '', 'error', '', 'created_at', ARGV[4], 'updated_at', ARGV[4], 'heartbeat_at', ARGV[4],
  'cancel_requested', '0', 'delivery_attempt', '0', 'admission_state', 'enqueued',
  'admission_context', ARGV[5], 'final_repair_report_state', ARGV[3])
local ingress_id = redis.call('XADD', KEYS[3], '*', 'task_id', ARGV[1])
redis.call('HSET', KEYS[2], 'ingress_message_id', ingress_id)
redis.call('ZADD', KEYS[4], ARGV[4], ARGV[1])
return {1, ARGV[1]}
"""

COMPLETE_FINAL_REPAIR_REPORT_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 or redis.call('EXISTS', KEYS[2]) == 0 then return -1 end
local incoming_ok, incoming = pcall(cjson.decode, ARGV[1])
if not incoming_ok then return -2 end
local existing_raw = redis.call('HGET', KEYS[2], 'final_repair_report_state') or ''
if existing_raw ~= '' then
  local existing_ok, existing = pcall(cjson.decode, existing_raw)
  if existing_ok and (existing.status == 'model_generated' or existing.status == 'fallback'
    or existing.status == 'not_applicable') then
    if (existing.input_digest or '') == (incoming.input_digest or '') then return 0 end
    return -3
  end
end
redis.call('HSET', KEYS[1], 'status', 'completed', 'result', ARGV[2], 'error', '',
  'final_repair_report_state', ARGV[1], 'updated_at', ARGV[3])
redis.call('HSET', KEYS[2], 'final_repair_report_state', ARGV[1], 'updated_at', ARGV[3])
if ARGV[4] ~= '' then redis.call('SET', KEYS[4], ARGV[4], 'EX', ARGV[5]) end
redis.call('ZREM', KEYS[3], ARGV[6])
local worker_id = redis.call('HGET', KEYS[1], 'worker_id') or ''
if worker_id ~= '' then redis.call('SREM', ARGV[7] .. worker_id .. ':tasks', ARGV[6]) end
return 1
"""

ENQUEUE_TASK_LUA = """
local created_at = tonumber(ARGV[4])
local dedup_ttl = tonumber(ARGV[2])
local payload_ok = pcall(cjson.decode, ARGV[3])
local context_ok = pcall(cjson.decode, ARGV[8])
if not created_at or created_at ~= created_at or created_at == math.huge or created_at == -math.huge
  or not dedup_ttl or dedup_ttl <= 0 or not payload_ok or not context_ok then
  return {-8, 'invalid admission arguments'}
end
local function key_type(key)
  local value = redis.call('TYPE', key)
  return type(value) == 'table' and value['ok'] or value
end
local expected_types = {
  {KEYS[1], 'string'}, {KEYS[2], 'hash'}, {KEYS[3], 'stream'}, {KEYS[4], 'set'},
  {KEYS[5], 'string'}, {KEYS[6], 'string'}, {KEYS[7], 'zset'}
}
for _, entry in ipairs(expected_types) do
  local actual = key_type(entry[1])
  if actual ~= 'none' and actual ~= entry[2] then
    return {-9, 'redis key type mismatch'}
  end
end
local function inspect_or_recover(task_id)
  local task_key = ARGV[7] .. task_id
  local actual = key_type(task_key)
  if actual == 'none' then return {'stale', ''} end
  if actual ~= 'hash' then return {'invalid', ''} end
  local status = redis.call('HGET', task_key, 'status') or ''
  if status == 'completed' or status == 'failed' or status == 'canceled' then
    return {'stale', status}
  end
  local admission_state = redis.call('HGET', task_key, 'admission_state') or ''
  local ingress_message_id = redis.call('HGET', task_key, 'ingress_message_id') or ''
  if status == 'queued' and (admission_state ~= 'enqueued' or ingress_message_id == '') then
    local stored_payload = redis.call('HGET', task_key, 'payload') or ''
    local stored_ok = pcall(cjson.decode, stored_payload)
    if not stored_ok then
      redis.call('HSET', task_key, 'status', 'failed', 'error', 'admission_incomplete', 'updated_at', ARGV[4])
      redis.call('ZREM', KEYS[7], task_id)
      return {'stale', 'failed'}
    end
    redis.call('SADD', KEYS[4], task_id)
    if ARGV[6] == '1' then
      redis.call('SET', KEYS[5], task_id)
      redis.call('ZADD', KEYS[7], ARGV[4], task_id)
    end
    local ingress_id = redis.call('XADD', KEYS[3], '*', 'task_id', task_id)
    redis.call('HSET', task_key,
      'admission_state', 'enqueued', 'ingress_message_id', ingress_id, 'updated_at', ARGV[4])
    return {'recovered', ingress_id}
  end
  return {'healthy', ingress_message_id}
end
local existing = redis.call('GET', KEYS[1])
if existing then
  local inspected = inspect_or_recover(existing)
  if inspected[1] == 'recovered' then return {0, existing, 1, inspected[2]} end
  if inspected[1] == 'healthy' then return {0, existing, 0, inspected[2]} end
  if inspected[1] == 'invalid' then return {-9, 'existing task key type mismatch'} end
  redis.call('DEL', KEYS[1])
end
if ARGV[6] == '1' then
  local active_triage = redis.call('GET', KEYS[5])
  if active_triage then
    local inspected = inspect_or_recover(active_triage)
    if inspected[1] == 'recovered' then
      redis.call('SET', KEYS[1], active_triage, 'EX', ARGV[2])
      return {0, active_triage, 1, inspected[2]}
    end
    if inspected[1] == 'invalid' then return {-9, 'active task key type mismatch'} end
    if inspected[1] == 'stale' and redis.call('EXISTS', KEYS[6]) == 0 then
      if redis.call('GET', KEYS[5]) == active_triage then redis.call('DEL', KEYS[5]) end
      active_triage = false
    end
  end
  if active_triage then
    redis.call('SET', KEYS[1], active_triage, 'EX', ARGV[2])
    return {0, active_triage, 0, ''}
  end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
redis.call('HSET', KEYS[2],
  'payload', ARGV[3], 'status', 'queued', 'attempt', '0',
  'created_at', ARGV[4], 'updated_at', ARGV[4],
  'admission_state', 'preparing', 'admission_context', ARGV[8])
if ARGV[5] == '1' then
  redis.call('SADD', KEYS[4], ARGV[1])
end
if ARGV[6] == '1' then
  redis.call('SET', KEYS[5], ARGV[1])
  redis.call('ZADD', KEYS[7], ARGV[4], ARGV[1])
end
local ingress_id = redis.call('XADD', KEYS[3], '*', 'task_id', ARGV[1])
redis.call('HSET', KEYS[2], 'admission_state', 'enqueued', 'ingress_message_id', ingress_id)
return {1, ARGV[1], 0, ingress_id}
"""

ENQUEUE_TASK_WITH_CARD_LUA = """
local dedup_ttl = tonumber(ARGV[2])
local created_at = tonumber(ARGV[4])
local card_ttl = tonumber(ARGV[8])
local updated_at = tonumber(ARGV[11])
local payload_ok = pcall(cjson.decode, ARGV[3])
local context_ok = pcall(cjson.decode, ARGV[19])
local selection_ok, selected_categories = pcall(cjson.decode, ARGV[20])
if not dedup_ttl or dedup_ttl <= 0 or not card_ttl or card_ttl <= 0
  or not created_at or created_at ~= created_at or created_at == math.huge or created_at == -math.huge
  or not updated_at or updated_at ~= updated_at or updated_at == math.huge or updated_at == -math.huge
  or not payload_ok or not context_ok or not selection_ok or type(selected_categories) ~= 'table' then
  return {-8, 'invalid admission arguments'}
end
if ARGV[15] ~= '' and (not tonumber(ARGV[16]) or not tonumber(ARGV[18])) then
  return {-8, 'invalid card admission arguments'}
end
local selected_category_set = {}
local allowed_batch_categories = {format = true, clang = true, build = true, unknown = true}
if ARGV[15] == 'batch' then
  if #selected_categories == 0 then return {-10, 'empty repair selection'} end
  for _, selected_category in ipairs(selected_categories) do
    if type(selected_category) ~= 'string' or not allowed_batch_categories[selected_category]
      or selected_category_set[selected_category] then
      return {-10, 'invalid repair selection'}
    end
    selected_category_set[selected_category] = true
  end
elseif #selected_categories ~= 0 then
  return {-10, 'unexpected repair selection'}
end
local function key_type(key)
  local value = redis.call('TYPE', key)
  return type(value) == 'table' and value['ok'] or value
end
local expected_types = {
  {KEYS[1], 'string'}, {KEYS[2], 'hash'}, {KEYS[3], 'stream'}, {KEYS[4], 'set'},
  {KEYS[5], 'hash'}, {KEYS[6], 'string'}, {KEYS[7], 'string'}, {KEYS[8], 'string'},
  {KEYS[9], 'zset'}
}
for _, entry in ipairs(expected_types) do
  local actual = key_type(entry[1])
  if actual ~= 'none' and actual ~= entry[2] then
    return {-9, 'redis key type mismatch'}
  end
end
if redis.call('EXISTS', KEYS[5]) == 0 then
  return {-1, ''}
end
if redis.call('HGET', KEYS[5], 'mr_url') ~= ARGV[10] then
  return {-2, ''}
end
local stored_message_id = redis.call('HGET', KEYS[5], 'open_message_id') or ''
if stored_message_id ~= '' and stored_message_id ~= ARGV[7] then
  return {-3, ''}
end
local receive_id = redis.call('HGET', KEYS[5], 'receive_id') or ''
if receive_id ~= '' and ARGV[12] ~= '' and receive_id ~= ARGV[12] then
  return {-4, ''}
end
if ARGV[15] ~= '' then
  local latest_card_id = redis.call('GET', KEYS[8])
  if latest_card_id and latest_card_id ~= ARGV[6] then
    return {-5, ''}
  end
  local current_revision = tonumber(redis.call('HGET', KEYS[5], 'revision') or '0')
  local current_pipeline_id = redis.call('HGET', KEYS[5], 'current_pipeline_id')
    or redis.call('HGET', KEYS[5], 'pipeline_id') or ''
  local current_pipeline_sha = redis.call('HGET', KEYS[5], 'current_pipeline_sha')
    or redis.call('HGET', KEYS[5], 'pipeline_sha') or ''
  local replay_task_id = redis.call('GET', KEYS[1]) or ''
  local active_task_id = redis.call('HGET', KEYS[5], 'active_task_id') or ''
  local idempotent_replay = replay_task_id ~= '' and active_task_id == replay_task_id
  if (current_revision ~= tonumber(ARGV[18])
    or current_pipeline_id ~= ARGV[16]
    or current_pipeline_sha ~= ARGV[17]) and not idempotent_replay then
    return {-5, ''}
  end
  local raw_items = redis.call('HGET', KEYS[5], 'repair_items') or '[]'
  local ok, items = pcall(cjson.decode, raw_items)
  if not ok or not items then
    return {-7, ''}
  end
  local function categories_match(stored_categories)
    if ARGV[15] ~= 'batch' then return true end
    if type(stored_categories) ~= 'table' or #stored_categories ~= #selected_categories then return false end
    for index, category in ipairs(selected_categories) do
      if stored_categories[index] ~= category then return false end
    end
    return true
  end
  local function bind_items(task_id)
    local required = {}
    if ARGV[15] == 'batch' then
      for _, category in ipairs(selected_categories) do required[category] = false end
    else
      required[ARGV[15]] = false
    end
    for _, item in ipairs(items) do
      if required[item.category] ~= nil
        and (item.status == 'pending' or item.status == 'failed' or item.task_id == task_id) then
        required[item.category] = true
      end
    end
    for _, matched in pairs(required) do
      if not matched then return false end
    end
    for _, item in ipairs(items) do
      if required[item.category] ~= nil then
        item.status = 'queued'
        item.task_id = task_id
        item.pipeline_id = tonumber(ARGV[16])
        item.pipeline_sha = ARGV[17]
        item.status_markdown = '已进入修复队列'
      end
    end
    return true
  end
  local function inspect_candidate(task_id)
    local task_key = ARGV[14] .. task_id
    local actual = key_type(task_key)
    if actual == 'none' then return {'stale', '', task_key} end
    if actual ~= 'hash' then return {'invalid', '', task_key} end
    local status = redis.call('HGET', task_key, 'status') or ''
    if status == 'completed' or status == 'failed' or status == 'canceled' then
      return {'stale', status, task_key}
    end
    local stored_payload = redis.call('HGET', task_key, 'payload') or ''
    local payload_valid, decoded = pcall(cjson.decode, stored_payload)
    if not payload_valid or not decoded or decoded.source ~= 'feishu' or decoded.pr_url ~= ARGV[10] then
      return {'conflict', status, task_key}
    end
    local stored_context = redis.call('HGET', task_key, 'admission_context') or ''
    if stored_context ~= '' then
      local context_valid, context = pcall(cjson.decode, stored_context)
      if not context_valid or not context or (context.card_id or '') ~= ARGV[6]
        or tostring(context.pipeline_id or '') ~= ARGV[16]
        or (context.pipeline_sha or '') ~= ARGV[17]
        or (context.category or '') ~= ARGV[15]
        or not categories_match(context.selected_categories or {}) then
        return {'conflict', status, task_key}
      end
    else
      local task_payload = decoded.payload or {}
      if tostring(task_payload.source_pipeline_id or '') ~= ARGV[16]
        or (task_payload.source_pipeline_sha or '') ~= ARGV[17]
        or (task_payload.repair_category or '') ~= ARGV[15]
        or not categories_match(task_payload.selected_categories or {}) then
        return {'conflict', status, task_key}
      end
    end
    local admission_state = redis.call('HGET', task_key, 'admission_state') or ''
    local ingress_message_id = redis.call('HGET', task_key, 'ingress_message_id') or ''
    if status == 'queued' and (admission_state ~= 'enqueued' or ingress_message_id == '') then
      return {'incomplete', ingress_message_id, task_key}
    end
    return {'healthy', ingress_message_id, task_key}
  end
  local existing_task_id = redis.call('GET', KEYS[1])
  local candidate = existing_task_id
  if not candidate and ARGV[13] == '1' then candidate = redis.call('GET', KEYS[6]) end
  if candidate then
    local inspected = inspect_candidate(candidate)
    if inspected[1] == 'invalid' then return {-9, 'existing task key type mismatch'} end
    if inspected[1] == 'conflict' then return {-6, candidate} end
    if inspected[1] == 'stale' then
      if redis.call('GET', KEYS[1]) == candidate then redis.call('DEL', KEYS[1]) end
      if redis.call('GET', KEYS[6]) == candidate and redis.call('EXISTS', KEYS[7]) == 0 then
        redis.call('DEL', KEYS[6])
      end
      candidate = false
    else
      local active_task_id = redis.call('HGET', KEYS[5], 'active_task_id') or ''
      if active_task_id ~= '' and active_task_id ~= candidate then return {-6, active_task_id} end
      if not bind_items(candidate) then
        return {ARGV[15] == 'batch' and -10 or -7, ''}
      end
      local binding_id = redis.call('GET', ARGV[9] .. candidate) or ''
      local binding_incomplete = active_task_id ~= candidate or binding_id ~= ARGV[6]
      if inspected[1] == 'incomplete' then
        redis.call('SADD', KEYS[4], candidate)
        if ARGV[13] == '1' then
          redis.call('SET', KEYS[6], candidate)
          redis.call('ZADD', KEYS[9], ARGV[4], candidate)
        end
        local ingress_id = redis.call('XADD', KEYS[3], '*', 'task_id', candidate)
        redis.call('HSET', inspected[3],
          'admission_state', 'enqueued', 'ingress_message_id', ingress_id,
          'admission_context', ARGV[19], 'updated_at', ARGV[11])
        inspected[2] = ingress_id
        binding_incomplete = true
      end
      if binding_incomplete then
        redis.call('HSET', KEYS[5],
          'task_id', candidate, 'active_task_id', candidate, 'active_category', ARGV[15],
          'repair_items', cjson.encode(items), 'revision', current_revision + 1,
          'open_message_id', ARGV[7], 'updated_at', ARGV[11])
      end
      if receive_id == '' and ARGV[12] ~= '' then
        redis.call('HSET', KEYS[5], 'receive_id', ARGV[12])
      end
      redis.call('EXPIRE', KEYS[5], ARGV[8])
      redis.call('SET', ARGV[9] .. candidate, ARGV[6], 'EX', ARGV[8])
      redis.call('SET', KEYS[1], candidate, 'EX', ARGV[2])
      return {0, candidate, (inspected[1] == 'incomplete' or binding_incomplete) and 1 or 0, inspected[2]}
    end
  end
  local active_task_id = redis.call('HGET', KEYS[5], 'active_task_id') or ''
  if active_task_id ~= '' then return {-6, active_task_id} end
  if not bind_items(ARGV[1]) then
    return {ARGV[15] == 'batch' and -10 or -7, ''}
  end
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
  redis.call('HSET', KEYS[2],
    'payload', ARGV[3], 'status', 'queued', 'attempt', '0',
    'created_at', ARGV[4], 'updated_at', ARGV[4],
    'admission_state', 'preparing', 'admission_context', ARGV[19])
  if ARGV[5] == '1' then
    redis.call('SADD', KEYS[4], ARGV[1])
  end
  if ARGV[13] == '1' then
    redis.call('SET', KEYS[6], ARGV[1])
    redis.call('ZADD', KEYS[9], ARGV[4], ARGV[1])
  end
  local ingress_id = redis.call('XADD', KEYS[3], '*', 'task_id', ARGV[1])
  redis.call('HSET', KEYS[5],
    'task_id', ARGV[1], 'active_task_id', ARGV[1], 'active_category', ARGV[15],
    'repair_items', cjson.encode(items), 'revision', current_revision + 1,
    'open_message_id', ARGV[7], 'updated_at', ARGV[11])
  if receive_id == '' and ARGV[12] ~= '' then
    redis.call('HSET', KEYS[5], 'receive_id', ARGV[12])
  end
  redis.call('EXPIRE', KEYS[5], ARGV[8])
  redis.call('SET', ARGV[9] .. ARGV[1], ARGV[6], 'EX', ARGV[8])
  redis.call('HSET', KEYS[2], 'admission_state', 'enqueued', 'ingress_message_id', ingress_id)
  return {1, ARGV[1], 0, ingress_id}
end
local card_task_id = redis.call('HGET', KEYS[5], 'task_id') or ''
if card_task_id ~= '' then
  redis.call('SET', KEYS[1], card_task_id, 'EX', ARGV[2])
  redis.call('HSET', KEYS[5], 'open_message_id', ARGV[7], 'updated_at', ARGV[11])
  if receive_id == '' and ARGV[12] ~= '' then
    redis.call('HSET', KEYS[5], 'receive_id', ARGV[12])
  end
  redis.call('EXPIRE', KEYS[5], ARGV[8])
  redis.call('SET', ARGV[9] .. card_task_id, ARGV[6], 'EX', ARGV[8])
  return {0, card_task_id, 0, ''}
end
if ARGV[13] == '1' then
  local active_triage = redis.call('GET', KEYS[6])
  if active_triage then
    local active_status = redis.call('HGET', ARGV[14] .. active_triage, 'status')
    if (not active_status or active_status == 'completed' or active_status == 'failed'
      or active_status == 'canceled')
      and redis.call('EXISTS', KEYS[7]) == 0 then
      redis.call('DEL', KEYS[6])
      active_triage = false
    end
  end
  if active_triage then
    redis.call('SET', KEYS[1], active_triage, 'EX', ARGV[2])
    return {0, active_triage, 0, ''}
  end
end
local task_id = redis.call('GET', KEYS[1])
local created = 0
if not task_id then
  task_id = ARGV[1]
  created = 1
  redis.call('SET', KEYS[1], task_id, 'EX', ARGV[2])
  redis.call('HSET', KEYS[2],
    'payload', ARGV[3], 'status', 'queued', 'attempt', '0',
    'created_at', ARGV[4], 'updated_at', ARGV[4],
    'admission_state', 'preparing', 'admission_context', ARGV[19])
  if ARGV[5] == '1' then
    redis.call('SADD', KEYS[4], task_id)
  end
  if ARGV[13] == '1' then
    redis.call('SET', KEYS[6], task_id)
    redis.call('ZADD', KEYS[9], ARGV[4], task_id)
  end
  local ingress_id = redis.call('XADD', KEYS[3], '*', 'task_id', task_id)
  redis.call('HSET', KEYS[2], 'admission_state', 'enqueued', 'ingress_message_id', ingress_id)
end
redis.call('HSET', KEYS[5],
  'task_id', task_id, 'open_message_id', ARGV[7], 'updated_at', ARGV[11])
if receive_id == '' and ARGV[12] ~= '' then
  redis.call('HSET', KEYS[5], 'receive_id', ARGV[12])
end
redis.call('EXPIRE', KEYS[5], ARGV[8])
redis.call('SET', ARGV[9] .. task_id, ARGV[6], 'EX', ARGV[8])
return {created, task_id, 0, ''}
"""

RECORD_AUTO_COMMAND_COMPLETED_LUA = """
local status = redis.call('HGET', KEYS[1], 'status')
if status ~= 'running' then
  return 0
end
local owner = redis.call('HGET', KEYS[2], 'worker_id')
local fence = redis.call('HGET', KEYS[2], 'fencing_token')
if owner ~= ARGV[1] or fence ~= ARGV[2] then
  return -1
end
redis.call('HSET', KEYS[1],
  'auto_next_command_index', ARGV[3],
  'auto_completed_commands', ARGV[4],
  'auto_workflow_head_sha', ARGV[5],
  'updated_at', ARGV[6])
return 1
"""

PAUSE_AUTO_FOR_TRIAGE_LUA = """
local status = redis.call('HGET', KEYS[1], 'status')
if status ~= 'running' then
  return 0
end
local owner = redis.call('HGET', KEYS[2], 'worker_id')
local fence = redis.call('HGET', KEYS[2], 'fencing_token')
if owner ~= ARGV[1] or fence ~= ARGV[2] then
  return -1
end
local active_triage = redis.call('GET', KEYS[3])
if active_triage ~= ARGV[3] then
  return 0
end
local paused_auto = redis.call('GET', KEYS[4])
if paused_auto and paused_auto ~= ARGV[4] then
  return -2
end
redis.call('HSET', KEYS[1],
  'status', 'paused_by_triage',
  'auto_next_command_index', ARGV[5],
  'auto_completed_commands', ARGV[6],
  'auto_workflow_head_sha', ARGV[7],
  'paused_by_triage_task_id', ARGV[3],
  'wait_kind', 'mr_priority',
  'wait_identity', ARGV[9],
  'updated_at', ARGV[8])
redis.call('SET', KEYS[4], ARGV[4])
return 1
"""

RESUME_AUTO_AFTER_TRIAGE_LUA = """
local active_triage = redis.call('GET', KEYS[1])
if active_triage ~= ARGV[1] then
  return 0
end
local owner = redis.call('HGET', KEYS[3], 'worker_id')
local fence = redis.call('HGET', KEYS[3], 'fencing_token')
if owner ~= ARGV[2] or fence ~= ARGV[3] then
  return -1
end
local triage_status = redis.call('HGET', KEYS[4], 'status')
if triage_status ~= 'completed' and triage_status ~= 'failed' and triage_status ~= 'canceled' then
  return 0
end
local auto_task_id = redis.call('GET', KEYS[2])
if not auto_task_id then
  redis.call('DEL', KEYS[1])
  return 0
end
local auto_task_key = ARGV[6] .. auto_task_id
local auto_status = redis.call('HGET', auto_task_key, 'status')
local paused_by = redis.call('HGET', auto_task_key, 'paused_by_triage_task_id')
if auto_status ~= 'paused_by_triage' or paused_by ~= ARGV[1] then
  return 0
end
local old_worker_id = redis.call('HGET', auto_task_key, 'worker_id') or ''
if old_worker_id ~= '' then
  redis.call('SREM', ARGV[7] .. old_worker_id .. ':tasks', auto_task_id)
end
redis.call('HSET', auto_task_key,
  'status', 'assigned', 'worker_id', ARGV[2],
  'fencing_token', ARGV[3], 'updated_at', ARGV[4])
redis.call('SADD', KEYS[5], auto_task_id)
redis.call('XADD', KEYS[6], '*',
  'task_id', auto_task_id, 'delivery_kind', ARGV[5], 'payload', '{}')
redis.call('DEL', KEYS[1], KEYS[2])
return 1
"""

SAVE_TRIAGE_CARD_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  return 0
end
local current_pipeline_id = redis.call('GET', KEYS[3])
if not current_pipeline_id then
  local current_card_id = redis.call('GET', KEYS[2])
  if current_card_id then
    local current_card_key = ARGV[4] .. current_card_id
    current_pipeline_id = redis.call('HGET', current_card_key, 'current_pipeline_id')
      or redis.call('HGET', current_card_key, 'pipeline_id')
  end
end
if tonumber(current_pipeline_id or '0') >= tonumber(ARGV[3]) then
  return -1
end
for index = 5, #ARGV, 2 do
  redis.call('HSET', KEYS[1], ARGV[index], ARGV[index + 1])
end
redis.call('EXPIRE', KEYS[1], ARGV[1])
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[1])
redis.call('SET', KEYS[3], ARGV[3], 'EX', ARGV[1])
return 1
"""

RECORD_CARD_MESSAGE_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return -1
end
local current = redis.call('HGET', KEYS[1], 'open_message_id') or ''
if current ~= '' and current ~= ARGV[1] then
  return -2
end
redis.call('HSET', KEYS[1],
  'open_message_id', ARGV[1], 'receive_id', ARGV[2], 'updated_at', ARGV[3])
return 1
"""

TRANSITION_TRIAGE_CARD_LUA = """
local current = redis.call('HGET', KEYS[1], 'state')
if not current or not string.find(',' .. ARGV[1] .. ',', ',' .. current .. ',', 1, true) then
  return 0
end
redis.call('HSET', KEYS[1],
  'state', ARGV[2], 'status_markdown', ARGV[3], 'updated_at', ARGV[4])
return 1
"""

TRANSITION_TRIAGE_CARD_NOTIFICATION_LUA = """
local current = redis.call('HGET', KEYS[1], 'state')
if not current or not string.find(',' .. ARGV[1] .. ',', ',' .. current .. ',', 1, true) then
  return 0
end
if redis.call('SET', KEYS[2], '1', 'EX', ARGV[5], 'NX') == false then
  return 0
end
local raw_items = redis.call('HGET', KEYS[1], 'repair_items') or '[]'
local ok, items = pcall(cjson.decode, raw_items)
if ok and items then
  local item_status = ''
  if ARGV[2] == 'repair_queued' then item_status = 'queued' end
  if ARGV[2] == 'repair_running' then item_status = 'running' end
  if ARGV[2] == 'waiting_pipeline' then item_status = 'waiting_pipeline' end
  if item_status ~= '' then
    for _, item in ipairs(items) do
      if item.task_id == ARGV[9] then
        item.status = item_status
        item.status_markdown = ARGV[3]
      end
    end
    raw_items = cjson.encode(items)
  end
end
redis.call('HSET', KEYS[1],
  'state', ARGV[2], 'status_markdown', ARGV[3], 'repair_items', raw_items, 'updated_at', ARGV[4])
redis.call('HSET', KEYS[4],
  'payload', ARGV[6], 'status', 'queued', 'attempt', '0',
  'created_at', ARGV[8], 'updated_at', ARGV[8])
redis.call('XADD', KEYS[3], '*', 'notification_id', ARGV[7])
return 1
"""

UPDATE_REPAIR_PROGRESS_NOTIFICATION_LUA = """
local current = redis.call('HGET', KEYS[1], 'state')
local active_task_id = redis.call('HGET', KEYS[1], 'active_task_id') or ''
if not current or active_task_id ~= ARGV[1]
  or not string.find(',' .. ARGV[2] .. ',', ',' .. current .. ',', 1, true) then
  return 0
end
if redis.call('SET', KEYS[2], '1', 'EX', ARGV[8], 'NX') == false then
  return 0
end
local raw_items = ARGV[12]
if raw_items == '' then
  raw_items = redis.call('HGET', KEYS[1], 'repair_items') or '[]'
  local ok, items = pcall(cjson.decode, raw_items)
  if ok and items then
    local item_status = ''
    if ARGV[3] == 'repair_running' then item_status = 'running' end
    if ARGV[3] == 'waiting_pipeline' then item_status = 'waiting_pipeline' end
    if item_status ~= '' then
      for _, item in ipairs(items) do
        if item.task_id == ARGV[1] then
          item.status = item_status
          item.status_markdown = ARGV[4]
        end
      end
      raw_items = cjson.encode(items)
    end
  end
end
redis.call('HSET', KEYS[1],
  'state', ARGV[3], 'status_markdown', ARGV[4], 'repair_items', raw_items,
  'current_pipeline_id', ARGV[5], 'current_pipeline_sha', ARGV[6], 'updated_at', ARGV[7])
redis.call('HSET', KEYS[4],
  'payload', ARGV[9], 'status', 'queued', 'attempt', '0',
  'created_at', ARGV[11], 'updated_at', ARGV[11])
redis.call('XADD', KEYS[3], '*', 'notification_id', ARGV[10])
return 1
"""

RECONCILE_REPAIR_CARD_NOTIFICATION_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return -1
end
local active_task_id = redis.call('HGET', KEYS[1], 'active_task_id') or ''
local revision = tonumber(redis.call('HGET', KEYS[1], 'revision') or '0')
if active_task_id ~= ARGV[1] or revision ~= tonumber(ARGV[2]) then
  return 0
end
if redis.call('SET', KEYS[2], '1', 'EX', ARGV[10], 'NX') == false then
  return 0
end
redis.call('HSET', KEYS[1],
  'repair_items', ARGV[3], 'state', ARGV[4], 'status_markdown', ARGV[5],
  'current_pipeline_id', ARGV[6], 'current_pipeline_sha', ARGV[7],
  'active_task_id', '', 'active_category', '', 'revision', ARGV[8], 'updated_at', ARGV[9])
if ARGV[14] ~= '' then redis.call('HSET', KEYS[1], 'post_repair_ut', ARGV[14]) end
redis.call('HSET', KEYS[4],
  'payload', ARGV[11], 'status', 'queued', 'attempt', '0',
  'created_at', ARGV[13], 'updated_at', ARGV[13])
redis.call('XADD', KEYS[3], '*', 'notification_id', ARGV[12])
return 1
"""

CORRECT_LATE_REPAIR_TERMINAL_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 or redis.call('EXISTS', KEYS[2]) == 0 then return -1 end
local task_status = redis.call('HGET', KEYS[1], 'status') or ''
local card_state = redis.call('HGET', KEYS[2], 'state') or ''
local revision = tonumber(redis.call('HGET', KEYS[2], 'revision') or '0')
if task_status ~= ARGV[1]
  or not string.find(',' .. ARGV[3] .. ',', ',' .. card_state .. ',', 1, true)
  or revision ~= tonumber(ARGV[4]) then
  return 0
end
redis.call('HSET', KEYS[1],
  'pipeline_repair_state', ARGV[2], 'error', '', 'updated_at', ARGV[11])
redis.call('HSET', KEYS[2],
  'repair_items', ARGV[5], 'state', ARGV[6], 'status_markdown', ARGV[7],
  'current_pipeline_id', ARGV[8], 'current_pipeline_sha', ARGV[9],
  'active_task_id', '', 'active_category', '',
  'revision', ARGV[10], 'updated_at', ARGV[11])
if redis.call('SET', KEYS[3], '1', 'EX', ARGV[12], 'NX') then
  redis.call('HSET', KEYS[5],
    'payload', ARGV[13], 'status', 'queued', 'attempt', '0',
    'created_at', ARGV[15], 'updated_at', ARGV[15])
  redis.call('XADD', KEYS[4], '*', 'notification_id', ARGV[14])
end
return 1
"""

REQUEST_REPAIR_CANCEL_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return {-1, ''}
end
if redis.call('EXISTS', KEYS[2]) == 0 then
  return {-2, ''}
end
local stored_message_id = redis.call('HGET', KEYS[2], 'open_message_id') or ''
local receive_id = redis.call('HGET', KEYS[2], 'receive_id') or ''
local revision = tonumber(redis.call('HGET', KEYS[2], 'revision') or '0')
local active_task_id = redis.call('HGET', KEYS[2], 'active_task_id') or ''
local card_state = redis.call('HGET', KEYS[2], 'state') or ''
if stored_message_id ~= ARGV[2] or receive_id ~= ARGV[3]
  or revision ~= tonumber(ARGV[4]) or active_task_id ~= ARGV[1]
  or (card_state ~= 'repair_queued' and card_state ~= 'repair_running'
    and card_state ~= 'waiting_pipeline' and card_state ~= 'canceling') then
  return {-3, ''}
end
if redis.call('GET', KEYS[3]) ~= ARGV[1] then
  return {-4, ''}
end
local status = redis.call('HGET', KEYS[1], 'status') or ''
if status == 'completed' or status == 'failed' or status == 'canceled' then
  return {0, status}
end
if redis.call('HGET', KEYS[1], 'cancel_requested') == '1' then
  return {0, status}
end
redis.call('HSET', KEYS[1],
  'cancel_requested', '1', 'cancel_requested_by', ARGV[3],
  'cancel_requested_at', ARGV[5], 'cancel_reason', 'user_requested', 'updated_at', ARGV[5])
redis.call('HSET', KEYS[2],
  'state', 'canceling', 'status_markdown', ARGV[6], 'updated_at', ARGV[5])
if redis.call('SET', KEYS[4], '1', 'EX', ARGV[7], 'NX') then
  redis.call('HSET', KEYS[6],
    'payload', ARGV[8], 'status', 'queued', 'attempt', '0',
    'created_at', ARGV[10], 'updated_at', ARGV[10])
  redis.call('XADD', KEYS[5], '*', 'notification_id', ARGV[9])
end
return {1, status}
"""

REQUEST_POST_REPAIR_UT_CANCEL_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 or redis.call('EXISTS', KEYS[2]) == 0 then return {-1, ''} end
local message_id = redis.call('HGET', KEYS[2], 'open_message_id') or ''
local receive_id = redis.call('HGET', KEYS[2], 'receive_id') or ''
local revision = tonumber(redis.call('HGET', KEYS[2], 'revision') or '-1')
local active_task_id = redis.call('HGET', KEYS[2], 'active_task_id') or ''
local ut_ok, ut = pcall(cjson.decode, redis.call('HGET', KEYS[2], 'post_repair_ut') or '{}')
if not ut_ok or message_id ~= ARGV[2] or receive_id ~= ARGV[3]
  or revision ~= tonumber(ARGV[4]) or active_task_id ~= ARGV[1]
  or (ut.task_id or '') ~= ARGV[1] then return {-2, ''} end
if redis.call('GET', KEYS[3]) ~= ARGV[1] then return {-3, ''} end
local status = redis.call('HGET', KEYS[1], 'status') or ''
if status == 'completed' or status == 'failed' or status == 'canceled' then return {0, status} end
if redis.call('HGET', KEYS[1], 'cancel_requested') == '1' then return {0, status} end
redis.call('HSET', KEYS[1], 'cancel_requested', '1', 'cancel_requested_by', ARGV[3],
  'cancel_requested_at', ARGV[5], 'cancel_reason', 'user_requested', 'updated_at', ARGV[5])
ut.status = 'canceling'
ut.status_markdown = '正在取消补测并检查本次补测提交'
redis.call('HSET', KEYS[2], 'post_repair_ut', cjson.encode(ut),
  'revision', revision + 1, 'updated_at', ARGV[5])
return {1, status}
"""

FINALIZE_REPAIR_CANCEL_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return -1
end
if redis.call('HGET', KEYS[1], 'cancel_requested') ~= '1' then
  return 0
end
local status = redis.call('HGET', KEYS[1], 'status') or ''
if status == 'completed' or status == 'failed' then
  return 0
end
local worker_id = redis.call('HGET', KEYS[1], 'worker_id') or ''
local fencing_token = redis.call('HGET', KEYS[1], 'fencing_token') or ''
if ARGV[2] ~= '' and (worker_id ~= ARGV[2] or fencing_token ~= ARGV[3]) then
  return -2
end
local active_task_id = redis.call('GET', KEYS[3]) or ''
if active_task_id ~= '' and active_task_id ~= ARGV[1] then
  return -3
end
redis.call('HSET', KEYS[1],
  'status', 'canceled', 'result', ARGV[6], 'error', '', 'updated_at', ARGV[4])
if worker_id ~= '' then
  redis.call('SREM', ARGV[12] .. worker_id .. ':tasks', ARGV[1])
end
local waiter_key = redis.call('HGET', KEYS[1], 'pipeline_waiter_key')
if waiter_key then
  redis.call('SREM', waiter_key, ARGV[1])
  if redis.call('SCARD', waiter_key) == 0 then redis.call('DEL', waiter_key) end
end
redis.call('ZREM', KEYS[4], ARGV[1])
redis.call('ZREM', KEYS[10], ARGV[1])
if active_task_id == ARGV[1] then redis.call('DEL', KEYS[3]) end

local card_active_task_id = redis.call('HGET', KEYS[2], 'active_task_id') or ''
if card_active_task_id == ARGV[1] then
  redis.call('HSET', KEYS[2],
    'repair_items', ARGV[5], 'state', 'canceled', 'status_markdown', ARGV[6],
    'active_task_id', '', 'active_category', '', 'revision', ARGV[7], 'updated_at', ARGV[4])
  if redis.call('SET', KEYS[5], '1', 'EX', ARGV[8], 'NX') then
    redis.call('HSET', KEYS[7],
      'payload', ARGV[9], 'status', 'queued', 'attempt', '0',
      'created_at', ARGV[11], 'updated_at', ARGV[11])
    redis.call('XADD', KEYS[6], '*', 'notification_id', ARGV[10])
  end
end

local auto_task_id = redis.call('GET', KEYS[8])
if auto_task_id and worker_id ~= '' then
  local lease_owner = redis.call('HGET', KEYS[9], 'worker_id') or ''
  local lease_token = redis.call('HGET', KEYS[9], 'fencing_token') or ''
  local auto_task_key = ARGV[14] .. auto_task_id
  local auto_status = redis.call('HGET', auto_task_key, 'status') or ''
  local paused_by = redis.call('HGET', auto_task_key, 'paused_by_triage_task_id') or ''
  if lease_owner == worker_id and lease_token == fencing_token
    and auto_status == 'paused_by_triage' and paused_by == ARGV[1] then
    redis.call('HSET', auto_task_key,
      'status', 'assigned', 'worker_id', worker_id,
      'fencing_token', fencing_token, 'updated_at', ARGV[4])
    redis.call('SADD', ARGV[12] .. worker_id .. ':tasks', auto_task_id)
    redis.call('XADD', ARGV[13] .. worker_id .. ':inbox', '*',
      'task_id', auto_task_id, 'delivery_kind', ARGV[15], 'payload', '{}')
    redis.call('DEL', KEYS[8])
  end
end
return 1
"""

TRANSITION_TASK_LUA = """
local current = redis.call('HGET', KEYS[1], 'status')
if not current or not string.find(',' .. ARGV[1] .. ',', ',' .. current .. ',', 1, true) then
  return 0
end
if ARGV[3] ~= '' then
  local owner = redis.call('HGET', KEYS[2], 'worker_id')
  local fence = redis.call('HGET', KEYS[2], 'fencing_token')
  if owner ~= ARGV[3] or fence ~= ARGV[4] then
    return -1
  end
end
redis.call('HSET', KEYS[1], 'status', ARGV[2], 'updated_at', ARGV[5])
if ARGV[2] == 'completed' or ARGV[2] == 'failed' or ARGV[2] == 'canceled' then
  redis.call('ZREM', KEYS[3], ARGV[7])
end
local field_count = tonumber(ARGV[6])
for index = 0, field_count - 1 do
  redis.call('HSET', KEYS[1], ARGV[8 + index * 2], ARGV[9 + index * 2])
end
return 1
"""

HEARTBEAT_TASK_LUA = """
local status = redis.call('HGET', KEYS[1], 'status') or ''
local worker_id = redis.call('HGET', KEYS[1], 'worker_id') or ''
local fencing_token = redis.call('HGET', KEYS[1], 'fencing_token') or ''
if (status ~= 'running' and status ~= 'publishing')
  or worker_id ~= ARGV[1] or fencing_token ~= ARGV[2] then
  return 0
end
redis.call('HSET', KEYS[1], 'heartbeat_at', ARGV[3])
return 1
"""

FAIL_STALE_RUNNING_TASK_LUA = """
local status = redis.call('HGET', KEYS[1], 'status') or ''
if status ~= 'running' and status ~= 'publishing' then return 0 end
local task_owner = redis.call('HGET', KEYS[1], 'worker_id') or ''
local task_fence = redis.call('HGET', KEYS[1], 'fencing_token') or ''
local lease_owner = redis.call('HGET', KEYS[2], 'worker_id') or ''
local lease_fence = redis.call('HGET', KEYS[2], 'fencing_token') or ''
if task_owner ~= ARGV[2] or task_fence ~= ARGV[3]
  or lease_owner ~= ARGV[2] or lease_fence ~= ARGV[3] then
  return -1
end
if redis.call('HGET', KEYS[1], 'cancel_requested') == '1' then return 0 end
local heartbeat = tonumber(redis.call('HGET', KEYS[1], 'heartbeat_at')
  or redis.call('HGET', KEYS[1], 'updated_at') or '0')
if heartbeat > tonumber(ARGV[4]) then return 0 end
redis.call('HSET', KEYS[1],
  'status', 'failed', 'error', ARGV[5], 'updated_at', ARGV[6])
redis.call('ZREM', KEYS[3], ARGV[1])
return 1
"""

REQUEUE_STALE_REPAIR_LUA = """
local status = redis.call('HGET', KEYS[1], 'status') or ''
local updated_at = tonumber(redis.call('HGET', KEYS[1], 'updated_at') or '0')
if status ~= ARGV[1] or updated_at > tonumber(ARGV[2]) then
  return 0
end
if status == 'assigned' then
  local worker_id = redis.call('HGET', KEYS[1], 'worker_id') or ''
  if worker_id ~= '' then redis.call('SREM', ARGV[5] .. worker_id .. ':tasks', ARGV[3]) end
  redis.call('HSET', KEYS[1], 'status', 'queued', 'updated_at', ARGV[4])
  redis.call('HDEL', KEYS[1], 'worker_id', 'fencing_token')
end
redis.call('XADD', KEYS[2], '*', 'task_id', ARGV[3])
redis.call('HSET', KEYS[1], 'updated_at', ARGV[4])
return 1
"""

REQUEUE_STALE_AUTO_WORKFLOW_LUA = """
local status = redis.call('HGET', KEYS[1], 'status') or ''
local updated_at = tonumber(redis.call('HGET', KEYS[1], 'updated_at') or '0')
local attempt = tonumber(redis.call('HGET', KEYS[1], 'attempt') or '0')
if status ~= 'queued' or updated_at > tonumber(ARGV[1]) then
  return {0, attempt}
end
local payload_ok, payload = pcall(cjson.decode, redis.call('HGET', KEYS[1], 'payload') or '')
if not payload_ok or payload.kind ~= 'auto_workflow' then
  return {0, attempt}
end
if attempt >= tonumber(ARGV[3]) then
  redis.call('HSET', KEYS[1],
    'status', 'failed', 'error', 'QueueStartupTimeout', 'updated_at', ARGV[4])
  return {2, attempt}
end
attempt = attempt + 1
local ingress_id = redis.call('XADD', KEYS[2], '*', 'task_id', ARGV[2])
redis.call('HSET', KEYS[1],
  'attempt', attempt, 'updated_at', ARGV[4],
  'admission_state', 'enqueued', 'ingress_message_id', ingress_id)
return {1, attempt}
"""

RECONCILE_ADMISSION_GATE_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
local function key_type(key)
  local value = redis.call('TYPE', key)
  return type(value) == 'table' and value['ok'] or value
end
local function release_gate()
  if redis.call('GET', KEYS[1]) == ARGV[1] then redis.call('DEL', KEYS[1]) end
  redis.call('SREM', KEYS[3], ARGV[1])
  redis.call('ZREM', KEYS[4], ARGV[1])
end
local task_type = key_type(KEYS[2])
if task_type == 'none' then
  release_gate()
  return 3
end
if task_type ~= 'hash' then return -1 end
local status = redis.call('HGET', KEYS[2], 'status') or ''
if status == 'completed' or status == 'failed' or status == 'canceled' then
  release_gate()
  return 3
end
local membership_missing = redis.call('SISMEMBER', KEYS[3], ARGV[1]) == 0
  or not redis.call('ZSCORE', KEYS[4], ARGV[1])
local admission_state = redis.call('HGET', KEYS[2], 'admission_state') or ''
local ingress_message_id = redis.call('HGET', KEYS[2], 'ingress_message_id') or ''
if status == 'queued' and (admission_state ~= 'enqueued' or ingress_message_id == '') then
  local raw_context = redis.call('HGET', KEYS[2], 'admission_context') or ''
  local raw_payload = redis.call('HGET', KEYS[2], 'payload') or ''
  local context_ok, context = pcall(cjson.decode, raw_context)
  local payload_ok, payload = pcall(cjson.decode, raw_payload)
  local valid = context_ok and payload_ok and context and payload and payload.mr
    and payload.mr.project_id == ARGV[3] and tonumber(payload.mr.iid) == tonumber(ARGV[4])
  local card_id = valid and type(context.card_id) == 'string' and context.card_id or ''
  local card_key = card_id ~= '' and ARGV[5] .. card_id or ''
  local items = nil
  local card_revision = 0
  local context_card_ttl = 0
  local context_open_message_id = ''
  local context_category = ''
  if valid and card_id ~= '' then
    context_card_ttl = tonumber(context.ttl_seconds or '0')
    context_open_message_id = type(context.open_message_id) == 'string' and context.open_message_id or ''
    context_category = type(context.category) == 'string' and context.category or ''
    valid = key_type(card_key) == 'hash'
      and redis.call('HGET', card_key, 'project_id') == ARGV[3]
      and tonumber(redis.call('HGET', card_key, 'mr_iid') or '0') == tonumber(ARGV[4])
      and context_card_ttl > 0 and context_open_message_id ~= '' and context_category ~= ''
    local current_pipeline_id = redis.call('HGET', card_key, 'current_pipeline_id')
      or redis.call('HGET', card_key, 'pipeline_id') or ''
    local current_pipeline_sha = redis.call('HGET', card_key, 'current_pipeline_sha')
      or redis.call('HGET', card_key, 'pipeline_sha') or ''
    local context_pipeline_id = tostring(context.pipeline_id or '')
    local context_pipeline_sha = type(context.pipeline_sha) == 'string' and context.pipeline_sha or ''
    card_revision = tonumber(redis.call('HGET', card_key, 'revision') or '0')
    local context_revision = tonumber(context.revision or '-1')
    local active_task_id = redis.call('HGET', card_key, 'active_task_id') or ''
    valid = valid and current_pipeline_id == context_pipeline_id
      and current_pipeline_sha == context_pipeline_sha
      and (card_revision == context_revision or (card_revision == context_revision + 1 and active_task_id == ARGV[1]))
    local items_ok
    items_ok, items = pcall(cjson.decode, redis.call('HGET', card_key, 'repair_items') or '[]')
    valid = valid and items_ok and items
    if valid then
      local matched = false
      for _, item in ipairs(items) do
        if item.category == context_category
          and (item.status == 'pending' or item.status == 'failed' or item.task_id == ARGV[1]) then
          item.status = 'queued'
          item.task_id = ARGV[1]
          item.pipeline_id = tonumber(context.pipeline_id)
          item.pipeline_sha = context_pipeline_sha
          item.status_markdown = '已进入修复队列'
          matched = true
        end
      end
      valid = matched
    end
  end
  if not valid then
    redis.call('HSET', KEYS[2],
      'status', 'failed', 'error', 'admission_incomplete', 'updated_at', ARGV[2])
    release_gate()
    return 2
  end
  redis.call('SADD', KEYS[3], ARGV[1])
  redis.call('ZADD', KEYS[4], ARGV[2], ARGV[1])
  local ingress_id = redis.call('XADD', KEYS[5], '*', 'task_id', ARGV[1])
  redis.call('HSET', KEYS[2],
    'admission_state', 'enqueued', 'ingress_message_id', ingress_id, 'updated_at', ARGV[2])
  if card_id ~= '' then
    local active_task_id = redis.call('HGET', card_key, 'active_task_id') or ''
    local next_revision = active_task_id == ARGV[1] and card_revision or card_revision + 1
    redis.call('HSET', card_key,
      'task_id', ARGV[1], 'active_task_id', ARGV[1],
      'active_category', context_category, 'repair_items', cjson.encode(items),
      'revision', next_revision, 'open_message_id', context_open_message_id,
      'updated_at', ARGV[2])
    redis.call('EXPIRE', card_key, context_card_ttl)
    redis.call('SET', ARGV[6] .. ARGV[1], card_id, 'EX', context_card_ttl)
  end
  return 1
end
if membership_missing then
  redis.call('SADD', KEYS[3], ARGV[1])
  redis.call('ZADD', KEYS[4], ARGV[2], ARGV[1])
  return 4
end
return 0
"""

CLAIM_MR_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  local owner = redis.call('HGET', KEYS[1], 'worker_id')
  local token = redis.call('HGET', KEYS[1], 'fencing_token')
  if owner == ARGV[1] then
    redis.call('PEXPIRE', KEYS[1], ARGV[2])
  end
  return {owner, token}
end
local token = redis.call('INCR', KEYS[2])
redis.call('HSET', KEYS[1], 'worker_id', ARGV[1], 'fencing_token', token)
redis.call('PEXPIRE', KEYS[1], ARGV[2])
return {ARGV[1], token}
"""

RENEW_MR_LUA = """
local owner = redis.call('HGET', KEYS[1], 'worker_id')
local token = redis.call('HGET', KEYS[1], 'fencing_token')
if owner ~= ARGV[1] or token ~= ARGV[2] then
  return 0
end
redis.call('PEXPIRE', KEYS[1], ARGV[3])
return 1
"""

RELEASE_MR_LUA = """
local owner = redis.call('HGET', KEYS[1], 'worker_id')
local token = redis.call('HGET', KEYS[1], 'fencing_token')
if owner ~= ARGV[1] or token ~= ARGV[2] then
  return 0
end
redis.call('DEL', KEYS[1])
return 1
"""

ASSERT_FENCE_LUA = """
local owner = redis.call('HGET', KEYS[1], 'worker_id')
local token = redis.call('HGET', KEYS[1], 'fencing_token')
if owner == ARGV[1] and token == ARGV[2] then
  return 1
end
return 0
"""

CLAIM_EFFECT_LUA = """
if ARGV[1] ~= '' then
  local owner = redis.call('HGET', KEYS[2], 'worker_id')
  local token = redis.call('HGET', KEYS[2], 'fencing_token')
  if owner ~= ARGV[1] or token ~= ARGV[2] then
    return {'lost_lease', '', ''}
  end
end
local status = redis.call('HGET', KEYS[1], 'status')
if status then
  if status == 'started' then
    redis.call('HSET', KEYS[3], 'active_effect', ARGV[5])
  end
  return {
    status,
    redis.call('HGET', KEYS[1], 'metadata') or '{}',
    redis.call('HGET', KEYS[1], 'result') or ''
  }
end
redis.call('HSET', KEYS[1],
  'status', 'started', 'metadata', ARGV[3],
  'created_at', ARGV[4], 'updated_at', ARGV[4])
redis.call('HSET', KEYS[3], 'active_effect', ARGV[5])
return {'started', ARGV[3], ''}
"""

UPDATE_EFFECT_LUA = """
if ARGV[1] ~= '' then
  local owner = redis.call('HGET', KEYS[2], 'worker_id')
  local token = redis.call('HGET', KEYS[2], 'fencing_token')
  if owner ~= ARGV[1] or token ~= ARGV[2] then
    return -1
  end
end
local status = redis.call('HGET', KEYS[1], 'status')
if status ~= 'started' then
  return 0
end
redis.call('HSET', KEYS[1], 'metadata', ARGV[3], 'updated_at', ARGV[4])
return 1
"""

COMPLETE_EFFECT_LUA = """
if ARGV[1] ~= '' then
  local owner = redis.call('HGET', KEYS[2], 'worker_id')
  local token = redis.call('HGET', KEYS[2], 'fencing_token')
  if owner ~= ARGV[1] or token ~= ARGV[2] then
    return -1
  end
end
local status = redis.call('HGET', KEYS[1], 'status')
if not status then
  return 0
end
redis.call('HSET', KEYS[1],
  'status', 'completed', 'result', ARGV[3], 'updated_at', ARGV[4])
if redis.call('HGET', KEYS[3], 'active_effect') == ARGV[5] then
  redis.call('HDEL', KEYS[3], 'active_effect')
end
return 1
"""

REGISTER_PIPELINE_WAIT_LUA = """
local cached = redis.call('GET', KEYS[1])
if cached then
  local ok, event = pcall(cjson.decode, cached)
  if ok and event and (event.status == 'success' or event.status == 'failed'
    or event.status == 'canceled' or event.status == 'skipped') then
    return cached
  end
end
redis.call('SADD', KEYS[2], ARGV[1])
redis.call('EXPIRE', KEYS[2], ARGV[2])
redis.call('HSET', KEYS[3],
  'pipeline_project_id', ARGV[3], 'pipeline_sha', ARGV[4],
  'pipeline_wait_registered_at', ARGV[5],
  'pipeline_event_key', ARGV[6], 'pipeline_waiter_key', ARGV[7],
  'pipeline_attempt_id', ARGV[8], 'pipeline_id', ARGV[9])
redis.call('HDEL', KEYS[3],
  'pipeline_resume_queued', 'pipeline_resume_queued_attempt_id',
  'pipeline_resume_claimed_attempt_id', 'pipeline_resume_claimed_event')
redis.call('ZADD', KEYS[4], ARGV[5], ARGV[1])
return ''
"""

PUBLISH_PIPELINE_EVENT_LUA = """
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[2])
if ARGV[5] ~= '1' then
  return {}
end
local task_ids = {}
for _, task_id in ipairs(redis.call('SMEMBERS', KEYS[3])) do
  task_ids[task_id] = true
end
for _, task_id in ipairs(redis.call('SMEMBERS', KEYS[4])) do
  task_ids[task_id] = true
end
local resumed = {}
for task_id, _ in pairs(task_ids) do
  local task_key = ARGV[3] .. task_id
  local worker_id = redis.call('HGET', task_key, 'worker_id')
  local status = redis.call('HGET', task_key, 'status')
  local attempt_id = redis.call('HGET', task_key, 'pipeline_attempt_id') or ''
  local queued_attempt = redis.call('HGET', task_key, 'pipeline_resume_queued_attempt_id') or ''
  if worker_id and status == 'waiting_pipeline' and queued_attempt ~= attempt_id then
    local inbox_key = ARGV[4] .. worker_id .. ':inbox'
    redis.call('XADD', inbox_key, '*',
      'task_id', task_id, 'delivery_kind', 'resume_pipeline', 'payload', ARGV[1])
    redis.call('HSET', task_key,
      'pipeline_resume_queued', ARGV[1], 'pipeline_resume_queued_attempt_id', attempt_id)
    table.insert(resumed, task_id)
  end
end
return resumed
"""

RESUME_CACHED_PIPELINE_LUA = """
local status = redis.call('HGET', KEYS[1], 'status')
local worker_id = redis.call('HGET', KEYS[1], 'worker_id')
local event_key = redis.call('HGET', KEYS[1], 'pipeline_event_key')
local waiter_key = redis.call('HGET', KEYS[1], 'pipeline_waiter_key')
if not event_key or not waiter_key or status ~= 'waiting_pipeline' or not worker_id then
  return 0
end
if redis.call('SISMEMBER', waiter_key, ARGV[1]) == 0 then
  return 0
end
local event = redis.call('GET', event_key)
if not event then
  return 0
end
local ok, decoded = pcall(cjson.decode, event)
if not ok or not decoded or (decoded.status ~= 'success' and decoded.status ~= 'failed'
  and decoded.status ~= 'canceled' and decoded.status ~= 'skipped') then
  return 0
end
local attempt_id = redis.call('HGET', KEYS[1], 'pipeline_attempt_id') or ''
if redis.call('HGET', KEYS[1], 'pipeline_resume_queued_attempt_id') == attempt_id then
  return 0
end
redis.call('XADD', ARGV[2] .. worker_id .. ':inbox', '*',
  'task_id', ARGV[1], 'delivery_kind', 'resume_pipeline', 'payload', event)
redis.call('HSET', KEYS[1],
  'pipeline_resume_queued', event, 'pipeline_resume_queued_attempt_id', attempt_id)
return 1
"""

CLAIM_PIPELINE_RESUME_LUA = """
local status = redis.call('HGET', KEYS[1], 'status') or ''
local attempt_id = redis.call('HGET', KEYS[1], 'pipeline_attempt_id') or ''
local project_id = redis.call('HGET', KEYS[1], 'pipeline_project_id') or ''
local pipeline_sha = redis.call('HGET', KEYS[1], 'pipeline_sha') or ''
local claimed_attempt = redis.call('HGET', KEYS[1], 'pipeline_resume_claimed_attempt_id') or ''
local event_ok, event = pcall(cjson.decode, ARGV[2])
if not event_ok or not event then return 2 end
if status ~= 'waiting_pipeline' then
  if claimed_attempt ~= '' and claimed_attempt == attempt_id
    and tostring(event.project_id or '') == project_id and tostring(event.sha or '') == pipeline_sha then
    return 0
  end
  return 2
end
if ARGV[3] ~= '' then
  local lease_owner = redis.call('HGET', KEYS[2], 'worker_id') or ''
  local lease_fence = redis.call('HGET', KEYS[2], 'fencing_token') or ''
  local task_owner = redis.call('HGET', KEYS[1], 'worker_id') or ''
  local task_fence = redis.call('HGET', KEYS[1], 'fencing_token') or ''
  if lease_owner ~= ARGV[3] or lease_fence ~= ARGV[4]
    or task_owner ~= ARGV[3] or task_fence ~= ARGV[4] then
    return -1
  end
end
if tostring(event.project_id or '') ~= project_id or tostring(event.sha or '') ~= pipeline_sha then
  return 2
end
local queued_attempt = redis.call('HGET', KEYS[1], 'pipeline_resume_queued_attempt_id') or ''
local queued_event = redis.call('HGET', KEYS[1], 'pipeline_resume_queued') or ''
if queued_attempt ~= attempt_id or queued_event ~= ARGV[2] then return 2 end
local waiter_key = redis.call('HGET', KEYS[1], 'pipeline_waiter_key')
if waiter_key then
  redis.call('SREM', waiter_key, ARGV[1])
  if redis.call('SCARD', waiter_key) == 0 then redis.call('DEL', waiter_key) end
end
redis.call('ZREM', KEYS[3], ARGV[1])
redis.call('HSET', KEYS[1],
  'status', 'running', 'heartbeat_at', ARGV[5], 'updated_at', ARGV[5],
  'pipeline_resume_claimed_attempt_id', attempt_id, 'pipeline_resume_claimed_event', ARGV[2])
redis.call('HDEL', KEYS[1],
  'pipeline_event_key', 'pipeline_waiter_key', 'pipeline_wait_registered_at',
  'pipeline_resume_queued', 'pipeline_resume_queued_attempt_id',
  'delivery_attempt', 'delivery_error', 'delivery_message_id', 'delivery_failed_at')
return 1
"""

COMPLETE_PIPELINE_RESUME_LUA = """
local status = redis.call('HGET', KEYS[1], 'status')
if not status or status == 'waiting_pipeline' then
  return 0
end
local queued_event = redis.call('HGET', KEYS[1], 'pipeline_resume_queued') or ''
if queued_event ~= '' and queued_event ~= ARGV[2] then
  return -1
end
local waiter_key = redis.call('HGET', KEYS[1], 'pipeline_waiter_key')
if waiter_key then
  redis.call('SREM', waiter_key, ARGV[1])
  if redis.call('SCARD', waiter_key) == 0 then
    redis.call('DEL', waiter_key)
  end
end
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('HDEL', KEYS[1],
  'pipeline_event_key', 'pipeline_waiter_key', 'pipeline_wait_registered_at',
  'pipeline_resume_queued', 'delivery_attempt', 'delivery_error',
  'delivery_message_id', 'delivery_failed_at')
return 1
"""

ENQUEUE_NOTIFICATION_LUA = """
if redis.call('SET', KEYS[1], '1', 'EX', ARGV[1], 'NX') == false then
  return 0
end
redis.call('HSET', KEYS[3],
  'payload', ARGV[2], 'status', 'queued', 'attempt', '0',
  'created_at', ARGV[4], 'updated_at', ARGV[4])
redis.call('XADD', KEYS[2], '*', 'notification_id', ARGV[3])
return 1
"""

ENQUEUE_CARD_FALLBACK_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return -1
end
if redis.call('HGET', KEYS[1], 'fallback_sent') == '1' then
  return 0
end
if redis.call('SET', KEYS[2], '1', 'EX', ARGV[1], 'NX') == false then
  redis.call('HSET', KEYS[1], 'fallback_sent', '1', 'updated_at', ARGV[5])
  return 0
end
redis.call('HSET', KEYS[4],
  'payload', ARGV[2], 'status', 'queued', 'attempt', '0',
  'created_at', ARGV[4], 'updated_at', ARGV[4])
redis.call('XADD', KEYS[3], '*', 'notification_id', ARGV[3])
redis.call('HSET', KEYS[1], 'fallback_sent', '1', 'updated_at', ARGV[5])
return 1
"""

ACQUIRE_LEADER_LUA = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
  return 1
end
if redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2], 'NX') then
  return 1
end
return 0
"""

ASSIGN_TASK_LUA = """
local status = redis.call('HGET', KEYS[1], 'status')
if status ~= 'queued' then
  return 0
end
if ARGV[2] ~= '' then
  local owner = redis.call('HGET', KEYS[2], 'worker_id')
  local fence = redis.call('HGET', KEYS[2], 'fencing_token')
  if owner ~= ARGV[1] or fence ~= ARGV[2] then
    return -1
  end
end
redis.call('HSET', KEYS[1],
  'status', 'assigned', 'worker_id', ARGV[1],
  'fencing_token', ARGV[2], 'updated_at', ARGV[3])
redis.call('SADD', KEYS[3], ARGV[4])
redis.call('XADD', KEYS[4], '*',
  'task_id', ARGV[4], 'delivery_kind', 'execute', 'payload', '{}')
return 1
"""

RECOVER_DEAD_TASK_LUA = """
local owner = redis.call('HGET', KEYS[1], 'worker_id')
local status = redis.call('HGET', KEYS[1], 'status')
if owner ~= ARGV[1] then
  redis.call('SREM', KEYS[2], ARGV[2])
  return 0
end
if status == 'completed' or status == 'failed' then
  redis.call('SREM', KEYS[2], ARGV[2])
  return 0
end
if status == 'assigned' or status == 'running' then
  local attempt = tonumber(redis.call('HGET', KEYS[1], 'attempt') or '0') + 1
  if attempt > tonumber(ARGV[3]) then
    redis.call('HSET', KEYS[1],
      'status', 'failed', 'attempt', attempt,
      'error', 'worker lost and retry limit exceeded', 'updated_at', ARGV[4])
    redis.call('SREM', KEYS[2], ARGV[2])
    return 2
  end
  redis.call('HSET', KEYS[1], 'status', 'queued', 'attempt', attempt, 'updated_at', ARGV[4])
  redis.call('HDEL', KEYS[1], 'worker_id', 'fencing_token')
  redis.call('XADD', KEYS[3], '*', 'task_id', ARGV[2])
  redis.call('SREM', KEYS[2], ARGV[2])
  return 1
end
if status == 'waiting_pipeline' then
  return 3
end
if status == 'publishing' then
  return 4
end
if status == 'paused_by_triage' then
  redis.call('SREM', KEYS[2], ARGV[2])
  return 5
end
return 0
"""

REVOKE_MR_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 1
end
local owner = redis.call('HGET', KEYS[1], 'worker_id')
local token = redis.call('HGET', KEYS[1], 'fencing_token')
if owner ~= ARGV[1] or token ~= ARGV[2] then
  return 0
end
redis.call('DEL', KEYS[1])
return 1
"""

TRANSFER_ORPHANED_TASK_LUA = """
local owner = redis.call('HGET', KEYS[1], 'worker_id')
local status = redis.call('HGET', KEYS[1], 'status')
if owner ~= ARGV[1] or (status ~= 'waiting_pipeline' and status ~= 'publishing') then
  return 0
end
local lease_owner = redis.call('HGET', KEYS[2], 'worker_id')
local lease_token = redis.call('HGET', KEYS[2], 'fencing_token')
if lease_owner ~= ARGV[2] or lease_token ~= ARGV[3] then
  return -1
end
redis.call('HSET', KEYS[1],
  'worker_id', ARGV[2], 'fencing_token', ARGV[3], 'updated_at', ARGV[4])
redis.call('SREM', KEYS[3], ARGV[5])
redis.call('SADD', KEYS[4], ARGV[5])
if status == 'waiting_pipeline' and ARGV[6] ~= '' then
  redis.call('XADD', KEYS[5], '*',
    'task_id', ARGV[5], 'delivery_kind', 'resume_pipeline', 'payload', ARGV[6])
end
return 1
"""


class LostLeaseError(RuntimeError):
    pass


class StaleCardActionError(ValueError):
    pass


class RepairAlreadyRunningError(ValueError):
    pass


class UnauthorizedRepairRollback(ValueError):
    pass


class RepairRollbackUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class EnqueueResult:
    created: bool
    task_id: str
    recovered: bool = False


@dataclass(frozen=True)
class CancelRequestResult:
    task_id: str
    accepted: bool
    terminal_status: str = ""


@dataclass(frozen=True)
class RollbackRequestResult:
    repair_task_id: str
    rollback_task_id: str
    created: bool
    status: str


@dataclass(frozen=True)
class MrLease:
    mr: MrKey
    worker_id: str
    fencing_token: int


@dataclass(frozen=True)
class EffectRecord:
    status: str
    metadata: dict[str, Any]
    result: Any = None


@dataclass(frozen=True)
class AutoWorkflowCursor:
    next_command_index: int = 0
    completed_commands: tuple[str, ...] = ()
    workflow_head_sha: str = ""
    paused_by_triage_task_id: str = ""


@dataclass(frozen=True)
class StoredTask:
    envelope: TaskEnvelope
    status: TaskStatus
    attempt: int
    worker_id: str
    fencing_token: int | None
    result: str
    error: str
    pipeline_project_id: str = ""
    pipeline_sha: str = ""
    pipeline_attempt_id: str = ""
    pipeline_id: int | None = None
    auto_next_command_index: int = 0
    auto_completed_commands: tuple[str, ...] = ()
    auto_workflow_head_sha: str = ""
    paused_by_triage_task_id: str = ""
    wait_kind: str = ""
    wait_identity: str = ""
    pipeline_repair_state: PipelineRepairState = PipelineRepairState()
    created_at: float = 0.0
    updated_at: float = 0.0
    heartbeat_at: float = 0.0
    cancel_requested: bool = False
    delivery_attempt: int = 0
    admission_state: str = ""
    ingress_message_id: str = ""
    admission_context: dict[str, Any] = field(default_factory=dict)
    repair_commit_manifest: RepairCommitManifest | None = None
    repair_rollback_state: RepairRollbackState | None = None
    final_repair_report_state: FinalRepairReportState | None = None

    @property
    def task_id(self) -> str:
        return self.envelope.task_id

    @property
    def mr(self) -> MrKey | None:
        return self.envelope.mr

    @property
    def admission_complete(self) -> bool:
        return self.admission_state == "enqueued" and bool(self.ingress_message_id)


@dataclass(frozen=True)
class WorkerState:
    worker_id: str
    last_seen: float
    active_tasks: int
    owned_mrs: int
    degraded: bool
    active_report_tasks: int = 0


@dataclass(frozen=True)
class RedisKeys:
    prefix: str = "pr-agent"

    @property
    def ingress_stream(self) -> str:
        return f"{self.prefix}:ingress"

    @property
    def report_ingress_stream(self) -> str:
        return f"{self.prefix}:repair-reports:ingress"

    @property
    def notification_stream(self) -> str:
        return f"{self.prefix}:notifications"

    @property
    def worker_registry(self) -> str:
        return f"{self.prefix}:workers"

    def task(self, task_id: str) -> str:
        return f"{self.prefix}:task:{task_id}"

    def dedup(self, idempotency_key: str) -> str:
        return f"{self.prefix}:dedup:{quote(idempotency_key, safe='')}"

    def mr_lease(self, mr: MrKey) -> str:
        return f"{self.prefix}:mr:{mr.redis_id}:lease"

    def mr_fence(self, mr: MrKey) -> str:
        return f"{self.prefix}:mr:{mr.redis_id}:fence"

    def worker(self, worker_id: str) -> str:
        return f"{self.prefix}:worker:{worker_id}"

    def worker_inbox(self, worker_id: str) -> str:
        return f"{self.prefix}:worker:{worker_id}:inbox"

    def pipeline_event(self, project_id: str, sha: str, pipeline_id: int | None = None) -> str:
        suffix = "event" if pipeline_id is None else f"event:{int(pipeline_id)}"
        return f"{self.prefix}:pipeline:{quote(project_id, safe='')}:{quote(sha, safe='')}:{suffix}"

    def pipeline_waiters(self, project_id: str, sha: str, pipeline_id: int | None = None) -> str:
        suffix = "waiters" if pipeline_id is None else f"waiters:{int(pipeline_id)}"
        return f"{self.prefix}:pipeline:{quote(project_id, safe='')}:{quote(sha, safe='')}:{suffix}"

    def effect(self, effect_key: str) -> str:
        return f"{self.prefix}:effect:{quote(effect_key, safe='')}"

    @property
    def pipeline_waiting(self) -> str:
        return f"{self.prefix}:pipeline:waiting"

    @property
    def active_repairs(self) -> str:
        return f"{self.prefix}:repairs:active"

    @property
    def active_report_tasks(self) -> str:
        return f"{self.prefix}:repair-reports:active"

    def final_repair_report_input(self, repair_task_id: str) -> str:
        return f"{self.prefix}:task:{quote(repair_task_id, safe='')}:final-repair-report-input"

    def notification_dedup(self, notification_id: str) -> str:
        return f"{self.prefix}:notification:{notification_id}:queued"

    def notification(self, notification_id: str) -> str:
        return f"{self.prefix}:notification:{notification_id}"

    def triage_card(self, card_id: str) -> str:
        return f"{self.prefix}:feishu:card:{card_id}"

    def task_triage_card(self, task_id: str) -> str:
        return f"{self.prefix}:feishu:task-card:{task_id}"

    def feishu_user_name(self, open_id: str) -> str:
        return f"{self.prefix}:feishu:user-name:{quote(open_id, safe='')}"

    def mr_tasks(self, mr: MrKey) -> str:
        return f"{self.prefix}:mr:{mr.redis_id}:tasks"

    def mr_triage_active(self, mr: MrKey) -> str:
        return f"{self.prefix}:mr:{mr.redis_id}:triage-active"

    def mr_paused_auto(self, mr: MrKey) -> str:
        return f"{self.prefix}:mr:{mr.redis_id}:paused-auto"

    def mr_latest_repair_card(self, mr: MrKey) -> str:
        return f"{self.prefix}:mr:{mr.redis_id}:latest-repair-card"

    def mr_latest_repair_pipeline_id(self, mr: MrKey) -> str:
        return f"{self.prefix}:mr:{mr.redis_id}:latest-repair-pipeline-id"

    def fixing_notice(self, mr: MrKey) -> str:
        return f"{self.prefix}:mr:{mr.redis_id}:fixing-notice"

    @property
    def scheduler_leader(self) -> str:
        return f"{self.prefix}:scheduler:leader"

    def worker_tasks(self, worker_id: str) -> str:
        return f"{self.prefix}:worker:{worker_id}:tasks"

    def service_heartbeat(self, service: str) -> str:
        return f"{self.prefix}:service:{service}:heartbeat"

    @property
    def repair_reconciliation_metrics(self) -> str:
        return f"{self.prefix}:repairs:reconciliation"

    def repair_sha_tasks(self, project_id: str, sha: str) -> str:
        return f"{self.prefix}:repair-sha:{quote(project_id, safe='')}:{quote(sha, safe='')}:tasks"

    @property
    def triage_persistence_health(self) -> str:
        return f"{self.prefix}:triage:persistence-health"

    def lifecycle(self, task_id: str) -> str:
        return f"{self.prefix}:task:{quote(task_id, safe='')}:lifecycle"

    def lifecycle_events(self, task_id: str) -> str:
        return f"{self.prefix}:task:{quote(task_id, safe='')}:lifecycle-events"

    def repair_progress(self, task_id: str) -> str:
        return f"{self.prefix}:task:{quote(task_id, safe='')}:repair-progress"

    def parse_triage_gate(self, key: str) -> MrKey | None:
        prefix = f"{self.prefix}:mr:"
        suffix = ":triage-active"
        if not key.startswith(prefix) or not key.endswith(suffix):
            return None
        identity = key[len(prefix) : -len(suffix)]
        try:
            encoded_project, raw_iid = identity.rsplit(":", 1)
            return MrKey(unquote(encoded_project), int(raw_iid))
        except (TypeError, ValueError):
            return None


def _triage_card_mapping(binding: TriageCardBinding) -> dict[str, str | int]:
    value = binding.to_dict()
    value["fallback_sent"] = int(binding.fallback_sent)
    value["repair_items"] = json.dumps(value.get("repair_items") or [], ensure_ascii=False, separators=(",", ":"))
    value["failed_job_names"] = json.dumps(
        value.get("failed_job_names") or [], ensure_ascii=False, separators=(",", ":")
    )
    value["post_repair_ut"] = json.dumps(
        value.get("post_repair_ut") or {}, ensure_ascii=False, separators=(",", ":")
    )
    return value


def _triage_card_from_hash(value: dict[str, Any]) -> TriageCardBinding | None:
    if not value:
        return None
    normalized = dict(value)
    normalized["schema_version"] = int(normalized["schema_version"])
    normalized["mr_iid"] = int(normalized["mr_iid"])
    normalized["pipeline_id"] = int(normalized["pipeline_id"])
    normalized["fallback_sent"] = str(normalized.get("fallback_sent", "0")) == "1"
    raw_items = normalized.get("repair_items") or "[]"
    normalized["repair_items"] = json.loads(raw_items) if isinstance(raw_items, str) else raw_items
    raw_failed_job_names = normalized.get("failed_job_names") or "[]"
    normalized["failed_job_names"] = (
        json.loads(raw_failed_job_names) if isinstance(raw_failed_job_names, str) else raw_failed_job_names
    )
    raw_post_repair_ut = normalized.get("post_repair_ut") or "{}"
    normalized["post_repair_ut"] = (
        json.loads(raw_post_repair_ut) if isinstance(raw_post_repair_ut, str) else raw_post_repair_ut
    )
    normalized["revision"] = int(normalized.get("revision") or 0)
    normalized["current_pipeline_id"] = int(
        normalized.get("current_pipeline_id") or normalized.get("pipeline_id") or 0
    )
    return TriageCardBinding.from_dict(normalized)


def _is_triage_task(task: TaskEnvelope) -> bool:
    command = task.command.strip().split(maxsplit=1)
    return bool(
        task.mr
        and command
        and command[0].lower() in {"/triage", "/fix-format", "/fix_format", "/repair-pipeline"}
    )


def _completed_commands(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(str(command) for command in decoded)


def _admission_timestamp(task: TaskEnvelope) -> float:
    value = _stored_timestamp(task.created_at)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("task created_at must be a finite timestamp")
    return value


def _strict_json(value: Any, field_name: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be valid JSON") from error


def _json_object(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(str(value))
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _redis_text(value: Any) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")


def _validate_manifest_append_input(
    task_id: str,
    entry: RepairCommitEntry,
    base_tree_sha: str,
    source_branch: str,
    authorized_actor_id: str,
) -> None:
    values = (entry.commit_sha, entry.parent_sha, entry.tree_sha, base_tree_sha)
    if not task_id or entry.sequence <= 0 or not all(_full_sha(value) for value in values):
        raise RepairManifestConflict("repair commit identity is invalid")
    if not all((entry.effect_id, entry.task_marker, entry.pushed_at, source_branch, authorized_actor_id)):
        raise RepairManifestConflict("repair commit evidence is incomplete")
    if source_branch != source_branch.strip() or source_branch.startswith("refs/") or ".." in source_branch:
        raise RepairManifestConflict("source branch is not normalized")


def _full_sha(value: str) -> bool:
    return len(value) == 40 and all(character in "0123456789abcdef" for character in value)


def _repair_manifest_result(task_id: str, result: Any) -> RepairCommitManifest:
    code = int(result[0])
    if code == -2:
        raise LostLeaseError(task_id)
    if code < 0:
        raise RepairManifestConflict(_redis_text(result[1]))
    raw = _redis_text(result[1])
    if not raw:
        raise RepairManifestConflict("Redis returned an empty repair commit manifest")
    return RepairCommitManifest.from_json(raw)


def _repair_progress_events(records) -> list:
    from pr_agent.triage.repair_details import RepairProgressEvent

    events = []
    for raw_event_id, raw_fields in records or ():
        fields = {
            _redis_text(key): _redis_text(value)
            for key, value in (raw_fields or {}).items()
        }
        payload = fields.get("payload", "")
        if not payload:
            continue
        try:
            events.append(RepairProgressEvent.from_json(payload).with_event_id(_redis_text(raw_event_id)))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return events


def _stored_timestamp(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class RedisBroker:
    def __init__(
        self, redis_client: async_redis.Redis, settings: DistributedSettings, keys: RedisKeys | None = None
    ) -> None:
        self.redis = redis_client
        self.settings = settings
        self.keys = keys or RedisKeys()
        self._local_inbox_messages: dict[str, set[str]] = {}

    async def _eval(self, script: str, keys: list[str], args: list[Any]) -> Any:
        return await self.redis.eval(script, len(keys), *keys, *args)

    async def record_lifecycle_event(self, event) -> bool:
        key = self.keys.lifecycle(event.task_id)
        events_key = self.keys.lifecycle_events(event.task_id)
        async with self.redis.pipeline(transaction=True) as pipeline:
            pipeline.zadd(key, {event.event_id: event.occurred_at}, nx=True)
            pipeline.hsetnx(events_key, event.event_id, event.to_json())
            pipeline.expire(key, self.settings.pipeline_event_ttl_seconds)
            pipeline.expire(events_key, self.settings.pipeline_event_ttl_seconds)
            result = await pipeline.execute()
        return bool(int(result[0]))

    async def append_repair_progress(self, event) -> str:
        from pr_agent.triage.repair_details import repair_details_event_limit, repair_details_retention_seconds

        key = self.keys.repair_progress(event.task_id)
        event_id = await self.redis.xadd(
            key,
            {"payload": event.to_json()},
            maxlen=repair_details_event_limit(),
            approximate=True,
        )
        await self.redis.expire(key, repair_details_retention_seconds())
        return _redis_text(event_id)

    async def get_repair_progress(self, task_id: str, *, after_id: str = "", count: int = 200):
        key = self.keys.repair_progress(task_id)
        minimum = f"({after_id}" if after_id else "-"
        records = await self.redis.xrange(key, min=minimum, max="+", count=max(1, count))
        return _repair_progress_events(records)

    async def read_repair_progress(
        self,
        task_id: str,
        *,
        after_id: str = "$",
        block_ms: int = 15_000,
        count: int = 50,
    ):
        key = self.keys.repair_progress(task_id)
        streams = await self.redis.xread(
            {key: after_id or "$"},
            count=max(1, count),
            block=max(1, block_ms),
        )
        records = [record for _stream, values in streams or () for record in values]
        return _repair_progress_events(records)

    async def get_feishu_user_name(self, open_id: str) -> str:
        if not open_id:
            return ""
        return str(await self.redis.get(self.keys.feishu_user_name(open_id)) or "")

    async def cache_feishu_user_name(self, open_id: str, name: str, ttl_seconds: int) -> None:
        if not open_id or not name or ttl_seconds <= 0:
            return
        await self.redis.set(self.keys.feishu_user_name(open_id), name, ex=ttl_seconds)

    async def get_lifecycle_events(self, task_id: str):
        from pr_agent.distributed.lifecycle import LifecycleEvent

        event_ids = [str(value) for value in await self.redis.zrange(self.keys.lifecycle(task_id), 0, -1)]
        if not event_ids:
            return []
        values = await self.redis.hmget(self.keys.lifecycle_events(task_id), event_ids)
        return [LifecycleEvent.from_json(value) for value in values if value]

    async def enqueue_task(self, task: TaskEnvelope) -> EnqueueResult:
        mr_tasks_key = self.keys.mr_tasks(task.mr) if task.mr else f"{self.keys.prefix}:no-mr:tasks"
        triage_priority_key = (
            self.keys.mr_triage_active(task.mr) if task.mr else f"{self.keys.prefix}:no-mr:triage-active"
        )
        is_priority_triage = self.settings.triage_priority_over_auto and _is_triage_task(task)
        occurred_at = _admission_timestamp(task)
        task_json = _strict_json(task.to_dict(), "task payload")
        admission_context = _strict_json(
            {
                "task_id": task.task_id,
                "mr": task.mr.to_dict() if task.mr else None,
                "card_id": "",
            },
            "admission context",
        )
        result = await self._eval(
            ENQUEUE_TASK_LUA,
            [
                self.keys.dedup(task.idempotency_key),
                self.keys.task(task.task_id),
                self.keys.ingress_stream,
                mr_tasks_key,
                triage_priority_key,
                self.keys.mr_paused_auto(task.mr) if task.mr else f"{self.keys.prefix}:no-mr:paused-auto",
                self.keys.active_repairs,
            ],
            [
                task.task_id,
                self.settings.dedup_ttl_seconds,
                task_json,
                occurred_at,
                int(task.mr is not None),
                int(is_priority_triage),
                f"{self.keys.prefix}:task:",
                admission_context,
            ],
        )
        created = int(result[0])
        if created < 0:
            raise ValueError(f"task admission validation failed: {result[1] if len(result) > 1 else created}")
        enqueue_result = EnqueueResult(
            created=bool(created),
            task_id=str(result[1]),
            recovered=bool(int(result[2])) if len(result) > 2 else False,
        )
        from pr_agent.distributed.lifecycle import LifecycleEvent

        await self.record_lifecycle_event(
            LifecycleEvent.new(
                enqueue_result.task_id,
                "created",
                "point",
                occurred_at=occurred_at,
            )
        )
        await self.record_lifecycle_event(
            LifecycleEvent.new(
                enqueue_result.task_id,
                "queue",
                "start",
                segment_id="initial",
                occurred_at=occurred_at,
            )
        )
        return enqueue_result

    async def save_triage_card(self, binding: TriageCardBinding, ttl_seconds: int) -> bool:
        if ttl_seconds <= 0:
            raise ValueError("triage card TTL must be positive")
        mapping = _triage_card_mapping(binding)
        flattened = [item for pair in mapping.items() for item in pair]
        mr = MrKey(binding.project_id, binding.mr_iid)
        result = await self._eval(
            SAVE_TRIAGE_CARD_LUA,
            [
                self.keys.triage_card(binding.card_id),
                self.keys.mr_latest_repair_card(mr),
                self.keys.mr_latest_repair_pipeline_id(mr),
            ],
            [
                ttl_seconds,
                binding.card_id,
                binding.current_pipeline_id,
                self.keys.triage_card(""),
                *flattened,
            ],
        )
        return int(result) == 1

    async def get_triage_card(self, card_id: str) -> TriageCardBinding | None:
        return _triage_card_from_hash(await self.redis.hgetall(self.keys.triage_card(card_id)))

    async def resolve_unified_repair_card(
        self,
        card_id: str,
        mr: MrKey,
        pipeline_id: int,
    ) -> TriageCardBinding:
        binding = await self.get_triage_card(card_id)
        latest_card_id = await self.redis.get(self.keys.mr_latest_repair_card(mr))
        if (
            binding is None
            or binding.project_id != mr.project_id
            or binding.mr_iid != mr.iid
            or str(latest_card_id or "") != card_id
            or binding.current_pipeline_id != pipeline_id
            or not binding.current_pipeline_sha
        ):
            raise StaleCardActionError("repair card is not the latest Pipeline binding")
        repair_items = [
            item
            for item in binding.repair_items
            if item.category is RepairCategory.PIPELINE
            and item.command == "/repair-pipeline"
            and item.status in {RepairItemStatus.PENDING, RepairItemStatus.FAILED}
        ]
        if len(repair_items) != 1:
            raise StaleCardActionError("repair card is not a pending unified repair")
        return binding

    async def resolve_repair_card_selection(
        self,
        card_id: str,
        mr: MrKey,
        pipeline_id: int,
        selected_categories: tuple[str, ...],
    ) -> TriageCardBinding:
        binding = await self.get_triage_card(card_id)
        latest_card_id = await self.redis.get(self.keys.mr_latest_repair_card(mr))
        if (
            binding is None
            or binding.project_id != mr.project_id
            or binding.mr_iid != mr.iid
            or str(latest_card_id or "") != card_id
            or binding.current_pipeline_id != pipeline_id
            or not binding.current_pipeline_sha
            or binding.active_task_id
            or binding.repair_card_mode != "multi_select"
        ):
            raise StaleCardActionError("repair card is not the latest selectable Pipeline binding")
        allowed = {
            RepairCategory.FORMAT.value,
            RepairCategory.CLANG.value,
            RepairCategory.BUILD.value,
            RepairCategory.UNKNOWN.value,
        }
        if not selected_categories or len(set(selected_categories)) != len(selected_categories):
            raise StaleCardActionError("repair selection is empty or duplicated")
        if any(category not in allowed for category in selected_categories):
            raise StaleCardActionError("repair selection contains an unsupported category")
        actionable = {
            item.category.value
            for item in binding.repair_items
            if item.status in {RepairItemStatus.PENDING, RepairItemStatus.FAILED}
        }
        if any(category not in actionable for category in selected_categories):
            raise StaleCardActionError("repair selection is not actionable")
        return binding

    async def get_task_triage_card(self, task_id: str) -> TriageCardBinding | None:
        card_id = await self.redis.get(self.keys.task_triage_card(task_id))
        return await self.get_triage_card(str(card_id)) if card_id else None

    async def request_repair_cancel(
        self,
        task_id: str,
        card_id: str,
        open_message_id: str,
        sender_id: str,
        revision: int,
    ) -> CancelRequestResult:
        stored = await self.get_task(task_id)
        binding = await self.get_triage_card(card_id)
        if stored is None:
            raise ValueError(f"unknown task: {task_id}")
        if binding is None or stored.mr is None:
            raise StaleCardActionError("repair card is no longer available")
        from pr_agent.distributed.notifications import build_card_update_notification

        status_markdown = "正在取消并检查已提交修改。"
        predicted = replace(binding, state=TriageCardState.CANCELING, status_markdown=status_markdown)
        notification = build_card_update_notification(
            predicted,
            task_id,
            TriageCardState.CANCELING,
            status_markdown,
        )
        result = await self._eval(
            REQUEST_REPAIR_CANCEL_LUA,
            [
                self.keys.task(task_id),
                self.keys.triage_card(card_id),
                self.keys.mr_triage_active(stored.mr),
                self.keys.notification_dedup(notification.notification_id),
                self.keys.notification_stream,
                self.keys.notification(notification.notification_id),
            ],
            [
                task_id,
                open_message_id,
                sender_id,
                revision,
                time.time(),
                status_markdown,
                self.settings.dedup_ttl_seconds,
                notification.to_json(),
                notification.notification_id,
                notification.created_at,
            ],
        )
        code = int(result[0])
        status = str(result[1])
        if code == -1:
            raise ValueError(f"unknown task: {task_id}")
        if code in {-2, -3, -4}:
            raise StaleCardActionError("cancel action no longer owns the active repair")
        return CancelRequestResult(task_id, code == 1, status)

    async def request_post_repair_ut_cancel(
        self,
        task_id: str,
        card_id: str,
        open_message_id: str,
        sender_id: str,
        revision: int,
    ) -> CancelRequestResult:
        stored = await self.get_task(task_id)
        binding = await self.get_triage_card(card_id)
        if stored is None:
            raise ValueError(f"unknown task: {task_id}")
        if binding is None or stored.mr is None:
            raise StaleCardActionError("unit-test card is no longer available")
        result = await self._eval(
            REQUEST_POST_REPAIR_UT_CANCEL_LUA,
            [
                self.keys.task(task_id),
                self.keys.triage_card(card_id),
                self.keys.mr_triage_active(stored.mr),
            ],
            [task_id, open_message_id, sender_id, revision, time.time()],
        )
        code = int(result[0])
        status = str(result[1])
        if code == -1:
            raise ValueError(f"unknown task: {task_id}")
        if code in {-2, -3}:
            raise StaleCardActionError("cancel action no longer owns the unit-test task")
        if code == 1:
            updated = await self.get_triage_card(card_id)
            if updated is not None:
                from pr_agent.distributed.notifications import build_card_update_notification

                await self.enqueue_notification(
                    build_card_update_notification(
                        updated,
                        task_id,
                        TriageCardState.REPAIR_SUCCEEDED,
                        updated.status_markdown,
                    )
                )
        return CancelRequestResult(task_id, code == 1, status)

    async def request_repair_rollback(
        self,
        repair_task_id: str,
        card_id: str,
        open_message_id: str,
        sender_id: str,
        revision: int,
        *,
        trigger: str = "post_repair",
    ) -> RollbackRequestResult:
        if not repair_rollback_enabled():
            raise RepairRollbackUnavailable("repair rollback is disabled")
        stored = await self.get_task(repair_task_id)
        binding = await self.get_triage_card(card_id)
        if stored is None or binding is None or stored.mr is None:
            raise StaleCardActionError("repair task or card is unavailable")
        manifest = stored.repair_commit_manifest
        if manifest is None:
            raise RepairRollbackUnavailable("repair commit manifest is missing")
        validation = manifest.validate_static()
        if not validation.ok:
            raise RepairRollbackUnavailable(validation.message)
        if sender_id != binding.receive_id or sender_id != manifest.authorized_actor_id:
            raise UnauthorizedRepairRollback("only the original repair card receiver can rollback")
        rollback_task_id = hashlib.sha256(f"repair-rollback:{repair_task_id}".encode("utf-8")).hexdigest()[:32]
        now_iso = datetime.now(timezone.utc).isoformat()
        state = RepairRollbackState(
            rollback_task_id=rollback_task_id,
            repair_task_id=repair_task_id,
            status=RepairRollbackStatus.QUEUED,
            trigger=trigger,
            requested_by=sender_id,
            expected_remote_head=manifest.final_repair_sha,
            manifest_digest=manifest.digest(),
            created_at=now_iso,
            updated_at=now_iso,
        )
        task = replace(
            TaskEnvelope.new(
                kind=TaskKind.REPAIR_ROLLBACK,
                source="feishu",
                mr=stored.mr,
                pr_url=stored.envelope.pr_url,
                command="/rollback-repair",
                payload={
                    "repair_task_id": repair_task_id,
                    "manifest_digest": manifest.digest(),
                    "trigger": trigger,
                    "requested_by": sender_id,
                },
                idempotency_key=f"repair-rollback:{repair_task_id}",
            ),
            task_id=rollback_task_id,
        )
        status_markdown = (
            "修复已停止，正在撤回已提交修改。"
            if trigger == "cancel"
            else "补测已停止，正在撤回本次补测提交。"
            if trigger == "post_repair_ut_cancel"
            else "补测未完成，正在撤回本次补测提交。"
            if trigger == "post_repair_ut_failure"
            else "修复未成功，正在撤回本次自动修改"
            if trigger == "auto_failure"
            else f"正在安全撤回本次自动修复产生的 {len(manifest.entries)} 个提交。"
        )
        context = {
            "repair_task_id": repair_task_id,
            "card_id": card_id,
            "open_message_id": open_message_id,
            "manifest_digest": manifest.digest(),
            "trigger": trigger,
        }
        result = await self._eval(
            ADMIT_REPAIR_ROLLBACK_LUA,
            [
                self.keys.task(repair_task_id),
                self.keys.task(rollback_task_id),
                self.keys.triage_card(card_id),
                self.keys.mr_triage_active(stored.mr),
                self.keys.active_repairs,
                self.keys.ingress_stream,
                self.keys.task_triage_card(rollback_task_id),
                self.keys.mr_tasks(stored.mr),
                self.keys.pipeline_waiting,
                self.keys.mr_paused_auto(stored.mr),
            ],
            [
                repair_task_id,
                rollback_task_id,
                task.to_json(),
                _strict_json(context, "rollback admission context"),
                state.to_json(),
                card_id,
                open_message_id,
                sender_id,
                revision,
                trigger,
                time.time(),
                status_markdown,
                self.settings.pipeline_event_ttl_seconds,
                manifest.digest(),
                f"{self.keys.prefix}:worker:",
                f"{self.keys.prefix}:task:",
                _strict_json(
                    {
                        "ok": False if trigger.startswith("post_repair_ut_") else True,
                        "auto_rollback": "queued",
                        "trigger": trigger,
                    },
                    "automatic rollback result",
                ),
            ],
        )
        code = int(result[0])
        if code == -2:
            raise RepairRollbackUnavailable("repair manifest is incomplete")
        if code == -3:
            raise UnauthorizedRepairRollback("repair rollback actor or card is invalid")
        if code in {-1, -4}:
            raise StaleCardActionError("repair task state changed")
        if code == -5:
            raise RepairAlreadyRunningError("another repair owns the MR gate")
        updated = await self.get_triage_card(card_id)
        if updated is not None:
            from pr_agent.distributed.notifications import build_card_update_notification

            await self.enqueue_notification(
                build_card_update_notification(
                    updated,
                    rollback_task_id,
                    TriageCardState.ROLLBACK_QUEUED,
                    updated.status_markdown,
                )
            )
        return RollbackRequestResult(repair_task_id, rollback_task_id, code == 1, "queued")

    async def finalize_cancel_or_enqueue_rollback(
        self,
        task: TaskEnvelope,
        lease: MrLease | None,
    ) -> RollbackRequestResult | None:
        manifest = await self.freeze_repair_commit_manifest(task.task_id, lease)
        if (
            not repair_rollback_enabled()
            or not cancel_reverts_pushed_commits()
            or manifest is None
            or not manifest.entries
        ):
            await self.finalize_repair_cancel(task, lease, "修复已取消。")
            return None
        binding = await self.get_task_triage_card(task.task_id)
        if binding is None:
            raise RepairRollbackUnavailable("repair card is unavailable")
        trigger = "post_repair_ut_cancel" if task.kind is TaskKind.POST_REPAIR_UT else "cancel"
        return await self.request_repair_rollback(
            task.task_id,
            binding.card_id,
            binding.open_message_id,
            binding.receive_id,
            binding.revision,
            trigger=trigger,
        )

    async def complete_repair_rollback(
        self,
        rollback_task: TaskEnvelope,
        lease: MrLease | None,
        state: RepairRollbackState,
    ) -> bool:
        repair_task_id = str(rollback_task.payload.get("repair_task_id") or "")
        binding = await self.get_task_triage_card(rollback_task.task_id)
        if not repair_task_id or binding is None or rollback_task.mr is None:
            return False
        succeeded = state.status is RepairRollbackStatus.SUCCEEDED
        automatic_failure = state.trigger == "auto_failure"
        post_repair_ut = state.trigger in {"post_repair_ut_failure", "post_repair_ut_cancel"}
        task_status = TaskStatus.COMPLETED if succeeded else TaskStatus.FAILED
        card_state = (
            TriageCardState.REPAIR_SUCCEEDED
            if post_repair_ut
            else TriageCardState.ROLLBACK_SUCCEEDED
            if succeeded
            else TriageCardState.ROLLBACK_FAILED
        )
        if post_repair_ut and succeeded and state.trigger == "post_repair_ut_cancel":
            status_markdown = f"补测已取消，本次补测提交已撤回。撤回 Commit：`{state.rollback_commit_sha[:12]}`"
        elif post_repair_ut and succeeded:
            status_markdown = f"补测失败，本次补测提交已撤回。撤回 Commit：`{state.rollback_commit_sha[:12]}`"
        elif post_repair_ut:
            status_markdown = f"补测已停止，但自动撤回未完成：{state.failure_message or '无法确认安全撤回条件'}"
        elif automatic_failure and succeeded:
            status_markdown = f"修复失败，本次自动修改已撤回。撤回 Commit：`{state.rollback_commit_sha[:12]}`"
        elif automatic_failure:
            status_markdown = f"修复失败，自动撤回未完成：{state.failure_message or '无法确认安全撤回条件'}"
        elif succeeded:
            status_markdown = f"撤回成功。撤回 Commit：`{state.rollback_commit_sha[:12]}`"
        else:
            status_markdown = f"撤回失败：{state.failure_message or '无法确认安全撤回条件'}"
        result = await self._eval(
            COMPLETE_REPAIR_ROLLBACK_LUA,
            [
                self.keys.task(rollback_task.task_id),
                self.keys.task(repair_task_id),
                self.keys.triage_card(binding.card_id),
                self.keys.mr_triage_active(rollback_task.mr),
                self.keys.active_repairs,
                self.keys.mr_lease(rollback_task.mr),
            ],
            [
                lease.worker_id if lease else "",
                str(lease.fencing_token) if lease else "",
                task_status.value,
                state.to_json(),
                _strict_json(state.to_dict(), "rollback result"),
                "" if succeeded else state.failure_message,
                time.time(),
                card_state.value,
                status_markdown,
                state.status.value,
                state.rollback_commit_sha,
                rollback_task.task_id,
                f"{self.keys.prefix}:worker:",
            ],
        )
        if int(result) == -2:
            raise LostLeaseError(rollback_task.task_id)
        if int(result) != 1:
            return False
        updated = await self.get_triage_card(binding.card_id)
        if updated is not None:
            from pr_agent.distributed.notifications import (
                build_auto_failure_rollback_reminder,
                build_card_update_notification,
                build_post_repair_ut_terminal_reminder,
                build_repair_rollback_reminder,
            )

            await self.enqueue_notification(
                build_card_update_notification(updated, rollback_task.task_id, card_state, status_markdown)
            )
            if post_repair_ut:
                await self.enqueue_notification(build_post_repair_ut_terminal_reminder(updated, repair_task_id))
            elif automatic_failure:
                await self.enqueue_notification(
                    build_auto_failure_rollback_reminder(
                        updated,
                        rollback_task.task_id,
                        succeeded=succeeded,
                        rollback_commit_sha=state.rollback_commit_sha,
                        failure_message=state.failure_message,
                    )
                )
            elif succeeded:
                from pr_agent.triage.repair_rollback import rollback_success_notification_enabled

                if rollback_success_notification_enabled():
                    await self.enqueue_notification(
                        build_repair_rollback_reminder(updated, rollback_task.task_id, state.rollback_commit_sha)
                    )
        return True

    async def is_cancel_requested(self, task_id: str) -> bool:
        return await self.redis.hget(self.keys.task(task_id), "cancel_requested") == "1"

    async def finalize_repair_cancel(
        self,
        task: TaskEnvelope,
        lease: MrLease | None,
        status_markdown: str,
    ) -> bool:
        if task.mr is None:
            return False
        binding = await self.get_task_triage_card(task.task_id)
        if binding is None:
            return False
        from pr_agent.distributed.notifications import build_card_update_notification

        items = tuple(
            replace(item, status=RepairItemStatus.FAILED, status_markdown="修复已取消")
            if item.task_id == task.task_id
            else item
            for item in binding.repair_items
        )
        predicted = replace(
            binding,
            repair_items=items,
            state=TriageCardState.CANCELED,
            status_markdown=status_markdown,
            active_task_id="",
            active_category="",
            revision=binding.revision + 1,
        )
        notification = build_card_update_notification(
            predicted,
            task.task_id,
            TriageCardState.CANCELED,
            status_markdown,
        )
        items_json = json.dumps(
            [item.to_dict() for item in items],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        result = await self._eval(
            FINALIZE_REPAIR_CANCEL_LUA,
            [
                self.keys.task(task.task_id),
                self.keys.triage_card(binding.card_id),
                self.keys.mr_triage_active(task.mr),
                self.keys.pipeline_waiting,
                self.keys.notification_dedup(notification.notification_id),
                self.keys.notification_stream,
                self.keys.notification(notification.notification_id),
                self.keys.mr_paused_auto(task.mr),
                self.keys.mr_lease(task.mr),
                self.keys.active_repairs,
            ],
            [
                task.task_id,
                lease.worker_id if lease else "",
                str(lease.fencing_token) if lease else "",
                time.time(),
                items_json,
                status_markdown,
                predicted.revision,
                self.settings.dedup_ttl_seconds,
                notification.to_json(),
                notification.notification_id,
                notification.created_at,
                f"{self.keys.prefix}:worker:",
                f"{self.keys.prefix}:worker:",
                f"{self.keys.prefix}:task:",
                DeliveryKind.RESUME_AUTO.value,
            ],
        )
        if int(result) == -2:
            raise LostLeaseError(task.task_id)
        if int(result) == -3:
            raise RepairAlreadyRunningError("another repair owns the MR gate")
        return int(result) == 1

    async def record_card_message(self, card_id: str, message_id: str, receive_id: str) -> bool:
        result = int(
            await self._eval(
                RECORD_CARD_MESSAGE_LUA,
                [self.keys.triage_card(card_id)],
                [message_id, receive_id, time.time()],
            )
        )
        if result == -1:
            raise ValueError(f"unknown triage card: {card_id}")
        if result == -2:
            raise ValueError(f"triage card message mismatch: {card_id}")
        return result == 1

    async def enqueue_task_with_card(
        self,
        task: TaskEnvelope,
        card_id: str,
        open_message_id: str,
        ttl_seconds: int,
        sender_id: str = "",
        category: str = "",
        selected_categories: tuple[str, ...] = (),
        pipeline_id: int | None = None,
        pipeline_sha: str = "",
        revision: int | None = None,
    ) -> EnqueueResult:
        if ttl_seconds <= 0:
            raise ValueError("triage card TTL must be positive")
        if pipeline_id is not None and pipeline_id <= 0:
            raise ValueError("pipeline_id must be positive")
        if revision is not None and revision < 0:
            raise ValueError("card revision must be non-negative")
        normalized_categories = tuple(str(value).strip().lower() for value in selected_categories)
        allowed_categories = {
            RepairCategory.FORMAT.value,
            RepairCategory.CLANG.value,
            RepairCategory.BUILD.value,
            RepairCategory.UNKNOWN.value,
        }
        if category == "batch":
            if (
                not normalized_categories
                or len(set(normalized_categories)) != len(normalized_categories)
                or any(value not in allowed_categories for value in normalized_categories)
            ):
                raise ValueError("batch repair requires unique supported selected_categories")
        elif normalized_categories:
            raise ValueError("selected_categories require category=batch")
        selected_categories_json = _strict_json(list(normalized_categories), "selected repair categories")
        mr_tasks_key = self.keys.mr_tasks(task.mr) if task.mr else f"{self.keys.prefix}:no-mr:tasks"
        triage_priority_key = (
            self.keys.mr_triage_active(task.mr) if task.mr else f"{self.keys.prefix}:no-mr:triage-active"
        )
        is_priority_triage = self.settings.triage_priority_over_auto and _is_triage_task(task)
        occurred_at = _admission_timestamp(task)
        task_json = _strict_json(task.to_dict(), "task payload")
        admission_context = _strict_json(
            {
                "task_id": task.task_id,
                "mr": task.mr.to_dict() if task.mr else None,
                "card_id": card_id,
                "open_message_id": open_message_id,
                "sender_id": sender_id,
                "category": category,
                "selected_categories": list(normalized_categories),
                "pipeline_id": pipeline_id,
                "pipeline_sha": pipeline_sha,
                "revision": revision,
                "ttl_seconds": ttl_seconds,
            },
            "admission context",
        )
        result = await self._eval(
            ENQUEUE_TASK_WITH_CARD_LUA,
            [
                self.keys.dedup(task.idempotency_key),
                self.keys.task(task.task_id),
                self.keys.ingress_stream,
                mr_tasks_key,
                self.keys.triage_card(card_id),
                triage_priority_key,
                self.keys.mr_paused_auto(task.mr) if task.mr else f"{self.keys.prefix}:no-mr:paused-auto",
                self.keys.mr_latest_repair_card(task.mr)
                if task.mr
                else f"{self.keys.prefix}:no-mr:latest-repair-card",
                self.keys.active_repairs,
            ],
            [
                task.task_id,
                self.settings.dedup_ttl_seconds,
                task_json,
                occurred_at,
                int(task.mr is not None),
                card_id,
                open_message_id,
                ttl_seconds,
                f"{self.keys.prefix}:feishu:task-card:",
                task.pr_url,
                time.time(),
                sender_id,
                int(is_priority_triage),
                f"{self.keys.prefix}:task:",
                category,
                str(pipeline_id or ""),
                pipeline_sha,
                str(revision if revision is not None else ""),
                admission_context,
                selected_categories_json,
            ],
        )
        created = int(result[0])
        if created < 0:
            reasons = {
                -1: "card not found",
                -2: "MR URL mismatch",
                -3: "message ID mismatch",
                -4: "card recipient mismatch",
                -5: "card revision or pipeline is stale",
                -6: "another repair is already running",
                -7: "repair category is not actionable",
                -8: "invalid admission arguments",
                -9: "Redis key type mismatch",
                -10: "repair selection is not actionable",
            }
            message = reasons.get(created, "unknown error")
            if created == -5:
                raise StaleCardActionError(message)
            if created == -6:
                raise RepairAlreadyRunningError(message)
            if created == -10:
                raise ValueError(message)
            raise ValueError(f"cannot bind triage card: {message}")
        enqueue_result = EnqueueResult(
            created=bool(created),
            task_id=str(result[1]),
            recovered=bool(int(result[2])) if len(result) > 2 else False,
        )
        from pr_agent.distributed.lifecycle import LifecycleEvent

        await self.record_lifecycle_event(
            LifecycleEvent.new(
                enqueue_result.task_id,
                "created",
                "point",
                occurred_at=occurred_at,
            )
        )
        await self.record_lifecycle_event(
            LifecycleEvent.new(
                enqueue_result.task_id,
                "queue",
                "start",
                segment_id="initial",
                occurred_at=occurred_at,
            )
        )
        return enqueue_result

    async def admit_post_repair_ut(
        self,
        task: TaskEnvelope,
        *,
        repair_task_id: str,
        card_id: str,
        open_message_id: str,
        sender_id: str,
        pipeline_id: int,
        pipeline_sha: str,
        revision: int,
        ttl_seconds: int,
        coverage_threshold: float,
    ) -> EnqueueResult:
        if task.kind is not TaskKind.POST_REPAIR_UT or task.mr is None:
            raise ValueError("post-repair UT admission requires an MR-scoped UT task")
        if not all((repair_task_id, card_id, open_message_id, sender_id, pipeline_sha)):
            raise ValueError("post-repair UT admission identity is incomplete")
        if pipeline_id <= 0 or revision < 0 or ttl_seconds <= 0:
            raise ValueError("post-repair UT admission numeric identity is invalid")
        occurred_at = _admission_timestamp(task)
        task_json = _strict_json(task.to_dict(), "post-repair UT payload")
        admission_context = _strict_json(
            {
                "task_id": task.task_id,
                "repair_task_id": repair_task_id,
                "card_id": card_id,
                "open_message_id": open_message_id,
                "sender_id": sender_id,
                "pipeline_id": pipeline_id,
                "pipeline_sha": pipeline_sha,
                "revision": revision,
            },
            "post-repair UT admission context",
        )
        initial_state = PostRepairUTState(
            status=PostRepairUTStatus.QUEUED,
            task_id=task.task_id,
            origin_repair_task_id=repair_task_id,
            baseline_pipeline_id=pipeline_id,
            baseline_sha=pipeline_sha,
            coverage_before=task.payload.get("coverage_before"),
            coverage_status_before=str(task.payload.get("coverage_status_before") or ""),
            current_pipeline_id=pipeline_id,
            current_sha=pipeline_sha,
            status_markdown="已进入单元测试补充队列",
        )
        result = await self._eval(
            ADMIT_POST_REPAIR_UT_LUA,
            [
                self.keys.dedup(task.idempotency_key),
                self.keys.task(task.task_id),
                self.keys.ingress_stream,
                self.keys.mr_tasks(task.mr),
                self.keys.triage_card(card_id),
                self.keys.mr_triage_active(task.mr),
                self.keys.task_triage_card(task.task_id),
                self.keys.mr_latest_repair_card(task.mr),
                self.keys.active_repairs,
            ],
            [
                task.task_id,
                self.settings.dedup_ttl_seconds,
                task_json,
                occurred_at,
                card_id,
                repair_task_id,
                open_message_id,
                sender_id,
                revision,
                pipeline_id,
                pipeline_sha,
                coverage_threshold,
                admission_context,
                ttl_seconds,
                json.dumps(initial_state.to_dict(), ensure_ascii=False, separators=(",", ":")),
                "" if initial_state.coverage_before is None else initial_state.coverage_before,
            ],
        )
        code = int(result[0])
        if code == -5:
            raise StaleCardActionError("post-repair UT card is stale or ineligible")
        if code == -6:
            raise RepairAlreadyRunningError("another task owns the MR gate")
        if code < 0:
            raise ValueError(f"cannot admit post-repair UT task: {code}")
        enqueue_result = EnqueueResult(
            created=bool(code),
            task_id=str(result[1]),
            recovered=bool(int(result[2])) if len(result) > 2 else False,
        )
        from pr_agent.distributed.lifecycle import LifecycleEvent

        await self.record_lifecycle_event(
            LifecycleEvent.new(enqueue_result.task_id, "created", "point", occurred_at=occurred_at)
        )
        await self.record_lifecycle_event(
            LifecycleEvent.new(
                enqueue_result.task_id,
                "queue",
                "start",
                segment_id="initial",
                occurred_at=occurred_at,
            )
        )
        return enqueue_result

    async def update_post_repair_ut_with_notification(
        self,
        task_id: str,
        state: PostRepairUTState,
        notification: NotificationEnvelope,
        *,
        terminal: bool,
    ) -> bool:
        card_id = await self.redis.get(self.keys.task_triage_card(task_id))
        if not card_id:
            return False
        result = await self._eval(
            UPDATE_POST_REPAIR_UT_NOTIFICATION_LUA,
            [
                self.keys.triage_card(str(card_id)),
                self.keys.notification_dedup(notification.notification_id),
                self.keys.notification_stream,
                self.keys.notification(notification.notification_id),
            ],
            [
                task_id,
                json.dumps(state.to_dict(), ensure_ascii=False, separators=(",", ":")),
                int(terminal),
                time.time(),
                self.settings.dedup_ttl_seconds,
                notification.to_json(),
                notification.notification_id,
                notification.created_at,
            ],
        )
        return int(result) == 1

    async def transition_triage_card(
        self,
        task_id: str,
        expected: set[TriageCardState],
        target: TriageCardState,
        status_markdown: str,
    ) -> TriageCardBinding | None:
        card_id = await self.redis.get(self.keys.task_triage_card(task_id))
        if not card_id or not expected:
            return None
        changed = await self._eval(
            TRANSITION_TRIAGE_CARD_LUA,
            [self.keys.triage_card(str(card_id))],
            [",".join(sorted(state.value for state in expected)), target.value, status_markdown, time.time()],
        )
        return await self.get_triage_card(str(card_id)) if int(changed) == 1 else None

    async def transition_triage_card_with_notification(
        self,
        task_id: str,
        expected: set[TriageCardState],
        target: TriageCardState,
        status_markdown: str,
        notification: NotificationEnvelope,
    ) -> bool:
        card_id = await self.redis.get(self.keys.task_triage_card(task_id))
        if not card_id or not expected:
            return False
        result = await self._eval(
            TRANSITION_TRIAGE_CARD_NOTIFICATION_LUA,
            [
                self.keys.triage_card(str(card_id)),
                self.keys.notification_dedup(notification.notification_id),
                self.keys.notification_stream,
                self.keys.notification(notification.notification_id),
            ],
            [
                ",".join(sorted(state.value for state in expected)),
                target.value,
                status_markdown,
                time.time(),
                self.settings.dedup_ttl_seconds,
                notification.to_json(),
                notification.notification_id,
                notification.created_at,
                task_id,
            ],
        )
        return bool(int(result))

    async def update_repair_progress_with_notification(
        self,
        task_id: str,
        expected: set[TriageCardState],
        target: TriageCardState,
        status_markdown: str,
        current_pipeline_id: int,
        current_pipeline_sha: str,
        notification: NotificationEnvelope,
        repair_items: tuple[RepairItem, ...] = (),
    ) -> bool:
        card_id = await self.redis.get(self.keys.task_triage_card(task_id))
        if not card_id or not expected:
            return False
        items_json = ""
        if repair_items:
            items_json = json.dumps(
                [item.to_dict() for item in repair_items],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        result = await self._eval(
            UPDATE_REPAIR_PROGRESS_NOTIFICATION_LUA,
            [
                self.keys.triage_card(str(card_id)),
                self.keys.notification_dedup(notification.notification_id),
                self.keys.notification_stream,
                self.keys.notification(notification.notification_id),
            ],
            [
                task_id,
                ",".join(sorted(state.value for state in expected)),
                target.value,
                status_markdown,
                current_pipeline_id,
                current_pipeline_sha,
                time.time(),
                self.settings.dedup_ttl_seconds,
                notification.to_json(),
                notification.notification_id,
                notification.created_at,
                items_json,
            ],
        )
        return bool(int(result))

    async def reconcile_repair_card_with_notification(
        self,
        task_id: str,
        expected_revision: int,
        repair_items: tuple[RepairItem, ...],
        state: TriageCardState,
        status_markdown: str,
        current_pipeline_id: int,
        current_pipeline_sha: str,
        revision: int,
        notification: NotificationEnvelope,
        post_repair_ut: PostRepairUTState | None = None,
    ) -> bool:
        card_id = await self.redis.get(self.keys.task_triage_card(task_id))
        if not card_id:
            return False
        items_json = json.dumps(
            [item.to_dict() for item in repair_items],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        result = await self._eval(
            RECONCILE_REPAIR_CARD_NOTIFICATION_LUA,
            [
                self.keys.triage_card(str(card_id)),
                self.keys.notification_dedup(notification.notification_id),
                self.keys.notification_stream,
                self.keys.notification(notification.notification_id),
            ],
            [
                task_id,
                expected_revision,
                items_json,
                state.value,
                status_markdown,
                current_pipeline_id,
                current_pipeline_sha,
                revision,
                time.time(),
                self.settings.dedup_ttl_seconds,
                notification.to_json(),
                notification.notification_id,
                notification.created_at,
                json.dumps(post_repair_ut.to_dict(), ensure_ascii=False, separators=(",", ":"))
                if post_repair_ut is not None
                else "",
            ],
        )
        return int(result) == 1

    async def transition_task(
        self,
        task_id: str,
        expected: set[TaskStatus],
        target: TaskStatus,
        lease: MrLease | None = None,
        fields: dict[str, str] | None = None,
    ) -> bool:
        extra_fields = fields or {}
        flattened_fields = [item for pair in extra_fields.items() for item in pair]
        lease_key = self.keys.mr_lease(lease.mr) if lease else f"{self.keys.prefix}:no-lease"
        result = await self._eval(
            TRANSITION_TASK_LUA,
            [self.keys.task(task_id), lease_key, self.keys.active_repairs],
            [
                ",".join(sorted(status.value for status in expected)),
                target.value,
                lease.worker_id if lease else "",
                str(lease.fencing_token) if lease else "",
                time.time(),
                len(extra_fields),
                task_id,
                *flattened_fields,
            ],
        )
        if int(result) == -1:
            raise LostLeaseError(task_id)
        return int(result) == 1

    async def get_task(self, task_id: str) -> StoredTask | None:
        value = await self.redis.hgetall(self.keys.task(task_id))
        if not value:
            return None
        return StoredTask(
            envelope=TaskEnvelope.from_json(value["payload"]),
            status=TaskStatus(value["status"]),
            attempt=int(value.get("attempt", 0)),
            worker_id=value.get("worker_id", ""),
            fencing_token=int(value["fencing_token"]) if value.get("fencing_token") else None,
            result=value.get("result", ""),
            error=value.get("error", ""),
            pipeline_project_id=value.get("pipeline_project_id", ""),
            pipeline_sha=value.get("pipeline_sha", ""),
            pipeline_attempt_id=value.get("pipeline_attempt_id", ""),
            pipeline_id=int(value["pipeline_id"]) if value.get("pipeline_id") else None,
            auto_next_command_index=int(value.get("auto_next_command_index", 0)),
            auto_completed_commands=_completed_commands(value.get("auto_completed_commands", "")),
            auto_workflow_head_sha=value.get("auto_workflow_head_sha", ""),
            paused_by_triage_task_id=value.get("paused_by_triage_task_id", ""),
            wait_kind=value.get("wait_kind", ""),
            wait_identity=value.get("wait_identity", ""),
            pipeline_repair_state=PipelineRepairState.from_json(value.get("pipeline_repair_state", "")),
            created_at=_stored_timestamp(value.get("created_at")),
            updated_at=_stored_timestamp(value.get("updated_at")),
            heartbeat_at=_stored_timestamp(value.get("heartbeat_at")),
            cancel_requested=value.get("cancel_requested") == "1",
            delivery_attempt=int(value.get("delivery_attempt") or 0),
            admission_state=value.get("admission_state", ""),
            ingress_message_id=value.get("ingress_message_id", ""),
            admission_context=_json_object(value.get("admission_context")),
            repair_commit_manifest=(
                RepairCommitManifest.from_json(value["repair_commit_manifest"])
                if value.get("repair_commit_manifest")
                else None
            ),
            repair_rollback_state=(
                RepairRollbackState.from_json(value["repair_rollback_state"])
                if value.get("repair_rollback_state")
                else None
            ),
            final_repair_report_state=FinalRepairReportState.from_json(
                value.get("final_repair_report_state", "")
            ),
        )

    async def admit_final_repair_report(self, repair_task_id: str) -> EnqueueResult | None:
        original = await self.get_task(repair_task_id)
        if original is None or original.repair_commit_manifest is None or not original.repair_commit_manifest.entries:
            return None
        child_id = hashlib.sha256(f"final-repair-report:{repair_task_id}".encode("utf-8")).hexdigest()[:32]
        child = TaskEnvelope.new(
            kind=TaskKind.REPAIR_REPORT,
            source="repair_report",
            mr=None,
            pr_url=original.envelope.pr_url,
            command="/summarize-repair",
            payload={"repair_task_id": repair_task_id},
            idempotency_key=f"final-repair-report:{repair_task_id}",
        )
        child = replace(child, task_id=child_id)
        now = datetime.now(timezone.utc).isoformat()
        state = FinalRepairReportState(
            status=RepairReportStatus.QUEUED,
            report_task_id=child_id,
            created_at=now,
            updated_at=now,
        )
        result = await self._eval(
            ADMIT_FINAL_REPAIR_REPORT_LUA,
            [
                self.keys.task(repair_task_id),
                self.keys.task(child_id),
                self.keys.report_ingress_stream,
                self.keys.active_report_tasks,
            ],
            [
                child_id,
                child.to_json(),
                state.to_json(),
                time.time(),
                _strict_json({"repair_task_id": repair_task_id}, "repair report admission context"),
            ],
        )
        created = int(result[0])
        if created < 0:
            return None
        return EnqueueResult(bool(created), str(result[1]))

    async def set_final_repair_report_state(
        self,
        repair_task_id: str,
        report_task_id: str,
        state: FinalRepairReportState,
    ) -> bool:
        original = await self.get_task(repair_task_id)
        if original is None:
            return False
        existing = original.final_repair_report_state
        if existing is not None and existing.status in REPORT_TERMINAL_STATUSES:
            return existing.input_digest == state.input_digest
        async with self.redis.pipeline(transaction=True) as pipeline:
            pipeline.hset(
                self.keys.task(repair_task_id),
                mapping={"final_repair_report_state": state.to_json(), "updated_at": time.time()},
            )
            pipeline.hset(
                self.keys.task(report_task_id),
                mapping={"final_repair_report_state": state.to_json(), "updated_at": time.time()},
            )
            await pipeline.execute()
        return True

    async def complete_final_repair_report(
        self,
        report_task: TaskEnvelope,
        value: FinalRepairReportInput | None,
        state: FinalRepairReportState,
    ) -> bool:
        repair_task_id = str(report_task.payload.get("repair_task_id") or "")
        if not repair_task_id or state.status not in REPORT_TERMINAL_STATUSES:
            raise ValueError("final repair report completion requires a terminal state")
        if value is not None and state.input_digest != value.digest():
            raise ValueError("final repair report digest mismatch")
        from pr_agent.triage.repair_details import repair_details_retention_seconds

        result = await self._eval(
            COMPLETE_FINAL_REPAIR_REPORT_LUA,
            [
                self.keys.task(report_task.task_id),
                self.keys.task(repair_task_id),
                self.keys.active_report_tasks,
                self.keys.final_repair_report_input(repair_task_id),
            ],
            [
                state.to_json(),
                _strict_json(state.report.to_dict() if state.report is not None else {}, "final repair report result"),
                time.time(),
                value.to_json() if value is not None else "",
                repair_details_retention_seconds(),
                report_task.task_id,
                f"{self.keys.prefix}:worker:",
            ],
        )
        if int(result) == -3:
            raise ValueError("completed final repair report digest cannot be replaced")
        return int(result) in {0, 1}

    async def get_final_repair_report_input(self, repair_task_id: str) -> FinalRepairReportInput | None:
        raw = await self.redis.get(self.keys.final_repair_report_input(repair_task_id))
        return FinalRepairReportInput.from_json(_redis_text(raw)) if raw else None

    async def append_repair_commit(
        self,
        task_id: str,
        entry: RepairCommitEntry,
        *,
        base_tree_sha: str,
        source_branch: str,
        authorized_actor_id: str,
        lease: MrLease | None,
    ) -> RepairCommitManifest:
        _validate_manifest_append_input(task_id, entry, base_tree_sha, source_branch, authorized_actor_id)
        result = await self._eval(
            APPEND_REPAIR_COMMIT_LUA,
            [
                self.keys.task(task_id),
                self.keys.mr_lease(lease.mr) if lease else f"{self.keys.prefix}:no-lease",
            ],
            [
                lease.worker_id if lease else "",
                str(lease.fencing_token) if lease else "",
                json.dumps(entry.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                base_tree_sha,
                source_branch,
                authorized_actor_id,
                time.time(),
            ],
        )
        return _repair_manifest_result(task_id, result)

    async def freeze_repair_commit_manifest(
        self,
        task_id: str,
        lease: MrLease | None,
    ) -> RepairCommitManifest | None:
        result = await self._eval(
            FREEZE_REPAIR_MANIFEST_LUA,
            [
                self.keys.task(task_id),
                self.keys.mr_lease(lease.mr) if lease else f"{self.keys.prefix}:no-lease",
            ],
            [
                lease.worker_id if lease else "",
                str(lease.fencing_token) if lease else "",
                datetime.now(timezone.utc).isoformat(),
                time.time(),
            ],
        )
        code = int(result[0])
        if code == -2:
            raise LostLeaseError(task_id)
        if code < 0:
            raise RepairManifestConflict(_redis_text(result[1]))
        raw = _redis_text(result[1])
        return RepairCommitManifest.from_json(raw) if raw else None

    async def publish_repair_rollback_eligibility(
        self,
        task_id: str,
        manifest: RepairCommitManifest | None,
    ) -> TriageCardBinding | None:
        if manifest is None or not manifest.validate_static().ok:
            return None
        binding = await self.get_task_triage_card(task_id)
        if binding is None:
            return None
        revision = binding.revision + 1
        await self.redis.hset(
            self.keys.triage_card(binding.card_id),
            mapping={
                "rollback_repair_task_id": task_id,
                "rollback_commit_count": len(manifest.entries),
                "rollback_task_id": "",
                "rollback_status": "",
                "rollback_commit_sha": "",
                "rollback_trigger": "",
                "revision": revision,
                "updated_at": time.time(),
            },
        )
        updated = await self.get_triage_card(binding.card_id)
        if updated is not None and updated.state in {
            TriageCardState.REPAIR_SUCCEEDED,
            TriageCardState.REPAIR_PARTIAL,
            TriageCardState.REPAIR_FAILED,
        }:
            from pr_agent.distributed.notifications import build_card_update_notification

            await self.enqueue_notification(
                build_card_update_notification(updated, task_id, updated.state, updated.status_markdown)
            )
        return updated

    async def heartbeat_task(self, task_id: str, worker_id: str, fencing_token: int | None) -> bool:
        result = await self._eval(
            HEARTBEAT_TASK_LUA,
            [self.keys.task(task_id)],
            [worker_id, str(fencing_token) if fencing_token is not None else "", time.time()],
        )
        return bool(int(result))

    async def correct_late_repair_terminal(
        self,
        *,
        task_id: str,
        expected_task_status: TaskStatus,
        terminal_state: PipelineRepairState,
        expected_card_states: set[TriageCardState],
        expected_revision: int,
        repair_items: tuple[RepairItem, ...],
        status_markdown: str,
        current_pipeline_id: int,
        current_pipeline_sha: str,
        notification: NotificationEnvelope,
    ) -> bool:
        card_id = await self.redis.get(self.keys.task_triage_card(task_id))
        if not card_id or not expected_card_states:
            return False
        items_json = json.dumps(
            [item.to_dict() for item in repair_items],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        result = await self._eval(
            CORRECT_LATE_REPAIR_TERMINAL_LUA,
            [
                self.keys.task(task_id),
                self.keys.triage_card(str(card_id)),
                self.keys.notification_dedup(notification.notification_id),
                self.keys.notification_stream,
                self.keys.notification(notification.notification_id),
            ],
            [
                expected_task_status.value,
                terminal_state.to_json(),
                ",".join(sorted(state.value for state in expected_card_states)),
                expected_revision,
                items_json,
                TriageCardState.REPAIR_SUCCEEDED.value,
                status_markdown,
                current_pipeline_id,
                current_pipeline_sha,
                expected_revision + 1,
                time.time(),
                self.settings.dedup_ttl_seconds,
                notification.to_json(),
                notification.notification_id,
                notification.created_at,
            ],
        )
        return int(result) == 1

    async def fail_stale_running_task(
        self,
        task_id: str,
        lease: MrLease,
        heartbeat_cutoff: float,
        error: str,
    ) -> bool:
        result = int(
            await self._eval(
                FAIL_STALE_RUNNING_TASK_LUA,
                [
                    self.keys.task(task_id),
                    self.keys.mr_lease(lease.mr),
                    self.keys.active_repairs,
                ],
                [
                    task_id,
                    lease.worker_id,
                    lease.fencing_token,
                    heartbeat_cutoff,
                    error,
                    time.time(),
                ],
            )
        )
        if result == -1:
            raise LostLeaseError(task_id)
        return result == 1

    async def list_active_repairs(self, limit: int = 32) -> list[StoredTask]:
        task_ids = await self.redis.zrange(self.keys.active_repairs, 0, max(0, limit - 1))
        tasks: list[StoredTask] = []
        for task_id in task_ids:
            task = await self.get_task(str(task_id))
            if task is None or task.status in TERMINAL_TASK_STATUSES:
                await self.redis.zrem(self.keys.active_repairs, task_id)
                continue
            tasks.append(task)
        return tasks

    async def requeue_stale_repair(self, task: StoredTask, age_seconds: int) -> bool:
        result = await self._eval(
            REQUEUE_STALE_REPAIR_LUA,
            [self.keys.task(task.task_id), self.keys.ingress_stream],
            [
                task.status.value,
                time.time() - age_seconds,
                task.task_id,
                time.time(),
                f"{self.keys.prefix}:worker:",
            ],
        )
        return bool(int(result))

    async def requeue_stale_auto_workflow(
        self,
        task_id: str,
        *,
        age_seconds: int,
        retry_limit: int,
    ) -> tuple[str, int]:
        result = await self._eval(
            REQUEUE_STALE_AUTO_WORKFLOW_LUA,
            [self.keys.task(task_id), self.keys.ingress_stream],
            [time.time() - age_seconds, task_id, retry_limit, time.time()],
        )
        code, attempt = int(result[0]), int(result[1])
        return {0: "ignored", 1: "requeued", 2: "failed"}[code], attempt

    async def scan_repair_gates(self, cursor: int, limit: int) -> tuple[int, list[tuple[MrKey, str]]]:
        next_cursor, keys = await self.redis.scan(
            cursor=cursor,
            match=f"{self.keys.prefix}:mr:*:triage-active",
            count=max(1, limit),
        )
        gates: list[tuple[MrKey, str]] = []
        for key in keys:
            mr = self.keys.parse_triage_gate(str(key))
            if mr is None:
                continue
            task_id = await self.redis.get(str(key))
            if task_id:
                gates.append((mr, str(task_id)))
        return int(next_cursor), gates

    async def reconcile_admission_gate(self, mr: MrKey, task_id: str) -> str:
        result = int(
            await self._eval(
                RECONCILE_ADMISSION_GATE_LUA,
                [
                    self.keys.mr_triage_active(mr),
                    self.keys.task(task_id),
                    self.keys.mr_tasks(mr),
                    self.keys.active_repairs,
                    self.keys.ingress_stream,
                ],
                [
                    task_id,
                    time.time(),
                    mr.project_id,
                    mr.iid,
                    f"{self.keys.prefix}:feishu:card:",
                    f"{self.keys.prefix}:feishu:task-card:",
                ],
            )
        )
        outcomes = {0: "healthy", 1: "recovered", 2: "failed", 3: "released", 4: "rebuilt"}
        if result not in outcomes:
            raise RuntimeError(f"cannot reconcile admission gate for {mr.redis_id}: code={result}")
        outcome = outcomes[result]
        if outcome != "healthy":
            from pr_agent.distributed.lifecycle import LifecycleEvent

            segment_id = {
                "recovered": "admission_recovered",
                "failed": "admission_failed",
                "released": "stale_gate_released",
                "rebuilt": "admission_index_rebuilt",
            }[outcome]
            await self.record_lifecycle_event(
                LifecycleEvent.new(
                    task_id,
                    "queue",
                    "point",
                    segment_id=segment_id,
                    metadata={"reconciliation": outcome},
                )
            )
            await self.redis.hincrby(self.keys.repair_reconciliation_metrics, outcome, 1)
        return outcome

    async def repair_health(self) -> dict[str, Any]:
        tasks = await self.list_active_repairs(limit=10_000)
        now = time.time()
        status_counts: dict[str, int] = {}
        oldest_state_seconds: dict[str, float] = {}
        cancel_requested = 0
        gate_mismatches = 0
        for task in tasks:
            status_counts[task.status.value] = status_counts.get(task.status.value, 0) + 1
            age = max(0.0, now - (task.updated_at or task.created_at or now))
            oldest_state_seconds[task.status.value] = max(
                oldest_state_seconds.get(task.status.value, 0.0),
                age,
            )
            cancel_requested += int(task.cancel_requested)
            if task.mr is not None:
                active = await self.redis.get(self.keys.mr_triage_active(task.mr))
                gate_mismatches += int(str(active or "") != task.task_id)
        reconciliation = {
            str(key): int(value)
            for key, value in (await self.redis.hgetall(self.keys.repair_reconciliation_metrics)).items()
        }
        return {
            "active": len(tasks),
            "status_counts": status_counts,
            "oldest_state_seconds": oldest_state_seconds,
            "cancel_requested": cancel_requested,
            "mr_gate_mismatches": gate_mismatches,
            "admission_reconciliation": reconciliation,
        }

    async def record_pipeline_repair_state(
        self,
        task_id: str,
        state: PipelineRepairState,
        lease: MrLease | None,
    ) -> bool:
        changed = await self.transition_task(
            task_id,
            {TaskStatus.RUNNING},
            TaskStatus.RUNNING,
            lease,
            {"pipeline_repair_state": state.to_json()},
        )
        if not changed or not state.latest_pipeline_sha:
            return changed
        try:
            stored = await self.get_task(task_id)
            if stored is None or stored.mr is None or not _is_triage_task(stored.envelope):
                return changed
            key = self.keys.repair_sha_tasks(stored.mr.project_id, state.latest_pipeline_sha)
            await self.redis.sadd(key, task_id)
            await self.redis.expire(key, self.settings.pipeline_event_ttl_seconds)
        except Exception:
            from pr_agent.log import get_logger

            get_logger().exception(f"Failed to index latest repair SHA: task_id={task_id}")
        return changed

    async def list_terminal_repair_candidates(self, project_id: str, sha: str) -> list[StoredTask]:
        task_ids = await self.redis.smembers(self.keys.repair_sha_tasks(project_id, sha))
        candidates = []
        for task_id in task_ids:
            stored = await self.get_task(str(task_id))
            if stored is None or stored.status not in TERMINAL_TASK_STATUSES:
                continue
            if stored.pipeline_repair_state.latest_pipeline_sha != sha:
                continue
            candidates.append(stored)
        return candidates

    async def record_triage_persistence(self, task_id: str, success: bool, error: str = "") -> None:
        await self.redis.hset(
            self.keys.triage_persistence_health,
            mapping={
                "status": "ok" if success else "error",
                "task_id": task_id,
                "updated_at": str(time.time()),
                "error": error,
            },
        )

    async def triage_persistence_health(self) -> dict[str, str]:
        value = await self.redis.hgetall(self.keys.triage_persistence_health)
        return {
            "status": str(value.get("status") or "never"),
            "task_id": str(value.get("task_id") or ""),
            "updated_at": str(value.get("updated_at") or ""),
            "error": str(value.get("error") or ""),
        }

    async def get_auto_cursor(self, task_id: str) -> AutoWorkflowCursor:
        stored = await self.get_task(task_id)
        if stored is None:
            raise ValueError(f"unknown task: {task_id}")
        return AutoWorkflowCursor(
            next_command_index=stored.auto_next_command_index,
            completed_commands=stored.auto_completed_commands,
            workflow_head_sha=stored.auto_workflow_head_sha,
            paused_by_triage_task_id=stored.paused_by_triage_task_id,
        )

    async def record_auto_command_completed(
        self,
        task_id: str,
        next_command_index: int,
        completed_commands: list[str],
        workflow_head_sha: str,
        lease: MrLease,
    ) -> bool:
        result = await self._eval(
            RECORD_AUTO_COMMAND_COMPLETED_LUA,
            [self.keys.task(task_id), self.keys.mr_lease(lease.mr)],
            [
                lease.worker_id,
                lease.fencing_token,
                next_command_index,
                json.dumps(completed_commands, ensure_ascii=False, separators=(",", ":")),
                workflow_head_sha,
                time.time(),
            ],
        )
        if int(result) == -1:
            raise LostLeaseError(task_id)
        return int(result) == 1

    async def active_triage_task_id(self, mr: MrKey) -> str:
        if not self.settings.triage_priority_over_auto:
            return ""
        value = await self.redis.get(self.keys.mr_triage_active(mr))
        return str(value) if value else ""

    async def has_pending_triage(self, mr: MrKey) -> bool:
        return bool(await self.active_triage_task_id(mr))

    async def pause_auto_for_triage(
        self,
        task_id: str,
        mr: MrKey,
        *,
        triage_task_id: str,
        next_command_index: int,
        completed_commands: list[str],
        workflow_head_sha: str,
        lease: MrLease,
    ) -> bool:
        if lease.mr != mr:
            raise LostLeaseError(task_id)
        result = await self._eval(
            PAUSE_AUTO_FOR_TRIAGE_LUA,
            [
                self.keys.task(task_id),
                self.keys.mr_lease(mr),
                self.keys.mr_triage_active(mr),
                self.keys.mr_paused_auto(mr),
            ],
            [
                lease.worker_id,
                lease.fencing_token,
                triage_task_id,
                task_id,
                next_command_index,
                json.dumps(completed_commands, ensure_ascii=False, separators=(",", ":")),
                workflow_head_sha,
                time.time(),
                mr.redis_id,
            ],
        )
        if int(result) == -1:
            raise LostLeaseError(task_id)
        if int(result) == -2:
            raise RuntimeError(f"another auto workflow is already paused for {mr.redis_id}")
        return int(result) == 1

    async def resume_auto_after_triage(
        self,
        mr: MrKey,
        *,
        triage_task_id: str,
        worker_id: str,
        fencing_token: int,
    ) -> bool:
        result = await self._eval(
            RESUME_AUTO_AFTER_TRIAGE_LUA,
            [
                self.keys.mr_triage_active(mr),
                self.keys.mr_paused_auto(mr),
                self.keys.mr_lease(mr),
                self.keys.task(triage_task_id),
                self.keys.worker_tasks(worker_id),
                self.keys.worker_inbox(worker_id),
            ],
            [
                triage_task_id,
                worker_id,
                fencing_token,
                time.time(),
                DeliveryKind.RESUME_AUTO.value,
                f"{self.keys.prefix}:task:",
                f"{self.keys.prefix}:worker:",
            ],
        )
        if int(result) == -1:
            raise LostLeaseError(triage_task_id)
        return int(result) == 1

    async def claim_mr(self, mr: MrKey, worker_id: str, lease_seconds: int) -> MrLease:
        owner, token = await self._eval(
            CLAIM_MR_LUA,
            [self.keys.mr_lease(mr), self.keys.mr_fence(mr)],
            [worker_id, lease_seconds * 1000],
        )
        return MrLease(mr=mr, worker_id=str(owner), fencing_token=int(token))

    async def renew_mr(self, mr: MrKey, worker_id: str, fencing_token: int, lease_seconds: int) -> bool:
        result = await self._eval(
            RENEW_MR_LUA,
            [self.keys.mr_lease(mr)],
            [worker_id, fencing_token, lease_seconds * 1000],
        )
        return bool(int(result))

    async def release_mr(self, lease: MrLease) -> bool:
        result = await self._eval(
            RELEASE_MR_LUA,
            [self.keys.mr_lease(lease.mr)],
            [lease.worker_id, lease.fencing_token],
        )
        return bool(int(result))

    async def assert_fence(self, lease: MrLease) -> None:
        result = await self._eval(
            ASSERT_FENCE_LUA,
            [self.keys.mr_lease(lease.mr)],
            [lease.worker_id, lease.fencing_token],
        )
        if not int(result):
            raise LostLeaseError(lease.mr.redis_id)

    async def claim_effect(
        self,
        effect_key: str,
        lease: MrLease | None,
        metadata: dict[str, Any] | None = None,
    ) -> EffectRecord:
        task_id = effect_key.split(":", 1)[0]
        result = await self._eval(
            CLAIM_EFFECT_LUA,
            [
                self.keys.effect(effect_key),
                self.keys.mr_lease(lease.mr) if lease else f"{self.keys.prefix}:no-lease",
                self.keys.task(task_id),
            ],
            [
                lease.worker_id if lease else "",
                str(lease.fencing_token) if lease else "",
                json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                time.time(),
                effect_key,
            ],
        )
        if str(result[0]) == "lost_lease":
            raise LostLeaseError(effect_key)
        return EffectRecord(
            status=str(result[0]),
            metadata=json.loads(str(result[1]) or "{}"),
            result=json.loads(str(result[2])) if result[2] else None,
        )

    async def update_effect_metadata(
        self,
        effect_key: str,
        lease: MrLease | None,
        metadata: dict[str, Any],
    ) -> bool:
        result = await self._eval(
            UPDATE_EFFECT_LUA,
            [
                self.keys.effect(effect_key),
                self.keys.mr_lease(lease.mr) if lease else f"{self.keys.prefix}:no-lease",
            ],
            [
                lease.worker_id if lease else "",
                str(lease.fencing_token) if lease else "",
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                time.time(),
            ],
        )
        if int(result) == -1:
            raise LostLeaseError(effect_key)
        return bool(int(result))

    async def complete_effect(self, effect_key: str, lease: MrLease | None, result_value: Any) -> bool:
        task_id = effect_key.split(":", 1)[0]
        result = await self._eval(
            COMPLETE_EFFECT_LUA,
            [
                self.keys.effect(effect_key),
                self.keys.mr_lease(lease.mr) if lease else f"{self.keys.prefix}:no-lease",
                self.keys.task(task_id),
            ],
            [
                lease.worker_id if lease else "",
                str(lease.fencing_token) if lease else "",
                json.dumps(result_value, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                time.time(),
                effect_key,
            ],
        )
        if int(result) == -1:
            raise LostLeaseError(effect_key)
        return bool(int(result))

    async def has_inflight_effect(self, task_id: str) -> bool:
        return bool(await self.redis.hget(self.keys.task(task_id), "active_effect"))

    async def get_active_effect(self, task_id: str) -> tuple[str, EffectRecord] | None:
        effect_key = _redis_text(await self.redis.hget(self.keys.task(task_id), "active_effect"))
        if not effect_key:
            return None
        value = await self.redis.hgetall(self.keys.effect(effect_key))
        if not value:
            return None
        return (
            effect_key,
            EffectRecord(
                status=_redis_text(value.get("status")),
                metadata=_json_object(value.get("metadata")),
                result=json.loads(_redis_text(value["result"])) if value.get("result") else None,
            ),
        )

    async def heartbeat_worker(
        self,
        worker_id: str,
        active_tasks: int,
        owned_mrs: int,
        *,
        degraded: bool = False,
        active_report_tasks: int = 0,
    ) -> None:
        worker_key = self.keys.worker(worker_id)
        async with self.redis.pipeline(transaction=True) as pipeline:
            pipeline.hset(
                worker_key,
                mapping={
                    "worker_id": worker_id,
                    "last_seen": time.time(),
                    "active_tasks": active_tasks,
                    "owned_mrs": owned_mrs,
                    "degraded": int(degraded),
                    "active_report_tasks": active_report_tasks,
                },
            )
            pipeline.expire(worker_key, self.settings.worker_dead_seconds * 2)
            pipeline.sadd(self.keys.worker_registry, worker_id)
            await pipeline.execute()

    async def heartbeat_service(self, service: str, ttl_seconds: int = 20) -> None:
        key = self.keys.service_heartbeat(service)
        async with self.redis.pipeline(transaction=True) as pipeline:
            pipeline.hset(key, mapping={"service": service, "last_seen": time.time()})
            pipeline.expire(key, ttl_seconds)
            await pipeline.execute()

    async def get_service_heartbeat(self, service: str) -> dict[str, Any]:
        value = await self.redis.hgetall(self.keys.service_heartbeat(service))
        if not value:
            return {"alive": False, "last_seen_age_seconds": None}
        last_seen = float(value.get("last_seen") or 0)
        return {
            "alive": True,
            "last_seen_age_seconds": max(0.0, time.time() - last_seen),
        }

    async def queue_depths(self) -> dict[str, int | float]:
        workers = await self.list_live_workers()
        inbox_depth = 0
        oldest_agent_inbox_seconds = 0.0
        for worker in workers:
            inbox_depth += await self._stream_group_depth(self.keys.worker_inbox(worker.worker_id), "agent")
            oldest_agent_inbox_seconds = max(
                oldest_agent_inbox_seconds,
                await self._oldest_stream_entry_seconds(self.keys.worker_inbox(worker.worker_id)),
            )
        return {
            "ingress": await self._stream_group_depth(self.keys.ingress_stream, "scheduler"),
            "oldest_ingress_seconds": await self._oldest_stream_entry_seconds(self.keys.ingress_stream),
            "agent_inboxes": inbox_depth,
            "oldest_agent_inbox_seconds": oldest_agent_inbox_seconds,
            "notifications": await self._stream_group_depth(self.keys.notification_stream, "feishu"),
            "waiting_pipeline": int(await self.redis.zcard(self.keys.pipeline_waiting)),
        }

    async def _stream_group_depth(self, stream: str, group: str) -> int:
        try:
            groups = await self.redis.xinfo_groups(stream)
        except redis.ResponseError:
            return 0
        for item in groups:
            if str(item.get("name")) == group:
                return int(item.get("pending") or 0) + int(item.get("lag") or 0)
        return 0

    async def _oldest_stream_entry_seconds(self, stream: str) -> float:
        entries = await self.redis.xrange(stream, min="-", max="+", count=1)
        if not entries:
            return 0.0
        timestamp_ms = int(str(entries[0][0]).split("-", 1)[0])
        return max(0.0, time.time() - timestamp_ms / 1000)

    async def list_live_workers(self) -> list[WorkerState]:
        worker_ids = await self.redis.smembers(self.keys.worker_registry)
        workers: list[WorkerState] = []
        for worker_id in worker_ids:
            value = await self.redis.hgetall(self.keys.worker(str(worker_id)))
            if not value:
                await self.redis.srem(self.keys.worker_registry, worker_id)
                continue
            if time.time() - float(value["last_seen"]) > self.settings.worker_dead_seconds:
                continue
            workers.append(
                WorkerState(
                    worker_id=str(worker_id),
                    last_seen=float(value["last_seen"]),
                    active_tasks=int(value.get("active_tasks", 0)),
                    owned_mrs=int(value.get("owned_mrs", 0)),
                    degraded=value.get("degraded", "0") == "1",
                    active_report_tasks=int(value.get("active_report_tasks", 0)),
                )
            )
        return workers

    async def acquire_scheduler_leader(self, scheduler_id: str, ttl_seconds: int) -> bool:
        result = await self._eval(
            ACQUIRE_LEADER_LUA,
            [self.keys.scheduler_leader],
            [scheduler_id, ttl_seconds],
        )
        return bool(int(result))

    async def _ensure_stream_group(self, stream: str, group: str) -> None:
        try:
            await self.redis.xgroup_create(stream, group, id="0", mkstream=True)
        except redis.ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    @staticmethod
    def _stream_messages(response: Any) -> list[tuple[str, dict[str, str]]]:
        return [(str(message_id), fields) for _, messages in response for message_id, fields in messages]

    async def read_ingress_group(self, consumer: str, limit: int, block_ms: int) -> list[IngressDelivery]:
        group = "scheduler"
        await self._ensure_stream_group(self.keys.ingress_stream, group)
        claimed = await self.redis.xautoclaim(
            self.keys.ingress_stream,
            group,
            consumer,
            min_idle_time=max(block_ms, 1000),
            start_id="0-0",
            count=limit,
        )
        claimed_messages = claimed[1] if len(claimed) > 1 else []
        remaining = max(0, limit - len(claimed_messages))
        new_response = []
        if remaining:
            new_response = await self.redis.xreadgroup(
                group,
                consumer,
                {self.keys.ingress_stream: ">"},
                count=remaining,
                block=block_ms,
            )
        messages = [(str(message_id), fields) for message_id, fields in claimed_messages]
        messages.extend(self._stream_messages(new_response))
        return [
            IngressDelivery(message_id=message_id, task_id=str(fields["task_id"]))
            for message_id, fields in messages
        ]

    async def ack_ingress(self, message_id: str) -> None:
        async with self.redis.pipeline(transaction=True) as pipeline:
            pipeline.xack(self.keys.ingress_stream, "scheduler", message_id)
            pipeline.xdel(self.keys.ingress_stream, message_id)
            await pipeline.execute()

    async def read_report_ingress_group(
        self,
        consumer: str,
        limit: int,
        block_ms: int = 0,
    ) -> list[IngressDelivery]:
        group = "report-scheduler"
        await self._ensure_stream_group(self.keys.report_ingress_stream, group)
        claimed = await self.redis.xautoclaim(
            self.keys.report_ingress_stream,
            group,
            consumer,
            min_idle_time=max(block_ms, 1000),
            start_id="0-0",
            count=limit,
        )
        claimed_messages = claimed[1] if len(claimed) > 1 else []
        remaining = max(0, limit - len(claimed_messages))
        new_response = []
        if remaining:
            if block_ms > 0:
                new_response = await self.redis.xreadgroup(
                    group,
                    consumer,
                    {self.keys.report_ingress_stream: ">"},
                    count=remaining,
                    block=block_ms,
                )
            else:
                # Redis interprets BLOCK 0 as "wait forever", not "do not block".
                new_response = await self.redis.xreadgroup(
                    group,
                    consumer,
                    {self.keys.report_ingress_stream: ">"},
                    count=remaining,
                )
        messages = [(str(message_id), fields) for message_id, fields in claimed_messages]
        messages.extend(self._stream_messages(new_response))
        return [
            IngressDelivery(message_id=message_id, task_id=str(fields["task_id"]))
            for message_id, fields in messages
        ]

    async def ack_report_ingress(self, message_id: str) -> None:
        async with self.redis.pipeline(transaction=True) as pipeline:
            pipeline.xack(self.keys.report_ingress_stream, "report-scheduler", message_id)
            pipeline.xdel(self.keys.report_ingress_stream, message_id)
            await pipeline.execute()

    async def get_mr_lease(self, mr: MrKey) -> MrLease | None:
        value = await self.redis.hgetall(self.keys.mr_lease(mr))
        if not value:
            return None
        return MrLease(mr=mr, worker_id=value["worker_id"], fencing_token=int(value["fencing_token"]))

    async def revoke_mr_if_owner(self, lease: MrLease) -> bool:
        result = await self._eval(
            REVOKE_MR_LUA,
            [self.keys.mr_lease(lease.mr)],
            [lease.worker_id, lease.fencing_token],
        )
        return bool(int(result))

    async def assign_to_worker(self, task: TaskEnvelope, lease: MrLease | None, worker_id: str) -> bool:
        if lease is not None and lease.worker_id != worker_id:
            raise LostLeaseError(task.task_id)
        result = await self._eval(
            ASSIGN_TASK_LUA,
            [
                self.keys.task(task.task_id),
                self.keys.mr_lease(lease.mr) if lease else f"{self.keys.prefix}:no-lease",
                self.keys.worker_tasks(worker_id),
                self.keys.worker_inbox(worker_id),
            ],
            [worker_id, str(lease.fencing_token) if lease else "", time.time(), task.task_id],
        )
        if int(result) == -1:
            raise LostLeaseError(task.task_id)
        assigned = int(result) == 1
        if assigned:
            from pr_agent.distributed.lifecycle import LifecycleEvent

            await self.record_lifecycle_event(
                LifecycleEvent.new(task.task_id, "queue", "end", segment_id="initial")
            )
        return assigned

    async def read_worker_inbox(self, worker_id: str, block_ms: int) -> InboxDelivery | None:
        stream = self.keys.worker_inbox(worker_id)
        group = "agent"
        await self._ensure_stream_group(stream, group)
        locally_active = self._local_inbox_messages.setdefault(worker_id, set())
        claimed = await self.redis.xautoclaim(
            stream,
            group,
            worker_id,
            min_idle_time=max(block_ms, 1000),
            start_id="0-0",
            count=max(32, self.settings.worker_inbox_prefetch),
        )
        messages = [
            (str(message_id), fields)
            for message_id, fields in (claimed[1] if len(claimed) > 1 else [])
            if str(message_id) not in locally_active
        ]
        if not messages:
            response = await self.redis.xreadgroup(group, worker_id, {stream: ">"}, count=1, block=block_ms)
            messages = self._stream_messages(response)
        if not messages:
            return None
        message_id, fields = messages[0]
        locally_active.add(message_id)
        stored_task = await self.get_task(str(fields["task_id"]))
        if stored_task is None:
            await self.ack_worker_inbox(worker_id, message_id)
            return None
        payload = json.loads(fields.get("payload", "{}"))
        return InboxDelivery(
            message_id=message_id,
            task=stored_task.envelope,
            kind=DeliveryKind(fields.get("delivery_kind", DeliveryKind.EXECUTE.value)),
            payload=payload,
        )

    async def ack_worker_inbox(self, worker_id: str, message_id: str) -> None:
        stream = self.keys.worker_inbox(worker_id)
        async with self.redis.pipeline(transaction=True) as pipeline:
            pipeline.xack(stream, "agent", message_id)
            pipeline.xdel(stream, message_id)
            await pipeline.execute()
        self._local_inbox_messages.setdefault(worker_id, set()).discard(message_id)

    async def record_delivery_failure(self, task_id: str, message_id: str, error: str) -> int:
        try:
            async with self.redis.pipeline(transaction=True) as pipeline:
                pipeline.hincrby(self.keys.task(task_id), "delivery_attempt", 1)
                pipeline.hset(
                    self.keys.task(task_id),
                    mapping={
                        "delivery_error": error,
                        "delivery_message_id": message_id,
                        "delivery_failed_at": time.time(),
                    },
                )
                result = await pipeline.execute()
        finally:
            for message_ids in self._local_inbox_messages.values():
                message_ids.discard(message_id)
        return int(result[0])

    async def list_dead_worker_ids(self) -> list[str]:
        worker_ids = await self.redis.smembers(self.keys.worker_registry)
        dead: list[str] = []
        now = time.time()
        for worker_id in worker_ids:
            last_seen = await self.redis.hget(self.keys.worker(str(worker_id)), "last_seen")
            if last_seen is None or now - float(last_seen) > self.settings.worker_dead_seconds:
                dead.append(str(worker_id))
        return dead

    async def get_worker_task_ids(self, worker_id: str) -> list[str]:
        return [str(task_id) for task_id in await self.redis.smembers(self.keys.worker_tasks(worker_id))]

    async def recover_dead_worker_task(self, worker_id: str, task_id: str) -> str:
        stored = await self.get_task(task_id)
        retry_limit = self.settings.task_retry_limit
        if stored is not None and stored.envelope.kind is TaskKind.AUTO_WORKFLOW:
            retry_limit = self.settings.auto_workflow_retry_limit
        ingress_stream = (
            self.keys.report_ingress_stream
            if stored is not None and stored.envelope.kind is TaskKind.REPAIR_REPORT
            else self.keys.ingress_stream
        )
        result = await self._eval(
            RECOVER_DEAD_TASK_LUA,
            [self.keys.task(task_id), self.keys.worker_tasks(worker_id), ingress_stream],
            [worker_id, task_id, retry_limit, time.time()],
        )
        return {
            0: "ignored",
            1: "requeued",
            2: "failed",
            3: "waiting_pipeline",
            4: "publishing",
            5: "paused_by_triage",
        }[int(result)]

    async def get_cached_pipeline_event(
        self,
        project_id: str,
        sha: str,
        pipeline_id: int | None = None,
    ) -> PipelineEvent | None:
        value = await self.redis.get(self.keys.pipeline_event(project_id, sha, pipeline_id))
        return PipelineEvent.from_json(value) if value else None

    async def transfer_orphaned_task(
        self,
        task: StoredTask,
        old_worker_id: str,
        lease: MrLease,
        pipeline_event: PipelineEvent | None = None,
    ) -> bool:
        result = await self._eval(
            TRANSFER_ORPHANED_TASK_LUA,
            [
                self.keys.task(task.task_id),
                self.keys.mr_lease(lease.mr),
                self.keys.worker_tasks(old_worker_id),
                self.keys.worker_tasks(lease.worker_id),
                self.keys.worker_inbox(lease.worker_id),
            ],
            [
                old_worker_id,
                lease.worker_id,
                lease.fencing_token,
                time.time(),
                task.task_id,
                pipeline_event.to_json() if pipeline_event else "",
            ],
        )
        if int(result) == -1:
            raise LostLeaseError(task.task_id)
        return int(result) == 1

    async def expire_worker_for_test(self, worker_id: str) -> None:
        await self.redis.delete(self.keys.worker(worker_id))

    async def record_task_result(self, task_id: str, result: dict[str, Any], lease: MrLease | None) -> bool:
        return await self.transition_task(
            task_id,
            {TaskStatus.PUBLISHING},
            TaskStatus.PUBLISHING,
            lease,
            {"result": json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)},
        )

    async def register_pipeline_wait(
        self,
        task_id: str,
        project_id: str,
        sha: str,
        attempt_id: str = "",
        pipeline_id: int | None = None,
    ) -> PipelineEvent | None:
        event_key = self.keys.pipeline_event(project_id, sha, pipeline_id)
        waiter_key = self.keys.pipeline_waiters(project_id, sha, pipeline_id)
        cached = await self._eval(
            REGISTER_PIPELINE_WAIT_LUA,
            [
                event_key,
                waiter_key,
                self.keys.task(task_id),
                self.keys.pipeline_waiting,
            ],
            [
                task_id,
                self.settings.pipeline_event_ttl_seconds,
                project_id,
                sha,
                time.time(),
                event_key,
                waiter_key,
                attempt_id,
                str(pipeline_id) if pipeline_id is not None else "",
            ],
        )
        return PipelineEvent.from_json(str(cached)) if cached else None

    async def publish_pipeline_event(self, event: PipelineEvent) -> list[str]:
        result = await self._eval(
            PUBLISH_PIPELINE_EVENT_LUA,
            [
                self.keys.pipeline_event(event.project_id, event.sha),
                self.keys.pipeline_event(event.project_id, event.sha, event.pipeline_id),
                self.keys.pipeline_waiters(event.project_id, event.sha),
                self.keys.pipeline_waiters(event.project_id, event.sha, event.pipeline_id),
                self.keys.pipeline_waiting,
            ],
            [
                event.to_json(),
                self.settings.pipeline_event_ttl_seconds,
                f"{self.keys.prefix}:task:",
                f"{self.keys.prefix}:worker:",
                "1" if event.terminal else "0",
            ],
        )
        resumed = [str(task_id) for task_id in result]
        if event.status == "success":
            try:
                from pr_agent.triage.terminal import reconcile_late_repair_success

                await reconcile_late_repair_success(self, event)
            except Exception:
                from pr_agent.log import get_logger

                get_logger().exception(
                    f"Failed to reconcile late repair pipeline: project={event.project_id} sha={event.sha}"
                )
        return resumed

    async def resume_pipeline_if_cached(self, task_id: str) -> bool:
        result = await self._eval(
            RESUME_CACHED_PIPELINE_LUA,
            [self.keys.task(task_id), self.keys.pipeline_waiting],
            [task_id, f"{self.keys.prefix}:worker:"],
        )
        return bool(int(result))

    async def complete_pipeline_resume(self, task_id: str, event: PipelineEvent) -> bool:
        result = await self._eval(
            COMPLETE_PIPELINE_RESUME_LUA,
            [self.keys.task(task_id), self.keys.pipeline_waiting],
            [task_id, event.to_json()],
        )
        return int(result) == 1

    async def claim_pipeline_resume(
        self,
        task_id: str,
        event: PipelineEvent,
        lease: MrLease | None,
    ):
        from pr_agent.distributed.models import PipelineResumeClaim

        lease_key = self.keys.mr_lease(lease.mr) if lease else f"{self.keys.prefix}:no-lease"
        result = int(
            await self._eval(
                CLAIM_PIPELINE_RESUME_LUA,
                [self.keys.task(task_id), lease_key, self.keys.pipeline_waiting],
                [
                    task_id,
                    event.to_json(),
                    lease.worker_id if lease else "",
                    str(lease.fencing_token) if lease else "",
                    time.time(),
                ],
            )
        )
        return {
            1: PipelineResumeClaim.CLAIMED,
            0: PipelineResumeClaim.DUPLICATE,
            2: PipelineResumeClaim.STALE,
            -1: PipelineResumeClaim.LOST_LEASE,
        }[result]

    async def list_stale_pipeline_waits(self, age_seconds: int, limit: int = 32) -> list[StoredTask]:
        task_ids = await self.redis.zrangebyscore(
            self.keys.pipeline_waiting,
            min="-inf",
            max=time.time() - age_seconds,
            start=0,
            num=limit,
        )
        tasks = []
        for task_id in task_ids:
            task = await self.get_task(str(task_id))
            if task is None or task.status is not TaskStatus.WAITING_PIPELINE:
                await self.redis.zrem(self.keys.pipeline_waiting, task_id)
            else:
                tasks.append(task)
        return tasks

    async def defer_pipeline_fallback(self, task_id: str) -> None:
        await self.redis.zadd(self.keys.pipeline_waiting, {task_id: time.time()})

    async def enqueue_notification(self, notification: NotificationEnvelope) -> bool:
        result = await self._eval(
            ENQUEUE_NOTIFICATION_LUA,
            [
                self.keys.notification_dedup(notification.notification_id),
                self.keys.notification_stream,
                self.keys.notification(notification.notification_id),
            ],
            [
                self.settings.dedup_ttl_seconds,
                notification.to_json(),
                notification.notification_id,
                notification.created_at,
            ],
        )
        queued = bool(int(result))
        if queued and notification.task_id:
            from pr_agent.distributed.lifecycle import LifecycleEvent

            await self.record_lifecycle_event(
                LifecycleEvent.new(
                    notification.task_id,
                    "notification",
                    "start",
                    segment_id=notification.notification_id,
                )
            )
        return queued

    async def enqueue_card_fallback(self, card_id: str, notification: NotificationEnvelope) -> bool:
        result = await self._eval(
            ENQUEUE_CARD_FALLBACK_LUA,
            [
                self.keys.triage_card(card_id),
                self.keys.notification_dedup(notification.notification_id),
                self.keys.notification_stream,
                self.keys.notification(notification.notification_id),
            ],
            [
                self.settings.dedup_ttl_seconds,
                notification.to_json(),
                notification.notification_id,
                notification.created_at,
                time.time(),
            ],
        )
        if int(result) == -1:
            raise ValueError(f"unknown triage card: {card_id}")
        return bool(int(result))

    async def read_notification(self, consumer_id: str, block_ms: int) -> tuple[str, NotificationEnvelope] | None:
        group = "feishu"
        await self._ensure_stream_group(self.keys.notification_stream, group)
        claimed = await self.redis.xautoclaim(
            self.keys.notification_stream,
            group,
            consumer_id,
            min_idle_time=max(block_ms, 1000),
            start_id="0-0",
            count=1,
        )
        messages = [(str(message_id), fields) for message_id, fields in (claimed[1] if len(claimed) > 1 else [])]
        if not messages:
            response = await self.redis.xreadgroup(
                group,
                consumer_id,
                {self.keys.notification_stream: ">"},
                count=1,
                block=block_ms,
            )
            messages = self._stream_messages(response)
        if not messages:
            return None
        message_id, fields = messages[0]
        notification_id = str(fields["notification_id"])
        payload = await self.redis.hget(self.keys.notification(notification_id), "payload")
        if not payload:
            await self.ack_notification(message_id)
            return None
        return message_id, NotificationEnvelope.from_json(payload)

    async def ack_notification(self, message_id: str) -> None:
        async with self.redis.pipeline(transaction=True) as pipeline:
            pipeline.xack(self.keys.notification_stream, "feishu", message_id)
            pipeline.xdel(self.keys.notification_stream, message_id)
            await pipeline.execute()

    async def complete_notification(self, notification_id: str, message_id: str) -> None:
        await self.redis.hset(
            self.keys.notification(notification_id),
            mapping={"status": "completed", "message_id": message_id, "updated_at": time.time()},
        )

    async def fail_notification_attempt(self, notification_id: str, error: str) -> int:
        key = self.keys.notification(notification_id)
        async with self.redis.pipeline(transaction=True) as pipeline:
            pipeline.hincrby(key, "attempt", 1)
            pipeline.hset(key, mapping={"status": "retrying", "error": error, "updated_at": time.time()})
            result = await pipeline.execute()
        return int(result[0])

    async def dead_letter_notification(self, notification_id: str, error: str) -> None:
        await self.redis.hset(
            self.keys.notification(notification_id),
            mapping={"status": "dead", "error": error, "updated_at": time.time()},
        )

    async def claim_fixing_notice(self, mr: MrKey, ttl_seconds: int) -> bool:
        return bool(await self.redis.set(self.keys.fixing_notice(mr), "1", ex=ttl_seconds, nx=True))

    async def is_mr_triage_active(self, mr: MrKey) -> bool:
        task_ids = await self.redis.smembers(self.keys.mr_tasks(mr))
        for task_id in task_ids:
            stored_task = await self.get_task(str(task_id))
            raw_command = stored_task.envelope.command if stored_task else ""
            command = raw_command.split()[0].lower() if raw_command else ""
            if (
                stored_task is not None
                and bool(stored_task.envelope.command)
                and command in {"/triage", "/fix-format", "/fix_format", "/repair-pipeline"}
                and stored_task.status in {TaskStatus.ASSIGNED, TaskStatus.RUNNING, TaskStatus.WAITING_PIPELINE}
            ):
                return True
        return False

    async def force_expire_mr_for_test(self, mr: MrKey) -> None:
        await self.redis.delete(self.keys.mr_lease(mr))


class SyncRedisBroker:
    """Synchronous bridge for existing Git provider and tool code."""

    def __init__(self, redis_client: redis.Redis, settings: DistributedSettings, keys: RedisKeys | None = None) -> None:
        self.redis = redis_client
        self.settings = settings
        self.keys = keys or RedisKeys()

    def _eval(self, script: str, keys: list[str], args: list[Any]) -> Any:
        return self.redis.eval(script, len(keys), *keys, *args)

    def record_lifecycle_event(self, event) -> bool:
        key = self.keys.lifecycle(event.task_id)
        events_key = self.keys.lifecycle_events(event.task_id)
        with self.redis.pipeline(transaction=True) as pipeline:
            pipeline.zadd(key, {event.event_id: event.occurred_at}, nx=True)
            pipeline.hsetnx(events_key, event.event_id, event.to_json())
            pipeline.expire(key, self.settings.pipeline_event_ttl_seconds)
            pipeline.expire(events_key, self.settings.pipeline_event_ttl_seconds)
            result = pipeline.execute()
        return bool(int(result[0]))

    def append_repair_progress(self, event) -> str:
        from pr_agent.triage.repair_details import repair_details_event_limit, repair_details_retention_seconds

        key = self.keys.repair_progress(event.task_id)
        event_id = self.redis.xadd(
            key,
            {"payload": event.to_json()},
            maxlen=repair_details_event_limit(),
            approximate=True,
        )
        self.redis.expire(key, repair_details_retention_seconds())
        return _redis_text(event_id)

    def get_repair_progress(self, task_id: str, *, after_id: str = "", count: int = 200):
        key = self.keys.repair_progress(task_id)
        minimum = f"({after_id}" if after_id else "-"
        records = self.redis.xrange(key, min=minimum, max="+", count=max(1, count))
        return _repair_progress_events(records)

    def get_lifecycle_events(self, task_id: str):
        from pr_agent.distributed.lifecycle import LifecycleEvent

        event_ids = [str(value) for value in self.redis.zrange(self.keys.lifecycle(task_id), 0, -1)]
        if not event_ids:
            return []
        values = self.redis.hmget(self.keys.lifecycle_events(task_id), event_ids)
        return [LifecycleEvent.from_json(value) for value in values if value]

    def get_triage_card(self, card_id: str) -> TriageCardBinding | None:
        return _triage_card_from_hash(self.redis.hgetall(self.keys.triage_card(card_id)))

    def get_task_triage_card(self, task_id: str) -> TriageCardBinding | None:
        card_id = self.redis.get(self.keys.task_triage_card(task_id))
        return self.get_triage_card(str(card_id)) if card_id else None

    def get_repair_commit_manifest(self, task_id: str) -> RepairCommitManifest | None:
        raw = self.redis.hget(self.keys.task(task_id), "repair_commit_manifest")
        return RepairCommitManifest.from_json(_redis_text(raw)) if raw else None

    def append_repair_commit(
        self,
        task_id: str,
        entry: RepairCommitEntry,
        *,
        base_tree_sha: str,
        source_branch: str,
        authorized_actor_id: str,
        lease: MrLease | None,
    ) -> RepairCommitManifest:
        _validate_manifest_append_input(task_id, entry, base_tree_sha, source_branch, authorized_actor_id)
        result = self._eval(
            APPEND_REPAIR_COMMIT_LUA,
            [
                self.keys.task(task_id),
                self.keys.mr_lease(lease.mr) if lease else f"{self.keys.prefix}:no-lease",
            ],
            [
                lease.worker_id if lease else "",
                str(lease.fencing_token) if lease else "",
                json.dumps(entry.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                base_tree_sha,
                source_branch,
                authorized_actor_id,
                time.time(),
            ],
        )
        return _repair_manifest_result(task_id, result)

    def freeze_repair_commit_manifest(
        self,
        task_id: str,
        lease: MrLease | None,
    ) -> RepairCommitManifest | None:
        result = self._eval(
            FREEZE_REPAIR_MANIFEST_LUA,
            [
                self.keys.task(task_id),
                self.keys.mr_lease(lease.mr) if lease else f"{self.keys.prefix}:no-lease",
            ],
            [
                lease.worker_id if lease else "",
                str(lease.fencing_token) if lease else "",
                datetime.now(timezone.utc).isoformat(),
                time.time(),
            ],
        )
        code = int(result[0])
        if code == -2:
            raise LostLeaseError(task_id)
        if code < 0:
            raise RepairManifestConflict(_redis_text(result[1]))
        raw = _redis_text(result[1])
        return RepairCommitManifest.from_json(raw) if raw else None

    def is_cancel_requested(self, task_id: str) -> bool:
        return self.redis.hget(self.keys.task(task_id), "cancel_requested") == "1"

    def transition_triage_card(
        self,
        task_id: str,
        expected: set[TriageCardState],
        target: TriageCardState,
        status_markdown: str,
    ) -> TriageCardBinding | None:
        card_id = self.redis.get(self.keys.task_triage_card(task_id))
        if not card_id or not expected:
            return None
        changed = self._eval(
            TRANSITION_TRIAGE_CARD_LUA,
            [self.keys.triage_card(str(card_id))],
            [",".join(sorted(state.value for state in expected)), target.value, status_markdown, time.time()],
        )
        return self.get_triage_card(str(card_id)) if int(changed) == 1 else None

    def transition_triage_card_with_notification(
        self,
        task_id: str,
        expected: set[TriageCardState],
        target: TriageCardState,
        status_markdown: str,
        notification: NotificationEnvelope,
    ) -> bool:
        card_id = self.redis.get(self.keys.task_triage_card(task_id))
        if not card_id or not expected:
            return False
        result = self._eval(
            TRANSITION_TRIAGE_CARD_NOTIFICATION_LUA,
            [
                self.keys.triage_card(str(card_id)),
                self.keys.notification_dedup(notification.notification_id),
                self.keys.notification_stream,
                self.keys.notification(notification.notification_id),
            ],
            [
                ",".join(sorted(state.value for state in expected)),
                target.value,
                status_markdown,
                time.time(),
                self.settings.dedup_ttl_seconds,
                notification.to_json(),
                notification.notification_id,
                notification.created_at,
                task_id,
            ],
        )
        return bool(int(result))

    def reconcile_repair_card_with_notification(
        self,
        task_id: str,
        expected_revision: int,
        repair_items: tuple[RepairItem, ...],
        state: TriageCardState,
        status_markdown: str,
        current_pipeline_id: int,
        current_pipeline_sha: str,
        revision: int,
        notification: NotificationEnvelope,
        post_repair_ut: PostRepairUTState | None = None,
    ) -> bool:
        card_id = self.redis.get(self.keys.task_triage_card(task_id))
        if not card_id:
            return False
        items_json = json.dumps(
            [item.to_dict() for item in repair_items],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        result = self._eval(
            RECONCILE_REPAIR_CARD_NOTIFICATION_LUA,
            [
                self.keys.triage_card(str(card_id)),
                self.keys.notification_dedup(notification.notification_id),
                self.keys.notification_stream,
                self.keys.notification(notification.notification_id),
            ],
            [
                task_id,
                expected_revision,
                items_json,
                state.value,
                status_markdown,
                current_pipeline_id,
                current_pipeline_sha,
                revision,
                time.time(),
                self.settings.dedup_ttl_seconds,
                notification.to_json(),
                notification.notification_id,
                notification.created_at,
                json.dumps(post_repair_ut.to_dict(), ensure_ascii=False, separators=(",", ":"))
                if post_repair_ut is not None
                else "",
            ],
        )
        return int(result) == 1

    def register_pipeline_wait(
        self,
        task_id: str,
        project_id: str,
        sha: str,
        attempt_id: str = "",
        pipeline_id: int | None = None,
    ) -> PipelineEvent | None:
        event_key = self.keys.pipeline_event(project_id, sha, pipeline_id)
        waiter_key = self.keys.pipeline_waiters(project_id, sha, pipeline_id)
        cached = self._eval(
            REGISTER_PIPELINE_WAIT_LUA,
            [
                event_key,
                waiter_key,
                self.keys.task(task_id),
                self.keys.pipeline_waiting,
            ],
            [
                task_id,
                self.settings.pipeline_event_ttl_seconds,
                project_id,
                sha,
                time.time(),
                event_key,
                waiter_key,
                attempt_id,
                str(pipeline_id) if pipeline_id is not None else "",
            ],
        )
        return PipelineEvent.from_json(str(cached)) if cached else None

    def assert_fence(self, lease: MrLease) -> None:
        result = self._eval(
            ASSERT_FENCE_LUA,
            [self.keys.mr_lease(lease.mr)],
            [lease.worker_id, lease.fencing_token],
        )
        if not int(result):
            raise LostLeaseError(lease.mr.redis_id)

    def claim_effect(
        self,
        effect_key: str,
        lease: MrLease | None,
        metadata: dict[str, Any] | None = None,
    ) -> EffectRecord:
        task_id = effect_key.split(":", 1)[0]
        result = self._eval(
            CLAIM_EFFECT_LUA,
            [
                self.keys.effect(effect_key),
                self.keys.mr_lease(lease.mr) if lease else f"{self.keys.prefix}:no-lease",
                self.keys.task(task_id),
            ],
            [
                lease.worker_id if lease else "",
                str(lease.fencing_token) if lease else "",
                json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                time.time(),
                effect_key,
            ],
        )
        if str(result[0]) == "lost_lease":
            raise LostLeaseError(effect_key)
        return EffectRecord(
            status=str(result[0]),
            metadata=json.loads(str(result[1]) or "{}"),
            result=json.loads(str(result[2])) if result[2] else None,
        )

    def update_effect_metadata(
        self,
        effect_key: str,
        lease: MrLease | None,
        metadata: dict[str, Any],
    ) -> bool:
        result = self._eval(
            UPDATE_EFFECT_LUA,
            [
                self.keys.effect(effect_key),
                self.keys.mr_lease(lease.mr) if lease else f"{self.keys.prefix}:no-lease",
            ],
            [
                lease.worker_id if lease else "",
                str(lease.fencing_token) if lease else "",
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                time.time(),
            ],
        )
        if int(result) == -1:
            raise LostLeaseError(effect_key)
        return bool(int(result))

    def complete_effect(self, effect_key: str, lease: MrLease | None, result_value: Any) -> bool:
        task_id = effect_key.split(":", 1)[0]
        result = self._eval(
            COMPLETE_EFFECT_LUA,
            [
                self.keys.effect(effect_key),
                self.keys.mr_lease(lease.mr) if lease else f"{self.keys.prefix}:no-lease",
                self.keys.task(task_id),
            ],
            [
                lease.worker_id if lease else "",
                str(lease.fencing_token) if lease else "",
                json.dumps(result_value, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                time.time(),
                effect_key,
            ],
        )
        if int(result) == -1:
            raise LostLeaseError(effect_key)
        return bool(int(result))

    def enqueue_notification(self, notification: NotificationEnvelope) -> bool:
        result = self._eval(
            ENQUEUE_NOTIFICATION_LUA,
            [
                self.keys.notification_dedup(notification.notification_id),
                self.keys.notification_stream,
                self.keys.notification(notification.notification_id),
            ],
            [
                self.settings.dedup_ttl_seconds,
                notification.to_json(),
                notification.notification_id,
                notification.created_at,
            ],
        )
        queued = bool(int(result))
        if queued and notification.task_id:
            from pr_agent.distributed.lifecycle import LifecycleEvent

            self.record_lifecycle_event(
                LifecycleEvent.new(
                    notification.task_id,
                    "notification",
                    "start",
                    segment_id=notification.notification_id,
                )
            )
        return queued

    def claim_fixing_notice_sync(self, mr: MrKey, ttl_seconds: int) -> bool:
        return bool(self.redis.set(self.keys.fixing_notice(mr), "1", ex=ttl_seconds, nx=True))

    def is_mr_triage_active_sync(self, mr: MrKey) -> bool:
        task_ids = self.redis.smembers(self.keys.mr_tasks(mr))
        for task_id in task_ids:
            value = self.redis.hgetall(self.keys.task(str(task_id)))
            if not value:
                continue
            task = TaskEnvelope.from_json(value["payload"])
            command = task.command.split()[0].lower() if task.command else ""
            if (
                task.command
                and command in {"/triage", "/fix-format", "/fix_format", "/repair-pipeline"}
                and TaskStatus(value["status"])
                in {TaskStatus.ASSIGNED, TaskStatus.RUNNING, TaskStatus.WAITING_PIPELINE}
            ):
                return True
        return False
