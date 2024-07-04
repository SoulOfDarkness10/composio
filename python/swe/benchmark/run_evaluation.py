# pylint: disable=logging-fstring-interpolation
import argparse
import datetime
import logging

from composio_swe.config.constants import KEY_API_KEY
from composio_swe.config.context import Context, set_context
from composio_swe.config.store import IssueConfig
from datasets import load_dataset
from rich.logging import RichHandler

from composio import Action, Composio
from composio.tools.env.factory import ExecEnv, WorkspaceFactory
from examples.crewai_agent import CrewaiAgent, SWEArgs


# get logger
LOGGER_NAME = "local_workspace"

handler = RichHandler(show_time=False, show_path=False)
handler.setLevel(logging.DEBUG)
logger = logging.getLogger(LOGGER_NAME)
logger.setLevel(logging.DEBUG)
logger.addHandler(handler)
logger.propagate = False


# princeton swe bench lite dataset has these fields
# instance_id: (str) - A formatted instance identifier, usually as repo_owner__repo_name-PR-number.
# patch: (str) - The gold patch, the patch generated by the PR (minus test-related code), that resolved the issue.
# repo: (str) - The repository owner/name identifier from GitHub.
# base_commit: (str) - The commit hash of the repository representing the HEAD of the repository before the solution PR is applied.
# hints_text: (str) - Comments made on the issue prior to the creation of the solution PR's first commit creation date.
# created_at: (str) - The creation date of the pull request.
# test_patch: (str) - A test-file patch that was contributed by the solution PR.
# problem_statement: (str) - The issue title and body.
# version: (str) - Installation version to use for running evaluation.
# environment_setup_commit: (str) - commit hash to use for environment setup and installation.
# FAIL_TO_PASS: (str) - A json list of strings that represent the set of tests resolved by the PR and tied to the issue resolution.
# PASS_TO_PASS: (str) - A json list of strings that represent tests that should pass before and after the PR application.


def filter_from_repo_name(curr_dataset, repo_name):
    filtered_dataset = curr_dataset.filter(
        lambda x: x["repo"] == repo_name.strip().lower()
    )
    return filtered_dataset


def get_issues_dataset(test_split):
    test_dataset = load_dataset(
        "princeton-nlp/SWE-bench_Lite",
        split=f"test[{test_split}]",
    )
    return test_dataset


def build_issue_description(hints, problem_statement, include_hints):
    if not problem_statement or not problem_statement.strip():
        raise ValueError("problem statement is empty")
    tmpl = f"""Here is the issue, that you have to solve all on your own:\n{problem_statement}"""
    if include_hints and hints:
        tmpl += f"""\n\nHere are few hints to solve the issue described in problem_statement: \n{hints}"""

    return tmpl


def get_workspace_from_repo_map(
    composio_client, repo, repo_to_workspace_map, base_commit
):
    workspace_id = repo_to_workspace_map.get(repo)
    if not workspace_id or not workspace_id.strip():
        return None
    print("Resetting repository to base commit")
    workspace_id = repo_to_workspace_map[repo]
    composio_client.actions.execute(
        action=Action.GITCMDTOOL_GITHUB_CLONE_CMD,
        params={
            "workspace_id": workspace_id,
            "repo_name": repo,
            "just_reset": True,
            "commit_id": base_commit,
        },
    )
    return workspace_id


def create_workspace_from_image(
    composio_client, repo, repo_to_image_id_map, base_commit
):
    if not repo_to_image_id_map.get(repo):
        logger.info("repo: %s not found in repo-to-image-map", repo)
        return ""
    logger.info("Using saved image")
    start_time = datetime.datetime.now()
    workspace_id = WorkspaceFactory.get_instance().create_workspace(
        workspace_type=ExecutionEnvironment.DOCKER,
        local_docker_args=LocalDockerArgumentsModel(
            image_name=repo_to_image_id_map[repo]
        ),
    )
    workspace_creation_time = datetime.datetime.now() - start_time
    logger.info(
        "workspace is created, workspace-id is: %s, creation time: %s",
        workspace_id,
        workspace_creation_time,
    )
    logger.info("Resetting repository to base commit")
    composio_client.actions.execute(
        action=Action.CMDMANAGERTOOL_GITHUBCLONECMD,
        params={
            "workspace_id": workspace_id,
            "repo_name": repo,
            "just_reset": True,
            "commit_id": base_commit,
        },
    )
    return workspace_id


