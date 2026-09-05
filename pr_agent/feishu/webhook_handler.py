import asyncio
import gitlab
from pr_agent.feishu.feishu_client import FeishuClient
from pr_agent.log import get_logger

async def handle_gitlab_webhook_event(data, settings):
    """
    Handle GitLab webhook events and send notifications to Feishu.
    """
    try:
        object_kind = data.get('object_kind')
        
        # Only care about MR events and comments
        if object_kind not in ['merge_request', 'note']:
            return

        client = FeishuClient()
        if not client.app_id or not client.app_secret:
            # Silent return if not configured
            return

        sender = data.get("user", {}).get("username", "unknown")
        
        # 1) 获取 MR 基本信息
        project_id = data.get('project', {}).get('id')
        mr_iid = None
        if 'merge_request' in data:
            mr_iid = data['merge_request'].get('iid')
        elif 'object_attributes' in data and data.get('object_kind') == 'merge_request':
            mr_iid = data['object_attributes'].get('iid')

        if not project_id or not mr_iid:
            get_logger().warning("Could not determine project_id or mr_iid")
            return

        # 2) 获取 MR 作者
        mr_author_username = await get_author_from_gitlab(data, settings)
        if not mr_author_username:
            get_logger().warning("Could not determine MR author")
            return

        # 3) 准备评论内容
        comment_body = None
        if object_kind == 'note':
            if data.get('event_type') != 'note':
                return
            comment_body = data.get('object_attributes', {}).get('note', '')
        elif object_kind == 'merge_request':
            attr = data.get('object_attributes', {})
            action = attr.get('action')
            if action not in ['open', 'reopen', 'merge', 'close']:
                return
            # MR 事件去 API 拿最新一条非系统评论
            comment_body = await fetch_latest_mr_comment(project_id, mr_iid, settings)

        if not comment_body:
            get_logger().info("No comment body found, skip Feishu notification")
            return

        # 4) 确定接收者
        recipient = None
        # 如果是 Bot 发的评论（PR-Agent 的回复）
        if _is_bot_sender(sender):
            # 过滤掉简单的状态消息（如 "Reviewing..." 或 "Preparing..."）
            if len(comment_body) < 20 and "..." in comment_body:
                get_logger().info(f"Skipping status message from bot: {comment_body}")
                return
            
            # 尝试找到触发该指令的人，如果找不到则发给 MR 作者
            command_sender = await fetch_command_sender(project_id, mr_iid, settings)
            if command_sender:
                get_logger().info(f"Bot response triggered by {command_sender}, sending to them")
                recipient = command_sender
            else:
                get_logger().warning(f"Could not find command sender, falling back to MR author: {mr_author_username}")
                recipient = mr_author_username
        else:
            # 如果是人类发的评论
            # 过滤掉指令（以 / 开头），避免自己发的指令弹回来
            if comment_body.strip().startswith('/'):
                get_logger().info(f"Skip command comment from {sender}: {comment_body}")
                return
            # 普通讨论评论，发给 MR 作者
            recipient = mr_author_username

        if not recipient:
            mr_url = None
            if 'merge_request' in data:
                mr_url = data['merge_request'].get('url')
            elif 'object_attributes' in data and data.get('object_kind') == 'merge_request':
                mr_url = data['object_attributes'].get('url')

            # Check if there is a pending request from Feishu
            # We use the MR URL to match
            # Need to normalize URL to match what we stored
            if mr_url and mr_url in pending_feishu_requests:
                recipient = pending_feishu_requests[mr_url]
                get_logger().info(f"Found pending Feishu request for {mr_url}, sending to {recipient}")
                # Optional: Remove from pending requests?
                # If we remove it, subsequent updates won't be sent.
                # If we keep it, updates will be sent (which might be good).
                # But we should probably have a TTL or cleanup mechanism.
                # For now, let's keep it to allow multi-turn conversation or updates.
            else:
                get_logger().warning(f"Could not determine recipient for {mr_url}")
                return

        # 如果接收者是机器人，跳过通知
        if _is_bot_sender(recipient):
            get_logger().info(f"Recipient is a bot, skip notification: {recipient}")
            return

        # 5) 发送给飞书
        get_logger().info(f"Sending Feishu markdown to recipient: {recipient}")
        await client.send_markdown_to_user(recipient, comment_body)
        
    except Exception as e:
        get_logger().error(f"Error handling Feishu webhook event: {e}")

