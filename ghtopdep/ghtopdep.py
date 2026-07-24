import calendar
import json
import os
import sys
import textwrap
import datetime
from email.utils import formatdate, parsedate
from urllib.parse import urlparse

import appdirs
import click
import github3
import pipdate
import requests
from urllib3.util.retry import Retry
from cachecontrol.caches import FileCache
from cachecontrol.heuristics import BaseHeuristic
from cachecontrol import CacheControl, CacheControlAdapter
from tqdm import tqdm
from selectolax.parser import HTMLParser
from tabulate import tabulate

from ghtopdep import __version__


PACKAGE_NAME = "ghtopdep"
CACHE_DIR = appdirs.user_cache_dir(PACKAGE_NAME)
NEXT_BUTTON_SELECTOR = "#dependents > div.paginate-container > div > a"
ITEM_SELECTOR = "#dependents > div.Box > div.flex-items-center"
REPO_SELECTOR = "span > a.text-bold"
STARS_SELECTOR = "div > span:nth-child(1)"
GITHUB_URL = "https://github.com"
REPOS_PER_PAGE = 30


if pipdate.needs_checking(PACKAGE_NAME):
    msg = pipdate.check(PACKAGE_NAME, __version__.__version__)
    click.echo(msg)


class OneDayHeuristic(BaseHeuristic):
    cacheable_by_default_statuses = {
        200,
        203,
        204,
        206,
        300,
        301,
        404,
        405,
        410,
        414,
        501,
    }

    def update_headers(self, response):
        if response.status not in self.cacheable_by_default_statuses:
            return {}

        date = parsedate(response.headers["date"])
        expires = datetime.datetime(*date[:6]) + datetime.timedelta(days=1)

        return {
            "expires": formatdate(calendar.timegm(expires.timetuple())),
            "cache-control": "public",
        }

    def warning(self, response):
        msg = "Automatically cached! Response is Stale."
        return "110 - {0}".format(msg)


def normalize_repo_url(url):
    """
    Normalize a repository URL by removing:

    - trailing slashes
    - a trailing ".git" suffix

    Examples:
        https://github.com/owner/repo/
        -> https://github.com/owner/repo

        https://github.com/owner/repo.git
        -> https://github.com/owner/repo

        https://github.com/owner/repo.git/
        -> https://github.com/owner/repo
    """
    parsed_url = urlparse(url.strip())

    path = parsed_url.path.rstrip("/")

    if path.endswith(".git"):
        path = path[:-4]

    path = path.rstrip("/")

    return "{0}://{1}{2}".format(
        parsed_url.scheme,
        parsed_url.netloc,
        path,
    )


def parse_repo_url(url):
    parsed_url = urlparse(url)
    path_parts = parsed_url.path.strip("/").split("/")

    if len(path_parts) != 2 or not all(path_parts):
        raise click.BadParameter(
            "Repository URL must look like "
            "https://github.com/owner/repository"
        )

    return path_parts[0], path_parts[1]


def already_added(repo_url, repos):
    for repo in repos:
        if repo["url"] == repo_url:
            return True

    return False


def fetch_description(gh, relative_url):
    _, owner, repository = relative_url.split("/")
    repository = gh.repository(owner, repository)

    repo_description = " "

    if repository.description:
        repo_description = textwrap.shorten(
            repository.description,
            width=60,
            placeholder="...",
        )

    return repo_description


def sort_repos(repos, rows):
    sorted_repos = sorted(
        repos,
        key=lambda item: item["stars"],
        reverse=True,
    )

    return sorted_repos[:rows]


def humanize(num):
    if num < 1_000:
        return num
    elif num < 10_000:
        return "{}K".format(round(num / 100) / 10)
    elif num < 1_000_000:
        return "{}K".format(round(num / 1_000))
    else:
        return num


def readable_stars(repos):
    for repo in repos:
        repo["stars"] = humanize(repo["stars"])

    return repos


def show_result(
    repos,
    total_repos_count,
    more_than_zero_count,
    destinations,
    table,
):
    if table:
        if repos:
            repos = readable_stars(repos)

            click.echo(
                tabulate(
                    repos,
                    headers="keys",
                    tablefmt="github",
                )
            )

            click.echo(
                "found {0} {1} others {2} are private".format(
                    total_repos_count,
                    destinations,
                    destinations,
                )
            )

            click.echo(
                "found {0} {1} with more than zero star".format(
                    more_than_zero_count,
                    destinations,
                )
            )
        else:
            click.echo(
                "Doesn't find any {0} that match search request".format(
                    destinations
                )
            )
    else:
        click.echo(json.dumps(repos))


def get_max_deps(sess, url):
    main_response = sess.get(url)
    main_response.raise_for_status()

    parsed_node = HTMLParser(main_response.text)

    deps_count_element = parsed_node.css_first(
        ".table-list-header-toggle .btn-link.selected"
    )

    if deps_count_element is None:
        raise click.ClickException(
            "Unable to determine the number of dependents. "
            "GitHub may have changed the page structure."
        )

    max_deps = int(
        deps_count_element.text()
        .strip()
        .split()[0]
        .replace(",", "")
    )

    return max_deps