def build_image_and_container(
    composio_client, repo, repo_to_workspace_map, base_commit
):
    logger.info("Falling back to creating new workspace.")
    start_time = datetime.datetime.now()
    workspace_id = WorkspaceFactory.get_instance().create_workspace(
        workspace_type=ExecutionEnvironment.DOCKER,
        local_docker_args=LocalDockerArgumentsModel(image_name="sweagent/swe-agent"),
    )
    workspace_creation_time = datetime.datetime.now() - start_time
    logger.info(
        "workspace is created, workspace-id is: %s, creation time: %s",
        workspace_id,
        workspace_creation_time,
    )

    start_time = datetime.datetime.now()
    composio_client.actions.execute(
        entity_id="123",
        action=Action.CMDMANAGERTOOL_GITHUBCLONECMD,
        params={
            "workspace_id": workspace_id,
            "repo_name": repo,
            "commit_id": base_commit,
        },
    )
    git_clone_time = datetime.datetime.now() - start_time
    logger.info("git clone completed, time taken: %s", git_clone_time)
    repo_to_workspace_map[repo] = workspace_id
    return workspace_id


def setup_workspace(repo, repo_to_workspace_map, repo_to_image_id_map, base_commit):
    composio_client = Composio()
    workspace_id = get_workspace_from_repo_map(
        composio_client, repo, repo_to_workspace_map, base_commit
    )
    if workspace_id:
        return workspace_id
    workspace_id = create_workspace_from_image(
        composio_client, repo, repo_to_image_id_map, base_commit
    )
    if workspace_id:
        return workspace_id
    return build_image_and_container(
        composio_client, repo, repo_to_workspace_map, base_commit
    )


def run(test_split, print_only=False, include_hints=True):
    """
    Main function to load and display entries from the SWE-bench lite dataset.
    """

    issues = get_issues_dataset(test_split)

    repo_to_workspace_map = {}
    repo_to_image_id_map = {}
    for count, issue in enumerate(issues, 1):
        try:
            repo = issue["repo"]
            print(f"Processing {count}th issue with repoMap: {repo_to_workspace_map}")
            print(f"Repo: {repo}")
            print(f"Issue id: {issue['instance_id']}")
            print(f"Issue description: {issue['problem_statement']}")

            if print_only:
                if include_hints:
                    print(f"Hints: {issue['hints_text']}")
                print("--------------------------------------------------")
                continue

            workspace_id = setup_workspace(
                repo, repo_to_workspace_map, repo_to_image_id_map, issue["base_commit"]
            )

            issue_description = build_issue_description(
                issue["hints_text"], issue["problem_statement"], include_hints
            )
            print(f"Issue description: {issue_description}")
            patch = issue["patch"]
            install_commit_id = issue["environment_setup_commit"]
            logger.info(
                "found patch-id: %s and install_commit_id: %s", patch, install_commit_id
            )
            issue_config = IssueConfig(
                repo_name=issue["repo"],
                issue_id=issue["instance_id"],
                base_commit_id=issue["base_commit"],
                issue_desc=issue_description,
            )
            logger.info(
                f"starting agent for issue-id: {issue['instance_id']}\n"
                f"issue-description: {issue_description}\n"
                f"repo_name: {issue['repo']}\n"
            )

            print("--------------------------------------------------")

            model_env_config = {
                KEY_API_KEY: "test-key",
                "azure_endpoint": "test-endpoint",
                "model_env": "azure",
            }
            ctx = Context()
            ctx.issue_config = issue_config
            ctx.model_env = model_env_config
            set_context(ctx)

            args = SWEArgs(agent_logs_dir=ctx.agent_logs_dir)
            coder = CrewaiAgent(args)
            coder.setup_and_solve(
                issue_config=ctx.issue_config, workspace_id=workspace_id
            )
        except Exception as e:
            print(f"Error processing issue {issue['instance_id']}: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run SWE-bench evaluation")
    parser.add_argument(
        "--test_split",
        type=str,
        default="1:10",
        help="Test split range (e.g., 1:10)",
    )
    parser.add_argument(
        "--print_only",
        action="store_true",
        help="Just print the issues without running an agent",
    )
    parser.add_argument(
        "--include_hints",
        action="store_true",
        help="Include hints in the issue description",
    )
    args = parser.parse_args()

    print("Starting evaluation")
    run(args.test_split, args.print_only, args.include_hints)