# 内部机器人账号关键词，用于识别机器人评论
BOT_INDICATORS = {'codium', 'bot_', 'bot-', '_bot', '-bot', 'eabot_devops'}

# 存储飞书端发起的待处理请求
# Key: MR URL (normalized), Value: Feishu Open ID
pending_feishu_requests = {}

def _is_bot_sender(sender):
    if not sender:
        return False
    sender = sender.lower()
    return any(indicator in sender for indicator in BOT_INDICATORS)

async def fetch_command_sender(project_id, mr_iid, settings):
    """回溯 MR 评论，找到最近的一条指令及其发布者"""
    try:
        url = settings.get("GITLAB.URL", "https://gitlab.com")
        token = settings.gitlab.personal_access_token
        if not token:
            return None
        gl = gitlab.Gitlab(url, private_token=token)
        project = gl.projects.get(project_id)
        # 获取最近 100 条评论，寻找指令。
        # 即使 /improve 产生了大量 suggestion，100 条通常也能覆盖到原始指令。
        notes = project.mergerequests.get(mr_iid).notes.list(
            sort="desc", order_by="created_at", per_page=100
        )
        get_logger().info(f"Scanning {len(notes)} notes for command sender...")
        for note in notes:
            if note.system:
                continue
            body = (note.body or "").strip()
            # 找到最近的一条以 / 开头的指令
            if body.startswith('/'):
                author = note.author.get('username')
                if author and not _is_bot_sender(author):
                    get_logger().info(f"Found command sender: {author} (command: {body[:20]}...)")
                    return author
        get_logger().warning("Command sender not found in recent notes")
    except Exception as e:
        get_logger().error(f"Failed to fetch command sender: {e}")
    return None

async def fetch_latest_mr_comment(project_id, mr_iid, settings):
    """获取 MR 下最近一条非系统评论的 body"""
    try:
        url = settings.get("GITLAB.URL", "https://gitlab.com")
        token = settings.gitlab.personal_access_token
        if not token:
            return None
        gl = gitlab.Gitlab(url, private_token=token)
        project = gl.projects.get(project_id)
        notes = project.mergerequests.get(mr_iid).notes.list(
            sort="desc", order_by="created_at", per_page=1
        )
        for note in notes:
            if not note.system:  # 过滤系统消息
                return note.body
    except Exception as e:
        get_logger().error(f"Failed to fetch latest MR comment: {e}")
    return None

async def get_author_from_gitlab(data, settings):
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _get_author_sync, data, settings)
    except Exception as e:
        get_logger().error(f"Failed to get MR author from GitLab: {e}")
        return None

def _get_author_sync(data, settings):
    try:
        url = settings.get("GITLAB.URL", "https://gitlab.com")
        token = settings.gitlab.personal_access_token
        
        # We need a token to query API
        if not token:
            get_logger().warning("No GitLab token available for API query")
            return None

        gl = gitlab.Gitlab(url, private_token=token)
        
        project_id = data.get('project', {}).get('id')
        
        iid = None
        if 'merge_request' in data:
            iid = data['merge_request'].get('iid')
        elif 'object_attributes' in data and data.get('object_kind') == 'merge_request':
            iid = data['object_attributes'].get('iid')
            
        if project_id and iid:
            project = gl.projects.get(project_id)
            mr = project.mergerequests.get(iid)
            # mr.author is a dict
            return mr.author['username']
    except Exception as e:
        get_logger().error(f"Error in _get_author_sync: {e}")
    return None
