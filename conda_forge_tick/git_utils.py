"""Utilities for managing github repos"""

import copy
import datetime
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Dict, Optional, Tuple, Union

import backoff
import github
import github3
import github3.exceptions
import github3.pulls
import github3.repos
import requests
from doctr.travis import run_command_hiding_token as doctr_run
from requests.exceptions import RequestException, Timeout

from conda_forge_tick import sensitive_env

# TODO: handle the URLs more elegantly (most likely make this a true library
# and pull all the needed info from the various source classes)
from conda_forge_tick.lazy_json_backends import LazyJson

from .contexts import FeedstockContext
from .os_utils import pushd
from .utils import get_bot_run_url

logger = logging.getLogger(__name__)

backoff._decorator._is_event_loop = lambda: False

GITHUB3_CLIENT = threading.local()
GITHUB_CLIENT = threading.local()

MAX_GITHUB_TIMEOUT = 60

GIT_CLONE_DIR = "./feedstocks/"

BOT_RERUN_LABEL = {
    "name": "bot-rerun",
}

CF_BOT_NAMES = {"regro-cf-autotick-bot", "conda-forge-linter"}

# these keys are kept from github PR json blobs
# to add more keys to keep, put them in the right spot in the dict and
# set them to None. Also add them to the PullRequestInfo Pydantic model!
PR_KEYS_TO_KEEP = {
    "ETag": None,
    "Last-Modified": None,
    "id": None,
    "number": None,
    "html_url": None,
    "created_at": None,
    "updated_at": None,
    "merged_at": None,
    "closed_at": None,
    "state": None,
    "mergeable_state": None,
    "labels": None,
    "merged": None,
    "draft": None,
    "mergeable": None,
    "head": {"ref": None},
    "base": {"repo": {"name": None}},
}


def github3_client() -> github3.GitHub:
    if not hasattr(GITHUB3_CLIENT, "client"):
        with sensitive_env() as env:
            GITHUB3_CLIENT.client = github3.login(token=env["BOT_TOKEN"])
    return GITHUB3_CLIENT.client


def github_client() -> github.Github:
    if not hasattr(GITHUB_CLIENT, "client"):
        with sensitive_env() as env:
            GITHUB_CLIENT.client = github.Github(
                auth=github.Auth.Token(env["BOT_TOKEN"])
            )
    return GITHUB_CLIENT.client


def get_default_branch(feedstock_name):
    """Get the default branch for a feedstock

    Parameters
    ----------
    feedstock_name : str
        The feedstock without '-feedstock'.

    Returns
    -------
    branch : str
        The default branch (e.g., 'main').
    """
    return (
        github_client()
        .get_repo(f"conda-forge/{feedstock_name}-feedstock")
        .default_branch
    )


def get_github_api_requests_left() -> Union[int, None]:
    """Get the number of remaining GitHub API requests.

    Returns
    -------
    left : int or None
        The number of remaining requests, or None if there is an exception.
    """
    gh = github3_client()
    try:
        left = gh.rate_limit()["resources"]["core"]["remaining"]
    except Exception:
        left = None

    return left