@click.command()
@click.argument("url")
@click.option(
    "--repositories/--packages",
    default=True,
    help="Sort repositories or packages (default repositories)",
)
@click.option(
    "--table/--json",
    default=True,
    help="View mode",
)
@click.option(
    "--report",
    is_flag=True,
    help="Report",
)
@click.option(
    "--description",
    is_flag=True,
    help=(
        "Show description of packages or repositories "
        "(performs additional request per repository)"
    ),
)
@click.option(
    "--rows",
    default=10,
    help="Number of showing repositories (default=10)",
)
@click.option(
    "--minstar",
    default=5,
    help="Minimum number of stars (default=5)",
)
@click.option(
    "--search",
    help="search code at dependents (repositories/packages)",
)
@click.option(
    "--token",
    envvar="GHTOPDEP_TOKEN",
)
def cli(
    url,
    repositories,
    search,
    table,
    rows,
    minstar,
    report,
    description,
    token,
):
    url = normalize_repo_url(url)
    owner, repository = parse_repo_url(url)

    mode = os.environ.get("GHTOPDEP_ENV")
    base_url = "http://159.223.231.170"

    if mode == "development":
        base_url = "http://127.0.0.1:3000"

    if report:
        try:
            result = requests.get(
                "{}/repos/{}/{}".format(
                    base_url,
                    owner,
                    repository,
                )
            )

            if result.status_code != 404:
                result.raise_for_status()

                sorted_repos = sort_repos(
                    result.json(),
                    rows,
                )
                repos = readable_stars(sorted_repos)

                click.echo(
                    tabulate(
                        repos,
                        headers="keys",
                        tablefmt="github",
                    )
                )

                sys.exit()
        except requests.exceptions.ConnectionError as exc:
            click.echo(exc)
        except requests.exceptions.RequestException as exc:
            click.echo(exc)

    if (description or search) and token:
        gh = github3.login(token=token)

        CacheControl(
            gh.session,
            cache=FileCache(CACHE_DIR),
            heuristic=OneDayHeuristic(),
        )
    elif description or search:
        click.echo("Please provide token")
        sys.exit()

    destination = "repository"
    destinations = "repositories"

    if not repositories:
        destination = "package"
        destinations = "packages"

    repos = []
    more_than_zero_count = 0
    total_repos_count = 0

    sess = requests.session()

    retries = Retry(
        total=15,
        backoff_factor=15,
        status_forcelist=[429],
    )

    adapter = CacheControlAdapter(
        max_retries=retries,
        cache=FileCache(CACHE_DIR),
        heuristic=OneDayHeuristic(),
    )

    sess.mount("http://", adapter)
    sess.mount("https://", adapter)

    page_url = (
        "{0}/network/dependents?dependent_type={1}".format(
            url,
            destination.upper(),
        )
    )

    max_deps = get_max_deps(sess, page_url)
    pbar = tqdm(total=max_deps)

    while True:
        response = sess.get(page_url)
        response.raise_for_status()

        parsed_node = HTMLParser(response.text)
        dependents = parsed_node.css(ITEM_SELECTOR)

        total_repos_count += len(dependents)

        for dep in dependents:
            repo_stars_list = dep.css(STARS_SELECTOR)

            # Only for ghost, private repositories, or packages.
            if repo_stars_list:
                repo_stars = repo_stars_list[0].text().strip()
                repo_stars_num = int(
                    repo_stars.replace(",", "")
                )
            else:
                continue

            if repo_stars_num != 0:
                more_than_zero_count += 1

            if repo_stars_num >= minstar:
                repo_elements = dep.css(REPO_SELECTOR)

                if not repo_elements:
                    continue

                relative_repo_url = repo_elements[0].attributes["href"]
                repo_url = "{0}{1}".format(
                    GITHUB_URL,
                    relative_repo_url,
                )

                # The same repository can be listed multiple times.
                is_already_added = already_added(
                    repo_url,
                    repos,
                )

                if not is_already_added and repo_url != url:
                    repo_data = {
                        "url": repo_url,
                        "stars": repo_stars_num,
                    }

                    if description:
                        repo_data["description"] = fetch_description(
                            gh,
                            relative_repo_url,
                        )

                    repos.append(repo_data)

        pagination_buttons = parsed_node.css(
            NEXT_BUTTON_SELECTOR
        )

        if len(pagination_buttons) == 2:
            page_url = pagination_buttons[1].attributes["href"]
        elif (
            pagination_buttons
            and pagination_buttons[0].text().strip() == "Next"
        ):
            page_url = pagination_buttons[0].attributes["href"]
        elif (
            not pagination_buttons
            or pagination_buttons[0].text().strip() == "Previous"
        ):
            break

        pbar.update(REPOS_PER_PAGE)

    pbar.close()

    if report:
        try:
            requests.post(
                "{}/repos".format(base_url),
                json={
                    "url": url,
                    "owner": owner,
                    "repository": repository,
                    "deps": repos,
                },
            )
        except requests.exceptions.ConnectionError as exc:
            click.echo(exc)

    sorted_repos = sort_repos(
        repos,
        rows,
    )

    if search:
        for repo in repos:
            repo_path = urlparse(repo["url"]).path[1:]

            for search_result in gh.search_code(
                "{0} repo:{1}".format(
                    search,
                    repo_path,
                )
            ):
                click.echo(
                    "{0} with {1} stars".format(
                        search_result.html_url,
                        repo["stars"],
                    )
                )
    else:
        show_result(
            sorted_repos,
            total_repos_count,
            more_than_zero_count,
            destinations,
            table,
        )


if __name__ == "__main__":
    cli()
