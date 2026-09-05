"""
诊断脚本：直接查询某个 pipeline 的 job 列表、bridges、状态，
用来定位为什么 fetch_pipeline_feedback 返回 failed_jobs=[]。

用法（在 pr-agent 仓库根目录下）:
    # 用配置文件里的 GITLAB.URL / GITLAB.PERSONAL_ACCESS_TOKEN
    PYTHONPATH=. python ut_agent/test/diag_pipeline.py <project_id_or_path> <pipeline_id>

    # 或直接用环境变量，不依赖 pr-agent 配置:
    GITLAB_URL=https://gitlab.example.com GITLAB_TOKEN=xxx \
        PYTHONPATH=. python ut_agent/test/diag_pipeline.py <project_id_or_path> <pipeline_id>

示例:
    PYTHONPATH=. python ut_agent/test/diag_pipeline.py group/repo 12992
"""
import os
import sys


def _get_gl():
    url = os.environ.get("GITLAB_URL")
    token = os.environ.get("GITLAB_TOKEN")
    if url and token:
        import gitlab
        print(f"[diag] 使用环境变量 GITLAB_URL={url}")
        return gitlab.Gitlab(url=url, private_token=token, ssl_verify=False)

    # 退回到 pr-agent 配置
    print("[diag] 未提供 GITLAB_URL/GITLAB_TOKEN，尝试从 pr-agent 配置读取...")
    from pr_agent.config_loader import get_settings
    import gitlab
    gitlab_url = get_settings().get("GITLAB.URL", None)
    gitlab_token = get_settings().get("GITLAB.PERSONAL_ACCESS_TOKEN", None)
    ssl_verify = get_settings().get("GITLAB.SSL_VERIFY", True)
    if not gitlab_url or not gitlab_token:
        raise RuntimeError(
            "无法获取 GitLab 连接信息：请设置 GITLAB_URL / GITLAB_TOKEN 环境变量，"
            "或确保 pr-agent 配置中 GITLAB.URL 和 GITLAB.PERSONAL_ACCESS_TOKEN 存在。"
        )
    print(f"[diag] 使用 pr-agent 配置 GITLAB.URL={gitlab_url}")
    return gitlab.Gitlab(url=gitlab_url, private_token=gitlab_token, ssl_verify=ssl_verify)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    project_ref = sys.argv[1]
    pipeline_id = int(sys.argv[2])

    gl = _get_gl()
    project = gl.projects.get(project_ref)
    print(f"[diag] 项目: id={project.id} path={project.path_with_namespace}")

    pipeline = project.pipelines.get(pipeline_id)
    print("\n=== Pipeline 基本信息 ===")
    print(f"  id           : {pipeline.id}")
    print(f"  iid          : {getattr(pipeline, 'iid', None)}")
    print(f"  status       : {pipeline.status}")
    print(f"  ref          : {pipeline.ref}")
    print(f"  sha          : {pipeline.sha}")
    print(f"  source       : {getattr(pipeline, 'source', None)}")
    print(f"  coverage     : {getattr(pipeline, 'coverage', None)}")
    print(f"  web_url      : {pipeline.web_url}")

    # 尝试列出该 pipeline 的所有 job
    print("\n=== pipeline.jobs.list(include_retried=True, get_all=True) ===")
    try:
        jobs = pipeline.jobs.list(include_retried=True, get_all=True, per_page=100)
    except TypeError:
        # 旧版 python-gitlab 不支持 include_retried
        jobs = pipeline.jobs.list(get_all=True, per_page=100)
    print(f"  jobs 数量: {len(jobs)}")
    for j in jobs:
        print(f"    - #{j.id} name={j.name!r} status={j.status} stage={getattr(j, 'stage', '?')}")

    # 列出 bridges（子 pipeline 触发器）
    print("\n=== pipeline.bridges.list(get_all=True) ===")
    try:
        bridges = pipeline.bridges.list(get_all=True, per_page=100)
        print(f"  bridges 数量: {len(bridges)}")
        for b in bridges:
            ds = getattr(b, "downstream_pipeline", None)
            print(f"    - #{b.id} name={b.name!r} status={b.status} downstream={ds}")
    except Exception as e:
        print(f"  bridges 查询失败: {e}")

    # 根据 SHA 反查 pipelines（模拟 fetch_pipeline 的查找逻辑）
    print(f"\n=== project.pipelines.list(sha={pipeline.sha!r}) ===")
    found = project.pipelines.list(sha=pipeline.sha, order_by="id", sort="desc", per_page=20, get_all=False)
    print(f"  同 SHA 的 pipeline 数量: {len(found)}")
    for p in found:
        print(f"    - #{p.id} status={p.status} ref={p.ref} source={getattr(p, 'source', None)}")

    print("\n[diag] 完成。")


if __name__ == "__main__":
    main()