def is_github_api_limit_reached(
    e: Union[github3.GitHubError, github.GithubException],
) -> bool:
    """Prints diagnostic information about a github exception.

    Parameters
    ----------
    e
        The exception to check.

    Returns
    -------
    out_of_api_calls
        A flag to indicate that the api call limit has been reached.
    """
    gh = github3_client()

    logger.warning("GitHub API error:", exc_info=e)

    try:
        c = gh.rate_limit()["resources"]["core"]
    except Exception:
        # if we can't connect to the rate limit API, let's assume it has been reached
        return True

    if c["remaining"] == 0:
        ts = c["reset"]
        logger.warning(
            "GitHub API timeout, API returns at %s",
            datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        return True

    return False


def feedstock_url(fctx: FeedstockContext, protocol: str = "ssh") -> str:
    """Returns the URL for a conda-forge feedstock."""
    feedstock = fctx.feedstock_name + "-feedstock"
    if feedstock.startswith("http://github.com/"):
        return feedstock
    elif feedstock.startswith("https://github.com/"):
        return feedstock
    elif feedstock.startswith("git@github.com:"):
        return feedstock
    protocol = protocol.lower()
    if protocol == "http":
        url = "http://github.com/conda-forge/" + feedstock + ".git"
    elif protocol == "https":
        url = "https://github.com/conda-forge/" + feedstock + ".git"
    elif protocol == "ssh":
        url = "git@github.com:conda-forge/" + feedstock + ".git"
    else:
        msg = "Unrecognized github protocol {0!r}, must be ssh, http, or https."
        raise ValueError(msg.format(protocol))
    return url


def feedstock_repo(fctx: FeedstockContext) -> str:
    """Gets the name of the feedstock repository."""
    repo = fctx.feedstock_name + "-feedstock"
    if repo.endswith(".git"):
        repo = repo[:-4]
    return repo


def fork_url(feedstock_url: str, username: str) -> str:
    """Creates the URL of the user's fork."""
    beg, end = feedstock_url.rsplit("/", 1)
    beg = beg[:-11]  # chop off 'conda-forge'
    url = beg + username + "/" + end
    return url


def fetch_repo(*, feedstock_dir, origin, upstream, branch, base_branch="main"):
    """fetch a repo and make a PR branch

    Parameters
    ----------
    feedstock_dir : str
        The directory where you want to clone the feedstock.
    origin : str
        The origin to clone from.
    upstream : str
        The upstream repo to add as a remote named `upstream`.
    branch : str
        The branch to make and checkout.
    base_branch : str, optional
        The branch from which to branch from to make `branch`. Defaults to "main".

    Returns
    -------
    success : bool
        True if the fetch worked, False otherwise.
    """
    if not os.path.isdir(feedstock_dir):
        p = subprocess.run(
            ["git", "clone", "-q", origin, feedstock_dir],
        )
        if p.returncode != 0:
            msg = "Could not clone " + origin
            msg += ". Do you have a personal fork of the feedstock?"
            print(msg, file=sys.stderr)
            return False
        reset_hard = False
    else:
        reset_hard = True

    def _run_git_cmd(cmd, **kwargs):
        return subprocess.run(["git"] + cmd, check=True, **kwargs)

    quiet = "--quiet"
    with pushd(feedstock_dir):
        if reset_hard:
            _run_git_cmd(["reset", "--hard", "HEAD"])

        # doesn't work if the upstream already exists
        try:
            # always run upstream
            _run_git_cmd(["remote", "add", "upstream", upstream])
        except subprocess.CalledProcessError:
            pass

        # fetch remote changes
        _run_git_cmd(["fetch", "--all", quiet])
        if _run_git_cmd(
            ["branch", "--list", base_branch],
            capture_output=True,
        ).stdout:
            _run_git_cmd(["checkout", base_branch, quiet])
        else:
            try:
                _run_git_cmd(["checkout", "--track", f"upstream/{base_branch}", quiet])
            except subprocess.CalledProcessError:
                _run_git_cmd(
                    ["checkout", "-b", base_branch, f"upstream/{base_branch}", quiet],
                )
        _run_git_cmd(["reset", "--hard", f"upstream/{base_branch}", quiet])

        # make and modify version branch
        try:
            _run_git_cmd(["checkout", branch, quiet])
        except subprocess.CalledProcessError:
            _run_git_cmd(["checkout", "-b", branch, base_branch, quiet])

    return True


def get_repo(
    fctx: FeedstockContext,
    branch: str,
    feedstock: Optional[str] = None,
    protocol: str = "ssh",
    pull_request: bool = True,
    fork: bool = True,
    base_branch: str = "main",
) -> Tuple[str, github3.repos.Repository]:
    """Get the feedstock repo

    Parameters
    ----------
    fcts : FeedstockContext
        Feedstock context used for constructing feedstock urls, etc.
    branch : str
        The branch to be made.
    feedstock : str, optional
        The feedstock to clone if None use $FEEDSTOCK
    protocol : str, optional
        The git protocol to use, defaults to ``ssh``
    pull_request : bool, optional
        If true issue pull request, defaults to true
    fork : bool
        If true create a fork, defaults to true
    base_branch : str, optional
        The base branch from which to make the new branch.

    Returns
    -------
    recipe_dir : str
        The recipe directory
    repo : github3 repository
        The github3 repository object.
    """
    gh = github3_client()
    gh_username = gh.me().login

    # first, let's grab the feedstock locally
    upstream = feedstock_url(fctx=fctx, protocol=protocol)
    origin = fork_url(upstream, gh_username)
    feedstock_reponame = feedstock_repo(fctx=fctx)

    if pull_request or fork:
        repo = gh.repository("conda-forge", feedstock_reponame)
        if repo is None:
            print("could not fork conda-forge/%s!" % feedstock_reponame, flush=True)
            with fctx.attrs["pr_info"] as pri:
                pri["bad"] = f"{fctx.package_name}: does not match feedstock name\n"
            return False, False

    # Check if fork exists
    if fork:
        try:
            fork_repo = gh.repository(gh_username, feedstock_reponame)
        except github3.GitHubError:
            fork_repo = None
        if fork_repo is None or (hasattr(fork_repo, "is_null") and fork_repo.is_null()):
            print("Fork doesn't exist creating feedstock fork...")
            repo.create_fork()
            # Sleep to make sure the fork is created before we go after it
            time.sleep(5)

        # sync the default branches if needed
        _sync_default_branches(feedstock_reponame)

    feedstock_dir = os.path.join(GIT_CLONE_DIR, fctx.package_name + "-feedstock")

    if fetch_repo(
        feedstock_dir=feedstock_dir,
        origin=origin,
        upstream=upstream,
        branch=branch,
        base_branch=base_branch,
    ):
        return feedstock_dir, repo
    else:
        return False, False


def _sync_default_branches(reponame):
    gh = github_client()
    forked_user = gh.get_user().login
    default_branch = gh.get_repo(f"conda-forge/{reponame}").default_branch
    forked_default_branch = gh.get_repo(f"{forked_user}/{reponame}").default_branch
    if default_branch != forked_default_branch:
        print("Fork's default branch doesn't match upstream, syncing...")
        forked_repo = gh.get_repo(f"{forked_user}/{reponame}")
        forked_repo.rename_branch(forked_default_branch, default_branch)

        # sleep to wait for branch name change
        time.sleep(5)


def delete_branch(pr_json: LazyJson, dry_run: bool = False) -> None:
    ref = pr_json["head"]["ref"]
    if dry_run:
        print(f"dry run: deleting ref {ref}")
        return
    name = pr_json["base"]["repo"]["name"]

    gh = github3_client()
    deploy_repo = gh.me().login + "/" + name

    with sensitive_env() as env:
        doctr_run(
            [
                "git",
                "push",
                f"https://{env['BOT_TOKEN']}@github.com/{deploy_repo}.git",
                "--delete",
                ref,
            ],
            token=env["BOT_TOKEN"].encode("utf-8"),
        )
    # Replace ref so we know not to try again
    pr_json["head"]["ref"] = "this_is_not_a_branch"


def trim_pr_json_keys(
    pr_json: Union[Dict, LazyJson],
    src_pr_json: Optional[Union[Dict, LazyJson]] = None,
) -> Union[Dict, LazyJson]:
    """Trim the set of keys in the PR json. The keys kept are defined by the global
    PR_KEYS_TO_KEEP.

    Parameters
    ----------
    pr_json : dict-like
        A dict-like object with the current PR information.
    src_pr_json : dict-like, optional
        If this object is sent, the values for the trimmed keys are taken
        from this object. Otherwise `pr_json` is used for the values.

    Returns
    -------
    pr_json : dict-like
        A dict-like object with the current PR information trimmed to the subset of
        keys.
    """

    # keep a subset of keys
    def _munge_dict(dest, src, keys):
        for k, v in keys.items():
            if k in src:
                if v is None:
                    dest[k] = src[k]
                else:
                    dest[k] = {}
                    _munge_dict(dest[k], src[k], v)

    if src_pr_json is None:
        src_pr_json = copy.deepcopy(dict(pr_json))

    pr_json.clear()
    _munge_dict(pr_json, src_pr_json, PR_KEYS_TO_KEEP)
    return pr_json


def lazy_update_pr_json(
    pr_json: Union[Dict, LazyJson], force: bool = False
) -> Union[Dict, LazyJson]:
    """Lazily update a GitHub PR.

    This function will use the ETag in the GitHub API to update PR information
    lazily. It sends the ETag to github properly and if nothing is changed on their
    end, it simply returns the PR. Otherwise the information is refershed.

    Parameters
    ----------
    pr_json : dict-like
        A dict-like object with the current PR information.
    force : bool, optional
        If True, forcibly update the PR json even if it is not out of date
        according to the ETag. Default is False.
    trim : bool, optional
        If True, trim the PR json keys to ones in the global PR_KEYS_TO_KEEP.
        Default is True.

    Returns
    -------
    pr_json : dict-like
        A dict-like object with the current PR information.
    """
    with sensitive_env() as env:
        hdrs = {
            "Authorization": f"token {env['BOT_TOKEN']}",
            "Accept": "application/vnd.github.v3+json",
        }
    if not force and "ETag" in pr_json:
        hdrs["If-None-Match"] = pr_json["ETag"]

    if "repo" not in pr_json["base"] or (
        "repo" in pr_json["base"] and "name" not in pr_json["base"]["repo"]
    ):
        # some pr json blobs never had this key so we backfill
        repo_name = pr_json["html_url"].split("/conda-forge/")[1]
        if repo_name[-1] == "/":
            repo_name = repo_name[:-1]
        if "repo" not in pr_json["base"]:
            pr_json["base"]["repo"] = {}
        pr_json["base"]["repo"]["name"] = repo_name

    if "/pull/" in pr_json["base"]["repo"]["name"]:
        pr_json["base"]["repo"]["name"] = pr_json["base"]["repo"]["name"].split(
            "/pull/",
        )[0]

    r = requests.get(
        "https://api.github.com/repos/conda-forge/"
        f"{pr_json['base']['repo']['name']}/pulls/{pr_json['number']}",
        headers=hdrs,
    )

    if r.status_code == 200:
        pr_json = trim_pr_json_keys(pr_json, src_pr_json=r.json())
        pr_json["ETag"] = r.headers["ETag"]
        pr_json["Last-Modified"] = r.headers["Last-Modified"]
    else:
        pr_json = trim_pr_json_keys(pr_json)

    return pr_json


@backoff.on_exception(
    backoff.expo,
    (RequestException, Timeout),
    max_time=MAX_GITHUB_TIMEOUT,
)
def refresh_pr(
    pr_json: LazyJson,
    dry_run: bool = False,
) -> Optional[dict]:
    if pr_json["state"] != "closed":
        if dry_run:
            print("dry run: refresh pr %s" % pr_json["id"])
            pr_dict = dict(pr_json)
        else:
            pr_json = lazy_update_pr_json(copy.deepcopy(pr_json))

            # if state passed from opened to merged or if it
            # closed for a day delete the branch
            if pr_json["state"] == "closed" and pr_json.get("merged_at", False):
                delete_branch(pr_json=pr_json, dry_run=dry_run)
            pr_dict = dict(pr_json)

        return pr_dict

    return None


def get_pr_obj_from_pr_json(
    pr_json: Union[Dict, LazyJson],
    gh: github3.GitHub,
) -> github3.pulls.PullRequest:
    """Produce a github3 pull request object from pr_json.

    Parameters
    ----------
    pr_json : dict-like
        A dict-like object with the current PR information.
    gh : github3 object
        The github3 object for interacting with the GitHub API.

    Returns
    -------
    pr_obj : github3.pulls.PullRequest
        The pull request object.
    """
    feedstock_reponame = pr_json["base"]["repo"]["name"]
    repo = gh.repository("conda-forge", feedstock_reponame)
    return repo.pull_request(pr_json["number"])


@backoff.on_exception(
    backoff.expo,
    (RequestException, Timeout),
    max_time=MAX_GITHUB_TIMEOUT,
)
def close_out_labels(
    pr_json: LazyJson,
    dry_run: bool = False,
) -> Optional[dict]:
    gh = github3_client()

    # run this twice so we always have the latest info (eg a thing was already closed)
    if pr_json["state"] != "closed" and "bot-rerun" in [
        lab["name"] for lab in pr_json.get("labels", [])
    ]:
        # update
        if dry_run:
            print("dry run: checking pr %s" % pr_json["id"])
        else:
            pr_json = lazy_update_pr_json(pr_json)

    if pr_json["state"] != "closed" and "bot-rerun" in [
        lab["name"] for lab in pr_json.get("labels", [])
    ]:
        if dry_run:
            print("dry run: comment and close pr %s" % pr_json["id"])
        else:
            pr_obj = get_pr_obj_from_pr_json(pr_json, gh)
            pr_obj.create_comment(
                "Due to the `bot-rerun` label I'm closing "
                "this PR. I will make another one as"
                f" appropriate. This message was generated by {get_bot_run_url()} - please use this URL for debugging.",
            )
            pr_obj.close()

            delete_branch(pr_json=pr_json, dry_run=dry_run)
            pr_json = lazy_update_pr_json(pr_json)

        return dict(pr_json)

    return None


def push_repo(
    fctx: FeedstockContext,
    feedstock_dir: str,
    body: str,
    repo: github3.repos.Repository,
    title: str,
    branch: str,
    base_branch: str = "main",
    head: Optional[str] = None,
    dry_run: bool = False,
) -> Union[dict, bool, None]:
    """Push a repo up to github

    Parameters
    ----------
    fcts : FeedstockContext
        Feedstock context used for constructing feedstock urls, etc.
    feedstock_dir : str
        The feedstock directory
    body : str
        The PR body.
    repo : github3.repos.Repository
        The feedstock repo as a github3 object.
    title : str
        The title of the PR.
    head : str, optional
        The github head for the PR in the form `username:branch`.
    branch : str
        The head branch of the PR.
    base_branch : str, optional
        The base branch or target branch of the PR.

    Returns
    -------
    pr_json: dict
        The dict representing the PR, can be used with `from_json`
        to create a PR instance.
    """
    with sensitive_env() as env, pushd(feedstock_dir):
        # Setup push from doctr
        # Copyright (c) 2016 Aaron Meurer, Gil Forsyth
        token = env["BOT_TOKEN"]
        gh_username = github3_client().me().login

        if head is None:
            head = gh_username + ":" + branch

        deploy_repo = gh_username + "/" + fctx.feedstock_name + "-feedstock"
        if dry_run:
            repo_url = f"https://github.com/{deploy_repo}.git"
            print(f"dry run: adding remote and pushing up branch for {repo_url}")
        else:
            ecode = doctr_run(
                [
                    "git",
                    "remote",
                    "add",
                    "regro_remote",
                    f"https://{token}@github.com/{deploy_repo}.git",
                ],
                token=token.encode("utf-8"),
            )
            if ecode != 0:
                print("Failed to add git remote!")
                return False

            ecode = doctr_run(
                ["git", "push", "--set-upstream", "regro_remote", branch],
                token=token.encode("utf-8"),
            )
            if ecode != 0:
                print("Failed to push to remote!")
                return False

    # lastly make a PR for the feedstock
    print("Creating conda-forge feedstock pull request...")
    if dry_run:
        print(f"dry run: create pr with title: {title}")
        return False
    else:
        pr = repo.create_pull(title, base_branch, head, body=body)
        if pr is None:
            print("Failed to create pull request!")
            return False
        else:
            print("Pull request created at " + pr.html_url)

    # Return a json object so we can remake the PR if needed
    pr_dict: dict = pr.as_dict()

    return trim_pr_json_keys(pr_dict)


def comment_on_pr(pr_json, comment, repo):
    """Make a comment on a PR.

    Parameters
    ----------
    pr_json : dict
        A dict-like json blob with the PR information
    comment : str
        The comment to make.
    repo : github3.repos.Repository
        The feedstock repo as a github3 object.
    """
    pr_obj = repo.pull_request(pr_json["number"])
    pr_obj.create_comment(comment)


@backoff.on_exception(
    backoff.expo,
    (RequestException, Timeout),
    max_time=MAX_GITHUB_TIMEOUT,
)
def ensure_label_exists(
    repo: github3.repos.Repository,
    label_dict: dict,
    dry_run: bool = False,
) -> None:
    if dry_run:
        print(f"dry run: ensure label exists {label_dict['name']}")
    try:
        repo.label(label_dict["name"])
    except github3.exceptions.NotFoundError:
        repo.create_label(**label_dict)


def label_pr(
    repo: github3.repos.Repository,
    pr_json: LazyJson,
    label_dict: dict,
    dry_run: bool = False,
) -> None:
    ensure_label_exists(repo, label_dict, dry_run)
    if dry_run:
        print(f"dry run: label pr {pr_json['number']} with {label_dict['name']}")
    else:
        iss = repo.issue(pr_json["number"])
        iss.add_labels(label_dict["name"])


def close_out_dirty_prs(
    pr_json: LazyJson,
    dry_run: bool = False,
) -> Optional[dict]:
    gh = github3_client()

    # run this twice so we always have the latest info (eg a thing was already closed)
    if pr_json["state"] != "closed" and pr_json["mergeable_state"] == "dirty":
        # update
        if dry_run:
            print("dry run: checking pr %s" % pr_json["id"])
        else:
            pr_json = lazy_update_pr_json(pr_json)

    if (
        pr_json["state"] != "closed"
        and pr_json["mergeable_state"] == "dirty"
        and not pr_json.get("draft", False)
    ):
        d = dict(pr_json)

        if dry_run:
            print("dry run: comment and close pr %s" % pr_json["id"])
        else:
            pr_obj = get_pr_obj_from_pr_json(pr_json, gh)

            if all(
                c.as_dict()["commit"]["author"]["name"] in CF_BOT_NAMES
                for c in pr_obj.commits()
            ):
                pr_obj.create_comment(
                    "I see that this PR has conflicts, and I'm the only committer. "
                    "I'm going to close this PR and will make another one as"
                    f" appropriate. This was generated by {get_bot_run_url()} - "
                    "please use this URL for debugging,",
                )
                pr_obj.close()

                delete_branch(pr_json=pr_json, dry_run=dry_run)

                pr_json = lazy_update_pr_json(pr_json)
                d = dict(pr_json)

                # This will cause the _update_nodes_with_bot_rerun to trigger
                # properly and shouldn't be overridden since
                # this is the last function to run, the long term solution here
                # is to add the bot to conda-forge and then
                # it should have label adding capability and we can just add
                # the label properly
                d["labels"].append(BOT_RERUN_LABEL)

        return d

    return None
