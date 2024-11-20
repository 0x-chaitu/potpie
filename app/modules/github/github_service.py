import asyncio
import logging
import os
import random
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import chardet
import requests
from fastapi import HTTPException
from github import Github
from github.Auth import AppAuth
from redis import Redis
from sqlalchemy.orm import Session

from app.core.config_provider import config_provider
from app.modules.projects.projects_service import ProjectService
from app.modules.users.user_model import User

logger = logging.getLogger(__name__)


class GithubService:
    gh_token_list: List[str] = []

    @classmethod
    def initialize_tokens(cls):
        token_string = os.getenv("GH_TOKEN_LIST", "")
        cls.gh_token_list = [
            token.strip() for token in token_string.split(",") if token.strip()
        ]
        if not cls.gh_token_list:
            raise ValueError(
                "GitHub token list is empty or not set in environment variables"
            )
        logger.info(f"Initialized {len(cls.gh_token_list)} GitHub tokens")

    def __init__(self, db: Session):
        self.db = db
        self.project_manager = ProjectService(db)
        if not GithubService.gh_token_list:
            GithubService.initialize_tokens()
        self.redis = Redis.from_url(config_provider.get_redis_url())
        self.max_workers = 10
        self.max_depth = 4
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers)

    def get_github_repo_details(self, repo_name: str) -> Tuple[Github, Dict, str]:
        private_key = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            + config_provider.get_github_key()
            + "\n-----END RSA PRIVATE KEY-----\n"
        )
        app_id = os.environ["GITHUB_APP_ID"]
        auth = AppAuth(app_id=app_id, private_key=private_key)
        jwt = auth.create_jwt()
        owner = repo_name.split("/")[0]

        url = f"https://api.github.com/repos/{owner}/{repo_name.split('/')[1]}/installation"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {jwt}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            raise HTTPException(
                status_code=400, detail=f"Failed to get installation ID for {repo_name}"
            )

        app_auth = auth.get_installation_auth(response.json()["id"])
        github = Github(auth=app_auth)

        return github, response.json(), owner

    def get_file_content(
        self,
        repo_name: str,
        file_path: str,
        start_line: int,
        end_line: int,
        branch_name: str,
    ) -> str:
        logger.info(f"Attempting to access file: {file_path} in repo: {repo_name}")

        # Clean up the file path
        path_parts = file_path.split("/")
        if len(path_parts) > 1 and "-" in path_parts[0]:
            path_parts = path_parts[1:]
        clean_file_path = "/".join(path_parts)

        logger.info(f"Cleaned file path: {clean_file_path}")

        try:
            # Try authenticated access first
            github, repo = self.get_repo(repo_name)
            file_contents = repo.get_contents(clean_file_path, ref=branch_name)
        except Exception as private_error:
            logger.info(f"Failed to access private repo: {str(private_error)}")
            # If authenticated access fails, try public access
            try:
                github = self.get_public_github_instance()
                repo = github.get_repo(repo_name)
                file_contents = repo.get_contents(clean_file_path)
            except Exception as public_error:
                logger.error(f"Failed to access public repo: {str(public_error)}")
                raise HTTPException(
                    status_code=404,
                    detail=f"Repository or file not found or inaccessible: {repo_name}/{clean_file_path}",
                )

        if isinstance(file_contents, list):
            raise HTTPException(
                status_code=400, detail="Provided path is a directory, not a file"
            )

        try:
            content_bytes = file_contents.decoded_content
            encoding = self._detect_encoding(content_bytes)
            decoded_content = content_bytes.decode(encoding)
            lines = decoded_content.splitlines()

            if (start_line == end_line == 0) or (start_line == end_line == None):
                return decoded_content
            # added -2 to start and end line to include the function definition/ decorator line
            start = start_line - 2 if start_line - 2 > 0 else 0
            selected_lines = lines[start:end_line]
            return "\n".join(selected_lines)
        except Exception as e:
            logger.error(
                f"Error processing file content for {repo_name}/{clean_file_path}: {e}",
                exc_info=True,
            )
            raise HTTPException(
                status_code=500,
                detail=f"Error processing file content: {str(e)}",
            )

    @staticmethod
    def _detect_encoding(content_bytes: bytes) -> str:
        detection = chardet.detect(content_bytes)
        encoding = detection["encoding"]
        confidence = detection["confidence"]

        if not encoding or confidence < 0.5:
            raise HTTPException(
                status_code=400,
                detail="Unable to determine file encoding or low confidence",
            )

        return encoding

    def get_github_oauth_token(self, uid: str) -> str:
        user = self.db.query(User).filter(User.uid == uid).first()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        return user.provider_info["access_token"]

    async def get_repos_for_user(self, user_id: str):
        try:
            user = self.db.query(User).filter(User.uid == user_id).first()
            if user is None:
                raise HTTPException(status_code=404, detail="User not found")

            firebase_uid = user.uid  # Assuming `uid` is the Firebase UID
            github_username = user.provider_username

            if not github_username:
                raise HTTPException(
                    status_code=400, detail="GitHub username not found for this user"
                )

            # Retrieve GitHub OAuth token from Firestore
            github_oauth_token = self.get_github_oauth_token(firebase_uid)
            if not github_oauth_token:
                raise HTTPException(
                    status_code=400, detail="GitHub OAuth token not found for this user"
                )

            # Initialize GitHub client with user's OAuth token
            user_github = Github(github_oauth_token)
            user_orgs = user_github.get_user().get_orgs()
            org_logins = [org.login.lower() for org in user_orgs]

            # Authenticate as GitHub App
            private_key = (
                "-----BEGIN RSA PRIVATE KEY-----\n"
                + config_provider.get_github_key()
                + "\n-----END RSA PRIVATE KEY-----\n"
            )
            app_id = os.environ["GITHUB_APP_ID"]

            auth = AppAuth(app_id=app_id, private_key=private_key)
            jwt = auth.create_jwt()
            installations_url = "https://api.github.com/app/installations"
            headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {jwt}",
                "X-GitHub-Api-Version": "2022-11-28",
            }

            response = requests.get(installations_url, headers=headers)

            if response.status_code != 200:
                logger.error(f"Failed to get installations. Response: {response.text}")
                raise HTTPException(
                    status_code=response.status_code,
                    detail=f"Failed to get installations: {response.text}",
                )

            all_installations = response.json()

            # Filter installations: user's personal installation + org installations where user is a member
            user_installations = []
            for installation in all_installations:
                account = installation["account"]
                account_login = account["login"].lower()
                account_type = account["type"]  # 'User' or 'Organization'

                if account_type == "User" and account_login == github_username.lower():
                    user_installations.append(installation)
                elif account_type == "Organization" and account_login in org_logins:
                    user_installations.append(installation)

            repos = []
            for installation in user_installations:
                app_auth = auth.get_installation_auth(installation["id"])
                github = Github(auth=app_auth)
                repos_url = installation["repositories_url"]
                repos_response = requests.get(
                    repos_url, headers={"Authorization": f"Bearer {app_auth.token}"}
                )
                if repos_response.status_code == 200:
                    repos.extend(repos_response.json().get("repositories", []))
                else:
                    logger.error(
                        f"Failed to fetch repositories for installation ID {installation['id']}. Response: {repos_response.text}"
                    )

            # Remove duplicate repositories if any
            unique_repos = {repo["id"]: repo for repo in repos}.values()

            repo_list = [
                {
                    "id": repo["id"],
                    "name": repo["name"],
                    "full_name": repo["full_name"],
                    "private": repo["private"],
                    "url": repo["html_url"],
                    "owner": repo["owner"]["login"],
                }
                for repo in unique_repos
            ]

            return {"repositories": repo_list}

        except Exception as e:
            logger.error(f"Failed to fetch repositories: {str(e)}", exc_info=True)
            raise HTTPException(
                status_code=500, detail=f"Failed to fetch repositories: {str(e)}"
            )

    async def get_combined_user_repos(self, user_id: str):
        parsed_repos = await self.project_manager.list_projects(user_id)
        project_list = [
            {
                "id": project["id"],
                "name": project["repo_name"].split("/")[-1],
                "full_name": project["repo_name"],
                "private": False,
                "url": f"https://github.com/{project['repo_name']}",
                "owner": project["repo_name"].split("/")[0],
            }
            for project in parsed_repos
        ]
        user_repo_response = await self.get_repos_for_user(user_id)
        user_repos = user_repo_response["repositories"]
        combined_repos = {"repositories": project_list + user_repos}
        combined_repos["repositories"] = list(reversed(combined_repos["repositories"]))
        return combined_repos

    async def get_branch_list(self, repo_name: str):
        try:
            github, repo = self.get_repo(repo_name)
            default_branch = repo.default_branch
            branches = repo.get_branches()
            branch_list = [
                branch.name for branch in branches if branch.name != default_branch
            ]
            return {"branches": [default_branch] + branch_list}
        except HTTPException as he:
            raise he
        except Exception as e:
            logger.error(
                f"Error fetching branches for repo {repo_name}: {str(e)}", exc_info=True
            )
            raise HTTPException(
                status_code=404,
                detail=f"Repository not found or error fetching branches: {str(e)}",
            )

    @classmethod
    def get_public_github_instance(cls):
        if not cls.gh_token_list:
            cls.initialize_tokens()
        token = random.choice(cls.gh_token_list)
        return Github(token)

    def get_repo(self, repo_name: str) -> Tuple[Github, Any]:
        try:
            # Try authenticated access first
            github, _, _ = self.get_github_repo_details(repo_name)
            repo = github.get_repo(repo_name)

            return github, repo
        except Exception as private_error:
            logger.info(
                f"Failed to access private repo {repo_name}: {str(private_error)}"
            )
            # If authenticated access fails, try public access
            try:
                github = self.get_public_github_instance()
                repo = github.get_repo(repo_name)
                return github, repo
            except Exception as public_error:
                logger.error(
                    f"Failed to access public repo {repo_name}: {str(public_error)}"
                )
                raise HTTPException(
                    status_code=404,
                    detail=f"Repository {repo_name} not found or inaccessible on GitHub",
                )

    async def get_project_structure_async(
        self, project_id: str, path: Optional[str] = None
    ) -> str:
        logger.info(
            f"Fetching project structure for project ID: {project_id}, path: {path}"
        )

        # Modify cache key to reflect that we're only caching the specific path
        cache_key = (
            f"project_structure:{project_id}:exact_path_{path}:depth_{self.max_depth}"
        )
        cached_structure = self.redis.get(cache_key)

        if cached_structure:
            logger.info(
                f"Project structure found in cache for project ID: {project_id}, path: {path}"
            )
            return cached_structure.decode("utf-8")

        project = await self.project_manager.get_project_from_db_by_id(project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        repo_name = project["project_name"]
        if not repo_name:
            raise HTTPException(
                status_code=400, detail="Project has no associated GitHub repository"
            )

        try:
            github, repo = self.get_repo(repo_name)

            # If path is provided, verify it exists
            if path:
                try:
                    # Check if the path exists in the repository
                    repo.get_contents(path)
                except Exception:
                    raise HTTPException(
                        status_code=404, detail=f"Path {path} not found in repository"
                    )

            # Start structure fetch from the specified path with depth 0
            structure = await self._fetch_repo_structure_async(
                repo, path or "", current_depth=0, base_path=path
            )
            formatted_structure = self._format_tree_structure(structure)

            self.redis.setex(cache_key, 3600, formatted_structure)  # Cache for 1 hour

            return formatted_structure
        except HTTPException as he:
            raise he
        except Exception as e:
            logger.error(
                f"Error fetching project structure for {repo_name}: {str(e)}",
                exc_info=True,
            )
            raise HTTPException(
                status_code=500, detail=f"Failed to fetch project structure: {str(e)}"
            )

    async def _fetch_repo_structure_async(
        self,
        repo: Any,
        path: str = "",
        current_depth: int = 0,
        base_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        exclude_extensions = [
            "png",
            "jpg",
            "jpeg",
            "gif",
            "bmp",
            "tiff",
            "webp",
            "ico",
            "svg",
            "mp4",
            "avi",
            "mov",
            "wmv",
            "flv",
            "ipynb",
            "zlib",
        ]

        # Calculate current depth relative to base_path
        if base_path:
            # If we have a base_path, calculate depth relative to it
            relative_path = path[len(base_path) :].strip("/")
            current_depth = len(relative_path.split("/")) if relative_path else 0
        else:
            # If no base_path, calculate depth from root
            current_depth = len(path.split("/")) if path else 0

        # If we've reached max depth, return truncated indicator
        if current_depth >= self.max_depth:
            return {
                "type": "directory",
                "name": path.split("/")[-1] or repo.name,
                "children": [{"type": "file", "name": "...", "path": "truncated"}],
            }

        structure = {
            "type": "directory",
            "name": path.split("/")[-1] or repo.name,
            "children": [],
        }

        try:
            contents = await asyncio.get_event_loop().run_in_executor(
                self.executor, repo.get_contents, path
            )

            if not isinstance(contents, list):
                contents = [contents]

            # Filter out files with excluded extensions
            contents = [
                item
                for item in contents
                if item.type == "dir"
                or not any(item.name.endswith(ext) for ext in exclude_extensions)
            ]

            tasks = []
            for item in contents:
                # Only process items within the base_path if it's specified
                if base_path and not item.path.startswith(base_path):
                    continue

                if item.type == "dir":
                    task = self._fetch_repo_structure_async(
                        repo,
                        item.path,
                        current_depth=current_depth,
                        base_path=base_path,
                    )
                    tasks.append(task)
                else:
                    structure["children"].append(
                        {
                            "type": "file",
                            "name": item.name,
                            "path": item.path,
                        }
                    )

            if tasks:
                children = await asyncio.gather(*tasks)
                structure["children"].extend(children)

        except Exception as e:
            logger.error(f"Error fetching contents for path {path}: {str(e)}")

        return structure

    def _format_tree_structure(
        self, structure: Dict[str, Any], prefix: str = ""
    ) -> str:
        output = []
        name = structure["name"]
        if prefix:
            output.append(f"{prefix[:-1]}└── {name}")
        else:
            output.append(name)

        children = sorted(structure["children"], key=lambda x: (x["type"], x["name"]))
        for i, child in enumerate(children):
            is_last = i == len(children) - 1
            new_prefix = prefix + ("    " if is_last else "│   ")

            if child["type"] == "directory":
                output.append(self._format_tree_structure(child, new_prefix))
            else:
                output.append(f"{new_prefix[:-1]}└── {child['name']}")

        return "\n".join(output)
