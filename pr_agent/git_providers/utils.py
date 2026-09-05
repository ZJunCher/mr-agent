import copy
import os
import tempfile
import traceback

from dynaconf import Dynaconf
from starlette_context import context

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.log import get_logger


def apply_repo_settings(pr_url):
    os.environ["AUTO_CAST_FOR_DYNACONF"] = "false"
    git_provider = get_git_provider_with_context(pr_url)
    if get_settings().config.use_repo_settings_file:
        repo_settings_file = None
        try:
            try:
                repo_settings_by_pr = context.get("repo_settings_by_pr", None)
            except Exception:
                repo_settings_by_pr = None
                pass
            if not isinstance(repo_settings_by_pr, dict):
                repo_settings_by_pr = {}
            repo_settings = repo_settings_by_pr.get(pr_url, None)
            if repo_settings is None:  # None is different from "", which is a valid value
                repo_settings = git_provider.get_repo_settings()
                try:
                    repo_settings_by_pr[pr_url] = repo_settings
                    context["repo_settings_by_pr"] = repo_settings_by_pr
                except Exception:
                    pass

            error_local = None
            if repo_settings:
                repo_settings_file = None
                category = 'local'
                try:
                    fd, repo_settings_file = tempfile.mkstemp(suffix='.toml')
                    os.write(fd, repo_settings)

                    try:
                        dynconf_kwargs = {'core_loaders': [],  # DISABLE default loaders, otherwise will load toml files more than once.
                             'loaders': ['pr_agent.custom_merge_loader'],
                             # Use a custom loader to merge sections, but overwrite their overlapping values. Don't involve ENV variables.
                             'merge_enabled': True  # Merge multiple files; ensures [XYZ] sections only overwrite overlapping keys, not whole sections.
                         }

                        new_settings = Dynaconf(settings_files=[repo_settings_file],
                                                # Disable all dynamic loading features
                                                load_dotenv=False,  # Don't load .env files
                                                envvar_prefix=False,  # Drop DYNACONF for env. variables
                                                **dynconf_kwargs
                                                )
                    except TypeError as e:
                        # Fallback for older Dynaconf versions that don't support these parameters
                        get_logger().warning(
                            "Your Dynaconf version does not support disabled 'load_dotenv'/'merge_enabled' parameters. "
                            "Loading repo settings without these security features. "
                            "Please upgrade Dynaconf for better security.",
                            artifact={"error": e, "traceback": traceback.format_exc()})
                        new_settings = Dynaconf(settings_files=[repo_settings_file])

                    prompt_overrides = set()
                    try:
                        current_settings_dict = get_settings().as_dict()
                        for section, contents in new_settings.as_dict().items():
                            if not isinstance(contents, dict):
                                continue
                            if "prompt" not in str(section).lower():
                                continue
                            old_section = current_settings_dict.get(section, {})
                            if not isinstance(old_section, dict):
                                old_section = {}
                            for k in ("system", "user", "prompt"):
                                if k not in contents:
                                    continue
                                new_v = contents.get(k)
                                old_v = old_section.get(k)
                                if str(new_v).strip() and str(new_v).strip() != str(old_v).strip():
                                    prompt_overrides.add(f"{section}.{k}")
                    except Exception:
                        prompt_overrides = set()

                    for section, contents in new_settings.as_dict().items():
                        if not contents:
                            # Skip excluded items, such as forbidden to load env.
                            get_logger().debug(f"Skipping a section: {section} which is not allowed")
                            continue
                        section_dict = copy.deepcopy(get_settings().as_dict().get(section, {}))
                        for key, value in contents.items():
                            section_dict[key] = value
                        get_settings().unset(section)
                        get_settings().set(section, section_dict, merge=False)
                    get_logger().info(f"Applying repo settings:\n{new_settings.as_dict()}")
                    if prompt_overrides:
                        msg = "使用自定义prompt：" + ", ".join(sorted(prompt_overrides))
                        get_logger().opt(colors=True).info(f"<yellow>{msg}</yellow>")
                except Exception as e:
                    get_logger().warning(f"Failed to apply repo {category} settings, error: {str(e)}")
                    error_local = {'error': str(e), 'settings': repo_settings, 'category': category}

                if error_local:
                    handle_configurations_errors([error_local], git_provider)
        except Exception as e:
            get_logger().exception("Failed to apply repo settings", e)
        finally:
            if repo_settings_file:
                try:
                    os.remove(repo_settings_file)
                except Exception as e:
                    get_logger().error(f"Failed to remove temporary settings file {repo_settings_file}", e)

    # enable switching models with a short definition
    if get_settings().config.model.lower() == 'claude-3-5-sonnet':
        set_claude_model()


def handle_configurations_errors(config_errors, git_provider):
    try:
        if not any(config_errors):
            return

        for err in config_errors:
            if err:
                configuration_file_content = err['settings'].decode()
                err_message = err['error']
                config_type = err['category']
                header = f"❌ **PR-Agent failed to apply '{config_type}' repo settings**"
                body = f"{header}\n\nThe configuration file needs to be a valid [TOML](https://qodo-merge-docs.qodo.ai/usage-guide/configuration_options/), please fix it.\n\n"
                body += f"___\n\n**Error message:**\n`{err_message}`\n\n"
                if git_provider.is_supported("gfm_markdown"):
                    body += f"\n\n<details><summary>Configuration content:</summary>\n\n```toml\n{configuration_file_content}\n```\n\n</details>"
                else:
                    body += f"\n\n**Configuration content:**\n\n```toml\n{configuration_file_content}\n```\n\n"
                get_logger().warning(f"Sending a 'configuration error' comment to the PR", artifact={'body': body})
                # git_provider.publish_comment(body)
                if hasattr(git_provider, 'publish_persistent_comment'):
                    git_provider.publish_persistent_comment(body,
                                                            initial_header=header,
                                                            update_header=False,
                                                            final_update_message=False)
                else:
                    git_provider.publish_comment(body)
    except Exception as e:
        get_logger().exception(f"Failed to handle configurations errors", e)


def set_claude_model():
    """
    set the claude-sonnet-3.5 model easily (even by users), just by stating: --config.model='claude-3-5-sonnet'
    """
    model_claude = "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0"
    get_settings().set('config.model', model_claude)
    get_settings().set('config.model_weak', model_claude)
    get_settings().set('config.fallback_models', [model_claude])
